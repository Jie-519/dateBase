# -*- coding: utf-8 -*-
"""record.py：把 训练数据/<模型名>/ 下的训练产物收进台账（catalog.sqlite）。

职责边界
--------
* 只读 run 目录，**绝不改**训练产物；
* 校验归 runlog.verify，本模块只补它管不到的事：
    - manifest 自身指纹（防手改数值）
    - 特征名单是否与 features.json 一致（防"名字叫 G2、实际塞了别的列"）
    - 黑名单再查一遍（防绕过 runlog 手工造 manifest）
    - 划分计数、预测行数、重要性行数是否自洽
    - 打标：冒烟测试 / 泄漏对照（这两类不能混进选型榜）
* 收录是幂等的：同一 run_id 重收 = 先删后插，不产生重复行。

用法
----
    python record.py            # 收录全部
    python record.py --check    # 只体检，不写库
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 允许被别处 import

import db
import runlog
from paths import DATASET_ID, OUTPUT_ROOT, PROJECT_ROOT, SNAPSHOT_DIR, ensure_dirs

#: 同源泄漏列（与目标由同一套 DFT 计算派生的量）
SAME_SOURCE_FEATURES = {"eff_mass_cbm", "eff_mass_vbm",
                        "polarizability_el", "polarizability_total"}
#: 答案泄漏列（直接就是带隙的一个分量）
ANSWER_FEATURES = {"gap_dir_pbe", "gap_kind_pbe", "gap_m", "gap_dft"}
#: 轮数不超过这个数的一律算冒烟测试（20 轮的试跑不该出现在榜上）
SMOKE_MAX_ROUNDS = 50


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def _load_features_catalog() -> dict:
    return json.loads((SNAPSHOT_DIR / f"{DATASET_ID}.features.json").read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- 体检
def inspect(run_dir: Path) -> dict:
    """把一个 run 目录读成"待入库的数据 + 问题清单"。不写库。"""
    run_dir = Path(run_dir)
    hard: list[str] = []          # 致命问题：不收
    soft: list[str] = []          # 可容忍：收，但标注

    mf = run_dir / "manifest.json"
    if not mf.exists():
        return {"run_dir": run_dir, "ok": False,
                "hard": ["manifest.json 不存在"], "soft": [],
                "doc": None, "data": None}

    raw = mf.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    manifest_sha = hashlib.sha256(raw).hexdigest()

    # ---- 1. runlog 原生复核
    v = runlog.verify(run_dir)
    for p in v["problems"]:
        (soft if p.startswith("产物缺失") else hard).append(p)

    # ---- 2. 特征名单：指纹 + 组内 + 黑名单
    cat = _load_features_catalog()
    feats = doc.get("features", {})
    names = feats.get("names") or []
    group = feats.get("group")
    if hashlib.sha256("\n".join(names).encode()).hexdigest() != feats.get("sha256"):
        hard.append("特征名与 features.sha256 对不上（manifest 被改过？）")
    if len(names) != feats.get("n"):
        hard.append(f"特征数不一致：names={len(names)} vs n={feats.get('n')}")
    groups = cat["groups"]
    if group not in groups:
        hard.append(f"未知特征组 {group}")
    else:
        extra = sorted(set(names) - set(groups[group]))
        if extra:
            hard.append(f"{group} 里出现组外列：{extra[:5]}")
    black = sorted(set(names) & set(cat["blacklist"]))
    if black:
        hard.append(f"特征里出现黑名单列：{black}")

    # ---- 3. 数据指纹：快照 / 划分
    dset = doc.get("dataset", {})
    split_info = dset.get("split", {})
    split_file = SNAPSHOT_DIR / split_info.get("file", f"{DATASET_ID}.split.json")
    if split_file.exists():
        sp = json.loads(split_file.read_text(encoding="utf-8"))
        if sp.get("snapshot_sha256") != dset.get("sha256"):
            hard.append("manifest 里的快照哈希与 split.json 不一致")
        counts = split_info.get("counts") or {}
        if counts and sum(counts.values()) != dset.get("rows"):
            hard.append(f"划分计数之和 {sum(counts.values())} != 行数 {dset.get('rows')}")
    else:
        soft.append(f"划分文件缺失：{split_file.name}")

    # ---- 4. 产物内容自洽
    pred_path = run_dir / "artifacts" / "predictions.csv"
    fi_path = run_dir / "artifacts" / "feature_importance.csv"
    preds: list[dict] = []
    fis: list[dict] = []
    if pred_path.exists():
        preds = _read_csv(pred_path)
        bad_split = {r["split"] for r in preds} - {"train", "valid", "test"}
        if bad_split:
            hard.append(f"predictions 出现未知 split：{sorted(bad_split)}")
        if len(preds) != dset.get("rows"):
            hard.append(f"predictions 行数 {len(preds)} != 样本数 {dset.get('rows')}")
        if len({r["material_id"] for r in preds}) != len(preds):
            hard.append("predictions 里 material_id 有重复")
        got = {}
        for r in preds:
            got[r["split"]] = got.get(r["split"], 0) + 1
        for k, n in (split_info.get("counts") or {}).items():
            if k in got and got[k] != n:
                hard.append(f"{k} 集预测条数 {got[k]} != 划分计数 {n}")
    if fi_path.exists():
        fis = _read_csv(fi_path)
        used = {r["feature"] for r in fis}
        extra_fi = sorted(used - set(names))
        if extra_fi:
            hard.append(f"重要性表出现组外特征：{extra_fi[:5]}")
        # xgboost 的 get_score 只返回"被树用到的"特征，gain=0 的不出现；
        # 因此行数少于特征数是正常现象，不算错，只记一笔。
        n_unused = len(set(names) - used)
        if n_unused:
            soft.append(f"{n_unused} 个特征未被任何树使用（gain=0，xgboost 不输出该行）")

    # ---- 5. 指标齐全性
    metrics = doc.get("metrics") or []
    finals = {(m["split"], m["name"]) for m in metrics if m.get("step") is None}
    need = {(s, n) for s in ("train", "valid", "test") for n in ("r2", "rmse")}
    lack = sorted(need - finals)
    if lack:
        hard.append(f"缺少最终指标：{lack}")

    # ---- 6. 打标
    tags = [str(t) for t in (doc.get("tags") or [])]
    training = doc.get("training") or {}
    n_rounds = training.get("n_rounds")
    is_smoke = int("smoke" in tags or (isinstance(n_rounds, int) and n_rounds <= SMOKE_MAX_ROUNDS))
    named = set(names)
    is_leak = int(bool(named & SAME_SOURCE_FEATURES) or bool(named & ANSWER_FEATURES)
                  or any(t.startswith("leak-") for t in tags))

    data = {
        "run_id": doc["run_id"],
        "run_dir": _rel(run_dir),
        "schema_version": doc.get("schema", ""),
        "project": doc.get("project", ""),
        "task": doc.get("task"),
        "model_family": (doc.get("model") or {}).get("family"),
        "model_version": (doc.get("model") or {}).get("version"),
        "objective": (doc.get("model") or {}).get("objective"),
        "target": doc.get("target", ""),
        "dataset_id": dset.get("dataset_id", ""),
        "dataset_sha256": dset.get("sha256", ""),
        "snapshot_rows": int(dset.get("rows") or 0),
        "split_seed": split_info.get("seed"),
        "split_strategy": split_info.get("strategy"),
        "split_counts_json": json.dumps(split_info.get("counts") or {}, ensure_ascii=False),
        "feature_group": group or "",
        "n_features": len(names),
        "features_sha256": feats.get("sha256", ""),
        "params_json": json.dumps(doc.get("params") or {}, ensure_ascii=False, sort_keys=True),
        "best_iteration": training.get("best_iteration"),
        "n_rounds": n_rounds,
        "duration_s": training.get("duration_s"),
        "missing_cells": training.get("missing_cells"),
        "deterministic": int(bool((doc.get("reproducibility") or {}).get("deterministic"))),
        "tolerance_json": json.dumps((doc.get("reproducibility") or {}).get("tolerance") or {},
                                     ensure_ascii=False),
        "env_python": (doc.get("env") or {}).get("python"),
        "env_os": (doc.get("env") or {}).get("os"),
        "env_git_commit": ((doc.get("env") or {}).get("git") or {}).get("commit"),
        "env_git_dirty": int(bool(((doc.get("env") or {}).get("git") or {}).get("dirty"))),
        "status": doc.get("status", "unknown"),
        "note": doc.get("note") or "",
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "created_at": doc.get("created_at", ""),
        "finished_at": doc.get("finished_at", ""),
        "manifest_sha256": manifest_sha,
        "manifest_bytes": len(raw),
        "verified": int(not hard),
        "verify_problems": "\n".join(hard + soft),
        "is_smoke": is_smoke,
        "is_leak": is_leak,
    }

    metrics_rows = [{"run_id": doc["run_id"], "split": m["split"], "name": m["name"],
                     "step": -1 if m.get("step") is None else int(m["step"]),
                     "value": float(m["value"])} for m in metrics]
    artifacts_rows = [{"run_id": doc["run_id"], "path": a["path"], "kind": a["kind"],
                       "bytes": int(a.get("bytes", 0)), "sha256": a.get("sha256", ""),
                       "present": int((run_dir / a["path"]).exists())}
                      for a in (doc.get("artifacts") or [])]
    pred_rows = [{"run_id": doc["run_id"], "material_id": r["material_id"],
                  "split": r["split"], "y_true": float(r["y_true"]),
                  "y_pred": float(r["y_pred"])} for r in preds]
    fi_rows = []
    if fis:
        order = sorted(fis, key=lambda r: -float(r["gain"]))
        rank = {r["feature"]: i + 1 for i, r in enumerate(order)}
        orderw = sorted(fis, key=lambda r: -float(r["weight"]))
        rankw = {r["feature"]: i + 1 for i, r in enumerate(orderw)}
        fi_rows = [{"run_id": doc["run_id"], "feature": r["feature"],
                    "gain": float(r["gain"]), "weight": float(r["weight"]),
                    "cover": float(r["cover"]),
                    "rank_gain": rank[r["feature"]], "rank_weight": rankw[r["feature"]]}
                   for r in fis]

    return {"run_dir": run_dir, "ok": not hard, "hard": hard, "soft": soft,
            "doc": doc, "data": data, "metrics": metrics_rows,
            "artifacts": artifacts_rows, "predictions": pred_rows,
            "importance": fi_rows, "n_features": len(names)}


# ---------------------------------------------------------------- 入库
RUN_COLS = ["run_id", "run_dir", "schema_version", "project", "task", "model_family",
            "model_version", "objective", "target", "dataset_id", "dataset_sha256",
            "snapshot_rows", "split_seed", "split_strategy", "split_counts_json",
            "feature_group", "n_features", "features_sha256", "params_json",
            "best_iteration", "n_rounds", "duration_s", "missing_cells",
            "deterministic", "tolerance_json", "env_python", "env_os",
            "env_git_commit", "env_git_dirty", "status", "note", "tags_json",
            "created_at", "finished_at", "manifest_sha256", "manifest_bytes",
            "verified", "verify_problems", "is_smoke", "is_leak", "imported_at"]


def record_run(conn, run_dir: Path, dry_run: bool = False) -> dict:
    info = inspect(run_dir)
    if not info["ok"]:
        return {"run_id": info["data"]["run_id"] if info["data"] else run_dir.name,
                "recorded": False, "why": info["hard"]}
    if dry_run:
        return {"run_id": info["data"]["run_id"], "recorded": False, "why": "dry-run",
                "soft": info["soft"], "data": info["data"],
                "n_metric": len(info["metrics"])}

    row = dict(info["data"], imported_at=db.now())
    rid = row["run_id"]
    with conn:                                   # 单事务：要么整条进，要么全不进
        conn.execute("DELETE FROM run WHERE run_id = ?", (rid,))   # 级联清子表
        conn.execute(
            f"INSERT INTO run ({','.join(RUN_COLS)}) VALUES ({','.join('?' * len(RUN_COLS))})",
            tuple(row[c] for c in RUN_COLS))
        conn.executemany(
            "INSERT INTO metric (run_id, split, name, step, value) VALUES (?,?,?,?,?)",
            [(m["run_id"], m["split"], m["name"], m["step"], m["value"]) for m in info["metrics"]])
        conn.executemany(
            "INSERT INTO artifact (run_id, path, kind, bytes, sha256, present) VALUES (?,?,?,?,?,?)",
            [(a["run_id"], a["path"], a["kind"], a["bytes"], a["sha256"], a["present"])
             for a in info["artifacts"]])
        conn.executemany(
            "INSERT INTO prediction (run_id, material_id, split, y_true, y_pred) VALUES (?,?,?,?,?)",
            [(p["run_id"], p["material_id"], p["split"], p["y_true"], p["y_pred"])
             for p in info["predictions"]])
        conn.executemany(
            "INSERT INTO feature_importance (run_id, feature, gain, weight, cover, rank_gain, rank_weight)"
            " VALUES (?,?,?,?,?,?,?)",
            [(f["run_id"], f["feature"], f["gain"], f["weight"], f["cover"],
              f["rank_gain"], f["rank_weight"]) for f in info["importance"]])
    return {"run_id": rid, "recorded": True, "soft": info["soft"], "data": row,
            "n_metric": len(info["metrics"]), "n_pred": len(info["predictions"]),
            "n_imp": len(info["importance"]), "is_smoke": row["is_smoke"], "is_leak": row["is_leak"]}


def record_all(root: Path = OUTPUT_ROOT, db_path=None, dry_run: bool = False) -> list[dict]:
    ensure_dirs()                       # 目录骨架可能被手工删过：先补回来再扫
    db.init(db_path) if db_path else db.init()
    conn = db.connect(db_path) if db_path else db.connect()
    try:
        out = []
        for d in sorted(p for p in Path(root).iterdir() if p.is_dir()):
            if not (d / "manifest.json").exists():
                continue
            out.append(record_run(conn, d, dry_run=dry_run))
        if not dry_run:
            purge_orphan_figures(conn)
        return out
    finally:
        conn.close()


def purge_orphan_figures(conn) -> int:
    """删掉 figure 表里"文件已经不在了"的记录。

    为什么需要：`figure.run_id` 的外键是 ON DELETE SET NULL，删一个 run 时它的图记录
    不会跟着消失，而是变成悬空指针 —— 展品目录被手工删掉时也一样。这些孤儿会让
    `cli.py verify` 的「图片可访问」报红，所以在每次收录后顺手清一次。
    """
    dead = [(r["fig_id"], r["path"]) for r in
            conn.execute("SELECT fig_id, path FROM figure").fetchall()
            if not (PROJECT_ROOT / r["path"]).exists()]
    if dead:
        with conn:
            for fid, _ in dead:
                conn.execute("DELETE FROM figure WHERE fig_id = ?", (fid,))
        print(f"    清理孤儿图记录 {len(dead)} 条（对应文件已不存在）")
    return len(dead)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    ensure_dirs()                       # OUTPUT_ROOT 不存在时 iterdir 会抛 FileNotFoundError
    db.init()
    conn = db.connect()
    try:
        dirs = [d for d in sorted(OUTPUT_ROOT.iterdir()) if (d / "manifest.json").exists()]
        print(f"发现 {len(dirs)} 个 run 目录\n")
        n_ok = 0
        for d in dirs:
            r = record_run(conn, d, dry_run=check)
            if "data" not in r:                       # 体检没过：列原因
                print(f"[拒绝] {d.name}")
                for why in r["why"]:
                    print(f"      ! {why}")
                continue
            n_ok += 1
            n_m = r.get("n_metric", 0)
            flag = "" if not r.get("soft") else f"  [可容忍 {len(r['soft'])} 条]"
            print(f"[OK] {r['run_id']}  group={r['data']['feature_group']:<3} "
                  f"smoke={r['data']['is_smoke']} leak={r['data']['is_leak']} "
                  f"metrics={n_m}{flag}")
            for s in r.get("soft", []):
                print(f"      - {s}")
        if not check:
            purge_orphan_figures(conn)
        print(f"\n{'体检' if check else '收录'}完成：{n_ok}/{len(dirs)}")
        return 0 if n_ok == len(dirs) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
