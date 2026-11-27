"""Whether an agent is connected, what it has read and whether it is working,
so the editor can tell the human what happens to a message they sent.

Two kinds of local files in .archplan/agents/, never committed:

- m-<pid>-<nonce>.json, one per archplan MCP server (one per Claude Code
  session). It holds a heartbeat, whether the agent waits in
  wait_for_comments, and for each thread how many messages the agent was
  shown. The file exists while the server runs.
- s-<session>.json, one per Claude Code session, written by the plugin's
  hooks: busy from a prompt until the turn stops.

The files are repository data to the editor server: a cloned repository can
hold planted ones, so every read is bounded and typed. A planted file can only
make the editor show a wrong state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path

from .project import Project, write_json

DIR = "agents"
BEAT = 10.0       # seconds between heartbeats
STALE = 35.0      # an MCP file whose beat is older is a stopped server
BUSY_MAX = 1800.0  # a busy flag older than this is a turn whose Stop hook did not run
CLEAN = 86400.0   # files older than this are removed
MAX_FILES = 64
MAX_BYTES = 64 << 10
MAX_SEEN = 500
SKEW = 60.0
MCP_NAME = re.compile(r"m-([0-9]{1,10})-[0-9a-f]{8}\.json")
SES_NAME = re.compile(r"s-([A-Za-z0-9_-]{1,64})\.json")


def agents_dir(p: Project) -> Path | None:
    """The agents directory, created when missing; None when it is not a plain
    directory (a planted symlink)."""
    d = p.path(DIR)
    try:
        os.mkdir(d)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        return d if stat.S_ISDIR(os.lstat(d).st_mode) else None
    except OSError:
        return None


def _read(path: Path) -> dict | None:
    from .safeio import read_bytes
    try:
        d = json.loads(read_bytes(path, MAX_BYTES))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # no cheap check; the heartbeat decides
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def _clean(d: Path, now: float) -> None:
    """Remove files of ours that nothing updated for a day."""
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names[:1000]:
        if not (MCP_NAME.fullmatch(n) or SES_NAME.fullmatch(n)):
            continue
        f = d / n
        try:
            st = os.lstat(f)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > CLEAN:
                os.unlink(f)
        except OSError:
            pass


class Presence:
    """The MCP server's own file. One state behind a lock; the heartbeat
    thread and the request loop both write it from that state."""

    def __init__(self, p: Project):
        self.p, self.lock = p, threading.Lock()
        self.state = {"pid": os.getpid(), "started": time.time(), "beat": time.time(),
                      "listening": False, "seen": {}}
        self.dir = agents_dir(p)
        self.file = self.dir / f"m-{os.getpid()}-{secrets.token_hex(4)}.json" if self.dir else None
        self.stop = threading.Event()
        if not self.file:
            return
        from .project import append_ignore
        try:
            append_ignore(p, DIR + "/")
        except OSError:
            pass
        _clean(self.dir, time.time())
        self._write()
        threading.Thread(target=self._beat, name="archplan-presence", daemon=True).start()

    def _write(self) -> None:
        if not self.file or self.stop.is_set():
            return
        with self.lock:
            self.state["beat"] = time.time()
            try:
                write_json(self.file, self.state)
            except OSError:
                pass  # Windows: the editor may hold the file open; the next beat retries

    def _beat(self) -> None:
        while not self.stop.wait(BEAT):
            try:
                self._write()
            except Exception:  # never let the heartbeat die: a live agent would look gone
                pass

    def listening(self, on: bool) -> None:
        with self.lock:
            self.state["listening"] = on
        self._write()

    def saw(self, counts: dict) -> None:
        """The agent was shown these threads with this many messages each."""
        if not counts:
            return
        with self.lock:
            seen = self.state["seen"]
            for k, n in counts.items():
                seen.pop(k, None)
                seen[k] = max(n, 0)
            while len(seen) > MAX_SEEN:
                seen.pop(next(iter(seen)))
        self._write()

    def close(self) -> None:
        self.stop.set()
        if self.file:
            try:
                os.unlink(self.file)
            except OSError:
                pass


def delivered(tool: str, out, limit: int = 40000) -> dict:
    """Thread id -> message count for the threads a tool result showed the
    agent with their latest message. A long list may be cut by the client
    before the model sees it, so lists count only when the result is short."""
    def one(s):
        if isinstance(s, dict) and isinstance(s.get("id"), str) and isinstance(s.get("messages"), int):
            return {s["id"]: s["messages"]}
        return {}
    if tool == "read_thread" and isinstance(out, dict) and isinstance(out.get("messages"), list):
        return {out["id"]: len(out["messages"])} if isinstance(out.get("id"), str) else {}
    if tool in ("list_threads", "wait_for_comments", "status"):
        items = out.get("threads", {}).get("waiting_on_agent", []) if tool == "status" and isinstance(out, dict) else out
        if not isinstance(items, list) or len(json.dumps(out, default=str)) > limit:
            return {}
        res = {}
        for s in items:
            res.update(one(s))
        return res
    return {}


def turn(p: Project, session: str, event: str, shown: dict | None = None) -> None:
    """Record a hook event for one Claude Code session: busy on a prompt,
    idle when the turn stops, gone when the session ends. shown holds the
    threads the hook just put into the agent's context."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session or "")[:64]
    d = agents_dir(p) if sid else None
    if not d:
        return
    f = d / f"s-{sid}.json"
    if event == "SessionEnd":
        try:
            os.unlink(f)
        except OSError:
            pass
        return
    old = _read(f) or {}
    seen = old.get("seen") if isinstance(old.get("seen"), dict) else {}
    seen.update(shown or {})
    while len(seen) > MAX_SEEN:
        seen.pop(next(iter(seen)))
    busy = event == "UserPromptSubmit" or (event != "Stop" and bool(old.get("busy")))
    try:
        write_json(f, {"busy": busy, "at": time.time(), "seen": seen})
    except OSError:
        pass


