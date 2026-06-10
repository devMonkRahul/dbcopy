"""Adapter registry.

get_adapter() picks the right adapter from the URL scheme, so the rest
of the code never needs to know which database it is talking to.

Adding MySQL later is just:

    from .mysql import MySQLAdapter
    ADAPTERS.append(MySQLAdapter)
"""

from __future__ import annotations

from .base import DatabaseAdapter
from .postgres import PostgresAdapter

ADAPTERS: list[type[DatabaseAdapter]] = [
    PostgresAdapter,
    # MySQLAdapter,   # future
    # MongoAdapter,   # future
]


def get_adapter(url: str) -> DatabaseAdapter:
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    for adapter_cls in ADAPTERS:
        if scheme in adapter_cls.schemes:
            return adapter_cls(url)
    supported = ", ".join(s for a in ADAPTERS for s in a.schemes)
    raise ValueError(
        f"Unsupported database URL scheme '{scheme}'. Supported: {supported}"
    )
