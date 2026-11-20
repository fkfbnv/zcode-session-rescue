# ZCode Session Rescue

Safely restore **one ordinary ZCode chat session** from an old `db.sqlite`, then undo that exact restore if it was a mistake.

This is not a whole-database merge tool. It is deliberately conservative: incompatible schemas, existing sessions, key conflicts, workflow links, unknown associations, and partial recovery conditions stop the operation before writing.

## Safety properties

- Dry-run by default. Only an explicit `--apply` changes a database.
- The source database is opened read-only.
- The target database is backed up with SQLite's online Backup API before every write.
- Ordinary `INSERT` is used; conflicts are never silently ignored.
- A JSON receipt records table names, primary keys, row hashes, counts, and backup paths—not message bodies.
- Undo verifies every restored row is unchanged and checks declared and semantic dependencies before deleting anything.
- Applied operations on the same target are serialized with a cross-process `.rescue.lock` file beside the target database.
- Unrecognized triggers and cascading/`SET NULL` foreign-key actions are rejected. Built-in ZCode guard/sequence triggers and official delete-action relationships are supported.
- `AUTOINCREMENT` restore tables and NULL primary-key components are rejected in this version.
- Crash states (`prepared` and `undo_prepared`) can be reconciled on the next run.
- No telemetry, upload, auto-update, or network access.

Backups contain the complete target database and are sensitive. Receipts contain local paths and session/row identifiers. Store both with the same care as the original database.

## Supported scope

The first version supports ordinary chats whose records are contained in `db.sqlite`, including dynamically discovered session-owned tables and composite primary keys.

It does **not** currently restore:

- workflow or DWF runs;
- parent/child session trees or cross-session task links;
- `tasks-index.sqlite`, rollout files, artifacts, or workspace files;
- damaged SQLite databases;
- sessions that already exist in the target.

Runtime-presence rows are intentionally excluded. Purged or otherwise unsupported sessions are rejected rather than partially restored.

## Requirements

- Python 3.11 or later.
- A source ZCode SQLite backup and an existing target `db.sqlite`.
- Close every ZCode instance that could write the target before using `--apply`.

## Install

```bash
python -m pip install .
```

For development, install in editable mode:

```bash
python -m pip install -e .
```

## 1. List sessions

```bash
zcode-session-rescue list --source OLD_DB
```

This prints session IDs, titles, timestamps, and message counts without printing message bodies.

## 2. Check a restore

```bash
zcode-session-rescue check \
  --source OLD_DB \
  --target TARGET_DB \
  --session SESSION_ID
```

The check is read-only. It reports the selected tables and row counts or explains why the restore is refused.

## 3. Restore one session

Close ZCode first. Run without `--apply` to repeat the dry-run:

```bash
zcode-session-rescue restore \
  --source OLD_DB \
  --target TARGET_DB \
  --session SESSION_ID \
  --operations-dir OUTPUT_DIR
```

After reviewing the plan, apply it deliberately:

```bash
zcode-session-rescue restore \
  --source OLD_DB \
  --target TARGET_DB \
  --session SESSION_ID \
  --operations-dir OUTPUT_DIR \
  --apply
```

Each applied operation creates a unique directory containing:

- `target-before.sqlite`: the pre-restore backup;
- `receipt.json`: the operation plan, row hashes, and final status.

Do not delete or edit the receipt if you may need undo.

## 4. Undo a restore

Close ZCode first. Check undo without changing anything:

```bash
zcode-session-rescue undo --receipt RECEIPT_JSON
```

Apply undo only when every restored row is unchanged and no new dependency blocks deletion:

```bash
zcode-session-rescue undo --receipt RECEIPT_JSON --apply
```

If you opened the restored session and ZCode modified it, or continued the conversation, undo is expected to refuse. Use the pre-restore backup for a deliberate whole-database rollback instead; that is a separate operation and is not performed automatically.

## JSON output and exit codes

Add `--json` to any command for machine-readable output:

```bash
zcode-session-rescue check \
  --source OLD_DB \
  --target TARGET_DB \
  --session SESSION_ID \
  --json
```

- `0`: successful command or dry-run;
- `1`: safety, validation, or operation error;
- `2`: command-line usage error.

## Development and verification

```bash
python -m unittest discover -s tests -t . -v
```

Tests build temporary synthetic ZCode databases. They never require or modify a real user database.

Current validation covers schema inspection, composite keys, BLOB and Unicode values, conflicts, rollback, backup integrity, receipt crash states, exact undo, semantic dependencies, CLI behavior, and special-character paths.

**Not yet accepted:** opening a restored session in the real ZCode application, continuing that conversation, and GUI-level behavior. Those require a separate real-host acceptance pass.

## License

MIT. See [LICENSE](LICENSE).
