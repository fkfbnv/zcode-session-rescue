import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import OTHER_SESSION_ID, SESSION_ID, create_source_db, create_target_db
from zcode_session_rescue.errors import RescueError
from zcode_session_rescue.operations import restore_session, undo_restore


def database_rows(path):
    conn = sqlite3.connect(path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    result = {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}
    conn.close()
    return result


class UndoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = create_source_db(self.root / "source.sqlite")
        self.target = create_target_db(self.root / "target.sqlite")
        self.before = database_rows(self.target)
        self.restore = restore_session(
            self.source,
            self.target,
            SESSION_ID,
            self.root / "operations",
            apply=True,
        )
        self.receipt_path = self.restore.receipt_path

    def tearDown(self):
        self.temporary.cleanup()

    def receipt(self):
        return json.loads(self.receipt_path.read_text(encoding="utf-8"))

    def set_receipt_status(self, status):
        receipt = self.receipt()
        receipt["status"] = status
        self.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    def test_dry_run_undo_changes_neither_database_nor_receipt(self):
        result = undo_restore(self.receipt_path)
        self.assertFalse(result.applied)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(self.receipt()["status"], "completed")
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session WHERE id=?", (SESSION_ID,)).fetchone()[0], 1)
        conn.close()

    def test_apply_undo_restores_exact_original_rows(self):
        result = undo_restore(self.receipt_path, apply=True)
        self.assertTrue(result.applied)
        self.assertEqual(result.status, "undone")
        self.assertEqual(self.receipt()["status"], "undone")
        self.assertEqual(database_rows(self.target), self.before)

    def test_repeated_undo_is_rejected(self):
        undo_restore(self.receipt_path, apply=True)
        with self.assertRaises(RescueError) as caught:
            undo_restore(self.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "undo_already_completed")

    def test_changed_restored_row_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("UPDATE message SET data='continued' WHERE id='message_1'")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(self.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "undo_row_changed")
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT data FROM message WHERE id='message_1'").fetchone()[0], "continued")
        conn.close()

    def test_partially_missing_rows_are_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("DELETE FROM part WHERE id='part_1'")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(self.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "undo_rows_missing")

    def test_new_declared_foreign_key_dependency_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("INSERT INTO custom_note VALUES ('later-note', ?, 'new')", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(self.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "undo_new_dependency")

    def test_new_semantic_dependency_without_foreign_key_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute(
            "INSERT INTO semantic_reference VALUES ('later-ref', ?, 'message_1')",
            (OTHER_SESSION_ID,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(self.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "undo_new_dependency")

    def test_schema_drift_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("CREATE TABLE later_table (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(self.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "schema_incompatible")

    def test_prepared_receipt_can_undo_a_committed_restore(self):
        self.set_receipt_status("prepared")
        result = undo_restore(self.receipt_path, apply=True)
        self.assertEqual(result.status, "undone")
        self.assertEqual(database_rows(self.target), self.before)

    def test_undo_prepared_with_all_rows_deleted_only_finalizes_receipt(self):
        self.set_receipt_status("undo_prepared")
        conn = sqlite3.connect(self.target)
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in self.receipt()["table_order"]:
            if table == "session":
                conn.execute("DELETE FROM session WHERE id=?", (SESSION_ID,))
            else:
                conn.execute(f'DELETE FROM "{table}" WHERE session_id=?', (SESSION_ID,))
        conn.commit()
        conn.close()
        result = undo_restore(self.receipt_path, apply=True)
        self.assertFalse(result.applied)
        self.assertEqual(result.status, "undone")
        self.assertEqual(self.receipt()["status"], "undone")
        self.assertEqual(database_rows(self.target), self.before)


if __name__ == "__main__":
    unittest.main()
