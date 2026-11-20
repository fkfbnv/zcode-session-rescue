from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, NamedTuple

from .errors import RescueError


class ForeignKey(NamedTuple):
    table: str
    from_column: str
    to_column: str | None
    id: int = 0
    seq: int = 0
    on_update: str = "NO ACTION"
    on_delete: str = "NO ACTION"
    match: str = "NONE"


@dataclass(frozen=True)
class Column:
    name: str
    declared_type: str
    not_null: bool
    default: str | None
    pk_position: int


class TriggerSchema(NamedTuple):
    name: str
    sql: str


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]
    foreign_keys: tuple[ForeignKey, ...]
    sql: str = ""
    triggers: tuple[TriggerSchema, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def primary_key(self) -> tuple[str, ...]:
        keyed = [column for column in self.columns if column.pk_position]
        return tuple(column.name for column in sorted(keyed, key=lambda column: column.pk_position))


@dataclass(frozen=True)
class RowSnapshot:
    table: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    values: tuple[object, ...]
    sha256: str

    @classmethod
    def from_values(
        cls,
        table: str,
        columns: tuple[str, ...],
        primary_key: tuple[str, ...],
        values: Iterable[object],
    ) -> "RowSnapshot":
        frozen_values = tuple(values)
        if len(columns) != len(frozen_values):
            raise ValueError("Column and value counts differ")
        return cls(table, columns, primary_key, frozen_values, row_content_digest(columns, frozen_values))

    def value_map(self) -> dict[str, object]:
        return dict(zip(self.columns, self.values))

    def key_values(self) -> dict[str, object]:
        values = self.value_map()
        return {column: values[column] for column in self.primary_key}

    def to_receipt(self) -> dict[str, object]:
        return {
            "table": self.table,
            "primary_key": {
                column: typed_value(value) for column, value in self.key_values().items()
            },
            "sha256": self.sha256,
        }


def _database_uri(path: Path, mode: str) -> str:
    return f"{path.absolute().as_uri()}?mode={mode}"


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise RescueError(f"Source database does not exist: {database}", "source_missing")
    try:
        conn = sqlite3.connect(_database_uri(database, "ro"), uri=True)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except sqlite3.Error as error:
        raise RescueError(f"Cannot open source database: {database}: {error}", "database_open_failed") from error


def connect_existing(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    if not database.is_file():
        raise RescueError(f"Target database does not exist: {database}", "target_missing")
    try:
        conn = sqlite3.connect(_database_uri(database, "rw"), uri=True, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except sqlite3.Error as error:
        raise RescueError(f"Cannot open target database: {database}: {error}", "database_open_failed") from error


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def inspect_schema(conn: sqlite3.Connection) -> dict[str, TableSchema]:
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    trigger_rows = conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY tbl_name, name"
    ).fetchall()
    triggers: dict[str, list[TriggerSchema]] = {}
    for trigger_name, table_name, sql in trigger_rows:
        triggers.setdefault(table_name, []).append(TriggerSchema(trigger_name, sql or ""))
    schema: dict[str, TableSchema] = {}
    for name, sql in table_rows:
        quoted = quote_identifier(name)
        columns = tuple(
            Column(
                name=row[1],
                declared_type=row[2].upper(),
                not_null=bool(row[3]),
                default=row[4],
                pk_position=row[5],
            )
            for row in conn.execute(f"PRAGMA table_info({quoted})")
        )
        foreign_keys = tuple(
            ForeignKey(row[2], row[3], row[4], row[0], row[1], row[5], row[6], row[7])
            for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")
        )
        schema[name] = TableSchema(name, columns, foreign_keys, sql or "", tuple(triggers.get(name, ())))
    return schema


def typed_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"type": "blob", "value": encoded}
    raise TypeError(f"Unsupported SQLite value type: {type(value).__name__}")


def canonical_values(values: Iterable[object]) -> str:
    return json.dumps(
        [typed_value(value) for value in values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def row_digest(values: Iterable[object]) -> str:
    return hashlib.sha256(canonical_values(values).encode("utf-8")).hexdigest()


def row_content_digest(columns: Iterable[str], values: Iterable[object]) -> str:
    content = dict(zip(columns, (typed_value(value) for value in values)))
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
