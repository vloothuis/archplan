"""Architecture profiles: the conventions a codebase follows inside its units.

A unit is a folder that repeats across the codebase: an Eyra system, a Django
app, a Phoenix context. A role is a kind of file a unit has, found by its
name: _public.ex, *_view.ex, models.py. Rules say who may call whom (only the
public API from other units) and which files come in pairs (every view has a
view builder).

archplan proposes a profile from the code. The human edits and locks it in
.archplan/architecture.json; from then on the analysis reports where the code
breaks it. File-level edges come from the model's `files` section.

Profile:

  {"version": 1, "status": "proposed" | "locked", "name", "source",
   "units": [{"kind", "glob", "require"?: [names], "exclude"?: [patterns], "except"?: [folder globs]}],
   "roles": [{"id", "name", "patterns", "color", "description"}],
   "rules": [{"id", "text", "type": "calls", "from", "to", "allow", "scope"}
             | {"id", "text", "type": "pair", "role", "needs", "match"}],
   "layers": [{"name", "components"}]}

Globs work per path segment: * matches within one folder name, ** matches any
number of folders. A role pattern without a slash matches the file name; one
with a slash matches the path inside the unit."""

from __future__ import annotations

import fnmatch
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from .hotspots import Mapper
from .project import Project, ProjectError, etag, read_json, write_json
from .safeio import model_base, prefix_of
from .store import stamp

FILE = "architecture.json"
MAX_BREAKS = 200
COLORS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#dc2626", "#0891b2", "#65a30d", "#db2777",
          "#4f46e5", "#ca8a04", "#0d9488", "#7c3aed", "#ea580c", "#475569"]
SOURCE_EXT = {".py", ".ex", ".exs", ".go", ".ts", ".tsx", ".js", ".jsx", ".rb", ".java", ".kt", ".cs", ".php", ".rs"}
SKIP_DIRS = {".git", "node_modules", "deps", "_build", "build", "dist", "vendor", "__pycache__", ".venv", "venv",
             ".archplan", ".ignored", "migrations"}

# Role names a person would use. Keys are the core of a file-name pattern.
NAMES = {"public": "Public API", "queries": "Queries", "switch": "Signal handler", "presenter": "Presenter",
         "routes": "Routes", "assembly": "Assembly", "view": "View", "view_builder": "View builder",
         "model": "Model", "form": "Form", "schema": "Schema", "builder": "Builder", "page": "Page",
         "models": "Models", "views": "Views", "forms": "Forms", "urls": "URLs", "admin": "Admin",
         "serializers": "Serializers", "api": "API", "filters": "Filters", "signals": "Signals",
         "tasks": "Tasks", "viewsets": "Viewsets", "services": "Services", "managers": "Managers", "templatetags": "Template tags",
         "commands": "Management commands", "validators": "Validators", "permissions": "Permissions"}
# Patterns that play the same role: a page is a view, a page builder a view builder.
MERGE = {"page": "view", "page_builder": "view_builder"}


class ArchitectureError(ProjectError):
    pass


# ---------- paths and globs ----------

def glob_match(path: str, pattern: str) -> bool:
    """Match path against a glob, one folder at a time; ** spans folders."""
    parts, pats = path.split("/"), []
    for x in pattern.strip("/").split("/"):
        if not (x == "**" and pats and pats[-1] == "**"):  # ** ** is the same as **
            pats.append(x)
    # Patterns come from the committed profile, so match in polynomial time:
    # ok[j] says whether parts[i:] matches pats[j:], filled from the end.
    n, m = len(parts), len(pats)
    nxt = [True] * (m + 1)  # i == n: only trailing ** match nothing
    for j in range(m - 1, -1, -1):
        nxt[j] = pats[j] == "**" and nxt[j + 1]
    for i in range(n - 1, -1, -1):
        ok = [False] * (m + 1)
        for j in range(m - 1, -1, -1):
            if pats[j] == "**":
                ok[j] = ok[j + 1] or nxt[j]
            else:
                ok[j] = nxt[j + 1] and fnmatch.fnmatchcase(parts[i], pats[j])
        nxt = ok
    return nxt[0]


