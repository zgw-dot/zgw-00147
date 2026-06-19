"""批次操作剧本：将批量 precheck/review/undo/export 动作存成 JSON 剧本，预演后正式执行"""

import csv
import json
import os
import time
from typing import Dict, List, Optional, Tuple

from . import db
from . import precheck as precheck_mod

PLAYBOOK_VERSION = "1.0"


class PlaybookError(Exception):
    pass


class PlaybookVersionError(PlaybookError):
    pass


class PlaybookFormatError(PlaybookError):
    pass


class PlaybookConflictError(PlaybookError):
    pass


class PlaybookStepError(PlaybookError):
    pass


PLAYBOOK_STEP_TYPES = ("precheck", "review", "undo", "export")


def create_playbook(
    batch_no: str,
    steps: List[Dict],
    operator: str = "",
    output_file: str = "",
    description: str = "",
) -> Dict:
    playbook = {
        "version": PLAYBOOK_VERSION,
        "batch_no": batch_no,
        "description": description,
        "operator": operator,
        "output_file": output_file,
        "created_at": time.time(),
        "steps": [],
    }

    for idx, step in enumerate(steps):
        step_type = step.get("type")
        if step_type not in PLAYBOOK_STEP_TYPES:
            raise PlaybookFormatError(f"步骤 {idx + 1}: 不支持的类型 '{step_type}'")

        entry = {"type": step_type, "order": idx + 1}

        if step_type == "review":
            target = step.get("target_status")
            if target not in ("signed", "supplement"):
                raise PlaybookFormatError(
                    f"步骤 {idx + 1}: review 步骤必须指定 target_status (signed/supplement)"
                )
            entry["target_status"] = target

        if step.get("filter_status"):
            entry["filter_status"] = step["filter_status"]

        if step.get("line_range"):
            lr = step["line_range"]
            if not isinstance(lr, list) or len(lr) != 2:
                raise PlaybookFormatError(f"步骤 {idx + 1}: line_range 必须为 [start, end]")
            entry["line_range"] = lr

        if step.get("remark_template"):
            entry["remark_template"] = step["remark_template"]

        if step.get("operator"):
            entry["operator"] = step["operator"]

        if step_type == "export":
            out = step.get("output_path")
            if not out:
                raise PlaybookFormatError(f"步骤 {idx + 1}: export 步骤必须指定 output_path")
            entry["output_path"] = out
            entry["export_format"] = step.get("export_format", "auto")

        playbook["steps"].append(entry)

    return playbook


def save_playbook(playbook: Dict, output_path: str) -> str:
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(playbook, f, ensure_ascii=False, indent=2)
    return output_path


def load_playbook(playbook_path: str) -> Dict:
    if not os.path.isfile(playbook_path):
        raise PlaybookError(f"剧本文件不存在: {playbook_path}")

    try:
        with open(playbook_path, "r", encoding="utf-8") as f:
            playbook = json.load(f)
    except json.JSONDecodeError as e:
        raise PlaybookFormatError(f"剧本 JSON 格式错误: {e}") from e

    if not isinstance(playbook, dict):
        raise PlaybookFormatError("剧本格式错误：根节点不是对象")

    version = playbook.get("version")
    if version != PLAYBOOK_VERSION:
        raise PlaybookVersionError(
            f"剧本版本不兼容：当前版本 {PLAYBOOK_VERSION}，剧本版本 {version}"
        )

    if "batch_no" not in playbook:
        raise PlaybookFormatError("剧本缺少 batch_no 字段")
    if "steps" not in playbook:
        raise PlaybookFormatError("剧本缺少 steps 字段")

    for idx, step in enumerate(playbook["steps"]):
        if step.get("type") not in PLAYBOOK_STEP_TYPES:
            raise PlaybookFormatError(f"步骤 {idx + 1}: 不支持的类型 '{step.get('type')}'")

    return playbook


