from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import (
    RowSnapshot,
    TableSchema,
    connect_existing,
    connect_readonly,
    inspect_schema,
    quote_identifier,
)
from .errors import RescueError
from .relations import (
    foreign_key_groups,
    query_column_values,
    query_group_references,
    query_key_tuples,
    reject_unsupported_objects,
    restored_id_values,
    validate_restore_closure,
)

SKIPPED_TABLES = frozenset({"session_runtime_presence", "session_purge"})
SESSION_REFERENCE_COLUMNS = ("session_id", "parent_session_id", "child_session_id")


@dataclass(frozen=True)
class SessionSummary:
    id: str
    title: str | None
    time_created: int | None
    time_updated: int | None
    message_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "time_created": self.time_created,
            "time_updated": self.time_updated,
            "message_count": self.message_count,
        }


@dataclass
class RestorePlan:
    source: Path
    target: Path
    session_id: str
    rows: dict[str, tuple[RowSnapshot, ...]]
    table_order: tuple[str, ...]
    skipped_tables: tuple[str, ...]
    source_schema: dict[str, TableSchema]
    target_schema: dict[str, TableSchema]
    schema_fingerprint: str
    source_migrations: list[dict[str, object]]
    target_migrations: list[dict[str, object]]

    def row_counts(self) -> dict[str, int]:
        return {table: len(self.rows[table]) for table in self.table_order}

    def to_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "session_id": self.session_id,
            "row_counts": self.row_counts(),
            "table_order": list(self.table_order),
            "skipped_tables": list(self.skipped_tables),
            "schema_fingerprint": self.schema_fingerprint,
            "source_migrations": self.source_migrations,
            "target_migrations": self.target_migrations,
        }


