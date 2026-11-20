import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import OTHER_SESSION_ID, SESSION_ID, create_source_db, create_target_db
from zcode_session_rescue.errors import RescueError
from zcode_session_rescue.planner import build_restore_plan, list_sessions


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = create_source_db(self.root / "source.sqlite")
        self.target = create_target_db(self.root / "target.sqlite")

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self):
        return build_restore_plan(self.source, self.target, SESSION_ID)

    def assert_rescue_code(self, code):
        return self.assertRaises(RescueError)

    def test_list_sessions_returns_metadata_without_message_bodies(self):
        sessions = list_sessions(self.source)
        restored = next(session for session in sessions if session.id == SESSION_ID)
        self.assertEqual(restored.title, "可恢复会话")
        self.assertEqual(restored.message_count, 1)
        self.assertNotIn("你好", str(restored.to_dict()))

    def test_plan_discovers_direct_tables_and_preserves_composite_keys(self):
        plan = self.plan()
        self.assertEqual(plan.rows["session"][0].key_values(), {"id": SESSION_ID})
        self.assertEqual(plan.rows["todo"][0].key_values(), {"session_id": SESSION_ID, "position": 0})
        self.assertEqual(plan.rows["turn_usage"][0].key_values(), {"session_id": SESSION_ID, "turn_id": ""})
        self.assertIn("custom_note", plan.rows)
        self.assertNotIn("session_runtime_presence", plan.rows)
        self.assertIn("session_runtime_presence", plan.skipped_tables)
        self.assertLess(plan.table_order.index("session"), plan.table_order.index("message"))
        self.assertLess(plan.table_order.index("message"), plan.table_order.index("part"))
        self.assertEqual(len(plan.schema_fingerprint), 64)

    def test_schema_mismatch_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("ALTER TABLE custom_note ADD COLUMN target_only TEXT")
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "schema_incompatible")

    def test_existing_target_session_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("INSERT INTO session VALUES (?, NULL, 'already', 0, 0)", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "target_session_exists")

    def test_target_orphan_rows_are_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute(
            "INSERT INTO message VALUES ('orphan', ?, 0, 'orphan')",
            (SESSION_ID,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "target_orphan_rows")

    def test_global_primary_key_conflict_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute(
            "INSERT INTO custom_note VALUES ('note_1', ?, 'do not ignore')",
            (OTHER_SESSION_ID,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "target_primary_key_conflict")

    def test_target_semantic_reverse_reference_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute(
            "INSERT INTO semantic_reference VALUES ('future-owner', ?, 'message_1')",
            (OTHER_SESSION_ID,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "target_reverse_reference")

    def test_workflow_association_is_rejected(self):
        conn = sqlite3.connect(self.source)
        conn.execute("INSERT INTO workflow_run VALUES ('run_1', ?, 'workflow')", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "unsupported_association")

    def test_dwf_association_is_rejected(self):
        conn = sqlite3.connect(self.source)
        conn.execute("INSERT INTO dwf_run VALUES ('dwf_1', ?)", (SESSION_ID,))
        conn.execute("INSERT INTO dwf_actor VALUES (1, 'dwf_1', ?, 'actor')", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "unsupported_association")

    def test_missing_parent_session_is_rejected(self):
        conn = sqlite3.connect(self.source)
        conn.execute("UPDATE session SET parent_id='missing-parent' WHERE id=?", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "unsupported_parent")

    def test_unknown_direct_table_without_primary_key_is_rejected(self):
        conn = sqlite3.connect(self.source)
        conn.execute("CREATE TABLE unknown_note (session_id TEXT, body TEXT)")
        conn.execute("INSERT INTO unknown_note VALUES (?, 'unknown')", (SESSION_ID,))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "unsupported_table")

    def test_purged_source_session_is_rejected(self):
        conn = sqlite3.connect(self.source)
        conn.execute("INSERT INTO session_purge VALUES (?, ?, 'done')", (SESSION_ID, SESSION_ID))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "unsupported_association")

    def test_target_purge_marker_is_rejected(self):
        conn = sqlite3.connect(self.target)
        conn.execute("INSERT INTO session_purge VALUES (?, ?, 'done')", (SESSION_ID, SESSION_ID))
        conn.commit()
        conn.close()
        with self.assertRaises(RescueError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "target_reverse_reference")

    def test_non_zcode_database_fails_concisely(self):
        empty = self.root / "empty.sqlite"
        sqlite3.connect(empty).close()
        with self.assertRaises(RescueError) as caught:
            list_sessions(empty)
        self.assertEqual(caught.exception.code, "invalid_source")


if __name__ == "__main__":
    unittest.main()
