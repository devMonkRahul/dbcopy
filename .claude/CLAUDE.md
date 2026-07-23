# dbcopy — Project Memory

## What this project is

A Python tool to backup, restore, and make a full copy of a database from a
source to a target, with a CLI and a FastAPI web dashboard. PostgreSQL and
MongoDB are supported today; the architecture is designed so MySQL, etc. can
be added later without touching core code. **The tool is self-sufficient: it
does not require any database client tools to be installed locally** — it
downloads and caches portable binaries itself (see decision 10).

## Core design decisions (do not change without good reason)

1. **Wrap native tools, never reimplement dump logic.** Backup/restore/copy
   shell out to `pg_dump`, `pg_restore`, and `psql`. These correctly handle
   schemas, data, sequences, indexes, constraints, views, and functions —
   hand-rolled row copying breaks on sequences and FKs.
2. **Adapter pattern for multi-DB support.** `dbcopy/adapters/base.py`
   defines the abstract `DatabaseAdapter` interface (`backup`, `restore`,
   `copy_to`, `test_connection`, `check_tools`). `get_adapter(url)` in
   `dbcopy/adapters/__init__.py` routes by URL scheme via the `ADAPTERS`
   registry list.
3. **core.py stays UI-free.** No printing, no argparse — only raises
   exceptions. The CLI (`cli.py`) and the web app (`app.py`) both import it.
   (Exception: `toolbox.py` writes one-time download progress to stderr —
   infrastructure noise, never stdout.)
4. **The `dbcopy` package stays stdlib-only.** No third-party imports inside
   `dbcopy/` (argparse, subprocess, urllib, tarfile, hashlib). FastAPI is a
   project dependency but is imported only by the web layer (`app.py`,
   `main.py`), never by the package.
5. **Copy streams with no temp file:** `pg_dump --format=plain` piped into
   `psql --set ON_ERROR_STOP=on` on the target. Target DB is auto-created
   unless `--no-create` is passed. In `copy_to`, `dump.stdout` is closed by
   hand for SIGPIPE; therefore NEVER call `dump.communicate()` — on Windows
   it spawns a reader thread on the closed pipe and crashes. Read
   `dump.stderr` directly and `wait()` instead.
6. **Backups use custom format** (`pg_dump --format=custom`) so they are
   compressed and restorable with `pg_restore` (selective restore possible).
7. **Dumps use `--no-owner --no-acl`** so they restore cleanly under a
   different role on the target.
8. **Passwords never go on the command line** (would leak in `ps` / shell
   history). Postgres uses the `PGPASSWORD` env var; MongoDB has no env-var
   equivalent, so the Mongo adapter writes the password to a temporary
   `--config` YAML file (mode 0600, deleted after) — see decision 13.
9. **Cross-engine copy (Postgres -> MySQL) is intentionally unsupported**;
   `core.copy_database` raises ValueError if adapter types differ.
10. **Self-managed client tools** (`dbcopy/toolbox.py`). Organized as a
    `_ToolFamily` registry (`_PG`, `_MONGO_TOOLS`) so each engine's
    version/platform/download differences live in one descriptor; the public
    API `find_tool(name)` / `ensure_tools(names)` is unchanged and routes by
    tool name via `_family_for_tool`. `find_tool(name)` resolves in this
    order: family override env dir (`DBCOPY_PG_BIN` / `DBCOPY_MONGO_BIN`) →
    managed cache `~/.dbcopy/tools/<dirname>-<ver>/bin` → system PATH →
    auto-download (SHA-256 verified best-effort, extracted atomically via
    temp dir + move). Cache root overridable with `DBCOPY_HOME`.
    - **Postgres**: GitHub `theseus-rs/postgresql-binaries`, asset
      `postgresql-{ver}-{rust-target-triple}.tar.gz`, pinned
      `DEFAULT_PG_VERSION` (18.4.0), override `DBCOPY_PG_VERSION`. Newer
      pg_dump dumps servers back to PG 9.2, so one client version covers all.
    - **MongoDB**: `fastdl.mongodb.org/tools/db`, asset
      `mongodb-database-tools-{token}-{ver}.{zip|tgz}` (`.zip` on
      Windows/macOS, `.tgz` on Linux — extraction branches on this; zip
      restores the exec bit on POSIX). Token is OS/distro-based
      (`windows-x86_64`, `macos-arm64`, `ubuntu2204-x86_64`, ...), NOT a rust
      triple; no universal Linux build, so the distro defaults to
      `ubuntu2204` and is overridable with `DBCOPY_MONGO_PLATFORM`. Pinned
      `DEFAULT_MONGO_TOOLS_VERSION`, override `DBCOPY_MONGO_VERSION`.
    Gotcha: the published `.sha256` files can be Windows CertUtil multi-line
    format, not `hash  filename` — parse by regexing the first 64-hex token
    (and verification silently skips if no sidecar is served).