def _resolve_items_for_step(
    db_path: str,
    batch: Dict,
    step: Dict,
) -> List[Dict]:
    items = db.get_evidence_items(db_path, batch["id"])

    filter_status = step.get("filter_status")
    if filter_status and filter_status != "all":
        if filter_status == "failed_precheck":
            items = [i for i in items if i["precheck_status"] == "failed"]
        else:
            items = [i for i in items if i["review_status"] == filter_status]

    line_range = step.get("line_range")
    if line_range:
        start, end = line_range
        items = [i for i in items if start <= i["manifest_line_no"] <= end]

    return items


def _format_remark(template: str, item: Dict, batch_no: str) -> str:
    if not template:
        return ""
    return template.format(
        batch_no=batch_no,
        item_id=item.get("id", ""),
        file_path=item.get("file_path", ""),
        line_no=item.get("manifest_line_no", ""),
    )


def preview_playbook(
    db_path: str,
    playbook: Dict,
) -> Dict:
    batch_no = playbook["batch_no"]
    batch = db.get_batch_by_no(db_path, batch_no)

    result = {
        "can_execute": True,
        "batch_no": batch_no,
        "steps": [],
        "global_conflicts": [],
    }

    if not batch:
        result["can_execute"] = False
        result["global_conflicts"].append(f"批次 '{batch_no}' 不存在")
        return result

    run = db.get_last_playbook_run(db_path, batch_no)
    if run and run["status"] == "executing":
        result["can_execute"] = False
        result["global_conflicts"].append(f"批次 '{batch_no}' 有正在执行中的剧本运行 #{run['id']}，无法开始新执行")

    for step in playbook["steps"]:
        step_preview = {
            "order": step["order"],
            "type": step["type"],
            "matched_items": [],
            "skipped_reasons": [],
            "will_overwrite": [],
            "conflicts": [],
        }

        if step["type"] == "precheck":
            items = _resolve_items_for_step(db_path, batch, step)
            for item in items:
                step_preview["matched_items"].append({
                    "id": item["id"],
                    "file_path": item["file_path"],
                    "manifest_line_no": item["manifest_line_no"],
                    "current_precheck_status": item["precheck_status"],
                })
            if not items:
                step_preview["skipped_reasons"].append("筛选条件下没有匹配的证据项")

        elif step["type"] == "review":
            items = _resolve_items_for_step(db_path, batch, step)
            target_status = step.get("target_status", "signed")
            for item in items:
                matched = {
                    "id": item["id"],
                    "file_path": item["file_path"],
                    "manifest_line_no": item["manifest_line_no"],
                    "current_status": item["review_status"],
                    "target_status": target_status,
                }
                step_preview["matched_items"].append(matched)
                if item["review_status"] == target_status:
                    step_preview["will_overwrite"].append(
                        f"证据项 #{item['id']} ({item['file_path']}) 已经是 {target_status}，"
                        f"操作会覆盖备注"
                    )
                elif item["review_status"] != "pending":
                    step_preview["will_overwrite"].append(
                        f"证据项 #{item['id']} ({item['file_path']}) 当前为 {item['review_status']}，"
                        f"将被改为 {target_status}"
                    )
            if not items:
                step_preview["skipped_reasons"].append("筛选条件下没有匹配的证据项")

        elif step["type"] == "undo":
            last_log = db.get_last_review_log(db_path, batch["id"])
            if last_log:
                item_info = db.get_evidence_item_by_id(db_path, last_log["item_id"])
                step_preview["matched_items"].append({
                    "id": last_log["item_id"],
                    "file_path": item_info["file_path"] if item_info else "",
                    "current_status": last_log["new_status"],
                    "will_revert_to": last_log["prev_status"],
                })
            else:
                step_preview["skipped_reasons"].append("没有可撤销的复核记录")

        elif step["type"] == "export":
            output_path = step.get("output_path", "")
            if output_path and os.path.isfile(os.path.abspath(output_path)):
                step_preview["conflicts"].append(
                    f"输出文件已存在: {output_path}，执行时会被覆盖"
                )
                step_preview["will_overwrite"].append(f"文件: {output_path}")
            if output_path:
                parent = os.path.dirname(os.path.abspath(output_path))
                if parent and os.path.isdir(parent) and not os.access(parent, os.W_OK):
                    step_preview["conflicts"].append(
                        f"导出目录只读: {parent}，无法写入"
                    )
            all_items = db.get_evidence_items(db_path, batch["id"])
            step_preview["matched_items"] = [
                {
                    "id": i["id"],
                    "file_path": i["file_path"],
                    "review_status": i["review_status"],
                }
                for i in all_items
            ]

        if step_preview["conflicts"]:
            step_preview["has_conflicts"] = True

        result["steps"].append(step_preview)

    output_file = playbook.get("output_file")
    if output_file and os.path.isfile(os.path.abspath(output_file)):
        result["global_conflicts"].append(f"剧本级输出文件已存在: {output_file}")

    return result


