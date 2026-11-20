# ZCode Session Rescue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standard-library Python CLI that safely restores one ordinary ZCode chat session from an old SQLite database and can precisely undo that restore.

**Architecture:** A schema-introspection layer produces a strict restore plan and immutable row snapshots. Operation code performs backup, pre-written receipts, transactional insertion, hash-checked undo, and crash-state reconciliation; the CLI is a thin adapter. Synthetic versioned databases exercise all writes without touching real sessions.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `argparse`, `dataclasses`, `hashlib`, `json`, `unittest`; setuptools `src/` layout; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-18-zcode-session-rescue-design.md`

## Global Constraints

- No runtime third-party dependencies; no commit, push, public repository, Release, upload, or real-database writes in this run.
- Default to dry-run; only explicit `--apply` writes, and only to synthetic databases during automated verification.
- Ordinary chats only. Reject workflow, DWF, parent/child links, unknown associations, incompatible schemas, conflicts, and unsupported external artifacts.
- Never use `INSERT OR IGNORE`; never silently skip a selected table or conflict.
- Source access is read-only; every source row and hash in one operation comes from one SQLite read snapshot.
- Every write has a fresh SQLite backup and a versioned JSON receipt containing no message bodies.
- Keep interfaces typed and modules focused; use `unittest`, not pytest.

## Review Focus

- Empty/invalid/non-ZCode databases should produce a concise error, not a traceback or empty success; pin in Tasks 2 and 5.
- Composite keys containing `0`, empty text, Unicode, and BLOB data must remain exact through JSON receipts; pin in Tasks 1 and 4.
- Target changes between dry-run and apply must be detected again inside the write transaction; pin in Task 3.
- Undo after a new semantic reference without a declared FK must refuse rather than orphan data; pin in Task 4.
- Paths containing spaces, Unicode, `?`, or `#` must open without URI corruption; pin in Tasks 1 and 5.

---

### Task 1: Package, SQLite primitives, and synthetic schemas

**Files:**
- Create: `pyproject.toml`, `src/zcode_session_rescue/__init__.py`, `errors.py`, `db.py`, `receipt.py`
- Test: `tests/__init__.py`, `tests/fixtures.py`, `tests/test_db.py`, `tests/test_receipt.py`

**Interfaces:**
- Produces: `RescueError(message, code)`, `Column`, `ForeignKey`, `TableSchema`, `RowSnapshot`, `connect_readonly(path)`, `connect_existing(path)`, `inspect_schema(conn)`, `row_digest(values)`, `atomic_write_json(path, payload)`, `read_json(path)`.
- `RowSnapshot` stores table, ordered columns, values, primary-key mapping, and SHA-256 digest; JSON conversion is type-preserving and contains values only for keys, not row bodies.
- Test fixture produces `create_source_db(path, schema_variant="v1")` and `create_target_db(path, schema_variant="v1")` with ordinary chat, composite-key, runtime, purge, workflow, and semantic-link tables.

- [ ] Write failing tests for read-only/existing-path opening, special-character paths, schema/PK/FK introspection, typed row hashes, atomic JSON, and fixture determinism.
- [ ] Run `python -I -m unittest tests.test_db tests.test_receipt -v`; expect import/module failures.
- [ ] Implement package metadata, typed schema/row primitives, SQLite URI connections, canonical hashes, and atomic JSON writes.
- [ ] Re-run the two modules; expect PASS.

### Task 2: Strict restore planning and session listing