def database_fingerprint(schema: dict[str, TableSchema]) -> str:
    payload = {
        name: {
            "sql": table.sql,
            "triggers": list(table.triggers),
            "columns": [
                [column.name, column.declared_type, column.not_null, column.default, column.pk_position]
                for column in table.columns
            ],
            "foreign_keys": [list(foreign_key) for foreign_key in table.foreign_keys],
        }
        for name, table in sorted(schema.items())
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_session_schema(schema: dict[str, TableSchema], code: str) -> None:
    session = schema.get("session")
    if not session or session.primary_key != ("id",):
        raise RescueError("Database does not have a supported ZCode session table", code)
    required = {"id", "title", "time_created", "time_updated"}
    if not required.issubset(session.column_names):
        raise RescueError("Database is missing required ZCode session columns", code)


def _migrations(conn: sqlite3.Connection, schema: dict[str, TableSchema]) -> list[dict[str, object]]:
    table = schema.get("schema_migration")
    if not table:
        return []
    columns = [name for name in ("id", "checksum", "app_version", "time_applied") if name in table.column_names]
    selected = ", ".join(quote_identifier(column) for column in columns)
    return [dict(zip(columns, row)) for row in conn.execute(f"SELECT {selected} FROM schema_migration ORDER BY id")]


def list_sessions(source: str | Path) -> list[SessionSummary]:
    conn = connect_readonly(source)
    try:
        schema = inspect_schema(conn)
        _require_session_schema(schema, "invalid_source")
        message = schema.get("message")
        if not message or "session_id" not in message.column_names:
            raise RescueError("Database does not have a supported ZCode message table", "invalid_source")
        rows = conn.execute(
            "SELECT s.id, s.title, s.time_created, s.time_updated, COUNT(m.id) "
            "FROM session AS s LEFT JOIN message AS m ON m.session_id = s.id "
            "GROUP BY s.id, s.title, s.time_created, s.time_updated "
            "ORDER BY s.time_updated DESC, s.id"
        )
        return [SessionSummary(*row) for row in rows]
    except sqlite3.Error as error:
        raise RescueError(f"Cannot read ZCode sessions: {error}", "invalid_source") from error
    finally:
        conn.close()


def _select_rows(
    conn: sqlite3.Connection,
    table: TableSchema,
    column: str,
    value: object,
) -> list[RowSnapshot]:
    columns = ", ".join(quote_identifier(name) for name in table.column_names)
    rows = conn.execute(
        f"SELECT {columns} FROM {quote_identifier(table.name)} WHERE {quote_identifier(column)} = ?",
        (value,),
    ).fetchall()
    return [
        RowSnapshot.from_values(table.name, table.column_names, table.primary_key, row)
        for row in rows
    ]


def _compatible(source: TableSchema, target: TableSchema) -> bool:
    def columns(table: TableSchema) -> list[tuple[object, ...]]:
        return sorted(
            (column.name, column.declared_type, column.not_null, column.default)
            for column in table.columns
        )

    return (
        columns(source) == columns(target)
        and source.primary_key == target.primary_key
        and sorted(source.foreign_keys) == sorted(target.foreign_keys)
        and ("STRICT" in source.sql.upper()) == ("STRICT" in target.sql.upper())
        and ("AUTOINCREMENT" in source.sql.upper()) == ("AUTOINCREMENT" in target.sql.upper())
    )


def _ordered_tables(
    rows: dict[str, tuple[RowSnapshot, ...]],
    schema: dict[str, TableSchema],
) -> tuple[str, ...]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(table: str) -> None:
        if table in visited:
            return
        if table in visiting:
            raise RescueError(f"Selected table dependency contains a cycle: {table}", "unsupported_association")
        visiting.add(table)
        for group in foreign_key_groups(schema[table]):
            if group.table in rows and group.table != table:
                visit(group.table)
        visiting.remove(table)
        visited.add(table)
        ordered.append(table)

    for table in sorted(rows):
        visit(table)
    return tuple(ordered)


def _check_source_indirect_references(
    conn: sqlite3.Connection,
    schema: dict[str, TableSchema],
    rows: dict[str, tuple[RowSnapshot, ...]],
) -> None:
    identifiers = restored_id_values(rows)
    for table in schema.values():
        if table.name in rows or table.name in SKIPPED_TABLES:
            continue
        for group in foreign_key_groups(table):
            if group.table in rows and query_group_references(
                conn, table, group, schema[group.table], rows[group.table]
            ):
                raise RescueError(
                    f"Session has an unsupported reference from {table.name}",
                    "unsupported_association",
                )
        for column in table.column_names:
            if column.endswith("_id") and query_column_values(conn, table, column, identifiers):
                raise RescueError(
                    f"Session has an unsupported semantic reference from {table.name}.{column}",
                    "unsupported_association",
                )


def _check_target_conflicts(
    conn: sqlite3.Connection,
    schema: dict[str, TableSchema],
    rows: dict[str, tuple[RowSnapshot, ...]],
    session_id: str,
) -> None:
    if conn.execute("SELECT 1 FROM session WHERE id = ?", (session_id,)).fetchone():
        raise RescueError("Target already contains this session", "target_session_exists")

    for table in schema.values():
        if table.name == "session":
            continue
        for column in SESSION_REFERENCE_COLUMNS:
            if column not in table.column_names:
                continue
            if query_column_values(conn, table, column, {session_id}):
                code = "target_orphan_rows" if table.name in rows and column == "session_id" else "target_reverse_reference"
                raise RescueError(f"Target already has related rows in {table.name}", code)

    for table_name, snapshots in rows.items():
        table = schema[table_name]
        keys = {
            tuple(snapshot.key_values()[column] for column in snapshot.primary_key)
            for snapshot in snapshots
        }
        if query_key_tuples(conn, table, keys):
            raise RescueError(f"Target primary key conflict in {table_name}", "target_primary_key_conflict")

    identifiers = restored_id_values(rows)
    for table in schema.values():
        for group in foreign_key_groups(table):
            if group.table in rows and query_group_references(
                conn, table, group, schema[group.table], rows[group.table]
            ):
                raise RescueError(
                    f"Target row in {table.name} would reference restored data",
                    "target_reverse_reference",
                )
        for column in table.column_names:
            if column.endswith("_id") and query_column_values(conn, table, column, identifiers):
                raise RescueError(
                    f"Target row in {table.name}.{column} would reference restored data",
                    "target_reverse_reference",
                )


def build_restore_plan(
    source: str | Path,
    target: str | Path,
    session_id: str,
) -> RestorePlan:
    source_path = Path(source).absolute()
    target_path = Path(target).absolute()
    if source_path == target_path:
        raise RescueError("Source and target databases must differ", "source_target_same")

    source_conn = connect_readonly(source_path)
    target_conn = connect_existing(target_path)
    try:
        source_conn.execute("BEGIN")
        target_conn.execute("BEGIN")
        source_schema = inspect_schema(source_conn)
        target_schema = inspect_schema(target_conn)
        reject_unsupported_objects(source_schema)
        reject_unsupported_objects(target_schema)
        _require_session_schema(source_schema, "invalid_source")
        _require_session_schema(target_schema, "invalid_target")

        source_session_rows = _select_rows(source_conn, source_schema["session"], "id", session_id)
        if not source_session_rows:
            raise RescueError(f"Session not found: {session_id}", "session_missing")
        if len(source_session_rows) != 1:
            raise RescueError("Source session ID is not unique", "invalid_source")
        if source_conn.execute("SELECT 1 FROM session WHERE parent_id = ? LIMIT 1", (session_id,)).fetchone():
            raise RescueError("Sessions with child sessions are not supported", "unsupported_parent")

        rows: dict[str, tuple[RowSnapshot, ...]] = {"session": tuple(source_session_rows)}
        session_values = source_session_rows[0]
        if "parent_id" in session_values.columns:
            parent_id = session_values.values[session_values.columns.index("parent_id")]
            if parent_id is not None and not target_conn.execute(
                "SELECT 1 FROM session WHERE id = ?", (parent_id,)
            ).fetchone():
                raise RescueError("Parent session is absent from the target", "unsupported_parent")

        for table in source_schema.values():
            if table.name in SKIPPED_TABLES or table.name == "session":
                continue
            for column in ("parent_session_id", "child_session_id"):
                if column in table.column_names and query_column_values(source_conn, table, column, {session_id}):
                    raise RescueError(
                        f"Session uses unsupported linked table {table.name}",
                        "unsupported_association",
                    )

        if "session_purge" in source_schema and source_conn.execute(
            "SELECT 1 FROM session_purge WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone():
            raise RescueError("Purged sessions are not supported", "unsupported_association")

        candidates = [
            table
            for table in source_schema.values()
            if table.name != "session"
            and table.name not in SKIPPED_TABLES
            and "session_id" in table.column_names
        ]
        candidate_names = {table.name for table in candidates}
        for table in candidates:
            selected = _select_rows(source_conn, table, "session_id", session_id)
            if not selected:
                continue
            if not table.primary_key or "AUTOINCREMENT" in table.sql.upper():
                raise RescueError(f"Cannot safely identify rows in {table.name}", "unsupported_table")
            if any(value is None for snapshot in selected for value in snapshot.key_values().values()):
                raise RescueError(f"NULL primary keys are not supported in {table.name}", "unsupported_table")
            for group in foreign_key_groups(table):
                if group.table not in candidate_names | {"session", "message"}:
                    raise RescueError(
                        f"Session table {table.name} depends on unsupported {group.table}",
                        "unsupported_association",
                    )
            rows[table.name] = tuple(selected)

        for table_name in rows:
            if table_name not in target_schema:
                raise RescueError(f"Target is missing table {table_name}", "schema_incompatible")
            if not _compatible(source_schema[table_name], target_schema[table_name]):
                raise RescueError(f"Schema differs for table {table_name}", "schema_incompatible")

        reject_unsupported_objects(source_schema, set(rows))
        reject_unsupported_objects(target_schema, set(rows))
        validate_restore_closure(rows, source_schema)
        _check_source_indirect_references(source_conn, source_schema, rows)
        _check_target_conflicts(target_conn, target_schema, rows, session_id)
        table_order = _ordered_tables(rows, source_schema)
        skipped = tuple(sorted(table for table in SKIPPED_TABLES if table in source_schema))
        plan = RestorePlan(
            source=source_path,
            target=target_path,
            session_id=session_id,
            rows=rows,
            table_order=table_order,
            skipped_tables=skipped,
            source_schema=source_schema,
            target_schema=target_schema,
            schema_fingerprint=database_fingerprint(target_schema),
            source_migrations=_migrations(source_conn, source_schema),
            target_migrations=_migrations(target_conn, target_schema),
        )
        source_conn.commit()
        target_conn.commit()
        return plan
    except sqlite3.Error as error:
        raise RescueError(f"Cannot build restore plan: {error}", "database_read_failed") from error
    finally:
        source_conn.close()
        target_conn.close()
