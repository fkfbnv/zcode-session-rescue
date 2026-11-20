import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import OTHER_SESSION_ID, SESSION_ID, create_source_db, create_target_db
from zcode_session_rescue.errors import RescueError
from zcode_session_rescue.operations import restore_session, undo_restore
from zcode_session_rescue.planner import (
    _check_target_conflicts,
    build_restore_plan,
    inspect_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = create_source_db(self.root / "source.sqlite")
        self.target = create_target_db(self.root / "target.sqlite")
        self.operations = self.root / "operations"

    def tearDown(self):
        self.temporary.cleanup()

    def execute_both(self, sql, parameters=()):
        for path in (self.source, self.target):
            conn = sqlite3.connect(path)
            conn.execute(sql, parameters)
            conn.commit()
            conn.close()

    def restore(self):
        return restore_session(self.source, self.target, SESSION_ID, self.operations, apply=True)

    def test_trigger_is_rejected_before_restore(self):
        conn = sqlite3.connect(self.target)
        conn.executescript(
            "CREATE TABLE audit_log (entry TEXT); "
            "CREATE TRIGGER session_insert_trigger AFTER INSERT ON session BEGIN "
            "INSERT INTO audit_log VALUES ('changed'); END;"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertEqual(caught.exception.code, "unsupported_trigger")

    def test_official_zcode_trigger_does_not_block_planning(self):
        sql = (
            "CREATE TRIGGER message_sequence_autofill "
            "after insert on message when new.sequence is null begin "
            "update message set sequence = ("
            "select coalesce(max(sequence), -1) + 1 from message where session_id = new.session_id"
            ") where id = new.id; end"
        )
        self.execute_both(sql)
        plan = build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertIn("message", plan.rows)

    def test_restore_closure_rejects_parent_row_from_another_session(self):
        conn = sqlite3.connect(self.source)
        conn.execute(
            "INSERT INTO part VALUES ('part_wrong_parent', 'message_existing', ?, 'wrong')",
            (SESSION_ID,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertEqual(caught.exception.code, "unclosed_reference")

    def test_source_child_session_is_rejected(self):
        conn = sqlite3.connect(self.source)
        conn.execute("INSERT INTO session VALUES ('child-session', ?, 'child', 1, 2)", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertEqual(caught.exception.code, "unsupported_parent")

    def test_autoincrement_table_is_rejected(self):
        self.execute_both(
            "CREATE TABLE auto_note (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, note TEXT)"
        )
        conn = sqlite3.connect(self.source)
        conn.execute("INSERT INTO auto_note(session_id, note) VALUES (?, 'auto')", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertEqual(caught.exception.code, "unsupported_table")

    def test_null_primary_key_component_is_rejected(self):
        self.execute_both(
            "CREATE TABLE nullable_key (session_id TEXT, position INTEGER, data TEXT, "
            "PRIMARY KEY(session_id, position))"
        )
        conn = sqlite3.connect(self.source)
        conn.execute("INSERT INTO nullable_key VALUES (?, NULL, 'null')", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertEqual(caught.exception.code, "unsupported_table")

    def test_cascade_foreign_key_is_rejected_during_planning(self):
        self.execute_both(
            "CREATE TABLE cascade_reference (id TEXT PRIMARY KEY, message_id TEXT, "
            "FOREIGN KEY(message_id) REFERENCES message(id) ON DELETE CASCADE)"
        )
        with self.assertRaises(RescueError) as caught:
            build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertEqual(caught.exception.code, "unsupported_foreign_key_action")

    def test_official_cascade_does_not_block_planning(self):
        self.execute_both(
            "CREATE TABLE session_target (session_id TEXT PRIMARY KEY, objective TEXT, "
            "FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE)"
        )
        plan = build_restore_plan(self.source, self.target, SESSION_ID)
        self.assertIn("session", plan.rows)

    def test_column_reordering_still_allows_exact_undo(self):
        conn = sqlite3.connect(self.target)
        conn.execute("DROP TABLE custom_note")
        conn.execute(
            "CREATE TABLE custom_note (note TEXT, session_id TEXT, id TEXT PRIMARY KEY, "
            "FOREIGN KEY(session_id) REFERENCES session(id))"
        )
        conn.commit()
        conn.close()
        restored = self.restore()
        result = undo_restore(restored.receipt_path, apply=True)
        self.assertEqual(result.status, "undone")

    def test_semantic_part_reference_without_foreign_key_blocks_undo(self):
        self.execute_both("CREATE TABLE part_reference (id TEXT PRIMARY KEY, part_id TEXT)")
        restored = self.restore()
        conn = sqlite3.connect(self.target)
        conn.execute("INSERT INTO part_reference VALUES ('new-part-ref', 'part_1')")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(restored.receipt_path, apply=False)
        self.assertEqual(caught.exception.code, "undo_new_dependency")

    def test_unique_non_primary_key_reference_blocks_undo(self):
        self.execute_both("ALTER TABLE custom_note ADD COLUMN alias TEXT")
        self.execute_both("CREATE UNIQUE INDEX custom_note_alias_idx ON custom_note(alias)")
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE custom_note SET alias='unique-alias' WHERE id='note_1'")
        conn.commit()
        conn.close()
        self.execute_both(
            "CREATE TABLE unique_reference (id TEXT PRIMARY KEY, note_alias TEXT, "
            "FOREIGN KEY(note_alias) REFERENCES custom_note(alias))"
        )
        restored = self.restore()
        conn = sqlite3.connect(self.target)
        conn.execute("INSERT INTO unique_reference VALUES ('new-unique-ref', 'unique-alias')")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(restored.receipt_path, apply=False)
        self.assertEqual(caught.exception.code, "undo_new_dependency")

    def test_implicit_primary_key_reference_blocks_undo(self):
        self.execute_both(
            "CREATE TABLE implicit_reference (id TEXT PRIMARY KEY, message_id TEXT, "
            "FOREIGN KEY(message_id) REFERENCES message)"
        )
        restored = self.restore()
        conn = sqlite3.connect(self.target)
        conn.execute("INSERT INTO implicit_reference VALUES ('new-implicit-ref', 'message_1')")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            undo_restore(restored.receipt_path, apply=False)
        self.assertEqual(caught.exception.code, "undo_new_dependency")

    def test_malformed_receipt_table_order_is_rejected_before_deletion(self):
        restored = self.restore()
        receipt = json.loads(restored.receipt_path.read_text(encoding="utf-8"))
        receipt["table_order"].remove("part")
        restored.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(RescueError) as caught:
            undo_restore(restored.receipt_path, apply=True)
        self.assertEqual(caught.exception.code, "receipt_invalid")
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session WHERE id=?", (SESSION_ID,)).fetchone()[0], 1)
        conn.close()

    def test_conflict_checks_use_bounded_query_count_for_large_session(self):
        conn = sqlite3.connect(self.source)
        for index in range(120):
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, 'body')",
                (f"large-message-{index}", SESSION_ID, index),
            )
            conn.execute(
                "INSERT INTO part VALUES (?, ?, ?, 'part')",
                (f"large-part-{index}", f"large-message-{index}", SESSION_ID),
            )
        conn.commit()
        conn.close()
        plan = build_restore_plan(self.source, self.target, SESSION_ID)
        conn = sqlite3.connect(self.target)
        statements = []
        conn.set_trace_callback(statements.append)
        _check_target_conflicts(conn, inspect_schema(conn), plan.rows, SESSION_ID)
        conn.close()
        self.assertLess(len(statements), 150)

    def test_concurrent_undo_cannot_restore_completed_receipt_state(self):
        restored = self.restore()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        command = [
            sys.executable,
            "-m",
            "zcode_session_rescue",
            "undo",
            "--receipt",
            str(restored.receipt_path),
            "--apply",
            "--json",
        ]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        first.communicate(timeout=120)
        second.communicate(timeout=120)
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 1])
        receipt = json.loads(restored.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "undone")


if __name__ == "__main__":
    unittest.main()
