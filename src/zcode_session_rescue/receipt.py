from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

from .errors import RescueError


def decode_typed_value(payload: dict[str, object]) -> object:
    kind = payload.get("type")
    value = payload.get("value")
    try:
        if kind == "null":
            return None
        if kind == "integer":
            return int(str(value))
        if kind == "float":
            return float.fromhex(str(value))
        if kind == "text":
            return str(value)
        if kind == "blob":
            return base64.b64decode(str(value), validate=True)
    except (ValueError, TypeError) as error:
        raise RescueError("Receipt contains an invalid typed value", "receipt_invalid") from error
    raise RescueError(f"Receipt contains an unknown value type: {kind}", "receipt_invalid")


def restrict_to_owner(path: str | Path, mode: int = 0o600) -> None:
    """On POSIX, keep backups and receipts readable only by their owner."""
    if os.name != "posix":
        return
    os.chmod(path, mode)


def atomic_write_json(path: str | Path, payload: dict[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=destination.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        restrict_to_owner(destination)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def read_json(path: str | Path) -> dict[str, object]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise RescueError(f"Cannot read receipt {source}: {error}", "receipt_invalid") from error
    if not isinstance(payload, dict):
        raise RescueError(f"Receipt must contain a JSON object: {source}", "receipt_invalid")
    return payload
