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


def _count_objects(adapter) -> int | None:
    """Object count for reporting, or None if it cannot be determined.

    Deliberately swallows errors: this runs after a copy has already
    succeeded, so a failure to *describe* the result must not turn a good
    copy into a reported failure.
    """
    try:
        return adapter.object_count()
    except Exception:
        return None


def copy_database(
    source_url: str,
    target_url: str,
    *,
    create_target: bool = True,
    overwrite: bool = False,
    skip_missing_extensions: bool = False,
) -> dict:
    """Full live copy of source -> target (streamed, no temp file).

    Source and target must currently be the same database type.
    With overwrite=True the target database is dropped and recreated
    before copying (destructive).

    Returns a summary of what was copied — object counts (None if the
    adapter cannot tell) plus where the data landed — so callers can say
    more than "done" and a copy that moved nothing is never reported as a
    plain success.
    """
    source = get_adapter(source_url)
    target = get_adapter(target_url)
    if type(source) is not type(target):
        raise ValueError(
            "Cross-database copy (e.g. Postgres -> MySQL) is not supported."
        )
    source.test_connection()
    source.copy_to(
        target,
        create_target=create_target,
        overwrite=overwrite,
        skip_missing_extensions=skip_missing_extensions,
    )
    return {
        "source_objects": _count_objects(source),
        "target_objects": _count_objects(target),
        "target_database": target.info.database,
        "target_endpoint": f"{target.info.host}:{target.info.port}",
    }


def clean_database(url: str) -> None:
    """Remove ALL user objects from the database, leaving it empty.

    Destructive — callers (CLI/UI) are responsible for confirming intent.
    """
    adapter = get_adapter(url)
    adapter.test_connection()
    adapter.clean_database()
