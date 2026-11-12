# -*- coding: utf-8 -*-
"""compare.py：复现校验 —— 用同一条历史训练记录的参数重跑一次，比指标。

为什么需要它
------------
"能复现"不等于"能跑"。仓库里存着每次训练的 manifest（参数、数据指纹、指标），
但没有任何工具去验证"照它再跑一遍，能不能得到同样的数"。本脚本补这一环：

    读历史 manifest -> 检查快照哈希没变 -> 用同样的参数重跑 -> 逐项比对 -> PASS/FAIL

判定口径
--------
容差取自历史 manifest 的 reproducibility.tolerance（单线程 RMSE 1e-6、
多线程 1e-3）；r2 / mae 缺省同样用 1e-6。逐项比 |本次 - 历史| <= 容差。

用法
----
    python compare.py <run_id 前缀>          # 重跑并比对，保留新 run 目录
    python compare.py c07a0c --rm            # 比对完删掉新 run 目录
    python compare.py c07a0c --tol 1e-3      # 手工放宽容差

退出码：0 全过；1 有指标超差；2 环境/数据不对（快照哈希不符、缺文件）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import (DATA_ROOT, SNAPSHOT_DIR, TARGET, TRAIN_DIR,  # noqa: E402
                   XGB_LIBS)

PY = sys.executable
LINE = "─" * 66


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def _find_runs(prefix: str) -> list[Path]:
    out = []
    for d in sorted(DATA_ROOT.glob("*/*")):
        if d.is_dir() and (d / "manifest.json").exists() and prefix in d.name:
            out.append(d)
    return out


def _finals(doc: dict) -> dict[tuple[str, str], float]:
    return {(m["split"], m["name"]): float(m["value"])
            for m in doc.get("metrics", []) if m.get("step") is None}


def _check_snapshot(doc: dict) -> list[str]:
    """重跑前先确认数据没被换过 —— 否则比出来的差异毫无意义。"""
    ds = doc["dataset"]
    csv = SNAPSHOT_DIR / ds["name"]
    if not csv.exists():
        return [f"快照不存在：{csv}"]
    import hashlib
    got = hashlib.sha256(csv.read_bytes()).hexdigest()
    if got != ds["sha256"]:
        return [f"快照哈希不符：记录 {ds['sha256'][:16]}… 实际 {got[:16]}…"
                f"（数据被改过，或 clone 时换行符被转换）"]
    return []


def _write_config(doc: dict, path: Path) -> None:
    import yaml
    cfg = {
        "project": doc.get("project", "c2db-gap"),
        "features": doc["features"]["group"],
        "target": doc.get("target", TARGET),
        "note": f"repro of {doc['run_id']}",
        "tags": ["repro"],
        "params": doc.get("params") or {},
    }
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def _rerun(doc: dict, config: Path) -> tuple[Path, dict]:
    before = {p.name for p in DATA_ROOT.glob("*/*") if p.is_dir()}
    env = {"C2DB_DATASET_ID": doc["dataset"]["dataset_id"], "PYTHONIOENCODING": "utf-8"}
    import os
    full_env = dict(os.environ, **env)
    cmd = [PY, "train.py", "--config", str(config),
           "--features", doc["features"]["group"], "--tag", "repro"]
    r = subprocess.run(cmd, cwd=str(TRAIN_DIR), env=full_env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print((r.stdout or "")[-1200:])
        print((r.stderr or "")[-1200:])
        raise SystemExit(f"[中止] 重跑失败（退出码 {r.returncode}）")
    new = [p for p in DATA_ROOT.glob("*/*") if p.is_dir() and p.name not in before]
    if not new:
        raise SystemExit("[中止] 重跑成功但找不到新 run 目录")
    run_dir = max(new, key=lambda p: p.stat().st_mtime)
    return run_dir, _manifest(run_dir)


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    prefix = argv[0]
    rm = "--rm" in argv
    tol_override = None
    if "--tol" in argv:
        tol_override = float(argv[argv.index("--tol") + 1])

    runs = _find_runs(prefix)
    if not runs:
        print(f"没有匹配 {prefix!r} 的 run（在 {DATA_ROOT} 下找）")
        return 2
    if len(runs) > 1:
        print(f"匹配到 {len(runs)} 个，请给更长的前缀：")
        for r in runs:
            print("  " + r.name)
        return 2

    old_dir = runs[0]
    old = _manifest(old_dir)
    tol = dict(old.get("reproducibility", {}).get("tolerance") or {})
    print(LINE)
    print(f" 复现校验：{old['run_id']}")
    print(LINE)
    print(f" 数据   {old['dataset']['dataset_id']}  {old['dataset']['rows']} 行")
    print(f" 特征   {old['features']['group']}（{old['features']['n']} 列）")
    print(f" 模型   {old['model']['family']} {old['model'].get('version') or ''}")
    print(f" 容差   {tol or '{}'}   （--tol 可覆盖）")

    print("\n① 核对快照")
    problems = _check_snapshot(old)
    if problems:
        for p in problems:
            print("   ✗ " + p)
        print("\n   数据对不上，重跑没有意义。先修数据（见 README 的换行符一节）。")
        return 2
    print("   ✓ 哈希与 manifest 记录一致")

    print("\n② 用同样的参数重跑")
    TMP = Path(__file__).resolve().parent / "_repro_config.yaml"
    _write_config(old, TMP)
    t0 = datetime.now()
    new_dir, new = _rerun(old, TMP)
    TMP.unlink(missing_ok=True)
    print(f"   ✓ {new_dir.name}（{(datetime.now() - t0).total_seconds():.1f}s）")

    print("\n③ 逐项比对")
    a, b = _finals(old), _finals(new)
    rows = []
    for key in sorted(a):
        if key not in b:
            rows.append((key[0], key[1], a[key], None, None, None, "缺失"))
            continue
        diff = abs(b[key] - a[key])
        lim = tol_override if tol_override is not None else tol.get(key[1], 1e-6)
        rows.append((key[0], key[1], a[key], b[key], diff, lim,
                     "PASS" if diff <= lim else "FAIL"))
    print(f"   {'集合':<6}{'指标':<7}{'历史':>12}{'本次':>12}{'差值':>11}{'容差':>10}  结论")
    print("   " + "-" * 64)
    bad = 0
    for sp, nm, va, vb, diff, lim, verdict in rows:
        if verdict == "FAIL":
            bad += 1
        print(f"   {sp:<6}{nm:<7}{va:>12.6f}"
              f"{(f'{vb:.6f}' if vb is not None else '—'):>12}"
              f"{(f'{diff:.2e}' if diff is not None else '—'):>11}"
              f"{(f'{lim:.0e}' if lim is not None else '—'):>10}  {verdict}")

    print(LINE)
    if bad == 0:
        print(f" 结论：PASS —— {len(rows)} 项指标全部落在容差内")
    else:
        print(f" 结论：FAIL —— {bad}/{len(rows)} 项超差")
    print(f" 新 run：{new_dir}")
    print(" 如需保留以便细看，不加 --rm 即可；否则重跑到同一条件会不断累积目录。")

    if rm:
        shutil.rmtree(new_dir, ignore_errors=True)
        print(f"\n 已删除 {new_dir.name}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
