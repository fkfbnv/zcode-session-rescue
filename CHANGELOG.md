# Changelog

## Unreleased

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
