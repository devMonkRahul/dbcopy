"""Copy an entire MongoDB database from one deployment to another.

The engine is deliberately dependency-light: it streams documents with a
cursor and writes them back with unordered bulk inserts, so memory stays
flat no matter how large the source is.

Imports nothing from FastAPI or from the CLI on purpose — :mod:`.routes`
drives it over HTTP and ``dbcopy.core`` drives it from the command line, and
both use exactly the API listed below:

    inspect(uri)                  -> server version + database list
    JobStore().create(src, dst)   -> a Job to hand to run_copy
    run_copy(job, **params)       -> blocking; mutates the job
    Job.snapshot()                -> plain JSON-safe dict
    Job.cancel()                  -> sets the stop flag

Copied: every non-system collection, its documents, its collection options
(capped, time-series, validators, collation), its secondary indexes, and
views. GridFS comes along free — ``.files`` and ``.chunks`` are ordinary
collections. Not copied: users, roles and server settings, which live in
``admin`` and belong to the deployment rather than to the database.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from pymongo import IndexModel, MongoClient
from pymongo.errors import (
    BulkWriteError,
    CollectionInvalid,
    OperationFailure,
    PyMongoError,
)

# Databases that belong to the server, not to the user.
SYSTEM_DBS = {"admin", "local", "config"}

# Keys returned by listCollections that must not be replayed into create().
_DROP_CREATE_OPTS = {"idIndex", "info", "type", "name"}

# Index metadata that the server owns and rejects on creation.
_DROP_INDEX_OPTS = {
    "v",
    "ns",
    "key",
    "textIndexVersion",
    "2dsphereIndexVersion",
    "background",
}


def make_client(uri: str, timeout_ms: int = 8000) -> MongoClient:
    """Build a client that fails fast instead of hanging on a bad URI."""
    return MongoClient(
        uri,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=0,  # long-running cursors must not time out
        appname="dbcopy",
    )


def default_db_from_uri(uri: str) -> str | None:
    """Return the database named in the URI path, if there is one."""
    try:
        client = make_client(uri, timeout_ms=2000)
    except PyMongoError:
        return None
    try:
        name = client.get_default_database().name
        return None if name in (None, "admin") else name
    except Exception:
        return None
    finally:
        client.close()


def inspect(uri: str) -> dict[str, Any]:
    """Ping a deployment and list its user databases."""
    client = make_client(uri)
    try:
        client.admin.command("ping")
        listing = client.list_databases()
        databases = [
            {
                "name": db["name"],
                "size_on_disk": int(db.get("sizeOnDisk") or 0),
                "system": db["name"] in SYSTEM_DBS,
            }
            for db in listing
        ]
        try:
            build = client.admin.command("buildInfo")
            version = build.get("version", "unknown")
        except OperationFailure:
            version = "unknown"
        return {
            "ok": True,
            "version": version,
            "databases": sorted(databases, key=lambda d: (d["system"], d["name"])),
            "default_db": default_db_from_uri(uri),
        }
    finally:
        client.close()


def ping(uri: str) -> None:
    """Raise if the deployment cannot be reached, return None if it can.

    ``inspect`` needs the listDatabases privilege, which a user scoped to a
    single database does not have; a plain ping only needs to connect. Used
    by the dashboard's Test-connection button so a least-privilege user is
    not reported as unreachable.
    """
    client = make_client(uri)
    try:
        client.admin.command("ping")
    finally:
        client.close()


# --------------------------------------------------------------------------
# Job bookkeeping
# --------------------------------------------------------------------------


@dataclass
class CollectionProgress:
    name: str
    total: int = 0
    copied: int = 0
    skipped: int = 0
    indexes: int = 0
    state: str = "pending"  # pending | copying | done | failed | skipped


@dataclass
class Job:
    id: str
    source_db: str
    target_db: str
    state: str = "queued"  # queued | running | done | failed | cancelled
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    total_docs: int = 0
    copied_docs: int = 0
    collections: list[CollectionProgress] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    #: Optional live sink for log lines — the CLI prints them as they happen.
    #: The web layer leaves it unset and reads the log out of snapshot().
    on_log: Callable[[str], None] | None = None
    #: What actually went wrong, kept so a blocking caller can re-raise with
    #: the original type (bad input stays a ValueError). ``error`` is the
    #: display form; this is never serialised into snapshot().
    failure: BaseException | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        self._cancel.set()
        self.say("Cancellation requested.")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def say(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        with self._lock:
            self.log.append(line)
            del self.log[:-400]  # keep the tail bounded
        if self.on_log is not None:
            try:
                self.on_log(line)
            except Exception:
                pass  # a broken log sink must never abort a running copy

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "state": self.state,
                "error": self.error,
                "source_db": self.source_db,
                "target_db": self.target_db,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "total_docs": self.total_docs,
                "copied_docs": self.copied_docs,
                "percent": (
                    round(100 * self.copied_docs / self.total_docs, 1)
                    if self.total_docs
                    else (100.0 if self.state == "done" else 0.0)
                ),
                "collections": [vars(c) for c in self.collections],
                "log": list(self.log),
            }


class JobStore:
    """In-memory registry. Swap for Redis if you run more than one worker."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, source_db: str, target_db: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], source_db=source_db, target_db=target_db)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)


