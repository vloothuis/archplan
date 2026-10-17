"""Annotators: declarations next to the code, read into the model and
written back as a patch. The contract is in format/annotator-plugins.md.

A project entry is a JSON file in .archplan/annotators/:

  {"id": "django-db", "keys": ["entrypoint.auth"], "read": "cmd {ctx}",
   "write": "cmd {ctx}", "params": {}, "timeout": 120}

The built-in annotator (markers.py) owns every key no project entry lists.
Each key has one owner; config.json "annotators": {"owners": {key: id}}
settles a key two entries list, and "builtin": false switches the built-in
one off.

After every export, read() runs each owner's read and puts the
declarations in the model's "declarations" section. write() runs the
owners' write in a scratch git worktree, reads the scratch copy back to
check each change, and returns the difference as a patch, applied to the
working tree only when asked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from . import markers
from .core.project import ProjectError, command_hash, read_json, record_trust, write_json
from .core.project import trust_state as project_trust_state
from .core.safeio import read_file
from .detectors.external import NotRun, run_json, safe_path, text_of

KEYS = ("component.zone", "function.guard", "entrypoint.auth")
KINDS = ("component", "function", "entrypoint")
BUILTIN = "builtin"
ID = re.compile(r"[a-z][a-z0-9_-]{0,40}")
ZONE = re.compile(r"[a-z][a-z0-9_-]{0,40}")
MAX_ENTRY = 64 * 1024
MAX_DECLARATIONS = 5000
MAX_CHANGES = 500
MAX_TIMEOUT = 600
REASON_MAX, NAME_MAX, ID_MAX = 300, 200, 400
STATUSES = ("applied", "unsupported", "conflict")
BREAKS = re.compile(r"[\x00-\x1f\x7f\x85  ]")  # a reason goes into a source line


def valid_value(key: str, v) -> bool:
    if key == "component.zone":
        return isinstance(v, str) and bool(ZONE.fullmatch(v))
    if key == "function.guard":
        return v is True
    if key == "entrypoint.auth":
        return v in ("required", "none")
    return False


# ---------- entries ----------

def check_entry(e) -> dict:
    """An entry read from disk, checked as untrusted data."""
    if not isinstance(e, dict):
        raise ValueError("an annotator file must hold an object")
    aid = e.get("id")
    if not isinstance(aid, str) or not ID.fullmatch(aid) or aid == BUILTIN:
        raise ValueError("id must be lower case letters, digits, _ and - (and not builtin)")
    name = e.get("name", aid)
    if not isinstance(name, str) or not 0 < len(name) <= 100:
        raise ValueError("name must be a short string")
    keys = e.get("keys")
    if not isinstance(keys, list) or not keys or not all(k in KEYS for k in keys):
        raise ValueError(f"keys must list some of {', '.join(KEYS)}")
    cmds = {}
    for k in ("read", "write"):
        c = e.get(k)
        if c is None and k == "write":
            continue
        if not isinstance(c, str) or not c.strip() or len(c) > 1000 or "\n" in c or "\r" in c:
            raise ValueError(f"{k} must be a one-line command")
        if "{ctx}" not in c:
            raise ValueError(f"{k} must read {{ctx}}")
        cmds[k] = c
    params = e.get("params") or {}
    if not isinstance(params, dict) or len(params) > 32 or not all(
            isinstance(k, str) and len(k) <= 40 and isinstance(v, (bool, int, float, str, list)) for k, v in params.items()):
        raise ValueError("params must map names to numbers, strings, booleans or lists")
    timeout = e.get("timeout", 120)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"timeout must be 1 to {MAX_TIMEOUT} seconds")
    return {"id": aid, "name": name, "keys": sorted(set(keys)), "read": cmds["read"], "write": cmds.get("write"),
            "params": params, "timeout": float(timeout)}


def load(p) -> tuple:
    """({id: spec}, [(file, error)]) for the project's entries."""
    out, errors = {}, []
    d = p.path("annotators")
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                e = check_entry(json.loads(read_file(f, MAX_ENTRY, errors="strict")))
            except (OSError, ValueError) as err:
                errors.append((str(f), str(err)[:300]))
                continue
            out[e["id"]] = dict(e, file=str(f))
    return out, errors


