# Changelog

## 0.1.1 - 2026-11-20

### Fixed

- Filesystem errors (`PermissionError`, a full disk, a locked path) now surface as
  one `error [io_error]: ...` line or a JSON error object instead of a traceback.

### Added

- `.gitattributes` pins LF line endings in the repository.
- POSIX: operation directories are created `0700`; backups and receipts are `0600`.

### Tests

- An interrupted insert rolls back without leaving rows behind and marks the receipt `failed`.
- A crash after commit (receipt left `prepared`, rows already inserted) is recoverable by undo.
- BLOB and Unicode primary keys round-trip through restore and undo.
- POSIX-only cases: `?` in file or directory names, owner-only permission bits.

## 0.1.0 - 2026-11-20

### Added

- Read-only session listing and strict restore planning.
- Schema-driven discovery of ordinary session-owned tables.
- Explicit dry-run and `--apply` modes.
- SQLite backup, pre-written JSON receipts, and transactional restore.
- Hash-verified undo with dependency checks and crash-state reconciliation.
- Human-readable and JSON CLI output.
- Synthetic SQLite test suite and cross-platform CI.

### Hardened

- Full foreign-key metadata, restore-closure validation, and batched conflict queries.
- Unknown-trigger and unrecognized cascading-action rejection, with verified built-in ZCode compatibility.
- `AUTOINCREMENT` and NULL-primary-key rejection.
- Column-order-independent row hashes and post-insert content verification.
- Strict receipt validation and cross-process operation locking.
- Semantic reference checks for restored identifiers, including non-PK and implicit-PK foreign keys.