def execute_playbook(
    db_path: str,
    playbook: Dict,
    operator: Optional[str] = None,
    force: bool = False,
    library_name: Optional[str] = None,
) -> Dict:
    batch_no = playbook["batch_no"]
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        raise PlaybookConflictError(f"批次 '{batch_no}' 不存在")

    playbook_operator = operator or playbook.get("operator", "")

    run = db.get_last_playbook_run(db_path, batch_no)
    if run and run["status"] == "executing":
        raise PlaybookConflictError(
            f"批次 '{batch_no}' 有正在执行中的剧本运行 #{run['id']}，无法开始新执行"
        )

    fingerprint_before = _compute_batch_fingerprint(db_path, batch)

    if not force:
        playbook_batch_updated_at = playbook.get("batch_updated_at")
        if playbook_batch_updated_at is not None:
            current_batch = db.get_batch_by_no(db_path, batch_no)
            if current_batch and current_batch["updated_at"] != playbook_batch_updated_at:
                raise PlaybookConflictError(
                    f"批次 '{batch_no}' 在剧本导入后被手工修改过（updated_at 变化），"
                    f"请确认最新状态后使用 --force 强制执行"
                )

    output_file = playbook.get("output_file")
    if output_file and os.path.isfile(os.path.abspath(output_file)) and not force:
        raise PlaybookConflictError(
            f"输出文件已存在: {output_file}，使用 --force 覆盖"
        )

    if not force:
        for step in playbook.get("steps", []):
            if step.get("type") == "export":
                step_output = step.get("output_path")
                if step_output:
                    abs_step_output = os.path.abspath(step_output)
                    parent = os.path.dirname(abs_step_output)
                    if parent and os.path.isdir(parent) and not os.access(parent, os.W_OK):
                        raise PlaybookConflictError(
                            f"导出目录只读: {parent}，无法写入 {step_output}"
                        )
                    if os.path.isfile(abs_step_output):
                        raise PlaybookConflictError(
                            f"导出步骤输出文件已存在: {step_output}，使用 --force 覆盖"
                        )

    preview = preview_playbook(db_path, playbook)
    if not preview["can_execute"] and not force:
        conflicts = "; ".join(preview["global_conflicts"])
        raise PlaybookConflictError(f"预演失败: {conflicts}")

    run_id = db.create_playbook_run(
        db_path,
        batch_no=batch_no,
        operator=playbook_operator,
        playbook_data=playbook,
        fingerprint_before=fingerprint_before,
    )

    executed_steps = []
    overall_error = None
    batch_ref = batch

    for step in playbook["steps"]:
        step_result = {
            "order": step["order"],
            "type": step["type"],
            "status": "success",
            "affected_items": [],
            "error": None,
        }

        try:
            if step["type"] == "precheck":
                _execute_precheck_step(db_path, batch_ref, step, step_result)
            elif step["type"] == "review":
                _execute_review_step(
                    db_path, batch_ref, step, step_result,
                    batch_no=batch_no, operator=playbook_operator,
                )
            elif step["type"] == "undo":
                _execute_undo_step(
                    db_path, batch_ref, step, step_result,
                    operator=playbook_operator,
                )
            elif step["type"] == "export":
                _execute_export_step(db_path, batch_ref, step, step_result)
        except PlaybookStepError as e:
            step_result["status"] = "failed"
            step_result["error"] = str(e)
            overall_error = f"步骤 {step['order']} ({step['type']}) 失败: {e}"

            db.log_playbook_step(
                db_path, run_id=run_id,
                step_order=step["order"], step_type=step["type"],
                status="failed", affected_items=step_result["affected_items"],
                error_message=str(e),
            )

            _rollback_executed_steps(db_path, executed_steps, batch_no, playbook_operator)
            db.update_playbook_run_status(db_path, run_id=run_id, status="rolled_back",
                                          error_message=overall_error)
            if library_name:
                try:
                    db.update_playbook_library_last_run(db_path, library_name, run_id, "rolled_back")
                except Exception:
                    pass
            return {
                "run_id": run_id,
                "batch_no": batch_no,
                "status": "rolled_back",
                "steps": executed_steps + [step_result],
                "error": overall_error,
            }

        if step_result["status"] == "skipped":
            db.log_playbook_step(
                db_path, run_id=run_id,
                step_order=step["order"], step_type=step["type"],
                status="skipped", affected_items=[],
                error_message=None,
            )
        else:
            db.log_playbook_step(
                db_path, run_id=run_id,
                step_order=step["order"], step_type=step["type"],
                status=step_result["status"],
                affected_items=step_result["affected_items"],
                error_message=None,
            )

        executed_steps.append(step_result)

        batch_ref = db.get_batch_by_no(db_path, batch_no) or batch_ref

    db.update_playbook_run_status(db_path, run_id=run_id, status="completed")
    if library_name:
        try:
            db.update_playbook_library_last_run(db_path, library_name, run_id, "completed")
        except Exception:
            pass
    return {
        "run_id": run_id,
        "batch_no": batch_no,
        "status": "completed",
        "steps": executed_steps,
        "error": None,
    }


