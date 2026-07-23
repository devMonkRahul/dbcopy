"""Self-managed database client tools.

dbcopy does NOT require the native client tools (pg_dump / psql for
PostgreSQL, mongodump / mongorestore for MongoDB) to be installed on the
machine. Tools are resolved in this order:

1. A per-family override directory environment variable
   (``DBCOPY_PG_BIN`` for PostgreSQL, ``DBCOPY_MONGO_BIN`` for MongoDB)
2. A previously downloaded copy under ``~/.dbcopy/tools/``
3. The system PATH (an existing install is happily reused)
4. Auto-download of portable, self-contained binaries (cached for next time):
   - PostgreSQL: https://github.com/theseus-rs/postgresql-binaries
   - MongoDB Database Tools: https://fastdl.mongodb.org/tools/db

Adding another engine is just a new ``_ToolFamily`` entry below.

Stdlib only — no third-party packages.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: Release version of theseus-rs/postgresql-binaries to download.
#: Newer pg_dump can dump older servers (back to 9.2), so one client
#: version serves every reasonable server version.
DEFAULT_PG_VERSION = "18.4.0"

#: Release version of the MongoDB Database Tools bundle to download.
DEFAULT_MONGO_TOOLS_VERSION = "100.10.0"

_PG_DOWNLOAD_BASE = "https://github.com/theseus-rs/postgresql-binaries/releases/download"
_MONGO_DOWNLOAD_BASE = "https://fastdl.mongodb.org/tools/db"

#: Memoized tool-name -> absolute-path lookups for this process.
_resolved: dict[str, str] = {}


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _cache_root() -> Path:
    home = os.environ.get("DBCOPY_HOME")
    root = Path(home) if home else Path.home() / ".dbcopy"
    return root / "tools"


def _env_version(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


# --------------------------------------------------------------------------
# Per-platform download-token mappers (one per family — naming differs wildly)
# --------------------------------------------------------------------------

def _pg_platform_token() -> str:
    """Map this machine to a release target triple of postgresql-binaries."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        # ARM64 Windows runs x64 binaries via emulation; only x64 is published.
        return "x86_64-pc-windows-msvc"
    if system == "Darwin":
        return "aarch64-apple-darwin" if machine == "arm64" else "x86_64-apple-darwin"
    if system == "Linux":
        arch = {"x86_64": "x86_64", "amd64": "x86_64",
                "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
        if arch:
            return f"{arch}-unknown-linux-gnu"
    raise RuntimeError(
        f"No portable PostgreSQL binaries are available for {system}/{machine}. "
        "Install the PostgreSQL client tools manually and either add them to "
        "PATH or point DBCOPY_PG_BIN at their bin directory."
    )


def _mongo_platform_token() -> str:
    """Map this machine to a MongoDB Database Tools release token.

    MongoDB names assets by OS (and Linux distro), not by rust triple:
    ``windows-x86_64``, ``macos-arm64``, ``ubuntu2204-x86_64`` ... There is
    no universal Linux build, so the distro defaults to ``ubuntu2204`` (glibc,
    broadly compatible) and can be overridden with ``DBCOPY_MONGO_PLATFORM``
    (e.g. ``rhel80``, ``amazon2023``, ``debian12``) for other distros.
    """
    system = platform.system()
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "arm64", "arm64": "arm64"}.get(machine)

    if system == "Windows":
        # Only x86_64 is published; ARM64 Windows runs it via emulation.
        return "windows-x86_64"
    if system == "Darwin":
        return f"macos-{arch or 'x86_64'}"
    if system == "Linux" and arch:
        distro = os.environ.get("DBCOPY_MONGO_PLATFORM", "ubuntu2204")
        return f"{distro}-{arch}"
    raise RuntimeError(
        f"No portable MongoDB Database Tools are available for {system}/{machine}. "
        "Install the MongoDB Database Tools manually and either add them to "
        "PATH or point DBCOPY_MONGO_BIN at their bin directory."
    )


