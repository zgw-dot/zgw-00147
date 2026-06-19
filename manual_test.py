"""
手动测试脚本 - 验证快照恢复预演功能
测试场景：
1. 普通恢复完整流程
2. 冲突覆盖恢复
3. 跨工作目录恢复 + 重映射证据目录
4. 导出核对
"""
import os
import sys
import json
import hashlib
import shutil
import tempfile
import subprocess


def run_cli(args, cwd, check=True):
    """运行 CLI 命令"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONIOENCODING"] = "utf-8"
    
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
        print(f"STDOUT:\n{result.stdout}")
        print(f"STDERR:\n{result.stderr}")
        raise RuntimeError(f"命令失败，返回码: {result.returncode}")
    return result


def create_fixture(base_dir):
    """创建证据 fixture"""
    evidence_dir = os.path.join(base_dir, "evidence")
    os.makedirs(os.path.join(evidence_dir, "docs"), exist_ok=True)
    os.makedirs(os.path.join(evidence_dir, "images"), exist_ok=True)

    files = [
        ("docs/a.txt", b"content of a 1234567890123456789012345678"),
        ("docs/b.txt", b"content of b 12345678901234567890123456789012"),
        ("images/c.png", b"png-bytes-here-1234567890123456789012345678901"),
    ]
    
    for rel_path, content in files:
        full_path = os.path.join(evidence_dir, rel_path)
        with open(full_path, "wb") as f:
            f.write(content)

    manifest_path = os.path.join(base_dir, "manifest.csv")
    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        f.write("file_path,size,sha256\n")
        for rel_path, content in files:
            full_path = os.path.join(evidence_dir, rel_path)
            sha = hashlib.sha256(content).hexdigest()
            f.write(f"{rel_path},{len(content)},{sha}\n")
    
    return {
        "manifest_path": manifest_path,
        "evidence_dir": evidence_dir,
    }


def test_normal_restore(base_dir):
    """测试1: 普通恢复完整流程"""
    print("\n" + "=" * 60)
    print("测试1: 普通恢复完整流程")
    print("=" * 60)
    
    work_a = os.path.join(base_dir, "work_a")
    work_b = os.path.join(base_dir, "work_b")
    os.makedirs(work_a)
    os.makedirs(work_b)
    
    print("\n[步骤1] 在 work_a 初始化并导入批次")
    run_cli(["init"], cwd=work_a)
    fixture = create_fixture(work_a)
    run_cli([
        "import",
        "-b", "batch_manual_01",
        "-m", fixture["manifest_path"],
        "-e", fixture["evidence_dir"],
        "-d", "手动测试批次",
    ], cwd=work_a)
    
    print("\n[步骤2] 复核2条记录")
    run_cli([
        "review",
        "-b", "batch_manual_01",
        "-i", "1",
        "-s", "signed",
        "-r", "手动复核第一条",
        "-o", "manual_tester",
    ], cwd=work_a)
    run_cli([
        "review",
        "-b", "batch_manual_01",
        "-i", "2",
        "-s", "supplement",
        "-r", "手动复核第二条",
        "-o", "manual_tester",
    ], cwd=work_a)
    
    print("\n[步骤3] 保存快照")
    snapshot_path = os.path.join(work_a, "snap_manual_01.json")
    run_cli([
        "snapshot", "save",
        "-b", "batch_manual_01",
        "-o", snapshot_path,
    ], cwd=work_a)
    
    print("\n[步骤4] 在 work_b 预演恢复（--dry-run）")
    run_cli(["init"], cwd=work_b)
    result = run_cli([
        "snapshot", "restore",
        "-s", snapshot_path,
        "-e", fixture["evidence_dir"],
        "--dry-run",
    ], cwd=work_b)
    print("预演输出预览:")
    for line in result.stdout.split("\n")[:15]:
        print(f"  {line}")
    assert "恢复预演" in result.stdout
    assert "[OK] 可以恢复" in result.stdout
    print("  ...预演成功")
    
    print("\n[步骤5] 在 work_b 确认恢复")
    result = run_cli([
        "snapshot", "restore",
        "-s", snapshot_path,
        "-e", fixture["evidence_dir"],
    ], cwd=work_b)
    assert "恢复完成" in result.stdout
    assert "恢复来源" in result.stdout
    print("  恢复成功")
    
    print("\n[步骤6] 验证 resume 显示恢复摘要")
    result = run_cli([
        "resume",
        "-b", "batch_manual_01",
    ], cwd=work_b)
    assert "恢复来源" in result.stdout
    assert "恢复时间" in result.stdout
    assert "手动复核第二条" in result.stdout
    print("  resume 显示恢复摘要成功")
    
    print("\n[步骤7] 继续复核第3条")
    run_cli([
        "review",
        "-b", "batch_manual_01",
        "-i", "3",
        "-s", "signed",
        "-r", "恢复后新增复核",
        "-o", "new_op",
    ], cwd=work_b)
    
    print("\n[步骤8] 撤销上一条复核")
    result = run_cli([
        "undo",
        "-b", "batch_manual_01",
        "-o", "new_op",
    ], cwd=work_b)
    assert "撤销成功" in result.stdout
    print("  撤销成功")
    
    print("\n[步骤9] 导出报告，验证包含恢复摘要")
    export_path = os.path.join(work_b, "export_manual_01.json")
    run_cli([
        "export",
        "-b", "batch_manual_01",
        "-o", export_path,
        "-f", "json",
    ], cwd=work_b)
    
    with open(export_path, "r", encoding="utf-8") as f:
        export_data = json.load(f)
    
    assert "restore" in export_data["batch"]
    assert export_data["batch"]["restore"]["restored_from"] == os.path.abspath(snapshot_path)
    assert len(export_data["items"]) == 3
    print("  导出包含恢复摘要成功")
    
    print("\n[步骤10] 新开进程验证数据一致性")
    result = run_cli(["list"], cwd=work_b)
    assert "[已恢复]" in result.stdout
    print("  新进程查询数据一致")
    
    print("\n[OK] 测试1通过")


def test_force_restore(base_dir):
    """测试2: 冲突覆盖恢复流程"""
    print("\n" + "=" * 60)
    print("测试2: 冲突覆盖恢复流程")
    print("=" * 60)
    
    work_dir = os.path.join(base_dir, "work_force")
    os.makedirs(work_dir)
    
    print("\n[步骤1] 创建旧批次")
    run_cli(["init"], cwd=work_dir)
    fixture_old = create_fixture(os.path.join(work_dir, "old"))
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
    
    print("\n[步骤2] 在独立目录创建新批次并快照")
    snap_work = os.path.join(base_dir, "snap_work")
    os.makedirs(snap_work)
    run_cli(["init"], cwd=snap_work)
    fixture_new = create_fixture(os.path.join(snap_work, "new"))
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
    assert result.returncode != 0
    assert "冲突" in result.stdout
    assert "无法恢复" in result.stdout
    print("  冲突检测成功")
    
    print("\n[步骤4] 使用 --force 预演，应该显示差异")
    result = run_cli([
        "snapshot", "restore",
        "-s", snapshot_path,
        "-e", fixture_new["evidence_dir"],
        "--force",
        "--dry-run",
    ], cwd=work_dir)
    assert "检测到同名批次，将使用 --force 覆盖" in result.stdout
    assert "覆盖差异" in result.stdout
    assert "已签收 2 → 0" in result.stdout
    print("  差异计算成功")
    
    print("\n[步骤5] 执行强制覆盖恢复")
    result = run_cli([
        "snapshot", "restore",
        "-s", snapshot_path,
        "-e", fixture_new["evidence_dir"],
        "--force",
    ], cwd=work_dir)
    assert "已覆盖原有批次" in result.stdout
    print("  强制覆盖恢复成功")
    
    print("\n[步骤6] 验证恢复结果")
    result = run_cli([
        "resume",
        "-b", "batch_force",
    ], cwd=work_dir)
    assert "恢复来源" in result.stdout
    assert "覆盖差异" in result.stdout
    assert "旧批次「旧批次（会被覆盖）」→ 新批次「新批次（覆盖用）」" in result.stdout
    assert "已签收 2 → 0" in result.stdout
    print("  恢复结果验证成功")
    
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
    
    assert "restore" in export_data["batch"]
    assert "diff" in export_data["batch"]["restore"]
    assert export_data["batch"]["restore"]["diff"]["old_batch"]["description"] == "旧批次（会被覆盖）"
    assert export_data["batch"]["restore"]["diff"]["new_batch"]["description"] == "新批次（覆盖用）"
    print("  导出差异验证成功")
    
    print("\n[OK] 测试2通过")


def main():
    base_dir = tempfile.mkdtemp(prefix="evi_manual_test_")
    print(f"测试根目录: {base_dir}")
    
    try:
        test_normal_restore(base_dir)
        test_force_restore(base_dir)
        
        print("\n" + "=" * 60)
        print("[OK] 所有手动测试通过！")
        print("=" * 60)
    finally:
        print(f"\n测试目录保留: {base_dir}")
        print("如需清理，请手动删除该目录")


if __name__ == "__main__":
    main()
