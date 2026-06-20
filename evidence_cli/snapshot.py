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
    operator: Optional[str] = None,
    handoff_import_id: Optional[int] = None,
) -> Tuple[str, int, Dict]:
    """
    从快照恢复批次到数据库。

    参数：
        db_path: 目标数据库路径
        snapshot_path: 快照文件路径
        force: 是否强制覆盖已存在的同名批次
        evidence_dir: 重映射证据目录路径（None 则使用快照中的路径）
        operator: 操作人（可选，写入恢复事件）
        handoff_import_id: 关联的交接包导入记录 ID（可选，用于恢复链路回退）

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
    snapshot_created_at = snapshot.get("snapshot_created_at")

    evidence_dir_before = batch_data.get("evidence_dir")
    manifest_path_before = batch_data.get("manifest_path")

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
        snapshot_created_at=snapshot_created_at,
        evidence_dir_before=evidence_dir_before,
        manifest_path_before=manifest_path_before,
        operator=operator,
        handoff_import_id=handoff_import_id,
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
    snapshot_created_at: Optional[float] = None,
    evidence_dir_before: Optional[str] = None,
    manifest_path_before: Optional[str] = None,
    operator: Optional[str] = None,
    handoff_import_id: Optional[int] = None,
) -> int:
    """
    原子恢复批次及其证据项、复核历史、恢复事件链路。

    在单个事务中完成：
      1. 捕获旧批次状态（用于父事件关联和旧快照存档）
      2. 删除旧批次（如果 force）
      3. 创建新批次
      4. 插入证据项
      5. 插入复核日志
      6. 插入 restore_events 事件并回写 batches.last_restore_event_id

    任何异常都会回滚，数据库保持不变。
    """
    import sqlite3
    import json
    import time

    from . import db as db_mod

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        old = conn.execute(
            "SELECT * FROM batches WHERE batch_no = ?",
            (batch_no,),
        ).fetchone()

        parent_restore_event_id = None
        old_batch_snapshot_json = None

        if old and force:
            old_dict = dict(old)
            parent_restore_event_id = old_dict.get("last_restore_event_id")

            old_items_rows = conn.execute(
                "SELECT * FROM evidence_items WHERE batch_id = ?",
                (old["id"],),
            ).fetchall()
            old_logs_rows = conn.execute(
                "SELECT * FROM review_logs WHERE batch_id = ?",
                (old["id"],),
            ).fetchall()
            old_review_stats = db_mod.count_reviewed.__wrapped__(conn, old["id"]) if hasattr(
                db_mod.count_reviewed, "__wrapped__"
            ) else None
            if old_review_stats is None:
                ot = conn.execute(
                    "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ?",
                    (old["id"],),
                ).fetchone()[0]
                os_ = conn.execute(
                    "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ? AND review_status = 'signed'",
                    (old["id"],),
                ).fetchone()[0]
                osupp = conn.execute(
                    "SELECT COUNT(*) FROM evidence_items WHERE batch_id = ? AND review_status = 'supplement'",
                    (old["id"],),
                ).fetchone()[0]
                old_review_stats = (ot, os_, osupp, ot - os_ - osupp)

            old_batch_snapshot = {
                "batch": {
                    "id": old_dict.get("id"),
                    "batch_no": old_dict.get("batch_no"),
                    "manifest_path": old_dict.get("manifest_path"),
                    "evidence_dir": old_dict.get("evidence_dir"),
                    "description": old_dict.get("description"),
                    "created_at": old_dict.get("created_at"),
                    "updated_at": old_dict.get("updated_at"),
                    "restored_from": old_dict.get("restored_from"),
                    "restored_at": old_dict.get("restored_at"),
                    "last_restore_event_id": old_dict.get("last_restore_event_id"),
                },
                "review_stats": {
                    "total": old_review_stats[0],
                    "signed": old_review_stats[1],
                    "supplement": old_review_stats[2],
                    "pending": old_review_stats[3],
                },
                "item_count": len(old_items_rows),
                "review_log_count": len(old_logs_rows),
            }
            old_batch_snapshot_json = json.dumps(old_batch_snapshot, ensure_ascii=False)

        restore_diff_json = json.dumps(restore_diff, ensure_ascii=False) if restore_diff else None
        restored_at = time.time() if snapshot_path else None

        old_batch_id_for_cleanup = None
        if old and force:
            old_batch_id_for_cleanup = old["id"]
            conn.execute(
                "UPDATE batches SET batch_no = ? WHERE id = ?",
                (f"__old_{old['id']}_{int(time.time()*1000)}", old["id"]),
            )

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

        if old_batch_id_for_cleanup is not None:
            conn.execute(
                "UPDATE restore_events SET batch_id = ? WHERE batch_id = ?",
                (new_batch_id, old_batch_id_for_cleanup),
            )
            conn.execute("DELETE FROM batches WHERE id = ?", (old_batch_id_for_cleanup,))

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

        if snapshot_path and restored_at is not None:
            db_mod._insert_restore_event_with_conn(
                conn,
                batch_id=new_batch_id,
                batch_no=batch_no,
                snapshot_path=snapshot_path,
                snapshot_created_at=snapshot_created_at,
                parent_restore_event_id=parent_restore_event_id,
                restored_at=restored_at,
                was_force=force,
                was_remapped=(evidence_dir_before is not None and evidence_dir_before != evidence_dir),
                evidence_dir_before=evidence_dir_before,
                evidence_dir_after=evidence_dir,
                manifest_path_before=manifest_path_before,
                manifest_path_after=manifest_path,
                old_batch_snapshot=old_batch_snapshot_json,
                restore_diff=restore_diff_json,
                operator=operator,
                handoff_import_id=handoff_import_id,
            )

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


def build_recovery_summary(db_path: str, batch_no: str) -> Optional[Dict]:
    """
    构建统一的、可对账的恢复摘要。

    这是所有恢复相关视图（trace / list / resume / export）的单一事实来源。
    从持久化数据（batches、restore_events、evidence_items、review_logs）动态聚合，
    重启 CLI 后查询结果完全一致。

    返回 None 表示批次不存在。从未恢复的批次同样返回结构（source_snapshot 为 None）。

    返回结构：
        {
            "batch_no": str,
            "has_restore": bool,
            "source_snapshot": {           # 来源快照
                "path": str,
                "exists": bool,
                "created_at": Optional[float],
            } | None,
            "overwrite_diff": Optional[Dict],  # 覆盖差异（仅 force 覆盖时有值）
            "last_review_log": Optional[Dict], # 快照内 / 当前最后一条复核记录（含 file_path）
            "post_restore_ops": {              # 恢复后新增操作数
                "count": int,
                "review_count": int,
                "undo_count": int,
            },
            "restore_event": {                 # 最近一次恢复事件详情
                "event_id": int,
                "restored_at": float,
                "was_force": bool,
                "was_remapped": bool,
                "operator": Optional[str],
                "parent_event_id": Optional[int],
            } | None,
            "precheck_stats": Dict,            # 预检统计 (total/passed/failed/unchecked)
            "review_stats": Dict,              # 复核统计 (total/signed/supplement/pending)
            "item_count": int,                 # 证据项总数
            "reconciled": bool,                # 对账是否通过
            "reconciliation_details": {
                "item_count_consistent": bool,
                "review_stats_consistent": bool,
                "post_restore_count_consistent": bool,
            },
            "warnings": List[str],
        }
    """
    import json as _json
    from . import db as db_mod

    batch = db_mod.get_batch_by_no(db_path, batch_no)
    if not batch:
        return None

    batch_id = batch["id"]
    items = db_mod.get_evidence_items(db_path, batch_id)

    total_pc, passed_pc, failed_pc, unchecked_pc = db_mod.count_precheck(db_path, batch_id)
    total_rv, signed_rv, supp_rv, pend_rv = db_mod.count_reviewed(db_path, batch_id)

    precheck_stats = {
        "total": total_pc,
        "passed": passed_pc,
        "failed": failed_pc,
        "unchecked": unchecked_pc,
    }
    review_stats = {
        "total": total_rv,
        "signed": signed_rv,
        "supplement": supp_rv,
        "pending": pend_rv,
    }

    last_review_log = None
    last_raw = db_mod.get_last_review_log(db_path, batch_id)
    if last_raw:
        last_item = db_mod.get_evidence_item_by_id(db_path, last_raw["item_id"])
        last_review_log = {
            "id": last_raw.get("id"),
            "action": last_raw.get("action"),
            "item_id": last_raw.get("item_id"),
            "prev_status": last_raw.get("prev_status"),
            "new_status": last_raw.get("new_status"),
            "prev_remark": last_raw.get("prev_remark"),
            "new_remark": last_raw.get("new_remark"),
            "operator": last_raw.get("operator"),
            "created_at": last_raw.get("created_at"),
            "file_path": last_item.get("file_path") if last_item else None,
            "manifest_line_no": last_item.get("manifest_line_no") if last_item else None,
        }

    events_raw = db_mod.get_restore_events_for_batch(db_path, batch_no=batch_no)
    has_chain = len(events_raw) > 0

    warnings: List[str] = []
    source_snapshot = None
    overwrite_diff = None
    restore_event = None
    post_restore_ops = {"count": 0, "review_count": 0, "undo_count": 0}

    if has_chain:
        last_evt = events_raw[-1]

        snapshot_path = last_evt["snapshot_path"]
        snapshot_exists = os.path.isfile(snapshot_path)
        handoff_import_id = last_evt.get("handoff_import_id")
        handoff_import = None
        if handoff_import_id:
            handoff_import = db_mod.get_handoff_import_by_id(db_path, handoff_import_id)

        if not snapshot_exists:
            if handoff_import:
                warnings.append("临时快照文件已清理，但交接包来源记录已持久化保存")
            else:
                warnings.append("来源快照文件已不存在，无法再次从此源恢复")

        source_snapshot = {
            "path": snapshot_path,
            "exists": snapshot_exists,
            "created_at": last_evt.get("snapshot_created_at"),
        }

        if handoff_import:
            package_path = handoff_import.get("package_path", "")
            package_exists = os.path.isfile(package_path)
            source_snapshot["handoff_package"] = {
                "path": package_path,
                "exists": package_exists,
                "package_version": handoff_import.get("package_version"),
                "imported_at": handoff_import.get("imported_at"),
                "operator": handoff_import.get("operator"),
            }
            source_summary = handoff_import.get("source_summary", {})
            if isinstance(source_summary, dict):
                source_snapshot["source_summary"] = source_summary
            restore_result = handoff_import.get("restore_result", {})
            if isinstance(restore_result, dict):
                if "manifest_path" in restore_result:
                    source_snapshot["manifest_path"] = restore_result["manifest_path"]
                if "evidence_dir" in restore_result:
                    source_snapshot["evidence_dir"] = restore_result["evidence_dir"]
                if "evidence_remapped" in restore_result:
                    source_snapshot["evidence_remapped"] = restore_result["evidence_remapped"]

        if last_evt.get("restore_diff"):
            try:
                overwrite_diff = _json.loads(last_evt["restore_diff"])
            except (_json.JSONDecodeError, TypeError):
                warnings.append("恢复差异数据损坏，无法解析")
                overwrite_diff = None

        restore_event = {
            "event_id": last_evt["id"],
            "restored_at": last_evt["restored_at"],
            "was_force": bool(last_evt["was_force"]),
            "was_remapped": bool(last_evt["was_remapped"]),
            "operator": last_evt.get("operator"),
            "parent_event_id": last_evt.get("parent_restore_event_id"),
        }

        post_activity = db_mod.get_review_logs_after_time(
            db_path, batch_id, last_evt["restored_at"]
        )
        review_count = sum(1 for l in post_activity if l["action"] != "undo")
        undo_count = sum(1 for l in post_activity if l["action"] == "undo")
        post_restore_ops = {
            "count": len(post_activity),
            "review_count": review_count,
            "undo_count": undo_count,
        }
        if post_restore_ops["count"] > 0:
            warnings.append(
                f"恢复后有 {post_restore_ops['count']} 条新操作"
                f"（复核 {review_count} 条，撤销 {undo_count} 条）"
            )

        event_by_id = {e["id"]: e for e in events_raw}
        for e in events_raw:
            if e.get("parent_restore_event_id") is not None:
                pid = e["parent_restore_event_id"]
                if pid not in event_by_id:
                    warnings.append(
                        f"恢复链路断档：事件 #{e['id']} 的父事件 #{pid} 未找到"
                    )

        missing_count = sum(1 for e in events_raw if not os.path.isfile(e["snapshot_path"]))
        if missing_count > 0 and missing_count == len(events_raw):
            warnings.append(
                f"全部 {len(events_raw)} 次恢复的源快照均已丢失"
            )
        elif missing_count > 0:
            warnings.append(
                f"{missing_count} / {len(events_raw)} 次恢复的源快照已丢失"
            )

    elif batch.get("restored_from"):
        snapshot_path = batch["restored_from"]
        snapshot_exists = os.path.isfile(snapshot_path)
        source_snapshot = {
            "path": snapshot_path,
            "exists": snapshot_exists,
            "created_at": None,
        }
        if not snapshot_exists:
            warnings.append("来源快照文件已不存在")
        if batch.get("restore_diff"):
            try:
                overwrite_diff = _json.loads(batch["restore_diff"])
            except (_json.JSONDecodeError, TypeError):
                warnings.append("恢复差异数据损坏，无法解析")
        warnings.append("旧版本恢复数据，仅含基础来源信息（无完整恢复链路）")

    item_count_consistent = len(items) == precheck_stats["total"] == review_stats["total"]

    review_stats_consistent = (
        review_stats["signed"] + review_stats["supplement"] + review_stats["pending"]
        == review_stats["total"]
    )

    post_count_from_db = post_restore_ops["count"]
    post_restore_count_consistent = True
    if has_chain:
        last_evt = events_raw[-1]
        post_activity_check = db_mod.get_review_logs_after_time(
            db_path, batch_id, last_evt["restored_at"]
        )
        post_restore_count_consistent = len(post_activity_check) == post_count_from_db

    reconciled = (
        item_count_consistent
        and review_stats_consistent
        and post_restore_count_consistent
    )

    if not item_count_consistent:
        warnings.append(
            f"对账告警：证据项数不一致 items={len(items)} "
            f"precheck.total={precheck_stats['total']} "
            f"review.total={review_stats['total']}"
        )
    if not review_stats_consistent:
        warnings.append(
            f"对账告警：复核统计不一致 "
            f"signed+supplement+pending="
            f"{review_stats['signed'] + review_stats['supplement'] + review_stats['pending']} "
            f"!= total={review_stats['total']}"
        )
    if not post_restore_count_consistent:
        warnings.append("对账告警：恢复后操作计数与实际记录数不符")

    return {
        "batch_no": batch_no,
        "has_restore": has_chain or batch.get("restored_from") is not None,
        "source_snapshot": source_snapshot,
        "overwrite_diff": overwrite_diff,
        "last_review_log": last_review_log,
        "post_restore_ops": post_restore_ops,
        "restore_event": restore_event,
        "precheck_stats": precheck_stats,
        "review_stats": review_stats,
        "item_count": len(items),
        "reconciled": reconciled,
        "reconciliation_details": {
            "item_count_consistent": item_count_consistent,
            "review_stats_consistent": review_stats_consistent,
            "post_restore_count_consistent": post_restore_count_consistent,
        },
        "warnings": warnings,
    }


def build_recovery_summary_from_preview(preview: Dict) -> Dict:
    """
    根据预演结果构建与 build_recovery_summary 同构的摘要。

    用于预演（dry-run）阶段，让用户在落库前就能看到与恢复后一致的摘要结构，
    判断这份快照值不值得恢复。
    """
    import json as _json

    has_diff = preview.get("diff") is not None
    overwrite_diff = preview.get("diff")
    last_review_log = preview.get("last_log")

    post_restore_ops = {"count": 0, "review_count": 0, "undo_count": 0}

    precheck_stats = preview.get("precheck_stats", {
        "total": 0, "passed": 0, "failed": 0, "unchecked": 0,
    })
    review_stats = preview.get("review_stats", {
        "total": 0, "signed": 0, "supplement": 0, "pending": 0,
    })

    warnings: List[str] = []

    if not preview.get("can_restore", False):
        if preview.get("conflict_reason"):
            warnings.append(f"恢复受阻：{preview['conflict_reason']}")
        if preview.get("missing_reason"):
            warnings.append(preview["missing_reason"])

    snapshot_path = preview.get("snapshot_path", "")
    snapshot_exists = os.path.isfile(snapshot_path) if snapshot_path else False

    source_snapshot = {
        "path": snapshot_path,
        "exists": snapshot_exists,
        "created_at": None,
    }

    restore_event = None
    if preview.get("can_restore"):
        restore_event = {
            "event_id": None,
            "restored_at": None,
            "was_force": bool(preview.get("will_conflict") and preview.get("can_restore")),
            "was_remapped": bool(preview.get("evidence_remapped")),
            "operator": None,
            "parent_event_id": None,
        }

    item_count = preview.get("item_count", 0)

    item_count_consistent = (
        item_count == precheck_stats.get("total", 0) == review_stats.get("total", 0)
    )
    review_stats_consistent = (
        review_stats.get("signed", 0)
        + review_stats.get("supplement", 0)
        + review_stats.get("pending", 0)
        == review_stats.get("total", 0)
    )
    can_restore = preview.get("can_restore", False)
    reconciled = item_count_consistent and review_stats_consistent and can_restore

    if not can_restore:
        warnings.append("对账告警：预演结果表明无法恢复，数据未落地")
    if not item_count_consistent:
        warnings.append(
            f"对账告警：证据项数不一致 item_count={item_count} "
            f"precheck.total={precheck_stats.get('total')} "
            f"review.total={review_stats.get('total')}"
        )
    if not review_stats_consistent:
        warnings.append(
            f"对账告警：复核统计不一致 "
            f"signed+supplement+pending="
            f"{review_stats.get('signed', 0) + review_stats.get('supplement', 0) + review_stats.get('pending', 0)} "
            f"!= total={review_stats.get('total', 0)}"
        )

    return {
        "batch_no": preview.get("batch_no", ""),
        "has_restore": True,
        "source_snapshot": source_snapshot,
        "overwrite_diff": overwrite_diff,
        "last_review_log": last_review_log,
        "post_restore_ops": post_restore_ops,
        "restore_event": restore_event,
        "precheck_stats": precheck_stats,
        "review_stats": review_stats,
        "item_count": item_count,
        "reconciled": reconciled,
        "reconciliation_details": {
            "item_count_consistent": item_count_consistent,
            "review_stats_consistent": review_stats_consistent,
            "post_restore_count_consistent": True,
        },
        "warnings": warnings,
    }


def build_trace(db_path: str, batch_no: str) -> Optional[Dict]:
    """
    构建批次的完整恢复链路追踪信息。

    返回 None 表示批次不存在。
    返回结构：
        {
            "batch_no": str,
            "batch_id": int,
            "batch": dict,  # 完整批次记录
            "recovery_summary": dict,  # 统一恢复摘要（与 build_recovery_summary 同构）
            "events": [     # 按时间顺序（从最早到最近）的恢复事件
                {
                    "event_id": int,
                    "restored_at": float,
                    "snapshot_path": str,
                    "snapshot_exists": bool,
                    "snapshot_created_at": Optional[float],
                    "parent_event_id": Optional[int],
                    "was_force": bool,
                    "was_remapped": bool,
                    "evidence_dir_before": Optional[str],
                    "evidence_dir_after": str,
                    "manifest_path_before": Optional[str],
                    "manifest_path_after": str,
                    "old_batch_snapshot": Optional[Dict],  # 已反序列化
                    "restore_diff": Optional[Dict],         # 已反序列化
                    "operator": Optional[str],
                    "chain_ok": bool,
                    "warnings": List[str],
                },
                ...
            ],
            "post_restore_activity": List[Dict],  # 最近一次恢复后的复核/撤销记录
            "modified_after_restore": bool,
            "warnings": List[str],
            "has_restore_chain": bool,
        }
    """
    import json
    from . import db as db_mod

    batch = db_mod.get_batch_by_no(db_path, batch_no)
    if not batch:
        return None

    batch_id = batch["id"]
    events_raw = db_mod.get_restore_events_for_batch(db_path, batch_no=batch_no)

    has_chain = len(events_raw) > 0
    warnings: List[str] = []
    events: List[Dict] = []

    event_by_id = {e["id"]: e for e in events_raw}

    last_restored_at = None

    for e in events_raw:
        ev_warnings: List[str] = []

        snapshot_exists = os.path.isfile(e["snapshot_path"])
        handoff_import_id = e.get("handoff_import_id")
        handoff_import = None
        if handoff_import_id:
            handoff_import = db_mod.get_handoff_import_by_id(db_path, handoff_import_id)

        if not snapshot_exists:
            if handoff_import:
                ev_warnings.append("临时快照已清理，但交接包来源记录已持久化保存")
            else:
                ev_warnings.append("快照源文件已不存在")

        chain_ok = True
        if e.get("parent_restore_event_id") is not None:
            parent_id = e["parent_restore_event_id"]
            if parent_id not in event_by_id:
                chain_ok = False
                ev_warnings.append(
                    f"父恢复事件 #{parent_id} 未找到，恢复链路可能断档（可能该批次从外部恢复而来）"
                )

        old_batch_snapshot = None
        if e.get("old_batch_snapshot"):
            try:
                old_batch_snapshot = json.loads(e["old_batch_snapshot"])
            except (json.JSONDecodeError, TypeError):
                ev_warnings.append("旧批次存档数据损坏，无法解析")

        restore_diff = None
        if e.get("restore_diff"):
            try:
                restore_diff = json.loads(e["restore_diff"])
            except (json.JSONDecodeError, TypeError):
                ev_warnings.append("恢复差异数据损坏，无法解析")

        event_data = {
            "event_id": e["id"],
            "restored_at": e["restored_at"],
            "snapshot_path": e["snapshot_path"],
            "snapshot_exists": snapshot_exists,
            "snapshot_created_at": e.get("snapshot_created_at"),
            "parent_event_id": e.get("parent_restore_event_id"),
            "was_force": e["was_force"],
            "was_remapped": e["was_remapped"],
            "evidence_dir_before": e.get("evidence_dir_before"),
            "evidence_dir_after": e["evidence_dir_after"],
            "manifest_path_before": e.get("manifest_path_before"),
            "manifest_path_after": e["manifest_path_after"],
            "old_batch_snapshot": old_batch_snapshot,
            "restore_diff": restore_diff,
            "operator": e.get("operator"),
            "chain_ok": chain_ok,
            "warnings": ev_warnings,
        }

        if handoff_import:
            package_path = handoff_import.get("package_path", "")
            event_data["handoff_import"] = {
                "import_id": handoff_import["id"],
                "package_path": package_path,
                "package_exists": os.path.isfile(package_path),
                "package_version": handoff_import.get("package_version"),
                "imported_at": handoff_import.get("imported_at"),
                "operator": handoff_import.get("operator"),
                "was_force": handoff_import.get("was_force", False),
                "source_summary": handoff_import.get("source_summary", {}),
                "restore_result": handoff_import.get("restore_result", {}),
                "status": handoff_import.get("status"),
            }

        events.append(event_data)

        last_restored_at = e["restored_at"]

    post_activity: List[Dict] = []
    modified_after = False
    if last_restored_at is not None:
        post_activity = db_mod.get_review_logs_after_time(
            db_path, batch_id, last_restored_at
        )
        modified_after = len(post_activity) > 0

    if modified_after:
        warnings.append(
            f"该批次在最近一次恢复后有 {len(post_activity)} 条新的复核/撤销操作"
        )

    if has_chain:
        if not all(ev["chain_ok"] for ev in events):
            warnings.append("恢复链路存在断档，部分历史可能不可追溯")
        missing_count = sum(1 for ev in events if not ev["snapshot_exists"])
        has_handoff_backup = sum(1 for ev in events if ev.get("handoff_import"))
        if missing_count > 0:
            if has_handoff_backup == missing_count:
                warnings.append(
                    f"有 {missing_count} / {len(events)} 个临时快照已清理，"
                    f"但对应交接包来源记录已持久化保存（可通过 handoff log 查看）"
                )
            else:
                warnings.append(
                    f"有 {missing_count} / {len(events)} 个快照源文件已丢失，无法再次从源恢复"
                )

    if not has_chain and batch.get("restored_from"):
        warnings.append(
            "批次标记为已恢复，但缺少 restore_events 明细（旧版本数据，链路不可追溯）"
        )

    recovery_summary = build_recovery_summary(db_path, batch_no)

    return {
        "batch_no": batch_no,
        "batch_id": batch_id,
        "batch": batch,
        "recovery_summary": recovery_summary,
        "events": events,
        "post_restore_activity": post_activity,
        "modified_after_restore": modified_after,
        "warnings": warnings,
        "has_restore_chain": has_chain,
    }


def build_command_chain(db_path: str, batch_no: str) -> Optional[Dict]:
    """
    基于持久化数据生成恢复核对命令链。

    返回 None 表示批次不存在。

    返回结构:
        {
            "batch_no": str,
            "recovery_summary": dict,        # 统一恢复摘要
            "trace_data": dict,              # 完整 trace 数据
            "scenario": str,                 # 当前场景描述
            "steps": [                        # 命令链步骤（按推荐顺序）
                {
                    "order": int,
                    "name": str,             # 步骤名（预演恢复 / 正式恢复 / 追链 / 继续复核 / 撤销 / 导出 等）
                    "description": str,      # 步骤说明
                    "required": bool,        # 是否必填步骤
                    "command": str,          # 可直接复制执行的完整命令
                    "required_options": [    # 必填选项说明
                        {"option": str, "value": str, "reason": str},
                        ...
                    ],
                    "optional_options": [    # 可选选项说明
                        {"option": str, "value": str, "reason": str},
                        ...
                    ],
                    "applicable": bool,      # 该步骤在当前场景下是否适用
                    "applicable_reason": str, # 不适用时的原因
                },
                ...
            ],
            "warnings": List[str],           # 与其他命令对齐的告警
        }
    """
    from . import db as db_mod

    batch = db_mod.get_batch_by_no(db_path, batch_no)
    if not batch:
        return None

    recovery_summary = build_recovery_summary(db_path, batch_no)
    trace_data = build_trace(db_path, batch_no)

    has_restore = recovery_summary.get("has_restore", False)
    was_force = recovery_summary.get("restore_event", {}).get("was_force", False) if recovery_summary.get("restore_event") else False
    was_remapped = recovery_summary.get("restore_event", {}).get("was_remapped", False) if recovery_summary.get("restore_event") else False
    snapshot_exists = recovery_summary.get("source_snapshot", {}).get("exists", False) if recovery_summary.get("source_snapshot") else False
    snapshot_path = recovery_summary.get("source_snapshot", {}).get("path", "") if recovery_summary.get("source_snapshot") else ""
    post_op_count = recovery_summary.get("post_restore_ops", {}).get("count", 0)
    reconciled = recovery_summary.get("reconciled", False)
    manifest_path = batch.get("manifest_path", "")
    evidence_dir = batch.get("evidence_dir", "")

    warnings: List[str] = list(recovery_summary.get("warnings", []))

    scenario_parts = []
    if not has_restore:
        scenario_parts.append("原始导入批次，未从快照恢复")
    else:
        if was_force:
            scenario_parts.append("强制覆盖恢复")
        else:
            scenario_parts.append("普通恢复")
        if was_remapped:
            scenario_parts.append("目录重映射")
        if not snapshot_exists:
            scenario_parts.append("来源快照丢失")
        if post_op_count > 0:
            scenario_parts.append(f"恢复后追加 {post_op_count} 条操作")
        if not reconciled:
            scenario_parts.append("对账告警")
    scenario = "、".join(scenario_parts) if scenario_parts else "未知场景"

    steps: List[Dict] = []
    order = 0

    if not has_restore:
        order += 1
        steps.append({
            "order": order,
            "name": "预演恢复",
            "description": "从快照预演恢复到当前工作目录，不修改数据库，仅显示恢复摘要",
            "required": False,
            "command": f"evi snapshot restore -s <快照文件路径> --dry-run",
            "required_options": [
                {"option": "-s / --snapshot", "value": "<快照文件路径>", "reason": "指定要恢复的快照 JSON 文件"},
            ],
            "optional_options": [
                {"option": "-e / --evidence-dir", "value": "<新证据目录>", "reason": "若快照记录的证据目录已不存在，使用此选项重映射"},
                {"option": "-f / --force", "value": "", "reason": "若同名批次已存在，使用此选项强制覆盖（仅 dry-run 时预览差异）"},
                {"option": "-o / --operator", "value": "<操作人>", "reason": "记录恢复操作人"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        steps.append({
            "order": order,
            "name": "正式恢复",
            "description": "从快照正式恢复批次到当前工作目录的数据库",
            "required": True,
            "command": f"evi snapshot restore -s <快照文件路径>",
            "required_options": [
                {"option": "-s / --snapshot", "value": "<快照文件路径>", "reason": "指定要恢复的快照 JSON 文件"},
            ],
            "optional_options": [
                {"option": "-e / --evidence-dir", "value": "<新证据目录>", "reason": "重映射证据目录路径"},
                {"option": "-f / --force", "value": "", "reason": "若同名批次已存在，必须使用此选项强制覆盖"},
                {"option": "-o / --operator", "value": "<操作人>", "reason": "记录恢复操作人"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

    if has_restore:
        order += 1
        steps.append({
            "order": order,
            "name": "查看恢复摘要",
            "description": "显示批次的统一恢复摘要，与 trace/list/resume/export 使用同一份持久化数据",
            "required": True,
            "command": f"evi resume -b {batch_no}",
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
            ],
            "optional_options": [
                {"option": "-n / --show-items", "value": "<N>", "reason": "显示最近 N 条复核历史（默认 10）"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        steps.append({
            "order": order,
            "name": "恢复链路追踪",
            "description": "查看完整恢复链路：每次恢复的父子关系、快照文件状态、目录映射、覆盖差异、恢复后操作明细",
            "required": True,
            "command": f"evi trace -b {batch_no}",
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
            ],
            "optional_options": [],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        steps.append({
            "order": order,
            "name": "批次列表概览",
            "description": "在批次列表中查看该批次的恢复标记、进度、告警，与其他批次横向对比",
            "required": False,
            "command": "evi list",
            "required_options": [],
            "optional_options": [],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        precheck_cmd = f"evi precheck -b {batch_no}"
        steps.append({
            "order": order,
            "name": "完整性预检",
            "description": "核对证据文件的路径、大小、SHA256 是否与清单一致（只读不修改证据文件）",
            "required": False,
            "command": precheck_cmd,
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
            ],
            "optional_options": [
                {"option": "-i / --item", "value": "<证据项ID>", "reason": "只检查指定证据项"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        review_cmd = f"evi review -b {batch_no} -i <证据项ID> -s <signed|supplement> -r <备注>"
        steps.append({
            "order": order,
            "name": "继续复核",
            "description": "对恢复后的批次继续复核，标记为已签收或待补件",
            "required": False,
            "command": review_cmd,
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
                {"option": "-i / --item", "value": "<证据项ID>", "reason": "要复核的证据项 ID（可用 evi status 查看）"},
                {"option": "-s / --status", "value": "<signed|supplement>", "reason": "复核状态：signed=已签收，supplement=待补件"},
            ],
            "optional_options": [
                {"option": "-r / --remark", "value": "<备注>", "reason": "复核备注"},
                {"option": "-o / --operator", "value": "<操作人>", "reason": "记录操作人"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        undo_cmd = f"evi undo -b {batch_no}"
        steps.append({
            "order": order,
            "name": "撤销复核",
            "description": "撤销最后一条复核操作，恢复之前的状态和备注",
            "required": False,
            "command": undo_cmd,
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
            ],
            "optional_options": [
                {"option": "-o / --operator", "value": "<操作人>", "reason": "记录撤销操作人"},
            ],
            "applicable": post_op_count > 0,
            "applicable_reason": "" if post_op_count > 0 else "当前批次恢复后暂无任何操作，无可撤销内容",
        })

        order += 1
        status_cmd = f"evi status -b {batch_no}"
        steps.append({
            "order": order,
            "name": "查看证据项状态",
            "description": "列出批次所有证据项的预检和复核状态，方便找到待处理项",
            "required": False,
            "command": status_cmd,
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
            ],
            "optional_options": [
                {"option": "-f / --filter", "value": "<all|pending|signed|supplement|failed_precheck>", "reason": "按状态筛选"},
                {"option": "-n / --limit", "value": "<N>", "reason": "最多显示数量（默认 20）"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

        order += 1
        export_cmd = f"evi export -b {batch_no} -o <输出路径> -f json"
        steps.append({
            "order": order,
            "name": "导出 JSON 报告",
            "description": "导出包含恢复摘要、恢复链路、证据项状态的完整 JSON 报告",
            "required": False,
            "command": export_cmd,
            "required_options": [
                {"option": "-b / --batch", "value": batch_no, "reason": "批次号（与当前核对批次一致）"},
                {"option": "-o / --output", "value": "<输出路径>", "reason": "导出文件路径（建议 .json 后缀）"},
                {"option": "-f / --format", "value": "json", "reason": "导出格式，json 格式包含完整恢复链路和摘要"},
            ],
            "optional_options": [
                {"option": "-f / --format", "value": "csv", "reason": "仅导出 CSV 格式的证据项状态（不含恢复摘要和链路）"},
            ],
            "applicable": True,
            "applicable_reason": "",
        })

        if snapshot_exists and was_force:
            order += 1
            steps.append({
                "order": order,
                "name": "再次预演（校验强制覆盖场景）",
                "description": "对已强制覆盖的批次，再次用同一份快照做 dry-run 预演，确认覆盖差异与记录一致",
                "required": False,
                "command": f"evi snapshot restore -s {snapshot_path} --force --dry-run",
                "required_options": [
                    {"option": "-s / --snapshot", "value": snapshot_path, "reason": "与原恢复使用同一份快照文件"},
                    {"option": "-f / --force", "value": "", "reason": "必须与原恢复方式一致，才能看到相同的覆盖差异"},
                    {"option": "--dry-run", "value": "", "reason": "仅预演，不修改数据库"},
                ],
                "optional_options": [
                    {"option": "-e / --evidence-dir", "value": evidence_dir, "reason": "如原恢复使用了重映射，此处也应使用相同路径"},
                ],
                "applicable": True,
                "applicable_reason": "",
            })

    return {
        "batch_no": batch_no,
        "recovery_summary": recovery_summary,
        "trace_data": trace_data,
        "scenario": scenario,
        "steps": steps,
        "warnings": warnings,
    }
