# -*- coding: utf-8 -*-
"""ETL：C2DB 筛选结果 xlsx -> SQLite 三子库。

    python 数据库/etl.py                 # 只导"保留数据"（3,354 条，默认）
    python 数据库/etl.py --scope all     # 保留 + 候选 + 剔除（17,001 条）

规矩：
  * 缺失写 NULL，原因写 data_gap，绝不把"缺失"字符串写进库
  * 源表没有的字段（名称/晶相/迁移率/有效质量/介电常数）不推断、不替代
  * 每个数值都带方法/泛函（property_dictionary）与出处（CIF 哈希 + source_url）
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from openpyxl import load_workbook  # noqa: E402

import db as dbmod  # noqa: E402
from models import (  # noqa: E402
    Z_OF, cell_volume, compose_text, crystal_system_of, parse_formula,
)

SOURCE_XLSX = HERE / "data" / "C2DB_筛选结果_20261031.xlsx"
SOURCE_ROWS = 17001
CIF_DIR = HERE / "data" / "cif"
EMASS_JSON = HERE / "data" / "emass.json"      # 有效质量补抓（emass_cbm / emass_vbm）

SHEETS_BY_SCOPE = {
    "kept": ["保留数据"],
    "all": ["保留数据", "待补充候选", "剔除或低完整度"],
}

# ---------------------------------------------------------------- 列索引（61 列）
C = {
    "uid": 0, "formula": 1, "reduced": 2, "sg": 4,
    "a": 5, "b": 6, "c": 7, "alpha": 8, "beta": 9, "gamma": 10,
    "thickness": 11, "area": 12,
    "gap": 13, "gap_dir": 14, "hse": 15, "gw": 16,
    "alphaz_el": 17, "alphaz_lat": 18, "alphaz": 19, "evac": 20,
    "dyn": 21, "ehull": 22, "hform": 23, "energy": 24,
    "gap_kind": 25, "cif_url": 27, "src_url": 28,
    "missing_fields": 30, "alphaz_check": 31,
    "ml_id": 32, "ml_formula": 33, "ml_name": 34, "ml_comp": 35, "ml_cat": 36,
    "score": 55, "verdict": 56, "verdict_reason": 57,
}

HEADER_CHECKS = {
    0: "uid (C2DB UID)", 13: "gap (eV, PBE)", 21: "dyn_stab",
    25: "direct_indirect_PBE", 27: "structure_download_url",
    30: "missing_fields", 32: "材料主库_ID", 55: "完整度评分", 56: "筛选结论",
}

# ---------------------------------------------------------------- 类别解析
CATEGORY_RE = re.compile(r"^(单质|二元化合物|三元化合物|多元化合物)（(含|不含)金属；元素\s*(.+?)）$")

# ---------------------------------------------------------------- 缺失登记
ITEM_RE = re.compile(r"([A-Za-z_]+)\(([^()]*)\)")
SRCKEY_TO_PROP = {
    "gap": "gap_pbe", "gap_dir": "gap_dir_pbe", "gap_hse": "gap_hse06", "gap_gw": "gap_gw",
    "alphaz_el": "polarizability_el", "alphaz_lat": "polarizability_lat",
    "alphaz": "polarizability_total", "dyn_stab": "dyn_stab",
    "ehull": "ehull", "hform": "hform", "energy": "energy",
}
REASON_MAP = {"未计算": "not_computed", "字段不存在": "no_field",
              "Unknown": "unknown", "不适用": "not_applicable"}

# ---------------------------------------------------------------- 字典：性质 -> 方法/泛函
# (key, 中文名, 表, 列, 单位, 方法, 泛函, 起始泛函, 精度, 源表列, origin, availability, 备注)
PROPS = [
    ("gap_pbe", "带隙（PBE）", "electrical", "gap_pbe", "eV", "PBE", "PBE", "PBE", 3,
     "gap (eV, PBE)", "c2db_table", "source", "0.000 是有效值（零带隙），与“未计算”严格区分"),
    ("gap_dir_pbe", "最小直接带隙（PBE）", "electrical", "gap_dir_pbe", "eV", "PBE", "PBE", "PBE", 3,
     "gap_dir (eV, PBE)", "c2db_table", "source", "与 gap_pbe 同法同精度；两者相等即直接带隙"),
    ("gap_hse06", "带隙（HSE06）", "electrical", "gap_hse06", "eV", "HSE06", "HSE06@PBE", "PBE", 3,
     "gap_hse (eV, HSE06)", "c2db_table", "source", "杂化泛函，非自洽 HSE06 起点为 PBE"),
    ("gap_gw", "带隙（G0W0）", "electrical", "gap_gw", "eV", "G0W0", "G0W0@PBE", "PBE", 3,
     "gap_gw (eV, G0W0)", "c2db_table", "source", "仅少数材料计算"),
    ("mobility_e", "电子迁移率", "electrical", "mobility_e", "cm^2/(V*s)", "待定", None, None, None,
     "官网无迁移率键（help 88 键核查 2026-10-31）", "no_field", "no_field",
     "C2DB 检索键与详情页均无迁移率；不许用带隙或有效质量替代"),
    ("mobility_h", "空穴迁移率", "electrical", "mobility_h", "cm^2/(V*s)", "待定", None, None, None,
     "官网无迁移率键", "no_field", "no_field", "同上"),
    ("eff_mass_cbm", "电子有效质量（CBM·DOS）", "electrical", "eff_mass_cbm", "m0", "PBE", "PBE", "PBE", 3,
     "emass_cbm（表格）= CBM DOS effective mass (PBE)（详情页）", "c2db_table", "source",
     "DOS 有效质量；详情页另有 Min/Max 有效质量与晶体坐标，本库只收 DOS 值"),
    ("eff_mass_vbm", "空穴有效质量（VBM·DOS）", "electrical", "eff_mass_vbm", "m0", "PBE", "PBE", "PBE", 3,
     "emass_vbm（表格）= VBM DOS effective mass (PBE)（详情页）", "c2db_table", "source",
     "DOS 有效质量；与 CBM 同一套 PBE 计算"),
    ("dielectric_static", "介电常数（静态）", "electrical", "dielectric_static", "1", "待定", None, None, None,
     "官网无 εr 键（help 88 键核查 2026-10-31）", "no_field", "no_field",
     "C2DB 不提供介电常数；alphaz_el/lat/alphaz 是二维极化率（单位 Å），明确不作为介电常数"),
    ("polarizability_el", "极化率（面外·电子）", "electrical", "polarizability_el", "Å", "RPA", "RPA", "PBE", 3,
     "alphaz_el (Å)", "c2db_table", "source", "极化率，不是介电常数"),
    ("polarizability_lat", "极化率（面外·晶格）", "electrical", "polarizability_lat", "Å", "RPA", "RPA", "PBE", 3,
     "alphaz_lat (Å)", "c2db_table", "source", "极化率，不是介电常数"),
    ("polarizability_total", "极化率（面外·总）", "electrical", "polarizability_total", "Å", "RPA", "RPA", "PBE", 3,
     "alphaz (Å)", "c2db_table", "source", "极化率，不是介电常数"),
    ("ehull", "能量高于凸包", "stability", "ehull", "eV/atom", "PBE", "PBE", "PBE", 3,
     "ehull (eV/atom)", "c2db_table", "source", "越小越稳定"),
    ("hform", "形成能", "stability", "hform", "eV/atom", "PBE", "PBE", "PBE", 3,
     "hform (eV/atom)", "c2db_table", "source", None),
    ("energy", "总能量", "stability", "energy", "eV", "PBE", "PBE", "PBE", 3,
     "energy (eV)", "c2db_table", "source", "单胞总能量，跨材料不可直接比较"),
]

# ---------------------------------------------------------------- 字典：字段级（非数值列 + 元数据）
# (表, 列, 中文名, 子库, 单位, origin, 源表列, 缺失口径, 备注)
MANUAL_FIELDS = [
    ("material", "material_id", "材料ID（C2DB UID）", "材料主库", None, "c2db_table",
     "uid (C2DB UID)", "主键不可缺", "全库唯一；三个子库的关联键"),
    ("material", "formula", "化学式", "材料主库", None, "c2db_table", "formula", "空白→缺失", "C2DB 原始化学式"),
    ("material", "reduced_formula", "约化化学式", "材料主库", None, "c2db_table", "reduced_formula", "空白→缺失", None),
    ("material", "name", "名称", "材料主库", None, "derived", "—", "源表无名称字段",
     "用 formula 生成的临时名（name_source 标注）"),
    ("material", "name_source", "名称来源", "材料主库", None, "derived", "—", "—",
     "formula_temp=C2DB 无名称字段，用化学式代称"),
    ("material", "composition", "组成", "材料主库", None, "derived", "formula（解析）", "—",
     "由化学式解析，未做数值推断"),
    ("material", "composition_json", "组成（机器可读）", "材料主库", None, "derived", "formula（解析）", "—",
     "{\"元素\": 化学计量比}"),
    ("material", "n_elements", "元素种类数", "材料主库", "种", "derived", "formula（解析）", "—", None),
    ("material", "category", "类别", "材料主库", None, "derived", "formula（元素规则）", "源表无类别字段",
     "规则推断：单质/二元/三元/多元；不是实验类别"),
    ("material", "has_metal", "是否含金属", "材料主库", None, "derived", "formula（元素规则）", "—", "规则推断"),
    ("material", "category_note", "类别推断原文", "材料主库", None, "derived", "—", "—", "留档推断依据（含元素列表）"),
    ("material", "score", "完整度评分", "材料主库", "分", "derived", "—", "—", "满分 100，见筛选规则"),
    ("material", "verdict", "筛选结论", "材料主库", None, "derived", "—", "—", "保留/候选/剔除"),
    ("material", "verdict_reason", "筛选理由", "材料主库", None, "derived", "—", "—", "写明硬性条件与分值归属"),
    ("material", "source_url", "官网详情页", "材料主库", None, "c2db_detail", "source_url", "—", None),
    ("structure", "a", "晶格常数 a", "结构信息库", "Å", "cif", "a (Å)", "获取失败→缺失", "官网显示 3 位小数"),
    ("structure", "b", "晶格常数 b", "结构信息库", "Å", "cif", "b (Å)", "获取失败→缺失", "同上"),
    ("structure", "c", "晶格常数 c", "结构信息库", "Å", "cif", "c (Å)", "获取失败→缺失", "二维材料含真空层"),
    ("structure", "alpha", "晶格角 α", "结构信息库", "°", "cif", "alpha (°)", "获取失败→缺失", "同上"),
    ("structure", "beta", "晶格角 β", "结构信息库", "°", "cif", "beta (°)", "获取失败→缺失", "同上"),
    ("structure", "gamma", "晶格角 γ", "结构信息库", "°", "cif", "gamma (°)", "获取失败→缺失", "同上"),
    ("structure", "space_group", "空间群（体相 AA 堆叠）", "结构信息库", None, "c2db_table",
     "space_group_bulk_AA", "空白→缺失", "1–230"),
    ("structure", "crystal_system", "晶系（派生）", "结构信息库", None, "derived", "space_group",
     "由空间群推导", "辅助分类；不等于源表缺失的“晶相”"),
    ("structure", "crystal_phase", "晶相", "结构信息库", None, "no_field", "—", "源表无此字段",
     "C2DB 无“晶相/相结构”字段；恒为 NULL，不用空间群或化学式推断"),
    ("structure", "cif_url", "CIF 结构", "结构信息库", None, "c2db_detail", "structure_download_url",
     "获取失败→缺失", "逐条 /material/<uid>/download/cif"),
    ("structure", "cif_sha256", "CIF 内容哈希", "结构信息库", None, "cif", "—", "无缓存→缺失",
     "本地缓存文件 SHA256，用于确认结构文件未被替换"),
    ("structure", "cif_bytes", "CIF 字节数", "结构信息库", "byte", "cif", "—", "无缓存→缺失", None),
    ("structure", "cif_cache", "CIF 本地缓存路径", "结构信息库", None, "cif", "—", "无缓存→缺失", "相对项目根目录"),
    ("structure", "cell_volume", "晶胞体积（派生）", "结构信息库", "Å³", "derived", "a,b,c,α,β,γ",
     "六参数不全→缺失", "按三斜通式计算，含真空层体积"),
    ("structure", "origin_note", "数值出处说明", "结构信息库", None, "derived", "—", "—", None),
    ("electrical", "gap_kind_pbe", "直接/间接（PBE）", "电学性能子库", None, "derived",
     "direct_indirect_PBE", "带隙缺失→缺失", "gap 与 gap_dir 相等=直接，否则=间接"),
    ("stability", "dyn_stab", "动力学稳定性", "辅助库", None, "c2db_table", "dyn_stab",
     "Unknown→NULL", "Yes/No 有效；Unknown 不等于不稳定，单独记 unknown"),
]


def num(v):
    """只接受数值；'缺失'/'Unknown'/空串一律 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_emass() -> dict[str, tuple[float | None, float | None]]:
    """补抓的有效质量：uid -> (CBM DOS, VBM DOS)，单位 m0、方法 PBE。"""
    if not EMASS_JSON.exists():
        return {}
    doc = json.loads(EMASS_JSON.read_text(encoding="utf-8"))
    return {uid: (v.get("cbm"), v.get("vbm")) for uid, v in doc.get("values", {}).items()}


