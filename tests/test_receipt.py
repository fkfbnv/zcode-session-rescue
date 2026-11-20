import json
import tempfile
import unittest
from pathlib import Path

from zcode_session_rescue.receipt import atomic_write_json, read_json
from zcode_session_rescue.errors import RescueError


class ReceiptTests(unittest.TestCase):
    def test_atomic_json_round_trip_preserves_unicode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "回 执.json"
            payload = {"状态": "准备", "number": 0, "nested": {"ok": True}}
            atomic_write_json(path, payload)
            self.assertEqual(read_json(path), payload)
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_invalid_json_raises_concise_rescue_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(RescueError) as caught:
                read_json(path)
            self.assertEqual(caught.exception.code, "receipt_invalid")


if __name__ == "__main__":
    unittest.main()
