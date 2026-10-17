"""types.synonyms: types in different components with mostly the same field names."""

from __future__ import annotations

from collections import Counter, defaultdict

from .common import COMMON_FIELDS, EXAMPLES, Weight, comp_name, finding, types_focus
from .fields import key

ID = "types.synonyms"
NAME = "Synonym types"
GROUP = "types"
NEEDS = {"fields"}
SYNONYM = 0.7            # Jaccard share of field names
SYNONYM_MIN = 4          # fields a type needs, after the common ones, to be compared
STRONG_MIN = 6           # shared fields a strong finding needs
PARAMS = {"share": SYNONYM, "min_fields": SYNONYM_MIN}



def run(ctx, params) -> list:
    return synonym_findings(ctx.model, params["share"], params["min_fields"])


def names_of(t: dict) -> frozenset:
    return frozenset(f["name"] for f in t.get("fields") or [] if f.get("name") and key(f["name"]) not in COMMON_FIELDS)


def mirrors(types: list) -> bool:
    """Whether the types list the same fields in the same order, with the same
    plain types: a deliberate copy, such as a wire type converted with T(x)."""
    shape = lambda t: [(f.get("name"), f.get("type")) for f in t.get("fields") or []]
    return all(shape(t) == shape(types[0]) for t in types[1:])


def synonym_findings(model: dict, share: float = SYNONYM, min_fields: int = SYNONYM_MIN) -> list:
    """Groups of types in different components whose field names mostly match."""
    types = {tid: t for tid, t in (model.get("types") or {}).items()
             if t.get("component") and t.get("kind") not in ("interface", "enum", "function", "module", "alias")}
    names = {tid: names_of(t) for tid, t in types.items()}
    names = {tid: n for tid, n in names.items() if len(n) >= min_fields}
    index = defaultdict(set)
    for tid, n in names.items():
        for x in n:
            index[x].add(tid)
    parent = {}

    def root(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x
    best = {}
    for a in sorted(names):
        shared = Counter(b for x in names[a] for b in index[x] if b > a)
        for b, k in shared.items():
            if types[a]["component"] == types[b]["component"]:
                continue
            j = k / len(names[a] | names[b])
            if j >= share:
                parent[root(b)] = root(a)
                best[(a, b)] = j
    groups = defaultdict(list)
    for x in {x for pair in best for x in pair}:
        groups[root(x)].append(x)
    out = []
    for members in groups.values():
        members.sort()
        common = frozenset.intersection(*(names[m] for m in members))
        js = [j for (a, b), j in best.items() if a in members]
        low = min(js)
        title = ", ".join(f"{types[m].get('name') or m} ({comp_name(model, types[m]['component'])})"
                          for m in members[:-1])
        title = (title + " and " if title else "") + \
            f"{types[members[-1]].get('name') or members[-1]} ({comp_name(model, types[members[-1]]['component'])})"
        union = frozenset.union(*(names[m] for m in members))
        copy = mirrors([types[m] for m in members])
        strength = "weak" if copy else "strong" if low >= 0.9 and len(common) >= STRONG_MIN else \
            "medium" if low >= 0.8 else "weak"
        out.append(finding(
            "types.synonyms", tuple(members), f"{title} share most of their fields.",
            ("The same fields in the same order: likely a deliberate copy, such as a wire type. " if copy else "")
            + f"{len(common)} of {len(union)} field names shared: " + ", ".join(sorted(common)[:8])
            + (", …" if len(common) > 8 else ""),
            strength, f"{round(low * 100)}% or more of field names in common" + (", same order" if copy else ""),
            [types[m]["component"] for m in members], [types[m].get("file") for m in members],
            {"types": members, "shared": sorted(common)[:EXAMPLES],
             "pairs": [{"a": a, "b": b, "jaccard": round(j, 2)} for (a, b), j in sorted(best.items()) if a in members]},
            focus=types_focus({m: sorted(common) for m in members}, [types[m]["component"] for m in members]),
            weight=Weight(35, "synonym types").add("shared fields", f"{round(low * 100)}%", 100 * (low - share))
            .add("few shared fields", len(common), -6 * max(0, STRONG_MIN - len(common)))
            .add("same fields, same order", "a deliberate copy", -30 if copy else 0)
            .spread(len({types[m]["component"] for m in members}), per=6, cap=12, free=2)
            .spread(len(common), "shared fields", per=3, cap=6, free=min_fields)))
    return out
