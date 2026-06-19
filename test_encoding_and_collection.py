"""
编码兼容性和测试收集范围验证

测试场景：
1. GBK 控制台编码下 trace 命令不崩溃（UnicodeEncodeError 回归）
2. pytest 仓库级收集范围正确（不误收集 manual_test.py 和其他非测试目录）
"""

import os
import sys
import json
import tempfile
import shutil
import subprocess
import unittest
from typing import List, Dict


def _run_cli_with_encoding(
    args: List[str],
    cwd: str,
    encoding: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """以指定编码环境运行 CLI 命令，返回 stdout/stderr 用 utf-8 容错解码的结果"""
    project_root = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = project_root
    env.pop("PYTHONLEGACYWINDOWSSTDIO", None)
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONIOENCODING"] = f"{encoding}:strict"

    cmd = [sys.executable, "-m", "evidence_cli"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=False,
        env=env,
    )

    decoded_stdout = result.stdout.decode("utf-8", errors="replace")
    decoded_stderr = result.stderr.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=decoded_stdout,
        stderr=decoded_stderr,
    )


def _create_test_env(base_dir: str) -> Dict:
    """创建测试环境：建批次 → 复核 → 快照 → 恢复"""
    src_dir = os.path.join(base_dir, "src")
    dst_dir = os.path.join(base_dir, "dst")
    os.makedirs(src_dir)
    os.makedirs(dst_dir)

    fixture = _create_evidence_fixture(src_dir)

    run_cli = lambda args, cwd: _run_cli_with_encoding(args, cwd, "utf-8")
    run_cli(["init"], src_dir)
    run_cli([
        "import", "-b", "enc_test",
        "-m", fixture["manifest_path"],
        "-e", fixture["evidence_dir"],
        "-d", "编码测试批次",
    ], src_dir)
    run_cli([
        "review", "-b", "enc_test", "-i", "1",
        "-s", "signed", "-r", "GBK测试复核", "-o", "tester",
    ], src_dir)

    snap_path = os.path.join(base_dir, "enc_snap.json")
    run_cli(["snapshot", "save", "-b", "enc_test", "-o", snap_path], src_dir)

    run_cli(["init"], dst_dir)
    run_cli([
        "snapshot", "restore", "-s", snap_path,
        "-e", fixture["evidence_dir"],
        "-o", "restore_op",
    ], dst_dir)

    run_cli([
        "review", "-b", "enc_test", "-i", "2",
        "-s", "supplement", "-r", "恢复后追加", "-o", "post_op",
    ], dst_dir)

    return {"dst_dir": dst_dir, "snap_path": snap_path, "fixture": fixture}


def _create_evidence_fixture(base_dir: str) -> Dict:
    """创建证据 fixture（与其他测试保持一致）"""
    evidence_dir = os.path.join(base_dir, "evidence")
    os.makedirs(os.path.join(evidence_dir, "docs"), exist_ok=True)
    os.makedirs(os.path.join(evidence_dir, "images"), exist_ok=True)

    file_a = os.path.join(evidence_dir, "docs", "a.txt")
    file_b = os.path.join(evidence_dir, "docs", "b.txt")
    file_c = os.path.join(evidence_dir, "images", "c.png")

    import hashlib
    for p, content in [(file_a, b"content of a 1234567890123456789012345678"),
                       (file_b, b"content of b 12345678901234567890123456789012"),
                       (file_c, b"png-bytes-here-1234567890123456789012345678901")]:
        with open(p, "wb") as f:
            f.write(content)

    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            h.update(f.read())
        return h.hexdigest()

    manifest_path = os.path.join(base_dir, "manifest.csv")
    import csv
    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file_path", "size", "sha256"])
        w.writerow(["docs/a.txt", os.path.getsize(file_a), sha(file_a)])
        w.writerow(["docs/b.txt", os.path.getsize(file_b), sha(file_b)])
        w.writerow(["images/c.png", os.path.getsize(file_c), sha(file_c)])

    return {
        "manifest_path": manifest_path,
        "evidence_dir": evidence_dir,
    }


