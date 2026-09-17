"""HTTP layer for the MongoDB copy engine.

Mounted by ``app.py`` with ``app.include_router(router)``. Everything here is
a thin shell over :mod:`.copier`: validate, hand the work to a thread, and
report. The copy itself never runs on the event loop.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import PyMongoError

from .copier import JobStore, inspect, run_copy

# The page itself is not served here — app.py owns the human-facing URLs and
# serves this engine's screen at /mongodb. This router is the API only.
router = APIRouter(prefix="/api/engines/mongo", tags=["mongo"])
jobs = JobStore()
pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mongo-copy")


def _validate_uri(value: str) -> str:
    value = value.strip()
    if not value.startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError("URI must start with mongodb:// or mongodb+srv://")
    return value


class ConnectRequest(BaseModel):
    uri: str

    @field_validator("uri")
    @classmethod
    def check(cls, v: str) -> str:
        return _validate_uri(v)


class CopyRequest(BaseModel):
    source_uri: str
    source_db: str
    target_uri: str
    target_db: str = ""
    drop_target: bool = False
    copy_indexes: bool = True
    batch_size: int = Field(default=1000, ge=1, le=10000)
    collections: list[str] | None = None

    @field_validator("source_uri", "target_uri")
    @classmethod
    def check(cls, v: str) -> str:
        return _validate_uri(v)


@router.post("/connect")
async def connect(req: ConnectRequest) -> dict:
    """Verify a connection URI and list the databases behind it."""
    try:
        return await asyncio.get_running_loop().run_in_executor(
            pool, inspect, req.uri
        )
    except PyMongoError as exc:
        raise HTTPException(status_code=400, detail=f"Could not connect: {exc}") from exc


@router.post("/copy")
async def start_copy(req: CopyRequest) -> dict[str, str]:
    """Kick off a copy and return a job id to follow."""
    target_db = (req.target_db or req.source_db).strip()
    job = jobs.create(req.source_db, target_db)
    pool.submit(
        run_copy,
        job,
        source_uri=req.source_uri,
        source_db=req.source_db,
        target_uri=req.target_uri,
        target_db=target_db,
        drop_target=req.drop_target,
        batch_size=req.batch_size,
        include=req.collections,
        copy_indexes=req.copy_indexes,
    )
    return {"job_id": job.id}


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job.snapshot()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    job.cancel()
    return {"status": "cancelling"}


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """Server-sent events: one snapshot every 500 ms until the job settles."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")

    async def events():
        while True:
            snapshot = job.snapshot()
            yield f"data: {json.dumps(snapshot)}\n\n"
            if snapshot["state"] in {"done", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
