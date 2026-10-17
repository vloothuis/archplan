"""The exporter registry: which command turns a repository into an archmodel.

Entries live in contrib/exporters/*.json; a project can add or replace them
in .archplan/exporters/*.json. An entry:

  {"name", "title"?, "languages": [...], "priority": int,
   "detect": {"files"?: [globs], "manifests"?: {file: [dependency names]},
              "subdirs"?: true, "weight"?: [globs]},
   "command": "archplan export-x{chdir} -o {out}",
   "check"?: {"programs"?: [names], "files"?: [paths]} | "a command",
   "capabilities": ["components", "types", "fields", "deps", "files", "calls"],
   "install"?: "how to install it", "runs_repo_code"?: true}

An entry matches a folder when one of its file globs matches a file there
(globs without a slash look at the folder itself) and one of its manifests
exists there, declaring one of the listed dependencies when any are listed.
With subdirs, a direct subfolder can match instead; the one with the most
files matching `weight` wins. In the command, {out} is the model path,
{root} the matched folder and {chdir} " -C <folder>" when that is not the
root.

runs_repo_code marks an exporter that runs code from the repository: the
TypeScript exporter loads the project's own typescript package from
node_modules, the Elixir exporter compiles mix.exs and the project, and the
Go exporter runs the go toolchain in the module. `archplan init` never
approves such a command; the person approves it with `archplan trust`.

Project entries are untrusted repository data. Their check commands never
run, and `archplan init` does not approve a command that came from one: the
person approves it with `archplan trust`."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .safeio import is_file, read_file

BUILTIN = Path(__file__).resolve().parents[1] / "contrib" / "exporters"
CAPABILITIES = ["components", "types", "fields", "deps", "files", "calls", "signatures", "literals", "schema"]
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SKIP = {".git", "node_modules", "deps", "_build", "build", "dist", "vendor", "target", "bin", "obj", "out",
        "coverage", "__pycache__", ".venv", "venv"}
MAX_FILE = 64 * 1024
WEIGHT_CAP = 5000


class ExporterError(ValueError):
    pass


def _strs(x, what: str, n: int = 64) -> list:
    if not isinstance(x, list) or len(x) > n or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in x):
        raise ExporterError(f"{what} must be a list of at most {n} short strings")
    return x


def check_entry(e) -> dict:
    """An entry read from disk, checked as untrusted data."""
    if not isinstance(e, dict):
        raise ExporterError("an exporter file must hold an object")
    if not isinstance(e.get("name"), str) or not NAME.fullmatch(e["name"]):
        raise ExporterError("name must be lower-case letters, digits and dashes")
    _strs(e.get("languages") or [], "languages")
    pr = e.get("priority", 0)
    if not isinstance(pr, int) or isinstance(pr, bool) or not -1000 <= pr <= 1000:
        raise ExporterError("priority must be an integer from -1000 to 1000")
    det = e.get("detect")
    if not isinstance(det, dict):
        raise ExporterError("detect must be an object")
    _strs(det.get("files") or [], "detect.files")
    _strs(det.get("weight") or [], "detect.weight")
    man = det.get("manifests") or {}
    if not isinstance(man, dict) or len(man) > 32:
        raise ExporterError("detect.manifests must map file names to dependency lists")
    for k, v in man.items():
        if not isinstance(k, str) or "/" in k or not 0 < len(k) <= 100:
            raise ExporterError("detect.manifests keys must be file names")
        _strs(v or [], f"detect.manifests[{k}]")
    if not (det.get("files") or man):
        raise ExporterError("detect needs files or manifests")
    if not isinstance(det.get("subdirs", False), bool):
        raise ExporterError("detect.subdirs must be true or false")
    cmd = e.get("command")
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 1000 or "\n" in cmd:
        raise ExporterError("command must be a one-line string")
    if "{out}" not in cmd:
        raise ExporterError("command must write to {out}")
    chk = e.get("check")
    if isinstance(chk, dict):
        _strs(chk.get("programs") or [], "check.programs", 8)
        _strs(chk.get("files") or [], "check.files", 8)
    elif chk is not None and (not isinstance(chk, str) or len(chk) > 500):
        raise ExporterError("check must be an object or a command")
    caps = _strs(e.get("capabilities") or [], "capabilities")
    if set(caps) - set(CAPABILITIES):
        raise ExporterError(f"unknown capabilities: {', '.join(sorted(set(caps) - set(CAPABILITIES)))}")
    if not isinstance(e.get("runs_repo_code", False), bool):
        raise ExporterError("runs_repo_code must be true or false")
    for k in ("title", "install"):
        if k in e and (not isinstance(e[k], str) or len(e[k]) > 1000):
            raise ExporterError(f"{k} must be a string")
    return e


def load(project_dir: Path | None = None) -> tuple:
    """({name: entry}, [(file, error)]); project entries replace built-ins."""
    out, errors = {}, []
    dirs = [(BUILTIN, "built-in")]
    if project_dir is not None:
        dirs.append((Path(project_dir) / "exporters", "project"))
    for d, origin in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                e = check_entry(json.loads(read_file(f, MAX_FILE, errors="strict")))
            except (OSError, ValueError) as err:
                errors.append((str(f), str(err)))
                continue
            out[e["name"]] = dict(e, origin=origin)
    return out, errors


# ---------- matching ----------

def _glob(d: Path, pattern: str, ex=None) -> bool:
    """Whether a file under d matches; a pattern without a slash looks at d itself.
    ex (an exclude.Exclude) leaves out the paths it matches."""
    if "/" not in pattern:
        return any(not (ex and ex(p)) for p in d.glob(pattern))
    from .architecture import glob_match
    for dirpath, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith(".")]
        if ex:
            ex.prune(dirpath, dirs)
        rel = os.path.relpath(dirpath, d).replace(os.sep, "/")
        for n in names:
            if ex and ex(os.path.join(dirpath, n)):
                continue
            if glob_match(n if rel == "." else f"{rel}/{n}", pattern):
                return True
    return False


def _deps_in(path: Path) -> set:
    from .frameworks import read_deps
    return read_deps(path)


def matches_dir(e: dict, d: Path, ex=None) -> bool:
    det = e["detect"]
    files, man = det.get("files") or [], det.get("manifests") or {}
    if man:
        ok = False
        for name, deps in man.items():
            for p in ([d / name] if not any(c in name for c in "*?[") else sorted(d.glob(name))):
                if not is_file(p) or (ex and ex(p)):
                    continue
                if not deps:
                    ok = True
                else:
                    from .frameworks import dep_match
                    have = _deps_in(p)
                    ok = any(dep_match(have, x) for x in deps)
                if ok:
                    break
            if ok:
                break
        if not ok:
            return False
    return not files or any(_glob(d, g, ex) for g in files)


def weight(e: dict, d: Path, ex=None) -> int:
    from .architecture import glob_match
    pats = e["detect"].get("weight") or []
    if not pats:
        return 0
    n = 0
    for dirpath, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith(".")]
        if ex:
            ex.prune(dirpath, dirs)
            names = [x for x in names if not ex(os.path.join(dirpath, x))]
        rel = os.path.relpath(dirpath, d).replace(os.sep, "/")
        n += sum(1 for x in names if any(glob_match(x if rel == "." else f"{rel}/{x}", p) for p in pats))
        if n >= WEIGHT_CAP:
            break
    return n


def locate(e: dict, root: Path, ex=None) -> Path | None:
    """The folder the entry matches: the root, or else the best direct subfolder."""
    if matches_dir(e, root, ex):
        return Path(".")
    if not e["detect"].get("subdirs"):
        return None
    subs = [d for d in sorted(root.iterdir())
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and d.name not in SKIP
            and not (ex and ex(d)) and matches_dir(e, d, ex)]
    if not subs:
        return None
    return max(subs, key=lambda d: weight(e, d, ex)).relative_to(root)


def installed(e: dict, root: Path, where: Path) -> bool | None:
    """True or False from the entry's check; None when it cannot be checked
    (a command in a project entry, which never runs)."""
    chk = e.get("check")
    if chk is None:
        return True
    if isinstance(chk, dict):
        return all(shutil.which(p) for p in chk.get("programs") or []) and \
            all((root / where / f).exists() for f in chk.get("files") or [])
    if e.get("origin") != "built-in":
        return None
    try:
        return subprocess.run(shlex.split(chk), capture_output=True, timeout=20).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def command(e: dict, where: Path, out: str) -> str:
    w = str(where)
    return (e["command"].replace("{chdir}", f" -C {shlex.quote(w)}" if w != "." else "")
            .replace("{root}", shlex.quote(w)).replace("{out}", shlex.quote(out)))


def survey(root: Path, project_dir: Path | None = None, out: str = ".archplan/model.archmodel.json") -> dict:
    """Every entry with its match, installed state and command, and the pick:
    the highest-priority match that is installed, else lite, else the
    highest-priority match."""
    from .exclude import load as load_exclude
    es, errors = load(project_dir)
    ex = load_exclude(root)
    rows = []
    for e in sorted(es.values(), key=lambda e: (-e.get("priority", 0), e["name"])):
        where = locate(e, root, ex)
        row = {"name": e["name"], "title": e.get("title"), "languages": e.get("languages") or [],
               "origin": e["origin"], "priority": e.get("priority", 0), "capabilities": e.get("capabilities") or [],
               "match": None if where is None else str(where), "install": e.get("install"),
               "runs_repo_code": bool(e.get("runs_repo_code"))}
        if where is not None:
            row["installed"] = installed(e, root, where)
            row["command"] = command(e, where, out)
        rows.append(row)
    hits = [r for r in rows if r["match"] is not None]
    pick = next((r for r in hits if r["installed"] is not False and r["name"] != "lite"), None) \
        or next((r for r in hits if r["name"] == "lite"), None) or (hits[0] if hits else None)
    return {"exporters": rows, "pick": pick["name"] if pick else None, "command": pick["command"] if pick else None,
            "trusted_source": bool(pick and pick["origin"] == "built-in"),
            "runs_repo_code": bool(pick and (pick["runs_repo_code"] or pick["origin"] != "built-in")),
            "errors": [{"file": f, "error": x} for f, x in errors], "exclude": ex.patterns}


def text(res: dict) -> str:
    lines = [f"  {'exporter':<12}{'languages':<34}{'origin':<10}{'prio':>5}  match"]
    for r in res["exporters"]:
        mark = "*" if r["name"] == res["pick"] else " "
        if r["match"] is None:
            m = "no"
        else:
            inst = {True: "installed", False: "not installed", None: "not checked"}[r["installed"]]
            m = f"{r['match']}, {inst}"
        lines.append(f"{mark} {r['name']:<12}{', '.join(r['languages'])[:33]:<34}{r['origin']:<10}{r['priority']:>5}  {m}")
    lines.append("")
    if res["command"]:
        lines.append(f"* {res['command']}")
        if res["runs_repo_code"]:
            lines.append("  This command runs code from the repository; `archplan trust` must approve it first.")
    for r in res["exporters"]:
        if r["match"] is not None and r["installed"] is False and r.get("install"):
            lines.append(f"to use {r['name']}: {r['install']}")
    for e in res["errors"]:
        lines.append(f"skipped {e['file']}: {e['error']}")
    if res.get("exclude"):
        lines.append("excluded (config.json): " + ", ".join(res["exclude"]))
    return "\n".join(lines)
