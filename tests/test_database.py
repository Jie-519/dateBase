# -*- coding: utf-8 -*-
"""库的单元测试（只用标准库 unittest）。

    python 数据库/tests/test_database.py -v
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DB_DIR))

import db as dbmod  # noqa: E402
import verify  # noqa: E402
from models import FORBIDDEN_TOKENS  # noqa: E402
from repo import MaterialRepo  # noqa: E402


class TestDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = MaterialRepo(read_only=True)
        cls.con = dbmod.connect(read_only=True)
        cls.con.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()
        cls.con.close()

    # ---------------------------------------------------------- 结构
    def test_01_counts(self):
        for t in ("material", "structure", "electrical", "stability"):
            n = self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            self.assertEqual(n, 3354, f"{t} 行数不对")

    def test_02_foreign_keys_clean(self):
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_02b_schema_is_strict_and_restrict(self):
        """schema.sql 的结构性保证（P0 回归测试）：STRICT + 主键非空 + 溯源表不许级联删。

        在建库时自检，不依赖 C2DB_materials.sqlite 的内容，所以重建后立刻能发现问题。
        """
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(dbmod.SCHEMA_PATH.read_text(encoding="utf-8"))
            con.execute("PRAGMA foreign_keys = ON")

            # 1) 每张表都是 STRICT（列类型才真的被强制）
            loose = [name for name, is_strict in con.execute(
                "SELECT name, strict FROM pragma_table_list "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'") if not is_strict]
            self.assertEqual(loose, [], f"存在非 STRICT 表：{loose}")

            # 2) 主键列都显式 NOT NULL（SQLite 非 INTEGER 主键历史上允许 NULL）
            nullable_pk = [f"{t}.{r[1]}" for t in dbmod.table_names(con)
                           for r in con.execute(f"PRAGMA table_info('{t}')")
                           if r[5] > 0 and not r[3]]
            self.assertEqual(nullable_pk, [], f"主键列允许 NULL：{nullable_pk}")

            con.execute("INSERT INTO import_batch VALUES (1,'x.xlsx','h',1,'kept',0,'t',NULL)")
            con.execute("INSERT INTO material(material_id,formula,name,name_source,batch_id) "
                        "VALUES ('t-1','X','X','formula_temp',1)")
            con.execute("INSERT INTO stability(material_id) VALUES ('t-1')")

            # 3) 类型强制：文本进 REAL 必须报错
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE stability SET ehull='abc' WHERE material_id='t-1'")

            # 4) 溯源表不许级联删掉事实（P0-2）
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM import_batch WHERE batch_id=1")

            # 5) 删材料仍然级联到子表（这条语义没变）
            con.execute("DELETE FROM material WHERE material_id='t-1'")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM stability").fetchone()[0], 0)
        finally:
            con.close()

    def test_03_every_material_has_three_sublibraries(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM material m "
            "LEFT JOIN structure s ON s.material_id = m.material_id "
            "LEFT JOIN electrical e ON e.material_id = m.material_id "
            "LEFT JOIN stability st ON st.material_id = m.material_id "
            "WHERE s.material_id IS NULL OR e.material_id IS NULL OR st.material_id IS NULL"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    # ---------------------------------------------------------- 对象
    def test_04_get_returns_composed_object(self):
        rec = self.repo.get("2As-1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.material.formula, "As2")
        self.assertEqual(rec.structure.space_group, 164)
        self.assertAlmostEqual(rec.structure.a, 3.608)
        self.assertAlmostEqual(rec.electrical.gap_hse06, 2.019)
        self.assertEqual(rec.electrical.gap_kind_pbe, "间接")
        self.assertIsNone(rec.electrical.mobility_e)          # C2DB 无此字段
        self.assertIsNone(rec.structure.crystal_phase)         # C2DB 无此字段
        self.assertAlmostEqual(rec.electrical.eff_mass_cbm, 0.246, places=6)   # emass_cbm
        self.assertAlmostEqual(rec.electrical.eff_mass_vbm, 0.155, places=6)   # emass_vbm
        self.assertEqual(rec.stability.dyn_stab, "Yes")
        self.assertIn("2As-1", rec.summary())

    def test_05_unknown_id(self):
        self.assertIsNone(self.repo.get("not-exist-1"))

    def test_06_missing_is_null_not_string(self):
        zero_gap = self.con.execute(
            "SELECT COUNT(*) FROM electrical WHERE gap_pbe = 0").fetchone()[0]
        self.assertEqual(zero_gap, 78)
        self.assertIsNone(self.repo.get("2As-1").electrical.gap_gw)

    # ---------------------------------------------------------- 约束
    def test_07_no_forbidden_tokens(self):
        hits = []
        for t in ("material", "structure", "electrical", "stability"):
            cols = [r["name"] for r in self.con.execute(f"PRAGMA table_info({t})")
                    if "TEXT" in (r["type"] or "")]
            for col in cols:
                marks = ",".join("?" * len(FORBIDDEN_TOKENS))
                n = self.con.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE {col} IN ({marks})", FORBIDDEN_TOKENS
                ).fetchone()[0]
                if n:
                    hits.append(f"{t}.{col}={n}")
        self.assertEqual(hits, [])

    def test_08_absent_fields_stay_null(self):
        for col, table in [("crystal_phase", "structure"), ("mobility_e", "electrical"),
                           ("mobility_h", "electrical"), ("dielectric_static", "electrical")]:
            n = self.con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL").fetchone()[0]
            self.assertEqual(n, 0, f"{table}.{col} 不该有值")

    def test_08b_effective_mass_filled(self):
        n = self.con.execute(
            "SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm IS NOT NULL").fetchone()[0]
        self.assertGreater(n, 2500)
        bad = self.con.execute(
            "SELECT COUNT(*) FROM electrical WHERE eff_mass_cbm < 0 OR eff_mass_vbm < 0"
        ).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_09_method_dictionary(self):
        rows = dict(self.con.execute(
            "SELECT property_key, functional FROM property_dictionary").fetchall())
        self.assertEqual(rows["gap_pbe"], "PBE")
        self.assertEqual(rows["gap_hse06"], "HSE06@PBE")
        self.assertEqual(rows["gap_gw"], "G0W0@PBE")

    # ---------------------------------------------------------- 查询
    def test_10_search_and_count(self):
        self.assertEqual(self.repo.count(category="单质"), 11)
        recs = self.repo.search(min_gap=3.0, limit=5)
        self.assertTrue(recs)
        self.assertTrue(all(r.electrical.gap_pbe >= 3.0 for r in recs))
        self.assertEqual(len(self.repo.search(element="Bi", limit=3)), 3)

    def test_11_views(self):
        n_mat = self.con.execute("SELECT COUNT(*) FROM material").fetchone()[0]
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v_material_full").fetchone()[0], n_mat)
        n_prop = self.con.execute("SELECT COUNT(*) FROM property_dictionary").fetchone()[0]
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM v_method_map").fetchone()[0], n_prop)
        n_gap = self.con.execute("SELECT COUNT(*) FROM data_gap").fetchone()[0]
        n_global = self.con.execute(
            "SELECT COUNT(*) FROM property_dictionary WHERE availability = 'no_field'").fetchone()[0]
        n_missing = self.con.execute("SELECT COUNT(*) FROM v_material_missing").fetchone()[0]
        self.assertEqual(n_missing, n_gap + n_global * n_mat)

    def test_12_full_verification(self):
        res = verify.run(write_report=False)
        bad = [c.name for c in res["checks"] if not c.ok]
        self.assertEqual(bad, [])

    # ---------------------------------------------------------- CIF 原文
    def test_13_cif_embedded(self):
        """CIF 原文件存在库内（schema v3）：条数齐、能取出、哈希对得上。"""
        n = self.con.execute(
            "SELECT COUNT(*) FROM structure WHERE cif_content IS NOT NULL").fetchone()[0]
        self.assertEqual(n, 3354)

        raw = self.repo.cif("2As-1")
        self.assertIsNotNone(raw, "2As-1 的 CIF 原文取不到")
        self.assertTrue(raw.lstrip().startswith(b"data_"), "不像 CIF 原文")
        self.assertIn(b"_cell_length_a", raw)

        stored = self.con.execute(
            "SELECT cif_sha256 FROM structure WHERE material_id = '2As-1'").fetchone()[0]
        self.assertEqual(hashlib.sha256(raw).hexdigest(), stored, "库内 CIF 与记录哈希不一致")

        # 文本接口
        self.assertIn("_cell_length_a", self.repo.cif_text("2As-1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
