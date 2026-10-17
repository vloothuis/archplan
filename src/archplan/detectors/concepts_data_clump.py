"""concepts.data_clump: the same group of field names in types of several
components, such as street, city and zip. The move is one value object."""

from __future__ import annotations

from collections import Counter, defaultdict

from .common import EXAMPLES, Weight, plural, types_focus
from .fields import key, plain_names, type_text

ID = "concepts.data_clump"
NAME = "Data clumps"
GROUP = "concepts"
NEEDS = {"fields"}
PARAMS = {"min_fields": 3, "min_components": 2, "max_findings": 20, "common_share": 0.05, "synonym_share": 0.7,
          "holder_share": 0.3, "same_type_share": 0.5}
SKIP_KINDS = ("enum", "function", "module", "alias")


def associations(names: set) -> set:
    """The names with user_id folded into user when both are there (an association and its key)."""
    return {n for n in names if not (n.endswith("_id") and n[:-3] in names)}


def holder(ctx, t: dict, share: float) -> bool:
    """A type whose fields are mostly interfaces: it holds services (Deps, App), not data."""
    fs = [f for f in t.get("fields") or [] if f.get("name")]
    n = sum(1 for f in fs if (ctx.types.get(f.get("ref")) or {}).get("kind") == "interface")
    return bool(fs) and n / len(fs) >= share


def field_type(t: dict, name: str) -> str:
    """The type of a field by its key: the referenced type's id, or the type text."""
    for f in t.get("fields") or []:
        if f.get("name") and key(f["name"]) == name:
            return f.get("ref") or type_text(f)
    return ""


def same_types(ctx, s: set, support: list) -> int:
    """How many of the field names have one type in all the types."""
    return sum(1 for n in s if len({field_type(ctx.types[t], n) for t in support}) == 1)


def clumps(ctx, params) -> list:
    """[(field names, [type ids])], biggest first."""
    types = {tid: t for tid, t in ctx.types.items()
             if t.get("component") in ctx.internal and t.get("kind") not in SKIP_KINDS
             and not holder(ctx, t, params["holder_share"])}
    names = {tid: associations({key(n) for n in plain_names(t)} - {""}) for tid, t in types.items()}
    freq = Counter(n for ns in names.values() for n in ns)
    cap = max(20, params["common_share"] * len(types))
    names = {tid: {n for n in ns if freq[n] <= cap} for tid, ns in names.items()}
    names = {tid: ns for tid, ns in names.items() if len(ns) >= params["min_fields"]}
    index = defaultdict(set)
    for tid, ns in names.items():
        for n in ns:
            index[n].add(tid)
    comp = {tid: types[tid]["component"] for tid in names}
    cands = set()
    for a in sorted(names):
        shared = Counter(b for n in names[a] for b in index[n] if b > a and comp[b] != comp[a])
        for b, k in shared.items():
            if k >= params["min_fields"]:
                cands.add(frozenset(names[a] & names[b]))
    out = []
    for s in cands:
        support = sorted(set.intersection(*(index[n] for n in s)))
        comps = {comp[t] for t in support}
        if len(comps) < params["min_components"]:
            continue
        # Types that are mostly this group are synonyms (types.synonyms), not a clump.
        if all(len(s) / len(names[t]) >= params["synonym_share"] for t in support):
            continue
        # The same names with other types are a naming habit, not one value.
        if same_types(ctx, s, support) < params["same_type_share"] * len(s):
            continue
        out.append((s, support, comps))
    out.sort(key=lambda x: (-len(x[0]) * len(x[2]), -len(x[0]), sorted(x[0])))
    kept = []
    for s, support, comps in out:
        if any(s < s2 and comps <= c2 for s2, _, c2 in kept):
            continue
        kept.append((s, support, comps))
    return kept[:params["max_findings"]]


def run(ctx, params) -> list:
    out = []
    for s, support, comps in clumps(ctx, params):
        fields = sorted(s)
        first = Counter(f.split("_")[0] for f in fields).most_common(1)[0]
        vo = first[0].capitalize() if first[1] == len(fields) else "a value object"
        shown = ", ".join(fields[:-1][:5]) + (", …" if len(fields) > 6 else "") + f" and {fields[-1]}"
        title = (f"The fields {shown} appear together in {plural(len(support), 'type')} across "
                 f"{plural(len(comps), 'component')}.")
        types = [{"type": t, "component": ctx.types[t]["component"]} for t in support[:EXAMPLES]]
        marked = {t: [f["name"] for f in ctx.types[t].get("fields") or [] if f.get("name")
                      and (key(f["name"]) in s or key(f["name"]).endswith("_id") and key(f["name"])[:-3] in s)]
                  for t in support}
        out.append(ctx.finding(
            tuple(fields), title,
            "In " + ", ".join(f"{ctx.types[t].get('name') or t} ({ctx.name(ctx.types[t]['component'])})"
                              for t in support[:6]) + (", …" if len(support) > 6 else "") + ".",
            "medium" if len(fields) >= 4 or len(comps) >= 3 else "weak",
            f"{plural(len(fields), 'field')} shared by {plural(len(support), 'type')} in "
            f"{plural(len(comps), 'component')}",
            sorted(comps), [ctx.types[t].get("file") for t in support],
            {"fields": fields, "types": types,
             "moves": [f"Introduce {vo if vo == 'a value object' else 'a ' + vo + ' value object'} with "
                       f"{', '.join(fields[:6])}, owned by one component, and use it in these types.",
                       "Move the rules about these fields (validation, formatting) into it."]},
            focus=types_focus(marked, sorted(comps)),
            weight=Weight(25, "data clump").spread(len(fields), "fields", per=8, cap=16, free=params["min_fields"] - 1)
            .spread(len(comps), per=8, cap=16, free=params["min_components"] - 1)
            .spread(len(support), "types", per=3, cap=6, free=2)))
    return out
