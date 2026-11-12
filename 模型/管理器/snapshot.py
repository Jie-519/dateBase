# -*- coding: utf-8 -*-
"""母库 -> 训练快照（CSV）+ 划分（split.json）+ 特征契约（features.json）。

为什么要有这一层：
  * 训练只认这三份文件，不直接连母库 —— 母库结构变了，快照仍可复现；
  * 划分只做一次、落盘并记哈希 —— 训练脚本不许自己 random；
  * 特征名单唯一来源是 features.json —— 黑名单（带隙同源列）在此集中拦截。

用法：
    python snapshot.py                # 生成/覆盖 快照 + 划分 + 契约
    python snapshot.py --check        # 只校验已有三份文件是否自洽（不重写）

约束（写死在代码里，不靠自觉）：
  1. 母库只读（mode=ro&immutable=1）
  2. 划分单位 = formula 分组（同化学式的不同相一致进同一侧），按 (带隙分箱, 含金属) 分层
  3. 三集两两交集必须为空、并集必须等于快照全集，否则直接抛异常
  4. 元素 one-hot 列序按原子序数 Z 升序固定，写进 features.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from paths import (DATASET_ID, MOTHER_DB, RATIO_TOLERANCE_PP, RATIOS, SEED,
                   SNAPSHOT_DIR, TARGET, ensure_dirs)

# ---------------------------------------------------------------- 特征清单
#: 结构（官网 CIF / 表格）
STRUCT_FEATURES = [
    ("a", "Å", "structure.a", "c2db_cif", "晶格常数 a"),
    ("b", "Å", "structure.b", "c2db_cif", "晶格常数 b"),
    ("c", "Å", "structure.c", "c2db_cif", "晶格常数 c（二维胞含真空层，不能当厚度用）"),
    ("alpha", "°", "structure.alpha", "c2db_cif", "晶胞角 α"),
    ("beta", "°", "structure.beta", "c2db_cif", "晶胞角 β"),
    ("gamma", "°", "structure.gamma", "c2db_cif", "晶胞角 γ"),
    ("space_group", "1-230", "structure.space_group", "c2db_table", "空间群号（体相 AA 堆叠）"),
    ("cell_volume", "Å³", "structure.cell_volume", "derived", "由 a,b,c,α,β,γ 推导"),
    ("thickness", "Å", "structure.thickness", "c2db_table", "官网 thickness；0 是单原子层有效值（v5 补入）"),
    ("unit_cell_area", "Å²", "structure.unit_cell_area", "c2db_table", "官网 area；与 a·b·sinγ 一致（v5 补入）"),
]
#: 组成
COMP_FEATURES = [
    ("n_elements", "个", "material.n_elements", "c2db_table", "元素种类数 1–7"),
    ("has_metal", "0/1", "material.has_metal", "c2db_table", "是否含金属元素"),
    ("frac_max", "0-1", "derived", "derived", "最大组分占比 = max(count)/Σcount"),
    ("n_atoms", "个", "derived", "derived", "胞内原子数 = Σcount（energy 归一化的分母）"),
]
#: 稳定性/能量
STAB_FEATURES = [
    ("ehull", "eV/atom", "stability.ehull", "c2db_table", "能量高于凸包，越小越稳"),
    ("hform", "eV/atom", "stability.hform", "c2db_table", "形成能"),
    ("energy_per_atom", "eV/atom", "derived", "derived", "总能量 / 原子数（跨组成可比）"),
    ("dyn_stab", "0/1", "stability.dyn_stab", "c2db_table", "动力学稳定：Yes=1, No=0, Unknown=空"),
]
#: 独立的电子结构量（非带隙同源）
ELEC_FEATURES = [
    ("evac", "eV", "electrical.evac", "c2db_table", "真空能级（v5 补入）；不是带隙"),
]
#: 同源泄漏组：只允许出现在 G3 / G3p 对照实验里
LEAKY_FEATURES = [
    ("eff_mass_cbm", "m0", "electrical.eff_mass_cbm", "c2db_table", "电子有效质量（由带边曲率算出 → 同源）"),
    ("eff_mass_vbm", "m0", "electrical.eff_mass_vbm", "c2db_table", "空穴有效质量（同源）"),
    ("polarizability_el", "Å", "electrical.polarizability_el", "c2db_table", "面外电子极化率（同一次电子结构计算）"),
    ("polarizability_total", "Å", "electrical.polarizability_total", "c2db_table", "面外总极化率（同源）"),
    ("gap_dir_pbe", "eV", "electrical.gap_dir_pbe", "c2db_table", "最小直接带隙 —— 与目标同源，G3p 专用"),
]
#: 缺失指示列（同源组用：NaN 不能静默）
INDICATOR_FEATURES = [
    ("is_missing_eff_mass", "0/1", "derived", "derived", "有效质量缺失指示（2,769 有值 / 585 缺）"),
    ("is_missing_polarizability", "0/1", "derived", "derived", "极化率缺失指示"),
    ("is_missing_gap_dir", "0/1", "derived", "derived", "gap_dir 缺失指示（G3p 专用）"),
]

#: 黑名单：任何情况下不得进特征（runlog 会二次拦截）
BLACKLIST = [
    "gap_pbe", "gap_hse06", "gap_gw", "gap_kind_pbe",
    "gap_dir", "gap_hse", "gap", "direct_indirect_PBE",
    "score", "verdict", "verdict_reason", "missing_reason", "missing_fields",
    "formula", "reduced_formula", "composition", "composition_json", "name",
    "source_url", "cif_url", "cif_sha256", "cif_bytes", "cif_cache", "cif_content",
    "material_id", "batch_id", "olduid",
]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 读母库（只读）
def load_mother() -> tuple[list[dict], list[dict]]:
    if not MOTHER_DB.exists():
        raise SystemExit(f"找不到母库 {MOTHER_DB}")
    uri = f"file:{MOTHER_DB.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT m.material_id, m.formula, m.n_elements, m.has_metal, m.composition_json,"
        "       s.a, s.b, s.c, s.alpha, s.beta, s.gamma, s.space_group, s.cell_volume,"
        "       s.thickness, s.unit_cell_area,"
        "       e.gap_pbe, e.gap_dir_pbe, e.eff_mass_cbm, e.eff_mass_vbm,"
        "       e.polarizability_el, e.polarizability_total, e.evac,"
        "       st.dyn_stab, st.ehull, st.hform, st.energy "
        "FROM material m "
        "JOIN structure  s  ON s.material_id  = m.material_id "
        "JOIN electrical e  ON e.material_id  = m.material_id "
        "JOIN stability  st ON st.material_id = m.material_id "
        "ORDER BY m.material_id").fetchall()
    elements = [dict(z) for z in con.execute("SELECT z, symbol FROM element ORDER BY z")]
    melem = defaultdict(dict)
    for r in con.execute("SELECT material_id, z, count FROM material_element"):
        melem[r["material_id"]][r["z"]] = float(r["count"])
    con.close()

    records = []
    for r in rows:
        d = dict(r)
        d["counts"] = melem.get(d["material_id"], {})
        records.append(d)
    if not records:
        raise SystemExit("母库为空")
    return records, elements


# ---------------------------------------------------------------- 派生特征
def _num(v, *, inf_to_nan: bool = False):
    if v is None:
        return math.nan
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    if inf_to_nan and (f == float("inf") or f == float("-inf")):
        return math.nan
    return f


def build_row(rec: dict, elements: list[dict]) -> dict:
    """一条母库记录 -> 一行特征字典（含派生）。缺失一律 NaN，0 保持 0。"""
    counts = rec["counts"]
    n_atoms = sum(counts.values())
    frac_max = (max(counts.values()) / n_atoms) if counts and n_atoms > 0 else math.nan
    energy = _num(rec["energy"])
    row = {
        "material_id": rec["material_id"],
        "formula": rec["formula"],
        "target": _num(rec["gap_pbe"]),
        # 结构
        "a": _num(rec["a"]), "b": _num(rec["b"]), "c": _num(rec["c"]),
        "alpha": _num(rec["alpha"]), "beta": _num(rec["beta"]), "gamma": _num(rec["gamma"]),
        "space_group": _num(rec["space_group"]),
        "cell_volume": _num(rec["cell_volume"]),
        "thickness": _num(rec["thickness"]),
        "unit_cell_area": _num(rec["unit_cell_area"]),
        # 组成
        "n_elements": _num(rec["n_elements"]),
        "has_metal": _num(rec["has_metal"]),
        "frac_max": frac_max,
        "n_atoms": float(n_atoms) if n_atoms else math.nan,
        # 稳定性
        "ehull": _num(rec["ehull"]),
        "hform": _num(rec["hform"]),
        "energy_per_atom": (energy / n_atoms) if (n_atoms and not math.isnan(energy)) else math.nan,
        "dyn_stab": {"Yes": 1.0, "No": 0.0}.get(rec["dyn_stab"], math.nan),
        # 电子（独立量）
        "evac": _num(rec["evac"]),
        # 同源组（默认不进主模型）
        "eff_mass_cbm": _num(rec["eff_mass_cbm"], inf_to_nan=True),
        "eff_mass_vbm": _num(rec["eff_mass_vbm"], inf_to_nan=True),
        "polarizability_el": _num(rec["polarizability_el"]),
        "polarizability_total": _num(rec["polarizability_total"]),
        "gap_dir_pbe": _num(rec["gap_dir_pbe"]),
    }
    row["is_missing_eff_mass"] = 1.0 if (math.isnan(row["eff_mass_cbm"]) or math.isnan(row["eff_mass_vbm"])) else 0.0
    row["is_missing_polarizability"] = 1.0 if math.isnan(row["polarizability_total"]) else 0.0
    row["is_missing_gap_dir"] = 1.0 if math.isnan(row["gap_dir_pbe"]) else 0.0
    for el in elements:
        row[f"elem_{el['z']:02d}_{el['symbol']}"] = 1.0 if el["z"] in counts else 0.0
    return row


# ---------------------------------------------------------------- 划分
def gap_bin(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "na"
    if v == 0:
        return "0"
    if v <= 0.5:
        return "(0,0.5]"
    if v <= 1:
        return "(0.5,1]"
    if v <= 2:
        return "(1,2]"
    if v <= 3:
        return "(2,3]"
    return ">3"


def make_split(records: list[dict], rows_by_id: dict[str, dict], seed: int) -> dict[str, str]:
    """按 formula 分组 + (带隙分箱, 含金属) 分层的贪心划分。返回 uid -> split。"""
    groups: dict[str, list[str]] = defaultdict(list)
    for rec in records:
        groups[rec["formula"]].append(rec["material_id"])

    def group_stratum(uids: list[str]) -> tuple[str, int]:
        c = Counter((gap_bin(rows_by_id[u]["target"]), int(rows_by_id[u]["has_metal"]))
                    for u in uids)
        return c.most_common(1)[0][0]

    by_stratum: dict[tuple, list[tuple[str, int]]] = defaultdict(list)
    for formula, uids in groups.items():
        by_stratum[group_stratum(uids)].append((formula, len(uids)))

    rnd = random.Random(seed)
    names = list(RATIOS)
    assign: dict[str, str] = {}
    for _, gs in sorted(by_stratum.items(), key=lambda kv: str(kv[0])):
        total = sum(sz for _, sz in gs)
        target = {n: RATIOS[n] * total for n in names}
        got = {n: 0 for n in names}
        order = sorted(gs, key=lambda x: -x[1])
        rnd.shuffle(order)
        order.sort(key=lambda x: -x[1])          # 大组优先，同大小随机
        for formula, sz in order:
            pick = max(names, key=lambda n: target[n] - got[n])
            got[pick] += sz
            for uid in groups[formula]:
                assign[uid] = pick
    return assign


def split_stats(assign: dict[str, str], n: int) -> dict:
    counts = Counter(assign.values())
    ratio = {k: counts[k] / n for k in RATIOS}
    dev = max(abs(ratio[k] - RATIOS[k]) * 100 for k in RATIOS)
    return {"counts": {k: counts[k] for k in RATIOS}, "ratios": ratio, "max_dev_pp": dev}


def choose_split(records, rows_by_id) -> tuple[dict, dict]:
    """在容差允许范围内找一个偏差最小的种子；找不到就用偏差最小的那个并如实报告。"""
    best = None
    for seed in range(SEED, SEED + 200):
        assign = make_split(records, rows_by_id, seed)
        st = split_stats(assign, len(records))
        if best is None or st["max_dev_pp"] < best[1]["max_dev_pp"]:
            best = (seed, st, assign)
        if st["max_dev_pp"] <= RATIO_TOLERANCE_PP:
            return best[2], {"seed": seed, **st}
    seed, st, assign = best
    return assign, {"seed": seed, **st}


# ---------------------------------------------------------------- 写三份文件
def feature_catalog(elements: list[dict]) -> dict:
    def pack(items):
        return [{"name": n, "unit": u, "source": s, "origin": o, "note": note}
                for (n, u, s, o, note) in items]

    elem_cols = [{"name": f"elem_{e['z']:02d}_{e['symbol']}", "unit": "0/1",
                  "source": f"material_element.z={e['z']}",
                  "origin": "derived_onehot", "note": f"含元素 {e['symbol']}（Z={e['z']}）"}
                 for e in elements]
    groups = {
        "G0": [f["name"] for f in pack(COMP_FEATURES)] + [c["name"] for c in elem_cols],
        "G1": None, "G2": None, "G3": None, "G3p": None,
    }
    g0 = groups["G0"]
    g1 = g0 + [f["name"] for f in pack(STRUCT_FEATURES)]
    g2 = g1 + [f["name"] for f in pack(STAB_FEATURES)] + [f["name"] for f in pack(ELEC_FEATURES)]
    g3 = g2 + [f["name"] for f in pack(LEAKY_FEATURES[:4])] + ["is_missing_eff_mass", "is_missing_polarizability"]
    g3p = g2 + ["gap_dir_pbe", "is_missing_gap_dir"]
    return {
        "schema": "features/1",
        "target": TARGET,
        "target_table": "electrical.gap_pbe",
        "unit": "eV",
        "task": "regression",
        "dataset_id": DATASET_ID,
        "blacklist": BLACKLIST,
        "features": {
            "structure": pack(STRUCT_FEATURES),
            "composition": pack(COMP_FEATURES),
            "stability": pack(STAB_FEATURES),
            "electronic_independent": pack(ELEC_FEATURES),
            "same_source_leaky": pack(LEAKY_FEATURES),
            "indicators": pack(INDICATOR_FEATURES),
            "elements": elem_cols,
        },
        "groups": {"G0": g0, "G1": g1, "G2": g2, "G3": g3, "G3p": g3p},
        "group_notes": {
            "G0": "仅组成（元素 one-hot + 组成统计）—— 基线",
            "G1": "G0 + 结构（晶格/角度/空间群/体积/厚度/面积）",
            "G2": "G1 + 稳定性/能量 + 真空能级 —— 主模型",
            "G3": "G2 + 有效质量 + 极化率（同源泄漏，对照实验，只看不用）",
            "G3p": "G2 + gap_dir_pbe（答案泄漏，对照实验，证明会虚高）",
        },
        "derived": {
            "frac_max": "max(count)/Σcount，来自 composition_json",
            "n_atoms": "Σcount，来自 composition_json",
            "energy_per_atom": "stability.energy / n_atoms",
            "dyn_stab": "Yes→1, No→0, Unknown→NaN",
            "is_missing_*": "对应列为 NaN 时置 1",
        },
        "missing_policy": "缺失一律 NaN（不写 0、不写字符串）；0.000 是有效值原样保留",
    }


def write_snapshot(records, elements, assign, out_csv: Path) -> tuple[str, int]:
    columns = ["material_id", "formula", "split", "target"]
    rows = []
    for rec in records:
        row = build_row(rec, elements)
        row["split"] = assign[rec["material_id"]]
        rows.append(row)
    # 列序：固定清单（不依赖 dict 插入顺序）
    feat_names = []
    for grp in ("structure", "composition", "stability", "electronic_independent",
                "same_source_leaky", "indicators"):
        feat_names += [f["name"] for f in feature_catalog(elements)["features"][grp]]
    feat_names += [f"elem_{e['z']:02d}_{e['symbol']}" for e in elements]
    columns += feat_names

    buf = []
    for row in rows:
        vals = []
        for c in columns:
            v = row.get(c)
            if isinstance(v, float):
                if math.isnan(v):
                    vals.append("")
                elif v.is_integer():
                    vals.append(str(int(v)))
                else:
                    vals.append(f"{v:.6f}")
            elif v is None:
                vals.append("")
            else:
                vals.append(str(v))
        buf.append(vals)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        w.writerows(buf)
    return sha256_file(out_csv), len(rows)


def build(check: bool = False) -> dict:
    ensure_dirs()
    records, elements = load_mother()
    rows_by_id = {r["material_id"]: {"target": _num(r["gap_pbe"]), "has_metal": r["has_metal"]}
                  for r in records}

    csv_path = SNAPSHOT_DIR / f"{DATASET_ID}.csv"
    split_path = SNAPSHOT_DIR / f"{DATASET_ID}.split.json"
    feat_path = SNAPSHOT_DIR / f"{DATASET_ID}.features.json"

    if check:
        return check_existing(csv_path, split_path, feat_path, records)

    assign, st = choose_split(records, rows_by_id)
    catalog = feature_catalog(elements)
    digest, n = write_snapshot(records, elements, assign, csv_path)

    # ---- 硬校验（不通过就中止）
    uids = [r["material_id"] for r in records]
    parts = {k: [u for u in uids if assign[u] == k] for k in RATIOS}
    for a in RATIOS:
        for b in RATIOS:
            if a < b:
                inter = set(parts[a]) & set(parts[b])
                if inter:
                    raise SystemExit(f"[中止] {a} 与 {b} 有交集 {len(inter)} 条：{sorted(inter)[:3]}")
    union = set(parts["train"]) | set(parts["valid"]) | set(parts["test"])
    if union != set(uids):
        raise SystemExit(f"[中止] 并集 {len(union)} != 快照全集 {len(uids)}")
    by_formula = defaultdict(set)
    for u in uids:
        by_formula[next(r["formula"] for r in records if r["material_id"] == u)].add(assign[u])
    leaked = [f for f, ss in by_formula.items() if len(ss) > 1]
    if leaked:
        raise SystemExit(f"[中止] 同化学式跨集 {len(leaked)} 组：{leaked[:3]}")

    split_doc = {
        "schema": "split/1",
        "dataset_id": DATASET_ID,
        "target": TARGET,
        "seed": st["seed"],
        "ratios_target": RATIOS,
        "counts": st["counts"],
        "ratios_actual": {k: round(v, 6) for k, v in st["ratios"].items()},
        "max_dev_pp": round(st["max_dev_pp"], 6),
        "tolerance_pp": RATIO_TOLERANCE_PP,
        "strategy": "grouped_by_formula + stratified(gap_bin,has_metal) + greedy_largest_deficit",
        "n_groups": len(by_formula),
        "snapshot_csv": csv_path.name,
        "snapshot_sha256": digest,
        "n_materials": n,
        "indices_sha256": {k: sha256_bytes(json.dumps(sorted(parts[k]), ensure_ascii=False).encode())
                           for k in RATIOS},
        "uids": parts,
    }
    split_path.write_text(json.dumps(split_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    feat_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "csv": str(csv_path), "csv_sha256": digest, "n": n,
        "split": str(split_path), "features": str(feat_path),
        "seed": st["seed"], "counts": st["counts"],
        "ratios": {k: round(v * 100, 3) for k, v in st["ratios"].items()},
        "max_dev_pp": round(st["max_dev_pp"], 4),
        "groups": len(by_formula),
    }


def check_existing(csv_path: Path, split_path: Path, feat_path: Path, records) -> dict:
    for p in (csv_path, split_path, feat_path):
        if not p.exists():
            raise SystemExit(f"缺少 {p}，先跑 python snapshot.py")
    doc = json.loads(split_path.read_text(encoding="utf-8"))
    uids = [r["material_id"] for r in records]
    ok = True
    if sha256_file(csv_path) != doc["snapshot_sha256"]:
        print("[FAIL] 快照哈希与 split.json 不一致（快照被改过）")
        ok = False
    parts = doc["uids"]
    for a in RATIOS:
        for b in RATIOS:
            if a < b and set(parts[a]) & set(parts[b]):
                print(f"[FAIL] {a} 与 {b} 有交集")
                ok = False
    if set(parts["train"]) | set(parts["valid"]) | set(parts["test"]) != set(uids):
        print("[FAIL] 并集 != 母库材料集")
        ok = False
    print(("[OK] " if ok else "[FAIL] ") + f"快照 {doc['n_materials']} 条，"
          f"uid 唯一、三集互斥、并集完整；比例 {doc['ratios_actual']}")
    return {"ok": ok, "doc": doc}


def main() -> int:
    ap = argparse.ArgumentParser(description="母库 -> 训练快照 + 划分 + 特征契约")
    ap.add_argument("--check", action="store_true", help="只校验已有文件是否自洽")
    args = ap.parse_args()
    out = build(check=args.check)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
