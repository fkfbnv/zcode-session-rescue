import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.fixtures import SESSION_ID, create_source_db, create_target_db
from zcode_session_rescue.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = create_source_db(self.root / "旧 库#%.sqlite")
        self.target = create_target_db(self.root / "目标 库.sqlite")
        self.operations = self.root / "operations"

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def restore_args(self):
        return (
            "restore",
            "--source",
            str(self.source),
            "--target",
            str(self.target),
            "--session",
            SESSION_ID,
            "--operations-dir",
            str(self.operations),
            "--json",
        )

    def test_help_lists_all_commands(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "zcode_session_rescue", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("list", "check", "restore", "undo"):
            self.assertIn(command, result.stdout)

    def test_list_json_returns_metadata_not_bodies(self):
        code, output, error = self.invoke("list", "--source", str(self.source), "--json")
        self.assertEqual(code, 0, error)
        payload = json.loads(output)
        restored = next(session for session in payload["sessions"] if session["id"] == SESSION_ID)
        self.assertEqual(restored["message_count"], 1)
        self.assertNotIn("你好", output)

    def test_check_and_restore_default_to_dry_run(self):
        code, output, error = self.invoke(
            "check",
            "--source",
            str(self.source),
            "--target",
            str(self.target),
            "--session",
            SESSION_ID,
            "--json",
        )
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["applied"])
        code, output, error = self.invoke(*self.restore_args())
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["applied"])
        self.assertEqual(json.loads(output)["plan"]["session_id"], SESSION_ID)
        self.assertFalse(self.operations.exists())
        conn = sqlite3.connect(self.target)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)
        conn.close()

    def test_apply_restore_and_undo_return_machine_readable_results(self):
        code, output, error = self.invoke(*self.restore_args(), "--apply")
        self.assertEqual(code, 0, error)
        restored = json.loads(output)
        self.assertTrue(restored["applied"])
        self.assertEqual(restored["status"], "completed")
        receipt = restored["receipt_path"]
        code, output, error = self.invoke("undo", "--receipt", receipt, "--apply", "--json")
        self.assertEqual(code, 0, error)
        undone = json.loads(output)
        self.assertTrue(undone["applied"])
        self.assertEqual(undone["status"], "undone")

    def test_human_output_includes_timestamps_and_skipped_tables(self):
        code, output, error = self.invoke("list", "--source", str(self.source))
        self.assertEqual(code, 0, error)
        self.assertIn("created=10", output)
        self.assertIn("updated=20", output)
        code, output, error = self.invoke(
            "check",
            "--source",
            str(self.source),
            "--target",
            str(self.target),
            "--session",
            SESSION_ID,
        )
        self.assertEqual(code, 0, error)
        self.assertIn("session_runtime_presence", output)

    def test_expected_error_is_concise_and_has_exit_one(self):
        empty = self.root / "empty.sqlite"
        sqlite3.connect(empty).close()
        code, output, error = self.invoke("list", "--source", str(empty), "--json")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("invalid_source", error)
        self.assertNotIn("Traceback", error)

    def test_missing_required_argument_has_usage_exit_two(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "zcode_session_rescue", "restore"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_filesystem_error_reports_io_error(self):
        error = PermissionError(13, "Permission denied", "locked.sqlite")
        with mock.patch("zcode_session_rescue.cli.list_sessions", side_effect=error):
            code, output, stderr = self.invoke("list", "--source", "locked.sqlite")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("io_error", stderr)
        self.assertIn("locked.sqlite", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_filesystem_error_json_shape(self):
        error = OSError(28, "No space left on device", "target.sqlite")
        with mock.patch("zcode_session_rescue.cli.list_sessions", side_effect=error):
            code, _, stderr = self.invoke("list", "--source", "target.sqlite", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "io_error")
        self.assertIn("No space left on device", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