def _execute_precheck_step(
    db_path: str, batch: Dict, step: Dict, step_result: Dict,
) -> None:
    items = _resolve_items_for_step(db_path, batch, step)
    if not items:
        step_result["status"] = "skipped"
        return

    evidence_dir = batch["evidence_dir"]
    if not os.path.isdir(evidence_dir):
        raise PlaybookStepError(f"证据目录不存在: {evidence_dir}")

    for item in items:
        status, actual_size, actual_sha256, issues = precheck_mod.precheck_item(
            evidence_dir, item
        )
        db.update_precheck_result(db_path, item["id"], actual_size, actual_sha256, status)
        step_result["affected_items"].append({
            "id": item["id"],
            "file_path": item["file_path"],
            "precheck_status": status,
        })


def _execute_review_step(
    db_path: str, batch: Dict, step: Dict, step_result: Dict,
    batch_no: str, operator: str,
) -> None:
    items = _resolve_items_for_step(db_path, batch, step)
    if not items:
        step_result["status"] = "skipped"
        return

    target_status = step.get("target_status", "signed")
    remark_template = step.get("remark_template", "")
    step_operator = step.get("operator", operator)

    for item in items:
        remark = _format_remark(remark_template, item, batch_no)
        log_id = db.review_item(
            db_path,
            batch_id=batch["id"],
            item_id=item["id"],
            new_status=target_status,
            remark=remark or None,
            operator=step_operator or None,
            action="review",
        )
        step_result["affected_items"].append({
            "id": item["id"],
            "file_path": item["file_path"],
            "log_id": log_id,
            "old_status": item["review_status"],
            "new_status": target_status,
        })


