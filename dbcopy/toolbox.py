"""Self-managed database client tools.

dbcopy does NOT require the native client tools (pg_dump / psql for
PostgreSQL, mysqldump / mysql for MySQL) to be installed on the machine.
Tools are resolved in this order:

1. A per-family override directory environment variable
   (``DBCOPY_PG_BIN``, ``DBCOPY_MYSQL_BIN``)
2. A previously downloaded copy under ``~/.dbcopy/tools/``
3. The system PATH (an existing install is happily reused)
4. Auto-download of portable, self-contained binaries (cached for next time):
   - PostgreSQL: https://github.com/theseus-rs/postgresql-binaries
   - MySQL Community archives: https://cdn.mysql.com

Adding another engine is just a new ``_ToolFamily`` entry below.

Stdlib only — no third-party packages.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
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

#: Newest theseus-rs release for each PostgreSQL major version.
#:
#: pg_dump writes SQL for its OWN version, so a dump taken with a client
#: newer than the destination server can be rejected outright — PG 17 added
#: `transaction_timeout`, which pg_dump 17+ always emits and PG 16 and older
#: refuse with "unrecognized configuration parameter". The Postgres adapter
#: therefore asks for a pg_dump matching the server it is dumping instead of
#: always using the newest one. Add new majors here as they are released.
PG_VERSIONS: dict[int, str] = {
    12: "12.20.0",
    13: "13.23.0",
    14: "14.24.0",
    15: "15.19.0",
    16: "16.15.0",
    17: "17.11.0",
    18: DEFAULT_PG_VERSION,
}

#: Release version of the MySQL Community archive to download. MySQL does not
#: publish a client-only bundle, so the full distribution is fetched and then
#: stripped down to the client programs on install (see _ToolFamily.prune).
DEFAULT_MYSQL_VERSION = "8.4.11"

#: macOS asset names embed the macOS release they were built on
#: ("mysql-8.4.11-macos15-arm64.tar.gz") and that moves between MySQL
#: releases, so it is pinned right next to the version above.
_MYSQL_MACOS_BUILD = "macos15"

_PG_DOWNLOAD_BASE = "https://github.com/theseus-rs/postgresql-binaries/releases/download"
#: MySQL assets are served straight from the CDN. The dev.mysql.com/get
#: redirector is bot-protected and 403s any non-browser User-Agent, so it is
#: deliberately not used. The CDN keeps the current release of a series under
#: /Downloads/ and moves it to /archives/ once a newer one ships, so a pinned
#: version has to be looked for in both (see _mysql_asset_url).
_MYSQL_DOWNLOAD_BASE = "https://cdn.mysql.com"

#: Memoized (tool name, version) -> absolute-path lookups for this process.
_resolved: dict[tuple[str, str], str] = {}


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


def _mysql_platform_token() -> str:
    """Map this machine to the platform part of a MySQL archive name.

    MySQL uses a naming scheme of its own (not the rust triple PostgreSQL
    ships under): assets are ``mysql-{version}-{token}.{ext}`` with tokens like
    ``winx64``, ``linux-glibc2.28-x86_64-minimal`` or ``macos15-arm64``. The
    slimmer ``-minimal`` build (no test suite / debug binaries) is published
    only for Linux x86_64; elsewhere the full archive is the only choice.
    ``DBCOPY_MYSQL_PLATFORM`` overrides the whole token.
    """
    override = os.environ.get("DBCOPY_MYSQL_PLATFORM")
    if override:
        return override

    system = platform.system()
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)

    if system == "Windows":
        # Only x86_64 is published; ARM64 Windows runs it via emulation.
        return "winx64"
    if system == "Darwin":
        # MySQL spells Apple silicon "arm64" and Intel "x86_64".
        return f"{_MYSQL_MACOS_BUILD}-{'arm64' if arch == 'aarch64' else 'x86_64'}"
    if system == "Linux" and arch:
        # No -minimal build is published for aarch64, so it pulls the full one.
        suffix = "-minimal" if arch == "x86_64" else ""
        return f"linux-glibc2.28-{arch}{suffix}"
    raise RuntimeError(
        f"No portable MySQL client binaries are available for {system}/{machine}. "
        "Install the MySQL client tools manually and either add them to "
        "PATH or point DBCOPY_MYSQL_BIN at their bin directory."
    )


# --------------------------------------------------------------------------
# Tool family descriptors
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Prune:
    """What to throw away from a downloaded distribution before installing.

    Only MySQL needs this so far: it publishes no client-only bundle, so the
    two programs dbcopy runs arrive wrapped in a 1.2 GB server distribution.
    """

    #: Top-level entries to keep; everything beside them is removed.
    keep_dirs: tuple[str, ...] = ()
    #: Glob patterns (relative to the distribution root) to delete.
    drop_globs: tuple[str, ...] = ()
    #: Delete programs in bin/ that are not one of the family's own tools.
    #: Shared libraries living in bin/ (Windows DLLs) are always kept.
    bin_tools_only: bool = False


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
    #: (version, token) -> download URL, or several to try in order.
    asset_url: Callable[[str, str], "str | tuple[str, ...]"]
    #: What to strip from the extracted archive; None installs it whole.
    prune: _Prune | None = None


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

def _mysql_asset_name(version: str, token: str) -> str:
    # Windows ships .zip, macOS .tar.gz, Linux .tar.xz (tarfile sniffs both).
    if token.startswith("win"):
        ext = "zip"
    elif token.startswith("macos"):
        ext = "tar.gz"
    else:
        ext = "tar.xz"
    return f"mysql-{version}-{token}.{ext}"


def _mysql_asset_url(version: str, token: str) -> tuple[str, ...]:
    """Both CDN locations a pinned MySQL version can live at, newest first.

    A release sits under ``/Downloads/MySQL-8.4/`` while it is the current one
    in its series and is moved to ``/archives/mysql-8.4/`` when superseded, so
    whichever we pin will eventually 404 at the first URL and be found at the
    second."""
    series = ".".join(version.split(".")[:2])
    asset = _mysql_asset_name(version, token)
    return (
        f"{_MYSQL_DOWNLOAD_BASE}/Downloads/MySQL-{series}/{asset}",
        f"{_MYSQL_DOWNLOAD_BASE}/archives/mysql-{series}/{asset}",
    )


_MYSQL = _ToolFamily(
    key="MySQL client tools",
    dirname="mysql",
    version_env="DBCOPY_MYSQL_VERSION",
    default_version=DEFAULT_MYSQL_VERSION,
    bin_env="DBCOPY_MYSQL_BIN",
    marker="mysqldump",
    tools=("mysqldump", "mysql"),
    platform_token=_mysql_platform_token,
    asset_name=_mysql_asset_name,
    asset_url=_mysql_asset_url,
    # The archive is a whole server distribution and unpacks to ~1.2 GB, of
    # which we need two programs and the libraries they link against. Pruning
    # brings the installed tree down to well under a tenth of that.
    prune=_Prune(
        # Drops docs, share/ (server error messages, SQL fixtures), include/.
        keep_dirs=("bin", "lib"),
        drop_globs=(
            "**/*.pdb",      # Windows debug symbols — mysqld.pdb alone is 577 MB
            "**/*-debug.*",  # debug builds shipped next to the release ones
            "**/debug",      # ... and their plugin directory
            "**/*.lib",      # import/static libs, only needed to compile against
            "**/*.a",
            "lib/mecab",     # server-side full-text dictionaries (130 MB)
        ),
        # Leaves mysql + mysqldump; drops mysqld, mysqlbinlog, ibd2sdi, ...
        bin_tools_only=True,
    ),
)

_TOOL_FAMILIES: tuple[_ToolFamily, ...] = (_PG, _MYSQL)


def pg_version_for_major(major: int) -> str:
    """Which PostgreSQL client release to use against a server of this major.

    An explicit ``DBCOPY_PG_VERSION`` always wins. An unknown major (a server
    older or newer than the table) falls back to the default pin, which either
    works or fails with pg_dump's own clear version-mismatch message."""
    override = os.environ.get(_PG.version_env)
    if override:
        return override
    return PG_VERSIONS.get(major, DEFAULT_PG_VERSION)