# --------------------------------------------------------------------------
# The copy itself
# --------------------------------------------------------------------------


def _batches(cursor: Iterator[dict], size: int) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for doc in cursor:
        batch.append(doc)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _clean(options: dict[str, Any], drop: set[str]) -> dict[str, Any]:
    return {k: v for k, v in options.items() if k not in drop}


def run_copy(
    job: Job,
    *,
    source_uri: str,
    source_db: str,
    target_uri: str,
    target_db: str,
    drop_target: bool = False,
    batch_size: int = 1000,
    include: list[str] | None = None,
    copy_indexes: bool = True,
) -> None:
    """Execute the copy. Blocking — call it on a worker thread.

    Never raises: every failure lands in ``job.state``/``job.error`` so a
    caller polling the job sees the same outcome whether it ran here or on a
    thread pool. Callers that want an exception check the state afterwards.
    """
    job.state = "running"
    job.started_at = datetime.now(timezone.utc).isoformat()
    source = target = None

    try:
        source = make_client(source_uri)
        target = make_client(target_uri)
        source.admin.command("ping")
        target.admin.command("ping")

        src = source[source_db]
        dst = target[target_db]

        if source_uri == target_uri and source_db == target_db:
            raise ValueError("Source and target point at the same database.")

        entries = [
            entry
            for entry in src.list_collections()
            if not entry["name"].startswith("system.")
            and (include is None or entry["name"] in include)
        ]
        if not entries:
            raise ValueError(f"No collections found in '{source_db}'.")

        job.say(f"Found {len(entries)} collections in '{source_db}'.")

        if drop_target:
            target.drop_database(target_db)
            job.say(f"Dropped target database '{target_db}'.")

        # Views are created last: they can reference other collections.
        views = [e for e in entries if e.get("type") == "view"]
        colls = [e for e in entries if e.get("type") != "view"]

        job.collections = [CollectionProgress(name=e["name"]) for e in colls + views]
        progress = {c.name: c for c in job.collections}

        total = 0
        for entry in colls:
            count = src[entry["name"]].estimated_document_count()
            progress[entry["name"]].total = count
            total += count
        job.total_docs = total
        job.say(f"About {total:,} documents to copy.")

        for entry in colls:
            if job.cancelled:
                break
            _copy_one(
                job=job,
                progress=progress[entry["name"]],
                src_coll=src[entry["name"]],
                dst_db=dst,
                entry=entry,
                batch_size=batch_size,
                copy_indexes=copy_indexes,
            )

        for entry in views:
            if job.cancelled:
                break
            row = progress[entry["name"]]
            try:
                options = _clean(entry.get("options", {}), _DROP_CREATE_OPTS)
                _create_view(dst, entry["name"], options)
                row.state = "done"
                job.say(f"Recreated view '{entry['name']}'.")
            except (OperationFailure, CollectionInvalid) as exc:
                row.state = "failed"
                job.say(f"View '{entry['name']}' failed: {exc}")

        if job.cancelled:
            job.state = "cancelled"
            job.say("Copy cancelled.")
        else:
            job.state = "done"
            job.say(f"Copy finished: {job.copied_docs:,} documents into '{target_db}'.")

    except Exception as exc:  # surfaced verbatim in the UI
        job.state = "failed"
        job.failure = exc
        job.error = f"{type(exc).__name__}: {exc}"
        job.say(f"Failed — {job.error}")
    finally:
        job.finished_at = datetime.now(timezone.utc).isoformat()
        for client in (source, target):
            if client is not None:
                client.close()


