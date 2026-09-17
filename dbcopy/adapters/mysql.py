"""MySQL adapter.

Wraps the native client tools instead of reimplementing them:
  - backup  -> mysqldump --result-file=<file>   (plain SQL)
  - restore -> mysql < <file>
  - copy    -> mysqldump | mysql on the target (streamed, no intermediate
               file — the same pattern as the Postgres adapter)

Notes specific to MySQL:
  * ``mysqldump`` has no compressed "custom" format like ``pg_dump``, so a
    backup is a plain ``.sql`` script. It is dumped WITHOUT ``--databases``,
    i.e. with no ``CREATE DATABASE`` / ``USE`` header, so the same file can be
    restored into a database of any name (the Postgres adapter behaves the
    same way).
  * The password is passed through the ``MYSQL_PWD`` environment variable —
    the direct analog of Postgres' ``PGPASSWORD`` — so it never lands on the
    command line where ``ps`` would show it (decision 8).
  * ``--protocol=TCP`` is forced. Otherwise a host of "localhost" makes the
    client silently ignore the port and use a Unix socket / named pipe, which
    breaks against a server that is only reachable over the mapped TCP port.
  * MySQL has no separate schema layer — a "database" *is* the schema — so
    ``clean`` drops and recreates the database itself (preserving its charset
    and collation), which leaves it empty just like the Postgres version.
"""

from __future__ import annotations

import os
import subprocess

from .. import toolbox
from .base import ConnectionInfo, DatabaseAdapter


