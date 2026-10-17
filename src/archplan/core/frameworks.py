"""Framework profiles: architecture defaults per framework, as data.

The built-in profiles live in contrib/profiles/*.json. A project can add or
override them in .archplan/profiles/*.json; a project file with the same name
wins. Both are untrusted data: every file is size-capped and checked like a
saved profile (role ids, rule references, hex colours, string patterns).

A profile file:

  {"name", "title", "language", "description",
   "detect": {"priority": int, "files": [glob | {"glob", "min"}], "deps": [names]}
             or {"priority", "any": [{"files", "deps"}, ...]},
   "base"?: [file names],
   "units", "roles", "rules",
   "layers"?: [{"name", "match": [component id or path globs]}]}

A detect alternative matches when every file glob matches (at least `min`
files) and, when deps are given, the project's manifests declare one of
them. `{base}` in a unit glob is the common parent of the folders that hold
one of the base files. Layers match components by id or path; the first
matching layer wins. When fewer than half of the components match, the
layers are inferred from the model instead."""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path

from .architecture import COLORS, NAMES, SKIP_DIRS, glob_match, infer_layers, role_of, units, validate
from .safeio import Unsafe, read_file
from .store import stamp

BUILTIN = Path(__file__).resolve().parents[1] / "contrib" / "profiles"
MAX_FILE = 256 * 1024
MAX_STR = 200
MAX_LIST = 64
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
WALK_CAP = 200_000

# Manifests and how to read dependency names out of them.
MANIFESTS = {
    "pyproject.toml": "python", "setup.cfg": "python", "setup.py": "python", "Pipfile": "python",
    "package.json": "npm", "pom.xml": "maven", "build.gradle": "gradle", "build.gradle.kts": "gradle",
    "Gemfile": "gem", "composer.json": "composer", "mix.exs": "mix", "go.mod": "go", "Cargo.toml": "cargo",
}
PY_REQ = re.compile(r"^[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)", re.M)


class ProfileError(ValueError):
    pass


# ---------- manifests ----------

