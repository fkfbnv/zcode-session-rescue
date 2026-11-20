from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .db import ForeignKey, RowSnapshot, TableSchema, quote_identifier
from .errors import RescueError


@dataclass(frozen=True)
class ForeignKeyGroup:
    id: int
    table: str
    columns: tuple[tuple[str, str | None], ...]
    on_delete: str


def foreign_key_groups(table: TableSchema) -> tuple[ForeignKeyGroup, ...]:
    grouped: dict[int, list[ForeignKey]] = {}
    for foreign_key in table.foreign_keys:
        grouped.setdefault(foreign_key.id, []).append(foreign_key)
    return tuple(
        ForeignKeyGroup(
            id=identifier,
            table=keys[0].table,
            columns=tuple((key.from_column, key.to_column) for key in sorted(keys, key=lambda key: key.seq)),
            on_delete=keys[0].on_delete.upper(),
        )
        for identifier, keys in sorted(grouped.items())
    )


def _normalized_sql(sql: str) -> str:
    normalized = " ".join(sql.split()).lower()
    return re.sub(r"\s*([(),;])\s*", r"\1", normalized)


_SEQUENCE_TRIGGERS = {
    "message_sequence_autofill": _normalized_sql(
        "CREATE TRIGGER message_sequence_autofill after insert on message "
        "when new.sequence is null begin update message set sequence = ("
        "select coalesce(max(sequence), -1) + 1 from message where session_id = new.session_id"
        ") where id = new.id; end"
    ),
    "part_sequence_autofill": _normalized_sql(
        "CREATE TRIGGER part_sequence_autofill after insert on part "
        "when new.sequence is null begin update part set sequence = ("
        "select coalesce(max(sequence), -1) + 1 from part where message_id = new.message_id"
        ") where id = new.id; end"
    ),
}


def _supported_trigger(name: str, sql: str) -> bool:
    normalized = _normalized_sql(sql)
    if name in _SEQUENCE_TRIGGERS:
        return normalized == _SEQUENCE_TRIGGERS[name]
    if name.startswith("session_purge_") and "raise(abort" in normalized:
        has_data_change = re.search(r"\b(?:insert\s+into|delete\s+from|update\s+\w+\s+set)\b", normalized)
        return has_data_change is None
    return False


_OFFICIAL_DELETE_ACTIONS = {
    ("message", "session", "CASCADE"),
    ("part", "message", "CASCADE"),
    ("todo", "session", "CASCADE"),
    ("session_entry", "session", "CASCADE"),
    ("session_target", "session", "CASCADE"),
    ("model_usage", "session", "CASCADE"),
    ("turn_usage", "session", "CASCADE"),
    ("tool_usage", "session", "CASCADE"),
    ("session_input", "session", "CASCADE"),
    ("workflow_run", "session", "SET NULL"),
    ("workflow_activity", "session", "SET NULL"),
    ("workflow_activity", "workflow_run", "CASCADE"),
    ("workflow_event", "workflow_activity", "SET NULL"),
    ("workflow_event", "workflow_run", "CASCADE"),
    ("session_task_link", "session", "CASCADE"),
    ("session_task_link", "session", "SET NULL"),
    ("session_task_link", "workflow_activity", "SET NULL"),
    ("session_task_link", "session_task_link", "CASCADE"),
    ("session_task_link", "workflow_run", "CASCADE"),
    ("dwf_actor", "dwf_run", "CASCADE"),
    ("dwf_node", "dwf_run", "CASCADE"),
    ("dwf_event", "dwf_run", "CASCADE"),
}


def reject_unsupported_objects(
    schema: dict[str, TableSchema],
    affected_tables: set[str] | None = None,
) -> None:
    if affected_tables is None:
        return
    for table in schema.values():
        for foreign_key in table.foreign_keys:
            if table.name not in affected_tables and foreign_key.table not in affected_tables:
                continue
            action = foreign_key.on_delete.upper()
            signature = (table.name, foreign_key.table, action)
            if action not in {"NO ACTION", "RESTRICT"} and signature not in _OFFICIAL_DELETE_ACTIONS:
                raise RescueError(
                    f"ON DELETE {foreign_key.on_delete} is not supported on {table.name}",
                    "unsupported_foreign_key_action",
                )
        if table.name in affected_tables:
            for trigger in table.triggers:
                if not _supported_trigger(trigger.name, trigger.sql):
                    raise RescueError(
                        f"Trigger {trigger.name} is not supported on {table.name}",
                        "unsupported_trigger",
                    )


def _referenced_column(
    referenced: TableSchema,
    column: str | None,
    sequence: int,
) -> str:
    if column is not None:
        return column
    if sequence >= len(referenced.primary_key):
        raise RescueError(f"Cannot resolve implicit key on {referenced.name}", "unsupported_association")
    return referenced.primary_key[sequence]


