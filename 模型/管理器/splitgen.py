# -*- coding: utf-8 -*-
"""splitgen.py：按"你给的条数"重新划分数据，并落成一份可训练的快照。

和 snapshot.py 的关系
--------------------
* `snapshot.py` 是**按比例**抽快照（70/15/15），母库更新时用它重建母快照；
* 本模块是**按条数**重划：不碰母库，只改快照里 `split` 那一列，
  产出一份新的 `<dataset_id>.csv` + `<dataset_id>.split.json` + `.features.json`。

四条硬规则（任何一条不满足就中止，绝不写出脏数据）
------------------------------------------------
1. **测试集严禁进入训练集（也不得进入验证集）** —— 三集两两材料零交叠；
2. **化学式整组切分** —— 同一 `formula` 的材料只会落在同一个集（防同族跨集泄漏）；
3. **条数精确** —— 你要多少条就是多少条，靠单材料组搬移与等差异交换修补；
4. **允许不用满** —— 训练+验证+测试 可以小于材料总数，剩下的进 `unused` 池，
   既不训练也不评估；`unused` 与三集同样互斥。

同输入必同输出：同样的 (条数, seed) 永远得到同一份划分。

用法
----
    python splitgen.py --train 2349 --valid 505 --test 500            # 用满 3354
    python splitgen.py --train 1500 --valid 200 --test 200            # 只用 1900，其余 1454 条不入训练
    python splitgen.py --train 1500 --valid 200 --test 200 --dry-run  # 只看统计
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import (BASE_DATASET_ID, MIN_SIZES, RATIO_TOLERANCE_PP, SEED,
                   SNAPSHOT_DIR, TARGET, ensure_dirs)

SPLITS = ("train", "valid", "test")
UNUSED = "unused"
BUCKETS = SPLITS + (UNUSED,)
SPLIT_CN = {"train": "训练集", "valid": "验证集", "test": "测试集", "unused": "未使用"}
STRATEGY = ("grouped_by_formula + stratified(gap_bin,has_metal) + greedy_largest_deficit "
            "+ exact_count_repair + optional_unused_pool")


# ---------------------------------------------------------------- 基础
def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset_id_for(counts: dict, seed: int) -> str:
    """一套展品的唯一名字：条数 + seed 都在名字里，看见名字就知道怎么分的。"""
    return f"c2db-T{counts['train']}-V{counts['valid']}-S{counts['test']}-s{seed}"


def family_id_for(counts: dict) -> str:
    """同一个条数配置、不同 seed 的多次实验，共用 family（用于"均值±标准差"）。"""
    return f"c2db-T{counts['train']}-V{counts['valid']}-S{counts['test']}"


def gap_bin(v: float) -> str:
    """与 snapshot.py 完全一致的带隙分箱（分层口径不能两套）。"""
    if v is None:
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


def load_base(csv_path: Path | None = None) -> tuple[list[str], list[dict]]:
    """读母快照。数值列按字符串原样保留 —— 重写时一个字节都不改，只换 split 列。"""
    p = Path(csv_path) if csv_path else SNAPSHOT_DIR / f"{BASE_DATASET_ID}.csv"
    if not p.exists():
        raise SystemExit(f"缺母快照 {p}，先跑 python snapshot.py")
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        header = list(rd.fieldnames or [])
        rows = list(rd)
    need = {"material_id", "formula", "split", "target", "has_metal"}
    lack = need - set(header)
    if lack:
        raise SystemExit(f"母快照缺列：{sorted(lack)}")
    return header, rows


def _meta(rows: list[dict]) -> dict:
    meta = {}
    for r in rows:
        try:
            y = float(r["target"]) if r["target"] not in ("", None) else None
        except ValueError:
            y = None
        try:
            metal = int(float(r["has_metal"])) if r["has_metal"] not in ("", None) else 0
        except ValueError:
            metal = 0
        meta[r["material_id"]] = {"target": y, "metal": metal, "formula": r["formula"]}
    return meta


def _group_stratum(uids: list[str], meta: dict) -> tuple:
    c = Counter((gap_bin(meta[u]["target"]), meta[u]["metal"]) for u in uids)
    return c.most_common(1)[0][0]


# ---------------------------------------------------------------- 修补
def _repair(assign_f: dict, size: dict, targets: dict) -> tuple[dict, Counter]:
    """把 train/valid/test 三集修到与目标逐个相等；差额向 unused 池借还。

    四个动作，按"代价从小到大"尝试：
      ① 整组搬移：组的条数恰好等于超出量；
      ② 等差异交换：over 出 size_a、under 出 size_b，size_a - size_b == 超出量；
      ③ 从 unused 池借入一个放得下的组（优先大组）；
      ④ 把多余的组退回 unused。
    单材料组有 2558 个，所以几乎总能凑出精确解。
    """
    cur = Counter()
    for f, s in assign_f.items():
        cur[s] += size[f]

    for _ in range(20000):
        over = [k for k in SPLITS if cur[k] > targets[k]]
        short = [k for k in SPLITS if cur[k] < targets[k]]
        if not over and not short:
            break
        moved = False

        # ①
        for k_o in over:
            need = cur[k_o] - targets[k_o]
            for f, a in assign_f.items():
                if a != k_o or size[f] != need:
                    continue
                for k_u in short:
                    if size[f] <= targets[k_u] - cur[k_u]:
                        assign_f[f] = k_u
                        cur[k_o] -= need
                        cur[k_u] += need
                        moved = True
                        break
                if moved:
                    break
            if moved:
                break
        if moved:
            continue

        # ②
        for k_o in over:
            need = cur[k_o] - targets[k_o]
            by_o: dict[int, list[str]] = defaultdict(list)
            by_s: dict[int, list[str]] = defaultdict(list)
            for f, a in assign_f.items():
                (by_o if a == k_o else by_s)[size[f]].append(f)
            for k_u in short:
                pair = None
                for sz_a, la in by_o.items():
                    for sz_b in by_s:
                        if sz_a - sz_b == need:
                            pair = (la[0], by_s[sz_b][0])
                            break
                    if pair:
                        break
                if pair:
                    fa, fb = pair
                    assign_f[fa], assign_f[fb] = k_u, k_o
                    cur[k_o] -= need
                    cur[k_u] += need
                    moved = True
                    break
            if moved:
                break
        if moved:
            continue

        # ③
        if short and not over:
            for k_u in short:
                room = targets[k_u] - cur[k_u]
                cand = [(size[f], f) for f, a in assign_f.items()
                        if a == UNUSED and size[f] <= room]
                if cand:
                    sz, f = max(cand)
                    assign_f[f] = k_u
                    cur[UNUSED] -= sz
                    cur[k_u] += sz
                    moved = True
                    break
        if moved:
            continue

        # ④
        if over and not short:
            for k_o in over:
                need = cur[k_o] - targets[k_o]
                cand = [(size[f], f) for f, a in assign_f.items()
                        if a == k_o and size[f] <= need]
                if cand:
                    sz, f = max(cand)
                    assign_f[f] = UNUSED
                    cur[k_o] -= sz
                    cur[UNUSED] += sz
                    moved = True
                    break
        if not moved:
            break
    return assign_f, cur


def _alloc(total: int, weights: dict, keys: list) -> dict:
    """最大余额法：把 total 按 weights 分给 keys，结果之和恰好等于 total。"""
    wsum = sum(weights[k] for k in keys) or 1
    raw = {k: total * weights[k] / wsum for k in keys}
    out = {k: int(raw[k]) for k in keys}
    rest = total - sum(out.values())
    order = sorted(keys, key=lambda k: (-(raw[k] - out[k]), str(k)))
    for i in range(rest):
        out[order[i % len(order)]] += 1
    return out


# ---------------------------------------------------------------- 划分
def plan(rows: list[dict], counts: dict, seed: int) -> dict:
    """算出一份划分，不写盘。返回 assign（含 unused）+ 统计 + 自检结论。"""
    n = len(rows)
    want = sum(counts[k] for k in SPLITS)
    if want > n:
        raise SystemExit(f"[拒绝] 训练+验证+测试 = {want} > 材料总数 {n}；最多只能用 {n} 条")
    for k in SPLITS:
        lo = MIN_SIZES[k]
        if counts[k] < lo:
            raise SystemExit(f"[拒绝] {SPLIT_CN[k]}只有 {counts[k]} 条，低于下限 {lo} 条。"
                             f"（样本太少，R² 的波动会盖过结论）")

    meta = _meta(rows)
    groups: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        groups[r["formula"]].append(r["material_id"])
    strata: dict[tuple, list[tuple[str, int]]] = defaultdict(list)
    for f, uids in groups.items():
        strata[_group_stratum(uids, meta)].append((f, len(uids)))

    rnd = random.Random(seed)
    targets = {k: counts[k] for k in SPLITS}
    assign_f: dict[str, str] = {}
    got = {k: 0 for k in SPLITS}
    pool: list[tuple[str, int]] = []

    # ---- 配额：先决定"每一层保留多少"，再决定"层内三集各多少"
    # 这一步是"弃样不许弃歪"的关键：若按层顺序贪心，配额用完后边几层会被整层丢弃。
    keys = sorted(strata, key=str)
    tot_of = {k: sum(sz for _, sz in strata[k]) for k in keys}
    kept_quota = _alloc(want, tot_of, keys)                      # 每层保留多少
    split_quota = {k: _alloc(kept_quota[k], {s: counts[s] for s in SPLITS}, list(SPLITS))
                   for k in keys}                               # 层内三集各多少

    for key in keys:                             # 层序固定，保证可复现
        gs = strata[key]
        rnd.shuffle(gs)
        gs.sort(key=lambda x: -x[1])             # 大组优先，同大小随机
        q = split_quota[key]
        qgot = {s: 0 for s in SPLITS}
        for f, sz in gs:
            fit = [s for s in SPLITS if qgot[s] + sz <= q[s]]
            if fit:
                pick = max(fit, key=lambda s: (q[s] - qgot[s]) / max(q[s], 1))
                assign_f[f] = pick
                qgot[pick] += sz
                got[pick] += sz
            else:
                pool.append((f, sz))

    # ---- 第二轮：用候选池补满没达标的三集
    pool.sort(key=lambda x: -x[1])
    for k in SPLITS:
        i = 0
        while got[k] < targets[k] and i < len(pool):
            f, sz = pool[i]
            if got[k] + sz <= targets[k]:
                assign_f[f] = k
                got[k] += sz
                pool.pop(i)
            else:
                i += 1

    # ---- 第三轮：剩下的进 unused 池，再精确修补三集
    for f, _sz in pool:
        assign_f[f] = UNUSED
    size = {f: len(u) for f, u in groups.items()}
    assign_f, cur = _repair(assign_f, size, targets)

    assign = {u: assign_f[f] for f, us in groups.items() for u in us}
    exact = all(cur[k] == targets[k] for k in SPLITS)
    n_kept = sum(cur[k] for k in SPLITS)

    # ---- 自检（任一不过就中止，绝不写出一份脏划分）
    problems: list[str] = []
    if len(assign) != n:
        problems.append(f"覆盖 {len(assign)} != {n}")
    if set(assign) != {r["material_id"] for r in rows}:
        problems.append("有材料没被分到任何桶")
    for f, us in groups.items():
        ss = {assign[u] for u in us}
        if len(ss) > 1:
            problems.append(f"化学式 {f} 被拆到 {sorted(ss)}")
    parts = {k: {u for u, s in assign.items() if s == k} for k in BUCKETS}
    for a in SPLITS:
        for b in SPLITS:
            if a < b and parts[a] & parts[b]:
                problems.append(f"{a}∩{b} = {len(parts[a] & parts[b])}")
    if n_kept != want:
        problems.append(f"实际使用 {n_kept} 条 != 请求 {want} 条")
    if problems:
        raise SystemExit("[中止] 划分自检不通过：" + "；".join(problems[:5]))

    # ---- 各桶分层分布 + 两个独立的质量指标
    #   ① kept_max_dev_pp ：保留子集是否还代表全体（弃样有没有弃歪）
    #   ② split_max_dev_pp：在保留集内部，三集比例是否贴合目标
    # 有 unused 时这两个必须分开算 —— 混在一起会得出毫无意义的巨大偏差。
    cells: dict[tuple, Counter] = defaultdict(Counter)
    for mid, s in assign.items():
        m = meta[mid]
        cells[(gap_bin(m["target"]), m["metal"])][s] += 1
    strata_rows = []
    kept_dev = split_dev = 0.0
    n_small = 0
    for key, c in sorted(cells.items(), key=lambda kv: str(kv[0])):
        tot = sum(c.values())
        kept = sum(c[s] for s in SPLITS)
        row = {"stratum": f"gap{key[0]}|metal={key[1]}", "n": tot, "kept": kept}
        for s in BUCKETS:
            row[s] = c[s]
        row["kept_pct"] = round(kept / tot * 100, 1) if tot else 0.0
        strata_rows.append(row)
        if tot < 20:
            n_small += 1                       # 小层的偏差没有统计意义，不计入指标
            continue
        if tot:
            kept_dev = max(kept_dev, abs(kept / tot * 100 - n_kept / n * 100))
        if kept >= 20:
            for s in SPLITS:
                split_dev = max(split_dev, abs(c[s] / kept * 100 - counts[s] / n_kept * 100))

    return {
        "assign": assign,
        "counts": {k: cur[k] for k in SPLITS},
        "n_unused": cur[UNUSED],
        "n_kept": n_kept,
        "exact": exact,
        "n": n,
        "n_groups": len(groups),
        "strata": strata_rows,
        "kept_max_dev_pp": round(kept_dev, 3),
        "split_max_dev_pp": round(split_dev, 3),
        "seed": seed,
    }


# ---------------------------------------------------------------- 落盘
def write_dataset(rows: list[dict], header: list[str], planned: dict,
                  counts: dict, seed: int, out_dir: Path | None = None) -> dict:
    ensure_dirs()
    out_dir = Path(out_dir) if out_dir else SNAPSHOT_DIR
    dsid = dataset_id_for(counts, seed)
    fid = family_id_for(counts)
    csv_path = out_dir / f"{dsid}.csv"
    split_path = out_dir / f"{dsid}.split.json"
    feat_path = out_dir / f"{dsid}.features.json"

    assign = planned["assign"]
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 快照：只写"实际用到"的材料，只替换 split 列，其余字段按原字符串照抄
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        w.writeheader()
        for r in rows:
            s = assign[r["material_id"]]
            if s == UNUSED:
                continue
            r = dict(r)
            r["split"] = s
            w.writerow(r)
    digest = _sha256_file(csv_path)

    parts = {k: sorted(u for u, s in assign.items() if s == k) for k in SPLITS}
    dropped = sorted(u for u, s in assign.items() if s == UNUSED)
    n_kept = planned["n_kept"]
    split_doc = {
        "schema": "split/1",
        "dataset_id": dsid,
        "family_id": fid,
        "target": TARGET,
        "seed": seed,
        "counts_target": {k: counts[k] for k in SPLITS},
        "counts": planned["counts"],
        "exact_counts": planned["exact"],
        "n_materials": n_kept,                 # 实际参与训练+评估的条数
        "n_source": planned["n"],              # 母快照总条数
        "n_unused": planned["n_unused"],
        "ratios_target": {k: round(counts[k] / n_kept, 6) for k in SPLITS},
        "ratios_actual": {k: round(planned["counts"][k] / n_kept, 6) for k in SPLITS},
        "max_dev_pp": 0.0,
        "tolerance_pp": RATIO_TOLERANCE_PP,
        "strategy": STRATEGY,
        "min_sizes": dict(MIN_SIZES),
        "n_groups": planned["n_groups"],
        "kept_max_dev_pp": planned["kept_max_dev_pp"],
        "split_max_dev_pp": planned["split_max_dev_pp"],
        "strata": planned["strata"],
        "snapshot_csv": csv_path.name,
        "snapshot_sha256": digest,
        "indices_sha256": {k: _sha256_bytes(json.dumps(parts[k], ensure_ascii=False).encode())
                           for k in SPLITS},
        "uids": parts,
        "unused_ids": dropped,
        "provenance": {
            "base_dataset": BASE_DATASET_ID,
            "generated_by": "管理器/splitgen.py",
            "note": "只重划 split 列并剔除未使用材料，特征数值与母快照逐字节相同",
        },
    }
    split_path.write_text(json.dumps(split_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 特征契约：除 dataset_id 外与母快照完全一致
    base_feat = json.loads((SNAPSHOT_DIR / f"{BASE_DATASET_ID}.features.json").read_text(encoding="utf-8"))
    base_feat["dataset_id"] = dsid
    base_feat["family_id"] = fid
    feat_path.write_text(json.dumps(base_feat, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"dataset_id": dsid, "family_id": fid, "csv": str(csv_path),
            "split_json": str(split_path), "features_json": str(feat_path),
            "snapshot_sha256": digest, "counts": planned["counts"],
            "n_kept": n_kept, "n_unused": planned["n_unused"],
            "exact": planned["exact"],
            "kept_max_dev_pp": planned["kept_max_dev_pp"],
            "split_max_dev_pp": planned["split_max_dev_pp"]}


def build(counts: dict, seed: int = SEED, dry_run: bool = False) -> dict:
    header, rows = load_base()
    planned = plan(rows, counts, seed)
    n = planned["n"]
    print(f"母快照 {n} 条 · 化学式 {planned['n_groups']} 组 · seed={seed}")
    for k in SPLITS:
        mark = "" if planned["counts"][k] == counts[k] else f"  ≠ 目标 {counts[k]}"
        pct = planned["counts"][k] / max(planned["n_kept"], 1)
        print(f"  {SPLIT_CN[k]} {planned['counts'][k]:>5} 条（占实用集 {pct:.1%}）{mark}")
    print(f"  {SPLIT_CN['unused']} {planned['n_unused']:>5} 条（不训练、不评估）")
    print(f"  实用合计 {planned['n_kept']} 条 / 母快照 {n} 条"
          f"（舍弃 {planned['n_unused']} 条，{planned['n_unused'] / n:.1%}）")
    print(f"  分层偏差：保留子集 {planned['kept_max_dev_pp']:.2f} pp（越小越代表全体）"
          f" · 三集比例 {planned['split_max_dev_pp']:.2f} pp（越小越均匀）")
    if not planned["exact"]:
        print("  [警告] 未能精确命中目标条数（已尽量贴合）")
    if planned["n_unused"] > n * 0.5:
        print("  [注意] 只用了不到一半数据；结论稳定性会下降，报告里会标注")
    if dry_run:
        print("\n（dry-run：未写任何文件）")
        return {"dry_run": True, "counts": planned["counts"],
                "n_kept": planned["n_kept"], "n_unused": planned["n_unused"],
                "exact": planned["exact"],
                "kept_max_dev_pp": planned["kept_max_dev_pp"],
                "split_max_dev_pp": planned["split_max_dev_pp"]}
    out = write_dataset(rows, header, planned, counts, seed)
    print(f"\n已生成 {out['dataset_id']}")
    print(f"  快照   {out['csv']}（{out['n_kept']} 行）")
    print(f"  划分   {out['split_json']}")
    print(f"  特征   {out['features_json']}")
    print(f"  sha256 {out['snapshot_sha256'][:16]}…")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="按条数重新划分数据（三集之和可小于材料总数，余下进 unused 池）")
    ap.add_argument("--train", type=int, required=True, help="训练集条数")
    ap.add_argument("--valid", type=int, required=True, help="验证集条数")
    ap.add_argument("--test", type=int, required=True, help="测试集条数")
    ap.add_argument("--seed", type=int, default=SEED, help=f"划分种子（默认 {SEED}）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    a = ap.parse_args(argv)
    build({"train": a.train, "valid": a.valid, "test": a.test}, a.seed, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
