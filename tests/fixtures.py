import sqlite3
from pathlib import Path

SESSION_ID = "session_restore"
OTHER_SESSION_ID = "session_existing"

SCHEMA_SQL = """
CREATE TABLE schema_migration (
    id TEXT PRIMARY KEY,
    checksum TEXT,
    app_version TEXT,
    time_applied INTEGER
);
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    title TEXT,
    time_created INTEGER,
    time_updated INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    time_created INTEGER,
    data TEXT,
    FOREIGN KEY(session_id) REFERENCES session(id)
);
CREATE TABLE part (
    id TEXT PRIMARY KEY,
    message_id TEXT,
    session_id TEXT,
    data BLOB,
    FOREIGN KEY(message_id) REFERENCES message(id)
);
CREATE TABLE todo (
    session_id TEXT,
    position INTEGER,
    content TEXT,
    status TEXT,
    PRIMARY KEY(session_id, position),
    FOREIGN KEY(session_id) REFERENCES session(id)
);
CREATE TABLE turn_usage (
    session_id TEXT,
    turn_id TEXT,
    input_tokens INTEGER,
    PRIMARY KEY(session_id, turn_id),
    FOREIGN KEY(session_id) REFERENCES session(id)
);
CREATE TABLE session_entry (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    data TEXT,
    FOREIGN KEY(session_id) REFERENCES session(id)
);
CREATE TABLE input_history (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    text TEXT,
    attachments TEXT
);
CREATE TABLE custom_note (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    note TEXT,
    FOREIGN KEY(session_id) REFERENCES session(id)
);
CREATE TABLE session_runtime_presence (
    session_id TEXT,
    owner_id TEXT,
    process_id INTEGER,
    PRIMARY KEY(session_id, owner_id)
);
CREATE TABLE session_purge (
    session_id TEXT PRIMARY KEY,
    root_id TEXT,
    state TEXT
);
CREATE TABLE workflow_run (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    name TEXT,
    FOREIGN KEY(parent_session_id) REFERENCES session(id)
);
CREATE TABLE workflow_activity (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    child_session_id TEXT,
    FOREIGN KEY(child_session_id) REFERENCES session(id)
);
CREATE TABLE dwf_run (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT
);
CREATE TABLE dwf_actor (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    session_id TEXT,
    name TEXT,
    FOREIGN KEY(run_id) REFERENCES dwf_run(id)
);
CREATE TABLE session_task_link (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    child_session_id TEXT,
    FOREIGN KEY(parent_session_id) REFERENCES session(id),
    FOREIGN KEY(child_session_id) REFERENCES session(id)
);
CREATE TABLE semantic_reference (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    message_id TEXT
);
"""


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_migration VALUES (?, ?, ?, ?)",
        ("0001", "fixture-checksum", "fixture", 1),
    )
    return conn


def create_source_db(path: Path, schema_variant: str = "v1") -> Path:
    conn = _create_db(path)
    if schema_variant == "extra_source_column":
        conn.execute("ALTER TABLE custom_note ADD COLUMN source_only TEXT")
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        (SESSION_ID, None, "可恢复会话", 10, 20),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("message_1", SESSION_ID, 11, '{"text":"你好"}'),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("part_1", "message_1", SESSION_ID, sqlite3.Binary(b"\x00\x01\xff")),
    )
    conn.execute(
        "INSERT INTO todo VALUES (?, ?, ?, ?)",
        (SESSION_ID, 0, "零号位置", "pending"),
    )
    conn.execute(
        "INSERT INTO turn_usage VALUES (?, ?, ?)",
        (SESSION_ID, "", 42),
    )
    conn.execute(
        "INSERT INTO session_entry VALUES (?, ?, ?)",
        ("entry_1", SESSION_ID, '{"kind":"测试"}'),
    )
    conn.execute(
        "INSERT INTO input_history VALUES (?, ?, ?, ?)",
        ("input_1", SESSION_ID, "输入", None),
    )
    conn.execute(
        "INSERT INTO custom_note VALUES (?, ?, ?)",
        ("note_1", SESSION_ID, "动态直属表"),
    )
    conn.execute(
        "INSERT INTO session_runtime_presence VALUES (?, ?, ?)",
        (SESSION_ID, "runtime", 123),
    )
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        ("session_other_source", None, "源库其他会话", 1, 2),
    )
    conn.commit()
    conn.close()
    return path


def create_target_db(path: Path, schema_variant: str = "v1") -> Path:
    conn = _create_db(path)
    if schema_variant == "extra_target_column":
        conn.execute("ALTER TABLE custom_note ADD COLUMN target_only TEXT")
    conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
        (OTHER_SESSION_ID, None, "目标原有会话", 100, 200),
    )
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        ("message_existing", OTHER_SESSION_ID, 101, "keep"),
    )
    conn.commit()
    conn.close()
    return path
