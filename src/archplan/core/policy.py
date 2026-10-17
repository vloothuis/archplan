"""Pure policy and glossary checks over archmodel/1 data."""

from __future__ import annotations


def allowed(m: dict, f: str, t: str) -> bool:
    """A deny rule wins, then the default or an allow rule."""
    p = m.get("policy", {})
    rules = p.get("rules", [])
    hits = lambda r: r.get("from") in ("*", f) and r.get("to") in ("*", t)
    if any(r.get("effect") == "deny" and hits(r) for r in rules):
        return False
    return p.get("default", "allow") == "allow" or any(r.get("effect") == "allow" and hits(r) for r in rules)


def leaks(m: dict) -> list:
    """Usage rows where a component uses a concept it must not name."""
    v = m.get("vocabulary") or {}
    concepts, comps, out = v.get("concepts", {}), v.get("components", {}), []
    for u in v.get("usage", []):
        c, k = u.get("component"), u.get("concept")
        owner = concepts.get(k, {}).get("owner")
        if k not in concepts or c == owner or k in comps.get(c, {}).get("uses", []):
            continue
        if k in comps.get(c, {}).get("avoid", []):
            why = "avoid"
        elif concepts[k].get("kind") == "vendor":
            continue  # a vendor name is an everyday word too; layers.policy reports the imports
        elif owner and c in m.get("components", {}) and owner in m.get("components", {}) and not allowed(m, c, owner):
            why = "policy"
        else:
            continue
        out.append({**u, "owner": owner, "why": why})
    return out
