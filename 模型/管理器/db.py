# -*- coding: utf-8 -*-
"""db.py：catalog.sqlite 的唯一入口（建库 / 连接 / 小工具）。

为什么单独一层
--------------
* 连接参数只在这里设一次：外键必须 ON（否则 ON DELETE CASCADE 静默失效）；
* 只读连接用 `mode=ro`，从根上防止"查的时候顺手写坏"；
* 建库脚本只有 schema.sql 一份，改结构就改它 + 抬 CATALOG_SCHEMA_VERSION。

用法
----
    from db import connect, init
    init()                       # 幂等，已存在就什么都不做
    with connect() as c:
        for r in c.execute("select * from v_leaderboard"):
            ...

命令行
------
    python db.py                 # 打印库概况
    python db.py --reinit        # 删库重建（只删索引，不动训练产物）
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from paths import CATALOG_DB, MANAGER_DIR, PROJECT_ROOT

#: 结构版本；改 schema.sql 必须同步 +1
CATALOG_SCHEMA_VERSION = 1

SCHEMA_FILE = MANAGER_DIR / "schema.sql"


# ------------------------------------------------------------------ 连接
def connect(db_path: Path | str = CATALOG_DB, readonly: bool = False) -> sqlite3.Connection:
    """开连接。只读模式用 URI，写不进去就报错，别靠自觉。"""
    p = Path(db_path)
    if readonly:
        if not p.exists():
            raise FileNotFoundError(f"台账不存在：{p}（先跑 cli.py record）")
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def check_version(conn: sqlite3.Connection) -> None:
    v = user_version(conn)
    if v != CATALOG_SCHEMA_VERSION:
        raise RuntimeError(
            f"台账结构版本 {v} != 期望 {CATALOG_SCHEMA_VERSION}；"
            f"跑 `python cli.py init --reinit` 重建（只删索引，不删训练产物）")


# ------------------------------------------------------------------ 建库
def init(db_path: Path | str = CATALOG_DB, reinit: bool = False) -> Path:
    """按 schema.sql 建库。已存在且版本对得上就跳过。"""
    p = Path(db_path)
    if reinit:
        for suffix in ("", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
    created = not p.exists()
    conn = connect(p)
    try:
        if created or user_version(conn) == 0:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("catalog_schema_version", str(CATALOG_SCHEMA_VERSION)))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("project_root", str(PROJECT_ROOT)))
            conn.commit()
    finally:
        conn.close()
    world = connect(p)
    try:
        check_version(world)
    finally:
        world.close()
    return p


# ------------------------------------------------------------------ 小工具
def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    r = conn.execute(sql, tuple(params)).fetchone()
    return r[0] if r and r[0] is not None else default


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"):
        out[name] = int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
    return out


# ------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    reinit = "--reinit" in argv
    p = init(reinit=reinit)
    conn = connect(p)
    try:
        print(f"台账: {p}")
        print(f"       {p.stat().st_size:,} B | user_version={user_version(conn)}")
        print("表/视图:")
        for (name, typ) in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"):
            n = "" if typ != "table" else f"  {table_counts(conn).get(name, 0):>6} 行"
            print(f"  {typ:<6} {name}{n}")
        print("\nv_run_score:")
        for r in rows(conn, "SELECT run_id, feature_group, round(valid_r2,4) v, round(test_r2,4) t, is_smoke, is_leak "
                            "FROM v_run_score ORDER BY created_at"):
            print(f"  {r['run_id']}  {r['feature_group']:<3} valid={r['v']} test={r['t']}"
                  f"  smoke={r['is_smoke']} leak={r['is_leak']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
