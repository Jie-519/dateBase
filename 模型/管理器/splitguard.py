# -*- coding: utf-8 -*-
"""splitguard.py：只回答一个问题——测试集到底干不干净。

硬规则（写在这里，也写在报告与图上）
--------------------------------------
**测试集里的材料，严禁出现在训练集或验证集里。**

这条规则不是靠"记得别混"，而是靠三件事层层卡住：

1. 划分方式：按 `formula` 整组切分（同一化学式的所有材料只会落在一个集里），
   再做 (带隙分箱 × 有无金属) 分层，比例容差 0.5 个百分点，seed 固定 42；
2. 落盘指纹：`split.json` 里存了快照 sha256 与三个集的 `indices_sha256`，
   任何人改了划分，指纹立刻对不上；
3. 入库复核：`verify.py` 每次自检都重新读快照，逐集比对 material_id 与 formula，
   交叠数只要不是 0 就报错 —— 也就是说，"不干净"不可能悄悄留在台账里。

本模块只做第 3 步的计算，不做任何写操作。
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import DATASET_ID, SNAPSHOT_DIR

SPLITS = ("train", "valid", "test")
SPLIT_CN = {"train": "训练集", "valid": "验证集", "test": "测试集"}

_CACHE: dict[tuple, dict] = {}      # (快照, 划分) -> 审计结果，可同时缓存多套数据集


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audit(snapshot_csv: Path | None = None,
          split_json: Path | None = None, refresh: bool = False,
          dataset_id: str | None = None) -> dict:
    """读快照（一列 split 就够），算出三集条数与两两交叠。

    dataset_id 给定时，直接读该数据集自己的快照与划分文件 —— 一套展品一套口径，
    不会拿母快照的数字去标别人家的图。

    返回
    ----
    {
      ok, counts, total, overlaps: {("train","test"): {...}, ...},
      dup_ids, counts_match_split_json, snapshot_sha256_match, problems: [...]
    }
    """
    global _CACHE
    if dataset_id:
        snapshot_csv = SNAPSHOT_DIR / f"{dataset_id}.csv"
        split_json = SNAPSHOT_DIR / f"{dataset_id}.split.json"
    key = (str(snapshot_csv or ""), str(split_json or ""))
    if not refresh and key in _CACHE:
        return _CACHE[key]

    snap = Path(snapshot_csv) if snapshot_csv else SNAPSHOT_DIR / f"{DATASET_ID}.csv"
    # 指定了别的快照却没指定划分文件时，不去和真实 split.json 对账
    # （否则拿测试用的小 csv 去比真实划分，必然"不一致"）
    if split_json:
        spf: Path | None = Path(split_json)
    elif snapshot_csv:
        spf = None
    else:
        spf = SNAPSHOT_DIR / f"{DATASET_ID}.split.json"
    problems: list[str] = []

    ids: dict[str, set] = {s: set() for s in SPLITS}
    forms: dict[str, set] = {s: set() for s in SPLITS}
    counts = {s: 0 for s in SPLITS}
    seen: list[str] = []
    bad_split: set[str] = set()

    with snap.open("r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            s = r["split"]
            if s not in ids:
                bad_split.add(s)
                continue
            counts[s] += 1
            ids[s].add(r["material_id"])
            forms[s].add(r["formula"])
            seen.append(r["material_id"])

    total = sum(counts.values())
    dup_ids = len(seen) - len(set(seen))
    if bad_split:
        problems.append(f"出现未知 split：{sorted(bad_split)}")
    if dup_ids:
        problems.append(f"快照里有 {dup_ids} 个重复 material_id")

    overlaps = {}
    for i, a in enumerate(SPLITS):
        for b in SPLITS[i + 1:]:
            io = ids[a] & ids[b]
            fo = forms[a] & forms[b]
            overlaps[(a, b)] = {"ids": len(io), "formulas": len(fo),
                                "sample_ids": sorted(io)[:5],
                                "sample_formulas": sorted(fo)[:5]}
            if io:
                problems.append(f"{SPLIT_CN[a]}∩{SPLIT_CN[b]} 材料交叠 {len(io)} 个")
            if fo:
                problems.append(f"{SPLIT_CN[a]}∩{SPLIT_CN[b]} 化学式交叠 {len(fo)} 个"
                                f"（化学式被拆到两集 = 同族材料跨集泄漏）")

    # 与 split.json 对账
    counts_match = sha_match = None
    n_source = total          # 母快照总条数（含未使用）
    n_unused = 0
    if spf is not None and spf.exists():
        sp = json.loads(spf.read_text(encoding="utf-8"))
        counts_match = sp.get("counts") == counts and sp.get("n_materials") == total
        if not counts_match:
            problems.append(f"分集条数与 split.json 不一致：{counts} vs {sp.get('counts')}")
        sha_match = _sha256(snap) == sp.get("snapshot_sha256")
        if not sha_match:
            problems.append("快照 sha256 与 split.json 不一致（快照被换过）")
        # 自定义划分会把"没用上的材料"记在 split.json 里 —— 快照 CSV 里没有它们，
        # 所以总条数与弃用条数只能从这里取，不能拿 CSV 行数糊弄。
        n_source = int(sp.get("n_source", total))
        n_unused = int(sp.get("n_unused", max(0, n_source - total)))

    _CACHE[key] = {
        "ok": not problems,
        "counts": counts,
        "total": total,
        "overlaps": {f"{a}-{b}": v for (a, b), v in overlaps.items()},
        "n_unique_ids": len(set(seen)),
        "dup_ids": dup_ids,
        "n_formulas": len(set().union(*forms.values())),
        "counts_match_split_json": counts_match,
        "snapshot_sha256_match": sha_match,
        "problems": problems,
        "snapshot": snap.name,
        "split_json": spf.name if spf else None,
        "n_source": n_source,
        "n_unused": n_unused,
    }
    return _CACHE[key]


def counts_line(fmt: str = "{train}/{valid}/{test}", dataset_id: str | None = None) -> str:
    a = audit(dataset_id=dataset_id)
    return fmt.format(**a["counts"])


def fig_footer_text(dataset_id: str | None = None) -> str:
    """图下方那行固定声明（每张图都有，避免"这张图用了多少数据"靠猜）。"""
    a = audit(dataset_id=dataset_id)
    c = a["counts"]
    tag = "已核：零交叠" if a["ok"] else "警告：分集有交叠"
    extra = ""
    if a.get("n_unused"):
        extra = f"（另有 {a['n_unused']} 条未使用，不参与训练/评估）"
    return (f"训练集 {c['train']} 条 · 验证集 {c['valid']} 条 · 测试集 {c['test']} 条{extra}"
            f"  ｜  测试集材料严禁进入训练集（{tag}）")


def main(argv: list[str] | None = None) -> int:
    a = audit()
    print(f"快照      {a['snapshot']}（{a['total']} 行，{a['n_formulas']} 个化学式）")
    for s in SPLITS:
        print(f"  {SPLIT_CN[s]:<4} {a['counts'][s]:>5} 条")
    print("\n两两交叠：")
    for k, v in a["overlaps"].items():
        print(f"  {k:<12} 材料 {v['ids']} 个 · 化学式 {v['formulas']} 个")
    print(f"\n重复 material_id：{a['dup_ids']}")
    print(f"与 split.json 对账：条数 {a['counts_match_split_json']}，"
          f"快照指纹 {a['snapshot_sha256_match']}")
    print(f"\n结论：{'测试集干净（零交叠）' if a['ok'] else '不合格 —— ' + '；'.join(a['problems'])}")
    return 0 if a["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
