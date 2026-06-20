"""批次交接包：打包导出、查看、导入恢复

交接包结构（tar.gz）：
    _manifest.json       包元数据（版本、校验和、时间戳、来源摘要）
    manifest.csv         原始清单文件
    snapshot.json        批次完整快照（批次信息、证据项、复核日志）
    recent_ops.json      最近操作日志
    last_playbook.json   最近一次剧本运行记录（可能不存在）
    export_report.json   导出报告
"""

import os
import sys
import json
import time
import gzip
import hashlib
import tarfile
import shutil
import tempfile
from io import BytesIO
from typing import Dict, List, Optional, Tuple, Any

from . import db
from . import snapshot as snapshot_mod
from . import report as report_mod

HANDOFF_VERSION = "1.0"
HANDOFF_DIR = ".handoffs"

PACKAGE_MANIFEST = "_manifest.json"
FILE_MANIFEST = "manifest.csv"
FILE_SNAPSHOT = "snapshot.json"
FILE_RECENT_OPS = "recent_ops.json"
FILE_LAST_PLAYBOOK = "last_playbook.json"
FILE_EXPORT_REPORT = "export_report.json"

REQUIRED_FILES = [PACKAGE_MANIFEST, FILE_MANIFEST, FILE_SNAPSHOT,
                  FILE_RECENT_OPS, FILE_EXPORT_REPORT]


class HandoffError(Exception):
    pass


class HandoffNotFoundError(HandoffError):
    pass


class HandoffFormatError(HandoffError):
    pass


class HandoffVersionError(HandoffError):
    pass


class HandoffChecksumError(HandoffError):
    pass


class HandoffMissingFilesError(HandoffError):
    pass


class HandoffConflictError(HandoffError):
    pass


class HandoffPermissionError(HandoffError):
    pass


def get_handoff_dir(work_dir: str) -> str:
    return os.path.join(work_dir, HANDOFF_DIR)


def get_handoff_path(work_dir: str, name: str) -> str:
    if not name.endswith(".tar.gz"):
        name += ".tar.gz"
    return os.path.join(get_handoff_dir(work_dir), name)


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _collect_recent_ops(db_path: str, batch_id: int, limit: int = 100) -> List[Dict]:
    return db.get_review_history(db_path, batch_id, limit=limit)


def _collect_last_playbook(db_path: str, batch_no: str) -> Optional[Dict]:
    run = db.get_last_playbook_run(db_path, batch_no)
    if not run:
        return None
    run_id = run["id"]
    run_with_steps = db.get_playbook_run_with_steps(db_path, run_id)

    library = db.list_playbook_library(db_path, batch_no=batch_no)

    return {
        "last_run": run_with_steps,
        "playbook_library": library,
    }


def _build_export_report(db_path: str, batch_no: str) -> Dict:
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        raise HandoffError(f"批次 '{batch_no}' 不存在")

    items = db.get_evidence_items(db_path, batch["id"])

    total_pc, passed, failed, unchecked = db.count_precheck(db_path, batch["id"])
    total_rv, signed, supplement, pending = db.count_reviewed(db_path, batch["id"])

    precheck_stats = {"total": total_pc, "passed": passed, "failed": failed, "unchecked": unchecked}
    review_stats = {"total": total_rv, "signed": signed, "supplement": supplement, "pending": pending}

    restore_trace = snapshot_mod.build_trace(db_path, batch_no)

    report = {
        "version": HANDOFF_VERSION,
        "generated_at": time.time(),
        "batch": {
            "batch_no": batch.get("batch_no", ""),
            "description": batch.get("description", ""),
            "manifest_path": batch.get("manifest_path", ""),
            "evidence_dir": batch.get("evidence_dir", ""),
            "created_at": batch.get("created_at", 0),
            "updated_at": batch.get("updated_at", 0),
            "restored_from": batch.get("restored_from"),
            "restored_at": batch.get("restored_at"),
        },
        "statistics": {
            "precheck": precheck_stats,
            "review": review_stats,
        },
        "items": items,
        "restore_trace": restore_trace,
    }
    return report