# --------------------------------------------------------------------------
# Tool family descriptors
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _ToolFamily:
    """Everything needed to self-provision one bundle of client tools."""

    key: str                       # human label for messages
    dirname: str                   # cache subdir prefix: "<dirname>-<version>"
    version_env: str               # env var overriding the pinned version
    default_version: str
    bin_env: str                   # env var pointing at an override bin dir
    marker: str                    # sentinel tool that proves the bundle is present
    tools: tuple[str, ...]         # every tool name this family provides
    platform_token: Callable[[], str]
    asset_name: Callable[[str, str], str]   # (version, token) -> archive filename
    asset_url: Callable[[str, str], str]    # (version, token) -> full download URL


def _mongo_asset_name(version: str, token: str) -> str:
    # Windows/macOS ship .zip, Linux ships .tgz.
    ext = "zip" if token.startswith(("windows", "macos")) else "tgz"
    return f"mongodb-database-tools-{token}-{version}.{ext}"


_PG = _ToolFamily(
    key="PostgreSQL",
    dirname="postgresql",
    version_env="DBCOPY_PG_VERSION",
    default_version=DEFAULT_PG_VERSION,
    bin_env="DBCOPY_PG_BIN",
    marker="pg_dump",
    tools=("pg_dump", "pg_restore", "psql"),
    platform_token=_pg_platform_token,
    asset_name=lambda version, token: f"postgresql-{version}-{token}.tar.gz",
    # theseus-rs layout: <base>/<version>/<asset>
    asset_url=lambda version, token: (
        f"{_PG_DOWNLOAD_BASE}/{version}/postgresql-{version}-{token}.tar.gz"
    ),
)

_MONGO_TOOLS = _ToolFamily(
    key="MongoDB Database Tools",
    dirname="mongodb-database-tools",
    version_env="DBCOPY_MONGO_VERSION",
    default_version=DEFAULT_MONGO_TOOLS_VERSION,
    bin_env="DBCOPY_MONGO_BIN",
    marker="mongodump",
    tools=("mongodump", "mongorestore"),
    platform_token=_mongo_platform_token,
    asset_name=_mongo_asset_name,
    # fastdl layout: <base>/<asset>
    asset_url=lambda version, token: f"{_MONGO_DOWNLOAD_BASE}/{_mongo_asset_name(version, token)}",
)

_TOOL_FAMILIES: tuple[_ToolFamily, ...] = (_PG, _MONGO_TOOLS)


def _family_for_tool(name: str) -> _ToolFamily:
    for family in _TOOL_FAMILIES:
        if name in family.tools:
            return family
    raise RuntimeError(f"Unknown client tool: {name}")


def _family_version(family: _ToolFamily) -> str:
    return _env_version(family.version_env, family.default_version)


def _archive_kind(asset: str) -> str:
    """Infer the archive format from the asset filename extension."""
    return "zip" if asset.endswith(".zip") else "tar.gz"


# --------------------------------------------------------------------------
# Download / verify / extract (generic)
# --------------------------------------------------------------------------

def _status(msg: str, end: str = "\n") -> None:
    """Progress notes go to stderr so stdout stays clean for tooling."""
    print(msg, end=end, file=sys.stderr, flush=True)


def _download(url: str, dest: Path, label: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "dbcopy"})
    with urllib.request.urlopen(request) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1024 * 256):
            out.write(chunk)
            done += len(chunk)
            if total:
                _status(f"\r  downloading {label}... "
                        f"{done * 100 // total}% of {total // (1024 * 1024)} MB", end="")
        _status("")


def _verify_sha256(archive: Path, url: str) -> None:
    """Check the archive against the published .sha256 file (best effort)."""
    try:
        request = urllib.request.Request(url + ".sha256", headers={"User-Agent": "dbcopy"})
        with urllib.request.urlopen(request) as resp:
            text = resp.read().decode()
    except urllib.error.URLError:
        return  # checksum file unavailable; skip verification
    # Format varies by platform (plain "hash  filename" vs CertUtil's
    # multi-line output) — find the first 64-char hex token.
    match = re.search(r"\b[0-9a-fA-F]{64}\b", text)
    if match is None:
        return
    expected = match.group(0).lower()
    digest = hashlib.sha256()
    with open(archive, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected:
        raise RuntimeError(f"Checksum mismatch for downloaded archive {archive.name}")


def _extract(archive: Path, dest: Path, kind: str) -> None:
    """Extract a downloaded archive, guarding against path traversal."""
    if kind == "tar.gz":
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")  # filter="data" needs Py 3.12+
        return
    # zip: zipfile has no `filter="data"`, so validate members ourselves and
    # restore the exec bit (a .zip drops Unix permissions, so macOS binaries
    # would come out non-executable).
    dest_root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest_root)):
                raise RuntimeError(f"Unsafe path in archive: {member.filename}")
            zf.extract(member, dest)
            if os.name != "nt" and not member.is_dir():
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(dest / member.filename, mode)


