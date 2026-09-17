# dbcopy — Project Memory

## What this project is

A Python tool to backup, restore, and make a full copy of a database from a
source to a target, with a CLI and a FastAPI web dashboard. PostgreSQL and
MySQL support all four operations; MongoDB supports copy only. The
architecture is designed so other engines can be added later without
touching core code. **The tool is self-sufficient: it does not require any
database client tools to be installed locally** — it downloads and caches
portable binaries itself (see decision 10), and MongoDB needs no binaries at
all because it runs on the pymongo driver (see decision 13).

## Core design decisions (do not change without good reason)

1. **Wrap native tools, never reimplement dump logic — for SQL engines.**
   Backup/restore/copy shell out to `pg_dump`/`pg_restore`/`psql` and
   `mysqldump`/`mysql`. These correctly handle schemas, data, sequences,
   indexes, constraints, views, and functions — hand-rolled row copying
   breaks on sequences and FKs. **MongoDB is the deliberate exception**
   (decision 13): it is driven through pymongo, because a document store has
   no sequences or FKs to get wrong, and the driver buys live per-collection
   progress, cancellation and resumability that piping `mongodump` into
   `mongorestore` could not.
2. **Adapter pattern for the tool-backed engines.** `dbcopy/adapters/base.py`
   defines the abstract `DatabaseAdapter` interface (`backup`, `restore`,
   `copy_to`, `test_connection`, `check_tools`). `get_adapter(url)` in
   `dbcopy/adapters/__init__.py` routes by URL scheme via the `ADAPTERS`
   registry list — which holds PostgreSQL and MySQL only. MongoDB is NOT an
   adapter (it implements one operation of the five, so the interface does
   not fit); `core._resolve_copy()` checks for a `mongodb://` URL first and
   hands off to `dbcopy/engines/mongo/`. `get_adapter` on a Mongo URL
   therefore raises "unsupported scheme" — every caller that could see one
   routes before it gets there.
3. **core.py stays UI-free.** No printing, no argparse — only raises
   exceptions. The CLI (`cli.py`) and the web app (`app.py`) both import it.
   (Exception: `toolbox.py` writes one-time download progress to stderr —
   infrastructure noise, never stdout.)
4. **The `dbcopy` package stays stdlib-only, except `dbcopy/engines/`.**
   No third-party imports in `adapters/`, `core.py`, `toolbox.py` or
   `cli.py` (argparse, subprocess, urllib, tarfile, hashlib). FastAPI is a
   project dependency but is imported only by the web layer (`app.py`,
   `main.py`). `dbcopy/engines/` is the documented carve-out for engines
   that need a driver: `engines/mongo/copier.py` imports pymongo and
   `engines/mongo/routes.py` imports FastAPI. The carve-out is contained —
   `engines/mongo/__init__.py` is stdlib-only URL helpers, and `core.py`
   imports `copier` *inside* `_copy_mongo()`, so a PostgreSQL or MySQL run
   never imports pymongo at all.
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
   analogous `MYSQL_PWD`. MongoDB spawns no subprocess at all, so the
   question does not arise — the password stays in the URI held in memory.
   `app.py`'s `_redact()` still hides it in job listings.
9. **Cross-engine copy (Postgres -> MySQL, Postgres -> MongoDB) is
   intentionally unsupported**; `core._resolve_copy` raises ValueError if
   one side is Mongo and the other is not, or if adapter types differ.
   `app.py` calls `core.check_copy_pair()` (the same check, no connection)
   to reject it with HTTP 400 before spawning a job.
