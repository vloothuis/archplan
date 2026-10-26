"""Several exporters for one project: their entries in config.json, and how
their partial models become one.

`export` in .archplan/config.json is a command, or a list of entries. An
entry is a command, or {"cmd": "...", "prefix": "web"}. Each command writes
a partial archmodel to the path archplan puts in place of {out}, or prints
it on standard output. archplan merges the parts in list order.

A prefix renames the part's component ids to "<prefix>/<id>", everywhere the
part names a component. Component ids of different parts must not collide,
and neither may type or sequence ids: merge() names the colliding id and
both commands."""

from __future__ import annotations

import json
import re

PREFIX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}")
LISTS = ("signatures", "literals", "enums", "routes")


class MergeError(ValueError):
    pass


def entries(export) -> list:
    """The export setting as [{"cmd", "prefix"}]: one entry for a string,
    none for nothing. Raises MergeError for a malformed setting."""
    if export is None or export == "":
        return []
    if isinstance(export, str):
        return [{"cmd": export, "prefix": None}]
    if not isinstance(export, list) or not export:
        raise MergeError('"export" must be a command, or a non-empty list of commands and {"cmd", "prefix"} objects')
    out = []
    for i, e in enumerate(export):
        if isinstance(e, str):
            e = {"cmd": e}
        if not isinstance(e, dict) or not isinstance(e.get("cmd"), str) or not e["cmd"].strip():
            raise MergeError(f'export[{i}] must be a command, or an object with a "cmd" command')
        extra = set(e) - {"cmd", "prefix"}
        if extra:
            raise MergeError(f"export[{i}] has unknown keys: {', '.join(sorted(extra))}")
        prefix = e.get("prefix")
        if prefix is not None and (not isinstance(prefix, str) or not PREFIX.fullmatch(prefix)):
            raise MergeError(f"export[{i}]: prefix must be 1 to 40 letters, digits, '_', '.' or '-'")
        out.append({"cmd": e["cmd"], "prefix": prefix})
    prefixes = [e["prefix"] for e in out if e["prefix"]]
    if len(prefixes) != len(set(prefixes)):
        raise MergeError("export: two entries have the same prefix")
    return out


def trust_text(export) -> str:
    """The text an approval of the export setting covers: the command itself,
    or the whole list in a canonical form, so any change to the list needs a
    new approval."""
    if isinstance(export, str):
        return export
    return "export-list\0" + json.dumps(entries(export), sort_keys=True, separators=(",", ":"))


def prefixed(m: dict, prefix: str | None) -> dict:
    """m with its component ids renamed to "<prefix>/<id>"."""
    if not prefix:
        return m
    comps = m.get("components") if isinstance(m.get("components"), dict) else {}
    ren = {c: f"{prefix}/{c}" for c in comps}

    def r(c):
        return ren.get(c, c) if isinstance(c, str) else c

    m["components"] = {ren[c]: v for c, v in comps.items()}
    for d in m.get("dependencies") or []:
        if isinstance(d, dict):
            d["from"], d["to"] = r(d.get("from")), r(d.get("to"))
    for v in (m.get("files") or {}).values():
        if isinstance(v, dict) and "component" in v:
            v["component"] = r(v["component"])
    for t in (m.get("types") or {}).values():
        if isinstance(t, dict) and "component" in t:
            t["component"] = r(t["component"])
    for k in LISTS:
        for x in m.get(k) or []:
            if isinstance(x, dict) and "component" in x:
                x["component"] = r(x["component"])
    for rule in (m.get("policy") or {}).get("rules") or []:
        if isinstance(rule, dict):
            rule["from"], rule["to"] = r(rule.get("from")), r(rule.get("to"))
    for s in (m.get("sequences") or {}).values():
        if not isinstance(s, dict):
            continue
        s["participants"] = [r(p) for p in s.get("participants") or []]
        for msg in s.get("messages") or []:
            if isinstance(msg, dict):
                msg["from"], msg["to"] = r(msg.get("from")), r(msg.get("to"))
    voc = m.get("vocabulary") if isinstance(m.get("vocabulary"), dict) else {}
    for c in (voc.get("concepts") or {}).values():
        if isinstance(c, dict) and "owner" in c:
            c["owner"] = r(c["owner"])
    if isinstance(voc.get("components"), dict):
        voc["components"] = {r(c): v for c, v in voc["components"].items()}
    for u in voc.get("usage") or []:
        if isinstance(u, dict):
            u["component"] = r(u.get("component"))
    sch = m.get("schema") if isinstance(m.get("schema"), dict) else {}
    for mp in (sch.get("maps") or {}).values():
        if isinstance(mp, dict) and "component" in mp:
            mp["component"] = r(mp["component"])
    for a in sch.get("access") or []:
        if isinstance(a, dict):
            a["component"] = r(a.get("component"))
    if isinstance(m.get("notes"), dict):
        m["notes"] = {(f"comp:{r(k[5:])}" if k.startswith("comp:") else k): v for k, v in m["notes"].items()}
    return m


