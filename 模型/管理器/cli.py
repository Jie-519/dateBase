# -*- coding: utf-8 -*-
"""cli.py：管理器的唯一入口。日常只记这几条命令。

    python cli.py all        # 一条命令从零重建：建库→收录→出图→出报告→自检
    python cli.py record     # 新训练完了，收进台账（幂等，重复跑没事）
    python cli.py chart      # 重画所有图（图不入 git，随时可重画）
    python cli.py report     # 重出 HTML（单 run 报告 + 首页）
    python cli.py top        # 看能选型的排行榜
    python cli.py list       # 看全部训练（含冒烟、泄漏对照）
    python cli.py show 3db   # 看某一次训练的细节（支持 run_id 前缀）
    python cli.py verify     # 台账自检：哈希、外键、产物、图
    python cli.py split      # 数据分集体检：三集条数 + 测试集是否零交叠
    python cli.py open       # 用浏览器打开首页
    python cli.py sql "select ..."   # 临时查询（只读连接）

设计约束
--------
本文件只做"调度 + 打印"，不写业务逻辑：建库在 db.py，收录在 record.py，
出图在 charts.py，报告在 report.py，自检在 verify.py。
改任何一条链路的实现，都不该改动这个文件里的命令。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import charts
import db
import record
import report
import splitguard
import verify
from paths import CATALOG_DB, FIGURE_ROOT, INDEX_HTML, REPORT_DIR

USAGE = __doc__.split("设计约束")[0].strip()


# ---------------------------------------------------------------- 打印
def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _row(cells: list, widths: list[int], right: set[int] | None = None) -> str:
    right = right or set()
    out = []
    for i, c in enumerate(cells):
        s = "" if c is None else str(c)
        pad = " " * max(0, widths[i] - _w(s))
        out.append(pad + s if i in right else s + pad)
    return "  ".join(out).rstrip()


def _table(headers: list[str], rows: list[list], right: set[int] | None = None) -> None:
    right = right or set()
    widths = [max(_w(str(h)), *(_w(str(r[i])) for r in rows)) if rows else _w(str(h))
              for i, h in enumerate(headers)]
    line = _row(headers, widths)
    print(line)
    print("-" * _w(line))
    for r in rows:
        print(_row(r, widths, right))


def _f(v, n: int = 4) -> str:
    return "—" if v is None else (f"{v:.{n}f}" if isinstance(v, float) else str(v))


# ---------------------------------------------------------------- 命令
def cmd_init(argv: list[str]) -> int:
    p = db.init(reinit="--reinit" in argv)
    print(f"台账就绪：{p}")
    print(f"结构版本 {db.CATALOG_SCHEMA_VERSION}，大小 {p.stat().st_size:,} B")
    return 0


def cmd_record(argv: list[str]) -> int:
    return record.main(["--check"] if "--check" in argv else [])


def cmd_list(argv: list[str]) -> int:
    conn = db.connect(readonly=True)
    try:
        rs = db.rows(conn, "SELECT * FROM v_run_score ORDER BY created_at")
        if not rs:
            print("台账里还没有训练记录：python cli.py record")
            return 1
        rows = []
        for r in rs:
            tag = "冒烟" if r["is_smoke"] else ("泄漏对照" if r["is_leak"] else "可选型")
            rows.append([r["run_id"], r["feature_group"], r["n_features"], _f(r["valid_r2"]),
                         _f(r["test_r2"]), _f(r["test_rmse"], 3), r["best_iteration"], tag])
        _table(["run_id", "组", "列数", "valid R²", "test R²", "test RMSE", "最好树", "标签"],
               rows, {2, 3, 4, 5, 6})
    finally:
        conn.close()
    return 0


def cmd_top(argv: list[str]) -> int:
    conn = db.connect(readonly=True)
    try:
        show_all = "--all" in argv
        sql = "SELECT * FROM v_run_score" if show_all else "SELECT * FROM v_leaderboard"
        rs = db.rows(conn, sql + " ORDER BY valid_r2 DESC")
        if not rs:
            print("没有可选型的 run（全是冒烟或泄漏对照）：python cli.py list --all")
            return 1
        rows = [[i + 1, r["feature_group"], r["run_id"].split("-")[-1], r["n_features"],
                 _f(r["valid_r2"]), _f(r["test_r2"]), _f(r["test_rmse"], 3),
                 "冒烟" if r["is_smoke"] else ("泄漏" if r["is_leak"] else "")]
                for i, r in enumerate(rs)]
        print("按验证集 R² 排序（测试集不参与选型）\n" if not show_all
              else "全部训练（含冒烟与泄漏对照）\n")
        _table(["#", "组", "run", "列数", "valid R²", "test R²", "test RMSE", "标签"],
               rows, {0, 3, 4, 5, 6})
        best = rs[0]
        print(f"\n主模型 = {best['feature_group']}（{best['run_id']}）"
              f"  test R² {_f(best['test_r2'])}  RMSE {_f(best['test_rmse'], 3)} eV")
    finally:
        conn.close()
    return 0


def cmd_show(argv: list[str]) -> int:
    if not argv:
        print("用法：python cli.py show <run_id 前缀>")
        return 2
    pref = argv[0]
    conn = db.connect(readonly=True)
    try:
        hit = db.rows(conn, "SELECT * FROM v_run_score WHERE run_id LIKE ?", (f"%{pref}%",))
        if not hit:
            print(f"没有匹配 {pref!r} 的 run")
            return 1
        if len(hit) > 1:
            print(f"匹配到 {len(hit)} 个，请给更长的前缀：")
            for h in hit:
                print("  " + h["run_id"])
            return 2
        r = hit[0]
        row = db.one(conn, "SELECT * FROM run WHERE run_id = ?", (r["run_id"],))
        print(f"run_id      {r['run_id']}")
        print(f"特征组      {r['feature_group']}（{r['n_features']} 列）")
        print(f"备注        {r['note']}")
        print(f"标签        冒烟={r['is_smoke']} 泄漏={r['is_leak']}")
        print(f"时间        {r['created_at']} → {row['finished_at']}（{row['duration_s']} s）")
        print(f"数据        {row['dataset_id']}，{row['snapshot_rows']} 行，"
              f"seed={row['split_seed']}，{row['split_counts_json']}")
        print(f"快照 sha256 {row['dataset_sha256']}")
        print(f"特征 sha256 {row['features_sha256']}")
        print(f"manifest    {row['manifest_sha256']}（{row['manifest_bytes']:,} B）")
        print("\n指标（最终值）")
        ms = db.rows(conn, "SELECT split, name, value FROM metric WHERE run_id = ? AND step = -1 "
                           "ORDER BY split, name", (r["run_id"],))
        _table(["数据集", "指标", "值"], [[m["split"], m["name"], f"{m['value']:.6f}"] for m in ms], {2})
        print("\n产物")
        arts = db.rows(conn, "SELECT kind, path, bytes, sha256, present FROM artifact "
                             "WHERE run_id = ? ORDER BY kind", (r["run_id"],))
        _table(["类型", "文件", "字节", "sha256", "在盘"],
               [[a["kind"], a["path"], a["bytes"], a["sha256"][:16] + "…",
                 "是" if a["present"] else "否"] for a in arts], {2})
        figs = db.rows(conn, "SELECT fig_id, title FROM figure WHERE run_id = ? ORDER BY fig_id",
                       (r["run_id"],))
        if figs:
            print("\n图")
            for f in figs:
                print(f"  {f['fig_id']:<34} {f['title']}")
        print(f"\n报告        {REPORT_DIR / (r['run_id'] + '.html')}")
    finally:
        conn.close()
    return 0


def cmd_chart(argv: list[str]) -> int:
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    charts.build_all(only=only)
    return 0


def cmd_report(argv: list[str]) -> int:
    report.build_all(index_only="--index-only" in argv)
    return 0


def cmd_verify(argv: list[str]) -> int:
    return verify.main(argv)


def cmd_sql(argv: list[str]) -> int:
    if not argv:
        print('用法：python cli.py sql "select * from v_leaderboard"')
        return 2
    conn = db.connect(readonly=True)
    try:
        rs = db.rows(conn, " ".join(argv))
        if not rs:
            print("（0 行）")
            return 0
        heads = list(rs[0].keys())
        body = [[("" if r[h] is None else r[h]) for h in heads] for r in rs]
        _table(heads, body)
        print(f"\n{len(rs)} 行（只读连接）")
    except sqlite3.Error as e:
        print(f"SQL 出错：{e}")
        return 1
    finally:
        conn.close()
    return 0


def cmd_split(argv: list[str]) -> int:
    """数据分集体检：三集条数 + 两两交叠（测试集干净性的独立入口）。"""
    return splitguard.main(argv)


def cmd_open(argv: list[str]) -> int:
    if not INDEX_HTML.exists():
        print("首页还没生成：python cli.py report")
        return 1
    os.startfile(str(INDEX_HTML))          # noqa: S606  仅 Windows，用默认程序打开
    print(f"已打开 {INDEX_HTML}")
    return 0


def cmd_all(argv: list[str]) -> int:
    print("① 建库/校验结构")
    db.init(reinit="--reinit" in argv)
    print("\n② 收录训练产物")
    rc = record.main([])
    print("\n③ 出图")
    charts.build_all()
    print("\n④ 出报告")
    report.build_all()
    print("\n⑤ 台账自检")
    rc2 = verify.main([])
    print(f"\n全部完成。首页：{INDEX_HTML}")
    print(f"图目录：{FIGURE_ROOT}")
    return 0 if (rc == 0 and rc2 == 0) else 1


COMMANDS = {
    "init": cmd_init, "record": cmd_record, "list": cmd_list, "top": cmd_top,
    "show": cmd_show, "chart": cmd_chart, "report": cmd_report, "verify": cmd_verify,
    "split": cmd_split, "sql": cmd_sql, "open": cmd_open, "all": cmd_all,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"未知命令 {cmd!r}\n")
        print(USAGE)
        return 2
    return COMMANDS[cmd](rest)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
