# -*- coding: utf-8 -*-
"""封装好的读取接口（相当于 C++ 里那个类的成员函数）+ 命令行示例。

    python 数据库/repo.py 2As-1                      # 按 ID 取一个完整对象
    python 数据库/repo.py --stats                    # 全库统计
    python 数据库/repo.py --search --category 二元化合物 --min-gap 2 --limit 8
    python 数据库/repo.py --search --element Bi --stable --limit 8
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db as dbmod  # noqa: E402
from models import MaterialRecord, record_from_join_row  # noqa: E402

ORDER_WHITELIST = {"score", "gap_pbe", "gap_hse06", "gap_gw", "gap_dir_pbe", "id", "space_group"}


class MaterialRepo:
    """一个入口对象：内部持有连接，对外只暴露"取对象 / 查对象 / 统计"三类方法。"""

    def __init__(self, path: str | Path | None = None, *, read_only: bool = True):
        self.con = dbmod.connect(path, read_only=read_only)
        self.con.row_factory = sqlite3.Row

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "MaterialRepo":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 取对象
    def get(self, material_id: str) -> MaterialRecord | None:
        """按 ID 取一个完整对象（三子库 + 辅助库）。"""
        row = self.con.execute("SELECT * FROM v_material_full WHERE id = ?",
                               (material_id,)).fetchone()
        return None if row is None else record_from_join_row(row)

    def exists(self, material_id: str) -> bool:
        return self.con.execute("SELECT 1 FROM material WHERE material_id = ?",
                                (material_id,)).fetchone() is not None

    def all_ids(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT material_id FROM material ORDER BY 1")]

    def cif(self, material_id: str) -> bytes | None:
        """取 CIF 原文件字节（存在 structure.cif_content，内容 = C2DB 官网下载的原文）。

        与 cif_sha256 逐字节对应，可用 hashlib.sha256(raw).hexdigest() 复核。
        """
        row = self.con.execute(
            "SELECT cif_content FROM structure WHERE material_id = ?",
            (material_id,)).fetchone()
        return None if row is None else row[0]

    def cif_text(self, material_id: str) -> str | None:
        """取 CIF 原文（UTF-8 文本形式）。"""
        raw = self.cif(material_id)
        return None if raw is None else raw.decode("utf-8", "replace")

    # ------------------------------------------------------------ 查对象
    def _filter(self, category=None, has_metal=None, element=None, min_gap=None,
                max_gap=None, gap_kind=None, space_group=None, crystal_system=None,
                min_score=None, has_gw=False, stable=None) -> tuple[str, list]:
        where, params = [], []
        if category:
            cats = [category] if isinstance(category, str) else list(category)
            where.append(f"category IN ({','.join('?' * len(cats))})")
            params += cats
        if has_metal is not None:
            where.append("has_metal = ?")
            params.append(1 if has_metal else 0)
        if element:
            where.append("EXISTS (SELECT 1 FROM material_element me JOIN element el "
                         "ON el.z = me.z WHERE me.material_id = v.id AND el.symbol = ?)")
            params.append(element)
        if min_gap is not None:
            where.append("gap_pbe >= ?")
            params.append(min_gap)
        if max_gap is not None:
            where.append("gap_pbe <= ?")
            params.append(max_gap)
        if gap_kind:
            where.append("gap_kind_pbe = ?")
            params.append(gap_kind)
        if space_group is not None:
            where.append("space_group = ?")
            params.append(space_group)
        if crystal_system:
            where.append("crystal_system = ?")
            params.append(crystal_system)
        if min_score is not None:
            where.append("score >= ?")
            params.append(min_score)
        if has_gw:
            where.append("gap_gw IS NOT NULL")
        if stable is not None:
            where.append("dyn_stab = ?" if stable else "(dyn_stab IS NULL OR dyn_stab <> 'Yes')")
            if stable:
                params.append("Yes")
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def search(self, *, limit: int | None = 50, offset: int = 0,
               order_by: str = "score", desc: bool = True, **filters) -> list[MaterialRecord]:
        """按条件查询，返回对象列表（limit=None 表示不限制）。"""
        if order_by not in ORDER_WHITELIST:
            raise ValueError(f"order_by 只能是 {sorted(ORDER_WHITELIST)}")
        where, params = self._filter(**filters)
        sql = (f"SELECT * FROM v_material_full v{where} "
               f"ORDER BY {order_by} {'DESC' if desc else 'ASC'}, id")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [record_from_join_row(r) for r in self.con.execute(sql, params)]

    def count(self, **filters) -> int:
        where, params = self._filter(**filters)
        return self.con.execute(f"SELECT COUNT(*) FROM v_material_full v{where}", params).fetchone()[0]

    # ------------------------------------------------------------ 缺失与统计
    def missing(self, material_id: str) -> list[sqlite3.Row]:
        """某材料缺什么、为什么缺（含"源表无此字段"的全局项）。"""
        return self.con.execute(
            "SELECT property_key, reason, is_global, detail FROM v_material_missing "
            "WHERE material_id = ? ORDER BY is_global, property_key", (material_id,)).fetchall()

    def elements(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT el.symbol, el.z, COUNT(*) AS n_materials "
            "FROM material_element me JOIN element el ON el.z = me.z "
            "GROUP BY el.z ORDER BY n_materials DESC, el.z LIMIT ?", (limit,)).fetchall()

    def stats(self) -> dict:
        q = self.con.execute
        return {
            "n_materials": q("SELECT COUNT(*) FROM material").fetchone()[0],
            "by_category": [dict(r) for r in q(
                "SELECT category, has_metal, n_materials, avg_score, avg_gap_pbe, n_direct "
                "FROM v_category_summary ORDER BY n_materials DESC")],
            "missing": [dict(r) for r in q(
                "SELECT property_key, availability, n_missing, missing_rate "
                "FROM v_missing_counts ORDER BY n_missing DESC")],
            "gap": dict(q(
                "SELECT SUM(gap_pbe = 0) AS pbe_zero, SUM(gap_hse06 IS NOT NULL) AS n_hse, "
                "SUM(gap_gw IS NOT NULL) AS n_gw, SUM(gap_kind_pbe = '直接') AS n_direct "
                "FROM electrical").fetchone()),
            "dyn_stab": dict(q("SELECT dyn_stab, COUNT(*) FROM stability GROUP BY 1").fetchall()),
        }


# ---------------------------------------------------------------- 命令行
def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _width(s))


def _rjust(s: str, width: int) -> str:
    return " " * max(0, width - _width(s)) + s


TABLE_COLS = [("ID", 14, "l"), ("化学式", 14, "l"), ("类别", 12, "l"),
              ("空间群", 7, "r"), ("a(Å)", 8, "r"), ("gap", 7, "r"),
              ("HSE06", 7, "r"), ("GW", 7, "r"), ("稳定性", 9, "l")]


def _table(records: list[MaterialRecord], limit: int = 20) -> str:
    def fmt(v):
        return "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))

    def cell(s: str, width: int, align: str) -> str:
        return _rjust(s, width) if align == "r" else _pad(s, width)

    head = "".join(cell(name, w, "l") for name, w, _ in TABLE_COLS)
    lines = [head, "-" * _width(head)]
    for r in records[:limit]:
        m, s, e, st = r.material, r.structure, r.electrical, r.stability
        cells = [m.material_id, m.formula, m.category or "—", fmt(s.space_group),
                 fmt(s.a), fmt(e.gap_pbe), fmt(e.gap_hse06), fmt(e.gap_gw),
                 " " + fmt(st.dyn_stab)]
        lines.append("".join(cell(v, w, a) for v, (_, w, a) in zip(cells, TABLE_COLS)).rstrip())
    if len(records) > limit:
        lines.append(f"... 共 {len(records)} 条，仅显示前 {limit} 条")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C2DB 材料数据库查询")
    ap.add_argument("uid", nargs="?", help="材料 ID，如 2As-1")
    ap.add_argument("--stats", action="store_true", help="全库统计")
    ap.add_argument("--search", action="store_true", help="按条件查询")
    ap.add_argument("--category")
    ap.add_argument("--element")
    ap.add_argument("--min-gap", type=float)
    ap.add_argument("--max-gap", type=float)
    ap.add_argument("--gap-kind", choices=["直接", "间接"])
    ap.add_argument("--space-group", type=int)
    ap.add_argument("--crystal-system", choices=["三斜", "单斜", "正交", "四方", "三方", "六方", "立方"])
    ap.add_argument("--metal", dest="has_metal", action="store_true", help="只看含金属")
    ap.add_argument("--stable", action="store_true", help="只看动力学稳定")
    ap.add_argument("--has-gw", action="store_true", help="只看有 G0W0 带隙")
    ap.add_argument("--cif", action="store_true", help="打印该材料的 CIF 原文（配合材料 ID）")
    ap.add_argument("--out", help="把 CIF 原文写到指定文件（配合 --cif）")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    with MaterialRepo(args.db) as repo:
        if args.stats:
            print(json.dumps(repo.stats(), ensure_ascii=False, indent=2))
            return 0
        if args.uid and args.cif:
            raw = repo.cif(args.uid)
            if raw is None:
                print(f"没有 {args.uid} 的 CIF 原文")
                return 1
            if args.out:
                Path(args.out).write_bytes(raw)
                print(f"已写出 {args.out}（{len(raw)} 字节，源码 sha256 见 structure.cif_sha256）")
            else:
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()
            return 0
        if args.uid:
            rec = repo.get(args.uid)
            if rec is None:
                print(f"没有 {args.uid}")
                return 1
            print(rec.summary())
            print("\n缺失清单：")
            for r in repo.missing(args.uid):
                tag = "源表无此字段" if r["is_global"] else "缺失"
                print(f"  {_pad(r['property_key'], 24)}{_pad(tag, 14)}"
                      f"{_pad(r['reason'], 16)}{r['detail'] or ''}")
            return 0
        if args.search:
            recs = repo.search(category=args.category, element=args.element,
                               min_gap=args.min_gap, max_gap=args.max_gap,
                               gap_kind=args.gap_kind, space_group=args.space_group,
                               crystal_system=args.crystal_system,
                               has_metal=True if args.has_metal else None,
                               stable=True if args.stable else None,
                               has_gw=args.has_gw, limit=None)
            print(f"命中 {len(recs)} 条")
            print(_table(recs, args.limit))
            return 0
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
