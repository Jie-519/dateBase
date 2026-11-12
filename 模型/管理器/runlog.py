# -*- coding: utf-8 -*-
"""runlog：一次训练 = 一个不可变目录 + 一份 manifest.json。

设计要点
--------
1. 本模块**不 import xgboost**，只认 JSON —— 训练用任何框架都能接；
2. 参数/指标只能由训练脚本传进来，这里绝不猜、不补；
3. 特征名单再过一遍黑名单 —— 带隙同源列进不来；
4. manifest 写完即只读：先写 .partial，finish 时原子替换成 manifest.json；
5. 每个产物都算 sha256，`verify()` 可随时复核有没有被改。

用法
----
    from runlog import start, verify
    run = start(project="c2db-gap", model="xgboost", params=params,
                dataset="c2db-kept-20261031", feature_group="G2",
                feature_names=cols, target="gap_pbe")
    ...训练...
    run.metric("valid", "rmse", 0.31, step=50)
    run.artifact("model", "artifacts/model.json")
    print(run.finish(note="baseline"))
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from paths import (DATASET_ID, OUTPUT_ROOT, PROJECT_ROOT, SNAPSHOT_DIR, TARGET,
                   ensure_dirs)

SCHEMA = "run/1"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_info(repo: Path) -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", "-C", str(repo), *args],
                                  capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            return ""
    commit = run("rev-parse", "HEAD")
    dirty = bool(run("status", "--porcelain"))
    return {"repo": str(repo), "commit": commit or None,
            "dirty": dirty, "describe": run("describe", "--tags", "--always") or None}


def _packages(names: Iterable[str]) -> dict:
    from importlib import metadata
    out = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except Exception:
            out[n] = None
    return {k: v for k, v in out.items() if v}


def _load_features(features_file: Path | None) -> dict:
    if features_file is None:
        features_file = SNAPSHOT_DIR / f"{DATASET_ID}.features.json"
    return json.loads(Path(features_file).read_text(encoding="utf-8"))


@dataclass
class Run:
    run_id: str
    run_dir: Path
    doc: dict
    metrics: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)

    # ---------------- 记录
    def metric(self, split: str, name: str, value: float, step: int | None = None) -> None:
        rec = {"split": split, "name": name, "value": float(value)}
        if step is not None:
            rec["step"] = int(step)
        self.metrics.append(rec)

    def artifact(self, kind: str, path: str | Path) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.run_dir / p
        if not p.exists():
            raise FileNotFoundError(f"登记产物失败，文件不存在：{p}")
        self.artifacts.append({
            "kind": kind,
            "path": p.relative_to(self.run_dir).as_posix(),
            "bytes": p.stat().st_size,
            "sha256": _sha256_file(p),
        })
        return p

    def log(self, line: str) -> None:
        with (self.run_dir / "logs" / "train.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{_now()}] {line}\n")

    # ---------------- 收尾
    def finish(self, note: str = "", status: str = "ok") -> Path:
        doc = dict(self.doc)
        doc["status"] = status
        doc["finished_at"] = _now()
        doc["metrics"] = self.metrics
        doc["artifacts"] = self.artifacts
        doc["note"] = note
        path = self.run_dir / "manifest.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        (self.run_dir / "manifest.partial.json").unlink(missing_ok=True)
        return path


def start(project: str, model: str, params: dict, dataset: str = DATASET_ID,
          feature_group: str = "G2", feature_names: list[str] | None = None,
          target: str = TARGET, features_file: Path | None = None,
          split_file: Path | None = None, snapshot_file: Path | None = None,
          model_version: str | None = None, objective: str | None = None,
          deterministic: bool = True, tolerance: dict | None = None,
          tags: list[str] | None = None) -> Run:
    """开一次训练：校验特征 -> 建目录 -> 写 manifest.partial.json。"""
    ensure_dirs()
    cat = _load_features(features_file)
    split_file = Path(split_file) if split_file else SNAPSHOT_DIR / f"{DATASET_ID}.split.json"
    snapshot_file = Path(snapshot_file) if snapshot_file else SNAPSHOT_DIR / f"{DATASET_ID}.csv"

    # ---- 特征校验：黑名单 + 组内成员
    names = list(feature_names or cat["groups"][feature_group])
    black = set(cat["blacklist"])
    hit = sorted(set(names) & black)
    if hit:
        raise SystemExit(f"[中止] 特征里出现黑名单列（带隙同源/管理字段）：{hit}")
    unknown = sorted(set(names) - set(cat["groups"][feature_group]))
    if unknown:
        raise SystemExit(f"[中止] 特征不在 {feature_group} 组内：{unknown[:5]}")

    # ---- 数据指纹
    if not snapshot_file.exists() or not split_file.exists():
        raise SystemExit("缺少快照或划分文件，先运行 管理器/snapshot.py")
    split = json.loads(split_file.read_text(encoding="utf-8"))
    data_sha = _sha256_file(snapshot_file)
    if data_sha != split["snapshot_sha256"]:
        raise SystemExit("[中止] 快照哈希与 split.json 不一致（快照被改过）")

    # ---- 目录
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(json.dumps(
        {"p": params, "g": feature_group, "t": ts}, sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()[:6]
    run_id = f"{ts}_{project}-{digest}"
    run_dir = OUTPUT_ROOT / run_id
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=False)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)

    doc = {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_at": _now(),
        "project": project,
        "task": cat.get("task", "regression"),
        "target": target,
        "model": {"family": model, "version": model_version, "objective": objective},
        "dataset": {
            "dataset_id": dataset,
            "name": snapshot_file.name,
            "sha256": data_sha,
            "rows": split["n_materials"],
            "target": target,
            "split": {
                "file": split_file.name,
                "seed": split["seed"],
                "strategy": split["strategy"],
                "counts": split["counts"],
                "ratios": split["ratios_actual"],
                "max_dev_pp": split["max_dev_pp"],
                "indices_sha256": split["indices_sha256"],
            },
        },
        "features": {
            "group": feature_group,
            "n": len(names),
            "sha256": hashlib.sha256("\n".join(names).encode()).hexdigest(),
            "names": names,
            "catalog": Path(features_file or SNAPSHOT_DIR / f"{DATASET_ID}.features.json").name,
        },
        "params": params,
        "env": {
            "python": platform.python_version(),
            "impl": platform.python_implementation(),
            "os": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "git": _git_info(PROJECT_ROOT),
            "packages": _packages(["xgboost", "numpy", "scipy", "PyYAML"]),
            "reference_repo": {
                "path": "模型/XGboost/xgboost",
                "compiled": False,
                "note": "官方源码仓库仅作参照实现，运行使用 wheel 版 xgboost",
            },
        },
        "reproducibility": {
            "deterministic": bool(deterministic),
            "tolerance": tolerance or {"rmse": 1e-6},
            "notes": "nthread=1 + tree_method=hist + 固定 seed；换机复现按 tolerance 判定",
        },
        "tags": list(tags or []),
    }
    (run_dir / "manifest.partial.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return Run(run_id=run_id, run_dir=run_dir, doc=doc)


# ---------------------------------------------------------------- 复核
def verify(run_dir: str | Path) -> dict:
    """复核一次训练：manifest 在、产物哈希一致、指标非空。"""
    run_dir = Path(run_dir)
    mf = run_dir / "manifest.json"
    problems: list[str] = []
    if not mf.exists():
        return {"ok": False, "problems": ["manifest.json 不存在"]}
    doc = json.loads(mf.read_text(encoding="utf-8"))
    for a in doc.get("artifacts", []):
        p = run_dir / a["path"]
        if not p.exists():
            problems.append(f"产物缺失 {a['path']}")
        elif _sha256_file(p) != a["sha256"]:
            problems.append(f"哈希不一致 {a['path']}")
    if not doc.get("metrics"):
        problems.append("metrics 为空")
    for k in ("run_id", "dataset", "features", "params", "env"):
        if not doc.get(k):
            problems.append(f"manifest 缺字段 {k}")
    return {"ok": not problems, "problems": problems, "run_id": doc.get("run_id"),
            "n_artifacts": len(doc.get("artifacts", [])), "n_metrics": len(doc.get("metrics", []))}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(json.dumps(verify(sys.argv[1]), ensure_ascii=False, indent=2))
    else:
        print("用法: python runlog.py <run 目录>")
