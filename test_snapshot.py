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

        restored_batch, restored_count, summary = snapshot_mod.restore_snapshot(
            new_db_path, snapshot_path
        )

        self.assertEqual(restored_batch, "test_batch")
        self.assertEqual(restored_count, item_count)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["batch_no"], "test_batch")
        self.assertEqual(summary["item_count"], item_count)
        self.assertIn("restored_from", summary)
        self.assertIn("review_stats", summary)

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

        restored_batch, count, summary = snapshot_mod.restore_snapshot(
            self.db_path, snapshot_path, force=True
        )

        self.assertEqual(restored_batch, "test_batch")
        self.assertIsNotNone(summary)
        self.assertTrue(summary["was_force"])
        self.assertTrue(summary["was_conflict"])
        self.assertIsNotNone(summary["diff"])

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

        restored_batch, _, summary = snapshot_mod.restore_snapshot(
            new_db_path,
            snapshot_path,
            force=False,
            evidence_dir=new_evidence_dir,
        )
        self.assertIsNotNone(summary)
        self.assertEqual(summary["evidence_dir"], os.path.abspath(new_evidence_dir))

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

        _, _, summary = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)
        self.assertIsNotNone(summary)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        self.assertIsNotNone(new_batch.get("restored_from"))
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

        _, _, _ = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        self.assertIsNotNone(new_batch.get("restored_from"))
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

        _, _, _ = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        self.assertIsNotNone(new_batch.get("restored_from"))

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

        _, _, _ = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        new_batch = db.get_batch_by_no(new_db_path, "test_batch")
        self.assertIsNotNone(new_batch.get("restored_from"))
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

        restored_batch, count, summary = snapshot_mod.restore_snapshot(
            new_db_path,
            snapshot_path,
            evidence_dir=remapped_dir,
        )
        self.assertEqual(restored_batch, "iso_remap")
        self.assertGreater(count, 0)
        self.assertIsNotNone(summary)

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

        restored_batch, count, summary = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)
        self.assertEqual(restored_batch, "iso_normal")
        self.assertEqual(count, 3)
        self.assertIsNotNone(summary)

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
        restored_batch, count, summary = snapshot_mod.restore_snapshot(
            new_db_path,
            snapshot_path,
            evidence_dir=intact_dir,
        )
        self.assertEqual(restored_batch, "iso_remap_miss")
        self.assertGreater(count, 0)
        self.assertIsNotNone(summary)

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

    def test_preview_restore_basic(self):
        """测试预演恢复基本信息"""
        batch_id, item_count = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "preview_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        preview = snapshot_mod.preview_restore(
            new_db_path, snapshot_path, force=False
        )

        self.assertEqual(preview["batch_no"], "test_batch")
        self.assertEqual(preview["item_count"], item_count)
        self.assertEqual(preview["will_conflict"], False)
        self.assertEqual(preview["can_restore"], True)
        self.assertIsNotNone(preview["manifest_path"])
        self.assertIsNotNone(preview["evidence_dir"])
        self.assertIsNotNone(preview["precheck_stats"])
        self.assertIsNotNone(preview["review_stats"])
        self.assertIsNotNone(preview["last_log"])
        self.assertEqual(preview["precheck_stats"]["total"], item_count)
        self.assertEqual(preview["review_stats"]["total"], item_count)

        batches_after = db.list_batches(new_db_path)
        self.assertEqual(len(batches_after), 0)

    def test_preview_restore_conflict(self):
        """测试预演时检测到冲突"""
        self._import_batch(description="已存在批次")

        batch_id2, _, _, _, _ = self._import_batch_isolated("test_batch", "快照批次")
        self._review_some_items(batch_id2)
        snapshot_path = os.path.join(self.work_dir, "conflict_snap.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        preview = snapshot_mod.preview_restore(
            self.db_path, snapshot_path, force=False
        )

        self.assertEqual(preview["will_conflict"], True)
        self.assertEqual(preview["can_restore"], False)
        self.assertIn("conflict_reason", preview)
        self.assertIsNotNone(preview["existing_batch"])

    def test_preview_restore_force_with_diff(self):
        """测试预演强制覆盖时的差异计算"""
        batch_id, _ = self._import_batch(description="测试批次")
        self._review_some_items(batch_id)

        snapshot_path_old = os.path.join(self.work_dir, "snap_old.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path_old)

        items = db.get_evidence_items(self.db_path, batch_id)
        db.review_item(
            self.db_path,
            batch_id=batch_id,
            item_id=items[0]["id"],
            new_status="supplement",
            remark="后续新增的复核",
            operator="later_op",
            action="review",
        )

        preview = snapshot_mod.preview_restore(
            self.db_path, snapshot_path_old, force=True
        )

        self.assertEqual(preview["will_conflict"], True)
        self.assertEqual(preview["can_restore"], True)
        self.assertIsNotNone(preview["diff"])
        self.assertIn("old_batch", preview["diff"])
        self.assertIn("new_batch", preview["diff"])
        self.assertIn("review_stats", preview["diff"])
        self.assertIn("precheck_stats", preview["diff"])
        self.assertIn("items", preview["diff"])

        old_rv = preview["diff"]["review_stats"]["old"]
        new_rv = preview["diff"]["review_stats"]["new"]
        self.assertEqual(old_rv["signed"], 0)
        self.assertEqual(old_rv["supplement"], 2)
        self.assertEqual(new_rv["signed"], 1)
        self.assertEqual(new_rv["supplement"], 1)

    def test_restore_summary_persisted(self):
        """测试恢复摘要持久化到数据库"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "persist_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        restored_batch, _, summary = snapshot_mod.restore_snapshot(
            new_db_path, snapshot_path
        )

        batch = db.get_batch_by_no(new_db_path, restored_batch)
        self.assertIsNotNone(batch.get("restored_from"))
        self.assertIsNotNone(batch.get("restored_at"))
        self.assertEqual(batch["restored_from"], os.path.abspath(snapshot_path))
        self.assertIsNone(batch.get("restore_diff"))

        batches = db.list_batches(new_db_path)
        self.assertEqual(len(batches), 1)
        self.assertIsNotNone(batches[0].get("restored_from"))

    def test_restore_force_diff_persisted(self):
        """测试覆盖恢复时差异持久化"""
        old_batch_id, _ = self._import_batch(description="旧描述")
        self._review_some_items(old_batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        db.review_item(
            self.db_path,
            batch_id=old_batch_id,
            item_id=db.get_evidence_items(self.db_path, old_batch_id)[0]["id"],
            new_status="supplement",
            remark="恢复前的修改",
            operator="someone",
            action="review",
        )

        restored_batch, _, summary = snapshot_mod.restore_snapshot(
            self.db_path, snapshot_path, force=True
        )

        self.assertTrue(summary["was_force"])
        self.assertTrue(summary["was_conflict"])
        self.assertIsNotNone(summary["diff"])

        batch = db.get_batch_by_no(self.db_path, restored_batch)
        self.assertIsNotNone(batch.get("restored_from"))
        self.assertIsNotNone(batch.get("restore_diff"))

        import json
        diff = json.loads(batch["restore_diff"])
        self.assertIn("old_batch", diff)
        self.assertIn("review_stats", diff)

    def test_restore_then_review_updates_stats(self):
        """测试恢复后继续复核，统计正确更新"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "review_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        _, _, summary = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)
        batch = db.get_batch_by_no(new_db_path, "test_batch")

        before_total, before_signed, before_supp, before_pending = db.count_reviewed(
            new_db_path, batch["id"]
        )

        items = db.get_evidence_items(new_db_path, batch["id"])
        pending_items = [i for i in items if i["review_status"] == "pending"]
        self.assertGreater(len(pending_items), 0)

        db.review_item(
            new_db_path,
            batch_id=batch["id"],
            item_id=pending_items[0]["id"],
            new_status="signed",
            remark="恢复后新增",
            operator="new_op",
            action="review",
        )

        after_total, after_signed, after_supp, after_pending = db.count_reviewed(
            new_db_path, batch["id"]
        )

        self.assertEqual(after_total, before_total)
        self.assertEqual(after_signed, before_signed + 1)
        self.assertEqual(after_pending, before_pending - 1)

        history = db.get_review_history(new_db_path, batch["id"], limit=1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["new_status"], "signed")
        self.assertEqual(history[0]["new_remark"], "恢复后新增")

    def test_restore_then_undo_rollback(self):
        """测试恢复后执行撤销，统计正确回滚"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "undo_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        _, _, _ = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)
        batch = db.get_batch_by_no(new_db_path, "test_batch")

        before_total, before_signed, before_supp, before_pending = db.count_reviewed(
            new_db_path, batch["id"]
        )
        before_history = db.get_review_history(new_db_path, batch["id"], limit=100)

        undo_result = db.undo_last_review(new_db_path, batch["id"], operator="undo_op")
        self.assertIsNotNone(undo_result)

        after_total, after_signed, after_supp, after_pending = db.count_reviewed(
            new_db_path, batch["id"]
        )
        after_history = db.get_review_history(new_db_path, batch["id"], limit=100)

        self.assertEqual(after_total, before_total)
        self.assertEqual(len(after_history), len(before_history) + 1)

        if undo_result["new_status"] == "signed":
            self.assertEqual(after_signed, before_signed - 1)
        elif undo_result["new_status"] == "supplement":
            self.assertEqual(after_supp, before_supp - 1)

    def test_restore_cross_process_consistency(self):
        """测试跨进程数据一致性（重新连接数据库验证）"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "cross_snap.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "cross_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        _, _, _ = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        db2_path = db.get_db_path(new_work_dir)
        batch = db.get_batch_by_no(db2_path, "test_batch")
        self.assertIsNotNone(batch)
        self.assertIsNotNone(batch.get("restored_from"))

        total, signed, supp, pending = db.count_reviewed(db2_path, batch["id"])
        self.assertGreater(total, 0)

        items = db.get_evidence_items(db2_path, batch["id"])
        self.assertEqual(len(items), total)

        history = db.get_review_history(db2_path, batch["id"], limit=100)
        self.assertGreater(len(history), 0)

        batches = db.list_batches(db2_path)
        self.assertEqual(len(batches), 1)
        self.assertIsNotNone(batches[0].get("restored_from"))

    def test_preview_does_not_modify_db(self):
        """测试预演不会修改数据库"""
        batch_id, _ = self._import_batch(description="保持不变")
        self._review_some_items(batch_id)

        before_batch = db.get_batch_by_no(self.db_path, "test_batch")
        before_items = db.get_evidence_items(self.db_path, batch_id)
        before_history = db.get_review_history(self.db_path, batch_id, limit=100)
        before_total, before_signed, before_supp, before_pending = db.count_reviewed(
            self.db_path, batch_id
        )

        new_batch_id, _, _, _, _ = self._import_batch_isolated("iso_preview", "隔离批次")
        snapshot_path = os.path.join(self.work_dir, "preview_snap.json")
        snapshot_mod.save_snapshot(self.db_path, "iso_preview", snapshot_path)

        preview = snapshot_mod.preview_restore(
            self.db_path, snapshot_path, force=False
        )
        self.assertEqual(preview["batch_no"], "iso_preview")
        self.assertEqual(preview["will_conflict"], True)
        self.assertEqual(preview["can_restore"], False)

        after_batch = db.get_batch_by_no(self.db_path, "test_batch")
        after_items = db.get_evidence_items(self.db_path, batch_id)
        after_history = db.get_review_history(self.db_path, batch_id, limit=100)
        after_total, after_signed, after_supp, after_pending = db.count_reviewed(
            self.db_path, batch_id
        )

        self.assertEqual(before_batch["description"], after_batch["description"])
        self.assertEqual(len(before_items), len(after_items))
        self.assertEqual(len(before_history), len(after_history))
        self.assertEqual(before_total, after_total)
        self.assertEqual(before_signed, after_signed)
        self.assertEqual(before_supp, after_supp)
        self.assertEqual(before_pending, after_pending)

        iso_batch = db.get_batch_by_no(self.db_path, "iso_preview")
        self.assertIsNotNone(iso_batch)

    def test_restore_failure_atomicity(self):
        """测试恢复失败时数据库保持原子性（无半截数据）"""
        keep_id, _, _, _, _ = self._import_batch_isolated("keep_me", "保留批次")
        db.review_item(
            self.db_path,
            batch_id=keep_id,
            item_id=db.get_evidence_items(self.db_path, keep_id)[0]["id"],
            new_status="signed",
            remark="原始复核",
            operator="orig_op",
            action="review",
        )

        snap_id, _, orig_manifest, orig_evidence, _ = self._import_batch_isolated(
            "to_restore", "待恢复批次"
        )
        snapshot_path = os.path.join(self.work_dir, "atomic_snap.json")
        snapshot_mod.save_snapshot(self.db_path, "to_restore", snapshot_path)

        os.remove(orig_manifest)

        before_keep = db.get_batch_by_no(self.db_path, "keep_me")
        before_keep_total, before_keep_signed, _, _ = db.count_reviewed(
            self.db_path, keep_id
        )
        before_snap = db.get_batch_by_no(self.db_path, "to_restore")
        before_batches = db.list_batches(self.db_path)

        try:
            snapshot_mod.restore_snapshot(
                self.db_path, snapshot_path, force=True
            )
            self.fail("应该抛出异常")
        except snapshot_mod.SnapshotMissingFilesError:
            pass

        after_keep = db.get_batch_by_no(self.db_path, "keep_me")
        after_keep_total, after_keep_signed, _, _ = db.count_reviewed(
            self.db_path, keep_id
        )
        after_snap = db.get_batch_by_no(self.db_path, "to_restore")
        after_batches = db.list_batches(self.db_path)

        self.assertEqual(before_keep["description"], after_keep["description"])
        self.assertEqual(before_keep_total, after_keep_total)
        self.assertEqual(before_keep_signed, after_keep_signed)
        self.assertEqual(before_snap["description"], after_snap["description"])
        self.assertEqual(len(before_batches), len(after_batches))

    def test_export_includes_restore_summary(self):
        """测试导出报告包含恢复摘要"""
        from evidence_cli import report as report_mod
        import json

        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)

        snapshot_path = os.path.join(self.work_dir, "test_snapshot.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work_dir = os.path.join(self.work_dir, "export_work")
        os.makedirs(new_work_dir, exist_ok=True)
        new_db_path = db.get_db_path(new_work_dir)
        db.init_db(new_db_path)

        _, _, _ = snapshot_mod.restore_snapshot(new_db_path, snapshot_path)

        batch = db.get_batch_by_no(new_db_path, "test_batch")
        items = db.get_evidence_items(new_db_path, batch["id"])

        export_path = os.path.join(new_work_dir, "export_with_restore.json")
        total_pc, passed, failed, unchecked = db.count_precheck(new_db_path, batch["id"])
        total_rv, signed, supplement, pending = db.count_reviewed(new_db_path, batch["id"])
        report_mod.export_json(
            items,
            export_path,
            batch_info=batch,
            precheck_stats={"total": total_pc, "passed": passed, "failed": failed, "unchecked": unchecked},
            review_stats={"total": total_rv, "signed": signed, "supplement": supplement, "pending": pending},
        )

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)

        self.assertIn("restore", export_data["batch"])
        self.assertEqual(
            export_data["batch"]["restore"]["restored_from"],
            os.path.abspath(snapshot_path)
        )
        self.assertIn("restored_at", export_data["batch"]["restore"])

    def test_build_trace_nonexistent_batch(self):
        """build_trace 对不存在批次返回 None"""
        trace = snapshot_mod.build_trace(self.db_path, "no_such_batch")
        self.assertIsNone(trace)

    def test_build_trace_original_import_no_restore(self):
        """从未恢复的原始批次，build_trace 显示 has_restore_chain=False"""
        self._import_batch(batch_no="orig_batch", description="原始批次")
        trace = snapshot_mod.build_trace(self.db_path, "orig_batch")
        self.assertIsNotNone(trace)
        self.assertEqual(trace["batch_no"], "orig_batch")
        self.assertFalse(trace["has_restore_chain"])
        self.assertEqual(trace["events"], [])
        self.assertFalse(trace["modified_after_restore"])
        self.assertEqual(trace["post_restore_activity"], [])

    def test_restore_event_written(self):
        """恢复后 restore_events 表有记录"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "evt_test.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "evt_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="unit_test")

        events = db.get_restore_events_for_batch(
            new_db, db.get_batch_by_no(new_db, "test_batch")["id"]
        )
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["snapshot_path"], os.path.abspath(snapshot_path))
        self.assertEqual(ev["operator"], "unit_test")
        self.assertFalse(ev["was_force"])
        self.assertFalse(ev["was_remapped"])
        self.assertIsNone(ev["parent_restore_event_id"])
        self.assertIsNotNone(ev["restored_at"])

    def test_build_trace_normal_restore(self):
        """普通恢复后 build_trace 返回完整链路"""
        batch_id, _ = self._import_batch(description="源批次")
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "trace_normal.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "trace_work_normal")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="trace_op")
        trace = snapshot_mod.build_trace(new_db, "test_batch")

        self.assertIsNotNone(trace)
        self.assertTrue(trace["has_restore_chain"])
        self.assertEqual(len(trace["events"]), 1)
        ev = trace["events"][0]
        self.assertTrue(ev["snapshot_exists"])
        self.assertEqual(ev["operator"], "trace_op")
        self.assertFalse(ev["was_force"])
        self.assertFalse(ev["was_remapped"])
        self.assertIsNone(ev["parent_event_id"])
        self.assertTrue(ev["chain_ok"])
        self.assertEqual(ev["warnings"], [])
        self.assertFalse(trace["modified_after_restore"])
        self.assertEqual(trace["warnings"], [])

    def test_build_trace_snapshot_missing(self):
        """快照源文件被删除后，build_trace 显示警告"""
        batch_id, _ = self._import_batch()
        snapshot_path = os.path.join(self.work_dir, "trace_missing.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "trace_work_missing")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        snapshot_mod.restore_snapshot(new_db, snapshot_path)
        os.remove(snapshot_path)
        self.assertFalse(os.path.exists(snapshot_path))

        trace = snapshot_mod.build_trace(new_db, "test_batch")
        self.assertTrue(trace["has_restore_chain"])
        ev = trace["events"][0]
        self.assertFalse(ev["snapshot_exists"])
        self.assertIn("快照源文件已不存在", ev["warnings"])
        self.assertTrue(
            any("快照源文件已丢失" in w for w in trace["warnings"])
        )

    def test_build_trace_force_restore_with_parent(self):
        """强制覆盖恢复建立父事件关联，build_trace 显示链路和差异"""
        batch_id1, _ = self._import_batch(description="第一版", batch_no="chain_batch")
        self._review_some_items(batch_id1)
        snap1 = os.path.join(self.work_dir, "chain_v1.json")
        snapshot_mod.save_snapshot(self.db_path, "chain_batch", snap1)

        work_dir2 = os.path.join(self.work_dir, "chain_work")
        os.makedirs(work_dir2, exist_ok=True)
        db2 = db.get_db_path(work_dir2)
        db.init_db(db2)
        snapshot_mod.restore_snapshot(db2, snap1, operator="op_v1")

        batch_v1 = db.get_batch_by_no(db2, "chain_batch")
        items_v1 = db.get_evidence_items(db2, batch_v1["id"])
        db.review_item(
            db2,
            batch_id=batch_v1["id"],
            item_id=items_v1[0]["id"],
            new_status="supplement",
            remark="v1 现场修改",
            operator="live_user",
            action="review",
        )

        snap2 = os.path.join(self.work_dir, "chain_v2.json")
        snapshot_mod.save_snapshot(db2, "chain_batch", snap2)

        snapshot_mod.restore_snapshot(db2, snap2, force=True, operator="op_v2")

        trace = snapshot_mod.build_trace(db2, "chain_batch")
        self.assertEqual(len(trace["events"]), 2)

        ev1, ev2 = trace["events"]
        self.assertIsNone(ev1["parent_event_id"])
        self.assertEqual(ev2["parent_event_id"], ev1["event_id"])
        self.assertTrue(ev2["was_force"])
        self.assertTrue(ev2["chain_ok"])

        self.assertIsNotNone(ev2.get("old_batch_snapshot"))
        self.assertEqual(
            ev2["old_batch_snapshot"]["batch"]["description"], "第一版"
        )
        self.assertIsNotNone(ev2.get("restore_diff"))
        self.assertIn("review_stats", ev2["restore_diff"])

    def test_build_trace_modified_after_restore(self):
        """恢复后继续复核，build_trace 显示 modified_after_restore 和操作记录"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "mod_after.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "mod_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        snapshot_mod.restore_snapshot(new_db, snapshot_path)
        batch = db.get_batch_by_no(new_db, "test_batch")
        items = db.get_evidence_items(new_db, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self.assertGreater(len(pending), 0)
        db.review_item(
            new_db,
            batch_id=batch["id"],
            item_id=pending[0]["id"],
            new_status="signed",
            remark="恢复后新加",
            operator="after_user",
            action="review",
        )
        db.undo_last_review(new_db, batch["id"], operator="after_user")

        trace = snapshot_mod.build_trace(new_db, "test_batch")
        self.assertTrue(trace["modified_after_restore"])
        self.assertEqual(len(trace["post_restore_activity"]), 2)
        actions = [log["action"] for log in trace["post_restore_activity"]]
        self.assertEqual(actions, ["review", "undo"])
        self.assertTrue(
            any("恢复后有" in w and "新的复核/撤销" in w for w in trace["warnings"])
        )

    def test_restore_event_atomic_failure(self):
        """恢复失败时 restore_events 不留半截记录"""
        keep_id, _, _, _, _ = self._import_batch_isolated("keep_atomic", "保留批次")
        db.review_item(
            self.db_path,
            batch_id=keep_id,
            item_id=db.get_evidence_items(self.db_path, keep_id)[0]["id"],
            new_status="signed",
            remark="原复核",
            operator="orig",
            action="review",
        )

        snap_id, _, orig_manifest, orig_evidence, _ = self._import_batch_isolated(
            "atomic_fail", "待恢复"
        )
        snapshot_path = os.path.join(self.work_dir, "atomic_fail.json")
        snapshot_mod.save_snapshot(self.db_path, "atomic_fail", snapshot_path)

        os.remove(orig_manifest)
        shutil.rmtree(orig_evidence)

        before_events = db.get_restore_events_for_batch(
            self.db_path, keep_id
        )

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError):
            snapshot_mod.restore_snapshot(
                self.db_path, snapshot_path, force=True
            )

        keep = db.get_batch_by_no(self.db_path, "keep_atomic")
        after_events = db.get_restore_events_for_batch(self.db_path, keep["id"])
        self.assertEqual(len(before_events), len(after_events))

        snap = db.get_batch_by_no(self.db_path, "atomic_fail")
        snap_events = db.get_restore_events_for_batch(self.db_path, snap["id"])
        self.assertEqual(len(snap_events), 0)

    def test_restore_with_remap_records_in_event(self):
        """目录重映射时，restore_events 正确记录 before/after"""
        batch_id, _, _, orig_evidence, _ = self._import_batch_isolated("remap_evt")
        snapshot_path = os.path.join(self.work_dir, "remap_evt.json")
        snapshot_mod.save_snapshot(self.db_path, "remap_evt", snapshot_path)

        mapped_dir = os.path.join(self.work_dir, "mapped_real")
        os.makedirs(mapped_dir, exist_ok=True)
        shutil.copytree(orig_evidence, os.path.join(mapped_dir, "sub"))
        real_mapped = os.path.join(mapped_dir, "sub")

        new_work = os.path.join(self.work_dir, "remap_evt_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        snapshot_mod.restore_snapshot(
            new_db, snapshot_path, evidence_dir=real_mapped
        )

        batch = db.get_batch_by_no(new_db, "remap_evt")
        events = db.get_restore_events_for_batch(new_db, batch["id"])
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertTrue(ev["was_remapped"])
        self.assertEqual(ev["evidence_dir_before"], orig_evidence)
        self.assertEqual(ev["evidence_dir_after"], os.path.abspath(real_mapped))

        trace = snapshot_mod.build_trace(new_db, "remap_evt")
        self.assertTrue(trace["events"][0]["was_remapped"])

    def test_last_restore_event_id_backref(self):
        """恢复后 batches.last_restore_event_id 正确回写"""
        batch_id, _ = self._import_batch()
        snapshot_path = os.path.join(self.work_dir, "backref.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "backref_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        snapshot_mod.restore_snapshot(new_db, snapshot_path)
        batch = db.get_batch_by_no(new_db, "test_batch")
        self.assertIsNotNone(batch.get("last_restore_event_id"))

        last_evt = db.get_last_restore_event(new_db, batch["id"])
        self.assertIsNotNone(last_evt)
        self.assertEqual(last_evt["id"], batch["last_restore_event_id"])

    def test_cross_restart_consistency(self):
        """重启后重新连接数据库，恢复链路数据依然完整"""
        batch_id, _ = self._import_batch()
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "restart.json")
        snapshot_mod.save_snapshot(self.db_path, "test_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "restart_work")
        os.makedirs(new_work, exist_ok=True)
        new_db_path = db.get_db_path(new_work)
        db.init_db(new_db_path)
        snapshot_mod.restore_snapshot(new_db_path, snapshot_path, operator="first")

        batch = db.get_batch_by_no(new_db_path, "test_batch")
        items = db.get_evidence_items(new_db_path, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        db.review_item(
            new_db_path,
            batch_id=batch["id"],
            item_id=pending[0]["id"],
            new_status="supplement",
            remark="重开前复核",
            operator="first",
            action="review",
        )

        import importlib
        import evidence_cli.db as db_mod_reimport
        importlib.reload(db_mod_reimport)

        batch2 = db_mod_reimport.get_batch_by_no(new_db_path, "test_batch")
        self.assertIsNotNone(batch2.get("last_restore_event_id"))

        trace2 = snapshot_mod.build_trace(new_db_path, "test_batch")
        self.assertTrue(trace2["has_restore_chain"])
        self.assertEqual(len(trace2["events"]), 1)
        self.assertTrue(trace2["modified_after_restore"])
        self.assertGreaterEqual(len(trace2["post_restore_activity"]), 1)
        self.assertEqual(trace2["events"][0]["operator"], "first")


class TestRecoverySummaryRegression(unittest.TestCase):
    """回归测试组1：同名批次冲突和来源快照缺失的提示清晰度 + 统一恢复摘要一致性"""

    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="evi_reg1_")
        self.db_path = db.get_db_path(self.work_dir)
        db.init_db(self.db_path)
        self.evidence_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sample_evidence",
        )
        self.manifest_path = os.path.join(self.evidence_dir, "manifest_mixed.csv")

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _import_batch(self, batch_no="test_batch", description="测试批次"):
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

    def _create_isolated_fixture(self, base_dir: str):
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
        import hashlib
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
        parsed = manifest_mod.parse_manifest(manifest_path)
        return manifest_path, evidence_dir, parsed.items

    def _import_batch_isolated(self, batch_no: str, description: str = "隔离批次"):
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

    def _review_some_items(self, batch_id: int):
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

    def test_conflict_error_message_is_clear(self):
        """同名批次冲突时，异常消息包含批次号和 --force 提示"""
        self._import_batch(batch_no="conflict_batch", description="已存在批次")
        batch_id2, _, m2, e2, _ = self._import_batch_isolated("conflict_batch", "新批次")
        self._review_some_items(batch_id2)
        snapshot_path = os.path.join(self.work_dir, "conflict_snap.json")
        snapshot_mod.save_snapshot(self.db_path, "conflict_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "conflict_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        fixture_dir2 = os.path.join(new_work, "fixture_existing")
        os.makedirs(fixture_dir2, exist_ok=True)
        m3, e3, items3 = self._create_isolated_fixture(fixture_dir2)
        db.replace_batch(new_db, "conflict_batch", m3, e3, items3, "目标目录已存在批次")

        with self.assertRaises(snapshot_mod.SnapshotConflictError) as ctx:
            snapshot_mod.restore_snapshot(new_db, snapshot_path, force=False)

        msg = str(ctx.exception)
        self.assertIn("conflict_batch", msg)
        self.assertIn("--force", msg)
        self.assertIn("已存在", msg)

    def test_conflict_preview_shows_clear_reason(self):
        """预演冲突时，conflict_reason 清晰，摘要 warnings 包含受阻信息"""
        self._import_batch(batch_no="prev_conflict", description="已有批次")
        batch_id2, _, m2, e2, _ = self._import_batch_isolated("prev_conflict", "快照批次")
        self._review_some_items(batch_id2)
        snapshot_path = os.path.join(self.work_dir, "prev_conflict.json")
        snapshot_mod.save_snapshot(self.db_path, "prev_conflict", snapshot_path)

        new_work = os.path.join(self.work_dir, "prev_conflict_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        fixture_dir2 = os.path.join(new_work, "fix_exist")
        os.makedirs(fixture_dir2, exist_ok=True)
        m3, e3, items3 = self._create_isolated_fixture(fixture_dir2)
        db.replace_batch(new_db, "prev_conflict", m3, e3, items3, "已存在")

        preview = snapshot_mod.preview_restore(new_db, snapshot_path, force=False)
        self.assertFalse(preview["can_restore"])
        self.assertTrue(preview["will_conflict"])
        self.assertIn("prev_conflict", preview["conflict_reason"])
        self.assertIn("--force", preview["conflict_reason"])

        summary = snapshot_mod.build_recovery_summary_from_preview(preview)
        self.assertFalse(summary["reconciled"])
        has_conflict_warn = any("恢复受阻" in w for w in summary["warnings"])
        self.assertTrue(has_conflict_warn, f"warnings 应含恢复受阻: {summary['warnings']}")

    def test_source_snapshot_missing_warning_is_clear(self):
        """来源快照文件缺失时，build_recovery_summary 给出清晰警告"""
        batch_id, _, _, _, _ = self._import_batch_isolated("miss_src", "缺失源批次")
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "miss_src.json")
        snapshot_mod.save_snapshot(self.db_path, "miss_src", snapshot_path)

        new_work = os.path.join(self.work_dir, "miss_src_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="src_test")

        self.assertTrue(os.path.exists(snapshot_path))
        os.remove(snapshot_path)
        self.assertFalse(os.path.exists(snapshot_path))

        summary = snapshot_mod.build_recovery_summary(new_db, "miss_src")
        self.assertIsNotNone(summary)
        self.assertTrue(summary["has_restore"])

        src = summary["source_snapshot"]
        self.assertIsNotNone(src)
        self.assertFalse(src["exists"])
        self.assertEqual(src["path"], os.path.abspath(snapshot_path))

        has_missing_warn = any(
            "已不存在" in w or "已丢失" in w or "源快照" in w
            for w in summary["warnings"]
        )
        self.assertTrue(has_missing_warn, f"warnings 应含快照缺失: {summary['warnings']}")

        trace = snapshot_mod.build_trace(new_db, "miss_src")
        self.assertIn("recovery_summary", trace)
        self.assertFalse(trace["recovery_summary"]["source_snapshot"]["exists"])

    def test_manifest_missing_error_is_clear(self):
        """清单文件缺失时，异常消息包含具体文件路径"""
        batch_id, _, orig_manifest, orig_evidence, _ = self._import_batch_isolated(
            "miss_manifest", "清单缺失批次"
        )
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "miss_manifest.json")
        snapshot_mod.save_snapshot(self.db_path, "miss_manifest", snapshot_path)
        os.remove(orig_manifest)

        new_work = os.path.join(self.work_dir, "miss_m_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)

        with self.assertRaises(snapshot_mod.SnapshotMissingFilesError) as ctx:
            snapshot_mod.restore_snapshot(new_db, snapshot_path)

        msg = str(ctx.exception)
        self.assertIn("清单文件", msg)
        self.assertIn(orig_manifest, msg)

        preview = snapshot_mod.preview_restore(new_db, snapshot_path)
        self.assertFalse(preview["can_restore"])
        self.assertIn("missing_reason", preview)
        self.assertIn("清单文件", preview["missing_reason"])
        self.assertIn(orig_manifest, preview["missing_reason"])

    def test_summary_consistency_across_views(self):
        """普通恢复后，build_recovery_summary 在 resume/list/trace 中返回一致数据"""
        batch_id, _, _, _, _ = self._import_batch_isolated("consist_batch", "一致性测试")
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "consist.json")
        snapshot_mod.save_snapshot(self.db_path, "consist_batch", snapshot_path)

        new_work = os.path.join(self.work_dir, "consist_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="consist_op")

        s1 = snapshot_mod.build_recovery_summary(new_db, "consist_batch")
        s2 = snapshot_mod.build_recovery_summary(new_db, "consist_batch")

        self.assertEqual(s1["batch_no"], s2["batch_no"])
        self.assertEqual(s1["source_snapshot"]["path"], s2["source_snapshot"]["path"])
        self.assertEqual(s1["source_snapshot"]["exists"], s2["source_snapshot"]["exists"])
        self.assertEqual(s1["review_stats"], s2["review_stats"])
        self.assertEqual(s1["precheck_stats"], s2["precheck_stats"])
        self.assertEqual(s1["post_restore_ops"], s2["post_restore_ops"])
        self.assertEqual(s1["reconciled"], s2["reconciled"])
        self.assertEqual(s1["item_count"], s2["item_count"])

        trace = snapshot_mod.build_trace(new_db, "consist_batch")
        self.assertIn("recovery_summary", trace)
        s3 = trace["recovery_summary"]
        self.assertEqual(s1["source_snapshot"]["path"], s3["source_snapshot"]["path"])
        self.assertEqual(s1["review_stats"], s3["review_stats"])
        self.assertEqual(s1["last_review_log"]["id"], s3["last_review_log"]["id"])
        self.assertEqual(s1["reconciled"], s3["reconciled"])

    def test_force_restore_summary_shows_diff(self):
        """强制覆盖恢复后，恢复摘要包含覆盖差异且对账通过"""
        old_batch_id, _, _, _, _ = self._import_batch_isolated("force_sum", "旧描述")
        self._review_some_items(old_batch_id)
        snapshot_path = os.path.join(self.work_dir, "force_sum.json")
        snapshot_mod.save_snapshot(self.db_path, "force_sum", snapshot_path)

        items = db.get_evidence_items(self.db_path, old_batch_id)
        db.review_item(
            self.db_path,
            batch_id=old_batch_id,
            item_id=items[0]["id"],
            new_status="supplement",
            remark="恢复前新增",
            operator="before_op",
            action="review",
        )

        _, _, summary = snapshot_mod.restore_snapshot(
            self.db_path, snapshot_path, force=True, operator="force_op"
        )
        self.assertIsNotNone(summary)

        recovery_summary = snapshot_mod.build_recovery_summary(self.db_path, "force_sum")
        self.assertTrue(recovery_summary["has_restore"])
        self.assertIsNotNone(recovery_summary["overwrite_diff"])
        self.assertIn("old_batch", recovery_summary["overwrite_diff"])
        self.assertIn("review_stats", recovery_summary["overwrite_diff"])
        self.assertTrue(recovery_summary["restore_event"]["was_force"])
        self.assertTrue(recovery_summary["reconciled"])
        self.assertEqual(recovery_summary["post_restore_ops"]["count"], 0)

        diff = recovery_summary["overwrite_diff"]
        self.assertEqual(diff["old_batch"]["description"], "旧描述")
        self.assertEqual(diff["new_batch"]["description"], "旧描述")

    def test_summary_persists_across_db_reconnect(self):
        """重启（重新连接数据库）后，恢复摘要数据一致"""
        batch_id, _, _, _, _ = self._import_batch_isolated("persist_sum", "持久化测试")
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "persist_sum.json")
        snapshot_mod.save_snapshot(self.db_path, "persist_sum", snapshot_path)

        new_work = os.path.join(self.work_dir, "persist_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="persist_op")

        batch = db.get_batch_by_no(new_db, "persist_sum")
        items = db.get_evidence_items(new_db, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        db.review_item(
            new_db,
            batch_id=batch["id"],
            item_id=pending[0]["id"],
            new_status="signed",
            remark="恢复后复核",
            operator="after_op",
            action="review",
        )

        s_before = snapshot_mod.build_recovery_summary(new_db, "persist_sum")

        import importlib
        import evidence_cli.db as db_reimport
        importlib.reload(db_reimport)

        s_after = snapshot_mod.build_recovery_summary(new_db, "persist_sum")

        self.assertEqual(s_before["source_snapshot"]["path"], s_after["source_snapshot"]["path"])
        self.assertEqual(s_before["review_stats"], s_after["review_stats"])
        self.assertEqual(s_before["post_restore_ops"], s_after["post_restore_ops"])
        self.assertEqual(s_before["overwrite_diff"], s_after["overwrite_diff"])
        self.assertEqual(s_before["reconciled"], s_after["reconciled"])
        self.assertEqual(
            s_before["last_review_log"]["new_remark"],
            s_after["last_review_log"]["new_remark"],
        )

    def test_json_export_includes_recovery_summary(self):
        """JSON 导出包含完整的统一恢复摘要"""
        from evidence_cli import report as report_mod

        batch_id, _, _, _, _ = self._import_batch_isolated("export_sum", "导出测试")
        self._review_some_items(batch_id)
        snapshot_path = os.path.join(self.work_dir, "export_sum.json")
        snapshot_mod.save_snapshot(self.db_path, "export_sum", snapshot_path)

        new_work = os.path.join(self.work_dir, "export_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="export_op")

        batch = db.get_batch_by_no(new_db, "export_sum")
        items = db.get_evidence_items(new_db, batch["id"])
        total_pc, passed, failed, unchecked = db.count_precheck(new_db, batch["id"])
        total_rv, signed, supp, pend = db.count_reviewed(new_db, batch["id"])
        restore_trace = snapshot_mod.build_trace(new_db, "export_sum")
        recovery_summary = snapshot_mod.build_recovery_summary(new_db, "export_sum")

        export_path = os.path.join(new_work, "export_with_sum.json")
        report_mod.export_json(
            items,
            export_path,
            batch_info=batch,
            precheck_stats={"total": total_pc, "passed": passed, "failed": failed, "unchecked": unchecked},
            review_stats={"total": total_rv, "signed": signed, "supplement": supp, "pending": pend},
            restore_trace=restore_trace,
        )

        with open(export_path, "r", encoding="utf-8") as f:
            export_data = json.load(f)

        self.assertIn("recovery_summary", export_data)
        exported_sum = export_data["recovery_summary"]
        self.assertEqual(exported_sum["batch_no"], "export_sum")
        self.assertEqual(
            exported_sum["source_snapshot"]["path"],
            os.path.abspath(snapshot_path),
        )
        self.assertEqual(exported_sum["review_stats"], recovery_summary["review_stats"])
        self.assertEqual(exported_sum["precheck_stats"], recovery_summary["precheck_stats"])
        self.assertEqual(exported_sum["post_restore_ops"], recovery_summary["post_restore_ops"])
        self.assertEqual(exported_sum["reconciled"], recovery_summary["reconciled"])
        self.assertIn("reconciliation_details", exported_sum)


class TestPostRestoreUndoRegression(unittest.TestCase):
    """回归测试组2：恢复后继续 review 再 undo，日志顺序、统计、恢复摘要都不倒退"""

    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="evi_reg2_")
        self.db_path = db.get_db_path(self.work_dir)
        db.init_db(self.db_path)
        self.evidence_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sample_evidence",
        )
        self.manifest_path = os.path.join(self.evidence_dir, "manifest_mixed.csv")

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _create_isolated_fixture(self, base_dir: str):
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
        import hashlib
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
        parsed = manifest_mod.parse_manifest(manifest_path)
        return manifest_path, evidence_dir, parsed.items

    def _import_batch_isolated(self, batch_no: str, description: str = "隔离批次"):
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

    def _review_item(self, db_path, batch_id, item_id, status, remark, op):
        return db.review_item(db_path, batch_id, item_id, status, remark, op, "review")

    def test_log_order_after_restore_review_undo(self):
        """恢复→复核→撤销后，日志按时间顺序排列，undo 记录排在最后"""
        batch_id, _, _, _, _ = self._import_batch_isolated("log_order", "日志顺序")
        self._review_item(self.db_path, batch_id, 1, "signed", "快照内复核1", "snap_op")
        self._review_item(self.db_path, batch_id, 2, "supplement", "快照内复核2", "snap_op")

        snapshot_path = os.path.join(self.work_dir, "log_order.json")
        snapshot_mod.save_snapshot(self.db_path, "log_order", snapshot_path)

        new_work = os.path.join(self.work_dir, "log_order_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="restore_op")

        batch = db.get_batch_by_no(new_db, "log_order")
        items = db.get_evidence_items(new_db, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self.assertGreater(len(pending), 0)

        new_log_id = self._review_item(
            new_db, batch["id"], pending[0]["id"],
            "signed", "恢复后复核", "post_op",
        )
        undo_result = db.undo_last_review(new_db, batch["id"], "undo_op")
        self.assertIsNotNone(undo_result)

        history = db.get_review_history(new_db, batch["id"], limit=100)
        history_asc = list(reversed(history))

        for i in range(len(history_asc) - 1):
            id_curr = history_asc[i]["id"]
            id_next = history_asc[i + 1]["id"]
            self.assertLess(
                id_curr, id_next,
                f"日志 #{id_curr} 应排在 #{id_next} 之前（按 id 升序）"
            )

        self.assertEqual(history[0]["action"], "undo")
        self.assertEqual(history[0]["id"], undo_result["undo_log_id"])
        self.assertEqual(history[0]["undo_of_id"], new_log_id)

        actions_asc = [h["action"] for h in history_asc]
        self.assertEqual(actions_asc[-1], "undo")
        self.assertEqual(actions_asc[-2], "review")

    def test_stats_do_not_regress_after_undo(self):
        """恢复后复核再撤销，复核统计不倒退为负数或错乱"""
        batch_id, _, _, _, _ = self._import_batch_isolated("stats_reg", "统计不倒退")
        self._review_item(self.db_path, batch_id, 1, "signed", "s1", "op")
        self._review_item(self.db_path, batch_id, 2, "supplement", "s2", "op")

        snapshot_path = os.path.join(self.work_dir, "stats_reg.json")
        snapshot_mod.save_snapshot(self.db_path, "stats_reg", snapshot_path)

        new_work = os.path.join(self.work_dir, "stats_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path)

        batch = db.get_batch_by_no(new_db, "stats_reg")
        t0, s0, sp0, p0 = db.count_reviewed(new_db, batch["id"])
        self.assertEqual(s0, 1)
        self.assertEqual(sp0, 1)
        self.assertEqual(p0, 1)

        items = db.get_evidence_items(new_db, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self._review_item(
            new_db, batch["id"], pending[0]["id"], "signed", "恢复后复核", "post"
        )
        t1, s1, sp1, p1 = db.count_reviewed(new_db, batch["id"])
        self.assertEqual(s1, s0 + 1)
        self.assertEqual(p1, p0 - 1)

        db.undo_last_review(new_db, batch["id"], "undo_op")
        t2, s2, sp2, p2 = db.count_reviewed(new_db, batch["id"])
        self.assertEqual(t2, t0)
        self.assertEqual(s2, s0)
        self.assertEqual(sp2, sp0)
        self.assertEqual(p2, p0)

        self.assertGreaterEqual(s2, 0)
        self.assertGreaterEqual(sp2, 0)
        self.assertGreaterEqual(p2, 0)
        self.assertEqual(s2 + sp2 + p2, t2)

    def test_recovery_summary_not_corrupted_by_post_ops(self):
        """恢复后复核再撤销，恢复摘要的来源快照、覆盖差异、对账状态不变"""
        old_id, _, _, _, _ = self._import_batch_isolated("corrupt_sum", "旧批次描述")
        self._review_item(self.db_path, old_id, 1, "signed", "旧s1", "old_op")
        self._review_item(self.db_path, old_id, 2, "supplement", "旧s2", "old_op")

        snapshot_path = os.path.join(self.work_dir, "corrupt_sum.json")
        snapshot_mod.save_snapshot(self.db_path, "corrupt_sum", snapshot_path)

        extra_items = db.get_evidence_items(self.db_path, old_id)
        self._review_item(
            self.db_path, old_id, extra_items[0]["id"],
            "supplement", "覆盖前追加", "before_op",
        )

        _, _, _ = snapshot_mod.restore_snapshot(
            self.db_path, snapshot_path, force=True, operator="restore_op"
        )

        sum_after_restore = snapshot_mod.build_recovery_summary(self.db_path, "corrupt_sum")
        src_path_after_restore = sum_after_restore["source_snapshot"]["path"]
        diff_after_restore = sum_after_restore["overwrite_diff"]
        reconciled_after_restore = sum_after_restore["reconciled"]

        self.assertIsNotNone(diff_after_restore)
        self.assertTrue(reconciled_after_restore)
        self.assertEqual(sum_after_restore["post_restore_ops"]["count"], 0)

        batch = db.get_batch_by_no(self.db_path, "corrupt_sum")
        items = db.get_evidence_items(self.db_path, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self._review_item(
            self.db_path, batch["id"], pending[0]["id"],
            "signed", "恢复后新增复核", "post_op",
        )
        db.undo_last_review(self.db_path, batch["id"], "undo_op")

        sum_after_undo = snapshot_mod.build_recovery_summary(self.db_path, "corrupt_sum")

        self.assertEqual(
            sum_after_undo["source_snapshot"]["path"], src_path_after_restore,
            "来源快照路径不应被后续操作改变"
        )
        self.assertEqual(
            sum_after_undo["overwrite_diff"], diff_after_restore,
            "覆盖差异不应被后续操作改变"
        )
        self.assertTrue(
            sum_after_undo["reconciled"],
            f"对账状态应为通过: {sum_after_undo['warnings']}"
        )
        self.assertEqual(
            sum_after_undo["restore_event"]["event_id"],
            sum_after_restore["restore_event"]["event_id"],
            "恢复事件 ID 不应改变"
        )

        ops = sum_after_undo["post_restore_ops"]
        self.assertEqual(ops["review_count"], 1)
        self.assertEqual(ops["undo_count"], 1)
        self.assertEqual(ops["count"], 2)

        self.assertEqual(
            sum_after_undo["review_stats"],
            sum_after_restore["review_stats"],
            "撤销后复核统计应回到恢复后初始状态"
        )

    def test_trace_post_activity_matches_summary_ops_count(self):
        """trace 的 post_restore_activity 数量与恢复摘要 post_restore_ops.count 一致"""
        batch_id, _, _, _, _ = self._import_batch_isolated("trace_match", "trace匹配")
        self._review_item(self.db_path, batch_id, 1, "signed", "snap内1", "snap")

        snapshot_path = os.path.join(self.work_dir, "trace_match.json")
        snapshot_mod.save_snapshot(self.db_path, "trace_match", snapshot_path)

        new_work = os.path.join(self.work_dir, "trace_match_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path, operator="r_op")

        batch = db.get_batch_by_no(new_db, "trace_match")
        items = db.get_evidence_items(new_db, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self._review_item(new_db, batch["id"], pending[0]["id"], "signed", "post1", "p1")
        self._review_item(new_db, batch["id"], pending[1]["id"], "supplement", "post2", "p2")
        db.undo_last_review(new_db, batch["id"], "undo1")

        trace = snapshot_mod.build_trace(new_db, "trace_match")
        summary = snapshot_mod.build_recovery_summary(new_db, "trace_match")

        self.assertEqual(
            len(trace["post_restore_activity"]),
            summary["post_restore_ops"]["count"],
            "trace post_restore_activity 数量应等于恢复摘要 post_restore_ops.count"
        )
        self.assertTrue(trace["modified_after_restore"])
        self.assertEqual(summary["post_restore_ops"]["review_count"], 2)
        self.assertEqual(summary["post_restore_ops"]["undo_count"], 1)
        self.assertEqual(summary["post_restore_ops"]["count"], 3)

        actions = [log["action"] for log in trace["post_restore_activity"]]
        self.assertEqual(actions.count("review"), 2)
        self.assertEqual(actions.count("undo"), 1)

    def test_last_review_log_updates_correctly(self):
        """恢复后复核再撤销，最后一条复核记录正确切换"""
        batch_id, _, _, _, _ = self._import_batch_isolated("last_log", "最后日志")
        self._review_item(self.db_path, batch_id, 1, "signed", "初始复核", "init_op")

        snapshot_path = os.path.join(self.work_dir, "last_log.json")
        snapshot_mod.save_snapshot(self.db_path, "last_log", snapshot_path)

        new_work = os.path.join(self.work_dir, "last_log_work")
        os.makedirs(new_work, exist_ok=True)
        new_db = db.get_db_path(new_work)
        db.init_db(new_db)
        snapshot_mod.restore_snapshot(new_db, snapshot_path)

        batch = db.get_batch_by_no(new_db, "last_log")
        sum0 = snapshot_mod.build_recovery_summary(new_db, "last_log")
        self.assertEqual(sum0["last_review_log"]["new_remark"], "初始复核")
        self.assertEqual(sum0["last_review_log"]["action"], "review")

        items = db.get_evidence_items(new_db, batch["id"])
        pending = [i for i in items if i["review_status"] == "pending"]
        self._review_item(
            new_db, batch["id"], pending[0]["id"],
            "supplement", "恢复后新复核", "post_op",
        )
        sum1 = snapshot_mod.build_recovery_summary(new_db, "last_log")
        self.assertEqual(sum1["last_review_log"]["new_remark"], "恢复后新复核")
        self.assertEqual(sum1["last_review_log"]["new_status"], "supplement")

        undo_result = db.undo_last_review(new_db, batch["id"], "undo_op")
        self.assertIsNotNone(undo_result)

        sum2 = snapshot_mod.build_recovery_summary(new_db, "last_log")
        self.assertEqual(sum2["last_review_log"]["action"], "review")
        self.assertEqual(sum2["last_review_log"]["new_remark"], "初始复核")
        self.assertEqual(sum2["last_review_log"]["new_status"], "signed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
