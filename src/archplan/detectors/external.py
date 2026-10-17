"""External detectors: a command that reads the model and prints findings.

An entry is a JSON file in contrib/detectors/ (shipped with archplan) or in
the project's .archplan/detectors/ (repository data):

  {"id": "group.name", "name": "...", "group": "concepts",
   "needs": ["fields"], "command": "python3 tools/check.py {ctx}",
   "params": {"limit": 3}, "timeout": 60}

archplan writes the context (the model, the parameters, the capabilities
and the files per component) to a temporary JSON file, puts its quoted path
in place of {ctx}, and runs the command in a shell in the project root. The
command prints {"findings": [{"key", "title", "detail"?, "strength"?,
"basis"?, "components"?, "files"?, "evidence"?, "focus"?, "weight"?,
"weight_basis"?}]}. focus says where the editor shows the finding (see
common.clean_focus); weight (0 to 100) how much it matters, with the factors
behind it in weight_basis (see common.Weight). Without a weight, the
strength sets it; with one, the strength follows from it.

A project entry's command comes from the repository, so it runs only after
a person approved that exact command for this directory: `archplan detectors
trust <id>` at a terminal. The approval also records a sha256 of each
argument that names a file in the repository (such as tools/check.py); when
one changes, the command needs a new approval. Shipped entries need no approval. The output is
untrusted: archplan keeps at most MAX_FINDINGS findings, shortens strings,
drops paths that leave the repository, components the model does not
have and focus entries with an unknown view, and makes each id from the
detector id and the finding's key.

A result is cached in .archplan/detectors-cache.json (not committed) for the
model, settings and command it came from. The editor shows the cached
result; `archplan check` and `archplan detectors run` run the command."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path

from ..core.project import append_ignore, changed_note, command_hash, etag, read_json, record_trust, write_json
from ..core.project import trust_state as project_trust_state
from ..core.safeio import read_file
from .common import EXAMPLES, GROUPS, STRENGTHS, Weight, clean_focus, clean_weight, fingerprint, set_weight

BUILTIN = Path(__file__).resolve().parent.parent / "contrib" / "detectors"
CACHE = "detectors-cache.json"
ID = re.compile(r"[a-z][a-z0-9_]{0,30}\.[a-z][a-z0-9_]{0,40}")
MAX_ENTRY = 64 * 1024
MAX_OUTPUT = 4 << 20
MAX_FINDINGS = 200
MAX_TIMEOUT = 600
TITLE_MAX, DETAIL_MAX, BASIS_MAX, PATH_MAX, KEY_MAX = 300, 2000, 300, 300, 200
EVIDENCE_MAX = 8000  # bytes of evidence JSON per finding


class NotRun(Exception):
    """The detector has no result for this model: not approved, not run yet, or it failed."""


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


# ---------- entries ----------

def check_entry(e) -> dict:
    """An entry read from disk, checked as untrusted data."""
    if not isinstance(e, dict):
        raise ValueError("a detector file must hold an object")
    did = e.get("id")
    if not isinstance(did, str) or not ID.fullmatch(did):
        raise ValueError("id must look like group.name (lower case, digits and _)")
    if e.get("group") not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")
    name = e.get("name", did)
    if not isinstance(name, str) or not 0 < len(name) <= 100:
        raise ValueError("name must be a short string")
    needs = e.get("needs") or []
    from . import KNOWN_NEEDS
    if not isinstance(needs, list) or not all(isinstance(n, str) and n in KNOWN_NEEDS for n in needs):
        raise ValueError(f"needs must list known needs: {', '.join(sorted(KNOWN_NEEDS))}")
    cmd = e.get("command")
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 1000 or "\n" in cmd or "\r" in cmd:
        raise ValueError("command must be a one-line string")
    if "{ctx}" not in cmd:
        raise ValueError("command must read {ctx}")
    params = e.get("params") or {}
    if not isinstance(params, dict) or len(params) > 32 or not all(
            isinstance(k, str) and len(k) <= 40 and isinstance(v, (bool, int, float, str, list)) for k, v in params.items()):
        raise ValueError("params must map names to numbers, strings, booleans or lists")
    timeout = e.get("timeout", 60)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"timeout must be 1 to {MAX_TIMEOUT} seconds")
    return {"id": did, "name": name, "group": e["group"], "needs": sorted(set(needs)), "params": params,
            "command": cmd, "timeout": float(timeout)}


def load(project_dir: Path | None = None) -> tuple:
    """({id: spec}, [(file, error)]); project entries replace shipped ones."""
    out, errors = {}, []
    dirs = [(BUILTIN, "contrib")]
    if project_dir is not None:
        dirs.append((Path(project_dir) / "detectors", "project"))
    for d, origin in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                e = check_entry(json.loads(read_file(f, MAX_ENTRY, errors="strict")))
            except (OSError, ValueError) as err:
                errors.append((str(f), str(err)[:300]))
                continue
            out[e["id"]] = dict(e, origin=origin, file=str(f))
    return out, errors


# ---------- trust ----------

def trust_key(root: Path, spec: dict) -> str:
    return command_hash(root, f"detector\0{spec['id']}\0{spec['command']}")


def trust_state(root: Path, spec: dict) -> tuple:
    """(approved, changed files). Shipped and built-in detectors need no
    approval; a project entry needs one for its command and for the content
    of the repository files the command names."""
    if spec.get("origin") in ("contrib", "built-in") or not spec.get("command"):
        return True, []
    return project_trust_state(root, "detectors", trust_key(root, spec), spec["command"])


def trusted(root: Path, spec: dict) -> bool:
    return trust_state(root, spec)[0]


def trust(root: Path, spec: dict) -> None:
    """Approve this detector's command, and the files it names, for this
    directory on this machine."""
    record_trust(root, "detectors", trust_key(root, spec), spec["command"])


# ---------- running ----------

def context(ctx, spec: dict, params: dict) -> dict:
    """What the command reads: the model and small extras."""
    return {"version": 1, "detector": spec["id"], "params": params, "root": str(ctx.root),
            "capabilities": ctx.capabilities, "model": ctx.model,
            "files_by_component": ctx.files_by_component}


def cache_key(ctx, spec: dict, params: dict) -> str:
    return short_hash(json.dumps([etag(ctx.p.model_path), etag(ctx.p.target_path), spec["command"], params],
                                 sort_keys=True))


def run(ctx, spec: dict, params: dict, execute: bool) -> list:
    """The detector's findings: from the cache, or by running the command when
    execute is set. Raises NotRun when there is no result to show."""
    path = ctx.p.path(CACHE)
    cache = read_json(path, {}) or {}
    key = cache_key(ctx, spec, params)
    hit = (cache.get(spec["id"]) or {}) if isinstance(cache, dict) else {}
    if not execute:
        if hit.get("key") == key and isinstance(hit.get("output"), list):
            if hit.get("error"):
                raise NotRun(text_of(hit["error"], 400))
            return validate(ctx, spec, {"findings": hit["output"]})
        raise NotRun(f"no result for the current model; run `archplan detectors run {spec['id']}`")
    ok, changed = trust_state(ctx.root, spec)
    if not ok:
        raise NotRun(f"its command is not approved on this machine: {spec['command']!r}{changed_note(changed)}; "
                     f"a person must run `archplan detectors trust {spec['id']}` in a terminal")
    try:
        raw = execute_command(ctx, spec, params)["findings"][:MAX_FINDINGS]
        fs = validate(ctx, spec, {"findings": raw})
        entry = {"key": key, "output": raw}
    except NotRun as e:
        fs, entry = None, {"key": key, "output": [], "error": str(e)}
    cache = cache if isinstance(cache, dict) else {}
    cache[spec["id"]] = entry
    write_json(path, cache)
    gitignore(ctx.p)
    if fs is None:
        raise NotRun(entry["error"])
    return fs


def gitignore(p) -> None:
    append_ignore(p, CACHE)


def execute_command(ctx, spec: dict, params: dict) -> dict:
    out = run_json(spec["command"], context(ctx, spec, params), ctx.root, spec["timeout"])
    if not isinstance(out, dict) or not isinstance(out.get("findings"), list):
        raise NotRun('did not print {"findings": [...]}')
    return out


def run_json(command: str, data: dict, cwd, timeout: float, env: dict | None = None):
    """Write data to a temporary file, run command with its quoted path in
    place of {ctx} in a shell in cwd, and return the JSON it prints. Raises
    NotRun when it cannot run, fails, times out or prints no JSON."""
    fd, tmp = tempfile.mkstemp(prefix="archplan-ctx-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        cmd = command.replace("{ctx}", shlex.quote(tmp))
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        except OSError as e:
            raise NotRun(f"could not run: {e}") from None
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # the shell and whatever it started
            except OSError:
                proc.kill()
            proc.communicate()
            raise NotRun(f"timed out after {timeout:g} seconds") from None
        r = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()[-300:]
        raise NotRun(f"failed with exit code {r.returncode}" + (f": {err}" if err else ""))
    if len(r.stdout) > MAX_OUTPUT:
        raise NotRun(f"printed more than {MAX_OUTPUT >> 20} MB")
    try:
        return json.loads(r.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        raise NotRun("did not print JSON") from None


def text_of(x, n: int) -> str:
    s = x if isinstance(x, str) else "" if x is None else str(x)
    s = "".join(c if c.isprintable() or c in "\n\t" else " " for c in s)
    return s[:n]


def safe_path(x) -> str | None:
    if not isinstance(x, str) or not x or len(x) > PATH_MAX or "\0" in x:
        return None
    x = x.replace("\\", "/")
    m = re.match(r"(.*?)(:\d+)?$", x)
    rel = m.group(1)
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/") or not rel.isprintable():
        return None
    return rel


def validate(ctx, spec: dict, out: dict) -> list:
    """Findings from untrusted output, in archplan's shape."""
    comps = set(ctx.components)
    fs = []
    for x in out.get("findings")[:MAX_FINDINGS]:
        if not isinstance(x, dict):
            continue
        title = text_of(x.get("title"), TITLE_MAX).strip()
        if not title:
            continue
        k = x.get("key", x.get("id", title))
        keys = [text_of(v, KEY_MAX) for v in (k if isinstance(k, list) else [k])][:10]
        ev = x.get("evidence")
        if not isinstance(ev, dict):
            ev = {}
        try:
            if len(json.dumps(ev)) > EVIDENCE_MAX:
                ev = {"truncated": True}
        except (TypeError, ValueError):
            ev = {}
        files = [safe_path(f) for f in (x.get("files") if isinstance(x.get("files"), list) else [])[:EXAMPLES]]
        cs = [c for c in (x.get("components") if isinstance(x.get("components"), list) else [])[:50]
              if isinstance(c, str) and c in comps]
        strength = x.get("strength") if x.get("strength") in STRENGTHS else "weak"
        fc = clean_focus(x.get("focus"))
        for e in fc:
            if "files" in e:
                e["files"] = [f for f in map(safe_path, e["files"]) if f]
                if not e["files"]:
                    del e["files"]
        fc = [e for e in fc if e["nodes"] or e.get("edges") or e.get("files")]
        f = {"id": fingerprint(spec["id"], *keys), "kind": spec["id"], "group": spec["group"],
             "title": title, "detail": text_of(x.get("detail"), DETAIL_MAX),
             "strength": strength, "basis": text_of(x.get("basis"), BASIS_MAX) or spec["name"],
             "components": list(dict.fromkeys(cs)), "files": list(dict.fromkeys(f for f in files if f)),
             "evidence": ev, "external": True, **({"focus": fc} if fc else {})}
        weight, wb = clean_weight(x.get("weight"), x.get("weight_basis"))
        w = Weight.of_strength(strength)
        if weight is not None:
            w = Weight(weight, "detector")
            w.basis += [b for b in wb if b["factor"] != "base"]
            w.basis[0]["effect"] = weight - sum(b["effect"] for b in w.basis[1:])  # the factors add up to the weight
        fs.append(set_weight(f, w))
    return fs
