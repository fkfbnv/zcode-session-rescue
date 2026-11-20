# Security

ZCode Session Rescue processes local conversation databases. Backups contain full database contents; receipts contain paths and identifiers. Do not attach either to public issues.

When reporting a vulnerability, use the repository's private security-advisory channel after publication. Include the affected version, a synthetic reproduction, expected behavior, and actual behavior. Never include real credentials, message bodies, or a personal ZCode database.

Expected validation errors exit with code `1`. A refusal to restore or undo is a safety result, not a reason to bypass checks with raw SQL.
