"""Python tracing: `archplan trace-python` runs a project's tests with the
shipped tracer (archplan/python/archplan_pytrace.py), which writes one trace
file per test to .archplan/sequences. The Python export turns those files into
sequences: a call is a message when it crosses from one component to another."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .safeio import read_file

FORMAT = "archplan-python-trace/1"
MAX_DEPTH, MAX_MESSAGES = 12, 400
USAGE = """usage: archplan trace-python [-C DIR] [--python PYTHON] [pytest|unittest|django] [ARGS...]

Runs the project's tests and writes one trace file per test to
<repo root>/.archplan/sequences. `archplan export` then turns them into
sequences. Without a runner it picks pytest when the project's python has it
(and pytest-django for a Django project), django for a project with manage.py,
and unittest otherwise. ARGS go to the runner, such as test files or -k.

  -C DIR           the project directory to run the tests in (default: .)
  --python PYTHON  the python to run them with (default: the active virtual
                   environment, then .venv or venv in DIR, then python3)
"""
RUNNERS = ("pytest", "unittest", "django")


def find_python(d: Path) -> str:
    env = os.environ.get("VIRTUAL_ENV")
    for c in ([Path(env)] if env else []) + [d / ".venv", d / "venv"]:
        for exe in (c / "bin" / "python", c / "Scripts" / "python.exe"):
            if exe.is_file():
                return str(exe)
    return shutil.which("python3") or sys.executable


def has(python: str, module: str, cwd: Path) -> bool:
    try:
        return subprocess.run([python, "-c", f"import {module}"], cwd=cwd, capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pick(python: str, d: Path) -> str:
    if (d / "manage.py").is_file():
        return "pytest" if has(python, "pytest_django", d) else "django"
    return "pytest" if has(python, "pytest", d) else "unittest"


def repo_root(d: Path) -> Path:
    for p in [d, *d.parents]:
        if (p / ".archplan").is_dir() or (p / ".git").exists():
            return p
    return d


def main(argv: list) -> int:
    d, python = Path("."), None
    while argv and argv[0] in ("-C", "--python", "-h", "--help"):
        if argv[0] in ("-h", "--help"):
            print(USAGE, end="")
            return 0
        if len(argv) < 2:
            print(USAGE, end="", file=sys.stderr)
            return 2
        if argv[0] == "-C":
            d = Path(argv[1])
        else:
            python = argv[1]
        argv = argv[2:]
    d = d.resolve()
    python = python or find_python(d)
    runner, args = (argv[0], argv[1:]) if argv and argv[0] in RUNNERS else (pick(python, d), argv)
    root = repo_root(d)
    here = str(Path(__file__).resolve().parent.parent / "python")
    env = dict(os.environ, ARCHPLAN_TRACE_ROOT=str(root), ARCHPLAN_TRACE_DIR=str(root / ".archplan" / "sequences"),
               PYTHONPATH=os.pathsep.join([here, *filter(None, [os.environ.get("PYTHONPATH")])]))
    print(f"archplan: tracing {runner} tests with {python}", file=sys.stderr)
    try:
        return subprocess.run([python, "-m", "archplan_pytrace", runner, *args], cwd=d, env=env).returncode
    except OSError as e:
        print(f"archplan: cannot run {python}: {e}", file=sys.stderr)
        return 2


# --------------------------------------------------------------- export

def sequences(root: Path, comp_for) -> dict:
    """Sequences from the trace files in root/.archplan/sequences. comp_for
    gives the component of a module, or None for one outside the model."""
    out = {}
    for f in sorted((root / ".archplan" / "sequences").glob("*.json")):
        try:
            data = json.loads(read_file(f))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("format") == FORMAT and isinstance(data.get("trees"), list):
            seq = sequence(data, comp_for)
            if seq["messages"]:
                out[data["id"]] = seq
    return out


def sequence(data: dict, comp_for) -> dict:
    trees = collapse([n for t in data["trees"] for n in nodes(t.get("root"), t.get("actor") or "Test", comp_for, 0)])
    msgs, frags, count = [], {}, {}
    flatten(trees, 0, None, msgs, frags, count)
    cut = len(msgs) > MAX_MESSAGES
    msgs = msgs[:MAX_MESSAGES]
    used = set()
    for m in msgs:
        f = m.get("fragment")
        while f and f not in used:
            used.add(f)
            f = frags[f].get("parent")
    title = data.get("title") or data["id"]
    if cut or data.get("truncated"):
        title += " (truncated)"
    if data.get("state") == "failed":
        title += " (failed)"
    seq = {"title": title, **({"entry": data["entry"]} if data.get("entry") else {}), "source": "pytest-trace",
           "participants": list(dict.fromkeys(p for m in msgs for p in (m["from"], m["to"]))), "messages": msgs}
    frags = {k: v for k, v in frags.items() if k in used}
    if frags:
        seq["fragments"] = frags
    return seq


def nodes(node, frm: str, comp_for, depth: int) -> list:
    """A trace node is [module, qualname, children], with an outcome after
    the children when the call may have refused. Returns component-level
    nodes {from, to, label, children, outcome, repeat}."""
    if not isinstance(node, list) or len(node) < 3:
        return []
    mod, q, kids = node[0], node[1], node[2]
    to = comp_for(mod)
    if to and to != frm:
        below = [] if depth + 1 >= MAX_DEPTH else [n for k in kids for n in nodes(k, to, comp_for, depth + 1)]
        return [{"from": frm, "to": to, "label": f"{mod}.{q}()", "children": below, "repeat": 1,
                 "outcome": node[3] if len(node) > 3 else None}]
    return [n for k in kids for n in nodes(k, frm, comp_for, depth)]


def collapse(ns: list) -> list:
    """Merge consecutive identical siblings into one node with a repeat count."""
    out = []
    for n in ns:
        n["children"] = collapse(n["children"])
        if out and same(out[-1], n):
            out[-1]["repeat"] += n["repeat"]
        else:
            out.append(n)
    return out


def same(a: dict, b: dict) -> bool:
    return all(a[k] == b[k] for k in ("from", "to", "label", "outcome", "children"))


def flatten(ns: list, depth: int, frag, msgs: list, frags: dict, count: dict):
    for n in ns:
        here = frag
        if n["repeat"] > 1:
            here = f"L{len(frags) + 1}"
            frags[here] = {"kind": "loop", "label": f"×{n['repeat']}", "parent": frag}
        key = f"{n['from']}>{n['to']}:{n['label']}"
        count[key] = count.get(key, 0) + 1
        m = {"id": f"{key}#{count[key]}", "from": n["from"], "to": n["to"], "label": n["label"], "depth": depth}
        if here:
            m["fragment"] = here
        if n["outcome"]:
            m["outcome"] = n["outcome"]
        msgs.append(m)
        flatten(n["children"], depth + 1, here, msgs, frags, count)