def _family_for_tool(name: str) -> _ToolFamily:
    for family in _TOOL_FAMILIES:
        if name in family.tools:
            return family
    raise RuntimeError(f"Unknown client tool: {name}")


def _family_version(family: _ToolFamily) -> str:
    return _env_version(family.version_env, family.default_version)


def _archive_kind(asset: str) -> str:
    """Infer the archive format from the asset filename extension.

    Anything that is not a .zip goes to tarfile, which sniffs the compression
    itself (.tar.gz and MySQL's .tar.xz both work)."""
    return "zip" if asset.endswith(".zip") else "tar"


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


def _remove(path: Path) -> None:
    """Delete a file or a whole directory."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _is_program(path: Path) -> bool:
    """Whether a file in bin/ is an executable rather than a shared library."""
    if os.name == "nt":
        return path.suffix.lower() == ".exe"
    if path.suffix == ".dylib" or ".so" in path.name:
        return False
    return os.access(path, os.X_OK)


def _prune(root: Path, family: _ToolFamily) -> None:
    """Strip the parts of a downloaded distribution we will never run."""
    policy = family.prune
    if policy is None:
        return

    if policy.keep_dirs:
        for entry in root.iterdir():
            if entry.name not in policy.keep_dirs:
                _remove(entry)

    for pattern in policy.drop_globs:
        for match in root.glob(pattern):
            if match.exists():  # an earlier pattern may have taken its parent
                _remove(match)

    if policy.bin_tools_only:
        wanted = {_exe(tool) for tool in family.tools}
        bin_dir = root / "bin"
        for entry in bin_dir.iterdir() if bin_dir.is_dir() else ():
            if entry.is_file() and _is_program(entry) and entry.name not in wanted:
                entry.unlink()


def _extract(archive: Path, dest: Path, kind: str) -> None:
    """Extract a downloaded archive, guarding against path traversal."""
    if kind == "tar":
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

def _managed_bin_dir(family: _ToolFamily, version: str | None = None) -> Path:
    """Where a family's auto-downloaded tools live (may not exist yet)."""
    version = version or _family_version(family)
    return _cache_root() / f"{family.dirname}-{version}" / "bin"


