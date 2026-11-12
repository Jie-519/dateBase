# -*- coding: utf-8 -*-
"""集成操作：输入三个条数 → 划分 → 训练 → 收录 → 出图 → 出报告，一步到底。

双击 `开始训练.bat` 即进入交互模式；也可以在命令行直接给参数：

    python run.py --train 2349 --valid 505 --test 500
    python run.py --train 1500 --valid 200 --test 200 --groups G2 --seed 42

它会做的事（每一步都会打印耗时）：
    1. 校验条数（下限、上限、三者之和）
    2. 生成划分          -> snapshots/<dataset_id>.{csv,split.json,features.json}
    3. 逐组训练          -> output_XGb/<run_id>/（调用原版 train.py，不改它一行）
    4. 收录台账          -> catalog.sqlite
    5. 出图 + 出报告     -> figure_XGb/exhibit/<dataset_id>/index.html
    6. 复核              -> 三集条数与零交叠

设计约束：本脚本只做编排，不实现算法。划分在 splitgen，训练在 train.py，
出图在 charts，报告在 report —— 任何一步出错，单独重跑那一步即可。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MANAGER = Path(__file__).resolve().parent.parent / "模型" / "管理器"
sys.path.insert(0, str(MANAGER))

import charts          # noqa: E402
import db              # noqa: E402
import record          # noqa: E402
import report          # noqa: E402
import splitguard      # noqa: E402
import splitgen        # noqa: E402
from paths import (BASE_DATASET_ID, EXHIBIT_ROOT, MAX_EXHIBITS, MIN_SIZES,  # noqa: E402
                   OUTPUT_ROOT, SEED, SNAPSHOT_DIR, TRAIN_DIR)

GROUPS_ALL = ["G0", "G1", "G2", "G3", "G3p"]
GROUPS_DEFAULT = ["G2", "G3", "G3p"]
LINE = "─" * 62


def say(msg: str = "", end: str = "\n") -> None:
    print(msg, end=end, flush=True)


def hr(title: str = "") -> None:
    say()
    say(LINE)
    if title:
        say(f" {title}")
        say(LINE)


def base_total() -> int:
    """母快照有多少条材料。"""
    doc = json.loads((SNAPSHOT_DIR / f"{BASE_DATASET_ID}.split.json").read_text(encoding="utf-8"))
    return int(doc["n_materials"])


def existing_exhibits() -> list[Path]:
    if not EXHIBIT_ROOT.exists():
        return []
    return sorted([p for p in EXHIBIT_ROOT.iterdir() if p.is_dir()],
                  key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------- 输入
def _clean(raw: str) -> str:
    """吃掉粘贴带来的隐形字符：BOM、全角空格、千分位逗号。"""
    return (raw.replace("\ufeff", "").replace("\u3000", " ").strip()
            .replace("，", "").replace(",", "").replace(" ", ""))


def _read(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        say("\n  输入已结束，退出。")
        raise SystemExit(130)


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = _clean(_read(f"  {prompt}（默认 {default}）: "))
        if raw == "":
            return default
        try:
            v = int(raw)
        except ValueError:
            say(f"    ✗ 要输入整数，收到 {raw!r}")
            continue
        if v < lo or v > hi:
            say(f"    ✗ 必须在 {lo} ~ {hi} 之间")
            continue
        return v


def ask_choice(prompt: str, options: list[str], default: int = 1) -> int:
    for i, o in enumerate(options, 1):
        say(f"    [{i}] {o}")
    while True:
        raw = _clean(_read(f"  {prompt}（默认 {default}）: "))
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        say(f"    ✗ 请输入 1~{len(options)}")


def interactive() -> dict:
    n = base_total()
    hr("第 1 步 / 3　输入三个条数")
    say(f"  材料总数 {n} 条（比这多就分不出来；比这少的部分不参与训练和评估）")
    say(f"  下限：训练 ≥ {MIN_SIZES['train']}　验证 ≥ {MIN_SIZES['valid']}　测试 ≥ {MIN_SIZES['test']}")
    say()
    while True:
        tr = ask_int("训练集条数", 2349, MIN_SIZES["train"], n)
        va = ask_int("验证集条数", 505, MIN_SIZES["valid"], n)
        te = ask_int("测试集条数", 500, MIN_SIZES["test"], n)
        total = tr + va + te
        if total > n:
            say(f"    ✗ 三者之和 {total} 超过材料总数 {n}，请重输")
            continue
        unused = n - total
        say(f"    → 实用 {total} 条" + (f"，弃用 {unused} 条" if unused else "，用满"))
        break

    hr("第 2 步 / 3　选要跑的特征组")
    pick = ask_choice("用哪套？（五组全跑约 8~15 秒，只跑主模型更快）",
                      ["五组全跑（G0 G1 G2 G3 G3p）—— 报告最完整",
                       "只跑主模型（G2）—— 最快",
                       "主模型 + 两组泄漏对照（G2 G3 G3p）"], default=1)
    groups = {1: GROUPS_ALL, 2: ["G2"], 3: GROUPS_DEFAULT}[pick]

    seed = SEED
    if pick:
        hr("第 3 步 / 3　确认")
        say(f"  训练 / 验证 / 测试 : {tr} / {va} / {te}")
        say(f"  特征组            : {' '.join(groups)}")
        say(f"  划分种子          : {seed}")
        say(f"  预计耗时          : 约 {len(groups) * 3 + 6} 秒")
        ans = _clean(_read("  开始？(Y/n): ")).lower()
        if ans not in ("", "y", "yes", "是"):
            say("  已取消。")
            raise SystemExit(0)
    return {"train": tr, "valid": va, "test": te, "seed": seed, "groups": groups}


# ---------------------------------------------------------------- 训练
def final_metrics(run_dir: Path) -> dict:
    doc = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    out = {}
    for m in doc.get("metrics", []):
        if m.get("step") is None:
            out[f"{m['split']}_{m['name']}"] = m["value"]
    return out


def train_one(env: dict, group: str, seed: int, note: str) -> tuple[Path, dict]:
    before = {p.name for p in OUTPUT_ROOT.iterdir() if p.is_dir()}
    cmd = [sys.executable, "train.py", "--config", "config.yaml",
           "--features", group, "--seed", str(seed), "--note", note]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(TRAIN_DIR), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0
    if r.returncode != 0:
        say(f"    ✗ {group} 训练失败（{r.returncode}）")
        say((r.stdout or "")[-1500:])
        say((r.stderr or "")[-1500:])
        raise SystemExit(2)
    new = [p for p in OUTPUT_ROOT.iterdir() if p.is_dir() and p.name not in before]
    if not new:
        raise SystemExit("训练成功但找不到新的 run 目录，请检查 output_XGb")
    run_dir = max(new, key=lambda p: p.stat().st_mtime)
    return run_dir, final_metrics(run_dir) | {"_sec": dt}


# ---------------------------------------------------------------- 主流程
def run(counts: dict, groups: list[str], seed: int = SEED, auto_open: bool = False) -> int:
    t_all = time.time()
    n = base_total()
    total = sum(counts.values())
    if total > n:
        say(f"[拒绝] 三者之和 {total} 超过材料总数 {n}")
        return 2
    for k, cn in (("train", "训练集"), ("valid", "验证集"), ("test", "测试集")):
        if counts[k] < MIN_SIZES[k]:
            say(f"[拒绝] {cn} {counts[k]} 条 < 下限 {MIN_SIZES[k]}")
            return 2

    hr("① 生成划分")
    t0 = time.time()
    out = splitgen.build(counts, seed)
    dsid = out["dataset_id"]
    say(f"    完成（{time.time() - t0:.1f}s）　{dsid}")

    show = existing_exhibits()
    if len(show) >= MAX_EXHIBITS:
        say(f"\n  [提示] 展品目录已有 {len(show)} 套（上限 {MAX_EXHIBITS}），"
            f"本程序不自动删除，需要时请手动清理：{EXHIBIT_ROOT}")

    hr("② 训练")
    env = dict(os.environ, C2DB_DATASET_ID=dsid, PYTHONIOENCODING="utf-8")
    note = f"集成操作 {dsid}"
    rows = []
    for g in groups:
        say(f"  [{g}] 训练中 ...", end=" ")
        run_dir, m = train_one(env, g, seed, note)
        say(f"完成 {m['_sec']:.1f}s　valid R²={m.get('valid_r2', float('nan')):.4f}　"
            f"test R²={m.get('test_r2', float('nan')):.4f}")
        rows.append((g, run_dir.name, m))

    hr("③ 收录台账")
    res = record.record_all()
    ok = [r for r in res if r.get("recorded")]
    bad = [r for r in res if not r.get("recorded")]
    say(f"    收录 {len(ok)} 个 run" + (f"，{len(bad)} 个被拒（见上）" if bad else ""))

    exhibit = EXHIBIT_ROOT / dsid
    hr("④ 出图")
    t0 = time.time()
    figs = charts.build_all(out_dir=exhibit, dataset_id=dsid)
    say(f"    {len(figs)} 张图（{time.time() - t0:.1f}s）")

    hr("⑤ 出报告")
    t0 = time.time()
    pages = report.build_all(out_dir=exhibit, dataset_id=dsid)
    say(f"    {len(pages)} 个页面（{time.time() - t0:.1f}s）")

    hr("⑥ 复核")
    a = splitguard.audit(dataset_id=dsid, refresh=True)
    c = a["counts"]
    ov = a["overlaps"]
    say(f"    训练 {c['train']} / 验证 {c['valid']} / 测试 {c['test']}"
        f"（实用 {sum(c.values())}，弃用 {a.get('n_unused', 0)}）")
    say(f"    测试集隔离：train∩test={ov['train-test']['ids']}　"
        f"valid∩test={ov['valid-test']['ids']}　"
        f"化学式交叠={ov['train-test']['formulas']}"
        f"　→ {'干净 ✓' if a['ok'] else '异常 ✗'}")

    hr("完成")
    say(f"  展品首页  {exhibit / 'index.html'}")
    say(f"  总耗时    {time.time() - t_all:.1f} 秒")
    say()
    say("  各组结果：")
    for g, rid, m in rows:
        say(f"    {g:<4} valid R² {m.get('valid_r2', 0):.4f}　"
            f"test R² {m.get('test_r2', 0):.4f}　"
            f"test RMSE {m.get('test_rmse', 0):.3f} eV")
    if auto_open:
        try:
            os.startfile(str(exhibit / "index.html"))   # noqa: S606
            say("\n  已在浏览器打开。")
        except Exception as e:
            say(f"\n  打开失败：{e}")
    return 0 if a["ok"] else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C2DB 集成操作：条数 → 划分 → 训练 → 报告")
    ap.add_argument("--train", type=int, help="训练集条数（不给则交互询问）")
    ap.add_argument("--valid", type=int, help="验证集条数")
    ap.add_argument("--test", type=int, help="测试集条数")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--groups", default=None, help="逗号分隔，如 G2,G3；默认三组 G2,G3,G3p")
    ap.add_argument("--open", action="store_true", help="完成后自动打开首页")
    a = ap.parse_args(argv)

    hr("C2DB 带隙预测 · 集成操作")
    say(f"  母快照 {BASE_DATASET_ID} · {base_total()} 条材料")
    say(f"  时间 {datetime.now():%Y-%m-%d %H:%M}")

    if a.train and a.valid and a.test:
        counts = {"train": a.train, "valid": a.valid, "test": a.test}
        groups = [g.strip() for g in (a.groups or "G2,G3,G3p").split(",") if g.strip()]
    else:
        cfg = interactive()
        counts, groups, a.seed = ({"train": cfg["train"], "valid": cfg["valid"],
                                   "test": cfg["test"]}, cfg["groups"], cfg["seed"])

    bad = [g for g in groups if g not in GROUPS_ALL]
    if bad:
        say(f"[拒绝] 未知特征组：{bad}；可用 {GROUPS_ALL}")
        return 2
    return run(counts, groups, a.seed, auto_open=a.open)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stdout.write("\n已中断。\n")
        raise SystemExit(130)
