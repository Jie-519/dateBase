# -*- coding: utf-8 -*-
"""C2DB 带隙回归 · XGBoost 训练脚本（可复现版）

职责边界
--------
做：读管理器产出的快照/划分/特征契约 -> 训练 -> 三集评估 -> 落盘产物 -> 写 manifest
不做：不自己划分数据（一律信 split.json）、不画图（归管理器 charts.py）、
      不写数据库（归管理器 record.py）、不猜任何超参（全部来自 config.yaml / 命令行）

一条命令
--------
    python train.py                      # 用 config.yaml 的默认值
    python train.py --features G3 --tag leak-check
    python train.py --dry-run            # 只校验数据装配，不训练

产物（output_XGb/<run_id>/）
-----------------------
    manifest.json                 训练台账（runlog 写）
    artifacts/model.json          XGBoost 模型
    artifacts/predictions.csv     uid, split, y_true, y_pred（三集全量）
    artifacts/feature_importance.csv
    logs/train.log
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

# ---- 目录约定：先接上管理器，再接上随目录带的 xgboost
MANAGER_DIR = Path(__file__).resolve().parents[2] / "管理器"
sys.path.insert(0, str(MANAGER_DIR))

from paths import (DATASET_ID, SNAPSHOT_DIR, TARGET, TRAIN_DIR, XGB_LIBS)  # noqa: E402

sys.path.insert(0, str(XGB_LIBS))

import xgboost as xgb  # noqa: E402

import runlog  # noqa: E402

DEFAULTS = {
    "project": "c2db-gap",
    "features": "G2",
    "target": TARGET,
    "note": "",
    "tags": [],
    "params": {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "n_estimators": 2000,
        "early_stopping_rounds": 50,
        "learning_rate": 0.05,
        "max_depth": 6,
        "min_child_weight": 3,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_lambda": 1.0,
        "tree_method": "hist",
        "nthread": 1,
        "seed": 42,
    },
}


# ---------------------------------------------------------------- 配置
def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))          # 深拷贝
    if path.exists():
        import yaml
        user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for k, v in user.items():
            if k == "params" and isinstance(v, dict):
                cfg["params"].update(v)
            else:
                cfg[k] = v
    return cfg


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="C2DB 带隙回归训练（XGBoost）")
    ap.add_argument("--config", default=str(TRAIN_DIR / "config.yaml"))
    ap.add_argument("--features", choices=["G0", "G1", "G2", "G3", "G3p"])
    ap.add_argument("--target")
    ap.add_argument("--n-estimators", type=int)
    ap.add_argument("--max-depth", type=int)
    ap.add_argument("--learning-rate", type=float)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--nthread", type=int)
    ap.add_argument("--tag", action="append", default=None)
    ap.add_argument("--note", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只装配数据并打印形状")
    return ap.parse_args(argv)


def apply_cli(cfg: dict, a: argparse.Namespace) -> dict:
    if a.features:
        cfg["features"] = a.features
    if a.target:
        cfg["target"] = a.target
    if a.note is not None:
        cfg["note"] = a.note
    if a.tag:
        cfg["tags"] = list(a.tag)
    p = cfg["params"]
    if a.n_estimators is not None:
        p["n_estimators"] = a.n_estimators
    if a.max_depth is not None:
        p["max_depth"] = a.max_depth
    if a.learning_rate is not None:
        p["learning_rate"] = a.learning_rate
    if a.seed is not None:
        p["seed"] = a.seed
    if a.nthread is not None:
        p["nthread"] = a.nthread
    return cfg


# ---------------------------------------------------------------- 数据
def load_dataset(feature_group: str, feature_names: list[str] | None = None):
    csv_path = SNAPSHOT_DIR / f"{DATASET_ID}.csv"
    feat_path = SNAPSHOT_DIR / f"{DATASET_ID}.features.json"
    split_path = SNAPSHOT_DIR / f"{DATASET_ID}.split.json"
    for p in (csv_path, feat_path, split_path):
        if not p.exists():
            raise SystemExit(f"缺少 {p}；先运行 管理器/snapshot.py")

    cat = json.loads(feat_path.read_text(encoding="utf-8"))
    cols = list(feature_names or cat["groups"][feature_group])
    black = set(cat["blacklist"])
    bad = sorted(set(cols) & black)
    if bad:
        raise SystemExit(f"[中止] 特征含黑名单列：{bad}")

    split_doc = json.loads(split_path.read_text(encoding="utf-8"))
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != split_doc["snapshot_sha256"]:
        raise SystemExit("[中止] 快照哈希与 split.json 不符（快照被改过）")

    rows = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        rd = csv.reader(fh)
        header = next(rd)
        missing_cols = [c for c in cols if c not in header]
        if missing_cols:
            raise SystemExit(f"[中止] 快照缺少特征列：{missing_cols[:5]}")
        i_id, i_sp = header.index("material_id"), header.index("split")
        i_y = header.index("target")
        idx = [header.index(c) for c in cols]
        for r in rd:
            rows.append((r[i_id], r[i_sp], r[i_y], [r[i] for i in idx]))

    uids = [r[0] for r in rows]
    seen = set(uids)
    if len(seen) != len(uids):
        raise SystemExit("[中止] 快照里 material_id 有重复")

    # 与 split.json 交叉核对：uid 集合必须一一对上
    for sp in ("train", "valid", "test"):
        want = set(split_doc["uids"][sp])
        have = {r[0] for r in rows if r[1] == sp}
        if want != have:
            raise SystemExit(f"[中止] split.json 的 {sp} 与快照不一致"
                             f"（多 {len(have - want)} / 少 {len(want - have)}）")

    X = np.array([[np.nan if v == "" else float(v) for v in r[3]] for r in rows], dtype=np.float64)
    y = np.array([float(r[2]) for r in rows], dtype=np.float64)
    sp = np.array([r[1] for r in rows])
    if np.isnan(y).any():
        raise SystemExit("[中止] 目标列有空值")
    return {"X": X, "y": y, "split": sp, "uid": np.array(uids, dtype=object),
            "columns": cols, "catalog": cat, "split_doc": split_doc, "csv": csv_path}


# ---------------------------------------------------------------- 指标
def rmse(y, p):
    return float(np.sqrt(np.mean((y - p) ** 2)))


def mae(y, p):
    return float(np.mean(np.abs(y - p)))


def r2(y, p):
    ss_res = float(np.sum((y - p) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


METRICS = {"rmse": rmse, "mae": mae, "r2": r2}


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    a = parse_args(argv)
    cfg = apply_cli(load_config(Path(a.config)), a)
    group = cfg["features"]
    p = cfg["params"]

    data = load_dataset(group)
    X, y, sp, cols = data["X"], data["y"], data["split"], data["columns"]
    mask = {k: (sp == k) for k in ("train", "valid", "test")}
    counts = {k: int(v.sum()) for k, v in mask.items()}
    n_nan = int(np.isnan(X).sum())

    print(f"[数据] {X.shape[0]} 行 × {X.shape[1]} 列；划分 {counts}；缺失单元格 {n_nan}")
    print(f"[特征] 组 {group}；名称 {cols[:6]}{' ...' if len(cols) > 6 else ''}")
    if a.dry_run:
        print("[dry-run] 数据装配通过，未训练")
        return 0

    run = runlog.start(
        project=cfg["project"], model="xgboost", model_version=xgb.__version__,
        params={k: v for k, v in p.items()}, dataset=DATASET_ID,
        feature_group=group, feature_names=cols, target=cfg["target"],
        objective=p.get("objective"), deterministic=(int(p.get("nthread", 1)) == 1),
        tolerance={"rmse": 1e-6} if int(p.get("nthread", 1)) == 1 else {"rmse": 1e-3},
        tags=cfg.get("tags") or [], features_file=SNAPSHOT_DIR / f"{DATASET_ID}.features.json",
        split_file=SNAPSHOT_DIR / f"{DATASET_ID}.split.json",
        snapshot_file=SNAPSHOT_DIR / f"{DATASET_ID}.csv")
    run.log(f"配置 {json.dumps(cfg, ensure_ascii=False)}")
    print(f"[run] {run.run_id} -> {run.run_dir}")

    t0 = time.time()
    dtrain = xgb.DMatrix(X[mask["train"]], label=y[mask["train"]],
                         feature_names=cols, missing=np.nan, nthread=int(p["nthread"]))
    dvalid = xgb.DMatrix(X[mask["valid"]], label=y[mask["valid"]],
                         feature_names=cols, missing=np.nan, nthread=int(p["nthread"]))

    train_params = {k: v for k, v in p.items()
                    if k not in ("n_estimators", "early_stopping_rounds")}
    evals_result: dict = {}
    booster = xgb.train(
        train_params, dtrain, num_boost_round=int(p["n_estimators"]),
        evals=[(dtrain, "train"), (dvalid, "valid")],
        early_stopping_rounds=int(p["early_stopping_rounds"]),
        evals_result=evals_result, verbose_eval=False)
    duration = time.time() - t0
    best_it = int(getattr(booster, "best_iteration", -1))
    print(f"[训练] {duration:.1f}s；best_iteration={best_it}"
          f"（共 {booster.num_boosted_rounds()} 轮）")

    # ---- 学习曲线写进 manifest（每 10 轮一次 + 最后一轮，控制体积）
    for name, hist in evals_result.items():
        for metric, series in hist.items():
            last = len(series) - 1
            for i, v in enumerate(series):
                if i % 10 == 0 or i == last:
                    run.metric(name, metric, v, step=i)

    # ---- 三集评估（test 只在这里算一次）
    preds = {}
    iter_range = (0, best_it + 1) if best_it >= 0 else None
    for k in ("train", "valid", "test"):
        dm = xgb.DMatrix(X[mask[k]], label=y[mask[k]], feature_names=cols,
                         missing=np.nan, nthread=int(p["nthread"]))
        preds[k] = (booster.predict(dm, iteration_range=iter_range)
                    if iter_range else booster.predict(dm))
        for mname, fn in METRICS.items():
            run.metric(k, mname, fn(y[mask[k]], preds[k]))
        print(f"[评估] {k:5s} n={counts[k]:4d}  "
              f"RMSE={rmse(y[mask[k]], preds[k]):.4f}  "
              f"MAE={mae(y[mask[k]], preds[k]):.4f}  "
              f"R²={r2(y[mask[k]], preds[k]):.4f}")

    # ---- 产物
    art = run.run_dir / "artifacts"
    model_path = art / "model.json"
    booster.save_model(str(model_path))

    y_pred_all = np.empty_like(y)
    for k in ("train", "valid", "test"):
        y_pred_all[mask[k]] = preds[k]

    with (art / "predictions.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["material_id", "split", "y_true", "y_pred"])
        for i, uid in enumerate(data["uid"]):
            w.writerow([uid, sp[i], f"{y[i]:.6f}", f"{y_pred_all[i]:.6f}"])

    score = booster.get_score(importance_type="gain")
    weight = booster.get_score(importance_type="weight")
    cover = booster.get_score(importance_type="cover")
    with (art / "feature_importance.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["feature", "gain", "weight", "cover"])
        for name in cols:
            if name in score or name in weight:
                w.writerow([name, f"{score.get(name, 0.0):.6f}",
                            f"{weight.get(name, 0.0):.6f}", f"{cover.get(name, 0.0):.6f}"])

    for kind, name in (("model", "artifacts/model.json"),
                       ("predictions", "artifacts/predictions.csv"),
                       ("feature_importance", "artifacts/feature_importance.csv")):
        run.artifact(kind, name)

    run.doc["training"] = {
        "best_iteration": best_it,
        "n_rounds": int(booster.num_boosted_rounds()),
        "duration_s": round(duration, 2),
        "n_rows": int(X.shape[0]),
        "n_features": int(X.shape[1]),
        "missing_cells": n_nan,
        "split_counts": counts,
    }
    mf = run.finish(note=cfg.get("note", ""))
    print(f"[manifest] {mf}")
    print(f"[复核] {json.dumps(runlog.verify(run.run_dir), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