def merge(parts: list) -> dict:
    """One model from [(entry, partial model)], in order. Raises MergeError
    when two parts use the same component, type or sequence id."""
    if len(parts) == 1:
        return prefixed(parts[0][1], parts[0][0]["prefix"])
    out = {"format": "archmodel/1", "source": {}, "layers": [], "components": {}, "dependencies": [], "files": {},
           "types": {}, "sequences": {}}
    owner = {}  # (section, id) -> command
    languages, caps, generators = [], [], []
    policies = []
    for e, m in parts:
        m = prefixed(m, e["prefix"])
        src = m.get("source") if isinstance(m.get("source"), dict) else {}
        if not out["source"]:
            out["source"] = {k: v for k, v in src.items() if k not in ("languages", "capabilities", "generator")}
        languages += [x for x in src.get("languages") or [] if x not in languages]
        caps += [x for x in src.get("capabilities") or [] if x not in caps]
        if src.get("generator"):
            generators.append(src["generator"])
        for section, what in (("components", "component"), ("types", "type"), ("sequences", "sequence")):
            for k, v in (m.get(section) or {}).items():
                if (section, k) in owner:
                    hint = ' Give one of them a "prefix".' if section == "components" else ""
                    raise MergeError(f"{what} id {k!r} comes from two exporters: {owner[(section, k)]!r} and "
                                     f"{e['cmd']!r}.{hint}")
                owner[(section, k)] = e["cmd"]
                out[section][k] = v
        ids = [x.get("id") for x in out["layers"]]
        out["layers"] += [x for x in m.get("layers") or [] if isinstance(x, dict) and x.get("id") not in ids]
        out["dependencies"] += [d for d in m.get("dependencies") or [] if isinstance(d, dict)]
        for f, v in (m.get("files") or {}).items():
            if f in out["files"] and isinstance(v, dict):
                previous = out["files"][f]
                if previous.get("module") and v.get("module") and previous["module"] != v["module"]:
                    raise MergeError(f"file {f!r} has conflicting logical module identities")
                if "module" in v:
                    previous.setdefault("module", v["module"])
                if "moduleImports" in v:
                    imports = previous.setdefault("moduleImports", [])
                    for imp in v["moduleImports"]:
                        if imp not in imports:
                            imports.append(imp)
                have = {r.get("to") for r in out["files"][f].get("refs") or [] if isinstance(r, dict)}
                out["files"][f].setdefault("refs", []).extend(
                    r for r in v.get("refs") or [] if isinstance(r, dict) and r.get("to") not in have)
            else:
                out["files"][f] = v
        for k in LISTS:
            if m.get(k):
                out.setdefault(k, []).extend(m[k])
        if isinstance(m.get("policy"), dict):
            policies.append((m["policy"], [c for c, v in (m.get("components") or {}).items()
                                           if isinstance(v, dict) and v.get("kind") != "external"]))
        sch = m.get("schema")
        if isinstance(sch, dict) and sch.get("tables") and not (out.get("schema") or {}).get("tables"):
            out["schema"] = sch
        voc = m.get("vocabulary")
        if isinstance(voc, dict):
            ov = out.setdefault("vocabulary", {})
            for k in ("concepts", "components"):
                if isinstance(voc.get(k), dict):
                    ov.setdefault(k, {}).update(voc[k])
            ov.setdefault("usage", []).extend(voc.get("usage") or [])
        if isinstance(m.get("notes"), dict):
            out.setdefault("notes", {}).update(m["notes"])
    out["source"].update(languages=languages, capabilities=caps, generator=" + ".join(generators) or "archplan")
    out["policy"] = merge_policies(policies)
    for k in ("files", "types", "sequences"):
        if not out[k]:
            del out[k]
    return out


def merge_policies(policies: list) -> dict:
    """One policy from [(policy, internal component ids)]. When any part
    denies by default, the merged policy does too, and every component of a
    part that allows by default gets a rule that keeps its dependencies
    allowed. Deny rules still win over that rule."""
    rules = [r for p, _ in policies for r in p.get("rules") or [] if isinstance(r, dict)]
    if not any(p.get("default") == "deny" for p, _ in policies):
        return {"default": "allow", "rules": rules}
    for p, comps in policies:
        if p.get("default", "allow") == "allow":
            rules += [{"from": c, "to": "*", "effect": "allow", "note": "its exporter allows by default"}
                      for c in comps]
    enforcer = next((p["enforcer"] for p, _ in policies if p.get("default") == "deny" and p.get("enforcer")), None)
    return {"default": "deny", "rules": rules, **({"enforcer": enforcer} if enforcer else {})}