def manifest_files(root: Path, depth: int = 3) -> list:
    """Manifest paths under root, at most depth folders down."""
    out = []
    for d, dirs, names in os.walk(root):
        rel = os.path.relpath(d, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")] if level < depth else []
        for n in names:
            if n in MANIFESTS or n.endswith(".csproj") or (n.startswith("requirements") and n.endswith(".txt")) \
                    or (os.path.basename(d) == "requirements" and n.endswith(".txt")):
                out.append(Path(d) / n)
    return out


def read_deps(path: Path) -> set:
    """Dependency names a manifest declares, lower-cased."""
    try:
        text = read_file(path, 2 * MAX_FILE)
    except OSError:
        return set()
    n, out = path.name, set()
    kind = MANIFESTS.get(n)
    if n.endswith(".csproj"):
        out |= set(re.findall(r'PackageReference\s+Include="([^"]+)"', text))
        if re.search(r'Sdk="Microsoft\.NET\.Sdk\.Web"', text):
            out.add("Microsoft.AspNetCore")
    elif n.endswith(".txt"):
        out |= {m for m in PY_REQ.findall(text) if not m.startswith("-")}
    elif kind == "python":
        out |= set(re.findall(r'["\']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*[<>=!~;,"\']', text))
        out |= set(re.findall(r"^[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", text, re.M))  # poetry and Pipfile
    elif kind in ("npm", "composer"):
        try:
            d = json.loads(text)
        except ValueError:
            return set()
        for k in ("dependencies", "devDependencies", "peerDependencies", "require", "require-dev"):
            if isinstance(d.get(k), dict):
                out |= {x for x in d[k] if isinstance(x, str)}
    elif kind == "maven":
        out |= set(re.findall(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", text))
        out |= set(re.findall(r"<groupId>\s*([^<\s]+)\s*</groupId>", text))
    elif kind == "gradle":
        for g, a in re.findall(r"""["']([\w.-]+):([\w.-]+)(?::[^"']*)?["']""", text):
            out |= {g, a}
        out |= set(re.findall(r"""id\s*(?:\(\s*)?["']([\w.-]+)["']""", text))
    elif kind == "gem":
        out |= set(re.findall(r"""^[ \t]*gem\s+["']([\w.-]+)["']""", text, re.M))
    elif kind == "mix":
        out |= set(re.findall(r"\{\s*:(\w+)\s*,", text))
    elif kind == "go":
        out |= set(re.findall(r"^[ \t]*(?:require\s+)?([\w.-]+\.[\w.-]+/[\w./-]+)\s+v", text, re.M))
    elif kind == "cargo":
        out |= set(re.findall(r"^[ \t]*([A-Za-z0-9_-]+)\s*=", text, re.M))
    return {x.lower() for x in out}


def dep_match(have: set, want: str) -> bool:
    w = want.lower()
    return w in have or any(h.startswith(w + "-") or h.startswith(w + ".") or h.endswith("/" + w) for h in have)


class Context:
    """What detection looks at: the source files, all file paths when a root
    is given, and the dependency names from the manifests."""

    def __init__(self, files: list, root: Path | None = None):
        self.files = list(files)
        self.paths = set(files)
        self.deps = set()
        if root is not None and Path(root).is_dir():
            root = Path(root)
            n = 0
            for d, dirs, names in os.walk(root):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
                rel = os.path.relpath(d, root).replace(os.sep, "/")
                for x in names:
                    self.paths.add(x if rel == "." else rel + "/" + x)
                n += len(names)
                if n > WALK_CAP:
                    break
            for m in manifest_files(root):
                self.deps |= read_deps(m)

    def count(self, pattern: str, limit: int) -> int:
        n = 0
        for p in self.paths:
            if glob_match(p, pattern):
                n += 1
                if n >= limit:
                    break
        return n


def alternatives(detect: dict) -> list:
    if isinstance(detect.get("any"), list):
        return detect["any"]
    return [{"files": detect.get("files") or [], "deps": detect.get("deps") or []}]


def detects(profile: dict, ctx: Context) -> bool:
    for alt in alternatives(profile.get("detect") or {}):
        files, deps = alt.get("files") or [], alt.get("deps") or []
        if not files and not deps:
            continue
        if deps and not any(dep_match(ctx.deps, d) for d in deps):
            continue
        ok = True
        for f in files:
            g, k = (f["glob"], f.get("min", 1)) if isinstance(f, dict) else (f, 1)
            if ctx.count(g, k) < k:
                ok = False
                break
        if ok:
            return True
    return False


# ---------- loading and checking ----------

def _str(x, what: str) -> str:
    if not isinstance(x, str) or not x or len(x) > MAX_STR:
        raise ProfileError(f"{what} must be a string of at most {MAX_STR} characters")
    return x


def _strs(x, what: str) -> list:
    if not isinstance(x, list) or len(x) > MAX_LIST:
        raise ProfileError(f"{what} must be a list of at most {MAX_LIST} items")
    return [_str(v, what) for v in x]


def check(t) -> dict:
    """A framework profile read from disk, checked as untrusted data."""
    if not isinstance(t, dict):
        raise ProfileError("a profile file must hold an object")
    if not isinstance(t.get("name"), str) or not NAME.fullmatch(t["name"]):
        raise ProfileError("name must be lower-case letters, digits and dashes")
    for k in ("title", "language", "description"):
        if k in t and (not isinstance(t[k], str) or len(t[k]) > 1000):
            raise ProfileError(f"{k} must be a string of at most 1000 characters")
    det = t.get("detect")
    if not isinstance(det, dict):
        raise ProfileError("detect must be an object")
    pr = det.get("priority", 0)
    if not isinstance(pr, int) or isinstance(pr, bool) or not -1000 <= pr <= 1000:
        raise ProfileError("detect.priority must be an integer from -1000 to 1000")
    alts = alternatives(det)
    if not isinstance(alts, list) or len(alts) > 16:
        raise ProfileError("detect.any must be a list of at most 16 alternatives")
    for a in alts:
        if not isinstance(a, dict):
            raise ProfileError("each detect alternative must be an object")
        for f in a.get("files") or []:
            if isinstance(f, dict):
                _str(f.get("glob"), "detect file glob")
                if not isinstance(f.get("min", 1), int) or not 1 <= f.get("min", 1) <= 1000:
                    raise ProfileError("detect file min must be an integer from 1 to 1000")
            else:
                _str(f, "detect file glob")
        _strs(a.get("deps") or [], "detect.deps")
    _strs(t.get("base") or [], "base")
    for layer in t.get("layers") or []:
        if not isinstance(layer, dict):
            raise ProfileError("each layer must be an object")
        _str(layer.get("name"), "layer name")
        _strs(layer.get("match") or [], "layer match")
    for r in t.get("roles") or []:
        if isinstance(r, dict):
            _strs(r.get("patterns") or [], f"role {r.get('id')} patterns")
    for u in t.get("units") or []:
        if isinstance(u, dict):
            for k in ("require", "exclude", "except"):
                _strs(u.get(k) or [], f"unit {k}")
    validate({k: t.get(k, []) for k in ("units", "roles", "rules")} | {"layers": []})
    return t


def read(path: Path) -> dict:
    try:
        text = read_file(path, MAX_FILE, errors="strict")
    except Unsafe as e:
        raise ProfileError(f"not read: {e}") from None
    try:
        t = json.loads(text)
    except ValueError as e:
        raise ProfileError(f"not JSON: {e}") from None
    return check(t)


def load(project_dir: Path | None = None) -> tuple:
    """({name: profile}, [(file, error)]): the built-in profiles, then the
    project's, which replace built-ins of the same name."""
    out, errors = {}, []
    dirs = [(BUILTIN, "built-in")]
    if project_dir is not None:
        dirs.append((Path(project_dir) / "profiles", "project"))
    for d, origin in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                t = read(f)
            except (OSError, ProfileError, ValueError) as e:
                errors.append((str(f), str(e)))
                continue
            out[t["name"]] = dict(t, origin=origin)
    return out, errors


# ---------- applying ----------

def base_of(names: list, files: list) -> str:
    dirs = sorted({f.rsplit("/", 1)[0] for f in files if "/" in f and f.rsplit("/", 1)[1] in names})
    if not dirs:
        return ""
    base = os.path.commonpath(dirs)
    if base in dirs:
        base = base.rsplit("/", 1)[0] if "/" in base else ""
    return base


def resolve_glob(glob: str, base: str) -> str:
    if "{base}" not in glob:
        return glob
    if base:
        return glob.replace("{base}", base)
    g = glob.replace("{base}/", "").replace("{base}", "")
    return g or "**"


def layers_for(t: dict, model: dict) -> list:
    specs = t.get("layers") or []
    comps = {c: v or {} for c, v in (model.get("components") or {}).items() if (v or {}).get("kind") != "external"}
    if specs and comps:
        got = {i: [] for i in range(len(specs))}
        for c, v in sorted(comps.items()):
            for i, layer in enumerate(specs):
                if any(fnmatch.fnmatchcase(c, p) or (v.get("path") and glob_match(v["path"], p))
                       for p in layer.get("match") or []):
                    got[i].append(c)
                    break
        if sum(len(x) for x in got.values()) >= 0.5 * len(comps):
            return [{"name": layer["name"], "components": got[i]} for i, layer in enumerate(specs) if got[i]]
    return infer_layers(model)


def apply(t: dict, model: dict, files: list) -> dict:
    """The proposal a framework profile gives for this model."""
    base = base_of(t.get("base") or [], files) if t.get("base") else ""
    us = []
    for u in t.get("units") or []:
        u = dict(u, glob=resolve_glob(u["glob"], base))
        us.append(u)
    roles = []
    for i, r in enumerate(t.get("roles") or []):
        r = dict(r)
        r.setdefault("name", NAMES.get(r["id"], r["id"].replace("_", " ").capitalize()))
        r.setdefault("color", COLORS[i % len(COLORS)])
        r.setdefault("description", f"{t.get('title') or t['name']}: {r['name'].lower()}")
        roles.append(r)
    out = {"version": 1, "status": "proposed", "name": t.get("title") or t["name"], "source": t["name"],
           "inferred": stamp(), "units": us, "roles": roles, "rules": [dict(r) for r in t.get("rules") or []],
           "layers": layers_for(t, model)}
    return validate(out)


def coverage(profile: dict, files: list) -> dict:
    """How well a profile fits: units found, and the share of source files
    that are in a unit and have a role."""
    us = units(profile, files)
    roles = profile.get("roles") or []
    hit = sum(1 for u in us for _, rel in u["files"] if role_of(rel, roles))
    return {"units": len(us), "files": len(files), "with_role": hit,
            "coverage": round(hit / len(files), 3) if files else 0.0}


def matches(model: dict, files: list, root: Path | None = None, project_dir: Path | None = None) -> list:
    """[(profile file, proposal, fit)] for every framework profile that
    detects this code and finds units, best first."""
    ts, _ = load(project_dir)
    ctx = Context(files, root)
    out = []
    for t in ts.values():
        if not detects(t, ctx):
            continue
        prop = apply(t, model, files)
        fit = coverage(prop, files)
        if fit["units"]:
            out.append((t, prop, fit))
    return sorted(out, key=lambda x: (-x[0]["detect"].get("priority", 0), -x[2]["coverage"], x[0]["name"]))


# A framework profile wins unless the generic inference covers this many times more files.
GENERIC_MARGIN = 1.5


def choose(fw: list, generic: dict | None, files: list) -> dict | None:
    """The framework proposal to use, or None for the generic one."""
    if not fw:
        return None
    best = fw[0]
    if generic is None:
        return best[1]
    g = coverage(generic, files)["coverage"]
    return None if g > GENERIC_MARGIN * best[2]["coverage"] else best[1]



def listing(model: dict, files: list, root: Path | None = None, project_dir: Path | None = None) -> dict:
    """Every profile with whether it matches this code and how well it fits,
    the generic inference, and the one `archplan profile infer` would pick."""
    from .architecture import ArchitectureError, generic
    ts, errors = load(project_dir)
    ctx = Context(files, root)
    rows = []
    for t in sorted(ts.values(), key=lambda t: (-t["detect"].get("priority", 0), t["name"])):
        row = {"name": t["name"], "title": t.get("title"), "language": t.get("language"), "origin": t["origin"],
               "priority": t["detect"].get("priority", 0), "matches": detects(t, ctx)}
        if row["matches"]:
            row.update(coverage(apply(t, model, files), files))
        rows.append(row)
    try:
        gen = generic(model, files)
        g = dict(coverage(gen, files), name="inferred", title=gen.get("name"))
    except ArchitectureError as e:
        gen, g = None, {"name": "inferred", "error": str(e)}
    fw = [(t, p, c) for t, p, c in matches(model, files, root, project_dir)]
    pick = choose(fw, gen, files)
    return {"profiles": rows, "inferred": g, "pick": pick["source"] if pick else ("inferred" if gen else None),
            "errors": [{"file": f, "error": e} for f, e in errors]}


def listing_text(res: dict) -> str:
    lines = [f"{'':2}{'profile':<14}{'language':<12}{'origin':<10}{'prio':>5}  match"]
    for r in res["profiles"]:
        mark = "*" if r["name"] == res["pick"] else " "
        fit = (f"yes: {r['units']} units, {r['with_role']} of {r['files']} files have a role ({r['coverage']:.0%})"
               if r["matches"] and r.get("units") else "yes, but finds no units" if r["matches"] else "no")
        lines.append(f"{mark} {r['name']:<14}{r.get('language') or '':<12}{r['origin']:<10}{r['priority']:>5}  {fit}")
    g = res["inferred"]
    mark = "*" if res["pick"] == "inferred" else " "
    lines.append(f"{mark} {'inferred':<14}{'':<12}{'generic':<10}{'':>5}  "
                 + (f"{g['units']} units, {g['with_role']} of {g['files']} files have a role ({g['coverage']:.0%})"
                    if "error" not in g else g["error"]))
    lines.append("")
    lines.append(f"* is what `archplan profile infer` proposes. Apply another with `archplan profile use <name>`.")
    for e in res["errors"]:
        lines.append(f"skipped {e['file']}: {e['error']}")
    return "\n".join(lines)
