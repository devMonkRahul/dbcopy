"""MongoDB copy engine.

The engine itself lives in :mod:`.copier` and the HTTP layer in
:mod:`.routes`; neither is imported here, so ``import dbcopy.engines.mongo``
stays stdlib-only and costs nothing. Callers that need the copier import it
explicitly — that keeps pymongo off the import path of every PostgreSQL and
MySQL run.

What is here is the small amount of URL knowledge the rest of dbcopy needs to
route a ``mongodb://`` URL to this engine without connecting to anything.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

#: URL schemes handled by this engine.
SCHEMES = ("mongodb", "mongodb+srv")

#: Port MongoDB listens on when the URL does not say.
DEFAULT_PORT = 27017


def is_mongo_url(url: str) -> bool:
    """True if `url` addresses MongoDB. Cheap — no connection, no DNS."""
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    return scheme in SCHEMES


def database_in_url(url: str) -> str:
    """The database named in the URL path, or "" when the path is empty.

    A MongoDB URL may legitimately carry no database (``mongodb://host/``),
    which is why this returns "" rather than raising: the caller decides
    whether a missing name is an error or something it can default.
    """
    path = (urlsplit(url).path or "").lstrip("/")
    return unquote(path.split("/")[0])


def endpoint_of(url: str) -> str:
    """``host:port`` for display, with any credentials stripped.

    Parses the netloc by hand rather than via ``urlsplit().port``: a
    comma-separated seed list (``h1:27017,h2:27017``) would trip that
    property's integer cast, and ``mongodb+srv`` URLs carry no port at all.
    Only the first host is reported — this is a label, not a connection.
    """
    authority = urlsplit(url).netloc.rpartition("@")[2]
    first = authority.split(",")[0]
    if first.startswith("["):  # IPv6 literal, e.g. [::1]:27017
        host, _, rest = first.partition("]")
        host, port_s = host[1:], rest.lstrip(":")
    else:
        host, _, port_s = first.partition(":")
    if not host:
        host = "localhost"
    try:
        port = int(port_s) if port_s else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return f"{unquote(host)}:{port}"
