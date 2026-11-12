# -*- coding: utf-8 -*-
"""管理器单元测试：不碰真实台账，全部在临时库里跑。

跑法
----
    cd D:\\数据库\\模型\\管理器
    python -m unittest discover -s tests -v

约定
----
* 临时目录放 模型/XGboost/_tmp/pytest_*（已在 .gitignore 里），用完即删；
* 只读真实 run 目录，拷贝出的小副本随便改，用来验证"改过就该被查出来"。
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 管理器
sys.path.insert(0, str(HERE))

import db          # noqa: E402
import record      # noqa: E402
import report      # noqa: E402
import splitguard  # noqa: E402
import verify      # noqa: E402
from paths import BASE_DATASET_ID, OUTPUT_ROOT, PROJECT_ROOT  # noqa: E402

SCRATCH = PROJECT_ROOT / "模型" / "XGboost" / "_tmp"
MAIN_RUN = "20261111-223846_c2db-gap-c07a0c"
SMOKE_RUN = "20261111-223842_c2db-gap-7efdfc"
LEAK_RUN = "20261111-223919_c2db-gap-46ca73"


def _copy_run(dst_root: Path, run_id: str, with_model: bool = False) -> Path:
    """拷一份 run 目录（默认不带 model.json，省空间也不影响校验）。"""
    src = OUTPUT_ROOT / run_id
    dst = dst_root / run_id
    (dst / "artifacts").mkdir(parents=True)
    (dst / "logs").mkdir()
    shutil.copy2(src / "manifest.json", dst / "manifest.json")
    for name in ("predictions.csv", "feature_importance.csv"):
        shutil.copy2(src / "artifacts" / name, dst / "artifacts" / name)
    if with_model:
        shutil.copy2(src / "artifacts" / "model.json", dst / "artifacts" / "model.json")
    shutil.copy2(src / "logs" / "train.log", dst / "logs" / "train.log")
    return dst


class ManagerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注意：不要用 tempfile —— 它建的目录带 0o700 ACL，沙箱里再往里 mkdir 会
        # WinError 5。这里自己建普通目录，跑完自己删。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        cls.tmp = SCRATCH / "ut" / stamp
        cls.runs = cls.tmp / "runs"
        cls.runs.mkdir(parents=True)
        cls.catalog = cls.tmp / "catalog.sqlite"
        for rid in (SMOKE_RUN, MAIN_RUN, LEAK_RUN):
            _copy_run(cls.runs, rid)
        db.init(cls.catalog)
        record.record_all(cls.runs, db_path=cls.catalog)   # 各测试共用这一份脏数据
        cls.counts = cls._counts()

    @classmethod
    def _counts(cls) -> dict:
        conn = db.connect(cls.catalog, readonly=True)
        try:
            return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                    for t in ("run", "metric", "artifact", "prediction", "feature_importance")}
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------ 结构
    def test_schema_version_and_tables(self):
        conn = db.connect(self.catalog, readonly=True)
        try:
            self.assertEqual(db.user_version(conn), db.CATALOG_SCHEMA_VERSION)
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            for t in ("run", "metric", "artifact", "prediction", "feature_importance", "figure"):
                self.assertIn(t, names)
            for v in ("v_run_score", "v_leaderboard", "v_leak_report"):
                self.assertIn(v, names)
            # STRICT 表：写错类型必须被拒
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='metric'").fetchone()[0]
            self.assertIn("STRICT", sql)
        finally:
            conn.close()

    def test_strict_rejects_wrong_type(self):
        conn = db.connect(self.catalog)
        try:
            with self.assertRaises(Exception):
                conn.execute("INSERT INTO run(run_id) VALUES (1)")   # run_id 必须 TEXT
        finally:
            conn.rollback()
            conn.close()

    # ------------------------------------------------------ 收录
    def test_record_all_and_idempotent(self):
        # 收录两次：行数必须一模一样（先删后插，不重复）
        first = record.record_all(self.runs, db_path=self.catalog)
        self.assertEqual(len(first), 3)
        after_first = self._counts()
        record.record_all(self.runs, db_path=self.catalog)
        after_second = self._counts()
        self.assertEqual(after_first, after_second)
        self.assertEqual(after_second["run"], 3)
        self.assertEqual(after_second["prediction"], 3 * 3354)
        self.assertEqual(self.counts, after_second)

    def test_tags_smoke_and_leak(self):
        conn = db.connect(self.catalog, readonly=True)
        try:
            got = {r["run_id"]: (r["is_smoke"], r["is_leak"]) for r in
                   conn.execute("SELECT run_id, is_smoke, is_leak FROM run")}
        finally:
            conn.close()
        self.assertEqual(got[SMOKE_RUN], (1, 0))
        self.assertEqual(got[MAIN_RUN], (0, 0))
        self.assertEqual(got[LEAK_RUN], (0, 1))

    def test_leaderboard_excludes_smoke_and_leak(self):
        conn = db.connect(self.catalog, readonly=True)
        try:
            board = db.rows(conn, "SELECT run_id, feature_group FROM v_leaderboard")
            score = db.rows(conn, "SELECT run_id FROM v_run_score")
        finally:
            conn.close()
        self.assertEqual([b["run_id"] for b in board], [MAIN_RUN])
        self.assertEqual(len(score), 3)
        self.assertEqual(board[0]["feature_group"], "G2")

    def test_final_metric_uses_minus_one_not_null(self):
        conn = db.connect(self.catalog, readonly=True)
        try:
            n_null = conn.execute("SELECT count(*) FROM metric WHERE step IS NULL").fetchone()[0]
            n_final = conn.execute(
                "SELECT count(*) FROM metric WHERE step = -1").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_null, 0)
        self.assertGreaterEqual(n_final, 3 * 9)      # 3 个 run × 3 集 × 3 指标

    # ------------------------------------------------------ 异常检测
    def test_tampered_predictions_is_rejected(self):
        """改一个预测值 → 产物哈希对不上 → 必须拒绝入库。"""
        tmp2 = self.tmp / "tamper"
        tmp2.mkdir()
        dst = _copy_run(tmp2, MAIN_RUN)
        p = dst / "artifacts" / "predictions.csv"
        lines = p.read_text(encoding="utf-8").splitlines()
        cells = lines[1].split(",")
        cells[3] = "9.999999"
        lines[1] = ",".join(cells)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        info = record.inspect(dst)
        self.assertFalse(info["ok"])
        self.assertTrue(any("哈希不一致" in p for p in info["hard"]), info["hard"])

    def test_manifest_mismatch_detected_by_verify(self):
        """入库后 manifest 被改 → verify 必须报出来。"""
        cat2 = self.tmp / "catalog2.sqlite"
        runs2 = self.tmp / "runs2"
        runs2.mkdir()
        _copy_run(runs2, MAIN_RUN)
        db.init(cat2)
        record.record_all(runs2, db_path=cat2)
        res = verify.check(cat2)
        self.assertTrue(res["ok"], res)
        mf = runs2 / MAIN_RUN / "manifest.json"
        doc = mf.read_text(encoding="utf-8").replace('"n_estimators": 2000',
                                                    '"n_estimators": 2001')
        mf.write_text(doc, encoding="utf-8")
        res = verify.check(cat2)
        self.assertFalse(res["ok"])
        self.assertIn("manifest 未被改动",
                      [c["check"] for c in res["checks"] if not c["ok"]])

    # ------------------------------------------------------ 报告
    def test_report_html_has_no_placeholder_leftovers(self):
        conn = db.connect(self.catalog, readonly=True)
        try:
            r = db.one(conn, "SELECT * FROM v_run_score WHERE run_id = ?", (MAIN_RUN,))
            html = report.run_report_html(conn, r)
        finally:
            conn.close()
        self.assertIn(MAIN_RUN, html)
        self.assertIn("测试集 R²", html)
        self.assertNotIn("None", html)
        self.assertIn("<!DOCTYPE html>", html)

    def test_index_html_mentions_every_run(self):
        conn = db.connect(self.catalog, readonly=True)
        try:
            html = report.index_html(conn)
        finally:
            conn.close()
        for rid in (SMOKE_RUN, MAIN_RUN, LEAK_RUN):
            self.assertIn(rid, html)
        self.assertIn("catalog.sqlite", html)


class SplitGenCase(unittest.TestCase):
    """按条数划分：条数精确、三集互斥、可用 subset、拒绝非法输入。"""

    @classmethod
    def setUpClass(cls):
        import splitgen
        cls.sg = splitgen
        cls.header, cls.rows = splitgen.load_base()

    def test_exact_counts_full_dataset(self):
        p = self.sg.plan(self.rows, {"train": 2600, "valid": 354, "test": 400}, 42)
        self.assertTrue(p["exact"], p["counts"])
        self.assertEqual(p["counts"], {"train": 2600, "valid": 354, "test": 400})
        self.assertEqual(p["n_unused"], 0)
        self.assertEqual(p["n_kept"], 3354)

    def test_subset_with_unused_pool(self):
        """三集之和小于总数：其余进 unused，且与三集互斥。"""
        p = self.sg.plan(self.rows, {"train": 1500, "valid": 200, "test": 200}, 42)
        self.assertTrue(p["exact"])
        self.assertEqual(p["counts"], {"train": 1500, "valid": 200, "test": 200})
        self.assertEqual(p["n_kept"], 1900)
        self.assertEqual(p["n_unused"], 1454)
        buckets = {}
        for mid, s in p["assign"].items():
            buckets.setdefault(s, set()).add(mid)
        self.assertEqual(len(buckets["unused"]), 1454)
        for a in ("train", "valid", "test"):
            self.assertEqual(buckets[a] & buckets["unused"], set(), f"{a} 与 unused 交叠")

    def test_hard_rule_test_never_in_train(self):
        """硬规则：无论条数怎么给，测试集材料都不得出现在训练集/验证集。"""
        for counts in ({"train": 2349, "valid": 505, "test": 500},
                       {"train": 1000, "valid": 100, "test": 100},
                       {"train": 2900, "valid": 200, "test": 254}):
            p = self.sg.plan(self.rows, counts, 7)
            buckets = {}
            for mid, s in p["assign"].items():
                buckets.setdefault(s, set()).add(mid)
            self.assertEqual(buckets["train"] & buckets["test"], set(), counts)
            self.assertEqual(buckets["valid"] & buckets["test"], set(), counts)
            self.assertEqual(buckets["train"] & buckets["valid"], set(), counts)

    def test_same_formula_not_split(self):
        p = self.sg.plan(self.rows, {"train": 1500, "valid": 200, "test": 200}, 42)
        by_formula = {}
        for r in self.rows:
            by_formula.setdefault(r["formula"], set()).add(p["assign"][r["material_id"]])
        bad = [f for f, ss in by_formula.items() if len(ss) > 1]
        self.assertEqual(bad, [])

    def test_deterministic(self):
        a = self.sg.plan(self.rows, {"train": 1500, "valid": 200, "test": 200}, 42)
        b = self.sg.plan(self.rows, {"train": 1500, "valid": 200, "test": 200}, 42)
        self.assertEqual(a["assign"], b["assign"])
        c = self.sg.plan(self.rows, {"train": 1500, "valid": 200, "test": 200}, 7)
        self.assertNotEqual(a["assign"], c["assign"])

    def test_reject_over_total(self):
        with self.assertRaises(SystemExit):
            self.sg.plan(self.rows, {"train": 3000, "valid": 300, "test": 300}, 42)

    def test_reject_below_min(self):
        with self.assertRaises(SystemExit):
            self.sg.plan(self.rows, {"train": 800, "valid": 200, "test": 200}, 42)


class GuardCase(unittest.TestCase):
    """防止"检查器本身是瞎的"：喂一份故意污染的划分，必须报警。"""

    def setUp(self):
        self.dir = SCRATCH / "ut" / "guard"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _csv(self, rows: list[tuple[str, str, str]]) -> Path:
        p = self.dir / "dirty.csv"
        with p.open("w", encoding="utf-8", newline="") as fh:
            fh.write("material_id,formula,split\n")
            for r in rows:
                fh.write(",".join(r) + "\n")
        return p

    def tearDown(self):
        splitguard.audit(refresh=True)          # 清掉被污染的缓存，恢复真实快照

    def test_clean_split_passes(self):
        p = self._csv([("m1", "A", "train"), ("m2", "B", "valid"), ("m3", "C", "test")])
        a = splitguard.audit(snapshot_csv=p, refresh=True)
        self.assertTrue(a["ok"], a["problems"])
        self.assertEqual(a["counts"], {"train": 1, "valid": 1, "test": 1})

    def test_same_material_in_train_and_test_is_caught(self):
        p = self._csv([("m1", "A", "train"), ("m1", "A", "test"), ("m3", "C", "test")])
        a = splitguard.audit(snapshot_csv=p, refresh=True)
        self.assertFalse(a["ok"])
        self.assertEqual(a["overlaps"]["train-test"]["ids"], 1)
        self.assertTrue(any("重复 material_id" in x for x in a["problems"]))

    def test_same_formula_split_across_sets_is_caught(self):
        """不同材料但同一化学式，被拆到训练/测试两集 —— 同族泄漏，必须抓。"""
        p = self._csv([("m1", "Fe2O3", "train"), ("m2", "Fe2O3", "test")])
        a = splitguard.audit(snapshot_csv=p, refresh=True)
        self.assertFalse(a["ok"])
        self.assertEqual(a["overlaps"]["train-test"]["formulas"], 1)
        self.assertTrue(any("化学式交叠" in x for x in a["problems"]))


class RegressionCase(unittest.TestCase):
    """对真实台账的只读回归：数字必须和训练日志里的一致。"""

    def setUp(self):
        if not db.CATALOG_DB.exists():
            self.skipTest("真实台账还没建：python cli.py record")
        self.conn = db.connect(readonly=True)

    def tearDown(self):
        self.conn.close()

    def test_real_catalog_passes_self_check(self):
        res = verify.check()
        self.assertTrue(res["ok"], [c for c in res["checks"] if not c["ok"]])

    def test_main_model_numbers(self):
        # 必须限定在母集：台账里还有按条数生成的划分，那些数字不可与母集混比
        # （不同划分的 R² 本来就不是一个尺度，拿来比会得到毫无意义的"第一名"）。
        r = db.one(self.conn,
                   "SELECT s.* FROM v_run_score s JOIN run r ON r.run_id = s.run_id"
                   " WHERE r.dataset_id = ? AND s.is_smoke = 0 AND s.is_leak = 0"
                   " ORDER BY s.valid_r2 DESC LIMIT 1", (BASE_DATASET_ID,))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["valid_r2"], 0.8198823070497583, places=9)
        self.assertAlmostEqual(r["test_r2"], 0.8148494729885833, places=9)
        self.assertEqual(r["feature_group"], "G2")

    def test_test_set_is_clean(self):
        """硬规则：测试集材料不得出现在训练集/验证集。真实快照上必须零交叠。"""
        a = splitguard.audit(refresh=True)
        self.assertTrue(a["ok"], a["problems"])
        self.assertEqual(a["counts"], {"train": 2349, "valid": 505, "test": 500})
        self.assertEqual(a["dup_ids"], 0)
        for pair, ov in a["overlaps"].items():
            self.assertEqual(ov["ids"], 0, f"{pair} 材料交叠")
            self.assertEqual(ov["formulas"], 0, f"{pair} 化学式交叠")
        self.assertTrue(a["counts_match_split_json"])
        self.assertTrue(a["snapshot_sha256_match"])

    def test_leak_gap_is_big(self):
        """答案泄漏必须显著抬高 R²，否则这套对照白跑了（同一份划分内比才有意义）。"""
        rows = {x["feature_group"]: x["test_r2"] for x in db.rows(
            self.conn,
            "SELECT s.feature_group, s.test_r2 FROM v_run_score s"
            " JOIN run r ON r.run_id = s.run_id"
            " WHERE r.dataset_id = ? AND s.is_smoke = 0"
            "   AND s.feature_group IN ('G2', 'G3p')", (BASE_DATASET_ID,))}
        self.assertIn("G2", rows)
        self.assertIn("G3p", rows)
        self.assertGreater(rows["G3p"] - rows["G2"], 0.15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