def _create_view(dst_db, name: str, options: dict[str, Any]) -> None:
    """Create a view, replacing one that is already there.

    A view holds no data, so redefining it costs nothing and leaves the
    target matching the source. Without this a second copy into the same
    target reports every view as failed — views hit the same already-exists
    pair of errors that ``_copy_one`` tolerates for collections (pymongo
    raises CollectionInvalid client-side, the server returns code 48).
    """
    try:
        dst_db.create_collection(name, **options)
        return
    except CollectionInvalid:
        pass
    except OperationFailure as exc:
        if exc.code != 48:  # 48 = NamespaceExists
            raise
    dst_db.drop_collection(name)
    dst_db.create_collection(name, **options)


def _copy_one(
    *,
    job: Job,
    progress: CollectionProgress,
    src_coll,
    dst_db,
    entry: dict[str, Any],
    batch_size: int,
    copy_indexes: bool,
) -> None:
    name = entry["name"]
    progress.state = "copying"
    job.say(f"Copying '{name}' ({progress.total:,} docs)...")

    options = _clean(entry.get("options", {}), _DROP_CREATE_OPTS)
    try:
        dst_db.create_collection(name, **options)
    except CollectionInvalid:
        pass  # already there — we append into it
    except OperationFailure as exc:
        if exc.code != 48:  # 48 = NamespaceExists, also fine
            progress.state = "failed"
            job.say(f"Could not create '{name}': {exc}")
            return

    dst_coll = dst_db[name]
    started = time.monotonic()

    try:
        cursor = src_coll.find({}, no_cursor_timeout=True).batch_size(batch_size)
        try:
            for batch in _batches(cursor, batch_size):
                if job.cancelled:
                    progress.state = "skipped"
                    return
                try:
                    result = dst_coll.insert_many(batch, ordered=False)
                    written = len(result.inserted_ids)
                except BulkWriteError as bwe:
                    duplicates = [
                        e for e in bwe.details.get("writeErrors", []) if e["code"] == 11000
                    ]
                    others = [
                        e for e in bwe.details.get("writeErrors", []) if e["code"] != 11000
                    ]
                    if others:
                        raise
                    written = bwe.details.get("nInserted", 0)
                    progress.skipped += len(duplicates)
                progress.copied += written
                job.copied_docs += written
        finally:
            cursor.close()

        if copy_indexes:
            progress.indexes = _copy_indexes(job, src_coll, dst_coll)

        progress.state = "done"
        elapsed = time.monotonic() - started
        rate = progress.copied / elapsed if elapsed > 0.01 else 0
        note = f" ({progress.skipped:,} duplicates skipped)" if progress.skipped else ""
        job.say(f"'{name}' done — {progress.copied:,} docs at {rate:,.0f}/s{note}.")

    except PyMongoError as exc:
        progress.state = "failed"
        job.say(f"'{name}' failed: {exc}")


def _copy_indexes(job: Job, src_coll, dst_coll) -> int:
    models: list[IndexModel] = []
    for index in src_coll.list_indexes():
        if index["name"] == "_id_":
            continue
        options = _clean(index, _DROP_INDEX_OPTS)
        options.pop("name", None)
        models.append(IndexModel(list(index["key"].items()), name=index["name"], **options))
    if not models:
        return 0
    try:
        dst_coll.create_indexes(models)
        return len(models)
    except OperationFailure as exc:
        job.say(f"Indexes on '{src_coll.name}' partially failed: {exc}")
        return 0
