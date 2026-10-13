"""Notice when archplan's own code changes on disk.

A build id names the running code: the version plus a hash of the package's
.py files and static/ files (names, sizes and modification times). The editor
server compares it with the code on disk and restarts itself when the code
changed (a `git pull` in a checkout). `archplan up` compares it with a running
server and replaces an older one. The MCP server only reports newer code.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__

PKG = Path(__file__).resolve().parent
INTERVAL = 2.0  # seconds between checks
STABLE = 2.0  # seconds the files must stay unchanged before a restart


def fingerprint(pkg: Path = PKG) -> str:
    """A hash of the package's .py files and static/ files: names, sizes, mtimes."""
    h = hashlib.sha1()
    for d, dirs, files in os.walk(pkg):
        dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", "node_modules"))
        static = Path(d) == pkg / "static" or pkg / "static" in Path(d).parents
        for f in sorted(files):
            if not (static or f.endswith(".py")):
                continue
            path = os.path.join(d, f)
            try:
                st = os.stat(path)
            except OSError:
                continue
            h.update(f"{os.path.relpath(path, pkg)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:12]


def build_id(pkg: Path = PKG, version: str = __version__) -> str:
    return f"{version}+{fingerprint(pkg)}"


BUILD = build_id()  # the code this process loaded


def disk_version(pkg: Path = PKG) -> str | None:
    """The version written in the package's __init__.py on disk."""
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', (pkg / "__init__.py").read_text())
    except OSError:
        return None
    return m and m.group(1)


def disabled() -> bool:
    return os.environ.get("ARCHPLAN_NO_RELOAD", "").strip() not in ("", "0")


def imports_cleanly(pkg: Path = PKG, timeout: float = 60) -> bool:
    """Whether the code on disk imports in a fresh interpreter with this
    process's sys.path, so a half-written checkout never replaces working code."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(pkg.parent)] + [x for x in sys.path if x]))
    try:
        r = subprocess.run([sys.executable, "-c", "import archplan.server, archplan.cli, archplan.mcp"],
                           env=env, cwd=str(pkg.parent), capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def watch(on_change: Callable[[str], None], pkg: Path = PKG, interval: float = INTERVAL,
          stable: float = STABLE, log=None, stop: threading.Event | None = None, start: str | None = None) -> None:
    """Call on_change(new fingerprint) once the package differs from `start`
    (default: the code this process loaded), stayed the same for `stable`
    seconds, and imports cleanly. A version that fails to import is logged
    once and skipped until the files change again."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    stop = stop or threading.Event()
    start = start or (BUILD.rsplit("+", 1)[1] if pkg == PKG else fingerprint(pkg))
    last = fingerprint(pkg)
    since, failed = time.monotonic(), None
    while not stop.wait(interval):
        fp = fingerprint(pkg)
        if fp != last:
            last, since = fp, time.monotonic()
            continue
        if fp == start or fp == failed or time.monotonic() - since < stable:
            continue
        if imports_cleanly(pkg):
            return on_change(fp)
        failed = fp
        log(f"archplan: the code in {pkg} changed but does not import; keeping the running version")


def restart_argv(port: int) -> list[str]:
    """This process's command line, for os.execv, with --port fixed to `port`."""
    args = list(sys.argv[1:])
    out = []
    skip = False
    for x in args:
        if skip:
            skip = False
            continue
        if x == "--port":
            skip = True
            continue
        if x.startswith("--port="):
            continue
        out.append(x)
    head = [sys.executable, "-m", "archplan"] if Path(sys.argv[0]).name == "__main__.py" else [sys.executable, sys.argv[0]]
    return head + out + ["--port", str(port)]


def _version_key(v: str):
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return None


def newer_on_disk(pkg: Path = PKG, version: str = __version__, build: str = BUILD) -> str | None:
    """The version of newer archplan code on disk than this process runs, or None.
    Two cases: the checkout this process runs from changed (git pull), or this
    is a plugin cache folder named after its version and a newer sibling folder
    exists (a plugin update)."""
    root = pkg.parent.parent
    if root.name == version and _version_key(version):
        best = None
        try:
            for d in root.parent.iterdir():
                k = _version_key(d.name)
                if k and k > _version_key(version) and (d / "src" / "archplan" / "__init__.py").exists():
                    if best is None or k > _version_key(best):
                        best = d.name
        except OSError:
            pass
        if best:
            return best
    if pkg.is_dir() and build_id(pkg, version) != build:
        return disk_version(pkg) or version
    return None