def _tool_major(path: str) -> int | None:
    """Major version reported by a client program, e.g. 16 for
    "pg_dump (PostgreSQL) 16.15". None if it cannot be determined."""
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)(?:\.\d+)*\s*$", (out.stdout or "").strip())
    return int(match.group(1)) if match else None


def _ensure_family_bin(family: _ToolFamily, version: str | None = None) -> Path:
    """Return the bin directory of a managed family, downloading on first use."""
    version = version or _family_version(family)
    bin_dir = _managed_bin_dir(family, version)
    if (bin_dir / _exe(family.marker)).exists():
        return bin_dir

    token = family.platform_token()
    asset = family.asset_name(version, token)
    urls = family.asset_url(version, token)
    candidates = (urls,) if isinstance(urls, str) else tuple(urls)
    install_dir = bin_dir.parent
    install_dir.parent.mkdir(parents=True, exist_ok=True)

    _status(f"{family.key} not found - fetching portable binaries "
            f"({version}, {token}) into {install_dir} (one-time setup)")

    with tempfile.TemporaryDirectory(dir=install_dir.parent) as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset
        # urlopen() is evaluated before the output file is opened, so a failed
        # candidate leaves no partial archive behind and the next can be tried.
        last_error: Exception | None = None
        for url in candidates:
            try:
                _download(url, archive, f"{family.key}")
                break
            except urllib.error.URLError as exc:
                last_error = exc
        else:
            tried = "\n  ".join(candidates)
            raise RuntimeError(
                f"Failed to download {family.key} ({last_error}). Tried:"
                f"\n  {tried}\n"
                "Check your internet connection, or install the tools manually "
                f"and add them to PATH (or set {family.bin_env})."
            ) from last_error
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

        _prune(root, family)

        if install_dir.exists():
            shutil.rmtree(install_dir)
        # tmp lives next to install_dir, so this is a cheap same-volume move.
        shutil.move(str(root), str(install_dir))

    _status(f"{family.key} ready: {bin_dir}")
    return bin_dir


def find_tool(name: str, *, version: str | None = None,
              auto_download: bool = True) -> str:
    """Absolute path to a client tool, provisioning it if necessary.

    `version` pins which release of the family to use — needed for PostgreSQL,
    where the dump has to be produced by a client matching the server (see
    PG_VERSIONS). When it is given, a tool found on PATH is only accepted if
    its major version actually matches; otherwise the right one is fetched."""
    family = _family_for_tool(name)
    wanted = version or _family_version(family)
    key = (name, wanted)
    if key in _resolved:
        return _resolved[key]

    candidates = []
    override = os.environ.get(family.bin_env)
    if override:
        candidates.append(Path(override) / _exe(name))
    candidates.append(_managed_bin_dir(family, wanted) / _exe(name))

    for candidate in candidates:
        if candidate.exists():
            _resolved[key] = str(candidate)
            return _resolved[key]

    on_path = shutil.which(name)
    if on_path and (version is None
                    or _tool_major(on_path) == int(wanted.split(".")[0])):
        _resolved[key] = on_path
        return on_path

    if not auto_download:
        raise RuntimeError(f"{family.key} tool not found: {name}")

    path = _ensure_family_bin(family, wanted) / _exe(name)
    if not path.exists():
        raise RuntimeError(f"Tool {name} missing from downloaded {family.key}")
    _resolved[key] = str(path)
    return _resolved[key]


def ensure_tools(names: tuple[str, ...] | list[str],
                 *, version: str | None = None) -> dict[str, str]:
    """Resolve every tool in `names`, downloading the bundle at most once."""
    return {name: find_tool(name, version=version) for name in names}


# --------------------------------------------------------------------------
# Back-compat shims (the PostgreSQL adapter calls these directly)
# --------------------------------------------------------------------------

def managed_bin_dir() -> Path:
    """Where the auto-downloaded PostgreSQL tools live (may not exist yet)."""
    return _managed_bin_dir(_PG)


def ensure_postgres_bin() -> Path:
    """Return the bin directory of the managed PostgreSQL tools."""
    return _ensure_family_bin(_PG)
