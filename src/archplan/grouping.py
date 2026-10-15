"""Components and layers from .archplan/config.json, applied to the as-is
model after any exporter ran.

  "components": {"probe/core": {"match": ["packages/probe/src/core/**"], "name": "probe core",
                                "layer": "framework", "doc": "..."}}
  "layers": [{"name": "framework", "components": ["probe/*"]}]

A file goes to the first configured component whose match pattern covers it,
in the order of the map. Patterns are relative to the project root, with the
glob rules of exclude.py. Files that match none keep the exporter's
component. archplan then moves the file's types, signatures, literals and
enums along, adds the configured components, drops exporter components that
lost all their files, and adds up the dependencies between the new
components again.

"layers" lists layers from top to bottom. Each names its components by id
or by a glob over ids. A configured component's "layer" puts it in that
layer too. The layers go into the model and seed the inferred profile.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import Path

from .project import ProjectError

EVIDENCE = 5
TEST_DIRS = {"test", "tests", "__tests__", "__mocks__", "e2e", "spec"}
GLOB = set("*?[")


class GroupingError(ProjectError):
    pass


def _strs(x, what: str) -> list:
    if not isinstance(x, list) or not x or not all(isinstance(v, str) and 0 < len(v) <= 500 for v in x):
        raise GroupingError(f"config.json: {what} must be a non-empty list of strings")
    return x


def specs_of(config: dict) -> list:
    """[(id, spec)] from config "components", in order; raises GroupingError."""
    comps = (config or {}).get("components")
    if comps is None:
        return []
    if not isinstance(comps, dict):
        raise GroupingError('config.json: "components" must be an object of component id to {"match": [...]}')
    out = []
    for cid, spec in comps.items():
        if not cid or not isinstance(spec, dict):
            raise GroupingError(f'config.json: component {cid!r} must be an object with "match"')
        _strs(spec.get("match"), f"components.{cid}.match")
        for k in ("name", "layer", "doc"):
            if k in spec and not isinstance(spec[k], str):
                raise GroupingError(f"config.json: components.{cid}.{k} must be a string")
        out.append((cid, spec))
    return out


def layers_of(config: dict) -> list:
    """[{"id", "name", "components"}] from config "layers"; raises GroupingError."""
    layers = (config or {}).get("layers")
    if layers is None:
        return []
    if not isinstance(layers, list):
        raise GroupingError('config.json: "layers" must be a list of {"name", "components"}')
    out = []
    for x in layers:
        if not isinstance(x, dict) or not isinstance(x.get("name"), str) or not x["name"]:
            raise GroupingError('config.json: every layer needs a "name"')
        comps = x.get("components", [])
        if not isinstance(comps, list) or not all(isinstance(c, str) for c in comps):
            raise GroupingError(f"config.json: layers.{x['name']}.components must be a list of strings")
        lid = x.get("id") if isinstance(x.get("id"), str) and x.get("id") else x["name"]
        out.append({"id": lid, "name": x["name"], "components": comps})
    return out


def config_layers(model: dict, config: dict) -> list:
    """The configured layers with the model's component ids in each, top to
    bottom: [{"id", "name", "components"}]. Empty without config layers or
    component layers."""
    layers = layers_of(config)
    specs = specs_of(config)
    if not layers and not any(s.get("layer") for _, s in specs):
        return []
    by_key = {}
    for x in layers:
        by_key.setdefault(x["id"], x)
        by_key.setdefault(x["name"], x)
    for _, s in specs:
        if s.get("layer") and s["layer"] not in by_key:
            x = {"id": s["layer"], "name": s["layer"], "components": []}
            layers.append(x)
            by_key[s["layer"]] = x
    from .architecture import wildcard
    comps = sorted(c for c, v in (model.get("components") or {}).items()
                   if not (isinstance(v, dict) and v.get("kind") == "external"))
    spec_layer = {cid: by_key[s["layer"]]["id"] for cid, s in specs if s.get("layer")}
    taken = {}
    for c in comps:
        if c in spec_layer:
            taken[c] = spec_layer[c]
            continue
        for x in layers:
            if any(c == p or wildcard(c, p) for p in x["components"]):
                taken[c] = x["id"]
                break
    return [{"id": x["id"], "name": x["name"], "components": [c for c in comps if taken.get(c) == x["id"]]}
            for x in layers]


def is_test(path: str) -> bool:
    from .vocab import is_test as test_name
    return test_name(path) or any(d in TEST_DIRS for d in path.split("/")[:-1])


def literal_dir(pattern: str) -> str:
    """The folders of a pattern before its first glob part: a/b for a/b/**/*.ts."""
    parts = pattern.split("/")
    lit = []
    for x in parts:
        if GLOB & set(x):
            break
        lit.append(x)
    else:
        return "/".join(parts)  # no glob: a folder or a file
    return "/".join(lit)


def _file_of(evidence) -> str | None:
    return str(evidence).rsplit(":", 1)[0] if isinstance(evidence, str) and evidence else None


def apply(model: dict, config: dict, root: Path) -> bool:
    """Apply config "components" and "layers" to model in place. Returns
    whether the model changed. Raises GroupingError for a malformed config."""
    specs = specs_of(config)
    changed = bool(specs) and regroup(model, specs, root)
    layers = config_layers(model, config)
    if layers:
        comps = model.get("components") or {}
        ids = {x["id"] for x in layers}
        for c, v in comps.items():
            if isinstance(v, dict) and v.get("kind") != "external" and v.get("layer") and v["layer"] not in ids:
                del v["layer"]
        for x in layers:
            for c in x["components"]:
                comps[c]["layer"] = x["id"]
        model["layers"] = [{"id": x["id"], "name": x["name"]} for x in layers]
        changed = True
    return changed


def regroup(model: dict, specs: list, root: Path) -> bool:
    from .exclude import Exclude
    from .hotspots import Mapper
    from .safeio import model_base, prefix_of
    pre = prefix_of(root, model_base(root, model))
    matchers = [(cid, Exclude(root, s["match"])) for cid, s in specs]
    comps = model.setdefault("components", {})
    files = model.get("files") if isinstance(model.get("files"), dict) else {}
    lists = [x for k in ("signatures", "literals", "enums") for x in (model.get(k) or []) if isinstance(x, dict)]
    types = [t for t in (model.get("types") or {}).values() if isinstance(t, dict)]

    # The exporter's component of every file the model names.
    orig = {}
    for f, v in files.items():
        if isinstance(v, dict) and v.get("component"):
            orig[f] = v["component"]
    for x in types + lists:
        if isinstance(x.get("file"), str) and x.get("component"):
            orig.setdefault(x["file"], x["component"])
    mapper = Mapper({c: v for c, v in comps.items() if isinstance(v, dict)})
    for d in model.get("dependencies") or []:
        for e in (d.get("evidence") or []) if isinstance(d, dict) else []:
            f = _file_of(e)
            if f and f not in orig:
                orig[f] = mapper(f) or d.get("from")

    def configured(f: str) -> str | None:
        return next((cid for cid, m in matchers if m.model_path(f, pre)), None)

    new = {f: configured(f) or c for f, c in orig.items()}
    moved = {f for f in orig if new[f] != orig[f]}
    used = Counter(new.values())
    had = Counter(orig.values())
    spec_ids = [cid for cid, _ in specs]
    if not moved and not any(cid in used for cid in spec_ids):
        return False

    # Component entries.
    lost = {c for c in had if c not in spec_ids and not used[c]}
    for c in lost:
        comps.pop(c, None)
    for cid, s in specs:
        if not used[cid]:
            continue
        old = comps.get(cid) if isinstance(comps.get(cid), dict) else {}
        pats = matchers[spec_ids.index(cid)][1].patterns
        dirs = [literal_dir(p) for p in pats]
        # In the model, a folder pattern is written as folder/**, as exporters do.
        match = [pre + p + ("/**" if not GLOB & set(p) and not os.path.isfile(root / p) else "") for p in pats]
        entry = {"name": s.get("name") or old.get("name") or cid.rsplit("/", 1)[-1],
                 "path": pre + dirs[0] if dirs[0] else (pre.rstrip("/") or "."), "match": match, "kind": "internal"}
        for k in ("layer", "doc"):
            if s.get(k) or old.get(k):
                entry[k] = s.get(k) or old[k]
        comps[cid] = entry
    for c, v in comps.items():
        if c not in spec_ids and isinstance(v, dict) and isinstance(v.get("match"), list):
            v["match"] = [p for p in v["match"] if p not in moved]

    # Files, types, signatures, literals and enums follow their file.
    for f, v in files.items():
        if isinstance(v, dict) and f in new:
            v["component"] = new[f]
    for x in types + lists:
        f = x.get("file")
        if isinstance(f, str) and f in new:
            x["component"] = new[f]
        elif x.get("component") in lost:
            x.pop("component", None)

    # Dependencies: edges between components nobody touched stay as they are;
    # the others are added up again.
    affected = {c for c in had if used[c] != had[c] or c in lost} | {cid for cid in spec_ids if used[cid]}
    successor = {}
    for c in lost:
        took = Counter(new[f] for f in orig if orig[f] == c)
        successor[c] = took.most_common(1)[0][0] if took else None
    kept, edges = [], {}  # untouched dependencies; (from, to) -> the added-up ones

    def add(a, b, weight, evidence, type_only=None):
        if not a or not b or a == b or a not in comps or b not in comps:
            return
        e = edges.setdefault((a, b), {"from": a, "to": b, "weight": 0, "evidence": []})
        e["weight"] += weight
        if type_only:
            e["typeOnly"] = e.get("typeOnly", 0) + type_only
        for x in evidence:
            if len(e["evidence"]) < EVIDENCE and x not in e["evidence"]:
                e["evidence"].append(x)

    have_refs = any(isinstance(v, dict) and v.get("refs") for v in files.values())
    for d in model.get("dependencies") or []:
        if not isinstance(d, dict):
            continue
        a, b = d.get("from"), d.get("to")
        weight = int(d["weight"]) if isinstance(d.get("weight"), int) else 1
        if a not in affected and b not in affected:
            kept.append(d)
            continue
        if have_refs and (b in had or b in spec_ids):
            continue  # an edge between project files: added up from the files section below
        # Split by the evidence files; the rest of the weight stays with the
        # group of the old source component.
        b2 = b if b in comps else successor.get(b)
        groups = defaultdict(list)
        for e in d.get("evidence") or []:
            f = _file_of(e)
            groups[new.get(f) or (a if a in comps else successor.get(a))].append(e)
        if not groups:
            add(a if a in comps else successor.get(a), b2, weight, [])
            continue
        if len(groups) == 1:
            (g, ev), = groups.items()
            add(g, b2, weight, ev, d.get("typeOnly"))
            continue
        home = a if a in groups else max(groups, key=lambda g: len(groups[g]))
        rest = max(0, weight - sum(len(v) for v in groups.values()))
        for g, ev in groups.items():
            add(g, b2, len(ev) + (rest if g == home else 0), ev)
    if have_refs:
        per = defaultdict(dict)  # (from, to) -> {source file: line}
        for f, v in sorted(files.items()):
            if not isinstance(v, dict) or is_test(f):
                continue
            a = new.get(f, v.get("component"))
            for r in v.get("refs") or []:
                b = new.get(r.get("to")) if isinstance(r, dict) else None
                if a and b and a != b and (a in affected or b in affected):
                    per[(a, b)].setdefault(f, r.get("line"))
        for (a, b), srcs in sorted(per.items()):
            add(a, b, len(srcs), [f"{f}:{line}" if line else f for f, line in list(srcs.items())[:EVIDENCE]])
    model["dependencies"] = sorted(kept + list(edges.values()), key=lambda d: (str(d.get("from")), str(d.get("to"))))
    return True
