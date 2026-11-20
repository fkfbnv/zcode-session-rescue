from __future__ import annotations

import argparse
import json
import sys

from .errors import RescueError
from .operations import restore_session, undo_restore
from .planner import build_restore_plan, list_sessions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zcode-session-rescue",
        description="Restore one ordinary ZCode chat session safely, with a verifiable undo.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List sessions in an old database without reading message bodies")
    listing.add_argument("--source", required=True, help="Old ZCode db.sqlite")
    listing.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    check = commands.add_parser("check", help="Build a read-only restore plan")
    check.add_argument("--source", required=True, help="Old ZCode db.sqlite")
    check.add_argument("--target", required=True, help="Existing target ZCode db.sqlite")
    check.add_argument("--session", required=True, help="Session ID to restore")
    check.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    restore = commands.add_parser("restore", help="Back up and restore one session; dry-run by default")
    restore.add_argument("--source", required=True, help="Old ZCode db.sqlite")
    restore.add_argument("--target", required=True, help="Existing target ZCode db.sqlite")
    restore.add_argument("--session", required=True, help="Session ID to restore")
    restore.add_argument("--operations-dir", required=True, help="Directory for backups and receipts")
    restore.add_argument("--apply", action="store_true", help="Actually write to the target database")
    restore.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    undo = commands.add_parser("undo", help="Undo an unchanged restore; dry-run by default")
    undo.add_argument("--receipt", required=True, help="Restore receipt.json")
    undo.add_argument("--target", help="Override the receipt target path for a moved test database")
    undo.add_argument("--apply", action="store_true", help="Actually delete the unchanged restored rows")
    undo.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def _print_payload(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if "sessions" in payload:
        for session in payload["sessions"]:
            print(
                f"{session['id']}\t{session['message_count']} messages\t{session['title'] or ''}"
                f"\tcreated={session['time_created']}\tupdated={session['time_updated']}"
            )
        return
    print(f"Action: {payload.get('action', 'check')}")
    print(f"Status: {payload.get('status', 'dry_run')}")
    if payload.get("plan"):
        print(f"Session: {payload['plan']['session_id']}")
        for table, count in payload["plan"]["row_counts"].items():
            print(f"  {table}: {count} rows")
        if payload["plan"].get("skipped_tables"):
            print("Skipped: " + ", ".join(payload["plan"]["skipped_tables"]))
    if payload.get("row_counts"):
        for table, count in payload["row_counts"].items():
            print(f"  {table}: {count} rows")
    if payload.get("receipt_path"):
        print(f"Receipt: {payload['receipt_path']}")
    if payload.get("backup_path"):
        print(f"Backup: {payload['backup_path']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            payload = {"sessions": [session.to_dict() for session in list_sessions(args.source)]}
        elif args.command == "check":
            plan = build_restore_plan(args.source, args.target, args.session)
            payload = {"action": "check", "status": "dry_run", "applied": False, "plan": plan.to_dict()}
        elif args.command == "restore":
            result = restore_session(
                args.source,
                args.target,
                args.session,
                args.operations_dir,
                apply=args.apply,
            )
            payload = result.to_dict()
        else:
            result = undo_restore(args.receipt, apply=args.apply, target_path=args.target)
            payload = result.to_dict()
        _print_payload(payload, args.json)
        return 0
    except RescueError as error:
        if args.json:
            print(
                json.dumps({"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False),
                file=sys.stderr,
            )
        else:
            print(f"error [{error.code}]: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
