"""SQLite 持久化：批次（预览/发布）状态管理。

状态机
------
pending  : 已上传，存在可发布的匿名方案，等待确认发布
published: 已确认发布（含可下载 CSV），重启后内容不变
rejected : 已上传但无可行匿名方案，不能发布
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL CHECK (status IN ('pending','published','rejected')),
    source_filename TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    plan_json       TEXT,
    output_csv      BLOB,
    output_filename TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def insert_pending(conn: sqlite3.Connection, filename: str, row_count: int,
                   plan_json: str) -> int:
    """单事务：使旧 pending 预览失效，再插入新批次（再次上传后旧预览不得发布）。"""
    with conn:
        conn.execute(
            "UPDATE batches SET status = 'rejected' WHERE status = 'pending'")
        cur = conn.execute(
            "INSERT INTO batches (status, source_filename, row_count, plan_json) "
            "VALUES ('pending', ?, ?, ?)",
            (filename, row_count, plan_json),
        )
        return int(cur.lastrowid)


def insert_rejected(conn: sqlite3.Connection, filename: str, row_count: int) -> int:
    with conn:
        conn.execute(
            "UPDATE batches SET status = 'rejected' WHERE status = 'pending'")
        cur = conn.execute(
            "INSERT INTO batches (status, source_filename, row_count) "
            "VALUES ('rejected', ?, ?)",
            (filename, row_count),
        )
        return int(cur.lastrowid)


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def get_latest_pending(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def list_published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM batches WHERE status = 'published' ORDER BY id DESC"))


def publish_batch(conn: sqlite3.Connection, batch_id: int,
                  output_csv: bytes, output_filename: str) -> None:
    """原子发布：仅当前 pending 批次可发布；失败抛异常并整体回滚，不留残件。"""
    with conn:
        row = conn.execute(
            "SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise LookupError("批次不存在")
        if row["status"] != STATUS_PENDING:
            raise RuntimeError("该预览已不可发布（已发布或已被新的上传取代）")
        conn.execute(
            "UPDATE batches SET status = 'published', output_csv = ?, "
            "output_filename = ? WHERE id = ?",
            (output_csv, output_filename, batch_id),
        )
