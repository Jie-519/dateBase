# -*- coding: utf-8 -*-
"""把库导出成 Excel（给不装数据库的人看，也方便贴进论文/答辩材料）。

    python 数据库/export.py

一个工作簿 7 页：材料主库 / 结构信息库 / 电学性能子库 / 辅助_稳定性 /
字段字典 / 方法字典 / 缺失统计。库里的 NULL 在这一层才渲染成"缺失"或"源表无此字段"。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

import db as dbmod  # noqa: E402

OUT = HERE / "C2DB_三子库导出_20261031.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=10)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
MISS_FONT = Font(color="9C0006", size=9)          # 缺失标红
NOFIELD_FONT = Font(color="7F6000", size=9)       # 源表无此字段标棕

SHEETS = {
    "材料主库": {
        "sql": ("SELECT material_id AS 'ID', formula AS '化学式', reduced_formula AS '约化化学式', "
                "name AS '名称', name_source AS '名称来源', composition AS '组成', "
                "n_elements AS '元素种类数', category AS '类别', has_metal AS '含金属', "
                "score AS '完整度评分', verdict AS '筛选结论', source_url AS '官网详情页' "
                "FROM material ORDER BY material_id"),
        "widths": [14, 16, 12, 16, 13, 40, 10, 11, 8, 10, 9, 46],
    },
    "结构信息库": {
        "sql": ("SELECT m.material_id AS 'ID', m.formula AS '化学式', s.a AS 'a (Å)', s.b AS 'b (Å)', "
                "s.c AS 'c (Å)', s.alpha AS 'alpha (°)', s.beta AS 'beta (°)', s.gamma AS 'gamma (°)', "
                "s.space_group AS '空间群', s.crystal_system AS '晶系(派生)', "
                "s.crystal_phase AS '晶相', s.cell_volume AS '晶胞体积 (Å³)', "
                "s.cif_url AS 'CIF结构', s.cif_sha256 AS 'CIF哈希', "
                "CASE WHEN s.cif_content IS NULL THEN '否' ELSE '是' END AS 'CIF原文内嵌', "
                "s.cif_bytes AS 'CIF字节数' "
                "FROM material m JOIN structure s ON s.material_id = m.material_id "
                "ORDER BY m.material_id"),
        "widths": [14, 14, 9, 9, 9, 9, 9, 9, 8, 11, 14, 13, 46, 20, 13, 11],
        "nofield": {"晶相"},
    },
    "电学性能子库": {
        "sql": ("SELECT m.material_id AS 'ID', m.formula AS '化学式', "
                "e.gap_pbe AS '带隙_PBE (eV)', e.gap_dir_pbe AS '带隙_dir_PBE (eV)', "
                "e.gap_hse06 AS '带隙_HSE06 (eV)', e.gap_gw AS '带隙_G0W0 (eV)', "
                "e.gap_kind_pbe AS '直接/间接(PBE)', "
                "e.eff_mass_cbm AS '电子有效质量_CBM-DOS (m0)', "
                "e.eff_mass_vbm AS '空穴有效质量_VBM-DOS (m0)', "
                "e.mobility_e AS '电子迁移率', "
                "e.mobility_h AS '空穴迁移率', e.dielectric_static AS '介电常数', "
                "e.polarizability_total AS '极化率_总 (Å)' "
                "FROM material m JOIN electrical e ON e.material_id = m.material_id "
                "ORDER BY m.material_id"),
        "widths": [14, 14, 12, 14, 13, 13, 12, 16, 16, 12, 12, 11, 13],
        "nofield": {"电子迁移率", "空穴迁移率", "介电常数"},
    },
    "辅助_稳定性": {
        "sql": ("SELECT m.material_id AS 'ID', m.formula AS '化学式', st.dyn_stab AS '动力学稳定', "
                "st.ehull AS 'ehull (eV/atom)', st.hform AS 'hform (eV/atom)', st.energy AS '能量 (eV)' "
                "FROM material m JOIN stability st ON st.material_id = m.material_id "
                "ORDER BY m.material_id"),
        "widths": [14, 14, 11, 14, 14, 12],
    },
}


def _write_table(ws, sql: str, widths: list[int], nofield: set[str] | None = None) -> int:
    nofield = nofield or set()
    cur = dbmod.connect().execute(sql)
    headers = [d[0] for d in cur.description]
    nofield_idx = {i for i, h in enumerate(headers) if h in nofield}
    ws.append(headers)
    for c, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
    ws.freeze_panes = "C2"
    n = 0
    for row in cur:
        out = []
        for i, v in enumerate(row):
            if v is None:
                out.append("源表无此字段" if i in nofield_idx else "缺失")
            elif isinstance(v, float) and v != v:
                out.append("n/a")
            elif isinstance(v, float) and (v == float("inf") or v == float("-inf")):
                out.append("∞")
            elif isinstance(v, float):
                out.append(round(v, 3))
            else:
                out.append(v)
        ws.append(out)
        r = ws.max_row
        for i, v in enumerate(out):
            cell = ws.cell(row=r, column=i + 1)
            cell.border = BORDER
            cell.font = Font(size=9)
            if v == "缺失":
                cell.font = MISS_FONT
            elif v == "源表无此字段":
                cell.font = NOFIELD_FONT
            if isinstance(v, float):
                cell.number_format = "0.000"
        n += 1
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"
    return n


def run(out_path: Path | None = None) -> Path:
    out = Path(out_path) if out_path else OUT
    wb = Workbook()
    wb.remove(wb.active)
    counts = {}
    for name, cfg in SHEETS.items():
        ws = wb.create_sheet(name)
        counts[name] = _write_table(ws, cfg["sql"], cfg["widths"], cfg.get("nofield"))

    con = dbmod.connect()
    # 字段字典
    ws = wb.create_sheet("字段字典")
    ws.append(["列", "中文字段", "所属子库", "单位", "来源", "源表列", "缺失口径", "说明"])
    rows = con.execute(
        "SELECT column_key, name_cn, sublibrary, unit, origin, source_column, missing_policy, note "
        "FROM field_dictionary ORDER BY sublibrary, column_key").fetchall()
    for c in range(1, 9):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
    for r in rows:
        ws.append(list(r))
    for c, w in enumerate([26, 22, 14, 10, 14, 24, 22, 60], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"

    # 方法字典
    ws = wb.create_sheet("方法字典")
    ws.append(["性质", "中文名", "表", "列", "单位", "方法", "泛函", "起始泛函",
               "精度", "源表列", "来源", "可用性", "备注"])
    for c in range(1, 14):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
    for r in con.execute(
            "SELECT property_key, name_cn, db_table, db_column, unit, method, functional, "
            "functional_base, precision, source_column, origin, availability, note "
            "FROM v_method_map"):
        ws.append(list(r))
    for c, w in enumerate([20, 22, 11, 20, 12, 10, 14, 10, 7, 22, 12, 10, 60], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"

    # 缺失统计
    ws = wb.create_sheet("缺失统计")
    ws.append(["字段", "中文名", "可用性", "总行数", "缺失行数", "缺失率"])
    for c in range(1, 7):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
    for r in con.execute(
            "SELECT d.property_key, d.name_cn, m.availability, m.n_total, m.n_missing, m.missing_rate "
            "FROM v_missing_counts m JOIN property_dictionary d "
            "ON d.property_key = m.property_key ORDER BY m.n_missing DESC, d.property_key"):
        r = list(r)
        if r[2] == "no_field":
            r[4], r[5] = "源表无此字段", "—"
        else:
            r[5] = f"{r[5] * 100:.2f}%"
        ws.append(r)
    for c, w in enumerate([24, 22, 14, 10, 12, 12], start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"
    con.close()

    wb.save(out)
    print("导出完成:", out)
    for k, v in counts.items():
        print(f"  {k}: {v} 行")
    return out


if __name__ == "__main__":
    run()