def settings(p) -> dict:
    s = p.config.get("annotators")
    return s if isinstance(s, dict) else {}


def owners(p, specs: dict) -> tuple:
    """({key: annotator id}, [conflict messages]). A key without an owner is
    left out: the built-in annotator is off, or two entries claim it."""
    s = settings(p)
    chosen = s.get("owners") if isinstance(s.get("owners"), dict) else {}
    builtin = s.get("builtin", True) is not False
    claims = defaultdict(list)
    for aid, spec in sorted(specs.items()):
        for k in spec["keys"]:
            claims[k].append(aid)
    out, conflicts = {}, []
    for k in KEYS:
        pick = chosen.get(k)
        if pick == BUILTIN or (pick in specs and k in specs[pick]["keys"]):
            out[k] = pick
        elif pick is not None:
            conflicts.append(f"config.json names {pick!r} as the owner of {k}, but it does not list that key")
        elif len(claims[k]) == 1:
            out[k] = claims[k][0]
        elif claims[k]:
            conflicts.append(f"{', '.join(claims[k])} all list {k}; choose one in config.json "
                             f'"annotators": {{"owners": {{"{k}": "<id>"}}}}')
        elif builtin:
            out[k] = BUILTIN
    return out, conflicts


# ---------- trust ----------

def commands(spec: dict) -> str:
    return " ".join(c for c in (spec["read"], spec.get("write")) if c)


def trust_key(root: Path, spec: dict) -> str:
    return command_hash(root, f"annotator\0{spec['id']}\0{spec['read']}\0{spec.get('write') or ''}")


def trust_state(root: Path, spec: dict) -> tuple:
    """(approved, changed files) for a project entry's commands."""
    return project_trust_state(root, "annotators", trust_key(root, spec), commands(spec))


def trust(root: Path, spec: dict) -> None:
    record_trust(root, "annotators", trust_key(root, spec), commands(spec))


# ---------- reading ----------

class Index:
    """Where the model declares functions and types: by id and by (file, name)."""

    def __init__(self, model: dict):
        self.by_id, self.by_name = {}, defaultdict(list)
        items = [(s.get("id"), s) for s in model.get("signatures") or [] if isinstance(s, dict)]
        items += list((model.get("types") or {}).items())
        for i, x in items:
            if not isinstance(i, str) or not isinstance(x, dict) or not isinstance(x.get("file"), str):
                continue
            name = x.get("name") or i.rsplit(".", 1)[-1]
            at = {"file": x["file"], "line": x.get("line") if isinstance(x.get("line"), int) else None, "name": name}
            self.by_id.setdefault(i, at)
            self.by_name[(x["file"], name)].append((at["line"] or 0, i))

    def find(self, file: str, name: str, line) -> str | None:
        hits = self.by_name.get((file, name))
        if not hits:
            return None
        line = line if isinstance(line, int) else 0
        return min(hits, key=lambda h: (abs(h[0] - line), h[1]))[1]

    def at(self, tid: str) -> dict | None:
        """Where a function target id is declared: the model's place, or the
        file and name of a <file>:<name> id."""
        if tid in self.by_id:
            return dict(self.by_id[tid])
        f, sep, name = tid.rpartition(":")
        if sep and safe_path(f) == f and name:
            return {"file": f, "name": name}
        return None


