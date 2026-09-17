"""Core operations — the layer both the CLI and a future web UI call.

Keep this free of any CLI/printing concerns so it can be imported by a
FastAPI app later without changes.
"""

from __future__ import annotations

import datetime
import os
from typing import Callable

from .adapters import get_adapter
from .engines import mongo

#: Why backup/restore/clean turn a MongoDB URL away. The other engines wrap
#: the vendor's dump tools, which produce a file; the MongoDB engine drives
#: the pymongo driver, which moves documents deployment-to-deployment and has
#: no dump format of its own.
_MONGO_COPY_ONLY = (
    "MongoDB supports copy only. dbcopy drives MongoDB through the pymongo "
    "driver, which has no dump-file format — use `dbcopy copy` (or the "
    "MongoDB screen in the dashboard) to move a database."
)


def _reject_mongo(url: str, alternative: str = "") -> None:
    """Raise a clear ValueError if `url` is MongoDB, which is copy-only."""
    if mongo.is_mongo_url(url):
        raise ValueError(f"{_MONGO_COPY_ONLY} {alternative}".strip())


def backup_database(url: str, output_path: str | None = None) -> str:
    """Dump `url` to a file. Auto-names the file if none given."""
    _reject_mongo(url)
    adapter = get_adapter(url)
    adapter.test_connection()
    if output_path is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"{adapter.info.database}_{stamp}.dump"
    return adapter.backup(os.path.abspath(output_path))


def restore_database(url: str, input_path: str, *, clean: bool = False) -> None:
    """Restore a dump file into `url`."""
    _reject_mongo(url)
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


def _copy_mongo(
    source_url: str,
    target_url: str,
    *,
    overwrite: bool,
    progress: Callable[[str], None] | None,
) -> dict:
    """Run a MongoDB copy to completion and summarise it like the others.

    pymongo is imported here, not at module scope, so PostgreSQL and MySQL
    runs never pay for a driver they do not use.
    """
    from .engines.mongo import copier

    source_db = mongo.database_in_url(source_url)
    if not source_db:
        raise ValueError(f"No database name found in URL: {source_url}")
    # An unnamed target means "same name as the source", matching the web UI.
    target_db = mongo.database_in_url(target_url) or source_db

    if source_url == target_url and source_db == target_db:
        raise ValueError("Source and target point at the same database.")

    job = copier.JobStore().create(source_db, target_db)
    job.on_log = progress
    copier.run_copy(
        job,
        source_uri=source_url,
        source_db=source_db,
        target_uri=target_url,
        target_db=target_db,
        drop_target=overwrite,
    )

    # run_copy reports failure on the job rather than raising, so that a web
    # caller polling the job sees it. A blocking caller wants the exception —
    # re-raised as ValueError when the cause was bad input, so the CLI and
    # app.py classify it the same way they do for the other engines.
    snapshot = job.snapshot()
    if snapshot["state"] != "done":
        if isinstance(job.failure, ValueError):
            raise ValueError(str(job.failure))
        raise RuntimeError(snapshot["error"] or f"MongoDB copy {snapshot['state']}")

    collections = snapshot["collections"]
    return {
        "source_objects": len(collections),
        "target_objects": sum(1 for c in collections if c["state"] == "done"),
        "target_database": target_db,
        "target_endpoint": mongo.endpoint_of(target_url),
        "object_label": "collection",
    }


def _resolve_copy(source_url: str, target_url: str):
    """Work out which engine a copy runs on, without connecting to anything.

    Returns the two adapters for a tool-backed copy, or ``(None, None)`` when
    the pair is MongoDB and the pymongo engine takes over. Raises ValueError
    for a pair no engine can serve, so callers can reject a bad request
    before starting a job.
    """
    source_is_mongo = mongo.is_mongo_url(source_url)
    if source_is_mongo != mongo.is_mongo_url(target_url):
        raise ValueError(
            "Cross-database copy (e.g. Postgres -> MongoDB) is not supported."
        )
    if source_is_mongo:
        return None, None

    source = get_adapter(source_url)
    target = get_adapter(target_url)
    if type(source) is not type(target):
        raise ValueError(
            "Cross-database copy (e.g. Postgres -> MySQL) is not supported."
        )
    return source, target


def check_copy_pair(source_url: str, target_url: str) -> None:
    """Raise ValueError if these two URLs cannot be copied between.

    Connects to nothing — the web layer uses it to answer a bad request with
    HTTP 400 instead of spawning a job that is certain to fail.
    """
    _resolve_copy(source_url, target_url)


def copy_database(
    source_url: str,
    target_url: str,
    *,
    create_target: bool = True,
    overwrite: bool = False,
    skip_missing_extensions: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Full live copy of source -> target (streamed, no temp file).

    Source and target must currently be the same database type.
    With overwrite=True the target database is dropped and recreated
    before copying (destructive).

    `progress` is an optional sink for human-readable progress lines. Only
    the MongoDB engine reports them; the tool-based engines leave it unused,
    since pg_dump and mysqldump write their own progress to stderr.

    Returns a summary of what was copied — object counts (None if the
    adapter cannot tell) plus where the data landed — so callers can say
    more than "done" and a copy that moved nothing is never reported as a
    plain success.
    """
    source, target = _resolve_copy(source_url, target_url)
    if source is None:
        return _copy_mongo(
            source_url, target_url, overwrite=overwrite, progress=progress
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
        "object_label": "table/view",
    }


def clean_database(url: str) -> None:
    """Remove ALL user objects from the database, leaving it empty.

    Destructive — callers (CLI/UI) are responsible for confirming intent.
    """
    _reject_mongo(url, "To replace a MongoDB database, copy with --overwrite.")
    adapter = get_adapter(url)
    adapter.test_connection()
    adapter.clean_database()
