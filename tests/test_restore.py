import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import OTHER_SESSION_ID, SESSION_ID, create_source_db, create_target_db
from zcode_session_rescue.errors import RescueError
from zcode_session_rescue.operations import restore_session
from zcode_session_rescue.planner import build_restore_plan


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = create_source_db(self.root / "source.sqlite")
        self.target = create_target_db(self.root / "target.sqlite")
        self.operations = self.root / "operations"

    def tearDown(self):
        self.temporary.cleanup()

    def restore(self, apply=False):
        return restore_session(
            self.source,
            self.target,
            SESSION_ID,
            self.operations,
            apply=apply,
        )

    def test_default_dry_run_changes_nothing_and_creates_no_operation(self):
        result = self.restore()
        self.assertFalse(result.applied)
        self.assertEqual(result.status, "dry_run")
        self.assertFalse(self.operations.exists())
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)
        conn.close()

    def test_apply_restores_exact_rows_and_preserves_other_sessions(self):
        result = self.restore(apply=True)
        self.assertTrue(result.applied)
        self.assertEqual(result.status, "completed")
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 2)
        self.assertEqual(
            conn.execute("SELECT data FROM message WHERE id='message_1'").fetchone()[0],
            '{"text":"你好"}',
        )
        self.assertEqual(
            conn.execute("SELECT data FROM part WHERE id='part_1'").fetchone()[0],
            b"\x00\x01\xff",
        )
        self.assertEqual(
            conn.execute("SELECT data FROM message WHERE id='message_existing'").fetchone()[0],
            "keep",
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_runtime_presence WHERE session_id=?", (SESSION_ID,)).fetchone()[0],
            0,
        )
        conn.close()

    def test_backup_and_receipt_are_valid_and_do_not_contain_bodies(self):
        result = self.restore(apply=True)
        backup = sqlite3.connect(result.backup_path)
        self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)
        backup.close()
        text = result.receipt_path.read_text(encoding="utf-8")
        receipt = json.loads(text)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["operation_id"], result.operation_id)
        self.assertNotIn("你好", text)
        self.assertNotIn("零号位置", text)
        self.assertGreaterEqual(len(receipt["rows"]), 7)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in receipt["rows"]))

    def test_conflict_introduced_after_planning_rolls_everything_back(self):
        original = build_restore_plan

        def plan_then_conflict(*args):
            plan = original(*args)
            conn = sqlite3.connect(self.target)
            conn.execute(
                "INSERT INTO custom_note VALUES ('note_1', ?, 'conflict')",
                (OTHER_SESSION_ID,),
            )
            conn.commit()
            conn.close()
            return plan

        with patch("zcode_session_rescue.operations.build_restore_plan", side_effect=plan_then_conflict):
            with self.assertRaises(RescueError) as caught:
                self.restore(apply=True)
        self.assertEqual(caught.exception.code, "target_primary_key_conflict")
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session WHERE id=?", (SESSION_ID,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM custom_note").fetchone()[0], 1)
        conn.close()
        receipts = list(self.operations.glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertNotEqual(json.loads(receipts[0].read_text(encoding="utf-8"))["status"], "completed")

    def test_schema_drift_after_planning_is_rejected(self):
        original = build_restore_plan

        def plan_then_drift(*args):
            plan = original(*args)
            conn = sqlite3.connect(self.target)
            conn.execute("ALTER TABLE custom_note ADD COLUMN later TEXT")
            conn.commit()
            conn.close()
            return plan

        with patch("zcode_session_rescue.operations.build_restore_plan", side_effect=plan_then_drift):
            with self.assertRaises(RescueError) as caught:
                self.restore(apply=True)
        self.assertEqual(caught.exception.code, "schema_incompatible")
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session WHERE id=?", (SESSION_ID,)).fetchone()[0], 0)
        conn.close()

    def test_insert_uses_staged_source_snapshot_not_later_source_changes(self):
        original = build_restore_plan

        def plan_then_change_source(*args):
            plan = original(*args)
            conn = sqlite3.connect(self.source)
            conn.execute("UPDATE custom_note SET note='later change' WHERE id='note_1'")
            conn.commit()
            conn.close()
            return plan

        with patch("zcode_session_rescue.operations.build_restore_plan", side_effect=plan_then_change_source):
            result = self.restore(apply=True)
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT note FROM custom_note WHERE id='note_1'").fetchone()[0], "动态直属表")
        conn.close()
        self.assertEqual(json.loads(result.receipt_path.read_text(encoding="utf-8"))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