def _execute_undo_step(
    db_path: str, batch: Dict, step: Dict, step_result: Dict,
    operator: str,
) -> None:
    step_operator = step.get("operator", operator)

    undo_log = db.undo_last_review(db_path, batch["id"], step_operator or None)
    if undo_log is None:
        step_result["status"] = "skipped"
        return

    step_result["affected_items"].append({
        "item_id": undo_log["item_id"],
        "file_path": undo_log["file_path"],
        "reverted_from": undo_log["new_status"],
        "reverted_to": undo_log["prev_status"],
    })


def _execute_export_step(
    db_path: str, batch: Dict, step: Dict, step_result: Dict,
) -> None:
    from . import report as report_mod

    output_path = step.get("output_path")
    if not output_path:
        raise PlaybookStepError("export 步骤缺少 output_path")

    fmt = step.get("export_format", "auto")
    output_path = os.path.abspath(output_path)

    items = db.get_evidence_items(db_path, batch["id"])
    total_pc, passed, failed, unchecked = db.count_precheck(db_path, batch["id"])
    total_rv, signed, supplement, pending = db.count_reviewed(db_path, batch["id"])

    precheck_stats = {"total": total_pc, "passed": passed, "failed": failed, "unchecked": unchecked}
    review_stats = {"total": total_rv, "signed": signed, "supplement": supplement, "pending": pending}

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if fmt == "auto":
        try:
            fmt = report_mod.detect_format_by_ext(output_path)
        except ValueError as e:
            raise PlaybookStepError(str(e))

    if fmt == "csv":
        count = report_mod.export_csv(items, output_path, batch_info=batch)
    elif fmt == "json":
        from . import snapshot as snapshot_mod
        restore_trace = snapshot_mod.build_trace(db_path, batch["batch_no"])
        count = report_mod.export_json(
            items, output_path,
            batch_info=batch,
            precheck_stats=precheck_stats,
            review_stats=review_stats,
            restore_trace=restore_trace,
        )
    else:
        raise PlaybookStepError(f"不支持的导出格式: {fmt}")

    step_result["affected_items"].append({
        "output_path": output_path,
        "format": fmt,
        "count": count,
    })


def _rollback_executed_steps(
    db_path: str,
    executed_steps: List[Dict],
    batch_no: str,
    operator: str,
) -> None:
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        return

    for step_result in reversed(executed_steps):
        if step_result["type"] == "review" and step_result["status"] == "success":
            for _ in step_result.get("affected_items", []):
                db.undo_last_review(db_path, batch["id"], operator or None)

        elif step_result["type"] == "undo" and step_result["status"] == "success":
            for affected in step_result.get("affected_items", []):
                item_id = affected.get("item_id")
                if item_id:
                    item = db.get_evidence_item_by_id(db_path, item_id)
                    if item and item["review_status"] != affected.get("reverted_from", "pending"):
                        db.review_item(
                            db_path,
                            batch_id=batch["id"],
                            item_id=item_id,
                            new_status=affected["reverted_from"],
                            remark=None,
                            operator=operator or None,
                            action="review",
                        )


def _compute_batch_fingerprint(db_path: str, batch: Dict) -> str:
    items = db.get_evidence_items(db_path, batch["id"])
    review_stats = db.count_reviewed(db_path, batch["id"])
    return json.dumps({
        "batch_id": batch["id"],
        "updated_at": batch["updated_at"],
        "review_stats": list(review_stats),
        "item_count": len(items),
    }, sort_keys=True)


def get_playbook_history(db_path: str, batch_no: str, limit: int = 20) -> List[Dict]:
    return db.get_playbook_runs(db_path, batch_no=batch_no, limit=limit)


def get_playbook_run_detail(db_path: str, run_id: int) -> Optional[Dict]:
    return db.get_playbook_run_with_steps(db_path, run_id)