def _check_source_files(batch: Dict) -> Tuple[bool, List[str]]:
    """检查打包前源文件是否完整"""
    issues = []

    manifest_path = batch.get("manifest_path", "")
    if not os.path.isfile(manifest_path):
        issues.append(f"清单文件缺失: {manifest_path}")

    evidence_dir = batch.get("evidence_dir", "")
    if not os.path.isdir(evidence_dir):
        issues.append(f"证据目录缺失: {evidence_dir}")

    return (len(issues) == 0, issues)


def _check_target_writable(output_path: str) -> Tuple[bool, Optional[str]]:
    """检查目标目录是否可写"""
    target_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    if not os.path.isdir(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            return False, f"无法创建目标目录: {e}"

    try:
        test_file = os.path.join(target_dir, f".writable_test_{os.getpid()}_{int(time.time()*1000)}")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return True, None
    except OSError as e:
        return False, f"目标目录不可写: {e}"


def create_handoff(
    db_path: str,
    work_dir: str,
    batch_no: str,
    output_path: str,
    operator: Optional[str] = None,
) -> Dict:
    """
    创建批次交接包。

    前置检查：
      - 源批次存在
      - manifest 文件存在、证据目录存在
      - 目标目录可写

    返回包元数据字典。
    """
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        raise HandoffNotFoundError(f"批次 '{batch_no}' 不存在")

    ok, missing = _check_source_files(batch)
    if not ok:
        raise HandoffMissingFilesError(
            "源文件缺失，无法打包:\n  " + "\n  ".join(missing)
        )

    ok, err = _check_target_writable(output_path)
    if not ok:
        raise HandoffPermissionError(err)

    output_path = os.path.abspath(output_path)

    recent_ops = _collect_recent_ops(db_path, batch["id"])
    last_playbook = _collect_last_playbook(db_path, batch_no)
    export_report = _build_export_report(db_path, batch_no)

    try:
        snapshot_data = snapshot_mod.save_snapshot(
            db_path, batch_no, os.path.join(tempfile.gettempdir(), f"_handoff_snap_{os.getpid()}.json")
        )
    except Exception as e:
        raise HandoffError(f"生成快照失败: {e}")

    with open(batch["manifest_path"], "rb") as f:
        manifest_bytes = f.read()

    files_in_package: Dict[str, bytes] = {}

    package_manifest = {
        "version": HANDOFF_VERSION,
        "created_at": time.time(),
        "source": {
            "work_dir": os.path.abspath(work_dir),
            "operator": operator,
            "db_path": os.path.abspath(db_path),
        },
        "batch": {
            "batch_no": batch["batch_no"],
            "description": batch.get("description"),
            "manifest_path": batch["manifest_path"],
            "evidence_dir": batch["evidence_dir"],
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "item_count": len(snapshot_data["items"]),
        },
        "checksums": {},
        "files": [],
    }

    snapshot_bytes = json.dumps(snapshot_data, ensure_ascii=False, indent=2).encode("utf-8")
    recent_ops_bytes = json.dumps(recent_ops, ensure_ascii=False, indent=2).encode("utf-8")
    export_report_bytes = json.dumps(export_report, ensure_ascii=False, indent=2).encode("utf-8")

    files_in_package[FILE_MANIFEST] = manifest_bytes
    files_in_package[FILE_SNAPSHOT] = snapshot_bytes
    files_in_package[FILE_RECENT_OPS] = recent_ops_bytes
    files_in_package[FILE_EXPORT_REPORT] = export_report_bytes
    package_manifest["files"] = [FILE_MANIFEST, FILE_SNAPSHOT, FILE_RECENT_OPS, FILE_EXPORT_REPORT]

    if last_playbook is not None:
        playbook_bytes = json.dumps(last_playbook, ensure_ascii=False, indent=2).encode("utf-8")
        files_in_package[FILE_LAST_PLAYBOOK] = playbook_bytes
        package_manifest["files"].append(FILE_LAST_PLAYBOOK)

    for fname, fbytes in files_in_package.items():
        package_manifest["checksums"][fname] = _sha256_bytes(fbytes)

    package_manifest_bytes = json.dumps(package_manifest, ensure_ascii=False, indent=2).encode("utf-8")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with tarfile.open(output_path, "w:gz") as tar:
        info = tarfile.TarInfo(name=PACKAGE_MANIFEST)
        info.size = len(package_manifest_bytes)
        info.mtime = time.time()
        tar.addfile(info, BytesIO(package_manifest_bytes))

        for fname, fbytes in files_in_package.items():
            info = tarfile.TarInfo(name=fname)
            info.size = len(fbytes)
            info.mtime = time.time()
            tar.addfile(info, BytesIO(fbytes))

    return {
        "output_path": output_path,
        "package_manifest": package_manifest,
    }


def _read_handoff(package_path: str) -> Dict[str, bytes]:
    """读取交接包中所有文件内容"""
    if not os.path.isfile(package_path):
        raise HandoffNotFoundError(f"交接包不存在: {package_path}")

    contents: Dict[str, bytes] = {}
    try:
        with tarfile.open(package_path, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                contents[member.name] = f.read()
    except (tarfile.TarError, gzip.BadGzipFile, OSError) as e:
        raise HandoffFormatError(f"交接包格式错误或损坏: {e}")

    return contents


def _validate_package(contents: Dict[str, bytes]) -> Dict:
    """
    校验交接包完整性：
      - 必需文件存在
      - 包元数据 JSON 合法
      - 版本兼容
      - 校验和匹配

    返回解析后的 package_manifest。
    """
    for req in REQUIRED_FILES:
        if req not in contents:
            raise HandoffFormatError(f"交接包缺少必需文件: {req}")

    try:
        package_manifest = json.loads(contents[PACKAGE_MANIFEST].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HandoffFormatError(f"包元数据 JSON 解析失败: {e}")

    if not isinstance(package_manifest, dict):
        raise HandoffFormatError("包元数据格式错误：根节点不是对象")

    version = package_manifest.get("version")
    if not version:
        raise HandoffFormatError("包元数据缺少 version 字段")

    if version != HANDOFF_VERSION:
        raise HandoffVersionError(
            f"交接包版本不兼容：当前版本 {HANDOFF_VERSION}，包版本 {version}"
        )

    checksums = package_manifest.get("checksums", {})
    if not isinstance(checksums, dict):
        raise HandoffFormatError("包元数据 checksums 格式错误")

    for fname, expected_sha in checksums.items():
        if fname not in contents:
            raise HandoffChecksumError(f"校验和列表中的文件缺失: {fname}")
        actual_sha = _sha256_bytes(contents[fname])
        if actual_sha != expected_sha:
            raise HandoffChecksumError(
                f"文件校验和不匹配: {fname} (期望 {expected_sha[:16]}..., 实际 {actual_sha[:16]}...)"
            )

    return package_manifest


def inspect_handoff(package_path: str) -> Dict:
    """
    查看交接包内容。不解包到磁盘，仅在内存中读取。

    返回结构：
      {
        "package_path": str,
        "package_manifest": dict,
        "snapshot": dict,
        "recent_ops": list,
        "last_playbook": dict | None,
        "export_report": dict,
      }
    """
    contents = _read_handoff(package_path)
    package_manifest = _validate_package(contents)

    try:
        snapshot = json.loads(contents[FILE_SNAPSHOT].decode("utf-8"))
        recent_ops = json.loads(contents[FILE_RECENT_OPS].decode("utf-8"))
        export_report = json.loads(contents[FILE_EXPORT_REPORT].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HandoffFormatError(f"交接包内 JSON 文件解析失败: {e}")

    last_playbook = None
    if FILE_LAST_PLAYBOOK in contents:
        try:
            last_playbook = json.loads(contents[FILE_LAST_PLAYBOOK].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            last_playbook = None

    return {
        "package_path": os.path.abspath(package_path),
        "package_manifest": package_manifest,
        "snapshot": snapshot,
        "recent_ops": recent_ops,
        "last_playbook": last_playbook,
        "export_report": export_report,
        "_manifest_bytes": contents.get(FILE_MANIFEST, b""),
    }


def _check_import_conflicts(
    db_path: str,
    package_manifest: Dict,
    last_playbook: Optional[Dict],
    force: bool,
) -> Tuple[bool, List[str]]:
    """
    检查导入冲突：
      - 同名批次已存在
      - 同名剧本已存在（若包中包含剧本库）

    force=True 时批次冲突不报错，但仍然返回在 conflicts 列表中供记录。
    """
    conflicts: List[str] = []

    batch_no = package_manifest["batch"]["batch_no"]
    existing_batch = db.get_batch_by_no(db_path, batch_no)
    if existing_batch:
        if force:
            conflicts.append(f"批次 '{batch_no}' 已存在（将被强制覆盖）")
        else:
            conflicts.append(f"批次 '{batch_no}' 已存在，使用 --force 强制覆盖")

    if last_playbook and last_playbook.get("playbook_library"):
        for pb in last_playbook["playbook_library"]:
            pb_name = pb.get("name", "")
            if pb_name and db.playbook_name_exists(db_path, pb_name):
                conflicts.append(f"同名剧本已存在: '{pb_name}'（导入时将跳过）")

    has_blocking = any("已存在，使用 --force" in c for c in conflicts)
    return (not has_blocking, conflicts)


def _check_work_dir_writable(work_dir: str) -> Tuple[bool, Optional[str]]:
    """检查 work-dir 是否可写"""
    if not os.path.isdir(work_dir):
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError as e:
            return False, f"工作目录不存在且无法创建: {e}"

    try:
        test_file = os.path.join(work_dir, f".writable_test_{os.getpid()}_{int(time.time()*1000)}")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return True, None
    except OSError as e:
        return False, f"工作目录不可写: {e}"


def preview_import_handoff(
    db_path: str,
    work_dir: str,
    package_path: str,
    force: bool = False,
    evidence_dir: Optional[str] = None,
) -> Dict:
    """
    预演交接包导入（只读核查），不修改数据库或文件系统。

    返回预演摘要：
      {
        "can_import": bool,
        "package_path": str,
        "package_manifest": dict,
        "snapshot_batch": dict,
        "work_dir_writable": bool,
        "work_dir_error": str | None,
        "conflicts": list[str],
        "missing_files": list[str],
        "manifest_path": str,
        "evidence_dir": str,
        "evidence_remapped": bool,
        "item_count": int,
        "precheck_stats": dict,
        "review_stats": dict,
        "last_log": dict | None,
        "last_playbook": dict | None,
        "diff": dict | None,  # 新旧批次差异（force 时）
        "import_log": list[dict],
      }
    """
    import_log: List[Dict] = []
    import_log.append({"step": "开始预演导入", "ts": time.time(), "ok": True})

    info = inspect_handoff(package_path)
    package_manifest = info["package_manifest"]
    snapshot = info["snapshot"]
    last_playbook = info["last_playbook"]
    batch_no = package_manifest["batch"]["batch_no"]
    import_log.append({"step": f"交接包解析成功，版本 {package_manifest.get('version')}",
                       "ts": time.time(), "ok": True})

    writable, wderr = _check_work_dir_writable(work_dir)
    import_log.append({
        "step": f"检查工作目录可写: {work_dir}",
        "ts": time.time(),
        "ok": writable,
        "detail": wderr if wderr else None,
    })

    can_import = writable

    ok, conflicts = _check_import_conflicts(db_path, package_manifest, last_playbook, force)
    import_log.append({
        "step": "检查冲突（批次/剧本）",
        "ts": time.time(),
        "ok": ok,
        "conflicts": conflicts,
    })
    if not ok:
        can_import = False

    snapshot_batch = snapshot["batch"]
    items_data = snapshot["items"]
    review_logs_data = snapshot["review_logs"]

    evidence_remapped = False
    if evidence_dir:
        evidence_dir = os.path.abspath(evidence_dir)
        evidence_remapped = True
    else:
        evidence_dir = snapshot_batch.get("evidence_dir")

    manifest_src = package_manifest["batch"]["manifest_path"]
    manifest_dir = os.path.dirname(manifest_src) or "."
    manifest_filename = os.path.basename(manifest_src)
    target_manifest_path = os.path.join(work_dir, manifest_filename)

    missing = []
    if not writable:
        missing.append(f"工作目录不可写: {wderr}")
    import_log.append({
        "step": "检查源路径引用",
        "ts": time.time(),
        "ok": len(missing) == 0,
        "missing": missing,
    })

    total = len(items_data)
    passed = sum(1 for i in items_data if i.get("precheck_status") == "passed")
    failed = sum(1 for i in items_data if i.get("precheck_status") == "failed")
    unchecked = total - passed - failed
    precheck_stats = {"total": total, "passed": passed, "failed": failed, "unchecked": unchecked}

    signed = sum(1 for i in items_data if i.get("review_status") == "signed")
    supplement = sum(1 for i in items_data if i.get("review_status") == "supplement")
    pending = total - signed - supplement
    review_stats = {"total": total, "signed": signed, "supplement": supplement, "pending": pending}

    last_log = None
    if review_logs_data:
        raw_last = review_logs_data[-1]
        last_log = {
            "id": raw_last.get("id"),
            "action": raw_last.get("action"),
            "item_id": raw_last.get("item_id"),
            "prev_status": raw_last.get("prev_status"),
            "new_status": raw_last.get("new_status"),
            "operator": raw_last.get("operator"),
            "created_at": raw_last.get("created_at"),
            "file_path": next(
                (i.get("file_path") for i in items_data if i.get("id") == raw_last.get("item_id")),
                None,
            ),
        }

    diff = None
    existing_batch = db.get_batch_by_no(db_path, batch_no)
    if existing_batch and force:
        old_items = db.get_evidence_items(db_path, existing_batch["id"])
        old_total, old_signed, old_supplement, old_pending = db.count_reviewed(db_path, existing_batch["id"])
        old_pc_total, old_pc_passed, old_pc_failed, old_pc_unchecked = db.count_precheck(db_path, existing_batch["id"])

        old_paths = {i["file_path"] for i in old_items}
        new_paths = {i["file_path"] for i in items_data}

        diff = {
            "old_batch": {
                "description": existing_batch.get("description"),
                "created_at": existing_batch.get("created_at"),
                "updated_at": existing_batch.get("updated_at"),
            },
            "new_batch": {
                "description": snapshot_batch.get("description"),
                "created_at": snapshot_batch.get("created_at"),
                "updated_at": snapshot_batch.get("updated_at"),
            },
            "review_stats": {
                "old": {"total": old_total, "signed": old_signed, "supplement": old_supplement, "pending": old_pending},
                "new": review_stats,
            },
            "precheck_stats": {
                "old": {"total": old_pc_total, "passed": old_pc_passed, "failed": old_pc_failed, "unchecked": old_pc_unchecked},
                "new": precheck_stats,
            },
            "items": {
                "only_in_old": sorted(old_paths - new_paths),
                "only_in_new": sorted(new_paths - old_paths),
                "in_both": sorted(old_paths & new_paths),
            },
        }

    import_log.append({
        "step": "预演完成",
        "ts": time.time(),
        "ok": can_import,
    })

    return {
        "can_import": can_import,
        "package_path": os.path.abspath(package_path),
        "package_manifest": package_manifest,
        "snapshot_batch": snapshot_batch,
        "work_dir_writable": writable,
        "work_dir_error": wderr,
        "conflicts": conflicts,
        "missing_files": missing,
        "manifest_path": target_manifest_path,
        "evidence_dir": evidence_dir,
        "evidence_remapped": evidence_remapped,
        "item_count": total,
        "precheck_stats": precheck_stats,
        "review_stats": review_stats,
        "last_log": last_log,
        "last_playbook": last_playbook,
        "diff": diff,
        "import_log": import_log,
    }


def import_handoff(
    db_path: str,
    work_dir: str,
    package_path: str,
    force: bool = False,
    evidence_dir: Optional[str] = None,
    operator: Optional[str] = None,
) -> Dict:
    """
    正式导入交接包：
      1. 复制 manifest 到 work-dir
      2. 保存持久化快照到 .snapshots 目录
      3. 先插入 handoff_imports 记录获取 ID
      4. 调用 snapshot_mod 恢复批次到数据库（关联 handoff_import_id）
      5. 导入剧本库（若有同名则跳过）
      6. 更新 handoff_imports 记录完整结果

    返回恢复结果字典。
    """
    preview = preview_import_handoff(
        db_path=db_path,
        work_dir=work_dir,
        package_path=package_path,
        force=force,
        evidence_dir=evidence_dir,
    )

    import_log = list(preview["import_log"])
    import_log.append({"step": "开始正式导入", "ts": time.time(), "ok": True})

    if not preview["can_import"]:
        db.insert_handoff_import(
            db_path=db_path,
            package_path=preview["package_path"],
            package_version=preview["package_manifest"].get("version", HANDOFF_VERSION),
            batch_no=preview["package_manifest"]["batch"]["batch_no"],
            source_summary=preview["package_manifest"].get("source", {}),
            import_log=import_log,
            restore_result={"error": "预演检查失败", "conflicts": preview["conflicts"]},
            status="failed",
            operator=operator,
            was_force=force,
            evidence_dir_remapped=evidence_dir if preview["evidence_remapped"] else None,
        )
        raise HandoffConflictError(
            "预演检查失败，无法导入:\n  " + "\n  ".join(
                c for c in preview["conflicts"] if "已存在，使用 --force" in c
            )
        )

    info = inspect_handoff(package_path)
    package_manifest = info["package_manifest"]
    snapshot = info["snapshot"]
    last_playbook = info["last_playbook"]
    batch_no = package_manifest["batch"]["batch_no"]

    manifest_src = package_manifest["batch"]["manifest_path"]
    manifest_filename = os.path.basename(manifest_src)
    target_manifest_path = os.path.join(work_dir, manifest_filename)

    manifest_bytes = info.get("_manifest_bytes", b"")
    with open(target_manifest_path, "wb") as f:
        f.write(manifest_bytes)
    import_log.append({
        "step": f"manifest 已复制到: {target_manifest_path}",
        "ts": time.time(), "ok": True,
    })

    snapshot_dir = snapshot_mod.get_snapshot_dir(work_dir)
    os.makedirs(snapshot_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    persistent_snap_path = os.path.join(
        snapshot_dir,
        f"handoff_{batch_no}_{timestamp}.json"
    )

    snapshot["batch"]["manifest_path"] = target_manifest_path
    snapshot["batch"]["evidence_dir"] = preview["evidence_dir"]

    with open(persistent_snap_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    import_log.append({
        "step": f"持久化快照已保存: {persistent_snap_path}",
        "ts": time.time(), "ok": True,
    })

    handoff_import_id = db.insert_handoff_import(
        db_path=db_path,
        package_path=preview["package_path"],
        package_version=package_manifest.get("version", HANDOFF_VERSION),
        batch_no=batch_no,
        source_summary=package_manifest.get("source", {}),
        import_log=import_log,
        restore_result={},
        status="importing",
        operator=operator,
        was_force=force,
        evidence_dir_remapped=evidence_dir if preview["evidence_remapped"] else None,
    )

    restored_batch_no, item_count, restore_summary = snapshot_mod.restore_snapshot(
        db_path=db_path,
        snapshot_path=persistent_snap_path,
        force=force,
        evidence_dir=preview["evidence_dir"],
        operator=operator,
        handoff_import_id=handoff_import_id,
    )

    import_log.append({
        "step": f"批次已恢复到数据库: {restored_batch_no} ({item_count} 项)",
        "ts": time.time(), "ok": True,
    })

    imported_playbooks: List[str] = []
    skipped_playbooks: List[str] = []
    if last_playbook and last_playbook.get("playbook_library"):
        for pb in last_playbook["playbook_library"]:
            pb_name = pb.get("name", "")
            if not pb_name:
                continue
            if db.playbook_name_exists(db_path, pb_name):
                skipped_playbooks.append(pb_name)
                continue
            try:
                db.save_playbook_to_library(
                    db_path=db_path,
                    name=pb_name,
                    batch_no=pb.get("batch_no", batch_no),
                    playbook_data=pb.get("playbook_data", {}),
                    description=pb.get("description"),
                    operator=pb.get("operator"),
                    output_file=pb.get("output_file"),
                    version=pb.get("version", "1.0"),
                    overwrite=False,
                )
                imported_playbooks.append(pb_name)
            except Exception as e:
                skipped_playbooks.append(f"{pb_name}(错误: {e})")

    import_log.append({
        "step": "剧本库导入完成",
        "ts": time.time(), "ok": True,
        "imported": imported_playbooks,
        "skipped": skipped_playbooks,
    })

    restore_result = {
        "batch_no": restored_batch_no,
        "item_count": item_count,
        "manifest_path": target_manifest_path,
        "evidence_dir": preview["evidence_dir"],
        "evidence_remapped": preview["evidence_remapped"],
        "restore_summary": restore_summary,
        "imported_playbooks": imported_playbooks,
        "skipped_playbooks": skipped_playbooks,
        "conflicts": preview["conflicts"],
    }

    db.update_handoff_import(
        db_path=db_path,
        import_id=handoff_import_id,
        import_log=import_log,
        restore_result=restore_result,
        status="imported",
    )

    import_log.append({"step": "导入完成，记录已写入 SQLite", "ts": time.time(), "ok": True})

    return {
        "batch_no": restored_batch_no,
        "item_count": item_count,
        "manifest_path": target_manifest_path,
        "evidence_dir": preview["evidence_dir"],
        "evidence_remapped": preview["evidence_remapped"],
        "restore_summary": restore_summary,
        "imported_playbooks": imported_playbooks,
        "skipped_playbooks": skipped_playbooks,
        "conflicts": preview["conflicts"],
        "import_log": import_log,
    }


def list_handoffs(work_dir: str) -> List[Dict]:
    handoff_dir = get_handoff_dir(work_dir)
    if not os.path.isdir(handoff_dir):
        return []

    result = []
    for fn in sorted(os.listdir(handoff_dir)):
        if not fn.endswith(".tar.gz"):
            continue
        fp = os.path.join(handoff_dir, fn)
        if not os.path.isfile(fp):
            continue
        try:
            stat = os.stat(fp)
            info = inspect_handoff(fp)
            batch_no = info["package_manifest"]["batch"].get("batch_no", "未知")
            created = info["package_manifest"].get("created_at", stat.st_mtime)
            result.append({
                "name": fn[:-7] if fn.endswith(".tar.gz") else fn,
                "path": fp,
                "size": stat.st_size,
                "created_at": created,
                "batch_no": batch_no,
                "version": info["package_manifest"].get("version", "?"),
            })
        except Exception:
            result.append({
                "name": fn[:-7] if fn.endswith(".tar.gz") else fn,
                "path": fp,
                "size": stat.st_size,
                "created_at": stat.st_mtime,
                "batch_no": "无效包",
                "version": "?",
            })
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return result
