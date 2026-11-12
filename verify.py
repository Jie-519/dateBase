# -*- coding: utf-8 -*-
"""库体检：把"不能错的地方"逐条断言，输出 数据库/体检报告.txt。

    python 数据库/verify.py

查什么（13 项）：
  1 行数/对象数   2 外键完整性   3 三子库 1:1 覆盖   4 主键唯一
  5 评分与结论     6 数值域       7 库内无"缺失"字符串  8 官网无字段必须为空
  9 有效质量补抓一致 10 极化率≠介电常数 11 方法/泛函齐全
  12 抽样逐值比对   13 CIF 哈希抽检
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from openpyxl import load_workbook  # noqa: E402

import db as dbmod  # noqa: E402
from models import DISPLAY_PRECISION, FORBIDDEN_TOKENS  # noqa: E402

SOURCE_XLSX = HERE / "data" / "C2DB_筛选结果_20261031.xlsx"
SHEET = "保留数据"
CIF_DIR = HERE / "data" / "cif"
REPORT = HERE / "体检报告.txt"
SUBLIB_TABLES = ["material", "structure", "electrical", "stability"]
COMPARE_COLS = ["a", "b", "c", "alpha", "beta", "gamma",
                "gap", "gap_dir", "hse", "gw", "score"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


def _src_rows() -> dict[str, tuple]:
    wb = load_workbook(SOURCE_XLSX, read_only=True, data_only=True)
    ws = wb[SHEET]
    it = ws.iter_rows(min_row=2, values_only=True)
    out = {}
    for r in it:
        if r and r[0]:
            out[str(r[0]).strip()] = r
    return out


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(db_path: Path | None = None, sample: int = 30, write_report: bool = True) -> dict:
    con = dbmod.connect(db_path, read_only=False)
    con.row_factory = sqlite3.Row
    q1 = lambda sql, p=(): con.execute(sql, p).fetchone()[0]  # noqa: E731
    checks: list[Check] = []
    src = _src_rows()
    n_src = len(src)

    # 1 ------------------------------------------------------------ 行数
    counts = {t: q1(f"SELECT COUNT(*) FROM {t}") for t in SUBLIB_TABLES}
    ok = all(v == n_src for v in counts.values())
    checks.append(Check("1 行数：三子库与源表保留行一一对应", ok,
                        f"源表保留 {n_src} 行；库内 " + "、".join(f"{k}={v}" for k, v in counts.items())))

    # 2 ------------------------------------------------------------ 外键
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    checks.append(Check("2 外键完整性", len(fk) == 0, f"foreign_key_check 返回 {len(fk)} 行违规"))

    # 3 ------------------------------------------------------------ 1:1 覆盖
    orphan = {
        "structure": q1("SELECT COUNT(*) FROM material m LEFT JOIN structure s "
                        "ON s.material_id=m.material_id WHERE s.material_id IS NULL"),
        "electrical": q1("SELECT COUNT(*) FROM material m LEFT JOIN electrical e "
                         "ON e.material_id=m.material_id WHERE e.material_id IS NULL"),
        "stability": q1("SELECT COUNT(*) FROM material m LEFT JOIN stability st "
                        "ON st.material_id=m.material_id WHERE st.material_id IS NULL"),
        "extra": q1("SELECT COUNT(*) FROM (SELECT material_id FROM structure "
                    "EXCEPT SELECT material_id FROM material)"),
    }
    checks.append(Check("3 三子库 1:1 覆盖（无孤儿行）", all(v == 0 for v in orphan.values()),
                        json.dumps(orphan, ensure_ascii=False)))

    # 4 ------------------------------------------------------------ 主键
    dup = q1("SELECT COUNT(*) - COUNT(DISTINCT material_id) FROM material")
    empty = q1("SELECT COUNT(*) FROM material WHERE material_id IS NULL OR TRIM(material_id)=''")
    checks.append(Check("4 主键唯一且非空", dup == 0 and empty == 0, f"重复 {dup}，空值 {empty}"))

    # 5 ------------------------------------------------------------ 评分
    lo, hi = con.execute("SELECT MIN(score), MAX(score) FROM material").fetchone()
    verdicts = dict(con.execute("SELECT verdict, COUNT(*) FROM material GROUP BY 1").fetchall())
    ok = lo is not None and lo >= 90 and set(verdicts) == {"保留"}
    checks.append(Check("5 评分与结论（保留口径 ≥90）", ok,
                        f"分数区间 {lo}–{hi}；结论分布 {verdicts}"))

    # 6 ------------------------------------------------------------ 数值域
    bad = {
        "a<=0": q1("SELECT COUNT(*) FROM structure WHERE a IS NOT NULL AND a<=0"),
        "angle": q1("SELECT COUNT(*) FROM structure WHERE alpha IS NOT NULL AND "
                    "(alpha<=0 OR alpha>=180)"),
        "space_group": q1("SELECT COUNT(*) FROM structure WHERE space_group IS NOT NULL AND "
                          "(space_group<1 OR space_group>230)"),
        "gap<0": q1("SELECT COUNT(*) FROM electrical WHERE gap_pbe<0 OR gap_dir_pbe<0 "
                    "OR gap_hse06<0 OR gap_gw<0"),
    }
    checks.append(Check("6 数值域（晶格/角度/空间群/带隙）", sum(bad.values()) == 0,
                        json.dumps(bad, ensure_ascii=False)))

    # 7 ------------------------------------------------------------ 无缺失字符串
    hits = {}
    for t in SUBLIB_TABLES:
        cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})")
                if "TEXT" in (r["type"] or "")]
        for col in cols:
            marks = ",".join("?" * len(FORBIDDEN_TOKENS))
            n = q1(f"SELECT COUNT(*) FROM {t} WHERE {col} IN ({marks})", FORBIDDEN_TOKENS)
            if n:
                hits[f"{t}.{col}"] = n
    checks.append(Check("7 库内不存“缺失/未计算/源表无此字段”字符串（应为 NULL）",
                        not hits, "命中 " + json.dumps(hits, ensure_ascii=False) if hits else "0 命中"))

    # 8 ------------------------------------------------------------ 官网无字段
    absent = {
        "crystal_phase": q1("SELECT COUNT(*) FROM structure WHERE crystal_phase IS NOT NULL"),
        "mobility": q1("SELECT COUNT(*) FROM electrical WHERE mobility_e IS NOT NULL "
                       "OR mobility_h IS NOT NULL"),
        "dielectric": q1("SELECT COUNT(*) FROM electrical WHERE dielectric_static IS NOT NULL"),
    }
    checks.append(Check("8 官网无的字段（晶相/迁移率/介电常数）必须为空",
                        all(v == 0 for v in absent.values()), json.dumps(absent, ensure_ascii=False)))

    # 9 ------------------------------------------------------------ 有效质量
    em_doc = json.loads((HERE / "data" / "emass.json").read_text(encoding="utf-8")) \
        if (HERE / "data" / "emass.json").exists() else {"values": {}}
    em_src = em_doc.get("values", {})
    lib_ids = {r[0] for r in con.execute("SELECT material_id FROM material")}
    expect = {u: v for u, v in em_src.items() if u in lib_ids}
    db_em = {r["material_id"]: (r["eff_mass_cbm"], r["eff_mass_vbm"])
             for r in con.execute("SELECT material_id, eff_mass_cbm, eff_mass_vbm "
                                  "FROM electrical WHERE eff_mass_cbm IS NOT NULL "
                                  "OR eff_mass_vbm IS NOT NULL")}
    missing_in_db = [u for u in expect if u not in db_em]
    diff = []
    for u, v in expect.items():
        got = db_em.get(u)
        if got is None:
            continue
        for a, b in zip((v.get("cbm"), v.get("vbm")), got):
            if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9):
                diff.append(f"{u}: 抓取 {a}/{v.get('vbm')} vs 库 {got}")
    bad_val = q1("SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm < 0 OR eff_mass_vbm < 0")
    n_zero = q1("SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm = 0 OR eff_mass_vbm = 0")
    n_inf = q1("SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm > 1e6 OR eff_mass_vbm > 1e6")
    ok = (len(missing_in_db) == 0 and not diff and bad_val == 0 and len(db_em) == len(expect))
    checks.append(Check("9 有效质量补抓与库内一致（emass_cbm / emass_vbm）", ok,
                        f"官网子集 {len(em_src)} 条，落在本库 {len(expect)} 条，库内有值 {len(db_em)} 条"
                        + (f"（0.000 共 {n_zero} 条、∞(平带) 共 {n_inf} 条）" if n_zero or n_inf else "")
                        + (f"，缺失 {missing_in_db[:3]}" if missing_in_db else "")
                        + (f"，不一致 {diff[:2]}" if diff else "")))

    # 10 ----------------------------------------------------------- 极化率≠介电
    alpha_nonnull = q1("SELECT COUNT(*) FROM electrical WHERE polarizability_total IS NOT NULL")
    dict_hits = q1("SELECT COUNT(*) FROM property_dictionary "
                   "WHERE property_key LIKE 'polarizability%' AND note LIKE '%不是介电常数%'")
    checks.append(Check("10 极化率(Å) 未被当成介电常数", alpha_nonnull > 0 and dict_hits == 3,
                        f"有极化率 {alpha_nonnull} 行；字典中 3 条明确标注“不是介电常数”（{dict_hits}/3）；"
                        "dielectric_static 全空"))

    # 11 ----------------------------------------------------------- 方法/泛函
    need = ["gap_pbe", "gap_dir_pbe", "gap_hse06", "gap_gw", "eff_mass_cbm", "eff_mass_vbm"]
    rows = {r["property_key"]: dict(r) for r in con.execute(
        "SELECT * FROM property_dictionary WHERE property_key IN (?,?,?,?,?,?)", need)}
    ok = (len(rows) == 6 and rows["gap_hse06"]["functional"] == "HSE06@PBE"
          and rows["gap_gw"]["functional"] == "G0W0@PBE"
          and rows["gap_pbe"]["method"] == "PBE" and rows["gap_pbe"]["unit"] == "eV"
          and rows["eff_mass_cbm"]["method"] == "PBE" and rows["eff_mass_cbm"]["unit"] == "m0")
    detail = "；".join(f"{k}:{v['method']}/{v['functional']}/{v['unit']}"
                       for k, v in sorted(rows.items()))
    checks.append(Check("11 方法/泛函齐全（每列都标了方法）", ok, detail))

    # 12 ----------------------------------------------------------- 抽样比对
    rnd = random.Random(20261031)
    uids = sorted(src)
    pick = rnd.sample(uids, min(sample, len(uids)))
    mismatch = []
    idx = {"a": 5, "b": 6, "c": 7, "alpha": 8, "beta": 9, "gamma": 10,
           "gap": 13, "gap_dir": 14, "hse": 15, "gw": 16, "score": 55}
    for uid in pick:
        row = src[uid]
        got = con.execute(
            "SELECT s.a,s.b,s.c,s.alpha,s.beta,s.gamma,e.gap_pbe AS gap,e.gap_dir_pbe AS gap_dir,"
            "e.gap_hse06 AS hse,e.gap_gw AS gw,m.score "
            "FROM material m JOIN structure s ON s.material_id=m.material_id "
            "JOIN electrical e ON e.material_id=m.material_id WHERE m.material_id=?", (uid,)).fetchone()
        if got is None:
            mismatch.append(f"{uid}: 库里没有")
            continue
        for col in COMPARE_COLS:
            want, have = _num(row[idx[col]]), _num(got[col])
            if (want is None) != (have is None) or (want is not None and abs(want - have) > 1e-9):
                mismatch.append(f"{uid}.{col}: 源表 {want} / 库 {have}")
    checks.append(Check(f"12 抽样 {len(pick)} 条逐值比对（{len(COMPARE_COLS)} 个数值列）",
                        not mismatch, "全部一致" if not mismatch else f"{len(mismatch)} 处不一致：" + "; ".join(mismatch[:5])))

    # 13 ----------------------------------------------------------- CIF 原文内嵌
    # 三层：(a) 每条材料都有 cif_content；(b) 逐条重算 sha256 与 cif_sha256 一致；
    #       (c) 抽 20 条与 data/cif 磁盘文件逐字节比对（证明库内 blob 就是下载到的原文件）
    n_cif = q1("SELECT COUNT(*) FROM structure WHERE cif_content IS NOT NULL")
    n_bytes = q1("SELECT COALESCE(SUM(LENGTH(cif_content)), 0) FROM structure")
    bad_hash = []
    for uid, blob, stored in con.execute(
            "SELECT material_id, cif_content, cif_sha256 FROM structure "
            "WHERE cif_content IS NOT NULL"):
        if stored is None or hashlib.sha256(blob).hexdigest() != stored:
            bad_hash.append(uid)
    cif_pick = rnd.sample(sorted(src), min(20, len(src)))
    bad_disk = []
    for uid in cif_pick:
        p = CIF_DIR / f"{uid}.cif"
        blob = q1("SELECT cif_content FROM structure WHERE material_id=?", (uid,))
        if not p.exists() or blob is None or p.read_bytes() != blob:
            bad_disk.append(uid)
    checks.append(
        Check("13 CIF 原文件内嵌库内（内容 = 官网下载字节，逐条重算哈希）",
              n_cif == n_src and not bad_hash and not bad_disk,
              f"内嵌 {n_cif}/{n_src} 条、共 {n_bytes / 1024 / 1024:.2f} MB；"
              f"sha256 逐条重算不一致 {len(bad_hash)} 条；"
              f"与 data/cif 抽检 {len(cif_pick)} 条，"
              + ("全部逐字节一致" if not bad_disk else f"不一致 {bad_disk[:3]}")
              + (f"；哈希异常 {bad_hash[:3]}" if bad_hash else "")))

    # 14 ----------------------------------------------------------- v5 新增三列
    # (a) 三列都必须全量覆盖；(b) unit_cell_area 与 a·b·sinγ 一致（容差 2%）；
    # (c) thickness=0 是单原子层的有效值，只统计不判错；(d) evac 必须是有限数
    n_th = q1("SELECT COUNT(*) FROM structure WHERE thickness IS NOT NULL")
    n_ar = q1("SELECT COUNT(*) FROM structure WHERE unit_cell_area IS NOT NULL")
    n_ev = q1("SELECT COUNT(*) FROM electrical WHERE evac IS NOT NULL")
    n_th_zero = q1("SELECT COUNT(*) FROM structure WHERE thickness = 0")
    n_bad_ev = q1("SELECT COUNT(*) FROM electrical WHERE evac IS NOT NULL "
                  "AND (evac <> evac OR ABS(evac) > 1e6)")
    area_bad, area_worst = [], 0.0
    for r in con.execute("SELECT material_id, a, b, gamma, unit_cell_area FROM structure "
                         "WHERE unit_cell_area IS NOT NULL AND a IS NOT NULL "
                         "AND b IS NOT NULL AND gamma IS NOT NULL"):
        calc = r["a"] * r["b"] * math.sin(math.radians(r["gamma"]))
        if calc <= 0:
            area_bad.append(f"{r['material_id']}: 计算面积 {calc:.4f} <= 0")
            continue
        rel = abs(calc - r["unit_cell_area"]) / calc
        area_worst = max(area_worst, rel)
        if rel > 0.02:
            area_bad.append(f"{r['material_id']}: 官网 {r['unit_cell_area']} vs 计算 {calc:.4f}")
    checks.append(
        Check("14 v5 补入列（thickness / unit_cell_area / evac）覆盖与自洽",
              n_th == n_src and n_ar == n_src and n_ev == n_src
              and not area_bad and n_bad_ev == 0,
              f"thickness {n_th}/{n_src}（其中 0.000 共 {n_th_zero} 条，单原子层有效值）；"
              f"unit_cell_area {n_ar}/{n_src}；evac {n_ev}/{n_src}；"
              f"area 与 a·b·sinγ 最大相对偏差 {area_worst:.4%}（容差 2%），"
              + ("全部一致" if not area_bad else f"超差 {area_bad[:3]}")
              + (f"；evac 非有限 {n_bad_ev} 条" if n_bad_ev else "")))

    # ---- 附加统计（写进报告，不作为断言）
    stats = {
        "counts": counts,
        "tables": dbmod.table_names(con),
        "views": dbmod.view_names(con),
        "missing": [dict(r) for r in con.execute(
            "SELECT property_key, availability, n_missing, missing_rate "
            "FROM v_missing_counts ORDER BY n_missing DESC, property_key")],
        "category": [dict(r) for r in con.execute(
            "SELECT category, has_metal, n_materials, avg_score, avg_gap_pbe, n_direct "
            "FROM v_category_summary ORDER BY n_materials DESC")],
        "gap": {
            "pbe_zero": q1("SELECT COUNT(*) FROM electrical WHERE gap_pbe = 0"),
            "hse": q1("SELECT COUNT(*) FROM electrical WHERE gap_hse06 IS NOT NULL"),
            "gw": q1("SELECT COUNT(*) FROM electrical WHERE gap_gw IS NOT NULL"),
            "direct": q1("SELECT COUNT(*) FROM electrical WHERE gap_kind_pbe='直接'"),
        },
        "dyn": dict(con.execute("SELECT dyn_stab, COUNT(*) FROM stability GROUP BY 1").fetchall()),
        "eff_mass": {
            "n": q1("SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm IS NOT NULL"),
            "inf": q1("SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm > 1e6 "
                      "OR eff_mass_vbm > 1e6"),
            "zero": q1("SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm = 0 "
                       "OR eff_mass_vbm = 0"),
        },
        "doc": {"precision": DISPLAY_PRECISION},
    }

    ok_all = all(c.ok for c in checks)
    if write_report:
        write_txt(checks, stats, ok_all)
    con.close()
    return {"ok": ok_all, "checks": checks, "stats": stats, "report": str(REPORT)}


def write_txt(checks, stats, ok_all) -> None:
    lines = []
    lines.append("C2DB 材料数据库 · 体检报告")
    lines.append("=" * 72)
    lines.append(f"结论：{'全部通过' if ok_all else '存在不通过项，请先修复'}")
    lines.append("")
    for c in checks:
        lines.append(f"[{'OK' if c.ok else 'FAIL'}] {c.name}")
        if c.detail:
            lines.append(f"        {c.detail}")
    lines.append("")
    lines.append("-" * 72)
    lines.append("规模")
    for k, v in stats["counts"].items():
        lines.append(f"  {k:<12}{v:>8}")
    lines.append(f"  表 {len(stats['tables'])} 个：{'、'.join(stats['tables'])}")
    lines.append(f"  视图 {len(stats['views'])} 个：{'、'.join(stats['views'])}")
    lines.append("")
    lines.append("缺失率（官网有该字段；分母 = 库内材料数）")
    for r in stats["missing"]:
        if r["availability"] == "no_field":
            continue
        lines.append(f"  {r['property_key']:<22}{r['n_missing']:>6}  {r['missing_rate']*100:6.2f}%")
    nobody = [r["property_key"] for r in stats["missing"] if r["availability"] == "no_field"]
    if nobody:
        lines.append("")
        lines.append("官网无此字段（全局，不存在逐行缺失率）")
        lines.append("  " + "、".join(nobody))
    lines.append("")
    lines.append("类别分布")
    for r in stats["category"]:
        lines.append(f"  {r['category']:<8}含金属={r['has_metal']}  {r['n_materials']:>5} 条  "
                     f"均分 {r['avg_score']}  平均带隙 {r['avg_gap_pbe']} eV  直接带隙 {r['n_direct']}")
    lines.append("")
    lines.append("电学覆盖")
    g = stats["gap"]
    lines.append(f"  PBE 带隙全有；其中零带隙 {g['pbe_zero']} 条；HSE06 {g['hse']} 条；"
                 f"G0W0 {g['gw']} 条；直接带隙 {g['direct']} 条")
    em = stats["eff_mass"]
    lines.append(f"  有效质量（PBE·DOS）：CBM/VBM 有值 {em['n']} 条；其中 ∞(平带) {em['inf']} 条、"
                 f"0.000（<0.0005 m0）{em['zero']} 条")
    lines.append(f"  动力学稳定 Yes/No：{stats['dyn']}")
    lines.append("")
    lines.append(f"注：所有数值按官网显示精度 {stats['doc']['precision']} 位小数存储。")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    res = run()
    for c in res["checks"]:
        print(f"[{'OK' if c.ok else 'FAIL'}] {c.name}  {c.detail}")
    print()
    print("报告:", res["report"])
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
