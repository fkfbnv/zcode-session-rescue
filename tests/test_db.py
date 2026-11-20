import sqlite3
import tempfile
import unittest
from pathlib import Path

from zcode_session_rescue.db import (
    RowSnapshot,
    connect_existing,
    connect_readonly,
    inspect_schema,
    row_digest,
)
from zcode_session_rescue.errors import RescueError
from tests.fixtures import create_source_db


class DatabaseTests(unittest.TestCase):
    def test_readonly_connection_handles_special_path_and_rejects_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = create_source_db(Path(tmp) / "旧 库#%.sqlite")
            conn = connect_readonly(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 2)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("INSERT INTO session VALUES ('x', NULL, 'x', 0, 0)")
            finally:
                conn.close()

    def test_connect_existing_does_not_create_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.sqlite"
            with self.assertRaises(RescueError) as caught:
                connect_existing(path)
            self.assertEqual(caught.exception.code, "target_missing")
            self.assertFalse(path.exists())

    def test_schema_introspection_preserves_composite_pk_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_readonly(create_source_db(Path(tmp) / "source.sqlite"))
            try:
                schema = inspect_schema(conn)
            finally:
                conn.close()
            self.assertEqual(schema["session"].primary_key, ("id",))
            self.assertEqual(schema["todo"].primary_key, ("session_id", "position"))
            self.assertEqual(schema["todo"].columns[1].not_null, False)
            self.assertIn(("message", "message_id", "id"), [foreign_key[:3] for foreign_key in schema["part"].foreign_keys])
            self.assertEqual(schema["part"].foreign_keys[0].on_delete, "NO ACTION")
            self.assertIn("PRIMARY KEY", schema["session"].sql)

    def test_row_digest_is_stable_and_type_sensitive(self):
        values = [None, 1, "1", sqlite3.Binary(b"\x00\xff"), 1.0, -0.0]
        self.assertEqual(row_digest(values), row_digest(values))
        self.assertNotEqual(row_digest([1]), row_digest(["1"]))
        self.assertNotEqual(row_digest([0.0]), row_digest([-0.0]))
        self.assertNotEqual(row_digest([sqlite3.Binary(b"\x00")]), row_digest(["\x00"]))

    def test_row_snapshot_receipt_has_key_and_hash_but_not_body(self):
        values = ("message_1", "session_restore", 11, "秘密正文")
        snapshot = RowSnapshot.from_values(
            "message", ("id", "session_id", "time_created", "data"), ("id",), values
        )
        receipt = snapshot.to_receipt()
        self.assertEqual(receipt["primary_key"], {"id": {"type": "text", "value": "message_1"}})
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertNotIn("秘密正文", str(receipt))
        self.assertEqual(snapshot.values, values)


if __name__ == "__main__":
    unittest.main()