def parse_step_spec(spec: str) -> Dict:
    """
    解析单条步骤描述字符串。

    格式:
      precheck[:filter_status]
      review:target_status[:filter_status][:line_range_start-line_range_end][:remark_template]
      undo[:operator]
      export:output_path[:export_format]

    示例:
      "precheck"
      "precheck:failed_precheck"
      "review:signed"
      "review:signed:pending"
      "review:signed:pending:1-5"
      "review:signed:pending:1-5:批次{batch_no}签收"
      "undo"
      "undo:op1"
      "export:/tmp/out.json"
      "export:/tmp/out.json:json"
    """
    parts = spec.split(":")
    step_type = parts[0].strip().lower()

    if step_type not in PLAYBOOK_STEP_TYPES:
        raise PlaybookFormatError(f"不支持的步骤类型: '{step_type}'")

    entry: Dict = {"type": step_type}

    if step_type == "precheck":
        if len(parts) > 1 and parts[1].strip():
            entry["filter_status"] = parts[1].strip()

    elif step_type == "review":
        if len(parts) < 2 or not parts[1].strip():
            raise PlaybookFormatError("review 步骤必须指定 target_status，格式: review:signed 或 review:supplement")
        entry["target_status"] = parts[1].strip()
        if len(parts) > 2 and parts[2].strip():
            entry["filter_status"] = parts[2].strip()
        if len(parts) > 3 and parts[3].strip():
            lr_str = parts[3].strip()
            if "-" in lr_str:
                lr_parts = lr_str.split("-", 1)
                try:
                    entry["line_range"] = [int(lr_parts[0]), int(lr_parts[1])]
                except ValueError:
                    raise PlaybookFormatError(f"行号范围格式错误: '{lr_str}'，应为 start-end")
            else:
                raise PlaybookFormatError(f"行号范围格式错误: '{lr_str}'，应为 start-end")
        if len(parts) > 4 and parts[4].strip():
            entry["remark_template"] = parts[4].strip()

    elif step_type == "undo":
        if len(parts) > 1 and parts[1].strip():
            entry["operator"] = parts[1].strip()

    elif step_type == "export":
        if len(parts) < 2 or not parts[1].strip():
            raise PlaybookFormatError("export 步骤必须指定 output_path，格式: export:/path/to/file[:format]")
        rest = spec[len(parts[0]) + 1:]
        known_formats = ("json", "csv", "auto")
        fmt = None
        for f in known_formats:
            suffix = ":" + f
            if rest.endswith(suffix):
                fmt = f
                rest = rest[:-len(suffix)]
                break
        entry["output_path"] = rest.strip()
        if fmt:
            entry["export_format"] = fmt

    return entry


def generate_from_args(
    batch_no: str,
    step_specs: List[str],
    operator: str = "",
    description: str = "",
    output_file: str = "",
    filter_status: str = "",
    line_range: Optional[str] = None,
    remark_template: str = "",
) -> Dict:
    """
    从命令行步骤描述字符串生成剧本。

    step_specs 为 parse_step_spec 格式的字符串列表。
    全局 filter_status / line_range / remark_template 会合并到各步骤（步骤级优先）。
    """
    steps = []
    for spec in step_specs:
        step = parse_step_spec(spec)
        if filter_status and "filter_status" not in step:
            step["filter_status"] = filter_status
        if line_range and "line_range" not in step:
            lr_parts = line_range.split("-", 1)
            try:
                step["line_range"] = [int(lr_parts[0]), int(lr_parts[1])]
            except (ValueError, IndexError):
                raise PlaybookFormatError(f"全局行号范围格式错误: '{line_range}'")
        if remark_template and "remark_template" not in step and step["type"] == "review":
            step["remark_template"] = remark_template
        steps.append(step)

    return create_playbook(
        batch_no=batch_no,
        steps=steps,
        operator=operator,
        output_file=output_file,
        description=description,
    )


