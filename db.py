# -*- coding: utf-8 -*-
"""SQLite 连接与建库工具（零第三方依赖，只用标准库 sqlite3）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
DB_PATH = PKG_DIR / "C2DB_materials.sqlite"
SCHEMA_PATH = PKG_DIR / "schema.sql"


def connect(path: str | Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """打开数据库：开启外键约束、返回 Row 工厂（可按列名取值）。"""
    p = Path(path) if path else DB_PATH
    if read_only:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    con.commit()


def query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return con.execute(sql, params).fetchall()


def scalar(con: sqlite3.Connection, sql: str, params: tuple = ()):
    row = con.execute(sql, params).fetchone()
    return None if row is None else row[0]


def table_names(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def view_names(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]


def counts(con: sqlite3.Connection) -> dict[str, int]:
    return {t: scalar(con, f"SELECT COUNT(*) FROM {t}") for t in table_names(con)}
