"""PostgreSQL adapter.

Wraps the battle-tested native tools instead of reimplementing them:
  - backup  -> pg_dump --format=custom   (compressed, supports pg_restore)
  - restore -> pg_restore
  - copy    -> pg_dump --format=plain piped straight into psql on the
               target (no intermediate file, works across servers)
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
        first use — no local PostgreSQL installation is required."""
        try:
            toolbox.ensure_tools(self.REQUIRED_TOOLS)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Could not provision PostgreSQL client tools: {exc}"
            ) from exc

    @staticmethod
    def _tool(name: str) -> str:
        return toolbox.find_tool(name)

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
            self._tool("pg_dump"), *self._conn_args(),
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
            self._tool("pg_restore"), *self._conn_args(),
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
    ) -> None:
        """Stream source -> target with no intermediate file:
        pg_dump --format=plain | psql (on the target)."""
        self.check_tools()
        target.check_tools()

        if overwrite:
            target.drop_database(target.info.database)
            target.create_database(target.info.database)
        elif create_target and not target.database_exists(target.info.database):
            target.create_database(target.info.database)

        dump_cmd = [
            self._tool("pg_dump"), *self._conn_args(),
            "--format=plain",
            "--no-owner", "--no-acl",
        ]
        restore_cmd = [
            self._tool("psql"), *target._conn_args(), "--no-psqlrc",
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
            message = restore_err.decode().strip()
            if "already exists" in message:
                message += (
                    "\nHint: the target database already contains objects. "
                    "Copy into a new database name (it will be auto-created), "
                    "or drop/recreate the target first."
                )
            raise RuntimeError(f"restore failed:\n{message}")
        if dump.returncode != 0:
            raise RuntimeError(f"pg_dump failed:\n{dump_err.decode().strip()}")