11. **Web jobs never block the request.** `app.py` starts copies in a
    daemon `threading.Thread`, stores state in an in-memory dict guarded by
    a lock, and exposes `GET /api/jobs/{id}` for polling. Blocking endpoints
    are plain `def` (FastAPI runs them in its threadpool). Connection-string
    passwords are redacted (`_redact`) before being stored/returned.
12. **Destructive operations confirm at the edge, not in core.**
    `clean_database` (drop every user schema CASCADE + recreate `public`)
    and copy's `overwrite` flag (`DROP DATABASE IF EXISTS ... WITH (FORCE)`
    — needs PG 13+ — then recreate) never prompt in `core.py`/adapters.
    The CLI prompts (`clean` asks y/N unless `-y`); the UI uses JS
    `confirm()` for both the Clean button and the overwrite checkbox.
    `app.py`'s `/api/clean` and `overwrite` field trust the caller.
13. **MongoDB adapter** (`dbcopy/adapters/mongo.py`, schemes `mongodb` /
    `mongodb+srv`, default port 27017) wraps the MongoDB Database Tools:
    `backup` → `mongodump --archive=<file> --gzip`; `restore` →
    `mongorestore --archive=<file> --gzip [--drop if clean]`; `copy` →
    `mongodump --archive | mongorestore --archive` streamed with the exact
    SIGPIPE / no-`communicate()` pattern as Postgres (decision 5).
    - **Connection** is passed as `--uri` with the password *stripped from the
      URL string* (via `urlsplit`/`urlunsplit`, so `mongodb+srv`, comma seed
      lists and query options survive). The password is supplied separately
      through a temp `--config` file (decision 8). `serverSelectionTimeoutMS`
      is injected (setdefault), BUT — GOTCHA — mongodump/mongorestore do NOT
      honor it for an unreachable (firewalled / IP-not-allowlisted) host: they
      hang indefinitely (verified). So the adapter enforces its OWN hard
      `subprocess` timeout (`CONNECT_TIMEOUT`, 20s) on every connection-
      establishing command via `_run(..., timeout=...)`; without it the web
      request never returns. `test_connection` is bounded, and `copy_to`
      pre-flights `test_connection()` on BOTH endpoints before the (unbounded,
      possibly long) data pipe so an unreachable host fails fast instead of
      hanging. The timeout error text triggers the existing `/api/test` hint.
    - **GOTCHA — `_uri()` drops the database from the path** and the code always
      passes the db explicitly (`--db` for dump, `--nsFrom/--nsTo` for copy).
      Reason: `mongorestore` treats a database in the URI path as an implicit
      `--db`, which silently conflicts with `--nsFrom/--nsTo` and restores **0
      documents while still exiting 0** (looks like "Copy complete" but copies
      nothing). Do NOT put the database back in the `--uri`. Because an
      unspecified `authSource` defaults to that path db, `_uri()` pins
      `authSource=<db>` before dropping the path so auth keeps working.
    - **`overwrite`** uses `mongorestore --drop` — collection-level (drops
      each collection as it is restored), NOT a whole-database drop.
      `create_target` is effectively a no-op (Mongo creates DBs/collections
      implicitly on first write).
    - **`copy` remaps** the dumped db into the target db name with
      `--nsFrom <src>.* --nsTo <tgt>.*` (equal single wildcards — a `*.*`→`X.*`
      remap is illegal, the wildcard counts must match). `restore` does NOT
      remap (it doesn't know the archive's source db), so it restores the
      namespaces the archive carries.
    - **`clean` is intentionally unsupported** for MongoDB: wiping a database
      needs `mongosh`, which is deliberately not bundled. It raises a clear
      RuntimeError pointing at `copy --overwrite`.

## Project layout

```
dbcopy/
├── adapters/
│   ├── base.py       # DatabaseAdapter ABC + ConnectionInfo dataclass
│   ├── postgres.py   # PostgresAdapter
│   ├── mongo.py      # MongoAdapter (mongodump / mongorestore)
│   └── __init__.py   # ADAPTERS registry + get_adapter(url)
├── core.py           # backup_database / restore_database / copy_database
├── toolbox.py        # self-managed client tools (_ToolFamily registry)
├── cli.py            # argparse CLI: backup | restore | copy
└── __main__.py       # enables `python -m dbcopy`
app.py                # FastAPI dashboard (HTML inline) + job API
main.py               # `python main.py` -> uvicorn on 127.0.0.1:8000
```

