"""Command-line interface.

Usage:
  python -m dbcopy backup  postgresql://user:pass@host:5432/mydb -o mydb.dump
  python -m dbcopy restore postgresql://user:pass@host:5432/mydb -i mydb.dump
  python -m dbcopy copy    postgresql://u:p@src:5432/proddb  postgresql://u:p@dst:5432/staging
  python -m dbcopy copy    mongodb://u:p@src:27017/proddb    mongodb://u:p@dst:27017/staging
  python -m dbcopy copy    mysql://u:p@src:3306/proddb       mysql://u:p@dst:3306/staging
"""

from __future__ import annotations

import argparse
import sys

from . import core, web


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbcopy",
        description="Backup, restore, and copy databases "
                    "(PostgreSQL and MySQL; MongoDB is copy-only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Dump a database to a file")
    p_backup.add_argument(
        "url",
        help="Database URL, e.g. postgresql://user:pass@host:5432/db "
             "or mysql://user:pass@host:3306/db",
    )
    p_backup.add_argument("-o", "--output", help="Output file (default: <db>_<timestamp>.dump)")

    p_restore = sub.add_parser("restore", help="Restore a dump file into a database")
    p_restore.add_argument("url", help="Target database URL")
    p_restore.add_argument("-i", "--input", required=True, help="Dump file to restore")
    p_restore.add_argument(
        "--clean", action="store_true",
        help="Drop existing objects in the target before restoring",
    )

    p_copy = sub.add_parser(
        "copy",
        help="Full copy of one database into another "
             "(PostgreSQL, MySQL or MongoDB)",
    )
    p_copy.add_argument("source_url", help="Source database URL")
    p_copy.add_argument("target_url", help="Target database URL")
    p_copy.add_argument(
        "--no-create", action="store_true",
        help="Do not auto-create the target database if it is missing",
    )
    p_copy.add_argument(
        "--skip-missing-extensions", action="store_true",
        help="PostgreSQL: copy without the extensions the target server does "
             "not have installed, instead of failing (lossy: objects that "
             "depend on them will not copy)",
    )
    p_copy.add_argument(
        "--overwrite", action="store_true",
        help="DROP and recreate the target database before copying "
             "(use when the target already contains objects). For "
             "MongoDB the target database is dropped before the "
             "restore; without it a repeated copy skips documents "
             "that are already there",
    )

    p_clean = sub.add_parser(
        "clean",
        help="Remove ALL tables and objects from a database (destructive; "
             "PostgreSQL and MySQL only - for MongoDB use copy --overwrite)",
    )
    p_clean.add_argument("url", help="Database URL to clean")
    p_clean.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt",
    )

    sub.add_parser("dashboard", help="Run the web dashboard to monitor copy jobs")

    return parser


#: Plural of each engine's object_label. Pluralisation is presentation, so it
#: lives here rather than in core.
_PLURALS = {"table/view": "tables/views", "collection": "collections"}


def _copy_summary(summary: dict) -> str:
    """Human-readable outcome of a copy.

    Always names the database the data actually landed in and how much of it
    arrived: a copy that silently moved nothing (wrong database name in the
    source URL) otherwise looks exactly like a successful one.
    """
    target = f'"{summary["target_database"]}" at {summary["target_endpoint"]}'
    singular = summary.get("object_label", "table/view")
    plural = _PLURALS.get(singular, f"{singular}s")
    if summary["source_objects"] == 0:
        return (
            "Copy complete, but nothing was copied: the source database "
            f"contains no {plural}. Check the database name in the source URL."
        )
    count = summary["target_objects"]
    if count is None:
        return f"Copy complete into {target}"
    return f"Copy complete: {target} now holds {count} {singular if count == 1 else plural}"


def _progress(line: str) -> None:
    """Live progress sink. Only the MongoDB engine reports through it -- the
    tool-based engines let pg_dump / mysqldump write their own to stderr."""
    print(line, flush=True)


def _confirm(prompt: str) -> bool:
    reply = input(f"{prompt} [y/N] ")
    return reply.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    
    # If no arguments provided, show help
    if argv is None and len(sys.argv) == 1:
        parser.print_help()
        return 0
    
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            path = core.backup_database(args.url, args.output)
            print(f"Backup written to {path}")
        elif args.command == "restore":
            core.restore_database(args.url, args.input, clean=args.clean)
            print("Restore complete")
        elif args.command == "copy":
            summary = core.copy_database(
                args.source_url, args.target_url,
                create_target=not args.no_create,
                overwrite=args.overwrite,
                skip_missing_extensions=args.skip_missing_extensions,
                progress=_progress,
            )
            print(_copy_summary(summary))
        elif args.command == "clean":
            if not args.yes and not _confirm(
                "This will permanently DELETE ALL tables and objects in the "
                "database. Continue?"
            ):
                print("Aborted.")
                return 1
            core.clean_database(args.url)
            print("Database cleaned")

        elif args.command == "dashboard":
            web.run_dashboard()

        return 0
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