def resolve(model: dict, index: Index, d, aid: str, owned: set, warn) -> dict | None:
    """A declaration from untrusted output in the model's shape, or None."""
    if not isinstance(d, dict) or not isinstance(d.get("target"), dict):
        return None
    t = d["target"]
    kind, key = t.get("kind"), d.get("key")
    full = f"{kind}.{key}"
    if kind not in KINDS or full not in KEYS:
        warn(f"{aid}: unknown key {text_of(full, 60)!r}")
        return None
    if full not in owned:
        warn(f"{aid}: does not own {full}")
        return None
    value = d.get("value")
    if not valid_value(full, value):
        warn(f"{aid}: {full} cannot be {text_of(json.dumps(value), 60)}")
        return None
    out = {"target": {"kind": kind}, "key": key, "value": value}
    if kind == "component":
        cid = t.get("id")
        if not isinstance(cid, str) or cid not in (model.get("components") or {}):
            warn(f"{aid}: no component {text_of(cid, 80)!r} in the model")
            return None
        out["target"]["id"] = cid
    else:
        tid = t.get("id")
        f, name, line = safe_path(t.get("file")), t.get("name"), t.get("line")
        name = text_of(name, NAME_MAX) if isinstance(name, str) and name.strip() else None
        if not (isinstance(tid, str) and 0 < len(tid) <= ID_MAX and tid.isprintable()):
            if not (f and name):
                warn(f"{aid}: a {kind} target needs an id, or a file and a name")
                return None
            tid = index.find(f, name, line) or f"{f}:{name}"
        out["target"]["id"] = tid
        at = {k: v for k, v in (("file", f), ("line", line if isinstance(line, int) else None), ("name", name)) if v}
        at = at if at.get("file") else index.at(tid)
        if at:
            out["at"] = at
    reason = d.get("reason")
    if isinstance(reason, str) and reason.strip():
        out["reason"] = text_of(reason, REASON_MAX).strip()
    f = safe_path(d.get("file"))
    if f:
        out["file"] = f
        if isinstance(d.get("line"), int) and d["line"] > 0:
            out["line"] = d["line"]
    out["annotator"] = aid
    return out


def env() -> dict:
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")  # no __pycache__ in a patch


def read_one(p, model: dict, aid: str, spec: dict | None, keys: list, root: Path, index: Index, warn) -> list:
    """The resolved declarations one annotator reads under root. The command
    runs in the project root, the approved copy of its files, also when root
    is the scratch copy. Raises NotRun."""
    if aid == BUILTIN:
        raw, warnings = markers.read(root, model, keys, p.exclude if root == p.root else None)
        for w in warnings:
            warn(w)
    else:
        ok, changed = trust_state(p.root, spec)
        if not ok:
            raise NotRun(f"not approved on this machine{'; changed: ' + ', '.join(changed) if changed else ''}; "
                         f"a person must run `archplan annotators trust {aid}` in a terminal")
        out = run_json(spec["read"], {"version": 1, "action": "read", "annotator": aid, "root": str(root),
                                      "keys": keys, "params": spec["params"], "model": model},
                       p.root, spec["timeout"], env=env())
        if not isinstance(out, dict) or not isinstance(out.get("declarations"), list):
            raise NotRun('did not print {"declarations": [...]}')
        raw = out["declarations"][:MAX_DECLARATIONS]
    owned = set(keys)
    return [x for x in (resolve(model, index, d, aid, owned, warn) for d in raw) if x]


def plain(model: dict) -> dict:
    return {k: v for k, v in model.items() if k != "declarations"}


def read(p, model: dict | None = None, root: Path | None = None) -> dict:
    """{"declarations", "warnings", "annotators": [{"id", "keys", "count" | "error"}]}."""
    model = plain(model if model is not None else read_json(p.model_path, {}) or {})
    root = Path(root or p.root)
    specs, errors = load(p)
    own, conflicts = owners(p, specs)
    warnings = [f"{f}: {e}" for f, e in errors] + conflicts
    index = Index(model)
    by_owner = defaultdict(list)
    for k, aid in own.items():
        by_owner[aid].append(k)
    decls, status = [], []
    for aid in sorted(by_owner, key=lambda a: (a != BUILTIN, a)):
        keys = sorted(by_owner[aid])
        try:
            got = read_one(p, model, aid, specs.get(aid), keys, root, index, warnings.append)
            status.append({"id": aid, "keys": keys, "count": len(got)})
            decls += got
        except NotRun as e:
            status.append({"id": aid, "keys": keys, "error": str(e)})
            warnings.append(f"{aid}: {e}")
    return {"declarations": dedupe(decls, warnings.append), "warnings": warnings, "annotators": status}


def ident(d: dict) -> tuple:
    return d["target"]["kind"], d["target"]["id"], d["key"]


