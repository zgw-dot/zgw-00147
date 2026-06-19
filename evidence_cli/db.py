"""SQLite 数据库模块：批次、证据项、复核历史、撤销栈"""

import sqlite3
import os
import time
from contextlib import contextmanager
from typing import List, Dict, Optional, Tuple


DB_FILENAME = "evidence_cli.db"


def get_db_path(work_dir: Optional[str] = None) -> str:
    """获取数据库文件路径"""
    base = work_dir or os.getcwd()
    return os.path.join(base, DB_FILENAME)


@contextmanager
def get_conn(db_path: str):
    """获取数据库连接上下文管理器"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """初始化数据库表结构"""
    with get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT UNIQUE NOT NULL,
                manifest_path TEXT NOT NULL,
                evidence_dir TEXT NOT NULL,
                description TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                expected_size INTEGER,
                expected_sha256 TEXT,
                manifest_line_no INTEGER NOT NULL,
                actual_size INTEGER,
                actual_sha256 TEXT,
                precheck_status TEXT DEFAULT 'unchecked',
                review_status TEXT DEFAULT 'pending',
                review_remark TEXT,
                reviewed_at REAL,
                FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                UNIQUE(batch_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS review_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                prev_status TEXT NOT NULL,
                prev_remark TEXT,
                new_status TEXT NOT NULL,
                new_remark TEXT,
                action TEXT NOT NULL,
                operator TEXT,
                undone INTEGER DEFAULT 0,
                undo_of_id INTEGER,
                created_at REAL NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
                FOREIGN KEY (item_id) REFERENCES evidence_items(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_items_batch ON evidence_items(batch_id);
            CREATE INDEX IF NOT EXISTS idx_logs_batch ON review_logs(batch_id);
            CREATE INDEX IF NOT EXISTS idx_logs_item ON review_logs(item_id);
        """)


def create_batch(
    db_path: str,
    batch_no: str,
    manifest_path: str,
    evidence_dir: str,
    description: Optional[str] = None,
) -> int:
    """创建批次，返回批次 ID"""
    now = time.time()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO batches (batch_no, manifest_path, evidence_dir, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_no, manifest_path, evidence_dir, description, now, now),
        )
        return cursor.lastrowid


def get_batch_by_no(db_path: str, batch_no: str) -> Optional[Dict]:
    """根据批次号获取批次信息"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?",
            (batch_no,),
        ).fetchone()
        return dict(row) if row else None


def get_batch_by_id(db_path: str, batch_id: int) -> Optional[Dict]:
    """根据 ID 获取批次信息"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        return dict(row) if row else None


def list_batches(db_path: str) -> List[Dict]:
    """列出所有批次"""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def batch_exists(db_path: str, batch_no: str) -> bool:
    """检查批次是否已存在"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM batches WHERE batch_no = ?",
            (batch_no,),
        ).fetchone()
        return row is not None


def insert_evidence_items(
    db_path: str,
    batch_id: int,
    items: List[Dict],
) -> int:
    """批量插入证据项，返回插入数量"""
    now = time.time()
    with get_conn(db_path) as conn:
        count = 0
        for item in items:
            conn.execute(
                """INSERT INTO evidence_items
                   (batch_id, file_path, expected_size, expected_sha256,
                    manifest_line_no, precheck_status, review_status)
                   VALUES (?, ?, ?, ?, ?, 'unchecked', 'pending')""",
                (
                    batch_id,
                    item["file_path"],
                    item.get("expected_size"),
                    item.get("expected_sha256"),
                    item["manifest_line_no"],
                ),
            )
            count += 1
        conn.execute(
            "UPDATE batches SET updated_at = ? WHERE id = ?",
            (now, batch_id),
        )
        return count


def get_evidence_items(db_path: str, batch_id: int) -> List[Dict]:
    """获取批次的所有证据项"""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_items WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_evidence_item_by_id(db_path: str, item_id: int) -> Optional[Dict]:
    """根据 ID 获取证据项"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM evidence_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def update_precheck_result(
    db_path: str,
    item_id: int,
    actual_size: Optional[int],
    actual_sha256: Optional[str],
    precheck_status: str,
) -> None:
    """更新预检结果"""
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE evidence_items
               SET actual_size = ?, actual_sha256 = ?, precheck_status = ?
               WHERE id = ?""",
            (actual_size, actual_sha256, precheck_status, item_id),
        )


def review_item(
    db_path: str,
    batch_id: int,
    item_id: int,
    new_status: str,
    remark: Optional[str] = None,
    operator: Optional[str] = None,
    action: str = "review",
) -> int:
    """复核证据项，记录历史，返回日志 ID"""
    now = time.time()
    with get_conn(db_path) as conn:
        item = conn.execute(
            "SELECT review_status, review_remark FROM evidence_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not item:
            raise ValueError(f"证据项不存在: {item_id}")

        prev_status = item["review_status"]
        prev_remark = item["review_remark"]

        log_cursor = conn.execute(
            """INSERT INTO review_logs
               (batch_id, item_id, prev_status, prev_remark, new_status, new_remark,
                action, operator, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, item_id, prev_status, prev_remark, new_status, remark,
             action, operator, now),
        )
        log_id = log_cursor.lastrowid

        conn.execute(
            """UPDATE evidence_items
               SET review_status = ?, review_remark = ?, reviewed_at = ?
               WHERE id = ?""",
            (new_status, remark, now, item_id),
        )

        conn.execute(
            "UPDATE batches SET updated_at = ? WHERE id = ?",
            (now, batch_id),
        )
        return log_id