def validate_headers(hdr: list, sheet: str) -> None:
    if len(hdr) != 61:
        raise SystemExit(f"[中止] 工作表 {sheet} 列数 {len(hdr)} != 61，不是本工具认识的筛选结果格式")
    for idx, expect in HEADER_CHECKS.items():
        if hdr[idx] != expect:
            raise SystemExit(f"[中止] {sheet} 第 {idx} 列是 {hdr[idx]!r}，应为 {expect!r}")


def parse_category(raw: str | None) -> tuple[str | None, int | None, str | None]:
    """'二元化合物（含金属；元素 Tl、Cl）' -> ('二元化合物', 1, 原文)"""
    if not raw:
        return None, None, None
    m = CATEGORY_RE.match(raw)
    if not m:
        return None, None, raw
    has_metal = 1 if m.group(2) == "含" else 0
    return m.group(1), has_metal, raw


def iter_sheet_rows(ws, sheet: str):
    it = ws.iter_rows(min_row=2, values_only=True)
    for r in it:
        if not r or not r[C["uid"]]:
            continue
        yield r


def build(scope: str = "kept", db_path: Path | None = None, verbose: bool = True) -> dict:
    if scope not in SHEETS_BY_SCOPE:
        raise SystemExit(f"scope 只能是 {list(SHEETS_BY_SCOPE)}")
    if not SOURCE_XLSX.exists():
        raise SystemExit(f"找不到源文件 {SOURCE_XLSX}")

    db_path = db_path or dbmod.DB_PATH
    if db_path.exists():
        db_path.unlink()
    con = dbmod.connect(db_path)
    dbmod.init_schema(con)

    wb = load_workbook(SOURCE_XLSX, read_only=True, data_only=True)
    source_hash = sha256_file(SOURCE_XLSX)
    imported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    emass = load_emass()

    # ---- 字典（先于数据插入，data_gap 有外键指向它）
    prop_rows = [(k, n, t, col, u, m, f, fb, p, sc, o, a, note)
                 for (k, n, t, col, u, m, f, fb, p, sc, o, a, note) in PROPS]
    con.executemany(
        "INSERT INTO property_dictionary(property_key,name_cn,db_table,db_column,unit,method,"
        "functional,functional_base,precision,source_column,origin,availability,note) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", prop_rows)

    field_rows = []
    for (k, n, t, col, u, m, f, fb, p, sc, o, a, note) in PROPS:
        field_rows.append((f"{t}.{col}", t, col, n, "电学性能子库" if t == "electrical" else "辅助库",
                           u, o, sc, "见 property_dictionary", note))
    for (t, col, cn, sub, u, o, sc, mp, note) in MANUAL_FIELDS:
        field_rows.append((f"{t}.{col}", t, col, cn, sub, u, o, sc, mp, note))
    con.executemany(
        "INSERT INTO field_dictionary(column_key,table_name,column_name,name_cn,sublibrary,unit,"
        "origin,source_column,missing_policy,note) VALUES(?,?,?,?,?,?,?,?,?,?)", field_rows)

    cur = con.cursor()
    cur.execute("INSERT INTO import_batch(source_file,source_sha256,source_rows,scope,imported_rows,"
                "imported_at,note) VALUES(?,?,?,?,?,?,?)",
                (SOURCE_XLSX.name, source_hash, SOURCE_ROWS, scope, 0, imported_at,
                 f"来源 {SOURCE_XLSX.name} 的 sheet={'+'.join(SHEETS_BY_SCOPE[scope])}"))
    batch_id = cur.lastrowid

    materials, structures, electricals, stabilities, melements, gaps = [], [], [], [], [], []
    symbols: dict[str, int] = {}
    n_cif = 0
    per_sheet: dict[str, int] = {}

    for sheet in SHEETS_BY_SCOPE[scope]:
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        hdr = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        validate_headers(list(hdr), sheet)
        per_sheet[sheet] = 0
        for r in iter_sheet_rows(ws, sheet):
            uid = text(r[C["ml_id"]]) or text(r[C["uid"]])
            formula = text(r[C["ml_formula"]]) or text(r[C["formula"]]) or ""
            counts = parse_formula(formula)
            n_el = len(counts)
            cat, has_metal, cat_note = parse_category(text(r[C["ml_cat"]]))
            reduced = text(r[C["reduced"]])
            comp_text = text(r[C["ml_comp"]]) or compose_text(counts, reduced)
            verdict = text(r[C["verdict"]])
            score = num(r[C["score"]])

            materials.append((uid, formula, reduced, text(r[C["ml_name"]]) or formula, "formula_temp",
                              comp_text, json.dumps(counts, ensure_ascii=False, sort_keys=True),
                              n_el, cat, has_metal, cat_note, score, verdict,
                              text(r[C["verdict_reason"]]), text(r[C["src_url"]]), batch_id))

            a, b, c = num(r[C["a"]]), num(r[C["b"]]), num(r[C["c"]])
            al, be, ga = num(r[C["alpha"]]), num(r[C["beta"]]), num(r[C["gamma"]])
            sg = r[C["sg"]] if isinstance(r[C["sg"]], int) else None
            cif_file = CIF_DIR / f"{uid}.cif"
            if cif_file.exists():
                raw = cif_file.read_bytes()          # 官网下载的原文件，一字不改
                digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
                rel = cif_file.relative_to(HERE).as_posix()
                n_cif += 1
            else:
                raw = digest = size = rel = None
            structures.append((uid, a, b, c, al, be, ga, sg, crystal_system_of(sg), None,
                               text(r[C["cif_url"]]), digest, size, rel,
                               cell_volume(a, b, c, al, be, ga),
                               "a,b,c,α,β,γ 取自官网 CIF（3 位小数）；cell_volume 由六参数推导",
                               raw))

            em = emass.get(uid, (None, None))
            electricals.append((uid, num(r[C["gap"]]), num(r[C["gap_dir"]]), num(r[C["hse"]]),
                                num(r[C["gw"]]), text(r[C["gap_kind"]]),
                                None, None, em[0], em[1], None,
                                num(r[C["alphaz_el"]]), num(r[C["alphaz_lat"]]),
                                num(r[C["alphaz"]])))
            stabilities.append((uid, text(r[C["dyn"]]), num(r[C["ehull"]]),
                                num(r[C["hform"]]), num(r[C["energy"]])))

            for sym, cnt in counts.items():
                z = Z_OF[sym]
                symbols[sym] = z
                melements.append((uid, z, cnt))
                # 不存占比：fraction = cnt / Σcnt 是纯派生，查询时开窗算（schema v2）

            raw_missing = text(r[C["missing_fields"]]) or ""
            seen = set()
            for key, reason in ITEM_RE.findall(raw_missing):
                prop = SRCKEY_TO_PROP.get(key)
                if not prop or prop in seen:
                    continue
                seen.add(prop)
                gaps.append((uid, prop, REASON_MAP.get(reason, "not_computed"), reason))
            per_sheet[sheet] += 1

    con.executemany("INSERT INTO element(z,symbol) VALUES(?,?)",
                    sorted((z, s) for s, z in symbols.items()))
    con.executemany(
        "INSERT INTO material(material_id,formula,reduced_formula,name,name_source,composition,"
        "composition_json,n_elements,category,has_metal,category_note,score,verdict,"
        "verdict_reason,source_url,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", materials)
    con.executemany(
        "INSERT INTO structure(material_id,a,b,c,alpha,beta,gamma,space_group,crystal_system,"
        "crystal_phase,cif_url,cif_sha256,cif_bytes,cif_cache,cell_volume,origin_note,cif_content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", structures)
    con.executemany(
        "INSERT INTO electrical(material_id,gap_pbe,gap_dir_pbe,gap_hse06,gap_gw,gap_kind_pbe,"
        "mobility_e,mobility_h,eff_mass_cbm,eff_mass_vbm,dielectric_static,"
        "polarizability_el,polarizability_lat,polarizability_total) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", electricals)
    con.executemany("INSERT INTO stability(material_id,dyn_stab,ehull,hform,energy) VALUES(?,?,?,?,?)",
                    stabilities)
    con.executemany("INSERT INTO material_element(material_id,z,count) VALUES(?,?,?)",
                    melements)
    known = {p[0] for p in prop_rows}
    con.executemany("INSERT OR IGNORE INTO data_gap(material_id,property_key,reason,detail) "
                    "VALUES(?,?,?,?)", [g for g in gaps if g[1] in known])
    # 有效质量：C2DB 只对部分材料给出（多为金属/零带隙体系没算），逐行登记"未给出"
    for prop in ("eff_mass_cbm", "eff_mass_vbm"):
        cur.execute(
            "INSERT OR IGNORE INTO data_gap(material_id,property_key,reason,detail) "
            "SELECT material_id, ?, 'not_computed', 'C2DB 该材料未给出有效质量（≠0）' "
            "FROM electrical WHERE " + prop + " IS NULL", (prop,))
    cur.execute("UPDATE import_batch SET imported_rows=? WHERE batch_id=?",
                (len(materials), batch_id))
    con.commit()

    summary = {
        "db": str(db_path),
        "batch_id": batch_id,
        "scope": scope,
        "per_sheet": per_sheet,
        "material": len(materials),
        "structure": len(structures),
        "electrical": len(electricals),
        "stability": len(stabilities),
        "material_element": len(melements),
        "element": len(symbols),
        "data_gap": cur.execute("SELECT COUNT(*) FROM data_gap").fetchone()[0],
        "cif_hashed": n_cif,
        "source_sha256": source_hash,
    }
    con.close()
    if verbose:
        print("导入完成:", json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    scope = "kept"
    if "--scope" in argv:
        scope = argv[argv.index("--scope") + 1]
    build(scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
