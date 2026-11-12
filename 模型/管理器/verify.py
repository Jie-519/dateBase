# -*- coding: utf-8 -*-
"""verify.py：台账自检 —— 回答"这个库还能信吗"。

查什么
------
1. 结构版本对不对（schema.sql 改过却没重建，会在这里翻车）；
2. 外键有没有孤儿行（ON DELETE CASCADE 生效的前提）；
3. 每个 run 的 manifest.json 还是不是入库时那一份（sha256 比对）；
4. 产物文件在不在盘上、哈希对不对（model.json 被 gitignore 清掉不算错，会注明）；
5. 每个 run 的预测条数、三个集的最终指标是否齐全；
6. 图在不在、sha256 对不对、配方是不是合法 JSON；
7. 排行榜非空。

为什么不在 runlog.verify 里做
------------------------------
runlog 管的是"一次训练的自证"，只认自己那个目录；
本模块管的是"整本台账的横向自洽"，需要跨表比对，属索引层职责。
所以 runlog.py 一行没动。

用法
----
    python verify.py          # 人看的报告
    python verify.py --json   # 给机器
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import splitguard
from paths import CATALOG_DB, DATASET_ID, PROJECT_ROOT, SNAPSHOT_DIR


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(db_path=CATALOG_DB) -> dict:
    db.init()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "hint": hint})

    if not Path(db_path).exists():
        add("台账存在", False, f"{db_path} 不存在", "先跑 python cli.py record")
        return {"ok": False, "checks": checks}

    conn = db.connect(db_path, readonly=True)
    try:
        # 1. 版本
        v = int(conn.execute("PRAGMA user_version").fetchone()[0])
        add("结构版本", v == db.CATALOG_SCHEMA_VERSION,
            f"user_version={v}，期望 {db.CATALOG_SCHEMA_VERSION}",
            "python cli.py init --reinit")

        # 2. 外键
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        add("外键完整性", not fk, f"{len(fk)} 处违规" if fk else "无孤儿行")

        # 3. manifest 是否还是入库时那一份
        bad_mf = []
        for r in conn.execute("SELECT run_id, run_dir, manifest_sha256 FROM run"):
            mf = PROJECT_ROOT / r["run_dir"] / "manifest.json"
            if not mf.exists():
                bad_mf.append(f"{r['run_id']}: manifest.json 不见了")
            elif _sha256(mf) != r["manifest_sha256"]:
                bad_mf.append(f"{r['run_id']}: manifest.json 与入库时不一致")
        add("manifest 未被改动", not bad_mf,
            "；".join(bad_mf), "manifest 变了说明台账已过期：python cli.py record")

        # 4. 产物哈希
        #    present=1 表示入库时文件在盘上，现在必须在且哈希一致；
        #    present=0 表示入库时就没有（例如 model.json 被 gitignore 清理过），
        #    这一条不算错，只在明细里注明——否则清一次缓存就再也过不了自检。
        missing, mismatch, absent = [], [], []
        for a in conn.execute("SELECT run_id, path, sha256, present FROM artifact"):
            run_dir = conn.execute("SELECT run_dir FROM run WHERE run_id = ?",
                                   (a["run_id"],)).fetchone()[0]
            p = PROJECT_ROOT / run_dir / a["path"]
            if not p.exists():
                (missing if a["present"] else absent).append(f"{a['run_id']}/{a['path']}")
            elif _sha256(p) != a["sha256"]:
                mismatch.append(f"{a['run_id']}/{a['path']}")
        add("产物文件齐全", not missing,
            f"{len(missing)} 个入库时存在、现在不见了: " + "；".join(missing[:3]) if missing
            else (f"全部在盘（另有 {len(absent)} 个入库时即不在盘）" if absent else "全部在盘"),
            "文件被删/被清理，需重新 record")
        add("产物哈希一致", not mismatch, "；".join(mismatch[:3]),
            "哈希变了 = 训练产物被改过，需重新 record")

        # 5. 预测与指标
        bad_pred, bad_metric = [], []
        for r in conn.execute("SELECT run_id, snapshot_rows, n_features FROM run"):
            n = conn.execute("SELECT count(*) FROM prediction WHERE run_id = ?",
                             (r["run_id"],)).fetchone()[0]
            if n != r["snapshot_rows"]:
                bad_pred.append(f"{r['run_id']}: {n} != {r['snapshot_rows']}")
            got = {(x["split"], x["name"]) for x in conn.execute(
                "SELECT split, name FROM metric WHERE run_id = ? AND step = -1", (r["run_id"],))}
            lack = sorted({(s, m) for s in ("train", "valid", "test") for m in ("r2", "rmse")} - got)
            if lack:
                bad_metric.append(f"{r['run_id']}: 缺 {lack}")
        add("预测条数", not bad_pred, "；".join(bad_pred), "python cli.py record")
        add("最终指标齐全", not bad_metric, "；".join(bad_metric), "python cli.py record")

        # 6. 图
        bad_fig, bad_recipe = [], []
        for f in conn.execute("SELECT fig_id, path, sha256, recipe_json FROM figure"):
            p = PROJECT_ROOT / f["path"]
            if not p.exists():
                bad_fig.append(f"{f['fig_id']} 文件不见了")
            elif _sha256(p) != f["sha256"]:
                bad_fig.append(f"{f['fig_id']} 哈希不一致")
            try:
                json.loads(f["recipe_json"])
            except Exception:
                bad_recipe.append(f["fig_id"])
        add("图片可访问", not bad_fig, "；".join(bad_fig[:3]), "python cli.py chart")
        add("图片配方合法", not bad_recipe, "；".join(bad_recipe[:3]))

        # 7. 榜单
        n_board = conn.execute("SELECT count(*) FROM v_leaderboard").fetchone()[0]
        n_run = conn.execute("SELECT count(*) FROM run").fetchone()[0]
        add("排行榜非空", n_board >= 1, f"{n_board} 行可选型 / 共 {n_run} 次训练",
            "全是冒烟或泄漏对照？补一次正常训练")

        # 8. 测试集干净性 —— 硬规则，不可豁免
        #    "测试集材料严禁出现在训练集"这条，靠这里每次自检重算，不靠人记。
        #    台账里可能有多份划分（母集 + 按条数生成的），逐份都查，漏一份都不行。
        dsids = [x[0] for x in conn.execute(
            "SELECT DISTINCT dataset_id FROM run ORDER BY dataset_id")]
        if DATASET_ID not in dsids:
            dsids.insert(0, DATASET_ID)
        bad_clean, clean_detail = [], []
        for ds in dsids:
            sg = splitguard.audit(dataset_id=ds)
            c = sg["counts"]
            if not sg["ok"]:
                bad_clean.append(f"{ds}: " + "；".join(sg["problems"]))
                continue
            ov = sg["overlaps"]
            clean_detail.append(f"{ds} 训练{c['train']}/验证{c['valid']}/测试{c['test']}，"
                                f"交叠 0")
        add("测试集干净", not bad_clean,
            "；".join(bad_clean[:2]) if bad_clean else
            f"{len(dsids)} 份划分全部零交叠：" + "；".join(clean_detail[:2]) +
            ("…" if len(clean_detail) > 2 else ""),
            "测试集与训练/验证集有交叠：立即停用这份划分，重跑 splitgen/snapshot")

        # 9. 每个 run 实际使用的 split 是否就是它自己那份划分
        bad_split = []
        maps: dict[str, dict] = {}
        for r in conn.execute("SELECT run_id, dataset_id FROM run"):
            ds = r["dataset_id"]
            if ds not in maps:
                p = SNAPSHOT_DIR / f"{ds}.csv"
                maps[ds] = {}
                if p.exists():
                    with p.open("r", encoding="utf-8-sig", newline="") as fh:
                        for row in csv.DictReader(fh):
                            maps[ds][row["material_id"]] = row["split"]
            snap = maps[ds]
            if not snap:
                bad_split.append(f"{r['run_id']}: 找不到划分文件 {ds}.csv")
                continue
            rows = conn.execute("SELECT material_id, split FROM prediction WHERE run_id = ?",
                                (r["run_id"],)).fetchall()
            diff = sum(1 for m, s in ((x[0], x[1]) for x in rows) if snap.get(m) != s)
            if diff:
                bad_split.append(f"{r['run_id']}: {diff} 条 split 与 {ds} 不一致")
        add("各 run 用对划分", not bad_split, "；".join(bad_split[:3]),
            "训练时喂的 split 列被改过？重新 record")
    finally:
        conn.close()

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def _w(s: str) -> int:
    """显示宽度：中日韩全角字符算 2 列，用来对齐终端表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _w(s))


def main(argv: list[str]) -> int:
    res = check()
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    print("台账自检\n" + "=" * 62)
    for c in res["checks"]:
        mark = "通过" if c["ok"] else "不通过"
        print(f"[{mark}] {_pad(c['check'], 20)} {c['detail']}")
        if not c["ok"] and c["hint"]:
            print(f"           → {c['hint']}")
    print("=" * 62)
    n_bad = sum(1 for c in res["checks"] if not c["ok"])
    print(f"结论：{'全部通过' if res['ok'] else f'{n_bad} 项不通过'}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
