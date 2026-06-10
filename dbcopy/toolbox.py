"""Self-managed PostgreSQL client tools.

dbcopy does NOT require pg_dump / pg_restore / psql to be installed on the
machine. Tools are resolved in this order:

1. ``DBCOPY_PG_BIN`` environment variable (directory containing the tools)
2. A previously downloaded copy under ``~/.dbcopy/tools/``
3. The system PATH (an existing install is happily reused)
4. Auto-download of portable, self-contained binaries from
   https://github.com/theseus-rs/postgresql-binaries (cached for next time)

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
from pathlib import Path

#: Release version of theseus-rs/postgresql-binaries to download.
#: Newer pg_dump can dump older servers (back to 9.2), so one client
#: version serves every reasonable server version.
DEFAULT_PG_VERSION = "18.4.0"

_DOWNLOAD_BASE = "https://github.com/theseus-rs/postgresql-binaries/releases/download"

#: Memoized tool-name -> absolute-path lookups for this process.
_resolved: dict[str, str] = {}


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _cache_root() -> Path:
    home = os.environ.get("DBCOPY_HOME")
    root = Path(home) if home else Path.home() / ".dbcopy"
    return root / "tools"


def _pg_version() -> str:
    return os.environ.get("DBCOPY_PG_VERSION", DEFAULT_PG_VERSION)


def _platform_target() -> str:
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


def _status(msg: str, end: str = "\n") -> None:
    """Progress notes go to stderr so stdout stays clean for tooling."""
    print(msg, end=end, file=sys.stderr, flush=True)


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "dbcopy"})
    with urllib.request.urlopen(request) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1024 * 256):
            out.write(chunk)
            done += len(chunk)
            if total:
                _status(f"\r  downloading PostgreSQL tools... "
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


def managed_bin_dir() -> Path:
    """Where the auto-downloaded tools live (may not exist yet)."""
    return _cache_root() / f"postgresql-{_pg_version()}" / "bin"


def ensure_postgres_bin() -> Path:
    """Return the bin directory of the managed tools, downloading on first use."""
    bin_dir = managed_bin_dir()
    if (bin_dir / _exe("pg_dump")).exists():
        return bin_dir

    version = _pg_version()
    target = _platform_target()
    asset = f"postgresql-{version}-{target}.tar.gz"
    url = f"{_DOWNLOAD_BASE}/{version}/{asset}"
    install_dir = bin_dir.parent
    install_dir.parent.mkdir(parents=True, exist_ok=True)

    _status(f"PostgreSQL client tools not found — fetching portable binaries "
            f"({version}, {target}) into {install_dir} (one-time setup)")

    with tempfile.TemporaryDirectory(dir=install_dir.parent) as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset
        try:
            _download(url, archive)
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Failed to download PostgreSQL tools from {url}: {exc}. "
                "Check your internet connection, or install the tools manually "
                "and add them to PATH (or set DBCOPY_PG_BIN)."
            ) from exc
        _verify_sha256(archive, url)

        extract_dir = tmp_path / "extracted"
        with tarfile.open(archive) as tar:
            tar.extractall(extract_dir, filter="data")

        # The archive may or may not contain a top-level directory; locate
        # the tree that actually holds bin/pg_dump.
        marker = _exe("pg_dump")
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

    _status(f"PostgreSQL client tools ready: {bin_dir}")
    return bin_dir


def find_tool(name: str, *, auto_download: bool = True) -> str:
    """Absolute path to a client tool, provisioning it if necessary."""
    if name in _resolved:
        return _resolved[name]

    candidates = []
    override = os.environ.get("DBCOPY_PG_BIN")
    if override:
        candidates.append(Path(override) / _exe(name))
    candidates.append(managed_bin_dir() / _exe(name))

    for candidate in candidates:
        if candidate.exists():
            _resolved[name] = str(candidate)
            return _resolved[name]

    on_path = shutil.which(name)
    if on_path:
        _resolved[name] = on_path
        return on_path

    if not auto_download:
        raise RuntimeError(f"PostgreSQL client tool not found: {name}")

    path = ensure_postgres_bin() / _exe(name)
    if not path.exists():
        raise RuntimeError(f"Tool {name} missing from downloaded PostgreSQL binaries")
    _resolved[name] = str(path)
    return _resolved[name]


def ensure_tools(names: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Resolve every tool in `names`, downloading the bundle at most once."""
    return {name: find_tool(name) for name in names}
