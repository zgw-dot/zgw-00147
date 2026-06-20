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
                updated_at REAL NOT NULL,
                restored_from TEXT,
                restored_at REAL,
                restore_diff TEXT,
                last_restore_event_id INTEGER
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

            CREATE TABLE IF NOT EXISTS restore_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                batch_no TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                snapshot_created_at REAL,
                parent_restore_event_id INTEGER,
                restored_at REAL NOT NULL,
                was_force INTEGER DEFAULT 0,
                was_remapped INTEGER DEFAULT 0,
                evidence_dir_before TEXT,
                evidence_dir_after TEXT NOT NULL,
                manifest_path_before TEXT,
                manifest_path_after TEXT NOT NULL,
                old_batch_snapshot TEXT,
                restore_diff TEXT,
                operator TEXT,
                FOREIGN KEY (batch_id) REFERENCES batches(id)
            );

            CREATE INDEX IF NOT EXISTS idx_items_batch ON evidence_items(batch_id);
            CREATE INDEX IF NOT EXISTS idx_logs_batch ON review_logs(batch_id);
            CREATE INDEX IF NOT EXISTS idx_logs_item ON review_logs(item_id);
            CREATE INDEX IF NOT EXISTS idx_restore_batch ON restore_events(batch_id);
            CREATE INDEX IF NOT EXISTS idx_restore_parent ON restore_events(parent_restore_event_id);

            CREATE TABLE IF NOT EXISTS playbook_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_no TEXT NOT NULL,
                operator TEXT,
                playbook_data TEXT NOT NULL,
                fingerprint_before TEXT,
                status TEXT NOT NULL DEFAULT 'executing',
                error_message TEXT,
                started_at REAL NOT NULL,
                finished_at REAL,
                FOREIGN KEY (batch_no) REFERENCES batches(batch_no)
            );

            CREATE TABLE IF NOT EXISTS playbook_step_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                step_order INTEGER NOT NULL,
                step_type TEXT NOT NULL,
                status TEXT NOT NULL,
                affected_items TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY (run_id) REFERENCES playbook_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS playbook_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                batch_no TEXT NOT NULL,
                description TEXT,
                operator TEXT,
                output_file TEXT,
                playbook_data TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '1.0',
                created_at REAL NOT NULL,
                modified_at REAL NOT NULL,
                last_run_id INTEGER,
                last_run_status TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_playbook_library_batch ON playbook_library(batch_no);

            CREATE INDEX IF NOT EXISTS idx_playbook_runs_batch ON playbook_runs(batch_no);
            CREATE INDEX IF NOT EXISTS idx_playbook_steps_run ON playbook_step_logs(run_id);

            CREATE TABLE IF NOT EXISTS handoff_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_path TEXT NOT NULL,
                package_version TEXT NOT NULL,
                batch_no TEXT NOT NULL,
                source_summary TEXT NOT NULL,
                import_log TEXT NOT NULL,
                restore_result TEXT NOT NULL,
                status TEXT NOT NULL,
                operator TEXT,
                imported_at REAL NOT NULL,
                was_force INTEGER DEFAULT 0,
                evidence_dir_remapped TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_handoff_batch ON handoff_imports(batch_no);
            CREATE INDEX IF NOT EXISTS idx_handoff_status ON handoff_imports(status);
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


