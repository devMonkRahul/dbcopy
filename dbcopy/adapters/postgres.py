"""PostgreSQL adapter.

Wraps the battle-tested native tools instead of reimplementing them:
  - backup  -> pg_dump --format=custom   (compressed, supports pg_restore)
  - restore -> pg_restore
  - copy    -> pg_dump --format=plain piped straight into psql on the
               target (no intermediate file, works across servers)

pg_dump is always run at a version matching the server being dumped, not at
whatever version happens to be bundled: pg_dump writes SQL for its own
version, and a newer client emits statements an older server rejects (PG 17
added `transaction_timeout`, which PG 16 and older refuse outright). psql is
version-agnostic — it only ships the SQL — so it stays on the default build.
"""

from __future__ import annotations

import os
import subprocess

from .. import toolbox
from .base import ConnectionInfo, DatabaseAdapter


class PostgresAdapter(DatabaseAdapter):
    schemes = ("postgresql", "postgres")

    DEFAULT_PORT = 5432
    REQUIRED_TOOLS = ("pg_dump", "pg_restore", "psql")
    #: Memoized server major version (see server_major_version).
    _server_major: int | None = None
    #: First pg_dump major version with --exclude-extension (16 has only the
    #: include-form --extension, which cannot express "everything but these").
    EXCLUDE_EXTENSION_MIN_MAJOR = 17

    # ---- helpers ---------------------------------------------------------

    @classmethod
    def parse_url(cls, url: str) -> ConnectionInfo:
        return ConnectionInfo.from_url(url, default_port=cls.DEFAULT_PORT)

    def _env(self) -> dict:
        """Environment for subprocesses; password goes via PGPASSWORD so it
        never appears in `ps` output or shell history."""
        env = os.environ.copy()
        if self.info.password:
            env["PGPASSWORD"] = self.info.password
        # Fail fast on unreachable hosts (firewalled RDS, wrong host, ...)
        # instead of hanging for minutes. Respect a user-set value.
        env.setdefault("PGCONNECT_TIMEOUT", "10")
        # Honor ?sslmode=require etc. from the connection URL.
        sslmode = self.info.options.get("sslmode")
        if sslmode:
            env["PGSSLMODE"] = sslmode
        return env

    def _conn_args(self, database: str | None = None) -> list[str]:
        return [
            "--host", self.info.host,
            "--port", str(self.info.port),
            "--username", self.info.user,
            "--dbname", database or self.info.database,
        ]

    def check_tools(self) -> None:
        """Resolve the client tools, auto-downloading portable binaries on
        first use — no local PostgreSQL installation is required.

        Only psql is fetched here. It is version-agnostic (libpq talks to any
        server) and is what we use to ask the server which pg_dump version it
        needs; the matching pg_dump/pg_restore are provisioned lazily by
        _versioned_tool once that answer is known."""
        try:
            toolbox.ensure_tools(("psql",))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Could not provision PostgreSQL client tools: {exc}"
            ) from exc

    @staticmethod
    def _tool(name: str) -> str:
        """A version-agnostic tool (psql) from the default bundle."""
        return toolbox.find_tool(name)

    def _server_query(self, sql: str) -> str:
        """Run a server-wide query without assuming this adapter's database
        exists yet.

        Tries this adapter's own database, then the always-present `postgres`
        maintenance database: copy_to has to interrogate the TARGET server
        (version, available extensions) before the target database has been
        created."""
        candidates = [self.info.database]
        if "postgres" not in candidates:
            candidates.append("postgres")
        failure: Exception | None = None
        for database in candidates:
            try:
                return self._run([
                    self._tool("psql"), *self._conn_args(database=database),
                    "--no-psqlrc", "-tAc", sql,
                ]).stdout
            except RuntimeError as exc:
                failure = exc
        raise failure

    def server_major_version(self) -> int:
        """Major version of the server this adapter points at, e.g. 16."""
        if self._server_major is None:
            # server_version_num is e.g. 160015 for 16.15.
            out = self._server_query("SHOW server_version_num").strip()
            self._server_major = int(out) // 10000
        return self._server_major

    def installed_extensions(self) -> set[str]:
        """Extensions actually installed in THIS database."""
        out = self._run([
            self._tool("psql"), *self._conn_args(), "--no-psqlrc", "-tAc",
            "SELECT extname FROM pg_extension",
        ]).stdout
        return {line.strip() for line in out.splitlines() if line.strip()}

    def available_extensions(self) -> set[str]:
        """Extensions this SERVER could install — i.e. whose control files are
        present on the machine running PostgreSQL. Server-wide, so it is
        readable before the target database exists."""
        out = self._server_query("SELECT name FROM pg_available_extensions")
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _versioned_tool(self, name: str, major: int | None = None) -> str:
        """pg_dump / pg_restore built for a given server major version
        (this adapter's own server by default)."""
        version = toolbox.pg_version_for_major(major or self.server_major_version())
        return toolbox.find_tool(name, version=version)

    def _run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd, env=self._env(), capture_output=True, text=True, **kwargs
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed: {' '.join(cmd[:1])} ...\n{result.stderr.strip()}"
            )
        return result

    def test_connection(self) -> None:
        self.check_tools()
        self._run([self._tool("psql"), *self._conn_args(), "--no-psqlrc", "-tAc", "SELECT 1"])

    def database_exists(self, name: str) -> bool:
        result = self._run([
            self._tool("psql"), *self._conn_args(database="postgres"), "--no-psqlrc", "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{name}'",
        ])
        return result.stdout.strip() == "1"

    def create_database(self, name: str) -> None:
        self._run([
            self._tool("psql"), *self._conn_args(database="postgres"), "--no-psqlrc",
            "-c", f'CREATE DATABASE "{name}"',
        ])

    def drop_database(self, name: str) -> None:
        """Drop a database, terminating any open connections first."""
        # First, terminate all connections to the target database
        self._run([
            self._tool("psql"), *self._conn_args(database="postgres"), "--no-psqlrc",
            "-c", f"""SELECT pg_terminate_backend(pg_stat_activity.pid)
                      FROM pg_stat_activity
                      WHERE pg_stat_activity.datname = '{name}' 
                      AND pid <> pg_backend_pid()""",
        ])
        # Now drop the database (WITH FORCE for PG 13+)
        self._run([
            self._tool("psql"), *self._conn_args(database="postgres"), "--no-psqlrc",
            "-c", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
        ])

    def object_count(self) -> int:
        """Number of user tables and views, across every non-system schema."""
        result = self._run([
            self._tool("psql"), *self._conn_args(), "--no-psqlrc", "-tAc",
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')",
        ])
        return int(result.stdout.strip() or 0)

    def clean_database(self) -> None:
        """Drop every user schema CASCADE (removes all tables, views,
        sequences, functions, ...), then recreate an empty `public`."""
        self.check_tools()
        result = self._run([
            self._tool("psql"), *self._conn_args(), "--no-psqlrc", "-tAc",
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname <> 'information_schema' AND nspname NOT LIKE 'pg\\_%'",
        ])
        schemas = [s.strip() for s in result.stdout.splitlines() if s.strip()]
        for schema in schemas:
            self._run([
                self._tool("psql"), *self._conn_args(), "--no-psqlrc",
                "-c", f'DROP SCHEMA "{schema}" CASCADE',
            ])
        self._run([
            self._tool("psql"), *self._conn_args(), "--no-psqlrc",
            "-c", "CREATE SCHEMA public",
        ])

    # ---- core operations ---------------------------------------------------

    def backup(self, output_path: str) -> str:
        """Full dump in custom format (compressed, restorable selectively)."""
        self.check_tools()
        self._run([
            self._versioned_tool("pg_dump"), *self._conn_args(),
            "--format=custom",
            "--no-owner", "--no-acl",
            "--file", output_path,
        ])
        return output_path

    def restore(self, input_path: str, *, clean: bool = False) -> None:
        """Restore a custom-format dump created by backup()."""
        self.check_tools()
        if not os.path.exists(input_path):
            raise FileNotFoundError(input_path)
        if not self.database_exists(self.info.database):
            self.create_database(self.info.database)
        cmd = [
            self._versioned_tool("pg_restore"), *self._conn_args(),
            "--no-owner", "--no-acl",
        ]
        if clean:
            cmd += ["--clean", "--if-exists"]
        cmd.append(input_path)
        self._run(cmd)

    def copy_to(
        self,
        target: "PostgresAdapter",
        *,
        create_target: bool = True,
        overwrite: bool = False,
        skip_missing_extensions: bool = False,
    ) -> None:
        """Stream source -> target with no intermediate file:
        pg_dump --format=plain | psql (on the target)."""
        self.check_tools()
        target.check_tools()

        # A dump is only loadable into a server at least as new as the client
        # that wrote it, and pg_dump refuses to read a server newer than
        # itself — so a downgrade has no valid client version and cannot work.
        source_major = self.server_major_version()
        target_major = target.server_major_version()
        if source_major > target_major:
            raise RuntimeError(
                f"Cannot copy PostgreSQL {source_major} -> PostgreSQL "
                f"{target_major}: pg_dump cannot produce a dump that an older "
                "server accepts. Upgrade the target server to at least "
                f"{source_major}, or migrate the schema by hand."
            )

        # A dump recreates the source's extensions with CREATE EXTENSION, which
        # fails outright when the package is not installed on the target
        # machine (common when copying out of a managed Postgres such as
        # Supabase or RDS). Check every extension up front rather than dying
        # part-way through on whichever one pg_dump happens to emit first.
        missing = sorted(self.installed_extensions() - target.available_extensions())
        exclude_extensions: list[str] = []
        if missing:
            if not skip_missing_extensions:
                raise RuntimeError(
                    "The source database uses PostgreSQL extensions that the "
                    f"target server does not have available:\n  {', '.join(missing)}\n"
                    "Install them on the target (the extension files must exist "
                    "on the server itself, not just be enabled), or re-run with "
                    "--skip-missing-extensions to copy without them. Skipping is "
                    "lossy: any table or function that depends on one of these "
                    "will fail to copy."
                )
            exclude_extensions = missing

        dump_major = source_major
        if exclude_extensions and dump_major < self.EXCLUDE_EXTENSION_MIN_MAJOR:
            # --exclude-extension only exists in pg_dump 17+. A newer client is
            # allowed as long as the target can still read what it writes.
            if target_major < self.EXCLUDE_EXTENSION_MIN_MAJOR:
                raise RuntimeError(
                    "Skipping extensions needs pg_dump "
                    f"{self.EXCLUDE_EXTENSION_MIN_MAJOR}+, whose output a "
                    f"PostgreSQL {target_major} target rejects. Install these "
                    f"extensions on the target instead: {', '.join(exclude_extensions)}."
                )
            dump_major = self.EXCLUDE_EXTENSION_MIN_MAJOR

        if overwrite:
            target.drop_database(target.info.database)
            target.create_database(target.info.database)
        elif create_target and not target.database_exists(target.info.database):
            target.create_database(target.info.database)

        dump_cmd = [
            # Matched to the SOURCE server: it must be new enough to read it,
            # and the check above guarantees the target is no older.
            self._versioned_tool("pg_dump", dump_major), *self._conn_args(),
            "--format=plain",
            "--no-owner", "--no-acl",
        ]
        for extension in exclude_extensions:
            dump_cmd.append(f"--exclude-extension={extension}")
        restore_cmd = [
            target._tool("psql"), *target._conn_args(), "--no-psqlrc",
            "--set", "ON_ERROR_STOP=on",
            "--quiet",
        ]

        dump = subprocess.Popen(
            dump_cmd, env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        restore = subprocess.Popen(
            restore_cmd, env=target._env(),
            stdin=dump.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        dump.stdout.close()  # let dump receive SIGPIPE if restore dies

        _, restore_err = restore.communicate()
        # stdout is already closed, so communicate() would blow up trying to
        # read it — drain stderr directly instead.
        dump_err = dump.stderr.read()
        dump.stderr.close()
        dump.wait()

        # Check the restore side first: when psql dies mid-stream, pg_dump
        # only sees a broken pipe — psql's stderr holds the root cause.
        if restore.returncode != 0:
            message = restore_err.decode(errors="replace").strip()
            if "is not available" in message and "extension" in message:
                message += (
                    "\nHint: the target server does not have this extension "
                    "installed. Install it there, or re-run with "
                    "--skip-missing-extensions."
                )
            if "already exists" in message:
                message += (
                    "\nHint: the target database already contains objects. "
                    "Copy into a new database name (it will be auto-created), "
                    "or drop/recreate the target first."
                )
            raise RuntimeError(f"restore failed:\n{message}")
        if dump.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed:\n{dump_err.decode(errors='replace').strip()}"
            )