def literal(pattern: str) -> int:
    """How specific a pattern is: its number of literal characters."""
    return len(re.sub(r"\*|\?|\[[^\]]*\]", "", pattern))


def role_match(rel: str, pattern: str) -> bool:
    if "/" in pattern:
        return glob_match(rel, pattern)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)


def source_files(model: dict, root: Path | None = None) -> list:
    """Repository-relative source files: from the model's files section and
    type files, or else from the file system under root, with the paths
    relative to the folder the model's paths are relative to."""
    fs = set((model.get("files") or {}).keys())
    fs |= {t["file"] for t in (model.get("types") or {}).values() if t.get("file")}
    if fs or root is None:
        return sorted(fs)
    pre = prefix_of(root, model_base(root, model))
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for n in names:
            if os.path.splitext(n)[1] in SOURCE_EXT:
                out.append(pre + os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/"))
    return sorted(out)


def edges_of(model: dict) -> dict:
    """{file: [(to file, line)]} from the model's files section."""
    return {f: [(r["to"], r.get("line")) for r in v.get("refs", [])] for f, v in (model.get("files") or {}).items()}


def dirs_of(files) -> set:
    out = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return out


# ---------- units and roles ----------

def units(profile: dict, files: list) -> list:
    """The profile's units with their files: [{id, kind, path, files: [(path, rel)]}].
    A file belongs to the deepest unit that contains it."""
    dirs = dirs_of(files)
    children = defaultdict(set)  # dir -> names of files and folders (folders end in /)
    for f in files:
        parts = f.split("/")
        for i in range(len(parts)):
            parent = "/".join(parts[:i])
            children[parent].add(parts[i] + ("/" if i < len(parts) - 1 else ""))
    found = {}
    for spec in profile.get("units") or []:
        req = spec.get("require") or []
        for d in sorted(dirs):
            if d in found or not glob_match(d, spec.get("glob", "")):
                continue
            if any(glob_match(d, x) for x in spec.get("except") or []):
                continue
            if req and not any(r in children[d] for r in req):
                continue
            found[d] = spec
    order = sorted(found, key=lambda d: -d.count("/"))
    out = {d: {"id": d, "kind": found[d].get("kind", "Unit"), "path": d, "files": []} for d in found}
    for f in files:
        d = next((u for u in order if f.startswith(u + "/")), None)
        if d is None:
            continue
        rel = f[len(d) + 1:]
        if any(glob_match(rel, x) for x in found[d].get("exclude") or []):
            continue
        out[d]["files"].append((f, rel))
    return [out[d] for d in sorted(out)]


def role_of(rel: str, roles: list) -> str | None:
    best, score = None, -1
    for r in roles:
        for pat in r.get("patterns") or []:
            if role_match(rel, pat) and literal(pat) > score:
                best, score = r["id"], literal(pat)
    return best


# ---------- analysis ----------

def analyse(profile: dict, model: dict, root: Path | None = None, cap: int = MAX_BREAKS) -> dict:
    """Units, role coverage and rule breaks. cap limits the breaks listed per rule."""
    files = source_files(model, root)
    us = units(profile, files)
    roles = profile.get("roles") or []
    mapper = Mapper(model.get("components") or {})
    unit_of, role_of_file, names_in = {}, {}, {}
    out_units = []
    for u in us:
        # Prefer the component of the unit's own folder; subpackages such as api/ can hold more files.
        top = Counter(c for c in (mapper(f) for f, rel in u["files"] if "/" not in rel) if c)
        comps = top or Counter(c for c in (mapper(f) for f, _ in u["files"]) if c)
        fs = []
        for f, rel in u["files"]:
            r = role_of(rel, roles)
            unit_of[f], role_of_file[f] = u["id"], r
            fs.append({"path": f, "role": r})
        names_in[u["id"]] = {rel.rsplit("/", 1)[-1] for _, rel in u["files"]}
        out_units.append({"id": u["id"], "kind": u["kind"], "path": u["path"],
                          "component": comps.most_common(1)[0][0] if comps else None, "files": fs,
                          "roles": sorted({x["role"] for x in fs if x["role"]})})
    edges = edges_of(model)
    checks, broken = Counter(), Counter()
    rule_out = []
    for rule in profile.get("rules") or []:
        rc, rb = Counter(), Counter()
        if rule.get("type") == "pair":
            n, breaks = pair_breaks(rule, out_units, names_in, rc, rb, cap)
        else:
            n, breaks = call_breaks(rule, edges, unit_of, role_of_file, rc, rb, cap)
        checks.update(rc)
        broken.update(rb)
        rule_out.append({"id": rule.get("id"), "text": rule.get("text") or rule_text(rule, roles),
                         "type": rule.get("type", "calls"), "count": n, "breaks": breaks[:cap],
                         "units_checked": len(rc), "units_broken": len(rb)})
    for u in out_units:
        u["conformance"] = round(1 - broken[u["id"]] / checks[u["id"]], 3) if checks[u["id"]] else 1.0
    n = len(out_units)
    in_layer = {c for layer in profile.get("layers") or [] for c in layer.get("components") or []}
    warnings = []
    if not model.get("files"):
        warnings.append("The model has no file-level references (files section); call rules cannot be checked. "
                        "Re-export with a current exporter.")
    return {"units": out_units,
            "roles": [{"id": r["id"], "present": sum(1 for u in out_units if r["id"] in u["roles"]), "of": n}
                      for r in roles],
            "rules": rule_out,
            "unmatched": sum(1 for u in out_units for f in u["files"] if f["role"] is None),
            "layers": {"unassigned": sorted(c for c in (model.get("components") or {})
                                            if c not in in_layer
                                            and (model["components"][c] or {}).get("kind") != "external")},
            "edges": bool(model.get("files")), "warnings": warnings}


def as_list(x) -> list:
    return x if isinstance(x, list) else [x] if x else []


def call_breaks(rule, edges, unit_of, role_of_file, checks, broken, cap: int = MAX_BREAKS) -> tuple:
    """Edges into unit files that break a calls rule. allow=true: matching
    edges may only reach the `to` roles. allow=false: they must not."""
    frm, to = as_list(rule.get("from") or "any"), set(as_list(rule.get("to")))
    scope, allow = rule.get("scope", "any"), rule.get("allow", True)
    if "other-units" in frm:
        scope = "other-units"
    n, out = 0, []
    for src, refs in edges.items():
        su = unit_of.get(src)
        for dst, line in refs:
            du = unit_of.get(dst)
            if du is None:
                continue
            same = su == du
            if (scope == "same-unit" and not same) or (scope == "other-units" and same):
                continue
            if not ("any" in frm or "other-units" in frm or role_of_file.get(src) in frm):
                continue
            owner = du if "other-units" in frm or su is None else su
            checks[owner] += 1
            dr = role_of_file.get(dst)
            if (dr not in to) if allow else (dr in to):
                n += 1
                broken[owner] += 1
                if len(out) < cap:
                    out.append({"unit": owner, "from": src, "to": dst,
                                "detail": f"{role_label(role_of_file.get(src), su)} -> {dr or 'no role'}"
                                          + (f" (line {line})" if line else "")})
    return n, out


def role_label(role, unit) -> str:
    return role or ("no role" if unit else "outside any unit")


def pair_specs(rule) -> list:
    """[(regex for the file name, template for its partner)] from "L -> R" strings."""
    out = []
    for m in as_list(rule.get("match")):
        left, _, right = m.partition("->")
        left, right = left.strip(), right.strip()
        if left.count("<stem>") != 1 or not right:  # one stem: a single group, linear matching
            continue
        rx = re.compile("^" + re.escape(left).replace(re.escape("<stem>"), "(?P<stem>.+)") + "$")
        out.append((rx, right))
    return out


def pair_breaks(rule, out_units, names_in, checks, broken, cap: int = MAX_BREAKS) -> tuple:
    specs = pair_specs(rule)
    n, out = 0, []
    for u in out_units:
        has_need = any(f["role"] == rule.get("needs") for f in u["files"])
        for f in u["files"]:
            if f["role"] != rule.get("role"):
                continue
            name = f["path"].rsplit("/", 1)[-1]
            want = None
            for rx, right in specs:
                m = rx.match(name)
                if m:
                    want = right.replace("<stem>", m.group("stem"))
                    break
            if specs and want is None:
                continue  # this file's name is not one the rule pairs
            ok = want in names_in[u["id"]] if want else has_need
            checks[u["id"]] += 1
            if not ok:
                n += 1
                broken[u["id"]] += 1
                if len(out) < cap:
                    out.append({"unit": u["id"], "from": f["path"],
                                "to": (u["path"] + "/" + want) if want else None,
                                "detail": f"no {want or rule.get('needs')}"})
    return n, out


def rule_text(rule, roles) -> str:
    name = {r["id"]: r.get("name", r["id"]) for r in roles}
    nm = lambda x: "other units" if x == "other-units" else "any file" if x == "any" else name.get(x, x)
    if rule.get("type") == "pair":
        return f"Every {nm(rule.get('role'))} has a matching {nm(rule.get('needs'))}"
    frm = as_list(rule.get("from") or "any")
    who = "Other units" if frm == ["other-units"] else "Any file" if frm == ["any"] else \
        " and ".join(nm(x) for x in frm) + " files"
    to = as_list(rule.get("to"))
    to = ", ".join(nm(x) for x in to[:-1]) + " or " + nm(to[-1]) if len(to) > 1 else nm(to[0]) if to else "?"
    where = " of their own unit" if rule.get("scope") == "same-unit" else ""
    if rule.get("allow", True):
        return f"{who} call only {to}{where}"
    return f"{who} do not call {to}{where}"


# ---------- inference ----------

def tokens(rel: str) -> set:
    """Name shapes of a file inside a candidate unit: its exact name, its last
    one or two name parts (*_view.ex, *_view_builder.ex), and its first folder."""
    name = rel.rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(name)
    out = {("name", name)}
    parts = stem.lstrip("_").split("_")
    if len(parts) >= 2:
        out.add(("suffix", "*_" + parts[-1] + ext))
    if len(parts) >= 3:
        out.add(("suffix", "*_" + "_".join(parts[-2:]) + ext))
    if "/" in rel:
        out.add(("dir", rel.split("/", 1)[0] + "/**"))
    return out


def token_key(tok) -> str:
    kind, pat = tok
    if kind == "dir":
        return pat[:-3]
    stem = os.path.splitext(pat)[0]
    return stem.lstrip("*").lstrip("_")


def candidates(files: list, min_units: int = 4) -> list:
    """Folders whose subfolders share file-name shapes: [(score, parent, {child: [rel]})]."""
    by_parent = defaultdict(lambda: defaultdict(list))
    for f in files:
        parts = f.split("/")
        for i in range(1, len(parts) - 1):
            by_parent["/".join(parts[:i])][parts[i]].append("/".join(parts[i + 1:]))
    out = []
    for parent, kids in by_parent.items():
        if len(kids) < min_units:
            continue
        cover = Counter(t for rels in kids.values() for t in {t for r in rels for t in tokens(r)})
        need = max(3, 0.3 * len(kids))
        score = sum(c for t, c in cover.items() if c >= need)
        if score:
            out.append((score, parent, kids))
    return sorted(out, key=lambda x: (-x[0], x[1]))


def singular(word: str) -> str:
    w = word.replace("_", " ").replace("-", " ").strip()
    w = w[:-3] + "y" if w.endswith("ies") else w[:-1] if w.endswith("s") and not w.endswith("ss") else w
    return w[:1].upper() + w[1:] if w else "Unit"


def infer(model: dict, root: Path | None = None, project_dir: Path | None = None) -> dict:
    """A proposed profile: the best framework profile that matches the code
    (contrib/profiles, or the project's .archplan/profiles), unless the
    profile inferred from recurring folder and file-name shapes fits much
    better; else that inferred profile."""
    from . import frameworks
    files = source_files(model, root)
    fw = frameworks.matches(model, files, root, project_dir)
    try:
        gen = generic(model, files)
    except ArchitectureError:
        if not fw:
            raise
        gen = None
    return frameworks.choose(fw, gen, files) or gen


def use(model: dict, name: str, root: Path | None = None, project_dir: Path | None = None) -> dict:
    """The proposal of one named framework profile, or "inferred"."""
    from . import frameworks
    files = source_files(model, root)
    if name == "inferred":
        return generic(model, files)
    ts, _ = frameworks.load(project_dir)
    if name not in ts:
        raise ArchitectureError(f"no profile named {name}; see `archplan profile list`")
    return frameworks.apply(ts[name], model, files)


def generic(model: dict, files: list) -> dict:
    cands = candidates(files)
    if not cands:
        raise ArchitectureError("found no folders whose subfolders repeat the same file names")
    _, parent, kids = cands[0]
    n = len(kids)
    need = max(3, 0.3 * n)
    rels = {k: v for k, v in kids.items()}
    # Most specific shapes claim their files first; a broader shape keeps only what is left.
    order = sorted({t for v in rels.values() for r in v for t in tokens(r)},
                   key=lambda t: ({"name": 0, "suffix": 1, "dir": 2}[t[0]], -literal(t[1]), t[1]))
    claimed = {k: set() for k in rels}
    roles = {}
    for tok in order:
        hit = {k: {r for r in v if tok in tokens(r) and r not in claimed[k]} for k, v in rels.items()}
        units_with = [k for k, v in hit.items() if v]
        if len(units_with) < need:
            continue
        key = token_key(tok)
        key = MERGE.get(key, key)
        if not re.match(r"^[A-Za-z][\w-]*$", key):
            continue
        role = roles.setdefault(key, {"id": key, "name": NAMES.get(key, key.replace("_", " ").capitalize()),
                                      "patterns": [], "units": set()})
        role["patterns"].append(tok[1])
        role["units"] |= set(units_with)
        for k, v in hit.items():
            claimed[k] |= v
    kind = singular(parent.rsplit("/", 1)[-1])
    out_roles = []
    for i, r in enumerate(sorted(roles.values(), key=lambda r: (-len(r["units"]), r["id"]))):
        out_roles.append({"id": r["id"], "name": r["name"], "patterns": r["patterns"], "color": COLORS[i % len(COLORS)],
                          "description": f"Files named {', '.join(r['patterns'])}. In {len(r['units'])} of {n} units."})
    profile = {"version": 1, "status": "proposed", "name": f"{model.get('source', {}).get('repo') or 'Project'} "
                                                           f"{kind.lower()}s",
               "source": "inferred", "inferred": stamp(),
               "units": [{"kind": kind, "glob": parent + "/*"}], "roles": out_roles, "rules": [], "layers": []}
    profile["rules"] = infer_rules(profile, model, files)
    profile["layers"] = infer_layers(model)
    return profile


def infer_rules(profile: dict, model: dict, files: list, hold: float = 0.9) -> list:
    """Rules that hold for most of the code: who other units call, which roles
    never call which in the same unit, and which files come in pairs."""
    us = units(profile, files)
    roles = profile["roles"]
    unit_of, role_at = {}, {}
    for u in us:
        for f, rel in u["files"]:
            unit_of[f], role_at[f] = u["id"], role_of(rel, roles)
    edges = edges_of(model)
    rules = []
    # 1. Who other units call.
    into = Counter()
    for src, refs in edges.items():
        for dst, _ in refs:
            if dst in unit_of and unit_of.get(src) != unit_of[dst]:
                into[role_at[dst]] += 1
    total = sum(into.values())
    if total >= 20:
        top = [r for r, _ in into.most_common() if r][:2]
        for k in (1, 2):
            share = sum(into[r] for r in top[:k]) / total
            if len(top) >= k and share >= hold:
                rules.append({"id": "other-units-call-" + "-".join(top[:k]), "type": "calls", "from": "other-units",
                              "to": top[:k] if k > 1 else top[0], "allow": True, "scope": "other-units",
                              "support": round(share, 3)})
                break
    # 2. Roles that never call another role of the same unit, where others do call it.
    has = defaultdict(set)
    for u in us:
        for f, _ in u["files"]:
            if role_at[f]:
                has[u["id"]].add(role_at[f])
    calls = defaultdict(set)  # (from role, to role) -> units where it happens
    called = defaultdict(set)  # role -> units where some other role of the unit calls it
    calling = defaultdict(set)  # role -> units where it calls some other role of the unit
    for src, refs in edges.items():
        for dst, _ in refs:
            if dst in unit_of and unit_of.get(src) == unit_of[dst] and role_at.get(src) and role_at[dst]:
                calls[(role_at[src], role_at[dst])].add(unit_of[dst])
                if role_at[src] != role_at[dst]:
                    called[role_at[dst]].add(unit_of[dst])
                    calling[role_at[src]].add(unit_of[dst])
    ids = [r["id"] for r in roles]
    never = []
    for a in ids:
        with_a = [u for u in has if a in has[u]]
        if len(calling[a]) < 0.3 * len(with_a):
            continue  # a calls little in its unit: what it leaves out says nothing
        nots = []
        for b in ids:
            both = [u for u in has if a in has[u] and b in has[u]]
            if a == b or len(both) < max(5, 0.3 * len(us)):
                continue
            with_b = [u for u in has if b in has[u]]
            if len(called[b]) < 0.3 * len(with_b):
                continue  # nobody calls b much: not calling it says nothing
            if len(calls[(a, b)] & set(both)) <= (1 - hold) * len(both):
                nots.append(b)
        if nots:
            never.append((len(with_a), {"id": f"{a}-not-" + "-".join(nots), "type": "calls", "from": a,
                                        "to": nots if len(nots) > 1 else nots[0], "allow": False,
                                        "scope": "same-unit"}))
    rules += [r for _, r in sorted(never, key=lambda x: -x[0])[:6]]
    # 3. Files that come in pairs: <stem>_view.ex and <stem>_view_builder.ex.
    by_id = {r["id"]: r for r in roles}
    names = {u["id"]: {rel.rsplit("/", 1)[-1] for _, rel in u["files"]} for u in us}
    for a in ids:
        for b in ids:
            if a == b:
                continue
            match, hits, total = [], 0, 0
            for pa in by_id[a]["patterns"]:
                for pb in by_id[b]["patterns"]:
                    if not (pa.startswith("*_") and pb.startswith("*_")):
                        continue
                    sa, ext = os.path.splitext(pa[2:])
                    sb, extb = os.path.splitext(pb[2:])
                    if ext != extb or not sb.startswith(sa + "_"):
                        continue
                    spec = f"<stem>_{sa}{ext} -> <stem>_{sb}{ext}"
                    rx = pair_specs({"match": spec})[0]
                    h = t = 0
                    for u in us:
                        for f, rel in u["files"]:
                            m = rx[0].match(rel.rsplit("/", 1)[-1])
                            if m and role_at[f] == a:
                                t += 1
                                h += rx[1].replace("<stem>", m.group("stem")) in names[u["id"]]
                    if t:
                        match.append(spec)
                        hits, total = hits + h, total + t
            if match and total >= 5 and hits / total >= 0.7:
                rules.append({"id": f"{a}-has-{b}", "type": "pair", "role": a, "needs": b, "match": match,
                              "support": round(hits / total, 3)})
    for r in rules:
        r["text"] = rule_text(r, roles)
    return rules


def infer_layers(model: dict) -> list:
    """Layers from the model's own layers, else from the first part of
    component ids, ordered so that groups that depend on others come first."""
    comps = {c: v or {} for c, v in (model.get("components") or {}).items() if (v or {}).get("kind") != "external"}
    if len(model.get("layers") or []) > 1 and any(v.get("layer") for v in comps.values()):
        return [{"name": layer.get("name", layer["id"]), "id": layer["id"],
                 "components": sorted(c for c, v in comps.items() if v.get("layer") == layer["id"])}
                for layer in model["layers"]]
    split = {c: re.split(r"[./]", c) for c in comps}
    # Descend while one name part holds most components: openzaak.* -> openzaak.components, openzaak.utils, ...
    prefix = []
    while True:
        under = [c for c in comps if split[c][:len(prefix)] == prefix and len(split[c]) > len(prefix) + 1]
        top = Counter(split[c][len(prefix)] for c in under).most_common(1)
        if not top or top[0][1] < 0.8 * len(comps):
            break
        prefix.append(top[0][0])
    group = {c: split[c][len(prefix)] if split[c][:len(prefix)] == prefix and len(split[c]) > len(prefix)
             else split[c][-1] for c in comps}
    groups = set(group.values())
    if not 2 <= len(groups) <= 16:
        return []
    out_deg, in_deg = Counter(), Counter()
    for d in model.get("dependencies") or []:
        a, b = group.get(d["from"]), group.get(d["to"])
        if a and b and a != b:
            out_deg[a] += 1
            in_deg[b] += 1
    order = sorted(groups, key=lambda g: (in_deg[g] - out_deg[g], g))
    return [{"name": g, "components": sorted(c for c in comps if group[c] == g)} for g in order]


# ---------- the project's profile ----------

# The profile is committed repo data, so colours are checked before they reach SVG markup.
COLOR = re.compile(r"#[0-9a-fA-F]{3,8}")


def clean_colors(profile):
    """Drop role colours that are not hex colours, for profiles read from disk."""
    for r in (profile or {}).get("roles", []) if isinstance(profile, dict) else []:
        if isinstance(r, dict) and "color" in r and not COLOR.fullmatch(str(r["color"])):
            del r["color"]
    return profile


def validate(profile) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    for key in ("units", "roles", "rules", "layers"):
        if not isinstance(profile.get(key, []), list):
            raise ValueError(f"{key} must be a list")
    ids = [r.get("id") for r in profile.get("roles", []) if isinstance(r, dict)]
    if len(ids) != len(profile.get("roles", [])) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("every role needs an id")
    if len(set(ids)) != len(ids):
        raise ValueError("role ids must be unique")
    for r in profile.get("roles", []):
        if not isinstance(r.get("patterns", []), list):
            raise ValueError(f"role {r['id']}: patterns must be a list")
        if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in r.get("patterns", [])):
            raise ValueError(f"role {r['id']}: patterns must be strings of at most 200 characters")
        if "color" in r and not COLOR.fullmatch(str(r["color"])):
            raise ValueError(f"role {r['id']}: color must be a hex colour such as #3b82f6")
    for u in profile.get("units", []):
        if not isinstance(u, dict) or not u.get("glob"):
            raise ValueError("every unit needs a glob")
    known = set(ids) | {"any", "other-units"}
    for r in profile.get("rules", []):
        if not isinstance(r, dict) or not r.get("id"):
            raise ValueError("every rule needs an id")
        if r.get("type", "calls") == "pair":
            bad = [x for x in (r.get("role"), r.get("needs")) if x not in ids]
        elif r.get("type", "calls") == "calls":
            bad = [x for x in as_list(r.get("from") or "any") + as_list(r.get("to")) if x not in known]
            if r.get("scope", "any") not in ("same-unit", "other-units", "any"):
                raise ValueError(f"rule {r['id']}: scope must be same-unit, other-units or any")
        else:
            raise ValueError(f"rule {r['id']}: type must be calls or pair")
        if bad:
            raise ValueError(f"rule {r['id']} names unknown roles: {', '.join(map(str, bad))}")
    return profile


