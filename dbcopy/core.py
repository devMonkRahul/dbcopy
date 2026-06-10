"""Core operations — the layer both the CLI and a future web UI call.

Keep this free of any CLI/printing concerns so it can be imported by a
FastAPI app later without changes.
"""

from __future__ import annotations

import datetime
import os

from .adapters import get_adapter


def backup_database(url: str, output_path: str | None = None) -> str:
    """Dump `url` to a file. Auto-names the file if none given."""
    adapter = get_adapter(url)
    adapter.test_connection()
    if output_path is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"{adapter.info.database}_{stamp}.dump"
    return adapter.backup(os.path.abspath(output_path))


def restore_database(url: str, input_path: str, *, clean: bool = False) -> None:
    """Restore a dump file into `url`."""
    adapter = get_adapter(url)
    adapter.check_tools()
    adapter.restore(input_path, clean=clean)


def copy_database(
    source_url: str,
    target_url: str,
    *,
    create_target: bool = True,
    overwrite: bool = False,
) -> None:
    """Full live copy of source -> target (streamed, no temp file).

    Source and target must currently be the same database type.
    With overwrite=True the target database is dropped and recreated
    before copying (destructive).
    """
    source = get_adapter(source_url)
    target = get_adapter(target_url)
    if type(source) is not type(target):
        raise ValueError(
            "Cross-database copy (e.g. Postgres -> MySQL) is not supported."
        )
    source.test_connection()
    source.copy_to(target, create_target=create_target, overwrite=overwrite)


def clean_database(url: str) -> None:
    """Remove ALL user objects from the database, leaving it empty.

    Destructive — callers (CLI/UI) are responsible for confirming intent.
    """
    adapter = get_adapter(url)
    adapter.test_connection()
    adapter.clean_database()