def generate_from_csv(
    batch_no: str,
    csv_path: str,
    operator: str = "",
    description: str = "",
    output_file: str = "",
) -> Dict:
    """
    从 CSV 模板文件生成剧本。

    CSV 格式（首行为表头）:
      type,filter_status,line_range,target_status,remark_template,operator,output_path,export_format
      precheck,pending,,,,,,
      review,pending,,signed,批次{batch_no}签收{file_path},,,
      export,all,,,,/tmp/out.json,json
    """
    if not os.path.isfile(csv_path):
        raise PlaybookError(f"CSV 模板文件不存在: {csv_path}")

    steps = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            step_type = row.get("type", "").strip().lower()
            if not step_type:
                continue
            if step_type not in PLAYBOOK_STEP_TYPES:
                raise PlaybookFormatError(f"CSV 第 {row_idx} 行: 不支持的类型 '{step_type}'")

            step: Dict = {"type": step_type}

            fs = row.get("filter_status", "").strip()
            if fs:
                step["filter_status"] = fs

            lr = row.get("line_range", "").strip()
            if lr:
                if "-" in lr:
                    lr_parts = lr.split("-", 1)
                    try:
                        step["line_range"] = [int(lr_parts[0]), int(lr_parts[1])]
                    except ValueError:
                        raise PlaybookFormatError(f"CSV 第 {row_idx} 行: 行号范围格式错误 '{lr}'")
                else:
                    raise PlaybookFormatError(f"CSV 第 {row_idx} 行: 行号范围格式错误 '{lr}'")

            if step_type == "review":
                ts = row.get("target_status", "").strip()
                if ts not in ("signed", "supplement"):
                    raise PlaybookFormatError(
                        f"CSV 第 {row_idx} 行: review 步骤必须指定 target_status (signed/supplement)"
                    )
                step["target_status"] = ts

            rt = row.get("remark_template", "").strip()
            if rt:
                step["remark_template"] = rt

            so = row.get("operator", "").strip()
            if so:
                step["operator"] = so

            if step_type == "export":
                op = row.get("output_path", "").strip()
                if not op:
                    raise PlaybookFormatError(f"CSV 第 {row_idx} 行: export 步骤必须指定 output_path")
                step["output_path"] = op
                ef = row.get("export_format", "").strip()
                if ef:
                    step["export_format"] = ef

            steps.append(step)

    if not steps:
        raise PlaybookFormatError("CSV 模板中没有有效的步骤定义")

    return create_playbook(
        batch_no=batch_no,
        steps=steps,
        operator=operator,
        output_file=output_file,
        description=description,
    )


def generate_from_last_run(
    db_path: str,
    batch_no: str,
    operator: str = "",
    description: str = "",
    output_file: str = "",
) -> Dict:
    """
    从最近一次批次操作记录生成剧本。

    扫描 review_logs 中最近的复核/撤销操作，按操作类型逆向构建步骤：
    - 连续的 review 操作合并为一个 review 步骤
    - undo 操作生成一个 undo 步骤
    如果最后一条记录是 undo，会在剧本中重建该 undo 步骤，方便"撤销后重放"。
    """
    batch = db.get_batch_by_no(db_path, batch_no)
    if not batch:
        raise PlaybookError(f"批次 '{batch_no}' 不存在")

    last_run = db.get_last_playbook_run(db_path, batch_no)
    if not last_run:
        raise PlaybookError(f"批次 '{batch_no}' 没有剧本执行记录，无法从最近操作生成")

    playbook_data = last_run.get("playbook_data")
    if isinstance(playbook_data, str):
        try:
            playbook_data = json.loads(playbook_data)
        except (json.JSONDecodeError, TypeError):
            raise PlaybookError(f"剧本运行 #{last_run['id']} 的 playbook_data 无法解析")

    if not playbook_data or "steps" not in playbook_data:
        raise PlaybookError(f"剧本运行 #{last_run['id']} 没有可用的步骤数据")

    steps = playbook_data["steps"]

    desc = description or f"从运行 #{last_run['id']} 重放的剧本"

    pb = create_playbook(
        batch_no=batch_no,
        steps=steps,
        operator=operator or playbook_data.get("operator", ""),
        output_file=output_file or playbook_data.get("output_file", ""),
        description=desc,
    )

    if last_run.get("status"):
        pb["replayed_from_run_id"] = last_run["id"]
        pb["replayed_from_status"] = last_run["status"]

    if output_file:
        for step in pb.get("steps", []):
            if step.get("type") == "export" and step.get("output_path"):
                step["output_path"] = output_file
                step["export_format"] = "auto"

    return pb


