"""Check an archmodel file against the exporter plugin contract
(format/exporter-plugins.md): the shape of each section, references to
unknown ids, and paths that are absolute or leave the repository root. It
also reports which capabilities the model shows, next to the ones it
declares in source.capabilities."""

from __future__ import annotations

from pathlib import Path

CAPABILITIES = ["components", "types", "fields", "deps", "files", "calls", "signatures", "literals", "schema"]
MAX_LISTED = 20


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []
        self.counts = {}

    def err(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)


def bad_path(p, root: Path | None) -> str | None:
    """Why a model path is not allowed, or None."""
    if not isinstance(p, str) or not p:
        return "not a string"
    if p.startswith(("/", "\\", "~")) or (len(p) > 1 and p[1] == ":"):
        return "absolute"
    if "\\" in p:
        return "uses backslashes"
    if ".." in p.replace("\\", "/").split("/"):
        return "leaves the root"
    if root is not None:
        r = root.resolve()
        full = (r / p.split("*")[0]).resolve()
        if full != r and r not in full.parents:
            return "leaves the root (through a link)"
    return None


def check(m, root: Path | None = None) -> dict:
    """{"ok", "errors", "warnings", "capabilities": {"declared", "found"}, "counts"}."""
    r = Report()
    if not isinstance(m, dict):
        r.err("the file must hold a JSON object")
        return result(r, [], [])
    if m.get("format") != "archmodel/1":
        r.err(f"format must be \"archmodel/1\", not {m.get('format')!r}")
    sections = {"source": dict, "layers": list, "components": dict, "dependencies": list, "files": dict,
                "policy": dict, "types": dict, "sequences": dict, "vocabulary": dict, "notes": dict,
                "signatures": list, "literals": list, "enums": list, "routes": list,
                "schema": dict, "declarations": list}
    for k, t in sections.items():
        if k in m and m[k] is not None and not isinstance(m[k], t):
            r.err(f"{k} must be {'an object' if t is dict else 'a list'}")
    get = lambda k, t: m.get(k) if isinstance(m.get(k), t) else t()
    src, layers, comps, deps = get("source", dict), get("layers", list), get("components", dict), get("dependencies", list)
    files, types, seqs, policy = get("files", dict), get("types", dict), get("sequences", dict), get("policy", dict)

    declared = src.get("capabilities")
    if declared is not None:
        if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
            r.err("source.capabilities must be a list of strings")
            declared = []
        elif set(declared) - set(CAPABILITIES):
            r.err(f"unknown capabilities: {', '.join(sorted(set(declared) - set(CAPABILITIES)))}")

    paths = []  # (where, path)
    layer_ids = {x.get("id") for x in layers if isinstance(x, dict)}
    for i, x in enumerate(layers):
        if not isinstance(x, dict) or not isinstance(x.get("id"), str):
            r.err(f"layers[{i}] needs a string id")
    for cid, c in comps.items():
        if not isinstance(c, dict):
            r.err(f"component {cid}: must be an object")
            continue
        if c.get("kind", "internal") not in ("internal", "external"):
            r.err(f"component {cid}: kind must be internal or external")
        if c.get("layer") is not None and c["layer"] not in layer_ids:
            r.err(f"component {cid}: unknown layer {c['layer']!r}")
        if c.get("kind") != "external":
            if c.get("path") is not None:
                paths.append((f"component {cid} path", c["path"]))
            if c.get("match") is not None and not isinstance(c["match"], list):
                r.err(f"component {cid}: match must be a list")
            for p in c.get("match") or []:
                paths.append((f"component {cid} match", p))
    for i, d in enumerate(deps):
        if not isinstance(d, dict):
            r.err(f"dependencies[{i}]: must be an object")
            continue
        for k in ("from", "to"):
            if d.get(k) not in comps:
                r.err(f"dependency {d.get('from')} -> {d.get('to')}: unknown component {d.get(k)!r}")
        w = d.get("weight", 1)
        if not isinstance(w, int) or isinstance(w, bool) or w < 0:
            r.err(f"dependency {d.get('from')} -> {d.get('to')}: weight must be a whole number")
        for e in d.get("evidence") or []:
            paths.append((f"dependency {d.get('from')} -> {d.get('to')} evidence", str(e).rsplit(":", 1)[0]))
    for f, v in files.items():
        paths.append(("files key", f))
        if not isinstance(v, dict):
            r.err(f"files[{f}]: must be an object")
            continue
        if v.get("component") is not None and v["component"] not in comps:
            r.err(f"files[{f}]: unknown component {v['component']!r}")
        for ref in v.get("refs") or []:
            to = ref.get("to") if isinstance(ref, dict) else None
            paths.append((f"files[{f}] ref", to))
            if isinstance(to, str) and to not in files:
                r.warn(f"files[{f}]: refers to {to}, which has no entry of its own")
    for tid, t in types.items():
        if not isinstance(t, dict):
            r.err(f"type {tid}: must be an object")
            continue
        if t.get("component") is not None and t["component"] not in comps:
            r.err(f"type {tid}: unknown component {t['component']!r}")
        if t.get("file") is not None:
            paths.append((f"type {tid} file", t["file"]))
        for fld in t.get("fields") or []:
            if isinstance(fld, dict) and fld.get("ref") is not None and fld["ref"] not in types:
                r.err(f"type {tid}: field {fld.get('name')} refers to unknown type {fld['ref']!r}")
        for k in ("implements", "uses"):
            for x in t.get(k) or []:
                if x not in types:
                    r.err(f"type {tid}: {k} names unknown type {x!r}")
    for rule in policy.get("rules") or []:
        if isinstance(rule, dict):
            for k in ("from", "to"):
                if rule.get(k) is not None and rule[k] != "*" and rule[k] not in comps:
                    r.err(f"policy rule {rule.get('from')} -> {rule.get('to')}: unknown component {rule[k]!r}")
    for sid, s in seqs.items():
        if not isinstance(s, dict) or not isinstance(s.get("messages", []), list):
            r.err(f"sequence {sid}: needs a messages list")
            continue
        for i, msg in enumerate(s.get("messages") or []):
            rt = msg.get("route") if isinstance(msg, dict) else None
            if rt is not None and not (isinstance(rt, dict) and isinstance(rt.get("path"), str)):
                r.err(f"sequence {sid}: message {i} has a route without a path")
            if isinstance(msg, dict) and msg.get("span") is not None and not isinstance(msg["span"], str):
                r.err(f"sequence {sid}: message {i} has a span that is not a string")
    for i, rt in enumerate(get("routes", list)):
        if not isinstance(rt, dict) or not isinstance(rt.get("pattern"), str) or not isinstance(rt.get("method"), str):
            r.err(f"routes[{i}]: needs a method and a pattern")
            continue
        if rt.get("component") is not None and rt["component"] not in comps:
            r.err(f"routes[{i}]: unknown component {rt['component']!r}")
        if rt.get("file") is not None:
            paths.append((f"routes[{i}] file", rt["file"]))
    bad = 0
    for where, p in paths:
        why = bad_path(p, root)
        if why:
            bad += 1
            if bad <= MAX_LISTED:
                r.err(f"{where}: {p!r} is {why}")
    if bad > MAX_LISTED:
        r.err(f"… {bad - MAX_LISTED} more bad paths")

    internal = [c for c, v in comps.items() if isinstance(v, dict) and v.get("kind") != "external"]
    found = [c for c, ok in (("components", bool(internal)), ("types", bool(types)),
                             ("fields", any(isinstance(t, dict) and t.get("fields") for t in types.values())),
                             ("deps", bool(deps)), ("files", bool(files)), ("calls", bool(seqs)),
                             ("signatures", bool(m.get("signatures"))), ("literals", bool(m.get("literals"))),
                             ("schema", bool((m.get("schema") or {}).get("tables")))) if ok]
    if declared:
        for c in declared:
            if c not in found:
                r.warn(f"declares {c} but the model has none")
        for c in found:
            if c not in declared:
                r.warn(f"has {c} but does not declare it in source.capabilities")
    r.counts = {"components": len(internal), "dependencies": len(deps), "files": len(files), "types": len(types),
                "sequences": len(seqs)}
    return result(r, declared, found)


def result(r: Report, declared, found) -> dict:
    return {"ok": not r.errors, "errors": r.errors, "warnings": r.warnings[:MAX_LISTED] +
            ([f"… {len(r.warnings) - MAX_LISTED} more warnings"] if len(r.warnings) > MAX_LISTED else []),
            "capabilities": {"declared": declared, "found": found}, "counts": r.counts}


def text(res: dict) -> str:
    c = res["capabilities"]
    lines = [("valid" if res["ok"] else f"invalid: {len(res['errors'])} errors") + ", "
             + ", ".join(f"{v} {k}" for k, v in res["counts"].items()),
             "capabilities found: " + (", ".join(c["found"]) or "none")
             + ("; declared: " + ", ".join(c["declared"]) if c["declared"] is not None else "; none declared")]
    lines += [f"error: {e}" for e in res["errors"]]
    lines += [f"warning: {w}" for w in res["warnings"]]
    return "\n".join(lines)
