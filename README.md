# dbcopy

Backup, restore, and full-copy databases — from the command line or a web
dashboard. PostgreSQL is supported today; the adapter architecture makes
adding MySQL, MongoDB, etc. straightforward.

**No local database tools required.** dbcopy wraps the native client tools
(`pg_dump`, `pg_restore`, `psql`) because they correctly handle schemas,
data, sequences, indexes, constraints, views, and functions — but it
provisions them itself: on first use it downloads portable, self-contained
binaries (~51 MB) and caches them in `~/.dbcopy/tools/`. If the tools are
already on your PATH, those are used instead and nothing is downloaded.

## Requirements

- Python 3.10+ (the core `dbcopy` package is stdlib-only)
- `fastapi[standard]` — only needed for the web dashboard
- Internet access on first run (one-time download of the client tools,
  skipped if PostgreSQL client tools are already installed)

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

# Backup to a file (compressed custom format)
python -m dbcopy backup postgresql://user:pass@host:5432/mydb -o mydb.dump

# Restore a backup (database is created if missing)
python -m dbcopy restore postgresql://user:pass@host:5432/newdb -i mydb.dump

# Restore over an existing database, dropping old objects first
python -m dbcopy restore postgresql://user:pass@host:5432/mydb -i mydb.dump --clean

# Copy into a target that already has data: drop + recreate it first
python -m dbcopy copy \
  postgresql://user:pass@source-host:5432/proddb \
  postgresql://user:pass@target-host:5432/staging --overwrite

# Remove ALL tables and objects from a database (asks for confirmation; -y skips)
python -m dbcopy clean postgresql://user:pass@host:5432/mydb
```

## Connection URLs

Format: `postgresql://user:password@host:port/dbname` (schemes
`postgresql` and `postgres` both work).

- Passwords with special characters (`@ : / # ?`) must be URL-encoded,
  e.g. `p%40ss` for `p@ss`.
- Query parameters are honored, e.g. `?sslmode=require` for servers that
  enforce SSL.
- Connection attempts time out after 10 seconds (override with the
  standard `PGCONNECT_TIMEOUT` env var) instead of hanging on
  unreachable hosts.
- Cloud databases (AWS RDS, etc.): the instance must be reachable from
  the machine running dbcopy — for RDS that means publicly accessible
  and a security group allowing your IP on the database port.

## How tools are resolved

`dbcopy/toolbox.py` looks for each client tool in this order:

1. `DBCOPY_PG_BIN` — environment variable pointing at a bin directory
2. The managed cache `~/.dbcopy/tools/postgresql-<version>/bin`
3. The system PATH
4. Auto-download from
   [theseus-rs/postgresql-binaries](https://github.com/theseus-rs/postgresql-binaries)
   (SHA-256 verified, cached for all future runs)

Environment variables:

- `DBCOPY_PG_BIN` — use your own client tools from this directory
- `DBCOPY_PG_VERSION` — pin the downloaded tools version (default 18.4.0;
  modern `pg_dump` can dump servers back to PostgreSQL 9.2)
- `DBCOPY_HOME` — move the cache somewhere other than `~/.dbcopy`

## Project layout

```
dbcopy/
├── adapters/
│   ├── base.py       # DatabaseAdapter abstract interface
│   ├── postgres.py   # PostgresAdapter (pg_dump / pg_restore / psql)
│   └── __init__.py   # registry: URL scheme -> adapter
├── core.py           # backup/restore/copy orchestration (no CLI code)
├── toolbox.py        # self-managed client tools (auto-download + cache)
├── cli.py            # argparse CLI
└── __main__.py       # enables `python -m dbcopy`
app.py                # FastAPI web dashboard + job API
main.py               # `python main.py` starts the dashboard
```

## Adding a new database (e.g. MySQL)

1. Create `dbcopy/adapters/mysql.py` with a `MySQLAdapter(DatabaseAdapter)`
   that sets `schemes = ("mysql",)` and implements `backup` (mysqldump),
   `restore` (mysql client), and `copy_to` (mysqldump piped into mysql).
2. Add it to the `ADAPTERS` list in `dbcopy/adapters/__init__.py`.

That's it — the CLI, core, and web API automatically support `mysql://...`
URLs. (Extend `toolbox.py` similarly if you want self-managed MySQL tools.)

## Notes & limitations

- Copy streams `pg_dump | psql` directly between servers — no disk space
  needed for an intermediate file, but both databases must be reachable
  from the machine running dbcopy.
- Dumps use `--no-owner --no-acl` so they restore cleanly even when the
  target uses a different role.
- Cross-engine copy (Postgres -> MySQL) is intentionally not supported;
  it requires schema translation, which is a much bigger problem.
- The web dashboard keeps job state in memory; restart clears history.
- For very large databases, consider adding `--jobs N` (parallel
  pg_dump/pg_restore with directory format) as a future enhancement.