**Files:**
- Create: `src/zcode_session_rescue/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: Task 1 schema and row primitives.
- Produces: `RestorePlan`, `PlanRow`, `build_restore_plan(source, target, session_id) -> RestorePlan`, `list_sessions(source) -> list[SessionSummary]`.
- `RestorePlan` includes source/target paths, session ID, schema fingerprint, selected table order, immutable row snapshots, skipped runtime tables, and migration metadata.

- [ ] Write failing tests for list output without bodies, ordinary chat discovery, dynamic direct tables, composite keys, runtime exclusion, missing/duplicate session, empty/non-ZCode DB, exact schema compatibility, target session/orphan/PK conflicts, reverse-reference conflicts, parent links, workflow/DWF/task links, and unknown associations.
- [ ] Run `python -I -m unittest tests.test_planner -v`; expect missing `planner` failure.
- [ ] Implement schema-driven discovery and conservative rejection. Snapshot every selected row inside one source read transaction; compute insertion order and schema fingerprint.
- [ ] Re-run planner and earlier tests; expect PASS.

### Task 3: Backup and transactional restore

**Files:**
- Create: `src/zcode_session_rescue/operations.py`
- Test: `tests/test_restore.py`

**Interfaces:**
- Consumes: `RestorePlan`, `RowSnapshot`, receipt helpers.
- Produces: `backup_database(target, destination)`, `restore_session(source_path, target_path, session_id, operations_dir, apply=False) -> OperationResult`.
- Receipt statuses: `prepared`, `completed`; result includes operation directory, backup, receipt, counts, and dry-run flag.

- [ ] Write failing tests proving default dry-run writes nothing, apply preserves other sessions, backup `quick_check` passes, receipt precedes writes and has hashes without bodies, committed rows match the plan, source changes cannot change staged rows, conflicts injected immediately before apply roll back, and no success receipt appears after failure.
- [ ] Run `python -I -m unittest tests.test_restore -v`; expect missing `operations` failure.
- [ ] Implement unique operation directories, SQLite online backup, pre-written receipt, `BEGIN IMMEDIATE`, in-transaction conflict/reverse-reference checks, ordinary `INSERT`, FK enforcement, and atomic completed receipt.
- [ ] Re-run restore and earlier tests; expect PASS.

### Task 4: Hash-checked undo and crash reconciliation

**Files:**
- Modify: `src/zcode_session_rescue/operations.py`, `src/zcode_session_rescue/receipt.py`
- Test: `tests/test_undo.py`

**Interfaces:**
- Produces: `undo_restore(receipt_path, apply=False, target_path=None) -> OperationResult`.
- Adds receipt states `undo_prepared` and `undone`; stores undo backup separately from the restore backup.

- [ ] Write failing tests for dry-run undo, successful composite-key undo, exact pre-restore state, repeated undo refusal, missing/changed row refusal, declared-FK dependency refusal, semantic dependency without FK refusal, schema drift refusal, and `prepared`/`undo_prepared` crash reconciliation.
- [ ] Run `python -I -m unittest tests.test_undo -v`; expect missing `undo_restore` failure.
- [ ] Implement receipt validation, all-present/all-deleted state classification, pre-undo backup and receipt, transactional row revalidation, declared and semantic reverse-reference checks, reverse-order delete, and atomic final state.
- [ ] Re-run the full suite; expect PASS.

### Task 5: CLI and machine-readable behavior

**Files:**
- Create: `src/zcode_session_rescue/cli.py`, `src/zcode_session_rescue/__main__.py`
- Modify: `pyproject.toml`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces console entry `zcode-session-rescue` and `main(argv=None) -> int` with `list`, `check`, `restore`, `undo`.
- Exit codes: `0` success/dry-run, `2` CLI usage, `1` safety/operation error; unexpected bugs are not relabeled as success.

- [ ] Write failing subprocess/in-process tests for help, list, check, restore without/with `--apply`, undo, `--json`, invalid DB, concise errors, and Unicode/space paths.
- [ ] Run `python -I -m unittest tests.test_cli -v`; expect missing CLI failure.
- [ ] Implement argparse commands and JSON/text renderers with no tracebacks for expected `RescueError`; wire the console script.
- [ ] Re-run the full suite with `python -I -m unittest discover -s tests -v`; expect PASS.

### Task 6: Open-source documentation, CI, and independent verification

**Files:**
- Create: `README.md`, `LICENSE`, `CHANGELOG.md`, `SECURITY.md`, `.gitignore`, `.github/workflows/tests.yml`
- Modify: `pyproject.toml` metadata as needed

**Interfaces:**
- Documents exact supported scope, command examples using placeholders, backup/receipt privacy, crash behavior, and untested real-host acceptance.

- [ ] Add documentation checks: no personal paths/session IDs/secrets, referenced commands match CLI, every write example warns to close ZCode and use `--apply` deliberately.
- [ ] Run the complete suite and `python -I -m zcode_session_rescue --help` with `PYTHONPATH=src`; record exact output.
- [ ] Run `list` and `check` read-only against the real ZCode database using a synthetic target or a nonexistent-session check as appropriate; never run real restore/undo.
- [ ] Dispatch an independent read-only reviewer for correctness, safety, and spec coverage; fix verified findings and re-run tests.
- [ ] Report changed files, verification output, and explicit untested items. Do not claim real ZCode recovery, continuation, GUI acceptance, GitHub publication, or Release completion.
