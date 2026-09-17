# dbcopy

Backup, restore, and full-copy databases — from the command line or a web
dashboard. PostgreSQL and MySQL support all three operations; MongoDB
supports copy. The engine architecture makes adding another database
straightforward.

**No local database tools required.** dbcopy wraps the native client tools
because they correctly handle schemas, data, sequences, indexes,
constraints, views, and functions — but it provisions them itself: on first
use it downloads portable, self-contained binaries and caches them in
`~/.dbcopy/tools/`. If the tools are already on your PATH, those are used
instead and nothing is downloaded.

| Engine     | URL schemes              | Driven by                       | Operations                   |
|------------|--------------------------|---------------------------------|------------------------------|
| PostgreSQL | `postgresql`, `postgres` | `pg_dump`, `pg_restore`, `psql` | copy, backup, restore, clean |
| MySQL      | `mysql`                  | `mysqldump`, `mysql`            | copy, backup, restore, clean |
| MongoDB    | `mongodb`, `mongodb+srv` | `pymongo` (no external tools)   | copy                         |

MongoDB is the exception to the wrap-the-native-tools rule: it is driven
through the `pymongo` driver, which streams collections directly and reports
live per-collection progress. Nothing is downloaded for it, and it has no
dump-file format — so `backup`, `restore` and `clean` are PostgreSQL/MySQL
only. See [MongoDB](#mongodb) below.

## Requirements

- Python 3.10+ (`dbcopy.adapters`, `core` and `toolbox` are stdlib-only)
- `pymongo` — only needed for MongoDB copies
- `fastapi[standard]` — only needed for the web dashboard
- Internet access on first run (one-time download of the client tools for
  whichever engine you use, skipped if those tools are already installed)

## Web dashboard

```bash
uv run python main.py        # then open http://127.0.0.1:8000
```

Enter the source and target connection strings, test both connections, and
start the copy. Copies run as background jobs — the dashboard polls a job
status endpoint, so large databases never block the UI.

API endpoints (also usable directly):

| Endpoint              | Method | Purpose                                       |
|-----------------------|--------|-----------------------------------------------|
| `/api/test`           | POST   | Test a connection string                      |
| `/api/copy`           | POST   | Start a copy job (`overwrite` drops + recreates the target first; `skip_missing_extensions` omits extensions the target lacks), returns `job_id` |
| `/api/clean`          | POST   | Remove ALL tables/objects from a database     |
| `/api/jobs/{job_id}`  | GET    | Poll job status                               |
| `/api/jobs`           | GET    | List all jobs (passwords redacted)            |

MongoDB has its own screen at `/mongodb`, linked from the top of the
dashboard, with a database picker and live progress:

| Endpoint                              | Method | Purpose                                     |
|---------------------------------------|--------|---------------------------------------------|
| `/api/engines/mongo/connect`          | POST   | Verify a URI and list its databases         |
| `/api/engines/mongo/copy`             | POST   | Start a copy, returns `job_id`              |
| `/api/engines/mongo/jobs/{id}`        | GET    | Poll a job snapshot                         |
| `/api/engines/mongo/jobs/{id}/stream` | GET    | Server-sent events, one snapshot per 500 ms |
| `/api/engines/mongo/jobs/{id}/cancel` | POST   | Ask a running copy to stop                  |

The dashboard's **Clean database** button and the **overwrite target**
checkbox both ask for confirmation before doing anything destructive.

## CLI usage

```bash
# Full copy of one database into another (target auto-created, no temp file)
python -m dbcopy copy \
  postgresql://user:pass@source-host:5432/proddb \
  postgresql://user:pass@target-host:5432/staging

python -m dbcopy copy \
  mysql://user:pass@source-host:3306/proddb \
  mysql://user:pass@target-host:3306/staging

python -m dbcopy copy \
  mongodb://user:pass@source-host:27017/proddb \
  mongodb+srv://user:pass@cluster0.abcde.mongodb.net/staging

# Backup to a file
python -m dbcopy backup postgresql://user:pass@host:5432/mydb -o mydb.dump
python -m dbcopy backup mysql://user:pass@host:3306/mydb      -o mydb.sql

# Restore a backup (database is created if missing)
python -m dbcopy restore postgresql://user:pass@host:5432/newdb -i mydb.dump

# Restore over an existing database, dropping old objects first
python -m dbcopy restore mysql://user:pass@host:3306/mydb -i mydb.sql --clean

# Copy out of Supabase/RDS into a plain server that lacks its extensions
python -m dbcopy copy \
  postgresql://user:pass@db.abcdefg.supabase.co:5432/postgres \
  postgresql://user:pass@target-host:5432/mycopy --skip-missing-extensions

# Copy into a target that already has data: drop + recreate it first
python -m dbcopy copy \
  mysql://user:pass@source-host:3306/proddb \
  mysql://user:pass@target-host:3306/staging --overwrite

# Remove ALL tables and objects from a database (asks for confirmation; -y skips)
python -m dbcopy clean postgresql://user:pass@host:5432/mydb
```

Cross-engine copy (e.g. Postgres -> MySQL, or Postgres -> MongoDB) is
rejected: source and target must be the same engine. `backup`, `restore` and
`clean` refuse a `mongodb://` URL with a message pointing at `copy`.

`copy` reports where the data landed and how much of it arrived, so a copy
that moved nothing is never mistaken for a successful one:

```
Copy complete: "staging" at db.example.com:5432 now holds 116 tables/views
Copy complete, but nothing was copied: the source database contains no
tables. Check the database name in the source URL.
```

## Connection URLs

Format: `scheme://user:password@host:port/dbname`. The port may be omitted
and defaults per engine (5432 / 3306 / 27017).

- Passwords with special characters (`@ : / # ?`) must be URL-encoded,
  e.g. `p%40ss` for `p@ss`. (The dashboard percent-encodes them for you.)
- Query parameters are honored: `?sslmode=require` (Postgres),
  `?ssl-mode=REQUIRED` (MySQL), and Mongo options such as `?replicaSet=rs0`
  or `?authSource=admin` are passed to the driver untouched.
- Connection attempts time out instead of hanging on unreachable hosts:
  10s for Postgres (`PGCONNECT_TIMEOUT`) and MySQL, 8s for MongoDB
  (pymongo's `serverSelectionTimeoutMS`).
- Passwords never appear on a command line. Postgres uses `PGPASSWORD` and
  MySQL uses `MYSQL_PWD`; MongoDB runs in-process, so no command line that
  could leak one is ever built.
- Cloud databases (AWS RDS, Atlas, etc.): the instance must be reachable
  from the machine running dbcopy — that usually means publicly accessible,
  with a security group / access list allowing your IP on the database port.

## How tools are resolved

`dbcopy/toolbox.py` looks for each client tool in this order:

1. The family's override environment variable, pointing at a bin directory
2. The managed cache `~/.dbcopy/tools/<name>-<version>/bin`
3. The system PATH
4. Auto-download from the upstream project, cached for all future runs

| Engine     | Override dir       | Pin version            | Downloaded from |
|------------|--------------------|------------------------|-----------------|
| PostgreSQL | `DBCOPY_PG_BIN`    | `DBCOPY_PG_VERSION`    | [theseus-rs/postgresql-binaries](https://github.com/theseus-rs/postgresql-binaries) (~44 MB per major, SHA-256 verified) |
| MySQL      | `DBCOPY_MYSQL_BIN` | `DBCOPY_MYSQL_VERSION` | [cdn.mysql.com](https://cdn.mysql.com) Community archives (~270 MB download) |

MongoDB is absent from this table on purpose: it needs no external tools.

`DBCOPY_HOME` moves the cache somewhere other than `~/.dbcopy`.

One platform escape hatch exists because MySQL names its release assets by
OS rather than by architecture alone: `DBCOPY_MYSQL_PLATFORM` replaces the
whole platform part of the archive name (e.g. `macos26-arm64` once MySQL
builds against a newer macOS, or `linux-glibc2.28-x86_64` for the full
rather than the minimal build).

### PostgreSQL client versions

`pg_dump` writes SQL for its *own* version, so dumping with a client newer
than the destination server produces a dump the server rejects — PG 17 added
`transaction_timeout`, which PG 16 and older refuse outright. dbcopy
therefore detects each server's major version and fetches a matching
`pg_dump`/`pg_restore` (`psql` is version-agnostic and stays on the default
build). The first copy against a new major downloads that client once.

A consequence: copying from a **newer** server to an **older** one is
rejected up front with a clear message. pg_dump cannot read a server newer
than itself, and cannot emit SQL an older server accepts, so no client
version satisfies both ends — upgrade the target, or migrate by hand.

### PostgreSQL extensions

A dump recreates the source's extensions with `CREATE EXTENSION`, which
fails when the extension is not installed on the target machine. This is the
usual wall when copying out of a managed Postgres such as Supabase or RDS,
whose databases carry extensions (`supabase_vault`, `pg_graphql`, `vector`,
…) a stock server does not have.

dbcopy checks this before starting and names **every** missing extension at
once, rather than failing on whichever one comes first:

```
Error: The source database uses PostgreSQL extensions that the target server
does not have available:
  pg_graphql, supabase_vault, vector
```

Either install them on the target (the extension files must exist on the
server itself), or copy without them:

```bash
python -m dbcopy copy SOURCE_URL TARGET_URL --skip-missing-extensions
```

Skipping is lossy: extensions the target *does* have are still created, but
anything that depends on a skipped one — a `vector` column, a function
calling into it — will not copy, and the error says so. It works well when
the extension only backs a managed provider's own internal schemas.

MySQL publishes no client-only bundle, so dbcopy downloads the Community
Server archive and installs only the parts it runs — the two client
programs and the libraries they link against. That trims a 1.2 GB
distribution to about 73 MB on disk; the download itself is unavoidably
large, so if you already have `mysqldump` and `mysql` installed, keeping
them on your PATH skips it entirely.

## MongoDB

MongoDB is copy-only and needs no downloaded tools — it runs on the
`pymongo` driver, in process. Use the dedicated screen at `/mongodb`
(linked from the dashboard) or the CLI:

```bash
python -m dbcopy copy \
  mongodb://user:pass@source-host:27017/proddb \
  mongodb+srv://user:pass@cluster0.abcde.mongodb.net/staging --overwrite
```

**What comes across:** every non-system collection, its documents, its
collection options (capped, time-series, validators, collation), its
secondary indexes, and views. GridFS follows for free, since `.files` and
`.chunks` are ordinary collections. Users, roles and server settings are
*not* copied — they live in `admin` and belong to the deployment.

**A copy without `--overwrite` is additive, and that is what makes it
resumable.** Documents whose `_id` is already on the target come back as
duplicate-key errors, are counted as `skipped`, and the copy carries on — so
a run cut short by a dropped connection can simply be started again. With
`--overwrite` the target database is dropped first, so it ends up an exact
match rather than a merge.

Two consequences worth knowing:

- A document that collides on a *unique secondary index* rather than on
  `_id` is also counted as `skipped`, not as an error — the two are
  indistinguishable by error code, and treating them differently would break
  resumability. The per-collection `skipped` count is where you see it, so
  check it if a target ends up short.
- Totals come from `estimated_document_count()`, which reads collection
  metadata instead of scanning. It is instant on a large database, but the
  percentage can drift slightly past or short of 100 on a live source.
  Completion is signalled by the job state, never by the percentage.

**Cancelling stops the copy; it does not roll it back.** The worker checks
between batches, so documents already written stay written.

Memory stays flat regardless of database size: documents move through a
cursor in batches (`batch_size`, default 1000). Raise it for many small
documents; lower it if large documents push a batch near MongoDB's 16 MB
write limit.

## Project layout

```
dbcopy/
├── adapters/
│   ├── base.py       # DatabaseAdapter abstract interface
│   ├── postgres.py   # PostgresAdapter (pg_dump / pg_restore / psql)
│   ├── mysql.py      # MySQLAdapter   (mysqldump / mysql)
│   └── __init__.py   # registry: URL scheme -> adapter
├── engines/          # driver-backed engines (may use third-party libs)
│   └── mongo/
│       ├── __init__.py  # URL helpers, stdlib only
│       ├── copier.py    # the pymongo copy engine (no web imports)
│       └── routes.py    # FastAPI router mounted by app.py
├── core.py           # backup/restore/copy orchestration (no CLI code)
├── toolbox.py        # self-managed client tools (auto-download + cache)
├── cli.py            # argparse CLI
└── __main__.py       # enables `python -m dbcopy`
app.py                # FastAPI web dashboard + job API
main.py               # `python main.py` starts the dashboard
static/
├── index.html        # PostgreSQL / MySQL dashboard
└── mongo.html        # MongoDB copy screen
```

## Adding a new database

1. Create `dbcopy/adapters/<engine>.py` with an adapter subclassing
   `DatabaseAdapter`, setting `schemes = (...)` and implementing every
   abstract method in `base.py`.
2. Add it to the `ADAPTERS` list in `dbcopy/adapters/__init__.py`.

That's it — the CLI, core, and web API pick up the new URL scheme
automatically. To have dbcopy provision that engine's client tools too, add
a `_ToolFamily` entry in `toolbox.py`; the MySQL entry is the template for a
download that needs pruning.

If the engine has no usable command-line tools and needs a Python driver
instead, put it under `dbcopy/engines/` like MongoDB, and route to it from
`core._resolve_copy` rather than the adapter registry.

## Notes & limitations

- Copy streams the dump straight from source to target (`pg_dump | psql`,
  `mysqldump | mysql`) — no disk space needed for an intermediate file, but
  both databases must be reachable from the machine running dbcopy. MongoDB
  streams documents through a cursor, which is likewise flat in memory.
- Postgres dumps use `--no-owner --no-acl`, and MySQL dumps omit
  `CREATE DATABASE`/`USE` and `GTID_PURGED`, so both restore cleanly into a
  differently-named database under a different role.
- MySQL backups are plain `.sql` scripts (mysqldump has no compressed
  custom format); Postgres backups use the compressed custom format.
- `backup`, `restore` and `clean` are not available for MongoDB — the
  driver-based engine has no dump format. Use `copy`, with `--overwrite`
  when you need the target replaced.
- Cross-engine copy (Postgres -> MySQL) is intentionally not supported;
  it requires schema translation, which is a much bigger problem.
- `restore --clean` means "drop what the dump contains" for PostgreSQL
  (pg_restore's native semantic — a table absent from the dump survives) and
  "drop and recreate the whole database" for MySQL, where every mysqldump
  script already drops the tables it carries.
- The web dashboard keeps job state in memory; restart clears history. Copy
  jobs carry `source_objects` / `target_objects` so a no-op copy is visible.
- For very large databases, consider adding `--jobs N` (parallel
  pg_dump/pg_restore with directory format) as a future enhancement.