class Summary:
    """What the editor server reports about agents, read at most every
    `every` seconds because the long poll asks about three times a second."""

    def __init__(self, p: Project, every: float = 2.0):
        self.p, self.every, self.at, self.val = p, every, -1e9, None
        self.lock = threading.Lock()

    def __call__(self) -> dict:
        with self.lock:
            if time.monotonic() - self.at >= self.every:
                self.val, self.at = summarize(self.p.path(DIR), time.time()), time.monotonic()
            return self.val


def summarize(d: Path, now: float) -> dict:
    """{"connected": bool, "listening": bool, "busy": bool, "seen": {thread: n},
    "key": short hash}. seen merges live MCP servers and session hooks."""
    out = {"connected": False, "listening": False, "busy": False, "seen": {}}
    try:
        st = os.lstat(d)
        names = os.listdir(d) if stat.S_ISDIR(st.st_mode) else []
    except OSError:
        names = []
    mcp, ses = [], []
    for n in names:
        if MCP_NAME.fullmatch(n):
            mcp.append(n)
        elif SES_NAME.fullmatch(n):
            ses.append(n)

    def newest(ns):
        def mt(n):
            try:
                return os.lstat(d / n).st_mtime
            except OSError:
                return 0
        return sorted(ns, key=mt, reverse=True)[:MAX_FILES]

    def merge(seen):
        if not isinstance(seen, dict):
            return
        for k, v in list(seen.items())[:MAX_SEEN]:
            if isinstance(k, str) and len(k) <= 64 and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out["seen"][k] = max(out["seen"].get(k, 0), v)

    for n in newest(mcp):
        f = _read(d / n)
        beat = _num(f.get("beat")) if f else None
        if beat is None or not (now - STALE <= beat <= now + SKEW):
            continue
        if not _pid_alive(int(MCP_NAME.fullmatch(n).group(1))):
            continue
        out["connected"] = True
        out["listening"] |= f.get("listening") is True
        merge(f.get("seen"))
    if out["connected"]:
        for n in newest(ses):
            f = _read(d / n)
            at = _num(f.get("at")) if f else None
            if at is None or at > now + SKEW or now - at > CLEAN:
                continue
            out["busy"] |= f.get("busy") is True and now - at < BUSY_MAX
            merge(f.get("seen"))
    out["key"] = hashlib.sha1(json.dumps(out, sort_keys=True).encode()).hexdigest()[:10]
    return out
