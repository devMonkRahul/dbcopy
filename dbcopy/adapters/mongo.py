"""MongoDB adapter.

Wraps the native MongoDB Database Tools instead of reimplementing them:
  - backup  -> mongodump --archive=<file> --gzip   (single compressed archive)
  - restore -> mongorestore --archive=<file> --gzip
  - copy    -> mongodump --archive | mongorestore --archive  (streamed, no
               intermediate file — the same pattern as the Postgres adapter)

Notes specific to MongoDB:
  * There is no PGPASSWORD-style env var, so the password is passed through a
    temporary ``--config`` file (deleted afterwards) to keep it off the command
    line and out of `ps` output (decision 8).
  * Connection is passed as a ``--uri`` with the password stripped out, so
    mongodb+srv URLs, comma-separated seed lists and query options
    (authSource, replicaSet, tls, ...) all keep working.
  * ``clean`` (wiping an entire database) is not supported: it needs mongosh,
    which dbcopy deliberately does not bundle. Use ``copy --overwrite`` to
    replace collections instead.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from .. import toolbox
from .base import ConnectionInfo, DatabaseAdapter


class MongoAdapter(DatabaseAdapter):
    schemes = ("mongodb", "mongodb+srv")

    DEFAULT_PORT = 27017
    REQUIRED_TOOLS = ("mongodump", "mongorestore")

    # ---- helpers ---------------------------------------------------------

    @classmethod
    def parse_url(cls, url: str) -> ConnectionInfo:
        """Tolerant parse. host/port are informational only for MongoDB (the
        raw URL is passed through as --uri), so we extract the FIRST host
        defensively — a comma seed list (h1:27017,h2:27017) would otherwise
        trip urlsplit's .port integer cast, and mongodb+srv omits the port."""
        parts = urlsplit(url)
        database = (parts.path or "").lstrip("/").split("/")[0]
        if not database:
            raise ValueError(f"No database name found in URL: {url}")

        authority = parts.netloc.rpartition("@")[2]  # drop any credentials
        first = authority.split(",")[0]
        if first.startswith("["):            # IPv6 literal, e.g. [::1]:27017
            host, _, rest = first.partition("]")
            host, port_s = host[1:], rest.lstrip(":")
        else:
            host, _, port_s = first.partition(":")
        try:
            port = int(port_s) if port_s else cls.DEFAULT_PORT
        except ValueError:
            port = cls.DEFAULT_PORT

        return ConnectionInfo(
            scheme=parts.scheme,
            host=unquote(host) if host else "localhost",
            port=port,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=unquote(database),
            options=dict(parse_qsl(parts.query)),
        )

    def _uri(self) -> str:
        """The connection URL with the password removed (it is supplied via a
        ``--config`` file instead, so it never lands on the command line).

        Operates on the raw URL string so mongodb+srv and comma-separated seed
        lists survive untouched. Also injects a short serverSelectionTimeoutMS
        so unreachable hosts fail fast instead of hanging (the Mongo analog of
        Postgres' PGCONNECT_TIMEOUT)."""
        parts = urlsplit(self.url)
        netloc = parts.netloc
        creds, sep, hosts = netloc.rpartition("@")
        if sep:  # credentials present -> keep only the username
            user = creds.split(":", 1)[0]
            netloc = f"{user}@{hosts}" if user else hosts
        query = parts.query
        if "serverSelectionTimeoutMS" not in query:
            query = (query + "&" if query else "") + "serverSelectionTimeoutMS=10000"
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))

    @contextlib.contextmanager
    def _password_config(self):
        """Yield ``["--config", <file>]`` with the password in a temp YAML file
        (mode 0600, deleted on exit), or ``[]`` when there is no password."""
        if not self.info.password:
            yield []
            return
        fd, path = tempfile.mkstemp(prefix="dbcopy-", suffix=".yaml")
        try:
            # YAML double-quoted string: escape backslash and double-quote.
            escaped = self.info.password.replace("\\", "\\\\").replace('"', '\\"')
            with os.fdopen(fd, "w") as f:
                f.write(f'password: "{escaped}"\n')
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # best effort (Windows ACLs differ)
            yield ["--config", path]
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)

    def _conn_args(self) -> list[str]:
        return ["--uri", self._uri()]

    def check_tools(self) -> None:
        """Resolve mongodump/mongorestore, auto-downloading the portable
        MongoDB Database Tools on first use — no local install required."""
        try:
            toolbox.ensure_tools(self.REQUIRED_TOOLS)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Could not provision MongoDB client tools: {exc}"
            ) from exc

    @staticmethod
    def _tool(name: str) -> str:
        return toolbox.find_tool(name)

    def _run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed: {' '.join(cmd[:1])} ...\n{result.stderr.strip()}"
            )
        return result

    def test_connection(self) -> None:
        """Probe connectivity + auth cheaply by dumping a non-existent
        collection (mongodump connects, finds nothing, exits 0)."""
        self.check_tools()
        with self._password_config() as cfg, tempfile.TemporaryDirectory() as tmp:
            self._run([
                self._tool("mongodump"), *self._conn_args(), *cfg,
                "--db", self.info.database,
                "--collection", "__dbcopy_conn_probe__",
                "--out", tmp,
                "--quiet",
            ])

    def clean_database(self) -> None:
        raise RuntimeError(
            "clean is not supported for MongoDB: wiping a database requires "
            "mongosh, which dbcopy does not bundle. Drop or recreate the "
            "database with your own Mongo client, or use copy --overwrite to "
            "replace collections."
        )

    # ---- core operations -------------------------------------------------

    def backup(self, output_path: str) -> str:
        """Dump the database to a single compressed archive file."""
        self.check_tools()
        with self._password_config() as cfg:
            self._run([
                self._tool("mongodump"), *self._conn_args(), *cfg,
                "--db", self.info.database,
                f"--archive={output_path}",
                "--gzip",
            ])
        return output_path

    def restore(self, input_path: str, *, clean: bool = False) -> None:
        """Restore an archive created by backup(). Restores the namespaces the
        archive carries (for dbcopy backups that is the database it was taken
        from); pass clean=True to drop existing collections first."""
        self.check_tools()
        if not os.path.exists(input_path):
            raise FileNotFoundError(input_path)
        with self._password_config() as cfg:
            cmd = [
                self._tool("mongorestore"), *self._conn_args(), *cfg,
                f"--archive={input_path}",
                "--gzip",
            ]
            if clean:
                cmd.append("--drop")
            self._run(cmd)

    def copy_to(
        self,
        target: "MongoAdapter",
        *,
        create_target: bool = True,
        overwrite: bool = False,
    ) -> None:
        """Stream source -> target with no intermediate file:
        mongodump --archive | mongorestore --archive (on the target).

        create_target is effectively always on for MongoDB — databases and
        collections are created implicitly on first write, so there is nothing
        to pre-create. overwrite adds mongorestore --drop, which drops each
        collection just before it is restored (collection-level, not a whole
        -database drop; that would need mongosh)."""
        self.check_tools()
        target.check_tools()

        src_db = self.info.database
        tgt_db = target.info.database

        with self._password_config() as src_cfg, target._password_config() as tgt_cfg:
            dump_cmd = [
                self._tool("mongodump"), *self._conn_args(), *src_cfg,
                "--db", src_db,
                "--archive",  # -> stdout
            ]
            restore_cmd = [
                self._tool("mongorestore"), *target._conn_args(), *tgt_cfg,
                "--archive",  # <- stdin
                # Remap the (single) dumped db into the target db name so a
                # copy into a differently-named database lands correctly.
                "--nsFrom", f"{src_db}.*",
                "--nsTo", f"{tgt_db}.*",
                "--quiet",
            ]
            if overwrite:
                restore_cmd.append("--drop")

            dump = subprocess.Popen(
                dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            restore = subprocess.Popen(
                restore_cmd, stdin=dump.stdout,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            dump.stdout.close()  # let dump receive SIGPIPE if restore dies

            _, restore_err = restore.communicate()
            # stdout is already closed, so communicate() would blow up trying
            # to read it — drain stderr directly instead (see decision 5).
            dump_err = dump.stderr.read()
            dump.stderr.close()
            dump.wait()

        # Check the restore side first: when it dies mid-stream, mongodump only
        # sees a broken pipe — mongorestore's stderr holds the root cause.
        if restore.returncode != 0:
            raise RuntimeError(
                f"mongorestore failed:\n{restore_err.decode(errors='replace').strip()}"
            )
        if dump.returncode != 0:
            raise RuntimeError(
                f"mongodump failed:\n{dump_err.decode(errors='replace').strip()}"
            )