def check_playbook(
    db_path: str,
    playbook: Dict,
    name: Optional[str] = None,
) -> Dict:
    """
    剧本检查：比 preview 更全面的冲突检测，正式执行前调用。

    额外检查:
    - 同名剧本在库中已存在
    - 批次在剧本生成后被修改
    - 导出目录只读
    - 版本冲突
    - 最近一次运行结果
    """
    result = preview_playbook(db_path, playbook)

    result["check_warnings"] = []
    result["check_errors"] = []
    result["same_name_conflict"] = False
    result["batch_modified"] = False
    result["readonly_export_dir"] = False
    result["version_ok"] = True

    pb_version = playbook.get("version", "")
    if pb_version != PLAYBOOK_VERSION:
        result["version_ok"] = False
        result["check_errors"].append(
            f"剧本版本不兼容: 当前 {PLAYBOOK_VERSION}，剧本 {pb_version}"
        )
        result["can_execute"] = False

    if name and db.playbook_name_exists(db_path, name):
        existing = db.get_playbook_from_library(db_path, name)
        result["same_name_conflict"] = True
        if existing:
            detail = (
                f"同名剧本 '{name}' 已存在于库中（创建于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(existing['created_at']))}），"
                f"使用 --overwrite 覆盖"
            )
            result["check_warnings"].append(detail)

    batch_no = playbook["batch_no"]
    batch = db.get_batch_by_no(db_path, batch_no)
    if batch:
        pb_batch_updated_at = playbook.get("batch_updated_at")
        if pb_batch_updated_at is not None and batch["updated_at"] != pb_batch_updated_at:
            result["batch_modified"] = True
            result["check_warnings"].append(
                f"批次 '{batch_no}' 在剧本生成后被修改过，执行时需要 --force"
            )

    for step in playbook.get("steps", []):
        if step.get("type") == "export":
            output_path = step.get("output_path", "")
            if output_path:
                abs_path = os.path.abspath(output_path)
                parent = os.path.dirname(abs_path)
                if parent and os.path.isdir(parent) and not os.access(parent, os.W_OK):
                    result["readonly_export_dir"] = True
                    result["check_errors"].append(
                        f"导出目录只读: {parent}，无法写入 {output_path}"
                    )
                    result["can_execute"] = False
                elif parent and not os.path.exists(parent):
                    result["check_warnings"].append(
                        f"导出目录尚不存在: {parent}，执行时会自动创建"
                    )

    if name and db.playbook_name_exists(db_path, name):
        existing = db.get_playbook_from_library(db_path, name)
        if existing and existing.get("last_run_status"):
            result["check_warnings"].append(
                f"同名剧本上次运行状态: {existing['last_run_status']}"
            )

    return result


def save_to_library(
    db_path: str,
    name: str,
    playbook: Dict,
    overwrite: bool = False,
) -> int:
    return db.save_playbook_to_library(
        db_path,
        name=name,
        batch_no=playbook["batch_no"],
        playbook_data=playbook,
        description=playbook.get("description", ""),
        operator=playbook.get("operator", ""),
        output_file=playbook.get("output_file", ""),
        version=playbook.get("version", PLAYBOOK_VERSION),
        overwrite=overwrite,
    )


def load_from_library(db_path: str, name: str) -> Optional[Dict]:
    record = db.get_playbook_from_library(db_path, name)
    if not record:
        return None
    return record


def list_library(db_path: str, batch_no: Optional[str] = None) -> List[Dict]:
    return db.list_playbook_library(db_path, batch_no=batch_no)


def delete_from_library(db_path: str, name: str) -> bool:
    return db.delete_playbook_from_library(db_path, name)