def replace_batch(
    db_path: str,
    batch_no: str,
    manifest_path: str,
    evidence_dir: str,
    items: List[Dict],
    description: Optional[str] = None,
) -> Tuple[int, int]:
    """
    原子替换批次：在同一事务中删除旧批次并创建新批次。

    如果旧批次不存在，则直接创建新批次。
    返回 (新批次ID, 证据项数量)。
    事务内任何异常都会回滚，旧数据保持不变。
    """
    now = time.time()
    with get_conn(db_path) as conn:
        old = conn.execute(
            "SELECT id FROM batches WHERE batch_no = ?",
            (batch_no,),
        ).fetchone()
        if old:
            conn.execute("DELETE FROM batches WHERE id = ?", (old["id"],))

        cursor = conn.execute(
            """INSERT INTO batches
               (batch_no, manifest_path, evidence_dir, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_no, manifest_path, evidence_dir, description, now, now),
        )
        new_batch_id = cursor.lastrowid

        count = 0
        for item in items:
            conn.execute(
                """INSERT INTO evidence_items
                   (batch_id, file_path, expected_size, expected_sha256,
                    manifest_line_no, precheck_status, review_status)
                   VALUES (?, ?, ?, ?, ?, 'unchecked', 'pending')""",
                (
                    new_batch_id,
                    item["file_path"],
                    item.get("expected_size"),
                    item.get("expected_sha256"),
                    item["manifest_line_no"],
                ),
            )
            count += 1

        return new_batch_id, count


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


def _insert_restore_event_with_conn(
    conn,
    batch_id: int,
    batch_no: str,
    snapshot_path: str,
    snapshot_created_at: Optional[float],
    parent_restore_event_id: Optional[int],
    restored_at: float,
    was_force: bool,
    was_remapped: bool,
    evidence_dir_before: Optional[str],
    evidence_dir_after: str,
    manifest_path_before: Optional[str],
    manifest_path_after: str,
    old_batch_snapshot: Optional[str],
    restore_diff: Optional[str],
    operator: Optional[str],
) -> int:
    """
    在现有数据库连接中插入恢复事件（供事务内调用）。
    返回新恢复事件 ID。
    """
    cursor = conn.execute(
        """INSERT INTO restore_events
           (batch_id, batch_no, snapshot_path, snapshot_created_at,
            parent_restore_event_id, restored_at, was_force, was_remapped,
            evidence_dir_before, evidence_dir_after,
            manifest_path_before, manifest_path_after,
            old_batch_snapshot, restore_diff, operator)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            batch_no,
            snapshot_path,
            snapshot_created_at,
            parent_restore_event_id,
            restored_at,
            1 if was_force else 0,
            1 if was_remapped else 0,
            evidence_dir_before,
            evidence_dir_after,
            manifest_path_before,
            manifest_path_after,
            old_batch_snapshot,
            restore_diff,
            operator,
        ),
    )
    event_id = cursor.lastrowid
    conn.execute(
        "UPDATE batches SET last_restore_event_id = ? WHERE id = ?",
        (event_id, batch_id),
    )
    return event_id


def insert_restore_event(
    db_path: str,
    batch_id: int,
    batch_no: str,
    snapshot_path: str,
    snapshot_created_at: Optional[float],
    parent_restore_event_id: Optional[int],
    restored_at: float,
    was_force: bool,
    was_remapped: bool,
    evidence_dir_before: Optional[str],
    evidence_dir_after: str,
    manifest_path_before: Optional[str],
    manifest_path_after: str,
    old_batch_snapshot: Optional[Dict] = None,
    restore_diff: Optional[Dict] = None,
    operator: Optional[str] = None,
) -> int:
    """
    独立事务插入一条恢复事件，返回事件 ID。
    old_batch_snapshot 和 restore_diff 若传入 Dict 会自动序列化为 JSON。
    """
    import json

    old_json = json.dumps(old_batch_snapshot, ensure_ascii=False) if old_batch_snapshot else None
    diff_json = json.dumps(restore_diff, ensure_ascii=False) if restore_diff else None

    with get_conn(db_path) as conn:
        return _insert_restore_event_with_conn(
            conn,
            batch_id=batch_id,
            batch_no=batch_no,
            snapshot_path=snapshot_path,
            snapshot_created_at=snapshot_created_at,
            parent_restore_event_id=parent_restore_event_id,
            restored_at=restored_at,
            was_force=was_force,
            was_remapped=was_remapped,
            evidence_dir_before=evidence_dir_before,
            evidence_dir_after=evidence_dir_after,
            manifest_path_before=manifest_path_before,
            manifest_path_after=manifest_path_after,
            old_batch_snapshot=old_json,
            restore_diff=diff_json,
            operator=operator,
        )


