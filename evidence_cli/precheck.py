"""完整性预检：路径存在性、文件大小、SHA-256 哈希校验"""

import os
import hashlib
from typing import Dict, List, Tuple


def compute_sha256(file_path: str, chunk_size: int = 65536) -> str:
    """计算文件的 SHA-256 哈希值。只读不改。"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def get_file_size(file_path: str) -> int:
    """获取文件大小（字节）"""
    return os.path.getsize(file_path)


class PrecheckIssue:
    """单个预检问题"""

    def __init__(self, item_id: int, file_path: str, manifest_line_no: int,
                 issue_type: str, detail: str):
        self.item_id = item_id
        self.file_path = file_path
        self.manifest_line_no = manifest_line_no
        self.issue_type = issue_type
        self.detail = detail

    def __str__(self) -> str:
        return f"第{self.manifest_line_no}行 [{self.issue_type}] {self.file_path}: {self.detail}"


def precheck_item(evidence_dir: str, item: Dict) -> Tuple[str, int, str, List[PrecheckIssue]]:
    """
    对单个证据项执行预检。

    返回: (状态, 实际大小, 实际sha256, 问题列表)
    状态: passed | failed
    """
    issues = []
    full_path = os.path.join(evidence_dir, item["file_path"])
    actual_size = None
    actual_sha256 = None

    if not os.path.exists(full_path):
        issues.append(PrecheckIssue(
            item_id=item["id"],
            file_path=item["file_path"],
            manifest_line_no=item["manifest_line_no"],
            issue_type="文件缺失",
            detail="文件不存在",
        ))
        return "failed", 0, "", issues

    if not os.path.isfile(full_path):
        issues.append(PrecheckIssue(
            item_id=item["id"],
            file_path=item["file_path"],
            manifest_line_no=item["manifest_line_no"],
            issue_type="非文件",
            detail="路径指向的不是文件",
        ))
        return "failed", 0, "", issues

    try:
        actual_size = get_file_size(full_path)
    except OSError as e:
        issues.append(PrecheckIssue(
            item_id=item["id"],
            file_path=item["file_path"],
            manifest_line_no=item["manifest_line_no"],
            issue_type="读取失败",
            detail=f"无法读取文件大小: {e}",
        ))
        return "failed", 0, "", issues

    if item.get("expected_size") is not None:
        if actual_size != item["expected_size"]:
            issues.append(PrecheckIssue(
                item_id=item["id"],
                file_path=item["file_path"],
                manifest_line_no=item["manifest_line_no"],
                issue_type="大小不符",
                detail=f"预期 {item['expected_size']} 字节，实际 {actual_size} 字节",
            ))

    if item.get("expected_sha256"):
        try:
            actual_sha256 = compute_sha256(full_path)
        except OSError as e:
            issues.append(PrecheckIssue(
                item_id=item["id"],
                file_path=item["file_path"],
                manifest_line_no=item["manifest_line_no"],
                issue_type="读取失败",
                detail=f"无法计算哈希: {e}",
            ))
            return "failed", actual_size, "", issues

        if actual_sha256 != item["expected_sha256"]:
            issues.append(PrecheckIssue(
                item_id=item["id"],
                file_path=item["file_path"],
                manifest_line_no=item["manifest_line_no"],
                issue_type="哈希不符",
                detail=f"预期 {item['expected_sha256']}，实际 {actual_sha256}",
            ))

    if not issues:
        status = "passed"
    else:
        status = "failed"

    return status, actual_size, actual_sha256 or "", issues


def precheck_all(evidence_dir: str, items: List[Dict]) -> Tuple[List[Dict], List[PrecheckIssue]]:
    """
    对所有证据项执行预检。

    返回: (更新后的项列表, 所有问题列表)
    更新后的项包含 actual_size、actual_sha256、precheck_status 字段
    """
    all_issues = []
    updated_items = []

    for item in items:
        status, actual_size, actual_sha256, issues = precheck_item(evidence_dir, item)
        updated = dict(item)
        updated["precheck_status"] = status
        updated["actual_size"] = actual_size
        updated["actual_sha256"] = actual_sha256
        updated_items.append(updated)
        all_issues.extend(issues)

    return updated_items, all_issues
