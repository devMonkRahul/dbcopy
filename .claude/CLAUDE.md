# dbcopy — Project Memory

## What this project is

A Python tool to backup, restore, and make a full copy of a database from a
source to a target, with a CLI and a FastAPI web dashboard. PostgreSQL,
MySQL, and MongoDB are supported today; the architecture is designed so
other engines can be added later without touching core code. **The tool is self-sufficient: it
does not require any database client tools to be installed locally** — it
downloads and caches portable binaries itself (see decision 10).

## Core design decisions (do not change without good reason)

1. **Wrap native tools, never reimplement dump logic.** Backup/restore/copy
   shell out to `pg_dump`/`pg_restore`/`psql`, `mysqldump`/`mysql`, and
   `mongodump`/`mongorestore`. These correctly handle
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
   history). Postgres uses the `PGPASSWORD` env var and MySQL the exactly
   analogous `MYSQL_PWD`; MongoDB has no env-var equivalent, so the Mongo
   adapter writes the password to a temporary `--config` YAML file
   (mode 0600, deleted after) — see decision 13.
9. **Cross-engine copy (Postgres -> MySQL) is intentionally unsupported**;
   `core.copy_database` raises ValueError if adapter types differ (and
   `app.py` rejects it with HTTP 400 before spawning a job).
10. **Self-managed client tools** (`dbcopy/toolbox.py`). Organized as a
    `_ToolFamily` registry (`_PG`, `_MONGO_TOOLS`, `_MYSQL`) so each engine's
    version/platform/download differences live in one descriptor; the public
    API `find_tool(name)` / `ensure_tools(names)` is unchanged and routes by
    tool name via `_family_for_tool`. `find_tool(name)` resolves in this
    order: family override env dir (`DBCOPY_PG_BIN` / `DBCOPY_MONGO_BIN` /
    `DBCOPY_MYSQL_BIN`) →
    managed cache `~/.dbcopy/tools/<dirname>-<ver>/bin` → system PATH →
    auto-download (SHA-256 verified best-effort, extracted atomically via
    temp dir + move). Cache root overridable with `DBCOPY_HOME`.
    `find_tool(name, version=...)` / `ensure_tools(..., version=...)` pin a
    specific release of a family (PostgreSQL needs this — decision 15); the
    memo dict is keyed by `(tool, version)`, and when an explicit version is
    asked for, a tool found on PATH is only accepted if `_tool_major()`
    confirms its major matches. `asset_url` may return one URL **or a tuple
    of candidates tried in order** (MySQL needs two — see below). `_archive_kind` returns
    `zip`/`tar`; everything non-zip goes to tarfile, which sniffs `.gz` vs
    `.xz` itself. A family may also carry a `_Prune` policy (`keep_dirs`,
    `drop_globs`, `bin_tools_only`) applied to the extracted tree before it
    is installed — only MySQL uses it.
    - **Postgres**: GitHub `theseus-rs/postgresql-binaries`, asset
      `postgresql-{ver}-{rust-target-triple}.tar.gz`, pinned
      `DEFAULT_PG_VERSION` (18.4.0), override `DBCOPY_PG_VERSION`. One client
      version does NOT cover all servers — see decision 15: the major is
      chosen per server from `PG_VERSIONS`, so several may be cached.
    - **MongoDB**: `fastdl.mongodb.org/tools/db`, asset
      `mongodb-database-tools-{token}-{ver}.{zip|tgz}` (`.zip` on
      Windows/macOS, `.tgz` on Linux — extraction branches on this; zip
      restores the exec bit on POSIX). Token is OS/distro-based
      (`windows-x86_64`, `macos-arm64`, `ubuntu2204-x86_64`, ...), NOT a rust
      triple; no universal Linux build, so the distro defaults to
      `ubuntu2204` and is overridable with `DBCOPY_MONGO_PLATFORM`. Pinned
      `DEFAULT_MONGO_TOOLS_VERSION`, override `DBCOPY_MONGO_VERSION`.
    - **MySQL**: no client-only bundle is published, so the full Community
      Server archive is downloaded and pruned. Asset
      `mysql-{ver}-{token}.{zip|tar.gz|tar.xz}` where the token is a third
      naming scheme (`winx64`, `linux-glibc2.28-x86_64-minimal`,
      `macos15-arm64`); override with `DBCOPY_MYSQL_PLATFORM`. Pinned
      `DEFAULT_MYSQL_VERSION` (8.4.11), override `DBCOPY_MYSQL_VERSION`.
      GOTCHAS, all verified: (a) `dev.mysql.com/get/...` is bot-protected and
      **403s any non-browser User-Agent**, so we hit `cdn.mysql.com`
      directly; (b) the CDN keeps the current release of a series under
      `/Downloads/MySQL-8.4/` and moves it to `/archives/mysql-8.4/` once
      superseded, so `_mysql_asset_url` returns BOTH and they are tried in
      order — a pinned version will eventually only exist at the second;
      (c) MySQL serves no `.sha256`/`.md5` sidecar, so checksum verification
      silently skips; (d) the macOS token embeds the macOS build
      (`macos15`), which moves between MySQL releases — pinned as
      `_MYSQL_MACOS_BUILD`; (e) `-minimal` exists only for Linux x86_64.
      The archive unpacks to ~1.2 GB, so `_Prune` keeps only `bin`+`lib`,
      drops `*.pdb` / `*-debug.*` / `debug/` / `*.lib` / `*.a` /
      `lib/mecab`, and (`bin_tools_only`) deletes every program in `bin/`
      that is not `mysql`/`mysqldump` while keeping the DLLs beside them.
      Result: 1.2 GB -> 73 MB installed.
    Gotcha: the published `.sha256` files can be Windows CertUtil multi-line
    format, not `hash  filename` — parse by regexing the first 64-hex token
    (and verification silently skips if no sidecar is served).
