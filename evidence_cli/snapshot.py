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
) -> Tuple[str, int, Dict]:
    """
    从快照恢复批次到数据库。

    参数：
        db_path: 目标数据库路径
        snapshot_path: 快照文件路径
        force: 是否强制覆盖已存在的同名批次
        evidence_dir: 重映射证据目录路径（None 则使用快照中的路径）

    返回：(批次号, 证据项数量, 恢复摘要字典)

    异常：
        SnapshotConflictError: 批次已存在且未使用 --force
        SnapshotNotFoundError: 快照文件不存在
        SnapshotFormatError: 快照格式错误
        SnapshotVersionError: 版本不兼容
        SnapshotMissingFilesError: 快照引用的清单、证据目录或单个证据文件缺失
    """
    preview = preview_restore(
        db_path=db_path,
        snapshot_path=snapshot_path,
        force=force,
        evidence_dir=evidence_dir,
    )

    if not preview["can_restore"]:
        if preview.get("conflict_reason"):
            raise SnapshotConflictError(preview["conflict_reason"])
        if preview.get("missing_reason"):
            raise SnapshotMissingFilesError(preview["missing_reason"])

    snapshot = load_snapshot(snapshot_path)
    batch_data = snapshot["batch"]
    batch_no = batch_data["batch_no"]
    items_data = snapshot["items"]
    review_logs_data = snapshot["review_logs"]

    abs_snapshot_path = os.path.abspath(snapshot_path)
    restore_diff = preview.get("diff")

    _restore_batch_with_logs(
        db_path=db_path,
        batch_no=batch_no,
        manifest_path=preview["manifest_path"],
        evidence_dir=preview["evidence_dir"],
        description=batch_data.get("description"),
        batch_created_at=batch_data.get("created_at"),
        batch_updated_at=batch_data.get("updated_at"),
        items=items_data,
        review_logs=review_logs_data,
        force=force,
        snapshot_path=abs_snapshot_path,
        restore_diff=restore_diff,
    )

    summary = {
        "batch_no": batch_no,
        "item_count": len(items_data),
        "restored_from": abs_snapshot_path,
        "manifest_path": preview["manifest_path"],
        "evidence_dir": preview["evidence_dir"],
        "evidence_remapped": preview.get("evidence_remapped", False),
        "precheck_stats": preview["precheck_stats"],
        "review_stats": preview["review_stats"],
        "last_log": preview["last_log"],
        "was_force": force,
        "was_conflict": preview["will_conflict"],
        "diff": restore_diff,
    }

    return batch_no, len(items_data), summary


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
    snapshot_path: Optional[str] = None,
    restore_diff: Optional[Dict] = None,
) -> int:
    """
    原子恢复批次及其证据项和复核历史。

    在单个事务中完成：删除旧批次（如果 force）、创建新批次、插入证据项、插入复核日志。
    任何异常都会回滚，数据库保持不变。

    参数：
        snapshot_path: 快照文件路径，用于记录恢复来源
        restore_diff: 新旧批次差异字典，用于记录覆盖恢复的差异
    """
    import sqlite3
    import json
    import time

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

        restore_diff_json = json.dumps(restore_diff, ensure_ascii=False) if restore_diff else None
        restored_at = time.time() if snapshot_path else None

        cursor = conn.execute(
            """INSERT INTO batches
               (batch_no, manifest_path, evidence_dir, description, created_at, updated_at,
                restored_from, restored_at, restore_diff)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_no,
                manifest_path,
                evidence_dir,
                description,
                batch_created_at,
                batch_updated_at,
                snapshot_path,
                restored_at,
                restore_diff_json,
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


def preview_restore(
    db_path: str,
    snapshot_path: str,
    force: bool = False,
    evidence_dir: Optional[str] = None,
) -> Dict:
    """
    预演恢复操作，不修改数据库。

    返回预演摘要，包含：
    - will_conflict: 是否会冲突
    - existing_batch: 已存在的批次信息（如果有）
    - snapshot_batch: 快照中的批次信息
    - manifest_path: 清单文件路径（映射后）
    - evidence_dir: 证据目录路径（映射后）
    - item_count: 证据项数量
    - precheck_stats: 预检统计 (total, passed, failed, unchecked)
    - review_stats: 复核统计 (total, signed, supplement, pending)
    - last_log: 最近一条操作记录
    - missing_files: 缺失的文件列表
    - diff: 新旧批次差异（如果 force 且存在旧批次）

    异常同 restore_snapshot。
    """
    from . import db as db_mod

    snapshot = load_snapshot(snapshot_path)

    batch_data = snapshot["batch"]
    batch_no = batch_data["batch_no"]
    items_data = snapshot["items"]
    review_logs_data = snapshot["review_logs"]

    result = {
        "snapshot_path": os.path.abspath(snapshot_path),
        "batch_no": batch_no,
        "will_conflict": False,
        "can_restore": True,
        "existing_batch": None,
        "snapshot_batch": batch_data,
        "manifest_path": None,
        "evidence_dir": None,
        "item_count": len(items_data),
        "precheck_stats": None,
        "review_stats": None,
        "last_log": None,
        "missing_files": [],
        "diff": None,
    }

    existing = db_mod.get_batch_by_no(db_path, batch_no)
    if existing:
        result["will_conflict"] = True
        result["existing_batch"] = existing
        if not force:
            result["can_restore"] = False
            result["conflict_reason"] = f"批次 '{batch_no}' 已存在，使用 --force 强制覆盖"

    evidence_remapped = False
    if evidence_dir:
        evidence_dir = os.path.abspath(evidence_dir)
        evidence_remapped = True
    else:
        evidence_dir = batch_data["evidence_dir"]

    manifest_path = batch_data["manifest_path"]

    result["manifest_path"] = manifest_path
    result["evidence_dir"] = evidence_dir
    result["evidence_remapped"] = evidence_remapped

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
    result["missing_files"] = missing
    if missing:
        result["can_restore"] = False
        result["missing_reason"] = "快照引用的路径缺失，无法恢复：\n  " + "\n  ".join(missing)

    total = len(items_data)
    passed = sum(1 for i in items_data if i.get("precheck_status") == "passed")
    failed = sum(1 for i in items_data if i.get("precheck_status") == "failed")
    unchecked = total - passed - failed
    result["precheck_stats"] = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "unchecked": unchecked,
    }

    signed = sum(1 for i in items_data if i.get("review_status") == "signed")
    supplement = sum(1 for i in items_data if i.get("review_status") == "supplement")
    pending = total - signed - supplement
    result["review_stats"] = {
        "total": total,
        "signed": signed,
        "supplement": supplement,
        "pending": pending,
    }

    if review_logs_data:
        last_log = review_logs_data[-1]
        result["last_log"] = {
            "id": last_log.get("id"),
            "action": last_log.get("action"),
            "item_id": last_log.get("item_id"),
            "prev_status": last_log.get("prev_status"),
            "new_status": last_log.get("new_status"),
            "operator": last_log.get("operator"),
            "created_at": last_log.get("created_at"),
            "file_path": next(
                (i.get("file_path") for i in items_data if i.get("id") == last_log.get("item_id")),
                None
            ),
        }

    if existing and force:
        old_items = db_mod.get_evidence_items(db_path, existing["id"])
        old_total, old_signed, old_supplement, old_pending = db_mod.count_reviewed(
            db_path, existing["id"]
        )
        old_pc_total, old_pc_passed, old_pc_failed, old_pc_unchecked = db_mod.count_precheck(
            db_path, existing["id"]
        )

        old_paths = {i["file_path"] for i in old_items}
        new_paths = {i["file_path"] for i in items_data}

        result["diff"] = {
            "old_batch": {
                "description": existing.get("description"),
                "created_at": existing.get("created_at"),
                "updated_at": existing.get("updated_at"),
                "manifest_path": existing.get("manifest_path"),
                "evidence_dir": existing.get("evidence_dir"),
            },
            "new_batch": {
                "description": batch_data.get("description"),
                "created_at": batch_data.get("created_at"),
                "updated_at": batch_data.get("updated_at"),
                "manifest_path": manifest_path,
                "evidence_dir": evidence_dir,
            },
            "review_stats": {
                "old": {"total": old_total, "signed": old_signed, "supplement": old_supplement, "pending": old_pending},
                "new": result["review_stats"],
            },
            "precheck_stats": {
                "old": {"total": old_pc_total, "passed": old_pc_passed, "failed": old_pc_failed, "unchecked": old_pc_unchecked},
                "new": result["precheck_stats"],
            },
            "items": {
                "only_in_old": sorted(old_paths - new_paths),
                "only_in_new": sorted(new_paths - old_paths),
                "in_both": sorted(old_paths & new_paths),
            },
        }

    return result


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
