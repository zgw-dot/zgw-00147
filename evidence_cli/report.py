"""报告导出：CSV 和 JSON 格式"""

import csv
import json
import os
from typing import List, Dict


def export_csv(items: List[Dict], output_path: str, batch_info: Dict = None) -> int:
    """
    导出 CSV 报告。

    列：序号、文件路径、清单行号、预期大小、实际大小、预期SHA256、实际SHA256、
         预检状态、复核状态、复核备注
    """
    fieldnames = [
        "序号",
        "文件路径",
        "清单行号",
        "预期大小",
        "实际大小",
        "预期SHA256",
        "实际SHA256",
        "预检状态",
        "复核状态",
        "复核备注",
    ]

    status_map = {
        "passed": "通过",
        "failed": "失败",
        "unchecked": "未检查",
    }

    review_map = {
        "pending": "待处理",
        "signed": "已签收",
        "supplement": "待补件",
    }

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        if batch_info:
            writer = csv.writer(f)
            writer.writerow([f"批次号: {batch_info.get('batch_no', '')}"])
            writer.writerow([f"描述: {batch_info.get('description', '')}"])
            writer.writerow([])

        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for idx, item in enumerate(items, start=1):
            precheck = status_map.get(item.get("precheck_status", "unchecked"), item.get("precheck_status", ""))
            review = review_map.get(item.get("review_status", "pending"), item.get("review_status", ""))

            writer.writerow({
                "序号": idx,
                "文件路径": item.get("file_path", ""),
                "清单行号": item.get("manifest_line_no", ""),
                "预期大小": item.get("expected_size", "") if item.get("expected_size") is not None else "",
                "实际大小": item.get("actual_size", "") if item.get("actual_size") is not None else "",
                "预期SHA256": item.get("expected_sha256", "") or "",
                "实际SHA256": item.get("actual_sha256", "") or "",
                "预检状态": precheck,
                "复核状态": review,
                "复核备注": item.get("review_remark", "") or "",
            })

    return len(items)


def export_json(items: List[Dict], output_path: str, batch_info: Dict = None,
                review_stats: Dict = None, precheck_stats: Dict = None) -> int:
    """
    导出 JSON 报告。

    包含：批次信息、统计信息、证据项列表、复核历史摘要、恢复摘要（如果有）
    """
    import json as _json

    report = {
        "version": "1.0",
        "batch": {},
        "statistics": {},
        "items": [],
    }

    if batch_info:
        report["batch"] = {
            "batch_no": batch_info.get("batch_no", ""),
            "description": batch_info.get("description", ""),
            "manifest_path": batch_info.get("manifest_path", ""),
            "evidence_dir": batch_info.get("evidence_dir", ""),
            "created_at": batch_info.get("created_at", 0),
            "updated_at": batch_info.get("updated_at", 0),
        }
        if batch_info.get("restored_from"):
            restore_info = {
                "restored_from": batch_info.get("restored_from"),
                "restored_at": batch_info.get("restored_at"),
            }
            if batch_info.get("restore_diff"):
                try:
                    restore_info["diff"] = _json.loads(batch_info["restore_diff"])
                except (_json.JSONDecodeError, TypeError):
                    restore_info["diff"] = batch_info["restore_diff"]
            report["batch"]["restore"] = restore_info

    if precheck_stats:
        report["statistics"]["precheck"] = precheck_stats

    if review_stats:
        report["statistics"]["review"] = review_stats

    for item in items:
        report["items"].append({
            "id": item.get("id"),
            "file_path": item.get("file_path", ""),
            "manifest_line_no": item.get("manifest_line_no"),
            "expected_size": item.get("expected_size"),
            "actual_size": item.get("actual_size"),
            "expected_sha256": item.get("expected_sha256") or None,
            "actual_sha256": item.get("actual_sha256") or None,
            "precheck_status": item.get("precheck_status", "unchecked"),
            "review_status": item.get("review_status", "pending"),
            "review_remark": item.get("review_remark"),
            "reviewed_at": item.get("reviewed_at"),
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return len(items)


def detect_format_by_ext(output_path: str) -> str:
    """根据文件扩展名推断导出格式"""
    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".csv":
        return "csv"
    elif ext == ".json":
        return "json"
    else:
        raise ValueError(f"不支持的导出格式: {ext}，请使用 .csv 或 .json")
