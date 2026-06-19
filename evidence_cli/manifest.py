"""Manifest 清单解析与导入"""

import csv
import os
from typing import List, Dict, Tuple


class ManifestParseError(Exception):
    """Manifest 解析错误"""

    def __init__(self, line_no: int, message: str):
        self.line_no = line_no
        self.message = message
        super().__init__(f"第 {line_no} 行: {message}")


class ManifestImportResult:
    """导入结果"""

    def __init__(self):
        self.success: int = 0
        self.errors: List[ManifestParseError] = []
        self.duplicates: List[Tuple[int, str]] = []
        self.items: List[Dict] = []


def parse_manifest(manifest_path: str) -> ManifestImportResult:
    """
    解析 manifest CSV 文件。

    支持的列：
    - file_path (必填): 相对证据目录的文件路径
    - size / file_size (可选): 文件大小（字节）
    - sha256 / hash (可选): SHA-256 哈希值

    返回解析结果，包含成功项、错误和重复项。
    """
    result = ManifestImportResult()

    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"清单文件不存在: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise ManifestParseError(1, "文件为空或缺少表头")

        headers = {h.lower().strip(): h for h in reader.fieldnames}

        if "file_path" not in headers:
            raise ManifestParseError(1, "缺少必填列 'file_path'")

        size_key = None
        for key in ("size", "file_size", "expected_size"):
            if key in headers:
                size_key = headers[key]
                break

        sha256_key = None
        for key in ("sha256", "hash", "sha256_hash"):
            if key in headers:
                sha256_key = headers[key]
                break

        seen_paths = {}

        for line_no, row in enumerate(reader, start=2):
            file_path = (row.get(headers["file_path"]) or "").strip()

            if not file_path:
                result.errors.append(
                    ManifestParseError(line_no, "file_path 不能为空")
                )
                continue

            if file_path in seen_paths:
                result.duplicates.append((line_no, file_path))
                result.errors.append(
                    ManifestParseError(
                        line_no, f"重复路径 '{file_path}'，首次出现在第 {seen_paths[file_path]} 行"
                    )
                )
                continue

            seen_paths[file_path] = line_no

            expected_size = None
            if size_key:
                size_str = (row.get(size_key) or "").strip()
                if size_str:
                    try:
                        expected_size = int(size_str)
                        if expected_size < 0:
                            result.errors.append(
                                ManifestParseError(line_no, f"大小不能为负数: {size_str}")
                            )
                            continue
                    except ValueError:
                        result.errors.append(
                            ManifestParseError(line_no, f"无效的大小值: {size_str}")
                        )
                        continue

            expected_sha256 = None
            if sha256_key:
                hash_val = (row.get(sha256_key) or "").strip().lower()
                if hash_val:
                    if len(hash_val) != 64:
                        result.errors.append(
                            ManifestParseError(
                                line_no,
                                f"SHA-256 哈希长度应为 64 字符，实际 {len(hash_val)} 字符",
                            )
                        )
                        continue
                    try:
                        int(hash_val, 16)
                    except ValueError:
                        result.errors.append(
                            ManifestParseError(line_no, f"无效的 SHA-256 哈希: {hash_val}")
                        )
                        continue
                    expected_sha256 = hash_val

            item = {
                "file_path": file_path,
                "expected_size": expected_size,
                "expected_sha256": expected_sha256,
                "manifest_line_no": line_no,
            }
            result.items.append(item)
            result.success += 1

    return result


def detect_manifest_format(manifest_path: str) -> str:
    """检测清单格式（目前仅支持 CSV）"""
    ext = os.path.splitext(manifest_path)[1].lower()
    if ext in (".csv", ".txt"):
        return "csv"
    raise ValueError(f"不支持的清单格式: {ext}")