class Architecture:
    """The profile in .archplan/architecture.json, and its analysis."""

    def __init__(self, p: Project):
        self.p = p
        self.path = p.path(FILE)
        self._cache = (None, None)

    def profile(self) -> dict | None:
        return clean_colors(read_json(self.path))

    def model(self) -> dict:
        m = read_json(self.p.model_path)
        if not m:
            raise ArchitectureError("no model yet; run the export first")
        return m

    def save(self, profile: dict, status: str | None = None) -> dict:
        profile = validate(dict(profile))
        old = self.profile() or {}
        profile["version"] = 1
        profile["status"] = status or old.get("status") or profile.get("status") or "proposed"
        profile["updated"] = stamp()
        write_json(self.path, profile)
        return profile

    def lock(self) -> dict:
        prof = self.profile()
        if not prof:
            raise ArchitectureError("no profile to lock in; save or infer one first")
        prof["locked"] = stamp()
        return self.save(prof, "locked")

    def infer(self) -> tuple:
        """(proposal, saved): the proposal replaces a profile that is not locked."""
        return self.propose(infer(self.model(), self.p.root, self.p.dir))

    def use(self, name: str) -> tuple:
        return self.propose(use(self.model(), name, self.p.root, self.p.dir))

    def propose(self, proposal: dict) -> tuple:
        cur = self.profile()
        if cur and cur.get("status") == "locked":
            return proposal, False
        write_json(self.path, proposal)
        return proposal, True

    def state(self) -> dict:
        """{"profile", "analysis"} and, without a saved profile, "proposal"."""
        key = (etag(self.path), etag(self.p.model_path))
        if self._cache[0] == key:
            return self._cache[1]
        model = self.model()
        prof = self.profile()
        out = {"profile": prof}
        if prof is None:
            out["proposal"] = infer(model, self.p.root, self.p.dir)
        out["analysis"] = analyse(prof or out["proposal"], model, self.p.root)
        self._cache = (key, out)
        return out