def dedupe(decls: list, warn) -> list:
    """The first declaration per target and key; a different second value is a conflict."""
    seen, out = {}, []
    for d in decls:
        k = ident(d)
        if k in seen:
            if seen[k]["value"] != d["value"]:
                where = lambda x: f"{x.get('file', '?')}:{x.get('line', '?')}"
                warn(f"{'.'.join(k[::2])} of {k[1]} is {seen[k]['value']!r} at {where(seen[k])} "
                     f"and {d['value']!r} at {where(d)}; using the first")
            continue
        seen[k] = d
        out.append(d)
    return out


def enrich(p) -> dict:
    """Read the declarations into the model after an export."""
    model = read_json(p.model_path, {}) or {}
    res = read(p, model)
    model["declarations"] = res["declarations"]
    write_json(p.model_path, model)
    return res


# ---------- writing ----------

def check_change(c) -> str | None:
    """Why a change from the agent or a file is not valid, or None."""
    if not isinstance(c, dict):
        return "a change must be an object"
    t = c.get("target")
    if c.get("op") not in ("set", "remove"):
        return 'op must be "set" or "remove"'
    if not isinstance(t, dict) or t.get("kind") not in KINDS or not isinstance(t.get("id"), str) or not t["id"]:
        return "target needs a kind (component, function or entrypoint) and an id"
    full = f"{t['kind']}.{c.get('key')}"
    if full not in KEYS:
        return f"unknown key {full}"
    if c["op"] == "set" and not valid_value(full, c.get("value")):
        return f"{full} cannot be {json.dumps(c.get('value'))}"
    r = c.get("reason")
    if r is not None and (not isinstance(r, str) or len(r) > REASON_MAX or BREAKS.search(r)):
        return f"reason must be one line of text, at most {REASON_MAX} characters"
    if r is not None and (re.search(r"[<>]", r) or re.search(r"coding[:=]", r)):
        return "reason must not contain < or > (they can end a PHP comment) or coding: (a Python encoding line)"
    return None


def git(cwd, *args, check=True, input=None) -> subprocess.CompletedProcess:
    # No hooks, and no line ending conversion: a patch must change only the lines a writer changed.
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                        "-C", str(cwd), *args], capture_output=True, input=input,
                       stdin=None if input is not None else subprocess.DEVNULL, timeout=300)
    if check and r.returncode != 0:
        raise ProjectError(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return r


# The patch must not depend on the user's git settings: no other prefixes,
# colours, external diff drivers or text conversion, and no renames.
DIFF = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "color.diff=false")
DIFF_ARGS = ("--cached", "--no-ext-diff", "--no-color", "--no-textconv", "--no-renames")


class Scratch:
    """A temporary git worktree with the working tree as it is now: tracked
    files with uncommitted changes, and untracked files git does not ignore."""

    def __init__(self, root: Path):
        r = git(root, "rev-parse", "--show-toplevel", check=False)
        if r.returncode != 0:
            raise ProjectError("writing declarations needs a git repository")
        self.top = Path(r.stdout.decode().strip()).resolve()
        self.prefix = Path(root).resolve().relative_to(self.top)
        if git(self.top, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
            raise ProjectError("writing declarations needs a commit to start from")

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="archplan-scratch-"))
        self.wt = self.tmp / "repo"
        try:
            base = git(self.top, "stash", "create").stdout.decode().strip() or "HEAD"
            git(self.top, "worktree", "add", "--detach", "-q", str(self.wt), base)
            untracked = git(self.top, "ls-files", "-o", "--exclude-standard", "-z").stdout.decode("utf-8", "replace")
            for rel in filter(None, untracked.split("\0")):
                parts = rel.split("/")
                if ".archplan" in parts and parts[parts.index(".archplan") + 1:][:1] != ["annotators"]:
                    continue  # editor state stays out; a plugin kept in .archplan/annotators/ comes along
                src, dst = self.top / rel, self.wt / rel
                if src.is_file() and not src.is_symlink():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            self.base_tree = self.snapshot()
        except BaseException:
            self.__exit__()
            raise
        return self

    @property
    def root(self) -> Path:
        return self.wt / self.prefix

    def snapshot(self) -> str:
        """The tree of the scratch copy as it is now."""
        git(self.wt, "add", "-A")
        return git(self.wt, "write-tree").stdout.decode().strip()

    def restore(self, tree: str) -> None:
        """Put the scratch copy back to tree, removing files made since."""
        git(self.wt, "add", "-A")
        git(self.wt, "read-tree", "-u", "--reset", tree)

    def patch(self) -> tuple:
        """(patch bytes, changed paths relative to the repository top)."""
        self.snapshot()
        names = git(self.wt, *DIFF, "diff", *DIFF_ARGS, "--name-only", "-z", self.base_tree).stdout
        diff = git(self.wt, *DIFF, "diff", *DIFF_ARGS, "--binary", "--src-prefix=a/", "--dst-prefix=b/",
                   self.base_tree).stdout
        return diff, [n for n in names.decode("utf-8", "replace").split("\0") if n]

    def __exit__(self, *exc):
        git(self.top, "worktree", "remove", "--force", str(self.wt), check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)
        git(self.top, "worktree", "prune", check=False)