10. **Self-managed client tools** (`dbcopy/toolbox.py`). Organized as a
    `_ToolFamily` registry (`_PG`, `_MYSQL`) so each engine's
    version/platform/download differences live in one descriptor; the public
    API `find_tool(name)` / `ensure_tools(names)` is unchanged and routes by
    tool name via `_family_for_tool`. MongoDB has no entry here — it needs
    no binaries. `find_tool(name)` resolves in this
    order: family override env dir (`DBCOPY_PG_BIN` /
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
13. **MongoDB copy engine** (`dbcopy/engines/mongo/`, schemes `mongodb` /
    `mongodb+srv`, default port 27017) is driven by **pymongo**, not by the
    MongoDB Database Tools. The tools-based adapter was removed: it could
    not report progress, could not be cancelled, and needed a 20s subprocess
    timeout because mongodump ignores `serverSelectionTimeoutMS` on an
    unreachable host. pymongo honors its own timeouts, so that whole class
    of workaround is gone.
    - **Three modules.** `copier.py` is the engine and imports no web
      framework — `core.py` drives it for the CLI and `routes.py` for HTTP,
      both through the same five-call API (`inspect`, `JobStore.create`,
      `run_copy`, `Job.snapshot`, `Job.cancel`). `routes.py` is an
      `APIRouter` under `/api/engines/mongo`, mounted by `app.py`.
      `__init__.py` is stdlib-only URL helpers (`is_mongo_url`,
      `database_in_url`, `endpoint_of`) so `core` can route without
      importing pymongo — see decision 4.
    - **Copy only.** `backup`, `restore` and `clean` raise a clear ValueError
      for a `mongodb://` URL (`core._reject_mongo`): the engine has no
      dump-file format. `clean`'s message points at `copy --overwrite`.
    - **What is copied:** every non-system collection, its documents, its
      collection options (capped, time-series, validators, collation), its
      secondary indexes, and views. GridFS comes free (`.files`/`.chunks`
      are ordinary collections). Users/roles/server settings are not — they
      live in `admin`.
    - **GOTCHA — `create_collection` raises two different things.** pymongo
      checks existence client-side and raises `CollectionInvalid`; the server
      raises `OperationFailure` code 48 (NamespaceExists). Both must be
      caught or every re-run into an existing target dies on the first
      collection. This is the single easiest bug to reintroduce here.
    - **GOTCHA — views need the same tolerance, and did not have it.** The
      spec this was built from caught the already-exists pair for
      collections but not for views, so a second copy into the same target
      reported every view as `failed`. `_create_view()` now drops and
      recreates an existing view (a view holds no data, so this is free and
      leaves the target matching the source). Verified.
    - **Views are created last.** A view referencing a collection that does
      not exist yet fails, so `run_copy` partitions `listCollections` output
      by `type` and does views after collections.
    - **Server-owned metadata must be stripped before replay.**
      `_DROP_CREATE_OPTS` (`idIndex`, `info`, `type`, `name`) and
      `_DROP_INDEX_OPTS` (`v`, `ns`, `key`, `textIndexVersion`,
      `2dsphereIndexVersion`, `background`) — passing any of them back into
      `create_collection` or `IndexModel` is an error.
    - **`socketTimeoutMS=0` is required** so a long `find` cursor is not
      killed mid-copy; connect/server-selection stay at 8s so a wrong URI
      fails fast (verified: unreachable host errors in ~10s, no hang).
    - **Without `overwrite` a copy is additive, and that is the point.**
      Documents whose `_id` is already present come back as duplicate-key
      errors inside a `BulkWriteError`, are counted as `skipped`, and the
      batch continues — so an interrupted run can just be re-run. Hence
      `insert_many(ordered=False)` and the handler that separates code 11000
      from every other write error (real errors still raise).
      GOTCHA: a collision on a *unique secondary index* is also code 11000,
      so it is counted as `skipped` too, not as a failure. That is
      deliberate — the two are indistinguishable by code, and failing the
      collection would break resumability. The per-collection `skipped`
      count is where it surfaces. Verified.
    - **`overwrite` (`drop_target`) drops the whole target database** before
      copying, unlike the old adapter's collection-level
      `mongorestore --drop`.
    - **Totals are estimates.** `estimated_document_count()` reads metadata
      instead of scanning (instant on a large database), so `percent` can
      drift slightly past or short of 100 on a live source. Never gate
      completion on it — gate on `state`.
    - **Cancel stops, it does not roll back.** The worker checks the flag
      between batches; documents already written stay written. The UI says so.
    - **`run_copy` never raises** — it records `state`/`error` on the job so
      a polling web caller sees the outcome. `Job.failure` keeps the original
      exception (never serialised into `snapshot()`) so `core._copy_mongo`
      can re-raise a user error as ValueError and everything else as
      RuntimeError, matching the repo convention.
    - `Job.on_log` is an optional sink invoked by `say()`; the CLI passes
      `print` so a terminal copy streams progress, the web layer leaves it
      unset and reads `snapshot()["log"]`. A raising sink is swallowed —
      a broken log must not abort a running copy.

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
      before the unbounded data pipe, so an unreachable host fails fast
      instead of hanging.
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
16. **Extensions the target cannot provide are caught before the copy
    starts.** A dump recreates the source's extensions with
    `CREATE EXTENSION`, which fails hard when the package is not installed on
    the target machine — the common wall when copying out of a managed
    Postgres (Supabase, RDS). `copy_to` diffs the source's `pg_extension`
    against the target's `pg_available_extensions` and raises listing **every**
    missing one, instead of dying part-way through on whichever one pg_dump
    emitted first. `skip_missing_extensions=True` (CLI
    `--skip-missing-extensions`, API field) passes `--exclude-extension` for
    each instead.
    - GOTCHA: `--exclude-extension` only exists in **pg_dump 17+**
      (`EXCLUDE_EXTENSION_MIN_MAJOR`); 16 has only the include-form
      `--extension`. When the source-matched client is older, the dump client
      is bumped to 17 — legal as long as it stays <= the target major
      (decision 15) — and otherwise raises rather than silently ignoring the
      request.
    - Skipping is lossy by nature: an object that USES a skipped extension
      (a `vector` column, say) still fails. That is verified, and the restore
      error appends a hint naming the skipped extensions so the cause is
      obvious. Extensions the target *does* have are still created normally.
    - `_server_query(sql)` is the shared "ask the server, not a specific
      database" helper (tries this adapter's db, falls back to `postgres`);
      both `server_major_version` and `available_extensions` use it, because
      copy_to interrogates the target before its database exists.
17. **A copy reports what it actually moved.** `copy_database` returns
    `{source_objects, target_objects, target_database, target_endpoint}`;
    `adapter.object_count()` backs it (concrete in `base.py` returning None,
    implemented for Postgres and MySQL). MongoDB builds the same summary in
    `core._copy_mongo` from the job snapshot instead — `source_objects` is
    the number of collections found, `target_objects` how many reached state
    `done` — and adds `object_label` ("collection" vs "table/view") so the
    CLI can name them correctly. The CLI prints the target database name
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
│   └── __init__.py   # ADAPTERS registry + get_adapter(url)  [PG + MySQL]
├── engines/          # driver-backed engines; MAY import third-party libs
│   └── mongo/
│       ├── __init__.py  # SCHEMES + URL helpers, stdlib only
│       ├── copier.py    # the pymongo engine; no FastAPI, no CLI imports
│       └── routes.py    # APIRouter at /api/engines/mongo, mounted by app.py
├── core.py           # backup_database / restore_database / copy_database
├── toolbox.py        # self-managed client tools (_ToolFamily registry)
├── cli.py            # argparse CLI: backup | restore | copy
└── __main__.py       # enables `python -m dbcopy`
app.py                # FastAPI dashboard + job API + mongo router
main.py               # `python main.py` -> uvicorn on 127.0.0.1:8000
static/
├── index.html        # PostgreSQL / MySQL dashboard
└── mongo.html        # MongoDB copy screen (picker, SSE progress, cancel)
```

## CLI / dashboard usage

```bash
python -m dbcopy copy    postgresql://u:p@src:5432/proddb postgresql://u:p@dst:5432/staging [--overwrite]
python -m dbcopy copy    mongodb://u:p@src:27017/proddb   mongodb://u:p@dst:27017/staging [--overwrite]
python -m dbcopy copy    mysql://u:p@src:3306/proddb      mysql://u:p@dst:3306/staging    [--overwrite]
python -m dbcopy backup  postgresql://u:p@host:5432/mydb -o mydb.dump
python -m dbcopy restore postgresql://u:p@host:5432/newdb -i mydb.dump [--clean]
python -m dbcopy clean   postgresql://u:p@host:5432/mydb [-y]   # removes ALL objects (PG/MySQL only)
uv run python main.py    # dashboard at http://127.0.0.1:8000
```

`copy --overwrite` drops + recreates the target DB first for Postgres (for
non-empty targets, which otherwise fail fast with a clear "already exists"
hint), for MySQL, and for MongoDB (a whole-database `drop_database`). A
MySQL copy into a non-empty target also succeeds *without* `--overwrite`,
because mysqldump emits `DROP TABLE IF EXISTS` per table — `--overwrite`
additionally removes objects absent from the source. A MongoDB copy without
it is additive and therefore resumable (decision 13).
Cross-engine copy (e.g. Postgres ↔ MongoDB) is rejected (decision 9).
`backup` / `restore` / `clean` reject a `mongodb://` URL outright.

URL format: `postgresql://user:password@host:port/dbname` (schemes
`postgresql`/`postgres`), `mysql://...` (port defaults to 3306), or
`mongodb://...` / `mongodb+srv://...` (port defaults to 27017). Credentials are percent-decoded (`p%40ss` -> `p@ss`);
query params land in `ConnectionInfo.options` (Postgres `?sslmode=require`
-> PGSSLMODE; MySQL `?ssl-mode=REQUIRED` (or `?sslmode=`) -> `--ssl-mode`).
A Mongo URL is NOT parsed into ConnectionInfo — it is handed to pymongo
verbatim, so `authSource`, `replicaSet`, `tls` and seed lists all just work;
`dbcopy/engines/mongo/__init__.py` only reads the database name and a
display endpoint out of it. The
Postgres adapter sets `PGCONNECT_TIMEOUT=10` (setdefault, so a user-set env
var wins), MySQL passes `--connect-timeout=10` to the `mysql` client, and
the Mongo engine uses pymongo's `serverSelectionTimeoutMS=8000` —
without these, connecting to a firewalled host (typical RDS misconfig) hangs
for minutes and the dashboard fetch dies with browser "Failed to fetch".
`/api/test` appends an RDS hint (public accessibility + security group)
when the error is a timeout.

`copy` prints the target database name and how many tables/views landed,
and says so explicitly when the source database was empty (decision 17).
`copy --skip-missing-extensions` (Postgres) copies without the extensions the
target server lacks (decision 16).

Dashboard API: `POST /api/test` {url}, `POST /api/copy` {source_url,
target_url, create_target, overwrite, skip_missing_extensions},
`POST /api/clean` {url},
`GET /api/jobs/{id}`, `GET /api/jobs`. All of these accept a `mongodb://`
URL too (`/api/test` pings, `/api/copy` runs the engine), except `/api/clean`
which reports the copy-only error.

MongoDB screen (decision 13) is served at `GET /mongodb` by `app.py`, not
by the engine router — the API below is what it calls:
`POST /connect` {uri} -> {ok, version, databases[], default_db};
`POST /copy` {source_uri, source_db, target_uri, target_db, drop_target,
copy_indexes, batch_size, collections} -> {job_id};
`GET /jobs/{id}` -> snapshot; `GET /jobs/{id}/stream` -> SSE snapshots every
500 ms, closed by the server on a terminal state; `POST /jobs/{id}/cancel`.
Job states: queued | running | done | failed | cancelled. Per collection:
pending | copying | done | failed | skipped.

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

## Extension handling verified (tested 2026-09-09)

Reproduced the reported Supabase failure with `pgvector/pgvector:pg17` as
source (has `vector` + `pgcrypto`) and stock `postgres:17` as target (has
`pgcrypto` only):

- Default: refuses up front naming `vector` only — `pgcrypto` is correctly
  NOT reported, because the target has it available.
- `--skip-missing-extensions` with a table using a `vector(3)` column: still
  fails (`type "public.vector" does not exist`) and the hint explains that a
  skipped extension is the cause. This is the documented lossy limit.
- `--skip-missing-extensions` with no object depending on it (the Supabase
  shape, where `supabase_vault` serves the internal `vault` schema rather
  than the user's tables): copy succeeds, rows intact, `pgcrypto` still
  created on the target, `vector` omitted.
- Same three paths verified through `/api/copy` with
  `{"skip_missing_extensions": true}`.

## MongoDB re-verified on the pymongo engine (tested 2026-09-17, MongoDB 8.2.11 on :27017/:27018)

Replaced the mongodump/mongorestore adapter entirely (decision 13). Fixture
covered 8 collections + 1 view: 2500 docs with a unique and a compound
index, a capped collection (max=100), a TTL index, a text index, a partial
index, GridFS-shaped `.files`/`.chunks`, an empty collection, and a document
of exotic BSON (ObjectId, Decimal128, Binary, tz-aware datetime, 2**62,
nested arrays, CJK + emoji + quote/backslash text).

Source and copy were compared with a fingerprint of every document (SHA-256
over canonical `json_util` form, order-independent), plus collection options
and full index definitions.

- Fresh copy: **identical** — documents, collection options (capped/size/max
  survived), every index, and the view definition.
- Re-run without `--overwrite`: 0 copied, all 2701 counted as `skipped`
  duplicates, target unchanged -> resumability confirmed.
- Re-run with `--overwrite`: dropped first, back to exactly the source
  counts, not doubled.
- **Bug found and fixed:** the first re-run marked the view `failed`
  ("collection high_scores already exists") because the spec tolerated the
  already-exists error pair for collections but not views. `_create_view()`
  now drops and recreates. See decision 13.
- Unique secondary index violated by source data: the losing document is
  counted as `skipped` (not a collection failure) and the other collections
  still copy — verified, and documented as deliberate in decision 13.
- Unreachable host (10.255.255.1): fails in ~10s via pymongo's own timeout,
  no hang. The old adapter needed a manual 20s subprocess timeout for this.
- Guardrails: same-URI-and-database rejected before anything is written,
  cross-engine both directions rejected, a URL with no database rejected,
  an empty/misnamed source database reports "No collections found".
- CLI: `copy` streams live per-collection progress (`Job.on_log`) and
  reports "now holds 9 collections" (not "tables/views"). `backup`,
  `restore` and `clean` refuse a mongodb:// URL with the copy-only message.
- Web (39 checks, all green): both screens serve, `/connect` lists and flags
  system databases, unreachable host -> 400 not a hang, non-mongo scheme ->
  422, copy job reaches `done` with 2701 docs and percent 100, SSE stream
  carries `text/event-stream` + `X-Accel-Buffering: no` and closes itself on
  a terminal state, cancel mid-run ends `cancelled` with partial data intact
  and no error, unknown job -> 404, snapshot is JSON-serialisable and never
  leaks the exception object, passwords redacted in `/api/jobs`.
- Regression: PostgreSQL 17 copy / backup / `restore --clean` / `clean` all
  still pass after the shared `core.copy_database` refactor, including the
  sequence-state check (after copying 2 customers the next INSERT got id 3).

Note: `python -m dbcopy` still needs the venv (`uv run python -m dbcopy`)
because `cli.py` imports `web`, which imports `app`, which imports FastAPI
at module level. Pre-existing, in tension with decision 4, not MongoDB's
doing.

## Roadmap / next steps (owner's stated intent)

1. **Dashboard enhancements**: backup/restore operations in the UI,
   persistent job history, progress percentage (needs `pg_dump --verbose`
   parsing or table counts).
2. Possible enhancement: parallel dump/restore for big DBs using
   `pg_dump --format=directory --jobs N` + `pg_restore --jobs N`.
3. Possible next engines: MariaDB (its client cannot do MySQL 8's default
   `caching_sha2_password`, so it needs its own tool family, not a reuse of
   `_MYSQL`), SQLite, MSSQL.
4. MongoDB gaps left open by the copy-only engine: no backup-to-file, and
   `collections: [...]` (subset copy) is wired through the HTTP API and
   `run_copy` but not exposed on the CLI or the screen.

## Conventions

- Python 3.10+ syntax (uses `X | None` unions, dataclasses); pyproject pins
  `requires-python >= 3.14` (tarfile `filter="data"` needs 3.12+).
- New adapters must implement every abstract method in `base.py` and set
  the `schemes` tuple.
- Raise `RuntimeError` for tool/connection failures, `ValueError` for bad
  input; `cli.py` catches these and exits 1 with a clean message. `app.py`
  maps ValueError -> HTTP 400, job failures -> job `status: "error"`.
- Subprocess failures must surface stderr in the raised exception.