def summary(state: dict, breaks: int = 5) -> dict:
    """A compact form for an agent."""
    prof = state.get("profile") or state.get("proposal") or {}
    a = state["analysis"]
    roles = {r["id"]: r for r in prof.get("roles", [])}
    units = sorted(a["units"], key=lambda u: u["conformance"])
    return {"status": prof.get("status") if state.get("profile") else "not saved (proposal)",
            "name": prof.get("name"), "source": prof.get("source"),
            "units": [{"kind": u.get("kind"), "glob": u.get("glob")} for u in prof.get("units", [])],
            "unit_count": len(a["units"]),
            "roles": [{"id": r["id"], "name": roles.get(r["id"], {}).get("name"),
                       "patterns": roles.get(r["id"], {}).get("patterns"), "present": f"{r['present']} of {r['of']}"}
                      for r in a["roles"]],
            "unmatched_files": a["unmatched"],
            "rules": [{"id": r["id"], "text": r["text"], "breaks": r["count"], "examples": r["breaks"][:breaks]}
                      for r in a["rules"]],
            "least_conforming": [{"unit": u["id"], "conformance": u["conformance"]} for u in units[:5]],
            "layers": [x.get("name") for x in prof.get("layers", [])],
            "unassigned_components": a["layers"]["unassigned"][:20],
            "warnings": a.get("warnings", [])}


def text(state: dict) -> str:
    s = summary(state, 3)
    lines = [f"{s['name']} ({s['status']}): {s['unit_count']} units "
             + ", ".join(f"{u['kind']} {u['glob']}" for u in s["units"]), "", "Roles (present in units):"]
    for r in s["roles"]:
        lines.append(f"  {r['present']:>9}  {r['id']:<16} {', '.join(r['patterns'] or [])}")
    lines.append(f"  {s['unmatched_files']} files match no role")
    lines += ["", "Rules (breaks):"]
    for r in s["rules"]:
        lines.append(f"  {r['breaks']:5}  {r['text']}")
        for b in r["examples"]:
            lines.append(f"         {b['from']} -> {b.get('to') or ''}  ({b['detail']})")
    if s["layers"]:
        lines += ["", "Layers: " + " > ".join(s["layers"])
                  + (f"; {len(state['analysis']['layers']['unassigned'])} components unassigned"
                     if state["analysis"]["layers"]["unassigned"] else "")]
    for w in s["warnings"]:
        lines += ["", "Note: " + w]
    return "\n".join(lines)