def get_last_review_log(db_path: str, batch_id: int) -> Optional[Dict]:
    """获取批次最新的未被撤销的复核日志"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM review_logs
               WHERE batch_id = ? AND action != 'undo' AND undone = 0
               ORDER BY id DESC LIMIT 1""",
            (batch_id,),
        ).fetchone()
        return dict(row) if row else None


def undo_last_review(
    db_path: str,
    batch_id: int,
    operator: Optional[str] = None,
) -> Optional[Dict]:
    """撤销上一条复核，恢复之前的状态和备注。返回被撤销的日志详情，无可撤销时返回 None"""
    now = time.time()
    with get_conn(db_path) as conn:
        last_log = conn.execute(
            """SELECT * FROM review_logs
               WHERE batch_id = ? AND action != 'undo' AND undone = 0
               ORDER BY id DESC LIMIT 1""",
            (batch_id,),
        ).fetchone()

        if not last_log:
            return None

        item = conn.execute(
            "SELECT id, manifest_line_no, file_path FROM evidence_items WHERE id = ?",
            (last_log["item_id"],),
        ).fetchone()
        if not item:
            return None

        conn.execute(
            "UPDATE review_logs SET undone = 1 WHERE id = ?",
            (last_log["id"],),
        )

        conn.execute(
            """UPDATE evidence_items
               SET review_status = ?, review_remark = ?, reviewed_at = ?
               WHERE id = ?""",
            (last_log["prev_status"], last_log["prev_remark"], now, last_log["item_id"]),
        )

        undo_log_cursor = conn.execute(
            """INSERT INTO review_logs
               (batch_id, item_id, prev_status, prev_remark, new_status, new_remark,
                action, operator, undo_of_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'undo', ?, ?, ?)""",
            (
                batch_id,
                last_log["item_id"],
                last_log["new_status"],
                last_log["new_remark"],
                last_log["prev_status"],
                last_log["prev_remark"],
                operator,
                last_log["id"],
                now,
            ),
        )

        conn.execute(
            "UPDATE batches SET updated_at = ? WHERE id = ?",
            (now, batch_id),
        )

        result = dict(last_log)
        result["manifest_line_no"] = item["manifest_line_no"]
        result["file_path"] = item["file_path"]
        result["undo_log_id"] = undo_log_cursor.lastrowid
        return result


def count_reviewed(db_path: str, batch_id: int) -> Tuple[int, int, int, int]:
    """统计复核状态：(总数, 已签收, 待补件, 待处理)"""
    with get_conn(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]

        signed = conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ? AND review_status = 'signed'",
            (batch_id,),
        ).fetchone()[0]

        supplement = conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ? AND review_status = 'supplement'",
            (batch_id,),
        ).fetchone()[0]

        pending = total - signed - supplement
        return total, signed, supplement, pending


def count_precheck(db_path: str, batch_id: int) -> Tuple[int, int, int, int]:
    """统计预检状态：(总数, 通过, 失败, 未检查)"""
    with get_conn(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]

        passed = conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ? AND precheck_status = 'passed'",
            (batch_id,),
        ).fetchone()[0]

        failed = conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ? AND precheck_status = 'failed'",
            (batch_id,),
        ).fetchone()[0]

        unchecked = total - passed - failed
        return total, passed, failed, unchecked


def get_review_history(db_path: str, batch_id: int, limit: int = 50) -> List[Dict]:
    """获取复核历史记录"""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT rl.*, ei.file_path
               FROM review_logs rl
               JOIN evidence_items ei ON rl.item_id = ei.id
               WHERE rl.batch_id = ?
               ORDER BY rl.id DESC LIMIT ?""",
            (batch_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
