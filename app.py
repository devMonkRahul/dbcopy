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
from pydantic import BaseModel

from dbcopy import core
from dbcopy.adapters import get_adapter

app = FastAPI(title="dbcopy", description="Database backup / restore / copy")

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
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
        if "timeout expired" in message or "timed out" in message.lower():
            message += (
                "\nHint: the database server did not respond. For cloud "
                "databases (e.g. AWS RDS): the instance must be publicly "
                "accessible and its security group must allow your IP on "
                "the database port."
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


# ---- dashboard page ----------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dbcopy — database copy dashboard</title>
<style>
  :root {
    --bg: #0f1419; --panel: #1a2129; --border: #2c3640;
    --text: #e6e8ea; --muted: #8a949e; --accent: #4f9cf9;
    --ok: #3fbf7f; --err: #f0606a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; justify-content: center; padding: 40px 16px;
  }
  main { width: 100%; max-width: 880px; }
  h1 { font-size: 26px; margin: 0 0 4px; }
  h1 span { color: var(--accent); }
  .sub { color: var(--muted); margin-bottom: 28px; }
  .grid { display: grid; grid-template-columns: 1fr 44px 1fr; gap: 12px; align-items: stretch; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px;
  }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
             color: var(--muted); margin: 0 0 10px; }
  input[type=text], input[type=password] {
    width: 100%; padding: 10px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: #0d1117; color: var(--text);
    font: 13px/1.4 ui-monospace, Consolas, monospace;
  }
  input:focus { outline: none; border-color: var(--accent); }
  .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .arrow { display: flex; align-items: center; justify-content: center;
           color: var(--accent); font-size: 22px; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
  button {
    padding: 9px 16px; border-radius: 8px; border: 1px solid var(--border);
    background: #232c36; color: var(--text); cursor: pointer; font-size: 14px;
  }
  button:hover { border-color: var(--accent); }
  button.danger:hover { border-color: var(--err); color: var(--err); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #08111f;
                   font-weight: 600; padding: 11px 26px; }
  button.primary:disabled { opacity: .5; cursor: not-allowed; }
  .status { font-size: 13px; min-height: 18px; }
  .ok  { color: var(--ok); }
  .err { color: var(--err); white-space: pre-wrap; }
  .muted { color: var(--muted); }
  label.chk { display: flex; gap: 8px; align-items: center; font-size: 14px; color: var(--muted); }
  #jobs { margin-top: 26px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  td.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .badge { padding: 2px 10px; border-radius: 99px; font-size: 12px; }
  .badge.running, .badge.pending { background: #2b3a55; color: var(--accent); }
  .badge.done { background: #1d3a2c; color: var(--ok); }
  .badge.error { background: #44232a; color: var(--err); }
  .spinner { display: inline-block; width: 14px; height: 14px; vertical-align: -2px;
             border: 2px solid var(--accent); border-top-color: transparent;
             border-radius: 50%; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<main>
  <h1>db<span>copy</span></h1>
  <div class="sub">Full database copy — source &rarr; target. Client tools are managed
  automatically; nothing needs to be installed.</div>

  <div class="grid">
    <div class="card">
      <h2>Source database</h2>
      <input id="src" type="text" spellcheck="false"
             placeholder="postgresql://user:password@host:5432/sourcedb">
      <div class="row">
        <button onclick="test('src')">Test connection</button>
        <span id="src-status" class="status"></span>
      </div>
    </div>
    <div class="arrow">&rarr;</div>
    <div class="card">
      <h2>Target database</h2>
      <input id="dst" type="text" spellcheck="false"
             placeholder="postgresql://user:password@host:5434/targetdb">
      <div class="row">
        <button onclick="test('dst')">Test connection</button>
        <button class="danger" onclick="cleanTarget()">Clean database</button>
        <span id="dst-status" class="status"></span>
      </div>
      <div class="hint">The target database is created automatically if it does not exist.
      <b>Clean</b> removes every table and object from it.</div>
    </div>
  </div>

  <div class="row" style="margin-top:20px">
    <button id="go" class="primary" onclick="startCopy()">Start copy</button>
    <label class="chk"><input id="create" type="checkbox" checked>
      auto-create target database</label>
    <label class="chk"><input id="overwrite" type="checkbox">
      overwrite target (drop &amp; recreate first)</label>
    <span id="copy-status" class="status"></span>
  </div>

  <div id="jobs" class="card" style="display:none">
    <h2>Jobs</h2>
    <table>
      <thead><tr><th>Started</th><th>Source</th><th>Target</th><th>Status</th></tr></thead>
      <tbody id="jobs-body"></tbody>
    </table>
  </div>
</main>

<script>
async function post(url, body) {
  let r;
  try {
    r = await fetch(url, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch (e) {
    throw new Error('Could not reach the dbcopy server — is it still running? (python main.py)');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

async function test(which) {
  const el = document.getElementById(which + '-status');
  const url = document.getElementById(which).value.trim();
  if (!url) { el.className = 'status err'; el.textContent = 'Enter a connection string first'; return; }
  el.className = 'status muted'; el.innerHTML = '<span class="spinner"></span> testing… (first run may download tools)';
  try {
    const res = await post('/api/test', { url });
    if (res.ok) { el.className = 'status ok'; el.textContent = '✓ connected'; }
    else { el.className = 'status err'; el.textContent = '✗ ' + res.error; }
  } catch (e) { el.className = 'status err'; el.textContent = '✗ ' + e.message; }
}

async function cleanTarget() {
  const el = document.getElementById('dst-status');
  const url = document.getElementById('dst').value.trim();
  if (!url) { el.className = 'status err'; el.textContent = 'Enter a connection string first'; return; }
  if (!confirm('This will permanently DELETE ALL tables and objects in the target database.\\n\\nContinue?')) return;
  el.className = 'status muted'; el.innerHTML = '<span class="spinner"></span> cleaning…';
  try {
    const res = await post('/api/clean', { url });
    if (res.ok) { el.className = 'status ok'; el.textContent = '✓ database cleaned (all objects removed)'; }
    else { el.className = 'status err'; el.textContent = '✗ ' + res.error; }
  } catch (e) { el.className = 'status err'; el.textContent = '✗ ' + e.message; }
}

let polling = null;

async function startCopy() {
  const el = document.getElementById('copy-status');
  const src = document.getElementById('src').value.trim();
  const dst = document.getElementById('dst').value.trim();
  if (!src || !dst) { el.className = 'status err'; el.textContent = 'Enter both connection strings'; return; }
  const overwrite = document.getElementById('overwrite').checked;
  if (overwrite && !confirm('Overwrite is ON: the target database will be DROPPED and recreated before copying.\\n\\nContinue?')) return;
  document.getElementById('go').disabled = true;
  el.className = 'status muted'; el.innerHTML = '<span class="spinner"></span> starting…';
  try {
    await post('/api/copy', { source_url: src, target_url: dst,
                              create_target: document.getElementById('create').checked,
                              overwrite: overwrite });
    el.innerHTML = '<span class="spinner"></span> copying…';
    if (!polling) polling = setInterval(refreshJobs, 1000);
    refreshJobs();
  } catch (e) {
    el.className = 'status err'; el.textContent = e.message;
    document.getElementById('go').disabled = false;
  }
}

async function refreshJobs() {
  const jobs = await (await fetch('/api/jobs')).json();
  if (!jobs.length) return;
  document.getElementById('jobs').style.display = 'block';
  document.getElementById('jobs-body').innerHTML = jobs.map(j => `
    <tr>
      <td>${j.started_at}</td>
      <td class="mono">${j.source}</td>
      <td class="mono">${j.target}</td>
      <td><span class="badge ${j.status}">${j.status}</span>
          ${j.error ? `<div class="err" style="margin-top:4px">${j.error}</div>` : ''}</td>
    </tr>`).join('');
  const active = jobs.some(j => j.status === 'running' || j.status === 'pending');
  if (!active) {
    document.getElementById('go').disabled = false;
    const el = document.getElementById('copy-status');
    const last = jobs[0];
    if (last.status === 'done') { el.className = 'status ok'; el.textContent = '✓ copy complete'; }
    else if (last.status === 'error') { el.className = 'status err'; el.textContent = '✗ copy failed — see job log below'; }
    clearInterval(polling); polling = null;
  }
}

refreshJobs();
</script>
</body>
</html>"""


# @app.get("/", response_class=HTMLResponse)
# def dashboard() -> str:
#     return PAGE
app.mount("/", StaticFiles(directory="static", html=True), name="static")