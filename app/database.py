"""SQLite 持久化：批次（预览/发布）状态管理。

状态机
------
pending  : 已上传，存在可发布的匿名方案，等待确认发布
published: 已确认发布（含可下载 CSV），重启后内容不变
rejected : 已上传但无可行匿名方案，不能发布
review   : 待核查——启动核查发现已发布文件的实际字节异常（病种含换行或
           无法解析），持久标记并停止下载；原始字节留存供内部核对
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
STATUS_REVIEW = "review"

_COLUMNS = """
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status           TEXT NOT NULL CHECK (status IN
                     ('pending','published','rejected','review')),
    source_filename  TEXT NOT NULL,
    row_count        INTEGER NOT NULL,
    plan_json        TEXT,
    output_csv       BLOB,
    output_filename  TEXT,
    review_reason    TEXT,
    review_marked_at TEXT
"""

_SCHEMA = f"CREATE TABLE IF NOT EXISTS batches ({_COLUMNS});"

# 旧库迁移：CHECK 约束不含 'review' 时整表重建（SQLite 无法修改 CHECK）。
# 复制全部既有列，新列取默认值 NULL；原字节原样保留。
_MIGRATION = f"""
BEGIN IMMEDIATE;
ALTER TABLE batches RENAME TO batches_old;
CREATE TABLE batches ({_COLUMNS});
INSERT INTO batches (id, created_at, status, source_filename, row_count,
                     plan_json, output_csv, output_filename)
SELECT id, created_at, status, source_filename, row_count,
       plan_json, output_csv, output_filename FROM batches_old;
DROP TABLE batches_old;
COMMIT;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='batches'"
        ).fetchone()
        if row is None:
            conn.executescript(_SCHEMA)
        elif "'review'" not in (row["sql"] or ""):
            conn.executescript(_MIGRATION)


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


def list_review(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM batches WHERE status = 'review' ORDER BY id DESC"))


def mark_review(conn: sqlite3.Connection, batch_id: int, reason: str) -> bool:
    """将已发布批次持久标记为“待核查”并停止下载；原始字节留存供内部核对。

    仅允许 published -> review 单向迁移；返回本次是否发生了标记。
    """
    with conn:
        cur = conn.execute(
            "UPDATE batches SET status = ?, review_reason = ?, "
            "review_marked_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (STATUS_REVIEW, reason, batch_id, STATUS_PUBLISHED),
        )
        return cur.rowcount > 0


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
