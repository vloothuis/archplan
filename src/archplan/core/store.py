"""Comment threads in .archplan/threads.json, shared by the editor server and
the MCP server. Writes take a file lock, so both may run at once.

Messages may mention model items as @[Label](anchor). The editor writes them;
mentions() turns them into plain text for the agent."""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .project import MAX_JSON, write_json
from .safeio import read_bytes


class NotFound(KeyError):
    """No thread, suggestion or handover with that id."""

    def __str__(self):
        return str(self.args[0]) if self.args else "not found"


class Conflict(ValueError):
    """A change the item's current state does not allow, such as unlocking a
    refactor the agent is working on, or deciding a suggestion twice."""


SNAPSHOT_MAX = 64 * 1024  # bytes of JSON a thread's graph snapshot may take


class JsonStore:
    """A JSON file with a version counter that every write bumps. Writes hold a
    lock file and replace the file atomically."""
    name = "threads"
    empty = {"threads": []}

    def __init__(self, dir: Path):
        self.dir = dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = dir / f"{self.name}.json"
        self.lock_path = dir / f".{self.name}.lock"

    @contextmanager
    def locked(self):
        # O_NOFOLLOW and O_NONBLOCK: a lock file planted by the repository as a
        # symlink or a FIFO fails instead of redirecting or hanging.
        fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
        with os.fdopen(fd, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def read(self) -> dict:
        try:
            return json.loads(read_bytes(self.path, MAX_JSON))
        except FileNotFoundError:
            return {"version": 0, **json.loads(json.dumps(self.empty))}

    def version(self) -> int:
        return self.read().get("version", 0)

    def update(self, fn):
        """Apply fn to the stored data under the lock and return its result."""
        with self.locked():
            data = self.read()
            out = fn(data)
            data["version"] = data.get("version", 0) + 1
            write_json(self.path, data)
            return out


class Store(JsonStore):
    def create(self, anchor: str, label: str, text: str, author: str,
               kind: str | None = None, options: list | None = None, snapshot: dict | None = None) -> dict:
        now = stamp()
        th = {"id": "th-" + uuid.uuid4().hex[:8], "anchor": anchor, "label": label or anchor,
              "status": "open", "created": now, "updated": now,
              "messages": [message(author, text, now, kind, options)]}
        if snapshot is not None:
            if not isinstance(snapshot, dict):
                raise ValueError("snapshot must be an object")
            if len(json.dumps(snapshot)) > SNAPSHOT_MAX:
                raise ValueError(f"snapshot larger than {SNAPSHOT_MAX // 1024} KB")
            th["snapshot"] = snapshot
        self.update(lambda d: d["threads"].append(th))
        return th

    def reply(self, tid: str, text: str, author: str, status: str | None = None,
              kind: str | None = None, options: list | None = None) -> dict:
        if status not in (None, "open", "resolved"):
            raise ValueError(f"bad status {status!r}")

        def fn(d):
            th = find(d, tid)
            now = stamp()
            if text:
                th["messages"].append(message(author, text, now, kind, options))
            if th["status"] == "planned":
                # A reply keeps a planned refactor planned. Only a finished one may be resolved;
                # unlock() reopens it.
                if status == "resolved" and th.get("refactor", {}).get("state") == "done":
                    th["status"] = status
            elif status:
                th["status"] = status
            th["updated"] = now
            return th
        return self.update(fn)

    def merge(self, threads: list) -> None:
        """Add threads the store does not have yet (the editor's local ones)."""
        def fn(d):
            have = {t["id"] for t in d["threads"]}
            d["threads"].extend(t for t in threads if t.get("id") not in have)
        self.update(fn)


def wait(key, since, timeout: float, step: float = 0.3):
    """Block until key() differs from since, or timeout passes; return key()."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        k = key()
        if k != since:
            return k
        time.sleep(step)
    return key()


def find(d: dict, tid: str) -> dict:
    for th in d["threads"]:
        if th["id"] == tid:
            return th
    raise NotFound(f"no thread {tid}")


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def message(author: str, text: str, at: str, kind: str | None, options: list | None) -> dict:
    """An agent message is feedback (the human only needs to read it) or a
    decision (the agent is blocked until the human chooses, optionally from options)."""
    m = {"author": author, "text": text, "at": at}
    if author == "agent":
        m["kind"] = "decision" if kind == "decision" else "feedback"
        if m["kind"] == "decision" and options:
            m["options"] = [str(o) for o in options]
    return m


def who_next(th: dict, active: frozenset = frozenset()) -> str:
    """Who should answer next: the agent when the human spoke last. A planned
    refactor waits on nobody until it is part of an active handover (active
    holds the thread ids of that handover)."""
    if th["status"] != "open" and not (th["status"] == "planned" and th["id"] in active):
        return "nobody"
    return "human" if th["messages"][-1]["author"] == "agent" else "agent"


def needs(th: dict, active: frozenset = frozenset()) -> str | None:
    """What the human owes a thread the agent spoke last in: 'decision' or 'feedback'."""
    if who_next(th, active) != "human":
        return None
    return th["messages"][-1].get("kind", "feedback")


MENTION = re.compile(r"@\[([^\]\n]*)\]\(([^)\s]+)\)")


def mentions(text: str) -> str:
    """Write each @[Label](anchor) mention as @Label (anchor)."""
    return MENTION.sub(lambda m: f"@{m.group(1) or m.group(2)} ({m.group(2)})", text or "")