class MySQLAdapter(DatabaseAdapter):
    schemes = ("mysql",)

    DEFAULT_PORT = 3306
    REQUIRED_TOOLS = ("mysqldump", "mysql")
    #: Seconds to wait for a TCP connection before giving up, so an
    #: unreachable host (firewalled RDS, wrong port) fails fast instead of
    #: hanging the CLI or a dashboard request. The Postgres analog is
    #: PGCONNECT_TIMEOUT; MySQL has no env var for it.
    #: GOTCHA: only the `mysql` client accepts --connect-timeout — mysqldump
    #: rejects it outright ("unknown variable 'connect-timeout'"), so it is
    #: applied in _mysql_cmd() rather than in the shared _conn_args(). To keep
    #: an unreachable host from hanging a dump, every operation that starts
    #: mysqldump pre-flights the connection with the `mysql` client first.
    CONNECT_TIMEOUT = 10

    #: Dump flags that make a dump portable between servers. Explained here
    #: rather than inline because every one of them is load-bearing:
    #:   --single-transaction  consistent InnoDB snapshot without locking
    #:   --routines/--triggers/--events  stored programs are part of the schema
    #:   --hex-blob            binary columns survive a text pipe intact
    #:   --set-gtid-purged=OFF omit SET @@GLOBAL.GTID_PURGED, which a restore
    #:                         into another server rejects unless it is a
    #:                         pristine replica target
    #:   --no-tablespaces      skip the tablespace clauses, which need the
    #:                         PROCESS privilege that managed MySQL (RDS,
    #:                         Aurora, Cloud SQL) does not grant
    #:   --column-statistics=0 do not query information_schema.COLUMN_STATISTICS
    #:                         (optimizer histograms); the bundled 8.4 client
    #:                         would otherwise fail against a 5.7 or MariaDB
    #:                         server, which has no such table
    DUMP_FLAGS = (
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--hex-blob",
        "--set-gtid-purged=OFF",
        "--no-tablespaces",
        "--column-statistics=0",
        "--default-character-set=utf8mb4",
    )

    # ---- helpers ---------------------------------------------------------

    @classmethod
    def parse_url(cls, url: str) -> ConnectionInfo:
        return ConnectionInfo.from_url(url, default_port=cls.DEFAULT_PORT)

    def _env(self) -> dict:
        """Environment for subprocesses; the password goes via MYSQL_PWD so it
        never appears in `ps` output or shell history."""
        env = os.environ.copy()
        if self.info.password:
            env["MYSQL_PWD"] = self.info.password
        return env

    def _conn_args(self) -> list[str]:
        """Connection flags shared by every tool.

        Deliberately carries NO database: `mysql` spells it `--database` but
        `mysqldump` takes it as a positional argument, and MySQL's option
        parser resolves unique prefixes — so `--database` handed to mysqldump
        would silently be read as `--databases` and change what gets dumped.
        Callers pass the database explicitly instead."""
        args = [
            "--host", self.info.host,
            "--port", str(self.info.port),
            # Never fall back to a socket/named pipe for host=localhost.
            "--protocol=TCP",
        ]
        if self.info.user:
            args += ["--user", self.info.user]
        # Honor ?ssl-mode=REQUIRED (or ?sslmode=... for symmetry with Postgres).
        ssl_mode = self.info.options.get("ssl-mode") or self.info.options.get("sslmode")
        if ssl_mode:
            args.append(f"--ssl-mode={ssl_mode.upper()}")
        return args

    def _mysql_cmd(self, *extra: str) -> list[str]:
        """The `mysql` client with connection flags and a connect timeout."""
        return [
            self._tool("mysql"), *self._conn_args(),
            f"--connect-timeout={self.CONNECT_TIMEOUT}",
            *extra,
        ]

    @staticmethod
    def _quote_literal(value: str) -> str:
        """Escape a value for use inside single quotes in a SQL statement."""
        return value.replace("\\", "\\\\").replace("'", "''")

    @staticmethod
    def _quote_ident(name: str) -> str:
        """Backtick-quote an identifier (a backtick inside is doubled)."""
        escaped = name.replace("`", "``")
        return f"`{escaped}`"

    def check_tools(self) -> None:
        """Resolve mysqldump/mysql, auto-downloading portable binaries on
        first use — no local MySQL installation is required."""
        try:
            toolbox.ensure_tools(self.REQUIRED_TOOLS)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Could not provision MySQL client tools: {exc}"
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

    def _sql(self, statement: str, *, database: str | None = None) -> str:
        """Run one statement with the `mysql` client and return its output.

        Batch mode (the default when stdin is not a terminal) already aborts
        on the first error and exits non-zero, so there is no ON_ERROR_STOP
        equivalent to set."""
        cmd = self._mysql_cmd()
        if database:
            cmd += ["--database", database]
        cmd += ["--batch", "--skip-column-names", "--execute", statement]
        return self._run(cmd).stdout

    def test_connection(self) -> None:
        self.check_tools()
        self._sql("SELECT 1")

    def database_exists(self, name: str) -> bool:
        out = self._sql(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            f"WHERE SCHEMA_NAME = '{self._quote_literal(name)}'"
        )
        return out.strip() != ""

    def object_count(self) -> int:
        """Number of tables and views in this database."""
        out = self._sql(
            "SELECT COUNT(*) FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{self._quote_literal(self.info.database)}'"
        ).strip()
        return int(out or 0)

    def create_database(self, name: str, *, charset: str = "utf8mb4",
                        collation: str | None = None) -> None:
        statement = (
            f"CREATE DATABASE IF NOT EXISTS {self._quote_ident(name)} "
            f"CHARACTER SET {charset}"
        )
        if collation:
            statement += f" COLLATE {collation}"
        self._sql(statement)

    def drop_database(self, name: str) -> None:
        self._sql(f"DROP DATABASE IF EXISTS {self._quote_ident(name)}")

    def _database_charset(self, name: str) -> tuple[str, str | None]:
        """Current default charset/collation of a database, so a drop +
        recreate does not silently change them. Falls back to utf8mb4."""
        out = self._sql(
            "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
            "FROM information_schema.SCHEMATA "
            f"WHERE SCHEMA_NAME = '{self._quote_literal(name)}'"
        ).strip()
        if not out:
            return "utf8mb4", None
        charset, _, collation = out.split("\n")[0].partition("\t")
        return charset or "utf8mb4", collation or None

    def _recreate_database(self, name: str) -> None:
        """Drop and recreate a database, keeping its charset/collation."""
        charset, collation = self._database_charset(name)
        self.drop_database(name)
        self.create_database(name, charset=charset, collation=collation)

    def clean_database(self) -> None:
        """Empty the database. In MySQL a database *is* a schema, so the
        equivalent of Postgres' "drop every schema and recreate public" is to
        drop the database and recreate it with the same charset/collation."""
        self.check_tools()
        self._recreate_database(self.info.database)

    # ---- core operations ---------------------------------------------------

    def _dump_cmd(self, *extra: str) -> list[str]:
        """mysqldump for this database — the database is the trailing
        positional argument (see _conn_args on why it is not a flag)."""
        return [
            self._tool("mysqldump"), *self._conn_args(), *self.DUMP_FLAGS,
            *extra,
            self.info.database,
        ]

    def _load_cmd(self) -> list[str]:
        """mysql set up to read a dump script from stdin."""
        return self._mysql_cmd(
            "--database", self.info.database,
            "--default-character-set=utf8mb4",
            # Tolerate binary data in the stream instead of failing with
            # "ASCII '\\0' appeared in the statement".
            "--binary-mode",
        )

    def backup(self, output_path: str) -> str:
        """Full dump as a plain SQL script (mysqldump has no custom format).

        --result-file is used rather than shell redirection so mysqldump
        writes the file itself and does not translate newlines on Windows."""
        self.check_tools()
        self._run(self._dump_cmd(f"--result-file={output_path}"))
        return output_path

    def restore(self, input_path: str, *, clean: bool = False) -> None:
        """Restore a SQL dump created by backup().

        A mysqldump script already carries DROP TABLE IF EXISTS for every
        table it contains, so clean=True means the stronger thing: drop and
        recreate the whole database first, which also removes objects that
        are *not* in the dump."""
        self.check_tools()
        if not os.path.exists(input_path):
            raise FileNotFoundError(input_path)
        if clean and self.database_exists(self.info.database):
            self._recreate_database(self.info.database)
        else:
            self.create_database(self.info.database)

        with open(input_path, "rb") as script:
            result = subprocess.run(
                self._load_cmd(), env=self._env(),
                stdin=script, capture_output=True, text=True,
            )
        if result.returncode != 0:
            raise RuntimeError(f"mysql restore failed:\n{result.stderr.strip()}")

    def copy_to(
        self,
        target: "MySQLAdapter",
        *,
        create_target: bool = True,
        overwrite: bool = False,
        skip_missing_extensions: bool = False,  # no MySQL equivalent; ignored
    ) -> None:
        """Stream source -> target with no intermediate file:
        mysqldump | mysql (on the target).

        The dump has no CREATE DATABASE/USE header, so piping it into
        `mysql --database <target>` lands it in the target database even when
        the two databases have different names."""
        self.check_tools()
        target.check_tools()
        # mysqldump has no --connect-timeout, so probe both endpoints with the
        # `mysql` client (which does) before starting the unbounded data pipe.
        # Otherwise an unreachable host hangs the copy indefinitely.
        self.test_connection()
        target.test_connection()

        if overwrite and target.database_exists(target.info.database):
            target._recreate_database(target.info.database)
        elif create_target and not target.database_exists(target.info.database):
            # Give the copy the source database's charset/collation.
            charset, collation = self._database_charset(self.info.database)
            target.create_database(
                target.info.database, charset=charset, collation=collation
            )

        dump = subprocess.Popen(
            self._dump_cmd(), env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        restore = subprocess.Popen(
            target._load_cmd(), env=target._env(),
            stdin=dump.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        dump.stdout.close()  # let dump receive SIGPIPE if restore dies

        _, restore_err = restore.communicate()
        # stdout is already closed, so communicate() would blow up trying to
        # read it — drain stderr directly instead (see decision 5).
        dump_err = dump.stderr.read()
        dump.stderr.close()
        dump.wait()

        # Check the restore side first: when mysql dies mid-stream, mysqldump
        # only sees a broken pipe — mysql's stderr holds the root cause.
        if restore.returncode != 0:
            message = restore_err.decode(errors="replace").strip()
            if "Unknown database" in message:
                message += (
                    "\nHint: the target database does not exist. Drop "
                    "--no-create so dbcopy creates it for you."
                )
            raise RuntimeError(f"mysql restore failed:\n{message}")
        if dump.returncode != 0:
            raise RuntimeError(
                f"mysqldump failed:\n{dump_err.decode(errors='replace').strip()}"
            )