class TestEncodingAndCollection(unittest.TestCase):
    """编码兼容性和测试收集范围验证"""

    def setUp(self):
        self.base_dir = tempfile.mkdtemp(prefix="enc_test_")

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def test_trace_command_no_gbk_crash(self):
        """GBK 编码控制台环境下 trace/list/resume/export 不崩溃，信息可读"""
        env = _create_test_env(self.base_dir)
        dst_dir = env["dst_dir"]
        snap_basename = os.path.basename(env["snap_path"])

        for encoding in ["gbk", "gb2312", "cp936"]:
            with self.subTest(encoding=encoding):
                result = _run_cli_with_encoding(
                    ["trace", "-b", "enc_test"],
                    dst_dir,
                    encoding=encoding,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"trace 在 {encoding} 下崩溃: {result.stderr}"
                )
                self.assertIn("[OK]", result.stdout)
                self.assertIn("enc_test", result.stdout)
                self.assertIn(snap_basename, result.stdout)
                self.assertIn("#1", result.stdout)
                self.assertIn("[!]", result.stdout)

                result_list = _run_cli_with_encoding(
                    ["list"], dst_dir, encoding=encoding, check=False
                )
                self.assertEqual(
                    result_list.returncode, 0,
                    f"list 在 {encoding} 下崩溃: {result_list.stderr}"
                )
                self.assertIn("enc_test", result_list.stdout)
                self.assertIn(snap_basename, result_list.stdout)
                self.assertIn("[!", result_list.stdout)

                result_resume = _run_cli_with_encoding(
                    ["resume", "-b", "enc_test"],
                    dst_dir, encoding=encoding, check=False
                )
                self.assertEqual(
                    result_resume.returncode, 0,
                    f"resume 在 {encoding} 下崩溃: {result_resume.stderr}"
                )
                self.assertIn("enc_test", result_resume.stdout)
                self.assertIn(snap_basename, result_resume.stdout)
                self.assertIn("trace", result_resume.stdout.lower())

                export_path = os.path.join(dst_dir, f"export_{encoding}.json")
                result_export = _run_cli_with_encoding(
                    ["export", "-b", "enc_test", "-o", export_path, "-f", "json"],
                    dst_dir, encoding=encoding, check=False
                )
                self.assertEqual(
                    result_export.returncode, 0,
                    f"export 在 {encoding} 下崩溃: {result_export.stderr}"
                )
                self.assertTrue(os.path.exists(export_path))
                with open(export_path, "r", encoding="utf-8") as f:
                    export_data = json.load(f)
                self.assertIn("restore_trace", export_data)
                self.assertEqual(export_data["restore_trace"]["event_count"], 1)

    def test_trace_with_unicode_chars_gbk_no_crash(self):
        """强制触发含 Unicode 字符的输出路径（丢失快照、链路断档），GBK 下不崩"""
        env = _create_test_env(self.base_dir)
        dst_dir = env["dst_dir"]

        os.remove(env["snap_path"])

        result = _run_cli_with_encoding(
            ["trace", "-b", "enc_test"],
            dst_dir, encoding="gbk", check=False
        )
        self.assertEqual(
            result.returncode, 0,
            f"快照丢失场景 trace 在 GBK 下崩溃: {result.stderr}"
        )
        self.assertIn("[MISSING]", result.stdout)
        self.assertIn("[!]", result.stdout)
        self.assertIn("enc_snap.json", result.stdout)
        self.assertIn("#1", result.stdout)

    def test_pytest_collection_scope(self):
        """仓库级 pytest 收集验证：只收集 test_*.py，共 58+ 个测试，不误收集 manual_test"""
        project_root = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
        env = os.environ.copy()
        env["PYTHONPATH"] = project_root

        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.assertEqual(result.returncode, 0, f"收集失败: {result.stderr}")

        collected_lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        test_lines = [l for l in collected_lines if "::" in l and not l.startswith("collected")]
        summary_line = [l for l in collected_lines if "tests collected" in l or "test collected" in l]

        self.assertGreater(len(summary_line), 0, "未找到收集统计行")
        summary_match = summary_line[0]
        import re
        count_match = re.search(r"(\d+)\s+tests?", summary_match)
        self.assertIsNotNone(count_match, f"无法解析收集数: {summary_match}")
        total_collected = int(count_match.group(1))

        self.assertGreaterEqual(
            total_collected, 58,
            f"收集测试数不足 58 (实际: {total_collected})，可能 pytest 配置未生效"
        )

        for l in test_lines:
            self.assertNotIn(
                "manual_test", l,
                f"pytest 误收集了 manual_test.py: {l}"
            )
            self.assertNotIn(
                ".review", l,
                f"pytest 误收集了 .review_* 目录内容: {l}"
            )
            self.assertNotIn(
                "evidence_cli/", l,
                f"pytest 误收集了 evidence_cli 包内容: {l}"
            )
            self.assertTrue(
                l.startswith("test_"),
                f"收集的测试不在 test_*.py 中: {l}"
            )

    def test_review_undo_export_after_restore_gbk_no_crash(self):
        """恢复后继续 review、undo、export 在 GBK 下可读且功能正常"""
        env = _create_test_env(self.base_dir)
        dst_dir = env["dst_dir"]

        result_review = _run_cli_with_encoding(
            ["review", "-b", "enc_test", "-i", "3",
             "-s", "signed", "-r", "GBK下复核", "-o", "gbk_user"],
            dst_dir, encoding="gbk", check=False
        )
        self.assertEqual(
            result_review.returncode, 0,
            f"review 在 GBK 下崩溃: {result_review.stderr}"
        )
        self.assertIn("#3", result_review.stdout)
        self.assertIn("images/c.png", result_review.stdout)
        self.assertIn("gbk_user", result_review.stdout)

        result_resume = _run_cli_with_encoding(
            ["resume", "-b", "enc_test", "-n", "5"],
            dst_dir, encoding="gbk", check=False
        )
        self.assertEqual(result_resume.returncode, 0)
        self.assertIn("enc_test", result_resume.stdout)
        self.assertIn("images/c.png", result_resume.stdout)
        self.assertIn("trace", result_resume.stdout.lower())

        result_undo = _run_cli_with_encoding(
            ["undo", "-b", "enc_test", "-o", "gbk_undoer"],
            dst_dir, encoding="gbk", check=False
        )
        self.assertEqual(
            result_undo.returncode, 0,
            f"undo 在 GBK 下崩溃: {result_undo.stderr}"
        )
        self.assertIn("images/c.png", result_undo.stdout)
        self.assertIn("#3", result_undo.stdout)

        export_path = os.path.join(dst_dir, "final_export.json")
        result_export = _run_cli_with_encoding(
            ["export", "-b", "enc_test", "-o", export_path, "-f", "json"],
            dst_dir, encoding="gbk", check=False
        )
        self.assertEqual(result_export.returncode, 0)
        with open(export_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["statistics"]["review"]["signed"], 1)
        self.assertIn("restore_trace", data)
        self.assertTrue(data["restore_trace"]["modified_after_restore"])
        self.assertGreaterEqual(len(data["restore_trace"]["post_restore_activity"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
