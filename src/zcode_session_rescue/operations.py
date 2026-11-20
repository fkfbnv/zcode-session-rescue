from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import RowSnapshot, TableSchema, connect_existing, connect_readonly, inspect_schema, quote_identifier
from .errors import RescueError
from .locking import operation_lock
from .planner import (
    RestorePlan,
    _check_target_conflicts,
    build_restore_plan,
    database_fingerprint,
)
from .receipt import atomic_write_json, decode_typed_value, read_json
from .relations import (
    foreign_key_groups,
    query_column_values,
    query_group_references,
    query_key_tuples,
    restored_id_values,
)

RECEIPT_FORMAT_VERSION = 1


@dataclass
class OperationResult:
    action: str
    status: str
    applied: bool
    operation_id: str | None
    operation_dir: Path | None
    backup_path: Path | None
    receipt_path: Path | None
    row_counts: dict[str, int]
    plan: RestorePlan | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": self.action,
            "status": self.status,
            "applied": self.applied,
            "operation_id": self.operation_id,
            "operation_dir": str(self.operation_dir) if self.operation_dir else None,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
            "row_counts": self.row_counts,
        }
        if self.plan is not None:
            payload["plan"] = self.plan.to_dict()
        return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _operation_directory(parent: Path, action: str, operation_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = parent / f"{action}-{stamp}-{operation_id}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def backup_database(target: str | Path, destination: str | Path) -> Path:
    backup_path = Path(destination)
    if backup_path.exists():
        raise RescueError(f"Backup already exists: {backup_path}", "backup_exists")
    source = connect_readonly(target)
    backup: sqlite3.Connection | None = None
    try:
        backup = sqlite3.connect(backup_path)
        source.backup(backup)
        result = backup.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RescueError("Backup failed SQLite quick_check", "backup_invalid")
        return backup_path
    except sqlite3.Error as error:
        raise RescueError(f"Cannot back up target database: {error}", "backup_failed") from error
    finally:
        if backup is not None:
            backup.close()
        source.close()


def _receipt_rows(plan: RestorePlan) -> list[dict[str, object]]:
    return [
        snapshot.to_receipt()
        for table in plan.table_order
        for snapshot in plan.rows[table]
    ]


def _insert_plan(conn: sqlite3.Connection, plan: RestorePlan) -> None:
    for table in plan.table_order:
        snapshots = plan.rows[table]
        if not snapshots:
            continue
        columns = plan.source_schema[table].column_names
        names = ", ".join(quote_identifier(column) for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {quote_identifier(table)} ({names}) VALUES ({placeholders})"
        conn.executemany(sql, [snapshot.values for snapshot in snapshots])
    violation = conn.execute("PRAGMA foreign_key_check").fetchone()
    if violation:
        raise RescueError(f"Foreign key check failed: {violation}", "foreign_key_violation")


def _verify_inserted_rows(conn: sqlite3.Connection, plan: RestorePlan) -> None:
    for table_name, snapshots in plan.rows.items():
        table = plan.target_schema[table_name]
        keys = {
            tuple(snapshot.key_values()[column] for column in snapshot.primary_key)
            for snapshot in snapshots
        }
        current_rows = query_key_tuples(conn, table, keys)
        positions = {column: index for index, column in enumerate(table.column_names)}
        current_by_key = {
            tuple(row[positions[column]] for column in table.primary_key): row
            for row in current_rows
        }
        for snapshot in snapshots:
            key = tuple(snapshot.key_values()[column] for column in snapshot.primary_key)
            current = current_by_key.get(key)
            if current is None:
                raise RescueError(f"Inserted row is missing in {table_name}", "restore_verification_failed")
            verified = RowSnapshot.from_values(table_name, table.column_names, table.primary_key, current)
            if verified.sha256 != snapshot.sha256:
                raise RescueError(f"Inserted row differs in {table_name}", "restore_verification_failed")


def _apply_restore(plan: RestorePlan, operations_dir: str | Path) -> OperationResult:
    operation_id = uuid.uuid4().hex
    directory = _operation_directory(Path(operations_dir), "restore", operation_id)
    backup_path = backup_database(plan.target, directory / "target-before.sqlite")
    receipt_path = directory / "receipt.json"
    receipt: dict[str, object] = {
        "format_version": RECEIPT_FORMAT_VERSION,
        "operation_id": operation_id,
        "action": "restore",
        "status": "prepared",
        "created_at": _utc_now(),
        "source": str(plan.source),
        "target": str(plan.target),
        "session_id": plan.session_id,
        "backup_path": str(backup_path),
        "target_schema_fingerprint": plan.schema_fingerprint,
        "source_migrations": plan.source_migrations,
        "target_migrations": plan.target_migrations,
        "table_order": list(plan.table_order),
        "row_counts": plan.row_counts(),
        "rows": _receipt_rows(plan),
    }
    atomic_write_json(receipt_path, receipt)

    target_conn = connect_existing(plan.target)
    try:
        target_conn.execute("BEGIN IMMEDIATE")
        current_schema = inspect_schema(target_conn)
        if database_fingerprint(current_schema) != plan.schema_fingerprint:
            raise RescueError("Target schema changed after planning", "schema_incompatible")
        _check_target_conflicts(target_conn, current_schema, plan.rows, plan.session_id)
        _insert_plan(target_conn, plan)
        _verify_inserted_rows(target_conn, plan)
        target_conn.commit()
    except (RescueError, sqlite3.Error) as error:
        if target_conn.in_transaction:
            target_conn.rollback()
        receipt["status"] = "failed"
        receipt["failed_at"] = _utc_now()
        receipt["error"] = str(error)
        atomic_write_json(receipt_path, receipt)
        if isinstance(error, RescueError):
            raise
        raise RescueError(f"Restore failed and was rolled back: {error}", "restore_failed") from error
    finally:
        target_conn.close()

    receipt["status"] = "completed"
    receipt["completed_at"] = _utc_now()
    atomic_write_json(receipt_path, receipt)
    return OperationResult(
        "restore", "completed", True, operation_id, directory, backup_path, receipt_path, plan.row_counts()
    )


def restore_session(
    source: str | Path,
    target: str | Path,
    session_id: str,
    operations_dir: str | Path,
    apply: bool = False,
) -> OperationResult:
    plan = build_restore_plan(source, target, session_id)
    if not apply:
        return OperationResult(
            "restore", "dry_run", False, None, None, None, None, plan.row_counts(), plan
        )
    with operation_lock(plan.target):
        return _apply_restore(plan, operations_dir)


def _receipt_key(row: dict[str, object]) -> dict[str, object]:
    table = row.get("table")
    primary_key = row.get("primary_key")
    digest = row.get("sha256")
    if not isinstance(table, str) or not isinstance(primary_key, dict) or not primary_key:
        raise RescueError("Receipt contains an invalid row entry", "receipt_invalid")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RescueError("Receipt contains an invalid row hash", "receipt_invalid")
    key: dict[str, object] = {}
    for column, encoded in primary_key.items():
        if not isinstance(column, str) or not isinstance(encoded, dict):
            raise RescueError("Receipt contains an invalid primary key", "receipt_invalid")
        value = decode_typed_value(encoded)
        if value is None:
            raise RescueError("Receipt contains a NULL primary key", "receipt_invalid")
        key[column] = value
    return key


def _validate_receipt(receipt: dict[str, object]) -> None:
    if receipt.get("format_version") != RECEIPT_FORMAT_VERSION or receipt.get("action") != "restore":
        raise RescueError("Unsupported receipt format", "receipt_invalid")
    for field in ("operation_id", "session_id", "target", "target_schema_fingerprint"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise RescueError(f"Receipt is missing {field}", "receipt_invalid")
    rows = receipt.get("rows")
    order = receipt.get("table_order")
    if not isinstance(rows, list) or not rows or not isinstance(order, list) or not order:
        raise RescueError("Receipt has no rows or table order", "receipt_invalid")
    if len(set(order)) != len(order) or not all(isinstance(table, str) for table in order):
        raise RescueError("Receipt table order is invalid", "receipt_invalid")
    row_tables = [row.get("table") if isinstance(row, dict) else None for row in rows]
    if set(row_tables) != set(order) or None in row_tables:
        raise RescueError("Receipt rows and table order differ", "receipt_invalid")
    unique_rows: set[str] = set()
    computed_counts: dict[str, int] = {}
    for row in rows:
        key = _receipt_key(row)
        identity = json.dumps([row["table"], row["primary_key"]], sort_keys=True, ensure_ascii=False)
        if identity in unique_rows:
            raise RescueError("Receipt contains duplicate rows", "receipt_invalid")
        unique_rows.add(identity)
        computed_counts[row["table"]] = computed_counts.get(row["table"], 0) + 1
    if receipt.get("row_counts") != computed_counts:
        raise RescueError("Receipt row counts do not match its rows", "receipt_invalid")
    session_rows = [row for row in rows if row["table"] == "session"]
    if len(session_rows) != 1 or _receipt_key(session_rows[0]).get("id") != receipt["session_id"]:
        raise RescueError("Receipt does not identify exactly one restored session", "receipt_invalid")


def _fetch_receipt_row(
    conn: sqlite3.Connection,
    schema: dict[str, TableSchema],
    row: dict[str, object],
) -> RowSnapshot | None:
    table_name = str(row["table"])
    table = schema.get(table_name)
    if table is None:
        raise RescueError(f"Target is missing receipt table {table_name}", "schema_incompatible")
    key = _receipt_key(row)
    if set(key) != set(table.primary_key):
        raise RescueError(f"Receipt primary key differs for {table_name}", "schema_incompatible")
    columns = ", ".join(quote_identifier(column) for column in table.column_names)
    where = " AND ".join(f"{quote_identifier(column)} = ?" for column in table.primary_key)
    values = tuple(key[column] for column in table.primary_key)
    current = conn.execute(
        f"SELECT {columns} FROM {quote_identifier(table_name)} WHERE {where} LIMIT 1",
        values,
    ).fetchone()
    if current is None:
        return None
    snapshot = RowSnapshot.from_values(table_name, table.column_names, table.primary_key, current)
    if snapshot.sha256 != row["sha256"]:
        raise RescueError(f"Restored row changed in {table_name}", "undo_row_changed")
    return snapshot


def _read_undo_rows(
    conn: sqlite3.Connection,
    schema: dict[str, TableSchema],
    rows: list[dict[str, object]],
) -> tuple[str, dict[str, tuple[RowSnapshot, ...]]]:
    missing = 0
    current_rows: dict[str, list[RowSnapshot]] = {}
    for row in rows:
        snapshot = _fetch_receipt_row(conn, schema, row)
        missing += snapshot is None
        if snapshot is not None:
            current_rows.setdefault(snapshot.table, []).append(snapshot)
    if missing == 0:
        state = "ready"
    elif missing == len(rows):
        state = "deleted"
    else:
        raise RescueError("Only part of the restored rows still exists", "undo_rows_missing")
    return state, {table: tuple(snapshots) for table, snapshots in current_rows.items()}


def _original_key_sets(
    rows: dict[str, tuple[RowSnapshot, ...]],
) -> dict[str, set[tuple[object, ...]]]:
    return {
        table: {
            tuple(sorted(snapshot.key_values().items()))
            for snapshot in snapshots
        }
        for table, snapshots in rows.items()
    }


def _is_original_row(
    table_name: str,
    current: tuple[object, ...],
    table: TableSchema,
    receipt_keys: dict[str, set[tuple[object, ...]]],
) -> bool:
    if table_name not in receipt_keys or not table.primary_key:
        return False
    positions = {column: index for index, column in enumerate(table.column_names)}
    key = tuple(sorted((column, current[positions[column]]) for column in table.primary_key))
    return key in receipt_keys[table_name]


def _check_undo_dependencies(
    conn: sqlite3.Connection,
    schema: dict[str, TableSchema],
    rows: dict[str, tuple[RowSnapshot, ...]],
) -> None:
    receipt_keys = _original_key_sets(rows)

    def reject_unowned(table: TableSchema, found: list[tuple[object, ...]]) -> None:
        for current in found:
            if not _is_original_row(table.name, current, table, receipt_keys):
                raise RescueError(f"New dependency in {table.name} prevents undo", "undo_new_dependency")

    identifiers = restored_id_values(rows)
    for table in schema.values():
        for group in foreign_key_groups(table):
            if group.table in rows:
                reject_unowned(
                    table,
                    query_group_references(conn, table, group, schema[group.table], rows[group.table]),
                )
        for column in table.column_names:
            if column.endswith("_id"):
                reject_unowned(table, query_column_values(conn, table, column, identifiers))


def _delete_receipt_rows(
    conn: sqlite3.Connection,
    schema: dict[str, TableSchema],
    receipt: dict[str, object],
) -> None:
    by_table: dict[str, list[dict[str, object]]] = {}
    for row in receipt["rows"]:
        by_table.setdefault(str(row["table"]), []).append(row)
    for table_name in reversed(receipt["table_order"]):
        table = schema[table_name]
        for row in by_table[table_name]:
            key = _receipt_key(row)
            where = " AND ".join(f"{quote_identifier(column)} = ?" for column in table.primary_key)
            cursor = conn.execute(
                f"DELETE FROM {quote_identifier(table_name)} WHERE {where}",
                tuple(key[column] for column in table.primary_key),
            )
            if cursor.rowcount != 1:
                raise RescueError(f"Undo did not delete exactly one row in {table_name}", "undo_rows_missing")
    violation = conn.execute("PRAGMA foreign_key_check").fetchone()
    if violation:
        raise RescueError(f"Foreign key check failed after undo: {violation}", "undo_new_dependency")


def _undo_at(path: Path, target: Path, apply: bool) -> OperationResult:
    receipt = read_json(path)
    _validate_receipt(receipt)
    status = receipt.get("status")
    if status == "undone":
        raise RescueError("This restore was already undone", "undo_already_completed")
    if status not in {"prepared", "completed", "undo_prepared"}:
        raise RescueError(f"Receipt status does not permit undo: {status}", "undo_not_applied")
    row_counts = dict(receipt["row_counts"])

    conn = connect_existing(target)
    try:
        schema = inspect_schema(conn)
        if database_fingerprint(schema) != receipt["target_schema_fingerprint"]:
            raise RescueError("Target schema changed after restore", "schema_incompatible")
        state, current_rows = _read_undo_rows(conn, schema, receipt["rows"])
        if state == "deleted":
            if status != "undo_prepared":
                raise RescueError("Restored rows are absent; undo was not prepared", "undo_not_applied")
        else:
            _check_undo_dependencies(conn, schema, current_rows)
    finally:
        conn.close()

    if not apply:
        return OperationResult("undo", "dry_run", False, None, None, None, path, row_counts)
    if state == "deleted":
        receipt["status"] = "undone"
        receipt["undo_completed_at"] = _utc_now()
        atomic_write_json(path, receipt)
        return OperationResult("undo", "undone", False, str(receipt["operation_id"]), None, None, path, row_counts)

    undo_id = uuid.uuid4().hex
    directory = _operation_directory(path.parent.parent, "undo", undo_id)
    backup_path = backup_database(target, directory / "target-before-undo.sqlite")
    previous_status = status
    receipt["status"] = "undo_prepared"
    receipt["undo"] = {
        "operation_id": undo_id,
        "operation_dir": str(directory),
        "backup_path": str(backup_path),
        "prepared_at": _utc_now(),
    }
    atomic_write_json(path, receipt)

    conn = connect_existing(target)
    try:
        conn.execute("BEGIN IMMEDIATE")
        schema = inspect_schema(conn)
        if database_fingerprint(schema) != receipt["target_schema_fingerprint"]:
            raise RescueError("Target schema changed before undo", "schema_incompatible")
        state, current_rows = _read_undo_rows(conn, schema, receipt["rows"])
        if state != "ready":
            raise RescueError("Restored rows are no longer all present", "undo_rows_missing")
        _check_undo_dependencies(conn, schema, current_rows)
        _delete_receipt_rows(conn, schema, receipt)
        conn.commit()
    except (RescueError, sqlite3.Error) as error:
        if conn.in_transaction:
            conn.rollback()
        receipt["status"] = previous_status
        receipt["undo_failed_at"] = _utc_now()
        receipt["undo_error"] = str(error)
        atomic_write_json(path, receipt)
        if isinstance(error, RescueError):
            raise
        raise RescueError(f"Undo failed and was rolled back: {error}", "undo_failed") from error
    finally:
        conn.close()

    receipt["status"] = "undone"
    receipt["undo_completed_at"] = _utc_now()
    receipt["undo"]["completed_at"] = _utc_now()
    atomic_write_json(path, receipt)
    return OperationResult("undo", "undone", True, undo_id, directory, backup_path, path, row_counts)


def undo_restore(
    receipt_path: str | Path,
    apply: bool = False,
    target_path: str | Path | None = None,
) -> OperationResult:
    path = Path(receipt_path).absolute()
    initial = read_json(path)
    _validate_receipt(initial)
    target = Path(target_path).absolute() if target_path else Path(str(initial["target"])).absolute()
    if not apply:
        return _undo_at(path, target, False)
    with operation_lock(target):
        return _undo_at(path, target, True)
