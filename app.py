"""dbcopy web dashboard.

Run with:  python main.py   (or: uvicorn app:app --reload)

A single-page dashboard where the user enters a source and a target
connection string, tests both connections, and starts a full copy.
Copies run in a background thread; the page polls the job-status
endpoint, so the request is never blocked (works for large databases).
"""

from __future__ import annotations

import datetime
import threading
import uuid

from urllib.parse import urlparse, urlsplit, urlunsplit, quote

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dbcopy import core
from dbcopy.adapters import get_adapter

app = FastAPI(title="dbcopy", description="Database backup / restore / copy")

# Enable CORS for all origins (needed when dashboard is accessed from different network/host)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- in-memory job store ----------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _redact(url: str) -> str:
    """Connection string with the password hidden, for display/logs.

    Works on the netloc directly (not urlparse().port) so MongoDB seed-list
    URLs like h1:27017,h2:27017 — which would trip the integer port cast —
    are handled too."""
    parts = urlsplit(url)
    creds, sep, hosts = parts.netloc.rpartition("@")
    if not sep or ":" not in creds:
        return url  # no credentials / no password to hide
    user = creds.split(":", 1)[0]
    netloc = f"{user}:****@{hosts}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _normalize_url(url: str) -> str:
    """Normalize URL by percent-encoding special characters in credentials.
    
    Handles characters: $ → %24, @ → %40, # → %23, [ → %5B, ] → %5D, : → %3A
    This allows users to paste URLs with unencoded special characters.
    """
    scheme_sep = "://"

    if scheme_sep not in url:
        raise ValueError("Invalid database URL")

    scheme, remainder = url.split(scheme_sep, 1)

    # Find the LAST @, which separates credentials from host
    at_pos = remainder.rfind("@")
    if at_pos == -1:
        return url

    credentials = remainder[:at_pos]
    host_part = remainder[at_pos:]

    if ":" not in credentials:
        return url

    username, password = credentials.split(":", 1)

    encoded_password = quote(password, safe="")

    return f"{scheme}://{username}:{encoded_password}{host_part}"


def _run_copy(
    job_id: str, source_url: str, target_url: str,
    create_target: bool, overwrite: bool,
) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
    try:
        core.copy_database(
            source_url, target_url,
            create_target=create_target, overwrite=overwrite,
        )
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(exc), finished_at=_now())
    else:
        with _jobs_lock:
            _jobs[job_id].update(status="done", finished_at=_now())


# ---- API --------------------------------------------------------------------

class CopyRequest(BaseModel):
    source_url: str
    target_url: str
    create_target: bool = True
    overwrite: bool = False


class TestRequest(BaseModel):
    url: str


@app.post("/api/test")
def test_connection(req: TestRequest):
    """Validate a connection string and try to reach the database.
    Sync endpoint: FastAPI runs it in a threadpool, so it never blocks."""
    try:
        normalized_url = _normalize_url(req.url)
        get_adapter(normalized_url).test_connection()
    except Exception as exc:
        message = str(exc)
        # Add helpful hints for common errors
        if "timeout expired" in message or "timed out" in message.lower():
            message += (
                "\nHint: the database server did not respond. For cloud "
                "databases (e.g. AWS RDS): the instance must be publicly "
                "accessible and its security group must allow your IP on "
                "the database port."
            )
        elif "invalid" in message.lower() and "url" in message.lower():
            message += (
                "\nHint: connection string format should be:\n"
                "  postgresql://user:password@host:5432/dbname\n"
                "  mongodb://user:password@host:27017/dbname\n"
                "If password contains special chars, use percent encoding:\n"
                "  $ → %24, @ → %40, # → %23, [ → %5B, ] → %5D, : → %3A\n"
                "Example: myP@ss$word[123] → myP%40ss%24word%5B123%5D"
            )
        return {"ok": False, "error": message}
    return {"ok": True}


@app.post("/api/clean")
def clean_database(req: TestRequest):
    """Remove ALL objects from the database. The UI confirms with the
    user before calling this; the endpoint itself does not ask twice."""
    try:
        normalized_url = _normalize_url(req.url)
        core.clean_database(normalized_url)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@app.post("/api/copy")
def start_copy(req: CopyRequest):
    try:
        # Normalize URLs to handle special characters in credentials
        source_url = _normalize_url(req.source_url)
        target_url = _normalize_url(req.target_url)
        
        # Fail fast on bad URLs / cross-engine copies before spawning the job.
        source = get_adapter(source_url)
        target = get_adapter(target_url)
        if type(source) is not type(target):
            raise ValueError("Cross-database copy (e.g. Postgres -> MySQL) is not supported.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "operation": "copy",
            "source": _redact(source_url),
            "target": _redact(target_url),
            "status": "pending",
            "error": None,
            "started_at": _now(),
            "finished_at": None,
        }
    threading.Thread(
        target=_run_copy,
        args=(job_id, source_url, target_url,
              req.create_target, req.overwrite),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        return sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)


# Serve the static files (HTML Dashboard) on the root path LAST
# to ensure API routes take precedence
app.mount("/", StaticFiles(directory="static", html=True), name="static")