11. **Web jobs never block the request.** `app.py` starts copies in a
    daemon `threading.Thread`, stores state in an in-memory dict guarded by
    a lock, and exposes `GET /api/jobs/{id}` for polling. Blocking endpoints
    are plain `def` (FastAPI runs them in its threadpool). Connection-string
    passwords are redacted (`_redact`) before being stored/returned.
12. **Destructive operations confirm at the edge, not in core.**
    `clean_database` (Postgres: drop every user schema CASCADE + recreate
    `public`; MySQL: drop + recreate the database itself)
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

14. **MySQL adapter** (`dbcopy/adapters/mysql.py`, scheme `mysql`, default
    port 3306) wraps `mysqldump` / `mysql`:
    `backup` → `mysqldump --result-file=<file>` (plain SQL — mysqldump has no
    compressed custom format); `restore` → `mysql < file`; `copy` →
    `mysqldump | mysql` streamed with the exact SIGPIPE / no-`communicate()`
    pattern as Postgres (decision 5).
    - **Password** via `MYSQL_PWD` (decision 8). **`--protocol=TCP` is
      forced** — otherwise a host of `localhost` makes the client ignore the
      port and use a socket/named pipe, which fails against a container or
      tunnel that only exposes TCP.
    - **GOTCHA — `_conn_args()` deliberately carries no database.** `mysql`
      spells it `--database` but `mysqldump` takes it as a trailing
      positional, and MySQL's option parser resolves unique prefixes, so
      `--database` handed to mysqldump would be silently read as
      `--databases` and change what is dumped. The db is passed explicitly
      per command instead.
    - **GOTCHA — `mysqldump` rejects `--connect-timeout`** ("unknown
      variable", verified); only the `mysql` client accepts it. So the flag
      lives in `_mysql_cmd()`, not `_conn_args()`, and `copy_to`
      pre-flights `test_connection()` (which uses `mysql`) on BOTH endpoints
      before the unbounded data pipe — same reasoning as the Mongo adapter.
    - **Dumps omit `CREATE DATABASE`/`USE`** (no `--databases`), so a dump
      restores into a database of any name, matching Postgres behavior.
      `DUMP_FLAGS` are all load-bearing: `--single-transaction`,
      `--routines --triggers --events`, `--hex-blob`,
      `--set-gtid-purged=OFF` (else the restore is rejected on a GTID
      server), `--no-tablespaces` (needs PROCESS priv on RDS/Aurora),
      `--column-statistics=0` (the 8.4 client would otherwise query a table
      that 5.7 / MariaDB do not have). `--events` needs the EVENT privilege.
    - **`restore --clean` drops and recreates the whole database.** A
      mysqldump script already carries `DROP TABLE IF EXISTS` per table, so
      `clean` has to mean the stronger thing to be useful: it also removes
      objects that are not in the dump. NOTE this differs from Postgres,
      where `--clean` is `pg_restore --clean --if-exists` and drops only the
      objects the dump itself contains (a stray table survives) — that is
      pg_restore's native semantic and is left alone deliberately.
    - **`clean` / `overwrite`** drop and recreate the database, reading its
      charset/collation first (`_database_charset`) so a recreate does not
      silently change them. A copy into an auto-created target inherits the
      *source* database's charset/collation.
    - The `mysql` client in batch mode already aborts on the first error, so
      there is no `ON_ERROR_STOP` equivalent to set. `--binary-mode` is set
      on load so binary data does not trip "ASCII '\0' appeared in the
      statement".

15. **The PostgreSQL client version must match the SERVER being dumped.**
    pg_dump writes SQL for its own version, so the bundled newest client is
    the wrong tool for an older server: pg_dump 17+ always emits
    `SET transaction_timeout = 0`, which PG 16 and older reject with
    "unrecognized configuration parameter" — every PG16 -> PG16 copy failed
    this way until it was fixed (verified). So:
    - `toolbox.PG_VERSIONS` maps each PG major (12-18) to a theseus-rs
      release; `pg_version_for_major()` picks one, with an explicit
      `DBCOPY_PG_VERSION` always winning. Add new majors as they ship.
    - `PostgresAdapter._versioned_tool()` resolves **pg_dump / pg_restore**
      at that version; `_tool()` keeps returning the default build for
      **psql**, which is version-agnostic (it only ships the SQL). So
      `check_tools()` fetches psql only, and the matched dump tools are
      provisioned lazily — this also means a second ~44 MB download the
      first time a differently-versioned server is seen.
    - `server_major_version()` reads `SHOW server_version_num`. GOTCHA: it
      tries the adapter's own database and falls back to `postgres`, because
      `copy_to` needs the TARGET's version *before* the target database has
      been created — querying only its own db raised
      `FATAL: database "..." does not exist`.
    - `copy_to` rejects a **downgrade** (source major > target major) up
      front: pg_dump refuses to read a server newer than itself, so no client
      version can satisfy both ends. Clear error beats a cryptic one.
16. **A copy reports what it actually moved.** `copy_database` returns
    `{source_objects, target_objects, target_database, target_endpoint}`;
    `adapter.object_count()` backs it (concrete in `base.py` returning None,
    implemented for Postgres and MySQL, left None for Mongo — counting
    collections would need mongosh). The CLI prints the target database name
    and the count, and says outright when the source held nothing; `app.py`
    puts both counts on the job. Reason: a copy from an empty or
    misnamed source database succeeds while moving nothing, and "Copy
    complete" alone was indistinguishable from a real copy — the reported
    symptom that led here. `core._count_objects` swallows errors on purpose:
    the copy already succeeded, so failing to *describe* it must not turn a
    good copy into a reported failure.

## Project layout

```
dbcopy/
├── adapters/
│   ├── base.py       # DatabaseAdapter ABC + ConnectionInfo dataclass
│   ├── postgres.py   # PostgresAdapter
│   ├── mysql.py      # MySQLAdapter (mysqldump / mysql)
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
python -m dbcopy copy    mysql://u:p@src:3306/proddb      mysql://u:p@dst:3306/staging    [--overwrite]
python -m dbcopy backup  postgresql://u:p@host:5432/mydb -o mydb.dump
python -m dbcopy restore postgresql://u:p@host:5432/newdb -i mydb.dump [--clean]
python -m dbcopy clean   postgresql://u:p@host:5432/mydb [-y]   # removes ALL objects (not Mongo)
uv run python main.py    # dashboard at http://127.0.0.1:8000
```

`copy --overwrite` drops + recreates the target DB first for Postgres (for
non-empty targets, which otherwise fail fast with a clear "already exists"
hint) and for MySQL; for MongoDB it means `mongorestore --drop`
(collection-level). A MySQL copy into a non-empty target also succeeds
*without* `--overwrite`, because mysqldump emits `DROP TABLE IF EXISTS` per
table — `--overwrite` additionally removes objects absent from the source.
Cross-engine copy (e.g. Postgres ↔ MongoDB) is rejected (decision 9).

URL format: `postgresql://user:password@host:port/dbname` (schemes
`postgresql`/`postgres`), `mysql://...` (port defaults to 3306), or
`mongodb://...` / `mongodb+srv://...` (port defaults to 27017). Credentials are percent-decoded (`p%40ss` -> `p@ss`);
query params land in `ConnectionInfo.options` (Postgres `?sslmode=require`
-> PGSSLMODE; MySQL `?ssl-mode=REQUIRED` (or `?sslmode=`) -> `--ssl-mode`;
Mongo query options are carried through in the `--uri`). The
Postgres adapter sets `PGCONNECT_TIMEOUT=10` (setdefault, so a user-set env
var wins), MySQL passes `--connect-timeout=10` to the `mysql` client, and
the Mongo adapter injects `serverSelectionTimeoutMS=10000` —
without these, connecting to a firewalled host (typical RDS misconfig) hangs
for minutes and the dashboard fetch dies with browser "Failed to fetch".
`/api/test` appends an RDS hint (public accessibility + security group)
when the error is a timeout.

`copy` prints the target database name and how many tables/views landed,
and says so explicitly when the source database was empty (decision 16).

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

## MySQL verified working (tested 2026-09-09, MySQL 8.4.11 in Docker on :3306/:3307)

Fixture covered AUTO_INCREMENT, an FK with CASCADE, a view, a trigger, a
procedure, a function, BLOBs, utf8mb4 (CJK + emoji) and quote/backslash
text. Source and copy were compared with a fingerprint of
information_schema plus `HEX()` of every text/blob column — hexing matters,
because the Windows console mangles UTF-8 on the way out and a corrupted
copy would otherwise look identical to a good one.

- First run with no client tools installed: auto-downloaded 8.4.11 winx64
  (268 MB), pruned 1.2 GB -> 73 MB, both clients ran fine afterwards (the
  prune keeps the DLLs in `bin/` and `lib/private`, which they need).
- `copy` to an auto-created target: fingerprint identical, including
  AUTO_INCREMENT (`audit_log` was at 6 after deletes; next insert got 6).
- `copy --overwrite` and a plain re-`copy` into the now non-empty target:
  both matched the source exactly.
- `backup` -> `restore` into a fresh db: identical. Dump confirmed to
  contain no `CREATE DATABASE`/`USE` and no `GTID_PURGED`.
- `restore --clean`: a table added to the target that was absent from the
  dump was removed; fingerprint still matched.
- `clean`: 4 tables -> 0, charset/collation preserved as
  `utf8mb4/utf8mb4_0900_ai_ci`.
- Guardrails: cross-engine copy raises ValueError (HTTP 400 from
  `/api/copy`), unreachable port and bad password both raise RuntimeError
  with the server's message, missing dump file raises FileNotFoundError,
  and the password appears in no argv (only in `MYSQL_PWD`).
- CLI: copy / backup / restore --clean / clean (prompt aborts with exit 1,
  `-y` wipes) all as expected. Dashboard: `/api/test`, `/api/copy`
  (pending -> done in ~1 s, passwords redacted), `/api/clean` all OK.
- Regression: a fresh PostgreSQL provision into an empty `DBCOPY_HOME`
  still works after the shared toolbox changes (pg_dump 18.4).

Note: `python -m dbcopy` needs the venv (`uv run python -m dbcopy`) because
`cli.py` imports `web`, which imports `app`, which imports FastAPI at module
level. Pre-existing, and in tension with decision 4; not MySQL-specific.

## PostgreSQL re-verified (tested 2026-09-09, PG 16.15 / 17.11 / 18.4 in Docker)

Full source x target matrix, 3-table fixture with an FK, a view, an index, a
function and a serial sequence:

| src \ dst | 16 | 17 | 18 |
|-----------|----|----|----|
| **16**    | OK | OK | OK |
| **17**    | clear downgrade error | OK | OK |
| **18**    | clear downgrade error | clear downgrade error | OK |

- **16 -> 16 was broken before this fix** (`unrecognized configuration
  parameter "transaction_timeout"`) and now passes; the fix is decision 15.
- Sequence state survives a copy: after copying 2 customers, the next
  INSERT got id 3 (the check CLAUDE.md asks to re-run if dump flags change).
- `copy --overwrite`, `backup` -> `restore`, `restore --clean`, and `clean`
  all verified on PG 16, with pg_dump/pg_restore resolved from
  `postgresql-16.15.0` while psql stayed on `postgresql-18.4.0`.
- Three ways a copy could look successful while the user saw no tables, all
  now self-explaining: empty/misnamed source database ("nothing was
  copied"), tables in a non-public schema (count is reported, so it is
  clearly not zero), and a mixed-case target name (the message names the
  database it actually wrote to, e.g. `"Staging"`).
- Dashboard jobs carry `source_objects` / `target_objects`; an empty-source
  copy reports `done` with both 0 rather than an indistinguishable success.
- MySQL suite (18 checks) re-run green after the shared `core.copy_database`
  signature change.

## Roadmap / next steps (owner's stated intent)

1. **Dashboard enhancements**: backup/restore operations in the UI,
   persistent job history, progress percentage (needs `pg_dump --verbose`
   parsing or table counts).
2. Possible enhancement: parallel dump/restore for big DBs using
   `pg_dump --format=directory --jobs N` + `pg_restore --jobs N`.
3. Possible next engines: MariaDB (its client cannot do MySQL 8's default
   `caching_sha2_password`, so it needs its own tool family, not a reuse of
   `_MYSQL`), SQLite, MSSQL.

## Conventions

- Python 3.10+ syntax (uses `X | None` unions, dataclasses); pyproject pins
  `requires-python >= 3.14` (tarfile `filter="data"` needs 3.12+).
- New adapters must implement every abstract method in `base.py` and set
  the `schemes` tuple.
- Raise `RuntimeError` for tool/connection failures, `ValueError` for bad
  input; `cli.py` catches these and exits 1 with a clean message. `app.py`
  maps ValueError -> HTTP 400, job failures -> job `status: "error"`.
- Subprocess failures must surface stderr in the raised exception.
