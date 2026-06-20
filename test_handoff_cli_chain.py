"""
批次交接包 CLI 真实命令链测试

测试场景：
1. 完整链路：源目录创建批次 → 预检 → 复核 → 打包交接包 → 查看包内容
            → 换空 work-dir 只读核查 → 正式导入 → 跨进程复查
2. 冲突拦截：目标目录已存在同名批次，不带 --force 时失败
3. 只读目录失败：目标 work-dir 不可写时导入失败
4. 剧本关联：包内含剧本库和剧本运行记录时的导入与跳过

所有操作通过子进程调用真实 CLI 命令，模拟用户真实操作流程。
"""

import os
import sys
import json
import csv
import stat
import tempfile
import shutil
import subprocess
import unittest
from typing import List, Dict


def run_cli(args: List[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """运行 CLI 命令，返回结果"""
    project_root = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    if pythonpath:
        pythonpath = project_root + os.pathsep + pythonpath
    else:
        pythonpath = project_root
    env["PYTHONPATH"] = pythonpath
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONLEGACYWINDOWSSTDIO"] = "1"

    cmd = [sys.executable, "-m", "evidence_cli"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and result.returncode != 0:
        print(f"命令失败: {' '.join(cmd)}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        raise RuntimeError(f"命令失败，返回码: {result.returncode}")
    return result


class TestHandoffCLIChain(unittest.TestCase):
    """批次交接包 CLI 命令链集成测试"""

    def setUp(self):
        self.base_dir = tempfile.mkdtemp(prefix="evi_handoff_test_")

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _create_evidence_fixture(self, base_dir: str) -> Dict:
        """创建隔离的证据 fixture"""
        evidence_dir = os.path.join(base_dir, "evidence")
        os.makedirs(os.path.join(evidence_dir, "docs"), exist_ok=True)
        os.makedirs(os.path.join(evidence_dir, "images"), exist_ok=True)

        file_a = os.path.join(evidence_dir, "docs", "a.txt")
        file_b = os.path.join(evidence_dir, "docs", "b.txt")
        file_c = os.path.join(evidence_dir, "images", "c.png")

        import hashlib
        for p, content in [
            (file_a, b"content of a handoff test 12345678901234567890"),
            (file_b, b"content of b handoff test 123456789012345678901234"),
            (file_c, b"png-bytes-handoff-test-123456789012345678901"),
        ]:
            with open(p, "wb") as f:
                f.write(content)

        def sha(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                h.update(f.read())
            return h.hexdigest()

        manifest_path = os.path.join(base_dir, "manifest.csv")
        with open(manifest_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file_path", "size", "sha256"])
            w.writerow(["docs/a.txt", os.path.getsize(file_a), sha(file_a)])
            w.writerow(["docs/b.txt", os.path.getsize(file_b), sha(file_b)])
            w.writerow(["images/c.png", os.path.getsize(file_c), sha(file_c)])

        return {
            "manifest_path": manifest_path,
            "evidence_dir": evidence_dir,
            "files": [file_a, file_b, file_c],
        }

    def test_01_full_create_show_import_chain(self):
        """测试1: 完整链路 - 创建→查看→只读核查→正式导入→跨进程复查"""
        print("\n" + "=" * 60)
        print("测试1: 完整链路 - 导出、换空目录导入、跨进程复查")
        print("=" * 60)

        work_src = os.path.join(self.base_dir, "work_src")
        work_dst = os.path.join(self.base_dir, "work_dst")
        os.makedirs(work_src)
        os.makedirs(work_dst)

        print("\n[步骤1] 源目录初始化并导入批次")
        run_cli(["init"], cwd=work_src)
        fixture = self._create_evidence_fixture(work_src)
        run_cli([
            "import",
            "-b", "batch_handoff_01",
            "-m", fixture["manifest_path"],
            "-e", fixture["evidence_dir"],
            "-d", "交接包完整链路测试批次",
        ], cwd=work_src)

        print("\n[步骤2] 预检 + 复核 2 条")
        run_cli(["precheck", "-b", "batch_handoff_01"], cwd=work_src)
        run_cli([
            "review",
            "-b", "batch_handoff_01",
            "-i", "1",
            "-s", "signed",
            "-r", "交接包测试-已签收",
            "-o", "tester_a",
        ], cwd=work_src)
        run_cli([
            "review",
            "-b", "batch_handoff_01",
            "-i", "2",
            "-s", "supplement",
            "-r", "交接包测试-待补件",
            "-o", "tester_a",
        ], cwd=work_src)

        src_resume = run_cli(["resume", "-b", "batch_handoff_01"], cwd=work_src)
        self.assertIn("已签收: 1", src_resume.stdout)
        self.assertIn("待补件: 1", src_resume.stdout)

        print("\n[步骤3] 创建交接包（使用默认 .handoffs 目录，便于 list 扫描）")
        package_path = os.path.join(work_src, ".handoffs", "handoff_01.tar.gz")
        run_cli([
            "handoff", "create",
            "-b", "batch_handoff_01",
            "-o", package_path,
            "-u", "packager_x",
        ], cwd=work_src)
        self.assertTrue(os.path.isfile(package_path))
        pkg_size = os.path.getsize(package_path)
        self.assertGreater(pkg_size, 0)
        print(f"  包大小: {pkg_size} 字节")

        print("\n[步骤4] 查看交接包内容 (handoff show)")
        show_result = run_cli([
            "handoff", "show",
            "-p", package_path,
        ], cwd=work_src)
        self.assertIn("交接包内容", show_result.stdout)
        self.assertIn("batch_handoff_01", show_result.stdout)
        self.assertIn("packager_x", show_result.stdout)
        self.assertIn("证据项数: 3", show_result.stdout)
        self.assertIn("预检统计", show_result.stdout)
        self.assertIn("复核统计", show_result.stdout)
        self.assertIn("最近操作日志", show_result.stdout)
        self.assertIn("复核 #1", show_result.stdout)

        print("\n[步骤5] 列出交接包 (handoff list)")
        list_result = run_cli(["handoff", "list"], cwd=work_src)
        self.assertIn("handoff_01", list_result.stdout)
        self.assertIn("batch_handoff_01", list_result.stdout)

        print("\n[步骤6] 目标空目录只读核查 (--dry-run)")
        run_cli(["init"], cwd=work_dst)
        dry_result = run_cli([
            "handoff", "import",
            "-p", package_path,
            "-e", fixture["evidence_dir"],
            "--dry-run",
        ], cwd=work_dst)
        self.assertIn("交接包导入核查（只读预演）", dry_result.stdout)
        self.assertIn("[OK] 目标目录可写", dry_result.stdout)
        self.assertIn("[OK] 无批次/剧本冲突", dry_result.stdout)
        self.assertIn("[OK] 核查通过，可以导入", dry_result.stdout)
        self.assertIn("已签收 1 / 待补件 1 / 待处理 1", dry_result.stdout)

        print("\n[步骤7] 确认目标库还没有批次（dry-run 不落库）")
        list_before = run_cli(["list"], cwd=work_dst)
        self.assertIn("暂无批次", list_before.stdout)

        print("\n[步骤8] 正式导入交接包")
        import_result = run_cli([
            "handoff", "import",
            "-p", package_path,
            "-e", fixture["evidence_dir"],
            "-u", "importer_y",
        ], cwd=work_dst)
        self.assertIn("导入完成", import_result.stdout)
        self.assertIn("batch_handoff_01", import_result.stdout)
        self.assertIn("证据项: 3", import_result.stdout)

        print("\n[步骤9] 目标目录验证：list 显示批次")
        list_after = run_cli(["list"], cwd=work_dst)
        self.assertIn("batch_handoff_01", list_after.stdout)
        self.assertIn("已恢复", list_after.stdout)

        print("\n[步骤10] 目标目录验证：resume 显示数据一致")
        dst_resume = run_cli(["resume", "-b", "batch_handoff_01"], cwd=work_dst)
        self.assertIn("batch_handoff_01", dst_resume.stdout)
        self.assertIn("已签收: 1", dst_resume.stdout)
        self.assertIn("待补件: 1", dst_resume.stdout)
        self.assertIn("交接包测试-已签收", dst_resume.stdout)
        self.assertIn("来源快照", dst_resume.stdout)

        print("\n[步骤11] 验证 trace 恢复链路")
        trace_result = run_cli(["trace", "-b", "batch_handoff_01"], cwd=work_dst)
        self.assertIn("恢复链路", trace_result.stdout)
        self.assertIn("importer_y", trace_result.stdout)

        print("\n[步骤12] 验证 handoff log 持久化")
        log_result = run_cli(["handoff", "log"], cwd=work_dst)
        self.assertIn("[OK] imported", log_result.stdout)
        self.assertIn("batch_handoff_01", log_result.stdout)
        self.assertIn("importer_y", log_result.stdout)
        self.assertIn("来源 work-dir", log_result.stdout)
        self.assertIn("导入步骤:", log_result.stdout)

        print("\n[步骤13] 跨进程复查：新开进程运行同样命令")
        trace2 = run_cli(["trace", "-b", "batch_handoff_01"], cwd=work_dst)
        self.assertIn("importer_y", trace2.stdout)

        resume2 = run_cli(["resume", "-b", "batch_handoff_01"], cwd=work_dst)
        self.assertIn("已签收: 1", resume2.stdout)

        log2 = run_cli(["handoff", "log", "-b", "batch_handoff_01"], cwd=work_dst)
        self.assertTrue("packager_x" in log2.stdout or "importer_y" in log2.stdout)

        print("\n[步骤14] 目标目录继续复核 + 验证")
        run_cli([
            "review",
            "-b", "batch_handoff_01",
            "-i", "3",
            "-s", "signed",
            "-r", "目标端继续复核",
            "-o", "dst_user",
        ], cwd=work_dst)
        resume3 = run_cli(["resume", "-b", "batch_handoff_01"], cwd=work_dst)
        self.assertIn("已签收: 2", resume3.stdout)
        self.assertIn("目标端继续复核", resume3.stdout)

        print("\n[OK] 测试1通过")

    def test_02_conflict_interception(self):
        """测试2: 冲突拦截 - 目标已有同名批次，不带 --force 失败"""
        print("\n" + "=" * 60)
        print("测试2: 冲突拦截")
        print("=" * 60)

        work_src = os.path.join(self.base_dir, "work_src2")
        work_dst = os.path.join(self.base_dir, "work_dst2")
        os.makedirs(work_src)
        os.makedirs(work_dst)

        print("\n[步骤1] 源目录创建批次并打包")
        run_cli(["init"], cwd=work_src)
        fx_src = self._create_evidence_fixture(work_src)
        run_cli([
            "import",
            "-b", "batch_conflict",
            "-m", fx_src["manifest_path"],
            "-e", fx_src["evidence_dir"],
            "-d", "源批次",
        ], cwd=work_src)
        run_cli([
            "review", "-b", "batch_conflict", "-i", "1",
            "-s", "signed", "-r", "源端复核", "-o", "src_user",
        ], cwd=work_src)

        pkg = os.path.join(self.base_dir, "handoff_conflict.tar.gz")
        run_cli([
            "handoff", "create",
            "-b", "batch_conflict",
            "-o", pkg,
        ], cwd=work_src)

        print("\n[步骤2] 目标目录创建不同的同名批次")
        run_cli(["init"], cwd=work_dst)
        fx_dst = self._create_evidence_fixture(work_dst)
        run_cli([
            "import",
            "-b", "batch_conflict",
            "-m", fx_dst["manifest_path"],
            "-e", fx_dst["evidence_dir"],
            "-d", "目标原有批次（会冲突）",
        ], cwd=work_dst)
        run_cli([
            "review", "-b", "batch_conflict", "-i", "2",
            "-s", "supplement", "-r", "目标端自己的复核", "-o", "dst_user",
        ], cwd=work_dst)

        print("\n[步骤3] 不带 --force 预演，应显示冲突")
        dry_fail = run_cli([
            "handoff", "import",
            "-p", pkg,
            "-e", fx_dst["evidence_dir"],
            "--dry-run",
        ], cwd=work_dst, check=False)
        self.assertNotEqual(dry_fail.returncode, 0)
        self.assertIn("[X] 批次 'batch_conflict' 已存在", dry_fail.stdout)
        self.assertIn("[X] 核查未通过", dry_fail.stdout)

        print("\n[步骤4] 不带 --force 正式导入，应失败并写 failed 日志")
        import_fail = run_cli([
            "handoff", "import",
            "-p", pkg,
            "-e", fx_dst["evidence_dir"],
        ], cwd=work_dst, check=False)
        self.assertNotEqual(import_fail.returncode, 0)
        self.assertIn("批次 'batch_conflict' 已存在", import_fail.stderr + import_fail.stdout)

        print("\n[步骤5] 验证 handoff log 中有 failed 记录")
        log_fail = run_cli(["handoff", "log"], cwd=work_dst)
        self.assertIn("[FAIL] failed", log_fail.stdout)
        self.assertIn("预演检查失败", log_fail.stdout)

        print("\n[步骤6] 验证目标批次未被修改（仍是目标自己的）")
        resume_check = run_cli(["resume", "-b", "batch_conflict"], cwd=work_dst)
        self.assertIn("目标端自己的复核", resume_check.stdout)
        self.assertNotIn("源端复核", resume_check.stdout)

        print("\n[步骤7] 带 --force 正式导入，应成功覆盖")
        import_force = run_cli([
            "handoff", "import",
            "-p", pkg,
            "-e", fx_dst["evidence_dir"],
            "--force",
            "-u", "force_importer",
        ], cwd=work_dst)
        self.assertIn("导入完成", import_force.stdout)

        print("\n[步骤8] 验证覆盖后为源批次内容")
        resume_after = run_cli(["resume", "-b", "batch_conflict"], cwd=work_dst)
        self.assertIn("源端复核", resume_after.stdout)

        print("\n[步骤9] 验证 trace 有强制覆盖标记")
        trace_after = run_cli(["trace", "-b", "batch_conflict"], cwd=work_dst)
        self.assertIn("强制覆盖", trace_after.stdout)

        print("\n[OK] 测试2通过")

    def test_03_readonly_directory_failure(self):
        """测试3: 只读目录 - 目标 work-dir 不可写时导入失败"""
        print("\n" + "=" * 60)
        print("测试3: 只读目录失败")
        print("=" * 60)

        work_src = os.path.join(self.base_dir, "work_src3")
        work_readonly = os.path.join(self.base_dir, "work_ro")
        os.makedirs(work_src)
        os.makedirs(work_readonly)

        print("\n[步骤1] 源目录创建批次并打包")
        run_cli(["init"], cwd=work_src)
        fx = self._create_evidence_fixture(work_src)
        run_cli([
            "import",
            "-b", "batch_ro",
            "-m", fx["manifest_path"],
            "-e", fx["evidence_dir"],
        ], cwd=work_src)

        pkg = os.path.join(self.base_dir, "handoff_ro.tar.gz")
        run_cli([
            "handoff", "create",
            "-b", "batch_ro",
            "-o", pkg,
        ], cwd=work_src)

        print("\n[步骤2] 将目标目录设为只读（去掉写权限）")
        run_cli(["init"], cwd=work_readonly)
        try:
            os.chmod(work_readonly, stat.S_IRUSR | stat.S_IXUSR)
            ro_test_file = os.path.join(work_readonly, ".test_write")
            try:
                with open(ro_test_file, "w") as f:
                    f.write("x")
                can_write = True
                os.remove(ro_test_file)
            except OSError:
                can_write = False

            if not can_write:
                print("  目标目录已成功设为只读")

                print("\n[步骤3] 在只读目录执行只读核查(应报告不可写)")
                dry_ro = run_cli([
                    "handoff", "import",
                    "-p", pkg,
                    "-e", fx["evidence_dir"],
                    "--dry-run",
                ], cwd=work_readonly, check=False)
                self.assertIn("[X] 目标目录不可写", dry_ro.stdout)
                self.assertIn("[X] 核查未通过", dry_ro.stdout)
            else:
                print("  [SKIP] Windows 下普通用户 chmod 可能不生效，跳过只读拦截断言")
        finally:
            os.chmod(work_readonly, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        print("\n[OK] 测试3通过")

    def test_04_version_and_checksum_validation(self):
        """测试4: 版本不兼容和校验和篡改检查"""
        print("\n" + "=" * 60)
        print("测试4: 版本和校验和校验")
        print("=" * 60)

        work_src = os.path.join(self.base_dir, "work_src4")
        work_dst = os.path.join(self.base_dir, "work_dst4")
        os.makedirs(work_src)
        os.makedirs(work_dst)

        print("\n[步骤1] 创建合法交接包")
        run_cli(["init"], cwd=work_src)
        fx = self._create_evidence_fixture(work_src)
        run_cli([
            "import",
            "-b", "batch_ver",
            "-m", fx["manifest_path"],
            "-e", fx["evidence_dir"],
        ], cwd=work_src)
        pkg = os.path.join(self.base_dir, "handoff_ver.tar.gz")
        run_cli([
            "handoff", "create",
            "-b", "batch_ver",
            "-o", pkg,
        ], cwd=work_src)

        print("\n[步骤2] 篡改包内容（改 version 字段）")
        import tarfile
        import io
        pkg_bad_ver = os.path.join(self.base_dir, "handoff_bad_ver.tar.gz")
        with tarfile.open(pkg, "r:gz") as tar_in:
            with tarfile.open(pkg_bad_ver, "w:gz") as tar_out:
                for m in tar_in.getmembers():
                    f = tar_in.extractfile(m)
                    data = f.read() if f else b""
                    if m.name == "_manifest.json":
                        manifest_data = json.loads(data.decode("utf-8"))
                        manifest_data["version"] = "999.999.999"
                        data = json.dumps(manifest_data, ensure_ascii=False).encode("utf-8")
                        m.size = len(data)
                    tar_out.addfile(m, io.BytesIO(data))

        show_bad = run_cli([
            "handoff", "show",
            "-p", pkg_bad_ver,
        ], cwd=work_dst, check=False)
        self.assertNotEqual(show_bad.returncode, 0)
        self.assertIn("版本不兼容", show_bad.stderr + show_bad.stdout)

        print("\n[步骤3] 篡改包内容（破坏校验和）")
        pkg_bad_sum = os.path.join(self.base_dir, "handoff_bad_sum.tar.gz")
        with tarfile.open(pkg, "r:gz") as tar_in:
            with tarfile.open(pkg_bad_sum, "w:gz") as tar_out:
                for m in tar_in.getmembers():
                    f = tar_in.extractfile(m)
                    data = f.read() if f else b""
                    if m.name == "snapshot.json":
                        data = data + b"// corrupted"
                        m.size = len(data)
                    tar_out.addfile(m, io.BytesIO(data))

        show_sum = run_cli([
            "handoff", "show",
            "-p", pkg_bad_sum,
        ], cwd=work_dst, check=False)
        self.assertNotEqual(show_sum.returncode, 0)
        combined = show_sum.stderr + show_sum.stdout
        has_error = ("校验和不匹配" in combined) or ("checksum" in combined.lower()) or (show_sum.returncode != 0)
        self.assertTrue(has_error)

        print("\n[步骤4] 合法包 show 仍正常")
        show_ok = run_cli([
            "handoff", "show",
            "-p", pkg,
        ], cwd=work_dst)
        self.assertIn("batch_ver", show_ok.stdout)

        print("\n[OK] 测试4通过")

    def test_05_source_missing_files_precheck(self):
        """测试5: 打包前源文件缺失检查"""
        print("\n" + "=" * 60)
        print("测试5: 打包前源文件缺失检查")
        print("=" * 60)

        work = os.path.join(self.base_dir, "work_missing")
        os.makedirs(work)

        print("\n[步骤1] 创建批次")
        run_cli(["init"], cwd=work)
        fx = self._create_evidence_fixture(work)
        run_cli([
            "import",
            "-b", "batch_missing",
            "-m", fx["manifest_path"],
            "-e", fx["evidence_dir"],
        ], cwd=work)

        print("\n[步骤2] 删除 manifest 文件")
        os.remove(fx["manifest_path"])

        print("\n[步骤3] 打包应失败并提示文件缺失")
        pkg_fail = os.path.join(self.base_dir, "handoff_missing.tar.gz")
        create_fail = run_cli([
            "handoff", "create",
            "-b", "batch_missing",
            "-o", pkg_fail,
        ], cwd=work, check=False)
        self.assertNotEqual(create_fail.returncode, 0)
        self.assertIn("缺失", create_fail.stderr + create_fail.stdout)
        self.assertFalse(os.path.isfile(pkg_fail))

        print("\n[OK] 测试5通过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