def _reference_conditions(
    group: ForeignKeyGroup,
    referenced_schema: TableSchema,
    snapshots: tuple[RowSnapshot, ...],
) -> set[tuple[tuple[str, ...], tuple[object, ...]]]:
    from_columns = tuple(column for column, _ in group.columns)
    conditions: set[tuple[tuple[str, ...], tuple[object, ...]]] = set()
    for snapshot in snapshots:
        source_values = snapshot.value_map()
        values: list[object] = []
        for sequence, (_, referenced_column) in enumerate(group.columns):
            resolved = _referenced_column(referenced_schema, referenced_column, sequence)
            if resolved not in source_values:
                raise RescueError(
                    f"Referenced column {referenced_schema.name}.{resolved} is not in the restore snapshot",
                    "unclosed_reference",
                )
            values.append(source_values[resolved])
        if any(value is None for value in values):
            continue
        conditions.add((from_columns, tuple(values)))
    return conditions


def _referencing_conditions(
    group: ForeignKeyGroup,
    snapshots: tuple[RowSnapshot, ...],
) -> set[tuple[tuple[str, ...], tuple[object, ...]]]:
    from_columns = tuple(column for column, _ in group.columns)
    conditions: set[tuple[tuple[str, ...], tuple[object, ...]]] = set()
    for snapshot in snapshots:
        source_values = snapshot.value_map()
        values = tuple(source_values[column] for column in from_columns)
        if any(value is None for value in values):
            continue
        conditions.add((from_columns, values))
    return conditions


def _select_or_chunks(
    conn: sqlite3.Connection,
    table: TableSchema,
    conditions: set[tuple[tuple[str, ...], tuple[object, ...]]],
    chunk_size: int = 50,
) -> list[tuple[object, ...]]:
    if not conditions:
        return []
    selected = ", ".join(quote_identifier(column) for column in table.column_names)
    output: list[tuple[object, ...]] = []
    ordered = sorted(conditions, key=lambda item: repr(item))
    for start in range(0, len(ordered), chunk_size):
        chunk = ordered[start : start + chunk_size]
        parameters: list[object] = []
        clauses: list[str] = []
        for columns, values in chunk:
            clauses.append("(" + " AND ".join(f"{quote_identifier(column)} = ?" for column in columns) + ")")
            parameters.extend(values)
        sql = f"SELECT {selected} FROM {quote_identifier(table.name)} WHERE " + " OR ".join(clauses)
        output.extend(conn.execute(sql, parameters).fetchall())
    return output


def query_group_references(
    conn: sqlite3.Connection,
    referencing: TableSchema,
    group: ForeignKeyGroup,
    referenced_schema: TableSchema,
    snapshots: tuple[RowSnapshot, ...],
) -> list[tuple[object, ...]]:
    return _select_or_chunks(
        conn,
        referencing,
        _reference_conditions(group, referenced_schema, snapshots),
    )


def query_column_values(
    conn: sqlite3.Connection,
    table: TableSchema,
    column: str,
    values: set[object],
    chunk_size: int = 200,
) -> list[tuple[object, ...]]:
    conditions = {
        ((column,), (value,))
        for value in values
        if value is not None
    }
    return _select_or_chunks(conn, table, conditions, chunk_size)


def validate_restore_closure(
    rows: dict[str, tuple[RowSnapshot, ...]],
    schema: dict[str, TableSchema],
) -> None:
    for table_name, snapshots in rows.items():
        table = schema[table_name]
        for group in foreign_key_groups(table):
            referenced_snapshots = rows.get(group.table)
            if referenced_snapshots is None:
                raise RescueError(
                    f"{table_name} references unselected {group.table}",
                    "unclosed_reference",
                )
            referenced_schema = schema[group.table]
            conditions = _referencing_conditions(group, snapshots)
            valid_conditions = _reference_conditions(group, referenced_schema, referenced_snapshots)
            # Compare referenced keys, not the referencing column names.
            valid_keys = {values for _, values in valid_conditions}
            for _, values in conditions:
                if values not in valid_keys:
                    raise RescueError(
                        f"{table_name} has a reference outside the selected session",
                        "unclosed_reference",
                    )


def query_key_tuples(
    conn: sqlite3.Connection,
    table: TableSchema,
    keys: set[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    if not table.primary_key:
        return []
    conditions = {
        (table.primary_key, key)
        for key in keys
        if len(key) == len(table.primary_key) and all(value is not None for value in key)
    }
    return _select_or_chunks(conn, table, conditions)


def restored_id_values(rows: dict[str, tuple[RowSnapshot, ...]]) -> set[object]:
    values: set[object] = set()
    for snapshots in rows.values():
        for snapshot in snapshots:
            if snapshot.primary_key == ("id",):
                value = snapshot.key_values()["id"]
                if value is not None:
                    values.add(value)
    return values
