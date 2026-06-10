"""Abstract base class that every database adapter must implement.

To add support for a new database (MySQL, MongoDB, ...), create a new
module in this package with a class that subclasses DatabaseAdapter,
then register it in dbcopy/adapters/__init__.py. Nothing else in the
codebase needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlparse


@dataclass
class ConnectionInfo:
    """Parsed pieces of a database connection URL."""

    scheme: str
    host: str
    port: int
    user: str
    password: str
    database: str
    #: extra query parameters, e.g. {"sslmode": "require"}
    options: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_url(cls, url: str, default_port: int) -> "ConnectionInfo":
        parsed = urlparse(url)
        database = (parsed.path or "").lstrip("/")
        if not database:
            raise ValueError(f"No database name found in URL: {url}")
        # urlparse does NOT percent-decode credentials — do it here so
        # passwords with special characters (@ : / # ...) can be written
        # URL-encoded, e.g. p%40ss for p@ss.
        return cls(
            scheme=parsed.scheme,
            host=parsed.hostname or "localhost",
            port=parsed.port or default_port,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=unquote(database),
            options=dict(parse_qsl(parsed.query)),
        )


class DatabaseAdapter(ABC):
    """Common interface for backup / restore / copy operations."""

    #: URL schemes this adapter handles, e.g. ("postgresql", "postgres")
    schemes: tuple[str, ...] = ()

    def __init__(self, url: str):
        self.url = url
        self.info = self.parse_url(url)

    # ---- required hooks -------------------------------------------------

    @classmethod
    @abstractmethod
    def parse_url(cls, url: str) -> ConnectionInfo:
        """Parse a connection URL into ConnectionInfo."""

    @abstractmethod
    def check_tools(self) -> None:
        """Raise RuntimeError if required client tools are missing."""

    @abstractmethod
    def test_connection(self) -> None:
        """Raise RuntimeError if the database is unreachable."""

    @abstractmethod
    def backup(self, output_path: str) -> str:
        """Dump the database to a file. Returns the file path."""

    @abstractmethod
    def restore(self, input_path: str, *, clean: bool = False) -> None:
        """Restore a dump file into this database."""

    @abstractmethod
    def copy_to(
        self,
        target: "DatabaseAdapter",
        *,
        create_target: bool = True,
        overwrite: bool = False,
    ) -> None:
        """Stream a full copy of this database into the target database.

        With overwrite=True the target database is dropped and recreated
        first, so the copy always lands in an empty database."""

    @abstractmethod
    def clean_database(self) -> None:
        """Remove ALL user objects (tables, views, sequences, ...) from
        this database, leaving it empty. Destructive."""