def get_restore_events_for_batch(
    db_path: str, batch_id: Optional[int] = None, batch_no: Optional[str] = None
) -> List[Dict]:
    """
    获取批次的全部恢复事件，按时间从早到晚排序。
    优先按 batch_no 查询（不受 force 覆盖时 batch_id 变化影响），
    batch_no 未提供时回退到 batch_id。
    """
    if batch_no is None and batch_id is None:
        raise ValueError("必须提供 batch_id 或 batch_no")

    with get_conn(db_path) as conn:
        if batch_no is not None:
            rows = conn.execute(
                "SELECT * FROM restore_events WHERE batch_no = ? ORDER BY id ASC",
                (batch_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM restore_events WHERE batch_id = ? ORDER BY id ASC",
                (batch_id,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["was_force"] = bool(d.get("was_force", 0))
            d["was_remapped"] = bool(d.get("was_remapped", 0))
            result.append(d)
        return result


def get_last_restore_event(
    db_path: str, batch_id: Optional[int] = None, batch_no: Optional[str] = None
) -> Optional[Dict]:
    """
    获取批次最近一次恢复事件。
    优先按 batch_no 查询。
    """
    if batch_no is None and batch_id is None:
        raise ValueError("必须提供 batch_id 或 batch_no")

    with get_conn(db_path) as conn:
        if batch_no is not None:
            row = conn.execute(
                "SELECT * FROM restore_events WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
                (batch_no,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM restore_events WHERE batch_id = ? ORDER BY id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["was_force"] = bool(d.get("was_force", 0))
        d["was_remapped"] = bool(d.get("was_remapped", 0))
        return d


def get_review_logs_after_time(
    db_path: str, batch_id: int, after_ts: float
) -> List[Dict]:
    """获取指定时间戳之后的所有复核/撤销记录（含 file_path）"""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT rl.*, ei.file_path
               FROM review_logs rl
               JOIN evidence_items ei ON rl.item_id = ei.id
               WHERE rl.batch_id = ? AND rl.created_at > ?
               ORDER BY rl.id ASC""",
            (batch_id, after_ts),
        ).fetchall()
        return [dict(r) for r in rows]


def create_playbook_run(
    db_path: str,
    batch_no: str,
    operator: Optional[str] = None,
    playbook_data: Optional[Dict] = None,
    fingerprint_before: Optional[str] = None,
) -> int:
    import json as _json
    now = time.time()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO playbook_runs
               (batch_no, operator, playbook_data, fingerprint_before, status, started_at)
               VALUES (?, ?, ?, ?, 'executing', ?)""",
            (
                batch_no,
                operator,
                _json.dumps(playbook_data, ensure_ascii=False) if playbook_data else None,
                fingerprint_before,
                now,
            ),
        )
        return cursor.lastrowid


def update_playbook_run_status(
    db_path: str,
    run_id: int,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    now = time.time()
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE playbook_runs SET status = ?, error_message = ?, finished_at = ?
               WHERE id = ?""",
            (status, error_message, now, run_id),
        )


def log_playbook_step(
    db_path: str,
    run_id: int,
    step_order: int,
    step_type: str,
    status: str,
    affected_items: Optional[List[Dict]] = None,
    error_message: Optional[str] = None,
) -> int:
    import json as _json
    now = time.time()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO playbook_step_logs
               (run_id, step_order, step_type, status, affected_items, error_message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                step_order,
                step_type,
                status,
                _json.dumps(affected_items, ensure_ascii=False) if affected_items else None,
                error_message,
                now,
            ),
        )
        return cursor.lastrowid


def get_last_playbook_run(
    db_path: str, batch_no: str,
) -> Optional[Dict]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_runs WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
            (batch_no,),
        ).fetchone()
        return dict(row) if row else None


def get_playbook_runs(
    db_path: str, batch_no: str, limit: int = 20,
) -> List[Dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM playbook_runs WHERE batch_no = ? ORDER BY id DESC LIMIT ?",
            (batch_no, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_playbook_run_with_steps(
    db_path: str, run_id: int,
) -> Optional[Dict]:
    import json as _json
    with get_conn(db_path) as conn:
        run_row = conn.execute(
            "SELECT * FROM playbook_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if not run_row:
            return None
        run_data = dict(run_row)

        step_rows = conn.execute(
            "SELECT * FROM playbook_step_logs WHERE run_id = ? ORDER BY step_order ASC",
            (run_id,),
        ).fetchall()
        steps = []
        for s in step_rows:
            sd = dict(s)
            if sd.get("affected_items"):
                try:
                    sd["affected_items"] = _json.loads(sd["affected_items"])
                except (_json.JSONDecodeError, TypeError):
                    pass
            steps.append(sd)

        run_data["steps"] = steps
        return run_data


def playbook_name_exists(db_path: str, name: str) -> bool:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM playbook_library WHERE name = ?",
            (name,),
        ).fetchone()
        return row is not None


def save_playbook_to_library(
    db_path: str,
    name: str,
    batch_no: str,
    playbook_data: Dict,
    description: Optional[str] = None,
    operator: Optional[str] = None,
    output_file: Optional[str] = None,
    version: str = "1.0",
    overwrite: bool = False,
) -> int:
    import json as _json
    now = time.time()
    data_json = _json.dumps(playbook_data, ensure_ascii=False)
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM playbook_library WHERE name = ?",
            (name,),
        ).fetchone()
        if existing:
            if not overwrite:
                raise ValueError(f"同名剧本 '{name}' 已存在（使用 --overwrite 覆盖）")
            conn.execute(
                """UPDATE playbook_library
                   SET batch_no=?, description=?, operator=?, output_file=?,
                       playbook_data=?, version=?, modified_at=?,
                       last_run_id=NULL, last_run_status=NULL
                   WHERE name=?""",
                (batch_no, description, operator, output_file,
                 data_json, version, now, name),
            )
            return existing["id"]
        cursor = conn.execute(
            """INSERT INTO playbook_library
               (name, batch_no, description, operator, output_file,
                playbook_data, version, created_at, modified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, batch_no, description, operator, output_file,
             data_json, version, now, now),
        )
        return cursor.lastrowid


def get_playbook_from_library(db_path: str, name: str) -> Optional[Dict]:
    import json as _json
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM playbook_library WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["playbook_data"] = _json.loads(d["playbook_data"])
        except (_json.JSONDecodeError, TypeError):
            pass
        return d


def list_playbook_library(
    db_path: str, batch_no: Optional[str] = None,
) -> List[Dict]:
    with get_conn(db_path) as conn:
        if batch_no:
            rows = conn.execute(
                "SELECT * FROM playbook_library WHERE batch_no = ? ORDER BY modified_at DESC",
                (batch_no,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM playbook_library ORDER BY modified_at DESC",
            ).fetchall()
        return [dict(r) for r in rows]


def update_playbook_library_last_run(
    db_path: str, name: str, run_id: int, status: str,
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE playbook_library SET last_run_id=?, last_run_status=?, modified_at=?
               WHERE name=?""",
            (run_id, status, time.time(), name),
        )


def delete_playbook_from_library(db_path: str, name: str) -> bool:
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM playbook_library WHERE name = ?",
            (name,),
        )
        return cursor.rowcount > 0


def insert_handoff_import(
    db_path: str,
    package_path: str,
    package_version: str,
    batch_no: str,
    source_summary: Dict,
    import_log: List[Dict],
    restore_result: Dict,
    status: str,
    operator: Optional[str] = None,
    was_force: bool = False,
    evidence_dir_remapped: Optional[str] = None,
) -> int:
    import json as _json
    now = time.time()
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO handoff_imports
               (package_path, package_version, batch_no, source_summary, import_log,
                restore_result, status, operator, imported_at, was_force, evidence_dir_remapped)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                package_path,
                package_version,
                batch_no,
                _json.dumps(source_summary, ensure_ascii=False),
                _json.dumps(import_log, ensure_ascii=False),
                _json.dumps(restore_result, ensure_ascii=False),
                status,
                operator,
                now,
                1 if was_force else 0,
                evidence_dir_remapped,
            ),
        )
        return cursor.lastrowid


def get_handoff_imports(
    db_path: str, batch_no: Optional[str] = None, limit: int = 50
) -> List[Dict]:
    import json as _json
    with get_conn(db_path) as conn:
        if batch_no:
            rows = conn.execute(
                "SELECT * FROM handoff_imports WHERE batch_no = ? ORDER BY id DESC LIMIT ?",
                (batch_no, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM handoff_imports ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["was_force"] = bool(d.get("was_force", 0))
            try:
                d["source_summary"] = _json.loads(d["source_summary"]) if d.get("source_summary") else {}
            except (_json.JSONDecodeError, TypeError):
                d["source_summary"] = {}
            try:
                d["import_log"] = _json.loads(d["import_log"]) if d.get("import_log") else []
            except (_json.JSONDecodeError, TypeError):
                d["import_log"] = []
            try:
                d["restore_result"] = _json.loads(d["restore_result"]) if d.get("restore_result") else {}
            except (_json.JSONDecodeError, TypeError):
                d["restore_result"] = {}
            result.append(d)
        return result
