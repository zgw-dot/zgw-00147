"""
快照恢复 CLI 真实命令链测试

测试场景：
1. 普通恢复：创建批次 → 复核 → 快照 → 新目录预演 → 恢复 → 继续复核 → 撤销 → 导出
2. 冲突覆盖：恢复到已有批次目录 → 预演冲突 → 强制覆盖 → 核对差异
3. 跨工作目录：重映射证据目录恢复 → 核对数据一致性
4. 导出核对：恢复后导出 → 验证包含恢复摘要

所有操作通过子进程调用真实 CLI 命令，模拟用户真实操作流程。
"""

import os
import sys
import json
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


class TestSnapshotCLIChain(unittest.TestCase):
    """CLI 命令链集成测试"""

    def setUp(self):
        """创建临时工作目录"""
        self.base_dir = tempfile.mkdtemp(prefix="evi_cli_test_")
        self.evidence_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sample_evidence",
        )
        self.manifest_path = os.path.join(self.evidence_dir, "manifest_mixed.csv")

    def tearDown(self):
        """清理临时目录"""
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

    def test_01_normal_restore_chain(self):
        """测试1: 普通恢复完整流程"""
        print("\n" + "=" * 60)
        print("测试1: 普通恢复完整流程")
        print("=" * 60)

        work_a = os.path.join(self.base_dir, "work_a")
        work_b = os.path.join(self.base_dir, "work_b")
        os.makedirs(work_a)
        os.makedirs(work_b)

        print("\n[步骤1] 在 work_a 初始化并导入批次")
        run_cli(["init"], cwd=work_a)
        fixture = self._create_evidence_fixture(work_a)
        run_cli([
            "import",
            "-b", "batch_chain_01",
            "-m", fixture["manifest_path"],
            "-e", fixture["evidence_dir"],
            "-d", "命令链测试批次",
        ], cwd=work_a)

        print("\n[步骤2] 复核2条记录")
        run_cli(["list"], cwd=work_a)
        run_cli([
            "review",
            "-b", "batch_chain_01",
            "-i", "1",
            "-s", "signed",
            "-r", "CLI复核第一条",
            "-o", "cli_tester",
        ], cwd=work_a)
        run_cli([
            "review",
            "-b", "batch_chain_01",
            "-i", "2",
            "-s", "supplement",
            "-r", "CLI复核第二条",
            "-o", "cli_tester",
        ], cwd=work_a)

        print("\n[步骤3] 保存快照")
        snapshot_path = os.path.join(work_a, "snap_chain_01.json")
        run_cli([
            "snapshot", "save",
            "-b", "batch_chain_01",
            "-o", snapshot_path,
        ], cwd=work_a)
        self.assertTrue(os.path.exists(snapshot_path))

        print("\n[步骤4] 在 work_b 预演恢复（--dry-run）")
        run_cli(["init"], cwd=work_b)
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture["evidence_dir"],
            "--dry-run",
        ], cwd=work_b)
        self.assertIn("恢复预演", result.stdout)
        self.assertIn("清单文件映射", result.stdout)
        self.assertIn("证据目录映射", result.stdout)
        self.assertIn("预检统计", result.stdout)
        self.assertIn("复核统计", result.stdout)
        self.assertIn("最近一条操作记录", result.stdout)
        self.assertIn("可以恢复", result.stdout)

        print("\n[步骤5] 在 work_b 确认恢复")
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture["evidence_dir"],
        ], cwd=work_b)
        self.assertIn("恢复完成", result.stdout)
        self.assertIn("恢复来源", result.stdout)

        print("\n[步骤6] 验证 list 显示恢复标记")
        result = run_cli(["list"], cwd=work_b)
        self.assertIn("batch_chain_01", result.stdout)
        self.assertIn("[已恢复]", result.stdout)
        self.assertIn("恢复来源", result.stdout)

        print("\n[步骤7] 验证 resume 显示恢复摘要")
        result = run_cli([
            "resume",
            "-b", "batch_chain_01",
        ], cwd=work_b)
        self.assertIn("恢复来源", result.stdout)
        self.assertIn("恢复时间", result.stdout)
        self.assertIn("CLI复核第二条", result.stdout)

        print("\n[步骤8] 继续复核第3条")
        run_cli([
            "review",
            "-b", "batch_chain_01",
            "-i", "3",
            "-s", "signed",
            "-r", "恢复后新增复核",
            "-o", "new_operator",
        ], cwd=work_b)

        result = run_cli([
            "resume",
            "-b", "batch_chain_01",
            "-n", "5",
        ], cwd=work_b)
        self.assertIn("恢复后新增复核", result.stdout)

        print("\n[步骤9] 撤销上一条复核")
        result = run_cli([
            "undo",
            "-b", "batch_chain_01",
            "-o", "new_operator",
        ], cwd=work_b)
        self.assertIn("撤销成功", result.stdout)

        print("\n[步骤10] 导出报告，验证包含恢复摘要")
        export_path = os.path.join(work_b, "export_chain_01.json")
        run_cli([
            "export",
            "-b", "batch_chain_01",
            "-o", export_path,
            "-f", "json",
        ], cwd=work_b)

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)

        self.assertIn("restore", export_data["batch"])
        self.assertEqual(
            export_data["batch"]["restore"]["restored_from"],
            os.path.abspath(snapshot_path)
        )
        self.assertEqual(len(export_data["items"]), 3)

        print("\n[步骤11] 新开进程验证数据一致性")
        result = run_cli(["list"], cwd=work_b)
        self.assertIn("[已恢复]", result.stdout)

        result = run_cli([
            "resume",
            "-b", "batch_chain_01",
            "-n", "1",
        ], cwd=work_b)
        self.assertIn("撤销", result.stdout)

        print("\n[OK] 测试1通过")

    def test_02_force_restore_chain(self):
        """测试2: 冲突覆盖恢复流程"""
        print("\n" + "=" * 60)
        print("测试2: 冲突覆盖恢复流程")
        print("=" * 60)

        work_dir = os.path.join(self.base_dir, "work_force")
        os.makedirs(work_dir)

        print("\n[步骤1] 创建旧批次")
        run_cli(["init"], cwd=work_dir)
        fixture_old = self._create_evidence_fixture(os.path.join(work_dir, "old"))
        run_cli([
            "import",
            "-b", "batch_force",
            "-m", fixture_old["manifest_path"],
            "-e", fixture_old["evidence_dir"],
            "-d", "旧批次（会被覆盖）",
        ], cwd=work_dir)

        run_cli([
            "review",
            "-b", "batch_force",
            "-i", "1",
            "-s", "signed",
            "-r", "旧批次复核",
            "-o", "old_user",
        ], cwd=work_dir)

        run_cli([
            "review",
            "-b", "batch_force",
            "-i", "2",
            "-s", "signed",
            "-r", "旧批次复核2",
            "-o", "old_user",
        ], cwd=work_dir)

        old_result = run_cli([
            "resume",
            "-b", "batch_force",
        ], cwd=work_dir)
        self.assertIn("已签收: 2", old_result.stdout)

        print("\n[步骤2] 在独立目录创建新批次并快照")
        snap_work = os.path.join(self.base_dir, "snap_work")
        os.makedirs(snap_work)
        run_cli(["init"], cwd=snap_work)
        fixture_new = self._create_evidence_fixture(os.path.join(snap_work, "new"))
        run_cli([
            "import",
            "-b", "batch_force",
            "-m", fixture_new["manifest_path"],
            "-e", fixture_new["evidence_dir"],
            "-d", "新批次（覆盖用）",
        ], cwd=snap_work)

        run_cli([
            "review",
            "-b", "batch_force",
            "-i", "1",
            "-s", "supplement",
            "-r", "新批次复核",
            "-o", "new_user",
        ], cwd=snap_work)

        snapshot_path = os.path.join(work_dir, "snap_force.json")
        run_cli([
            "snapshot", "save",
            "-b", "batch_force",
            "-o", snapshot_path,
        ], cwd=snap_work)

        print("\n[步骤3] 不使用 --force 预演，应该显示冲突")
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture_new["evidence_dir"],
            "--dry-run",
        ], cwd=work_dir, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("冲突", result.stdout)
        self.assertIn("无法恢复", result.stdout)

        print("\n[步骤4] 使用 --force 预演，应该显示差异")
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture_new["evidence_dir"],
            "--force",
            "--dry-run",
        ], cwd=work_dir)
        self.assertIn("检测到同名批次，将使用 --force 覆盖", result.stdout)
        self.assertIn("覆盖差异", result.stdout)
        self.assertIn("复核统计变化", result.stdout)
        self.assertIn("已签收: 2 → 0", result.stdout)
        self.assertIn("待补件: 0 → 1", result.stdout)
        self.assertIn("可以恢复", result.stdout)

        print("\n[步骤5] 执行强制覆盖恢复")
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture_new["evidence_dir"],
            "--force",
        ], cwd=work_dir)
        self.assertIn("已覆盖原有批次", result.stdout)

        print("\n[步骤6] 验证恢复结果")
        result = run_cli([
            "resume",
            "-b", "batch_force",
        ], cwd=work_dir)
        self.assertIn("恢复来源", result.stdout)
        self.assertIn("恢复时间", result.stdout)
        self.assertIn("覆盖差异", result.stdout)
        self.assertIn("旧批次「旧批次（会被覆盖）」→ 新批次「新批次（覆盖用）」", result.stdout)
        self.assertIn("已签收 2 → 0", result.stdout)
        self.assertIn("已签收: 0", result.stdout)
        self.assertIn("待补件: 1", result.stdout)
        self.assertIn("新批次复核", result.stdout)

        print("\n[步骤7] 导出验证差异持久化")
        export_path = os.path.join(work_dir, "export_force.json")
        run_cli([
            "export",
            "-b", "batch_force",
            "-o", export_path,
            "-f", "json",
        ], cwd=work_dir)

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)

        self.assertIn("restore", export_data["batch"])
        self.assertIn("diff", export_data["batch"]["restore"])
        self.assertEqual(
            export_data["batch"]["restore"]["diff"]["old_batch"]["description"],
            "旧批次（会被覆盖）"
        )
        self.assertEqual(
            export_data["batch"]["restore"]["diff"]["new_batch"]["description"],
            "新批次（覆盖用）"
        )

        print("\n[OK] 测试2通过")

    def test_03_cross_workdir_restore(self):
        """测试3: 跨工作目录 + 重映射证据目录"""
        print("\n" + "=" * 60)
        print("测试3: 跨工作目录 + 重映射证据目录")
        print("=" * 60)

        src_dir = os.path.join(self.base_dir, "src")
        dst_dir = os.path.join(self.base_dir, "dst")
        mapped_dir = os.path.join(self.base_dir, "mapped_evidence")
        os.makedirs(src_dir)
        os.makedirs(dst_dir)

        print("\n[步骤1] 在源目录创建批次")
        run_cli(["init"], cwd=src_dir)
        fixture = self._create_evidence_fixture(src_dir)
        run_cli([
            "import",
            "-b", "batch_cross",
            "-m", fixture["manifest_path"],
            "-e", fixture["evidence_dir"],
            "-d", "跨目录测试批次",
        ], cwd=src_dir)

        run_cli([
            "review",
            "-b", "batch_cross",
            "-i", "1",
            "-s", "signed",
            "-r", "源目录复核",
            "-o", "src_user",
        ], cwd=src_dir)

        print("\n[步骤2] 复制证据到映射目录")
        shutil.copytree(fixture["evidence_dir"], mapped_dir)

        print("\n[步骤3] 保存快照")
        snapshot_path = os.path.join(src_dir, "snap_cross.json")
        run_cli([
            "snapshot", "save",
            "-b", "batch_cross",
            "-o", snapshot_path,
        ], cwd=src_dir)

        print("\n[步骤4] 删除源证据目录，模拟源不可用")
        shutil.rmtree(fixture["evidence_dir"])

        print("\n[步骤5] 不使用重映射预演，应该失败")
        run_cli(["init"], cwd=dst_dir)
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "--dry-run",
        ], cwd=dst_dir, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺失文件", result.stdout)
        self.assertIn("证据目录", result.stdout)

        print("\n[步骤6] 使用重映射证据目录预演")
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", mapped_dir,
            "--dry-run",
        ], cwd=dst_dir)
        self.assertIn("可以恢复", result.stdout)
        self.assertIn(mapped_dir, result.stdout)

        print("\n[步骤7] 使用重映射执行恢复")
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", mapped_dir,
        ], cwd=dst_dir)
        self.assertIn("证据目录(重映射)", result.stdout)
        self.assertIn(mapped_dir, result.stdout)

        print("\n[步骤8] 验证数据一致性")
        result = run_cli([
            "resume",
            "-b", "batch_cross",
        ], cwd=dst_dir)
        self.assertIn(mapped_dir, result.stdout)
        self.assertIn("已签收: 1", result.stdout)
        self.assertIn("源目录复核", result.stdout)

        print("\n[步骤9] 继续复核和撤销")
        run_cli([
            "review",
            "-b", "batch_cross",
            "-i", "2",
            "-s", "supplement",
            "-r", "重映射后复核",
            "-o", "dst_user",
        ], cwd=dst_dir)

        result = run_cli([
            "undo",
            "-b", "batch_cross",
            "-o", "dst_user",
        ], cwd=dst_dir)
        self.assertIn("撤销成功", result.stdout)

        print("\n[步骤10] 导出验证")
        export_path = os.path.join(dst_dir, "export_cross.json")
        run_cli([
            "export",
            "-b", "batch_cross",
            "-o", export_path,
            "-f", "json",
        ], cwd=dst_dir)

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)

        self.assertEqual(export_data["batch"]["evidence_dir"], os.path.abspath(mapped_dir))
        self.assertEqual(len(export_data["items"]), 3)
        self.assertEqual(
            export_data["statistics"]["review"]["signed"], 1
        )

        print("\n[OK] 测试3通过")

    def test_04_export_consistency_check(self):
        """测试4: 导出核对 - 预演统计 vs 恢复后导出统计"""
        print("\n" + "=" * 60)
        print("测试4: 导出核对 - 预演统计 vs 恢复后导出统计")
        print("=" * 60)

        work_a = os.path.join(self.base_dir, "work_check_a")
        work_b = os.path.join(self.base_dir, "work_check_b")
        os.makedirs(work_a)
        os.makedirs(work_b)

        print("\n[步骤1] 创建包含多种状态的批次")
        run_cli(["init"], cwd=work_a)
        fixture = self._create_evidence_fixture(work_a)
        run_cli([
            "import",
            "-b", "batch_check",
            "-m", fixture["manifest_path"],
            "-e", fixture["evidence_dir"],
            "-d", "统计核对批次",
        ], cwd=work_a)

        run_cli([
            "review",
            "-b", "batch_check",
            "-i", "1",
            "-s", "signed",
            "-r", "已签收",
            "-o", "checker",
        ], cwd=work_a)
        run_cli([
            "review",
            "-b", "batch_check",
            "-i", "2",
            "-s", "supplement",
            "-r", "待补件",
            "-o", "checker",
        ], cwd=work_a)

        print("\n[步骤2] 保存快照")
        snapshot_path = os.path.join(work_a, "snap_check.json")
        run_cli([
            "snapshot", "save",
            "-b", "batch_check",
            "-o", snapshot_path,
        ], cwd=work_a)

        print("\n[步骤3] 预演并提取预演统计")
        run_cli(["init"], cwd=work_b)
        result = run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture["evidence_dir"],
            "--dry-run",
        ], cwd=work_b)

        import re
        preview_signed = int(re.search(r"已签收:\s*(\d+)", result.stdout).group(1))
        preview_supplement = int(re.search(r"待补件:\s*(\d+)", result.stdout).group(1))
        preview_pending = int(re.search(r"待处理:\s*(\d+)", result.stdout).group(1))
        preview_total = preview_signed + preview_supplement + preview_pending

        print(f"预演统计: 总数={preview_total}, 已签收={preview_signed}, 待补件={preview_supplement}, 待处理={preview_pending}")

        print("\n[步骤4] 执行恢复")
        run_cli([
            "snapshot", "restore",
            "-s", snapshot_path,
            "-e", fixture["evidence_dir"],
        ], cwd=work_b)

        print("\n[步骤5] resume 核对统计")
        result = run_cli([
            "resume",
            "-b", "batch_check",
        ], cwd=work_b)

        resume_signed = int(re.search(r"已签收:\s*(\d+)", result.stdout).group(1))
        resume_supplement = int(re.search(r"待补件:\s*(\d+)", result.stdout).group(1))
        resume_pending = int(re.search(r"待处理:\s*(\d+)", result.stdout).group(1))
        resume_total = resume_signed + resume_supplement + resume_pending

        print(f"resume统计: 总数={resume_total}, 已签收={resume_signed}, 待补件={resume_supplement}, 待处理={resume_pending}")

        self.assertEqual(preview_signed, resume_signed)
        self.assertEqual(preview_supplement, resume_supplement)
        self.assertEqual(preview_pending, resume_pending)
        self.assertEqual(preview_total, resume_total)

        print("\n[步骤6] 导出核对统计")
        export_path = os.path.join(work_b, "export_check.json")
        run_cli([
            "export",
            "-b", "batch_check",
            "-o", export_path,
            "-f", "json",
        ], cwd=work_b)

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)

        export_review = export_data["statistics"]["review"]
        print(f"导出统计: {export_review}")

        self.assertEqual(export_review["signed"], preview_signed)
        self.assertEqual(export_review["supplement"], preview_supplement)
        self.assertEqual(export_review["pending"], preview_pending)
        self.assertEqual(export_review["total"], preview_total)

        print("\n[步骤7] list 核对进度")
        result = run_cli(["list"], cwd=work_b)
        self.assertIn(f"{preview_signed}/{preview_total}", result.stdout)

        print("\n[OK] 测试4通过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