## CLI / dashboard usage

```bash
python -m dbcopy copy    postgresql://u:p@src:5432/proddb postgresql://u:p@dst:5432/staging [--overwrite]
python -m dbcopy copy    mongodb://u:p@src:27017/proddb   mongodb://u:p@dst:27017/staging [--overwrite]
python -m dbcopy backup  postgresql://u:p@host:5432/mydb -o mydb.dump
python -m dbcopy restore postgresql://u:p@host:5432/newdb -i mydb.dump [--clean]
python -m dbcopy clean   postgresql://u:p@host:5432/mydb [-y]   # removes ALL objects (Postgres only)
uv run python main.py    # dashboard at http://127.0.0.1:8000
```

`copy --overwrite` drops + recreates the target DB first for Postgres (for
non-empty targets, which otherwise fail fast with a clear "already exists"
hint); for MongoDB it means `mongorestore --drop` (collection-level).
Cross-engine copy (e.g. Postgres ↔ MongoDB) is rejected (decision 9).

URL format: `postgresql://user:password@host:port/dbname` (schemes
`postgresql`/`postgres`) or `mongodb://...` / `mongodb+srv://...` (port
defaults to 27017). Credentials are percent-decoded (`p%40ss` -> `p@ss`);
query params land in `ConnectionInfo.options` (Postgres `?sslmode=require`
-> PGSSLMODE; Mongo query options are carried through in the `--uri`). The
Postgres adapter sets `PGCONNECT_TIMEOUT=10` (setdefault, so a user-set env
var wins) and the Mongo adapter injects `serverSelectionTimeoutMS=10000` —
without these, connecting to a firewalled host (typical RDS misconfig) hangs
for minutes and the dashboard fetch dies with browser "Failed to fetch".
`/api/test` appends an RDS hint (public accessibility + security group)
when the error is a timeout.

Dashboard API: `POST /api/test` {url}, `POST /api/copy` {source_url,
target_url, create_target, overwrite}, `POST /api/clean` {url},
`GET /api/jobs/{id}`, `GET /api/jobs`.

## Verified working (tested 2026-06-10, PostgreSQL servers on :5432/:5434, Python 3.14)

- First run with no client tools installed: auto-downloaded 18.4.0 binaries
  (~51 MB) to `~/.dbcopy/tools/`, checksum verified, copy succeeded.
- Second run: used the cache, no download, clean output.
- `copy` to a non-existent target DB: auto-created, all 116 tables present
  (matched source count).
- Dashboard end-to-end: `GET /` 200, `/api/test` ok + clean error for
  `mysql://`, `/api/copy` job went pending→running→done in ~2 s, passwords
  redacted in job listing.
- Sequence state survives backup/restore (verified earlier on PG 16): after
  restore, next INSERT got the correct next SERIAL id — regression-test
  this if dump flags ever change.
- Copy into a non-empty target fails fast with the psql "already exists"
  error + hint (restore error is checked before pg_dump's broken-pipe).
- `copy --overwrite` into a non-empty target: dropped, recreated, 116
  tables copied. CLI `clean`: y/N prompt aborts with exit 1; `-y` wiped
  116 -> 0 tables. Same verified through `/api/copy` {overwrite: true}
  and `/api/clean`.

## Roadmap / next steps (owner's stated intent)

1. **MySQL adapter**: `dbcopy/adapters/mysql.py`, `schemes = ("mysql",)`,
   wrap `mysqldump` / `mysql`; append to `ADAPTERS` list. For self-managed
   binaries add a third `_ToolFamily` in `toolbox.py` (the Mongo entry is the
   template for a non-theseus download source / zip archive).
2. **Dashboard enhancements**: backup/restore operations in the UI,
   persistent job history, progress percentage (needs `pg_dump --verbose`
   parsing or table counts).
3. Possible enhancement: parallel dump/restore for big DBs using
   `pg_dump --format=directory --jobs N` + `pg_restore --jobs N`.

## Conventions

- Python 3.10+ syntax (uses `X | None` unions, dataclasses); pyproject pins
  `requires-python >= 3.14` (tarfile `filter="data"` needs 3.12+).
- New adapters must implement every abstract method in `base.py` and set
  the `schemes` tuple.
- Raise `RuntimeError` for tool/connection failures, `ValueError` for bad
  input; `cli.py` catches these and exits 1 with a clean message. `app.py`
  maps ValueError -> HTTP 400, job failures -> job `status: "error"`.
- Subprocess failures must surface stderr in the raised exception.
