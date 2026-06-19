"""批次快照功能测试"""

import os
import sys
import csv
import json
import tempfile
import shutil
import unittest
from typing import List, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evidence_cli import db, snapshot as snapshot_mod
from evidence_cli import manifest as manifest_mod


class TestSnapshot(unittest.TestCase):
    """快照功能测试"""

    def setUp(self):
        """每个测试前创建临时工作目录"""
        self.work_dir = tempfile.mkdtemp(prefix="evi_test_")
        self.db_path = db.get_db_path(self.work_dir)
        db.init_db(self.db_path)

        self.evidence_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sample_evidence",
        )
        self.manifest_path = os.path.join(self.evidence_dir, "manifest_mixed.csv")

    def tearDown(self):
        """每个测试后清理临时目录"""
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _import_batch(self, batch_no="test_batch", description="测试批次"):
        """导入测试批次"""
        result = manifest_mod.parse_manifest(self.manifest_path)
        valid_items = [item for item in result.items if item["manifest_line_no"] in [2, 3, 4]]
        batch_id, count = db.replace_batch(
            self.db_path,
            batch_no=batch_no,
            manifest_path=self.manifest_path,
            evidence_dir=self.evidence_dir,
            items=valid_items,
            description=description,
        )
        return batch_id, count

    def _review_some_items(self, batch_id: int):
        """复核一些条目，生成一些历史记录"""
        items = db.get_evidence_items(self.db_path, batch_id)
        log_ids = []
        for i, item in enumerate(items[:2]):
            status = "signed" if i == 0 else "supplement"
            log_id = db.review_item(
                self.db_path,
                batch_id=batch_id,
                item_id=item["id"],
                new_status=status,
                remark=f"测试备注{i + 1}",
                operator="tester",
                action="review",
            )
            log_ids.append(log_id)
        return log_ids

    def _create_isolated_fixture(self, base_dir: str) -> Tuple[str, str, List[Dict]]:
        """
        在 base_dir 下创建一套完全独立的 manifest、evidence 目录和解析后的 items。

        返回 (manifest_path, evidence_dir, items)
        """
        evidence_dir = os.path.join(base_dir, "evidence")
        os.makedirs(os.path.join(evidence_dir, "docs"), exist_ok=True)
        os.makedirs(os.path.join(evidence_dir, "images"), exist_ok=True)

        file_a = os.path.join(evidence_dir, "docs", "a.txt")
        file_b = os.path.join(evidence_dir, "docs", "b.txt")
        file_c = os.path.join(evidence_dir, "images", "c.png")
        for p, content in [(file_a, b"content of a 1234567890123456789012345678"),
                           (file_b, b"content of b 12345678901234567890123456789012"),
                           (file_c, b"png-bytes-here-1234567890123456789012345678901")]:
            with open(p, "wb") as f:
                f.write(content)

        manifest_path = os.path.join(base_dir, "manifest.csv")
        import hashlib
        def sha(p):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                h.update(f.read())
            return h.hexdigest()

        with open(manifest_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file_path", "size", "sha256"])
            w.writerow(["docs/a.txt", os.path.getsize(file_a), sha(file_a)])
            w.writerow(["docs/b.txt", os.path.getsize(file_b), sha(file_b)])
            w.writerow(["images/c.png", os.path.getsize(file_c), sha(file_c)])

        parsed = manifest_mod.parse_manifest(manifest_path)
        return manifest_path, evidence_dir, parsed.items

    def _import_batch_isolated(self, batch_no: str, description: str = "隔离批次"):
        """
        使用隔离的 fixture（临时 manifest + 临时 evidence_dir）导入批次。
        返回 (batch_id, item_count, manifest_path, evidence_dir, items)
        """
        fixture_dir = os.path.join(self.work_dir, f"fixture_{batch_no}")
        os.makedirs(fixture_dir, exist_ok=True)
        manifest_path, evidence_dir, items = self._create_isolated_fixture(fixture_dir)

        batch_id, count = db.replace_batch(
            self.db_path,
            batch_no=batch_no,
            manifest_path=manifest_path,
            evidence_dir=evidence_dir,
            items=items,
            description=description,
        )
        return batch_id, count, manifest_path, evidence_dir, items

    def test_save_snapshot(self):
        """测试保存快照"""
        batch_id, item_count = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        result = snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        self.assertTrue(os.path.exists(snapshot_path))
        self.assertEqual(result["batch"]["batch_no"], "test_batch")
        self.assertEqual(len(result["items"]), item_count)
        self.assertGreater(len(result["review_logs"]), 0)

        with open(snapshot_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], snapshot_mod.SNAPSHOT_VERSION)
        self.assertEqual(data["batch"]["description"], "测试批次")

    def test_load_snapshot_success(self):
        """测试加载有效快照"""
        self._import_batch()
        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        data = snapshot_mod.load_snapshot(snapshot_path)
        self.assertIn("batch", data)
        self.assertIn("items", data)
        self.assertIn("review_logs", data)

    def test_load_snapshot_not_found(self):
        """测试加载不存在的快照"""
        with self.assertRaises(snapshot_mod.SnapshotNotFoundError):
            snapshot_mod.load_snapshot("/nonexistent/snapshot.json")

    def test_load_snapshot_bad_json(self):
        """测试加载坏 JSON 的快照"""
        bad_path = os.path.join(self.work_dir, "bad.json")
        with open(bad_path, "w") as f:
            f.write("this is not json{{{")

        with self.assertRaises(snapshot_mod.SnapshotFormatError):
            snapshot_mod.load_snapshot(bad_path)

    def test_load_snapshot_missing_version(self):
        """测试缺少 version 字段的快照"""
        bad_path = os.path.join(self.work_dir, "no_version.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({"batch": {}, "items": [], "review_logs": []}, f)

        with self.assertRaises(snapshot_mod.SnapshotFormatError):
            snapshot_mod.load_snapshot(bad_path)

    def test_load_snapshot_version_mismatch(self):
        """测试版本不兼容的快照"""
        bad_path = os.path.join(self.work_dir, "bad_version.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "999.0",
                "batch": {},
                "items": [],
                "review_logs": [],
            }, f)

        with self.assertRaises(snapshot_mod.SnapshotVersionError):
            snapshot_mod.load_snapshot(bad_path)

    def test_restore_snapshot_new_db(self):
        """测试恢复快照到新数据库"""
        batch_id, item_count = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        restored_batch, restored_count = snapshot_mod.restore_snapshot(
            new_db_path, snapshot_path
        )

        self.assertEqual(restored_batch, "test_batch")
        self.assertEqual(restored_count, item_count)

        batch = db.get_batch_by_no(new_db_path, "test_batch")
        self.assertIsNotNone(batch)
        self.assertEqual(batch["description"], "测试批次")

        items = db.get_evidence_items(new_db_path, batch["id"])
        self.assertEqual(len(items), item_count)

        logs = db.get_review_history(new_db_path, batch["id"], limit=100)
        self.assertEqual(len(logs), 2)

    def test_restore_snapshot_conflict(self):
        """测试恢复快照时批次已存在（冲突）"""
        self._import_batch()
        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        with self.assertRaises(snapshot_mod.SnapshotConflictError):
            snapshot_mod.restore_snapshot(self.db_path, snapshot_path, force=False)

    def test_restore_snapshot_force(self):
        """测试使用 --force 强制覆盖已存在的批次"""
        batch_id, _ = self._import_batch(description="旧描述")

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        db.review_item(
            self.db_path,
            batch_id=batch_id,
            item_id=db.get_evidence_items(self.db_path, batch_id)[0]["id"],
            new_status="signed",
            remark="恢复前的额外复核",
            operator="tester2",
            action="review",
        )

        before_logs = db.get_review_history(self.db_path, batch_id, limit=100)
        before_total, before_signed, before_supplement, before_pending = db.count_reviewed(
            self.db_path, batch_id
        )

        restored_batch, count = snapshot_mod.restore_snapshot(
            self.db_path, snapshot_path, force=True
        )

        self.assertEqual(restored_batch, "test_batch")

        new_batch = db.get_batch_by_no(self.db_path, "test_batch")
        self.assertEqual(new_batch["description"], "旧描述")

        after_logs = db.get_review_history(self.db_path, new_batch["id"], limit=100)
        after_total, after_signed, after_supplement, after_pending = db.count_reviewed(
            self.db_path, new_batch["id"]
        )

        self.assertLess(len(after_logs), len(before_logs))
        self.assertLess(after_signed, before_signed)

    def test_restore_snapshot_atomic_failure(self):
        """测试恢复失败时数据库保持不变（原子性）"""
        self._import_batch(batch_no="existing_batch", description="已存在批次")

        bad_snapshot_path = os.path.join(self.work_dir, "bad_snapshot.json")
        with open(bad_snapshot_path, "w") as f:
            f.write("not valid json")

        existing_before = db.get_batch_by_no(self.db_path, "existing_batch")
        self.assertIsNotNone(existing_before)

        try:
            snapshot_mod.restore_snapshot(
                self.db_path, bad_snapshot_path, force=False
            )
        except snapshot_mod.SnapshotFormatError:
            pass

        existing_after = db.get_batch_by_no(self.db_path, "existing_batch")
        self.assertIsNotNone(existing_after)
        self.assertEqual(existing_after["description"], "已存在批次")

    def test_restore_with_evidence_dir_remap(self):
        """测试恢复时重映射证据目录"""
        self._import_batch()
        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        new_evidence_dir = os.path.join(self.work_dir, "mapped_evidence")
        shutil.copytree(self.evidence_dir, new_evidence_dir)

        restored_batch, _ = snapshot_mod.restore_snapshot(
            new_db_path,
            snapshot_path,
            force=False,
            evidence_dir=new_evidence_dir,
        )

        batch = db.get_batch_by_no(new_db_path, restored_batch)
        self.assertEqual(batch["evidence_dir"], os.path.abspath(new_evidence_dir))
        self.assertNotEqual(batch["evidence_dir"], self.evidence_dir)

    def test_restore_after_review_undo(self):
        """测试有撤销记录的快照能否正确恢复"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        db.undo_last_review(self.db_path, batch_id, operator="tester")

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        logs = db.get_review_history(new_db_path, new_batch["id"], limit=100)

        self.assertEqual(len(logs), 3)

        undo_logs = [l for l in logs if l["action"] == "undo"]
        self.assertEqual(len(undo_logs), 1)

    def test_restore_then_continue_review(self):
        """测试恢复后继续复核操作"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        items = db.get_evidence_items(new_db_path, new_batch["id"])

        pending_items = [i for i in items if i["review_status"] == "pending"]
        self.assertGreater(len(pending_items), 0)

        log_id = db.review_item(
            new_db_path,
            batch_id=new_batch["id"],
            item_id=pending_items[0]["id"],
            new_status="signed",
            remark="恢复后新增的复核",
            operator="new_operator",
            action="review",
        )

        self.assertIsInstance(log_id, int)
        self.assertGreater(log_id, 0)

        updated = db.get_evidence_item_by_id(new_db_path, pending_items[0]["id"])
        self.assertEqual(updated["review_status"], "signed")
        self.assertEqual(updated["review_remark"], "恢复后新增的复核")

    def test_restore_then_undo(self):
        """测试恢复后执行撤销操作"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")

        undo_result = db.undo_last_review(new_db_path, new_batch["id"], operator="tester2")

        self.assertIsNotNone(undo_result)
        self.assertEqual(undo_result["action"], "review")

    def test_restore_then_export(self):
        """测试恢复后导出报告"""
        from evidence_cli import report as report_mod

        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        items = db.get_evidence_items(new_db_path, new_batch["id"])

        export_path = os.path.join(self.work_dir, "export.json")
        count = report_mod.export_json(items, export_path, batch_info=new_batch)

        self.assertEqual(count, len(items))
        self.assertTrue(os.path.exists(export_path))

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)
        self.assertEqual(export_data["batch"]["batch_no"], "test_batch")
        self.assertEqual(len(export_data["items"]), len(items))

    def test_list_snapshots(self):
        """测试列出快照"""
        self._import_batch()

        snapshots = snapshot_mod.list_snapshots(self.work_dir)
        self.assertEqual(len(snapshots), 0)

        snapshot_path = snapshot_mod.get_snapshot_path(self.work_dir, "snap1")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        snapshot_path2 = snapshot_mod.get_snapshot_path(self.work_dir, "snap2")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path2)

        snapshots = snapshot_mod.list_snapshots(self.work_dir)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["batch_no"], "test_batch")

    def test_save_nonexistent_batch(self):
        """测试保存不存在的批次快照"""
        snapshot_path = os.path.join(self.work_dir, "nonexistent.json")
        with self.assertRaises(snapshot_mod.SnapshotNotFoundError):
            snapshot_mod.save_snapshot(self.db_path, "no_such_batch", snapshot_path)

    def test_restore_manifest_missing(self):
        """测试原清单文件缺失时恢复失败"""
        batch_id, _, _, _, _ = self._import_batch_isolated("iso_manifest")
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "snap_manifest_gone.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_manifest", snapshot_path)

        batch_before = db.get_batch_by_no(self.db_path, "iso_manifest")
        orig_manifest = batch_before["manifest_path"]
        os.remove(orig_manifest)

        new_work_dir = os.path.join(self.work_dir, "new_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError) as ctx:
            snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        self.assertIn("清单文件", str(ctx.exception))
        self.assertIn(orig_manifest, str(ctx.exception))

        batches_after = db.list_batches(new_db_path)
        self.assertEqual(len(batches_after), 0)

    def test_restore_evidence_dir_missing(self):
        """测试原证据目录缺失时恢复失败"""
        batch_id, _, _, orig_evidence_dir, _ = self._import_batch_isolated("iso_evidence")
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "snap_evidence_gone.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_evidence", snapshot_path)

        shutil.rmtree(orig_evidence_dir)
        self.assertFalse(os.path.isdir(orig_evidence_dir))

        new_work_dir = os.path.join(self.work_dir, "new_work2")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError) as ctx:
            snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        self.assertIn("证据目录", str(ctx.exception))
        self.assertIn(orig_evidence_dir, str(ctx.exception))

        batches_after = db.list_batches(new_db_path)
        self.assertEqual(len(batches_after), 0)

    def test_restore_missing_files_no_partial_data(self):
        """测试缺文件失败后，已有批次的数据库保持不变（不落半截数据）"""
        keep_id, _, _, _, _ = self._import_batch_isolated("keep_me", "不可被破坏")
        first_item = db.get_evidence_items(self.db_path, keep_id)[0]
        db.review_item(
            self.db_path,
            batch_id=keep_id,
            item_id=first_item["id"],
            new_status="signed",
            remark="恢复前的原始复核",
            operator="original",
            action="review",
        )
        before_keep_logs = db.get_review_history(self.db_path, keep_id, limit=100)
        before_keep_total, before_keep_signed, _, _ = db.count_reviewed(self.db_path, keep_id)

        snap_id, _, orig_manifest, orig_evidence, _ = self._import_batch_isolated("to_snapshot", "待快照批次")
        items_before = db.get_evidence_items(self.db_path, snap_id)
        logs_before = db.get_review_history(self.db_path, snap_id, limit=100)
        _, before_snap_signed, before_snap_supp, before_snap_pend = db.count_reviewed(
            self.db_path, snap_id
        )

        snapshot_path = os.path.join(self.work_dir, "snap_both_gone.json")
        snapshot_mod.save_snapshot(self.db_path, "to_snapshot", snapshot_path)

        os.remove(orig_manifest)
        shutil.rmtree(orig_evidence)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError):
            snapshot_mod.restore_snapshot(self.db_path, snapshot_path, force=True)

        keep_batch = db.get_batch_by_no(self.db_path, "keep_me")
        self.assertIsNotNone(keep_batch)
        self.assertEqual(keep_batch["description"], "不可被破坏")
        after_keep_logs = db.get_review_history(self.db_path, keep_batch["id"], limit=100)
        after_keep_total, after_keep_signed, _, _ = db.count_reviewed(self.db_path, keep_batch["id"])
        self.assertEqual(len(after_keep_logs), len(before_keep_logs))
        self.assertEqual(after_keep_signed, before_keep_signed)
        self.assertEqual(after_keep_total, before_keep_total)

        snap_after = db.get_batch_by_no(self.db_path, "to_snapshot")
        self.assertIsNotNone(snap_after)
        self.assertEqual(snap_after["description"], "待快照批次")
        items_after = db.get_evidence_items(self.db_path, snap_after["id"])
        self.assertEqual(len(items_after), len(items_before))
        _, after_snap_signed, after_snap_supp, after_snap_pend = db.count_reviewed(
            self.db_path, snap_after["id"]
        )
        self.assertEqual(after_snap_signed, before_snap_signed)
        self.assertEqual(after_snap_supp, before_snap_supp)
        self.assertEqual(after_snap_pend, before_snap_pend)

    def test_restore_with_remapped_evidence_bypasses_check(self):
        """测试使用 --evidence-dir 重映射时，会校验新目录而非原目录"""
        batch_id, _, _, orig_evidence, _ = self._import_batch_isolated("iso_remap")
        snapshot_path = os.path.join(self.work_dir, "snap_remap.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_remap", snapshot_path)

        shutil.rmtree(orig_evidence)

        new_work_dir = os.path.join(self.work_dir, "remap_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError) as ctx:
            snapshot_mod.restore_snapshot(new_db_path, snapshot_path)
        self.assertIn("证据目录", str(ctx.exception))

        remapped_dir = os.path.join(self.work_dir, "mapped_evidence_real")
        remap_fixture = os.path.join(self.work_dir, "remap_fixture")
        os.makedirs(remap_fixture, exist_ok=True)
        _, _, _ = self._create_isolated_fixture(remap_fixture)
        shutil.move(os.path.join(remap_fixture, "evidence"), remapped_dir)

        restored_batch, count = snapshot_mod.restore_snapshot(
            new_db_path,
            snapshot_path,
            evidence_dir=remapped_dir,
        )
        self.assertEqual(restored_batch, "iso_remap")
        self.assertGreater(count, 0)

        new_batch = db.get_batch_by_no(new_db_path, restored_batch)
        self.assertEqual(new_batch["evidence_dir"], os.path.abspath(remapped_dir))

    def test_restore_normal_then_review_undo_export(self):
        """回归测试：正常恢复后仍可继续 review、undo、export"""
        batch_id, _, _, _, _ = self._import_batch_isolated("iso_normal")
        self._review_some_items(batch_id)
        batch_before = db.get_batch_by_no(self.db_path, "iso_normal")

        snapshot_path = os.path.join(self.work_dir, "snap_normal.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_normal", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "normal_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        restored_batch, count = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)
        self.assertEqual(restored_batch, "iso_normal")
        self.assertEqual(count, 3)

        new_batch = db.get_batch_by_no(new_db_path, restored_batch)
        items = db.get_evidence_items(new_db_path, new_batch["id"])

        pending = [i for i in items if i["review_status"] == "pending"]
        self.assertGreater(len(pending), 0)

        log_id = db.review_item(
            new_db_path,
            batch_id=new_batch["id"],
            item_id=pending[0]["id"],
            new_status="signed",
            remark="恢复后复核",
            operator="tester_new",
            action="review",
        )
        self.assertIsInstance(log_id, int)
        self.assertGreater(log_id, 0)

        undo = db.undo_last_review(new_db_path, new_batch["id"], operator="tester_new")
        self.assertIsNotNone(undo)
        self.assertEqual(undo["action"], "review")

        from evidence_cli import report as report_mod
        export_path = os.path.join(new_work_dir, "regression_export.json")
        export_count = report_mod.export_json(
            db.get_evidence_items(new_db_path, new_batch["id"]),
            export_path,
            batch_info=new_batch,
        )
        self.assertGreater(export_count, 0)
        self.assertTrue(os.path.exists(export_path))

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)
        self.assertEqual(export_data["batch"]["batch_no"], "iso_normal")
        self.assertEqual(len(export_data["items"]), count)

    def test_restore_single_evidence_file_missing(self):
        """复现：单个引用证据文件缺失 → 恢复失败且不落脏数据"""
        batch_id, _, _, evidence_dir, _ = self._import_batch_isolated("iso_file_miss")
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "snap_file_miss.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_file_miss", snapshot_path)

        items = db.get_evidence_items(self.db_path, batch_id)
        target_rel = items[0]["file_path"]
        target_line = items[0]["manifest_line_no"]
        target_full = os.path.join(evidence_dir, target_rel)
        self.assertTrue(os.path.isfile(target_full))
        os.remove(target_full)
        self.assertFalse(os.path.isfile(target_full))

        new_work_dir = os.path.join(self.work_dir, "file_miss_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError) as ctx:
            snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        msg = str(ctx.exception)
        self.assertIn("证据文件缺失", msg)
        self.assertIn(target_rel, msg)
        self.assertIn(str(target_line), msg)

        batches_after = db.list_batches(new_db_path)
        self.assertEqual(len(batches_after), 0)

    def test_restore_remapped_evidence_dir_missing_file(self):
        """重映射证据目录时也要校验每个引用文件是否齐全"""
        batch_id, _, _, orig_evidence, _ = self._import_batch_isolated("iso_remap_miss")
        snapshot_path = os.path.join(self.work_dir, "snap_remap_miss.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_remap_miss", snapshot_path)

        items = db.get_evidence_items(self.db_path, batch_id)
        target_rel = items[1]["file_path"]
        target_line = items[1]["manifest_line_no"]

        remap_fixture = os.path.join(self.work_dir, "remap_fixture2")
        _, remapped_dir, _ = self._create_isolated_fixture(remap_fixture)

        bad_file = os.path.join(remapped_dir, target_rel.replace("/", os.sep))
        self.assertTrue(os.path.isfile(bad_file))
        os.remove(bad_file)

        new_work_dir = os.path.join(self.work_dir, "remap_miss_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError) as ctx:
            snapshot_mod.restore_snapshot(
                new_db_path,
                snapshot_path,
                evidence_dir=remapped_dir,
            )
        msg = str(ctx.exception)
        self.assertIn("证据文件缺失", msg)
        self.assertIn(target_rel, msg)
        self.assertIn(str(target_line), msg)

        batches_after = db.list_batches(new_db_path)
        self.assertEqual(len(batches_after), 0)

        intact = os.path.join(self.work_dir, "remap_fixture_intact")
        _, intact_dir, _ = self._create_isolated_fixture(intact)
        restored_batch, count = snapshot_mod.restore_snapshot(
            new_db_path,
            snapshot_path,
            evidence_dir=intact_dir,
        )
        self.assertEqual(restored_batch, "iso_remap_miss")
        self.assertGreater(count, 0)

        new_batch = db.get_batch_by_no(new_db_path, restored_batch)
        items = db.get_evidence_items(new_db_path, new_batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self.assertGreater(len(pending), 0)

        log_id = db.review_item(
            new_db_path,
            batch_id=new_batch["id"],
            item_id=pending[0]["id"],
            new_status="supplement",
            remark="remap后补的复核",
            operator="tester_remap",
            action="review",
        )
        self.assertIsInstance(log_id, int)
        self.assertGreater(log_id, 0)

        undo = db.undo_last_review(new_db_path, new_batch["id"], operator="tester_remap")
        self.assertIsNotNone(undo)

        from evidence_cli import report as report_mod
        export_path = os.path.join(new_work_dir, "remap_export.json")
        c = report_mod.export_json(
            db.get_evidence_items(new_db_path, new_batch["id"]),
            export_path,
            batch_info=new_batch,
        )
        self.assertGreater(c, 0)
        self.assertTrue(os.path.exists(export_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
