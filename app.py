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
from urllib.parse import urlparse

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
    """Connection string with the password hidden, for display/logs."""
    parsed = urlparse(url)
    if parsed.password:
        netloc = f"{parsed.username}:****@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return parsed._replace(netloc=netloc).geturl()
    return url


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


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
        get_adapter(req.url).test_connection()
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
        core.clean_database(req.url)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@app.post("/api/copy")
def start_copy(req: CopyRequest):
    try:
        # Fail fast on bad URLs / cross-engine copies before spawning the job.
        source = get_adapter(req.source_url)
        target = get_adapter(req.target_url)
        if type(source) is not type(target):
            raise ValueError("Cross-database copy (e.g. Postgres -> MySQL) is not supported.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "operation": "copy",
            "source": _redact(req.source_url),
            "target": _redact(req.target_url),
            "status": "pending",
            "error": None,
            "started_at": _now(),
            "finished_at": None,
        }
    threading.Thread(
        target=_run_copy,
        args=(job_id, req.source_url, req.target_url,
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
