"""types.synonyms: types in different components with mostly the same field names.

The glossaries (the vocabulary section) can tell the cases apart. When each
type is named after its own concept, owned by its own component, people
decided they are different concepts: the same shape in two bounded contexts.
That weighs less. When all of them are named after one concept, they are
copies of it, and the owner's type is the one to keep. That weighs more."""

from __future__ import annotations

from collections import Counter, defaultdict

from ..core.vocab import term_key
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
    return synonym_findings(ctx.model, params["share"], params["min_fields"], ctx.vocabulary)


def concepts_by_term(vocabulary: dict | None) -> dict:
    """{term key: (concept id, concept)} for the concepts people wrote, not the suggested ones."""
    out = {}
    for cid, c in sorted(((vocabulary or {}).get("concepts") or {}).items()):
        if isinstance(c, dict) and c.get("owner") and c.get("source") != "suggested":
            for t in [c.get("name") or cid] + list(c.get("terms") or []):
                if isinstance(t, str) and term_key(t):
                    out.setdefault(term_key(t), (cid, c))
    return out


def glossary_view(members: list, types: dict, model: dict, by_term: dict) -> tuple:
    """(kind, detail, move): "separate" when each type is named after its own
    component's concept, "one" when all are named after one concept, else
    (None, "", None)."""
    named = [by_term.get(term_key(types[m].get("name") or m.rsplit(".", 1)[-1])) for m in members]
    if not by_term or not all(named):
        return None, "", None
    ids = {cid for cid, _ in named}
    if len(ids) == len(members) and all(c["owner"] == types[m]["component"] for m, (_, c) in zip(members, named)):
        return ("separate", "The glossaries define them as separate concepts: "
                + ", ".join(f"{c.get('name') or cid} ({comp_name(model, c['owner'])})" for cid, c in named) + ". ",
                None)
    if len(ids) == 1:
        cid, c = named[0]
        keep = next((m for m in members if types[m]["component"] == c["owner"]), None)
        return ("one", f"All are the glossary concept {c.get('name') or cid}, owned by "
                f"{comp_name(model, c['owner'])}. ",
                f"Keep {types[keep].get('name') or keep} in {comp_name(model, c['owner'])} and let the others use it."
                if keep else f"Give {comp_name(model, c['owner'])} one type for it and let the others use it.")
    return None, "", None


def names_of(t: dict) -> frozenset:
    return frozenset(f["name"] for f in t.get("fields") or [] if f.get("name") and key(f["name"]) not in COMMON_FIELDS)


def mirrors(types: list) -> bool:
    """Whether the types list the same fields in the same order, with the same
    plain types: a deliberate copy, such as a wire type converted with T(x)."""
    shape = lambda t: [(f.get("name"), f.get("type")) for f in t.get("fields") or []]
    return all(shape(t) == shape(types[0]) for t in types[1:])


def synonym_findings(model: dict, share: float = SYNONYM, min_fields: int = SYNONYM_MIN,
                     vocabulary: dict | None = None) -> list:
    """Groups of types in different components whose field names mostly match."""
    by_term = concepts_by_term(vocabulary if vocabulary is not None else model.get("vocabulary"))
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
        gloss, gloss_detail, gloss_move = glossary_view(members, types, model, by_term)
        out.append(finding(
            "types.synonyms", tuple(members), f"{title} share most of their fields.",
            ("The same fields in the same order: likely a deliberate copy, such as a wire type. " if copy else "")
            + gloss_detail + f"{len(common)} of {len(union)} field names shared: " + ", ".join(sorted(common)[:8])
            + (", …" if len(common) > 8 else ""),
            strength, f"{round(low * 100)}% or more of field names in common" + (", same order" if copy else ""),
            [types[m]["component"] for m in members], [types[m].get("file") for m in members],
            {"types": members, "shared": sorted(common)[:EXAMPLES],
             "pairs": [{"a": a, "b": b, "jaccard": round(j, 2)} for (a, b), j in sorted(best.items()) if a in members],
             **({"glossary": gloss} if gloss else {}), **({"moves": [gloss_move]} if gloss_move else {})},
            focus=types_focus({m: sorted(common) for m in members}, [types[m]["component"] for m in members]),
            weight=Weight(35, "synonym types").add("shared fields", f"{round(low * 100)}%", 100 * (low - share))
            .add("few shared fields", len(common), -6 * max(0, STRONG_MIN - len(common)))
            .add("same fields, same order", "a deliberate copy", -30 if copy else 0)
            .add("glossary", {"separate": "separate concepts", "one": "one concept"}.get(gloss),
                 {"separate": -20, "one": 10}.get(gloss, 0))
            .spread(len({types[m]["component"] for m in members}), per=6, cap=12, free=2)
            .spread(len(common), "shared fields", per=3, cap=6, free=min_fields)))
    return out
