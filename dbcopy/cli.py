"""Command-line interface.

Usage:
  python -m dbcopy backup  postgresql://user:pass@host:5432/mydb -o mydb.dump
  python -m dbcopy restore postgresql://user:pass@host:5432/mydb -i mydb.dump
  python -m dbcopy copy    postgresql://u:p@src:5432/proddb  postgresql://u:p@dst:5432/staging
"""

from __future__ import annotations

import argparse
import sys

from . import core


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbcopy",
        description="Backup, restore, and copy databases (PostgreSQL today, more later).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Dump a database to a file")
    p_backup.add_argument("url", help="Database URL, e.g. postgresql://user:pass@host:5432/db")
    p_backup.add_argument("-o", "--output", help="Output file (default: <db>_<timestamp>.dump)")

    p_restore = sub.add_parser("restore", help="Restore a dump file into a database")
    p_restore.add_argument("url", help="Target database URL")
    p_restore.add_argument("-i", "--input", required=True, help="Dump file to restore")
    p_restore.add_argument(
        "--clean", action="store_true",
        help="Drop existing objects in the target before restoring",
    )

    p_copy = sub.add_parser("copy", help="Full copy of one database into another")
    p_copy.add_argument("source_url", help="Source database URL")
    p_copy.add_argument("target_url", help="Target database URL")
    p_copy.add_argument(
        "--no-create", action="store_true",
        help="Do not auto-create the target database if it is missing",
    )
    p_copy.add_argument(
        "--overwrite", action="store_true",
        help="DROP and recreate the target database before copying "
             "(use when the target already contains objects)",
    )

    p_clean = sub.add_parser(
        "clean", help="Remove ALL tables and objects from a database (destructive)",
    )
    p_clean.add_argument("url", help="Database URL to clean")
    p_clean.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt",
    )

    return parser


def _confirm(prompt: str) -> bool:
    reply = input(f"{prompt} [y/N] ")
    return reply.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            path = core.backup_database(args.url, args.output)
            print(f"Backup written to {path}")
        elif args.command == "restore":
            core.restore_database(args.url, args.input, clean=args.clean)
            print("Restore complete")
        elif args.command == "copy":
            core.copy_database(
                args.source_url, args.target_url,
                create_target=not args.no_create,
                overwrite=args.overwrite,
            )
            print("Copy complete")
        elif args.command == "clean":
            if not args.yes and not _confirm(
                "This will permanently DELETE ALL tables and objects in the "
                "database. Continue?"
            ):
                print("Aborted.")
                return 1
            core.clean_database(args.url)
            print("Database cleaned")
        return 0
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
