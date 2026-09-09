# dbcopy

Backup, restore, and full-copy databases — from the command line or a web
dashboard. PostgreSQL, MySQL, and MongoDB are supported; the adapter
architecture makes adding another engine straightforward.

**No local database tools required.** dbcopy wraps the native client tools
because they correctly handle schemas, data, sequences, indexes,
constraints, views, and functions — but it provisions them itself: on first
use it downloads portable, self-contained binaries and caches them in
`~/.dbcopy/tools/`. If the tools are already on your PATH, those are used
instead and nothing is downloaded.

| Engine     | URL schemes              | Tools wrapped                     |
|------------|--------------------------|-----------------------------------|
| PostgreSQL | `postgresql`, `postgres` | `pg_dump`, `pg_restore`, `psql`   |
| MySQL      | `mysql`                  | `mysqldump`, `mysql`              |
| MongoDB    | `mongodb`, `mongodb+srv` | `mongodump`, `mongorestore`       |

## Requirements

- Python 3.10+ (the core `dbcopy` package is stdlib-only)
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
| `/api/copy`           | POST   | Start a copy job (`overwrite` drops + recreates the target first), returns `job_id` |
| `/api/clean`          | POST   | Remove ALL tables/objects from a database     |
| `/api/jobs/{job_id}`  | GET    | Poll job status                               |
| `/api/jobs`           | GET    | List all jobs (passwords redacted)            |

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

# Backup to a file
python -m dbcopy backup postgresql://user:pass@host:5432/mydb -o mydb.dump
python -m dbcopy backup mysql://user:pass@host:3306/mydb      -o mydb.sql

# Restore a backup (database is created if missing)
python -m dbcopy restore postgresql://user:pass@host:5432/newdb -i mydb.dump

# Restore over an existing database, dropping old objects first
python -m dbcopy restore mysql://user:pass@host:3306/mydb -i mydb.sql --clean

# Copy into a target that already has data: drop + recreate it first
python -m dbcopy copy \
  mysql://user:pass@source-host:3306/proddb \
  mysql://user:pass@target-host:3306/staging --overwrite

# Remove ALL tables and objects from a database (asks for confirmation; -y skips)
python -m dbcopy clean postgresql://user:pass@host:5432/mydb
```

Cross-engine copy (e.g. Postgres -> MySQL) is rejected: source and target
must be the same engine.

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
  are carried through to the connection URI.
- Connection attempts time out instead of hanging on unreachable hosts:
  10s for Postgres (`PGCONNECT_TIMEOUT`) and MySQL, 20s for MongoDB.
- Passwords never appear on a command line. Postgres uses `PGPASSWORD`,
  MySQL uses `MYSQL_PWD`, and MongoDB (which has no such variable) gets a
  temporary `--config` file that is deleted afterwards.
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
| MongoDB    | `DBCOPY_MONGO_BIN` | `DBCOPY_MONGO_VERSION` | [MongoDB Database Tools](https://fastdl.mongodb.org/tools/db) (~60 MB) |

`DBCOPY_HOME` moves the cache somewhere other than `~/.dbcopy`.

Two platform escape hatches exist because those projects name their release
assets by OS rather than by architecture alone: `DBCOPY_MONGO_PLATFORM`
(e.g. `rhel80`, `amazon2023` — Linux defaults to `ubuntu2204`) and
`DBCOPY_MYSQL_PLATFORM`, which replaces the whole platform part of the
archive name (e.g. `macos26-arm64` once MySQL builds against a newer macOS,
or `linux-glibc2.28-x86_64` for the full rather than the minimal build).

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

MySQL publishes no client-only bundle, so dbcopy downloads the Community
Server archive and installs only the parts it runs — the two client
programs and the libraries they link against. That trims a 1.2 GB
distribution to about 73 MB on disk; the download itself is unavoidably
large, so if you already have `mysqldump` and `mysql` installed, keeping
them on your PATH skips it entirely.

## Project layout

```
dbcopy/
├── adapters/
│   ├── base.py       # DatabaseAdapter abstract interface
│   ├── postgres.py   # PostgresAdapter (pg_dump / pg_restore / psql)
│   ├── mysql.py      # MySQLAdapter   (mysqldump / mysql)
│   ├── mongo.py      # MongoAdapter   (mongodump / mongorestore)
│   └── __init__.py   # registry: URL scheme -> adapter
├── core.py           # backup/restore/copy orchestration (no CLI code)
├── toolbox.py        # self-managed client tools (auto-download + cache)
├── cli.py            # argparse CLI
└── __main__.py       # enables `python -m dbcopy`
app.py                # FastAPI web dashboard + job API
main.py               # `python main.py` starts the dashboard
```

## Adding a new database

1. Create `dbcopy/adapters/<engine>.py` with an adapter subclassing
   `DatabaseAdapter`, setting `schemes = (...)` and implementing every
   abstract method in `base.py`.
2. Add it to the `ADAPTERS` list in `dbcopy/adapters/__init__.py`.

That's it — the CLI, core, and web API pick up the new URL scheme
automatically. To have dbcopy provision that engine's client tools too, add
a `_ToolFamily` entry in `toolbox.py`; the MySQL entry is the template for a
download that needs pruning, the MongoDB one for a non-GitHub source.

## Notes & limitations

- Copy streams the dump straight from source to target (`pg_dump | psql`,
  `mysqldump | mysql`, `mongodump | mongorestore`) — no disk space needed
  for an intermediate file, but both databases must be reachable from the
  machine running dbcopy.
- Postgres dumps use `--no-owner --no-acl`, and MySQL dumps omit
  `CREATE DATABASE`/`USE` and `GTID_PURGED`, so both restore cleanly into a
  differently-named database under a different role.
- MySQL backups are plain `.sql` scripts (mysqldump has no compressed
  custom format); Postgres backups use the compressed custom format and
  MongoDB backups are gzipped archives.
- `clean` is not supported for MongoDB: wiping a database needs `mongosh`,
  which dbcopy does not bundle. Use `copy --overwrite` instead.
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