def canonical(c: dict, index: Index) -> dict:
    """c with a <file>:<name> function id replaced by the model's id for it,
    the id a read gives the same function."""
    t = c["target"]
    if t["kind"] == "component" or t["id"] in index.by_id:
        return c
    at = index.at(t["id"])
    tid = at and index.find(at["file"], at["name"], None)
    return dict(c, target=dict(t, id=tid)) if tid else c


def prepare(changes: list, current: list, index: Index) -> list:
    """Changes with at (where the target is declared) and current (the declaration there now)."""
    by_ident = {ident(d): d for d in current}
    at_of = {}
    for d in current:
        if d.get("at"):
            at_of.setdefault((d["target"]["kind"], d["target"]["id"]), d["at"])
    out = []
    for c in changes:
        c = {k: c[k] for k in ("op", "target", "key", "value", "reason") if k in c}
        c["target"] = {"kind": c["target"]["kind"], "id": c["target"]["id"]}
        kind, tid = c["target"]["kind"], c["target"]["id"]
        if kind != "component":
            at = at_of.get((kind, tid)) or at_of.get(("function" if kind == "entrypoint" else "entrypoint", tid)) \
                or index.at(tid)
            if at:
                c["at"] = at
        cur = by_ident.get((kind, tid, c["key"]))
        if cur:
            c["current"] = {k: cur[k] for k in ("value", "reason", "file", "line") if k in cur}
        out.append(c)
    return out


def write(p, changes: list, apply: bool = False) -> dict:
    """{"results": [{"change", "status", "note"?, "annotator"?}], "patch", "files", "applied"}."""
    if not isinstance(changes, list) or not changes:
        raise ProjectError("no changes")
    if len(changes) > MAX_CHANGES:
        raise ProjectError(f"at most {MAX_CHANGES} changes at a time")
    model = plain(read_json(p.model_path, {}) or {})
    if not model:
        raise ProjectError("no model yet; run `archplan export` first")
    specs, _ = load(p)
    own, _ = owners(p, specs)
    index = Index(model)
    results = [None] * len(changes)
    groups = defaultdict(list)  # owner -> [change index]
    for i, c in enumerate(changes):
        why = check_change(c)
        if why:
            results[i] = {"status": "unsupported", "note": why}
            continue
        aid = own.get(f"{c['target']['kind']}.{c['key']}")
        if aid is None:
            results[i] = {"status": "unsupported", "note": "no annotator owns this key"}
        elif aid != BUILTIN and not specs[aid].get("write"):
            results[i] = {"status": "unsupported", "note": f"{aid} has no write command", "annotator": aid}
        else:
            groups[aid].append(i)
    valid = sorted(i for idx in groups.values() for i in idx)
    current = read(p, model)["declarations"] if groups else []
    prepared = dict(zip(valid, prepare([canonical(changes[i], index) for i in valid], current, index)))
    diff, files = b"", []
    if groups:
        with Scratch(p.root) as s:
            for aid in sorted(groups, key=lambda a: (a != BUILTIN, a)):
                # The built-in annotator runs change by change, a plugin all its changes at once.
                for batch in ([[i] for i in groups[aid]] if aid == BUILTIN else [groups[aid]]):
                    write_batch(p, model, aid, specs, batch, prepared, results, s, index)
            diff, files = s.patch()
        bad = [f for f in files if ".archplan" in f.split("/")[:-1] or f.split("/")[0] == ".git"]
        if bad:
            raise ProjectError(f"an annotator changed {', '.join(bad[:5])}; the patch was rejected")
    applied = False
    if apply and diff:
        git(s.top, "apply", "--binary", "--whitespace=nowarn", "-p1", "-", input=diff)
        applied = True
    out = [dict(r, change=changes[i]) for i, r in enumerate(results)]
    return {"results": out, "patch": diff.decode("utf-8", "replace"), "files": files, "applied": applied}


