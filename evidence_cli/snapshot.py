"""批次状态快照：保存和恢复批次的完整状态"""

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from . import db

SNAPSHOT_VERSION = "1.0"
SNAPSHOT_DIR = ".snapshots"


class SnapshotError(Exception):
    """快照操作错误基类"""
    pass


class SnapshotNotFoundError(SnapshotError):
    """快照文件不存在"""
    pass


class SnapshotFormatError(SnapshotError):
    """快照格式错误（坏 JSON）"""
    pass


class SnapshotVersionError(SnapshotError):
    """快照版本不兼容"""
    pass


class SnapshotConflictError(SnapshotError):
    """批次已存在冲突"""
    pass


class SnapshotMissingFilesError(SnapshotError):
    """快照引用的文件缺失"""
    pass


def get_snapshot_dir(work_dir: str) -> str:
    """获取快照目录路径"""
    return os.path.join(work_dir, SNAPSHOT_DIR)


def get_snapshot_path(work_dir: str, snapshot_name: str) -> str:
    """获取快照文件路径"""
    if not snapshot_name.endswith(".json"):
        snapshot_name += ".json"
    return os.path.join(get_snapshot_dir(work_dir), snapshot_name)


def save_snapshot(
    db_path: str,
    batch_no: str,
    output_path: str,
) -> Dict:
    """
    保存批次快照到 JSON 文件。

    快照包含：批次元信息、证据项状态、预检结果、复核备注、撤销历史。

    返回快照数据字典。
    """
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        raise SnapshotNotFoundError(f"批次 '{batch_no}' 不存在")

    items = db.get_evidence_items(db_path, batch["id"])
    review_logs = db.get_review_history(db_path, batch["id"], limit=10000)

    snapshot = {
        "version": SNAPSHOT_VERSION,
        "snapshot_created_at": time.time(),
        "batch": {
            "batch_no": batch["batch_no"],
            "manifest_path": batch["manifest_path"],
            "evidence_dir": batch["evidence_dir"],
            "description": batch.get("description"),
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
        },
        "items": items,
        "review_logs": review_logs,
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    return snapshot


def load_snapshot(snapshot_path: str) -> Dict:
    """
    加载并验证快照文件。

    检查：文件存在、JSON 格式、版本兼容性。
    返回快照数据字典。
    """
    if not os.path.isfile(snapshot_path):
        raise SnapshotNotFoundError(f"快照文件不存在: {snapshot_path}")

    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except json.JSONDecodeError as e:
        raise SnapshotFormatError(f"快照 JSON 格式错误: {e}") from e

    if not isinstance(snapshot, dict):
        raise SnapshotFormatError("快照格式错误：根节点不是对象")

    version = snapshot.get("version")
    if not version:
        raise SnapshotFormatError("快照缺少 version 字段")

    if version != SNAPSHOT_VERSION:
        raise SnapshotVersionError(
            f"快照版本不兼容：当前版本 {SNAPSHOT_VERSION}，快照版本 {version}"
        )

    if "batch" not in snapshot:
        raise SnapshotFormatError("快照缺少 batch 字段")
    if "items" not in snapshot:
        raise SnapshotFormatError("快照缺少 items 字段")
    if "review_logs" not in snapshot:
        raise SnapshotFormatError("快照缺少 review_logs 字段")

    return snapshot


def restore_snapshot(
    db_path: str,
    snapshot_path: str,
    force: bool = False,
    evidence_dir: Optional[str] = None,
) -> Tuple[str, int]:
    """
    从快照恢复批次到数据库。

    参数：
        db_path: 目标数据库路径
        snapshot_path: 快照文件路径
        force: 是否强制覆盖已存在的同名批次
        evidence_dir: 重映射证据目录路径（None 则使用快照中的路径）

    返回：(批次号, 证据项数量)

    异常：
        SnapshotConflictError: 批次已存在且未使用 --force
        SnapshotNotFoundError: 快照文件不存在
        SnapshotFormatError: 快照格式错误
        SnapshotVersionError: 版本不兼容
        SnapshotMissingFilesError: 快照引用的清单、证据目录或单个证据文件缺失
    """
    snapshot = load_snapshot(snapshot_path)

    batch_data = snapshot["batch"]
    batch_no = batch_data["batch_no"]
    items_data = snapshot["items"]
    review_logs_data = snapshot["review_logs"]

    existing = db.get_batch_by_no(db_path, batch_no)
    if existing and not force:
        raise SnapshotConflictError(
            f"批次 '{batch_no}' 已存在，使用 --force 强制覆盖"
        )

    if evidence_dir:
        evidence_dir = os.path.abspath(evidence_dir)
    else:
        evidence_dir = batch_data["evidence_dir"]

    manifest_path = batch_data["manifest_path"]

    missing = []
    if not os.path.isfile(manifest_path):
        missing.append(f"清单文件: {manifest_path}")
    if not os.path.isdir(evidence_dir):
        missing.append(f"证据目录: {evidence_dir}")
    else:
        missing_files = []
        for item in items_data:
            rel = item.get("file_path")
            if not rel:
                continue
            full = os.path.join(evidence_dir, rel)
            if not os.path.isfile(full):
                missing_files.append(f"{rel} (清单第{item.get('manifest_line_no', '?')}行)")
        if missing_files:
            missing.append(
                f"证据文件缺失 {len(missing_files)} 个:\n    "
                + "\n    ".join(missing_files)
            )
    if missing:
        raise SnapshotMissingFilesError(
            "快照引用的路径缺失，无法恢复：\n  " + "\n  ".join(missing)
        )

    _restore_batch_with_logs(
        db_path=db_path,
        batch_no=batch_no,
        manifest_path=manifest_path,
        evidence_dir=evidence_dir,
        description=batch_data.get("description"),
        batch_created_at=batch_data.get("created_at"),
        batch_updated_at=batch_data.get("updated_at"),
        items=items_data,
        review_logs=review_logs_data,
        force=force,
    )

    return batch_no, len(items_data)


def _restore_batch_with_logs(
    db_path: str,
    batch_no: str,
    manifest_path: str,
    evidence_dir: str,
    description: Optional[str],
    batch_created_at: float,
    batch_updated_at: float,
    items: List[Dict],
    review_logs: List[Dict],
    force: bool,
) -> int:
    """
    原子恢复批次及其证据项和复核历史。

    在单个事务中完成：删除旧批次（如果 force）、创建新批次、插入证据项、插入复核日志。
    任何异常都会回滚，数据库保持不变。
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        old = conn.execute(
            "SELECT id FROM batches WHERE batch_no = ?",
            (batch_no,),
        ).fetchone()
        if old and force:
            conn.execute("DELETE FROM batches WHERE id = ?", (old["id"],))

        cursor = conn.execute(
            """INSERT INTO batches
               (batch_no, manifest_path, evidence_dir, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                batch_no,
                manifest_path,
                evidence_dir,
                description,
                batch_created_at,
                batch_updated_at,
            ),
        )
        new_batch_id = cursor.lastrowid

        item_id_map = {}
        for item in items:
            item_cursor = conn.execute(
                """INSERT INTO evidence_items
                   (batch_id, file_path, expected_size, expected_sha256,
                    manifest_line_no, actual_size, actual_sha256,
                    precheck_status, review_status, review_remark, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_batch_id,
                    item["file_path"],
                    item.get("expected_size"),
                    item.get("expected_sha256"),
                    item["manifest_line_no"],
                    item.get("actual_size"),
                    item.get("actual_sha256"),
                    item.get("precheck_status", "unchecked"),
                    item.get("review_status", "pending"),
                    item.get("review_remark"),
                    item.get("reviewed_at"),
                ),
            )
            item_id_map[item["id"]] = item_cursor.lastrowid

        log_id_map = {}
        for log in review_logs:
            old_item_id = log["item_id"]
            new_item_id = item_id_map.get(old_item_id, old_item_id)

            old_undo_of_id = log.get("undo_of_id")
            new_undo_of_id = log_id_map.get(old_undo_of_id, old_undo_of_id) if old_undo_of_id else None

            log_cursor = conn.execute(
                """INSERT INTO review_logs
                   (batch_id, item_id, prev_status, prev_remark, new_status, new_remark,
                    action, operator, undone, undo_of_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_batch_id,
                    new_item_id,
                    log["prev_status"],
                    log.get("prev_remark"),
                    log["new_status"],
                    log.get("new_remark"),
                    log["action"],
                    log.get("operator"),
                    log.get("undone", 0),
                    new_undo_of_id,
                    log["created_at"],
                ),
            )
            log_id_map[log["id"]] = log_cursor.lastrowid

        conn.commit()
        return new_batch_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_snapshots(work_dir: str) -> List[Dict]:
    """
    列出工作目录中的所有快照。

    返回列表，每项包含：name, path, size, created_at, batch_no
    """
    snapshot_dir = get_snapshot_dir(work_dir)
    if not os.path.isdir(snapshot_dir):
        return []

    snapshots = []
    for filename in os.listdir(snapshot_dir):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(snapshot_dir, filename)
        if not os.path.isfile(filepath):
            continue

        try:
            stat = os.stat(filepath)
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            batch_no = data.get("batch", {}).get("batch_no", "未知")
            snapshot_created_at = data.get("snapshot_created_at", stat.st_mtime)

            snapshots.append({
                "name": filename[:-5] if filename.endswith(".json") else filename,
                "path": filepath,
                "size": stat.st_size,
                "created_at": snapshot_created_at,
                "batch_no": batch_no,
            })
        except (json.JSONDecodeError, OSError):
            snapshots.append({
                "name": filename[:-5] if filename.endswith(".json") else filename,
                "path": filepath,
                "size": stat.st_size,
                "created_at": stat.st_mtime,
                "batch_no": "无效快照",
            })

    snapshots.sort(key=lambda s: s["created_at"], reverse=True)
    return snapshots