# --------------------------------------------------------------------------
# Resolution / provisioning (family-parameterized)
# --------------------------------------------------------------------------

def _managed_bin_dir(family: _ToolFamily) -> Path:
    """Where a family's auto-downloaded tools live (may not exist yet)."""
    return _cache_root() / f"{family.dirname}-{_family_version(family)}" / "bin"


def _ensure_family_bin(family: _ToolFamily) -> Path:
    """Return the bin directory of a managed family, downloading on first use."""
    bin_dir = _managed_bin_dir(family)
    if (bin_dir / _exe(family.marker)).exists():
        return bin_dir

    version = _family_version(family)
    token = family.platform_token()
    asset = family.asset_name(version, token)
    url = family.asset_url(version, token)
    install_dir = bin_dir.parent
    install_dir.parent.mkdir(parents=True, exist_ok=True)

    _status(f"{family.key} not found — fetching portable binaries "
            f"({version}, {token}) into {install_dir} (one-time setup)")

    with tempfile.TemporaryDirectory(dir=install_dir.parent) as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset
        try:
            _download(url, archive, f"{family.key}")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Failed to download {family.key} from {url}: {exc}. "
                "Check your internet connection, or install the tools manually "
                f"and add them to PATH (or set {family.bin_env})."
            ) from exc
        _verify_sha256(archive, url)

        extract_dir = tmp_path / "extracted"
        _extract(archive, extract_dir, _archive_kind(asset))

        # The archive may or may not contain a top-level directory; locate
        # the tree that actually holds bin/<marker>.
        marker = _exe(family.marker)
        root = next(
            (p.parent.parent for p in extract_dir.rglob(marker) if p.parent.name == "bin"),
            None,
        )
        if root is None:
            raise RuntimeError(f"Downloaded archive {asset} did not contain bin/{marker}")

        if install_dir.exists():
            shutil.rmtree(install_dir)
        # tmp lives next to install_dir, so this is a cheap same-volume move.
        shutil.move(str(root), str(install_dir))

    _status(f"{family.key} ready: {bin_dir}")
    return bin_dir


def find_tool(name: str, *, auto_download: bool = True) -> str:
    """Absolute path to a client tool, provisioning it if necessary."""
    if name in _resolved:
        return _resolved[name]

    family = _family_for_tool(name)

    candidates = []
    override = os.environ.get(family.bin_env)
    if override:
        candidates.append(Path(override) / _exe(name))
    candidates.append(_managed_bin_dir(family) / _exe(name))

    for candidate in candidates:
        if candidate.exists():
            _resolved[name] = str(candidate)
            return _resolved[name]

    on_path = shutil.which(name)
    if on_path:
        _resolved[name] = on_path
        return on_path

    if not auto_download:
        raise RuntimeError(f"{family.key} tool not found: {name}")

    path = _ensure_family_bin(family) / _exe(name)
    if not path.exists():
        raise RuntimeError(f"Tool {name} missing from downloaded {family.key}")
    _resolved[name] = str(path)
    return _resolved[name]


def ensure_tools(names: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Resolve every tool in `names`, downloading the bundle at most once."""
    return {name: find_tool(name) for name in names}


# --------------------------------------------------------------------------
# Back-compat shims (the PostgreSQL adapter calls these directly)
# --------------------------------------------------------------------------

def managed_bin_dir() -> Path:
    """Where the auto-downloaded PostgreSQL tools live (may not exist yet)."""
    return _managed_bin_dir(_PG)


def ensure_postgres_bin() -> Path:
    """Return the bin directory of the managed PostgreSQL tools."""
    return _ensure_family_bin(_PG)