def write_batch(p, model: dict, aid: str, specs: dict, batch: list, prepared: dict, results: list, s: Scratch,
                index: Index) -> None:
    """Run one annotator's write on a batch of changes and read it back. When
    any change of the batch did not work, the batch's files go back to how
    they were, so the patch holds only changes that did."""
    before = s.snapshot()
    try:
        got = run_write(p, model, aid, specs.get(aid), [prepared[i] for i in batch], s)
    except NotRun as e:
        got = [{"status": "conflict", "note": str(e)}] * len(batch)
    for i, r in zip(batch, got):
        results[i] = dict(r, annotator=aid)
    verify(p, model, s, {i: prepared[i] for i in batch}, results, specs, index)
    if all(results[i]["status"] == "applied" for i in batch):
        return
    s.restore(before)
    for i in batch:
        if results[i]["status"] == "applied":
            results[i] = dict(results[i], status="failed",
                              note="left out: another change for this annotator did not work")


def run_write(p, model: dict, aid: str, spec: dict | None, chs: list, s: Scratch) -> list:
    if aid == BUILTIN:
        return markers.write(s.root, model, chs)
    ok, changed = trust_state(p.root, spec)
    if not ok:
        raise NotRun(f"{aid} is not approved on this machine; a person must run "
                     f"`archplan annotators trust {aid}` in a terminal")
    out = run_json(spec["write"], {"version": 1, "action": "write", "annotator": aid, "root": str(s.root),
                                   "repo": str(p.root), "keys": spec["keys"], "params": spec["params"],
                                   "model": model, "changes": chs}, s.root, spec["timeout"], env=env())
    rs = out.get("results") if isinstance(out, dict) else None
    if not isinstance(rs, list) or len(rs) != len(chs):
        raise NotRun(f'did not print {{"results": [...]}} with one result per change ({len(chs)})')
    clean = []
    for r in rs:
        st = r.get("status") if isinstance(r, dict) else None
        note = text_of(r.get("note"), 300) if isinstance(r, dict) and r.get("note") else None
        clean.append({"status": st if st in STATUSES else "conflict",
                      **({"note": note} if note else {} if st in STATUSES else {"note": "no valid status"})})
    return clean


def verify(p, model: dict, s: Scratch, changes: dict, results: list, specs: dict, index: Index) -> None:
    """Read the scratch copy back; an applied change that did not come back
    failed. changes maps a result's index to its prepared change."""
    by_owner = defaultdict(list)
    for i in changes:
        r = results[i]
        if r and r["status"] == "applied" and r.get("annotator"):
            by_owner[r["annotator"]].append(i)
    for aid, idx in by_owner.items():
        keys = sorted({f"{changes[i]['target']['kind']}.{changes[i]['key']}" for i in idx} |
                      set(specs[aid]["keys"] if aid in specs else []))
        try:
            got = read_one(p, model, aid, specs.get(aid), keys, s.root, index, lambda w: None)
        except NotRun as e:
            for i in idx:
                results[i] = dict(results[i], status="failed", note=f"reading it back failed: {e}")
            continue
        now = {ident(d): d for d in dedupe(got, lambda w: None)}
        for i in idx:
            c = changes[i]
            d = now.get((c["target"]["kind"], c["target"]["id"], c["key"]))
            if c["op"] == "set" and (d is None or d["value"] != c["value"]):
                results[i] = dict(results[i], status="failed",
                                  note="reading it back does not show the new value" if d is None
                                  else f"reading it back shows {d['value']!r}")
            elif c["op"] == "remove" and d is not None:
                results[i] = dict(results[i], status="failed", note="reading it back still shows it")
