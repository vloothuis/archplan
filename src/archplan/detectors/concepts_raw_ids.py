"""concepts.raw_ids: fields such as invoice_id or invoiceId that hold another
component's concept as a plain string or number. One finding per pair of
components; the move is a typed id owned by the concept's component."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, plural, types_focus
from .fields import is_primitive_id, key, words

ID = "concepts.raw_ids"
NAME = "Raw ids of other components' concepts"
GROUP = "concepts"
NEEDS = {"fields"}
PARAMS = {"min_fields": 1}
CONCEPT_KINDS = ("class", "struct", "record", "interface")


def owners(ctx) -> dict:
    """{name key: {components}}: who owns a concept, from the glossaries, else
    from the type names."""
    by_type = defaultdict(set)
    for t in ctx.types.values():
        if t.get("kind") in CONCEPT_KINDS and t.get("component") in ctx.internal and t.get("name"):
            by_type[key(t["name"])].add(t["component"])
    by_vocab = {}
    for c in (ctx.vocabulary.get("concepts") or {}).values():
        if isinstance(c, dict) and c.get("owner") in ctx.internal:
            for term in [c.get("name")] + list(c.get("terms") or []):
                if isinstance(term, str) and term:
                    by_vocab[key(term)] = {c["owner"]}
    return {**by_type, **by_vocab}


def run(ctx, params) -> list:
    own = owners(ctx)
    pairs = defaultdict(list)  # (component, owner) -> [(type, field, concept)]
    for tid, t in sorted(ctx.types.items()):
        c = t.get("component")
        if c not in ctx.internal:
            continue
        fields = [f for f in t.get("fields") or [] if f.get("name")]
        typed = {key(f["name"]) for f in fields if f.get("ref")}
        for f in fields:
            w = words(f["name"])
            if len(w) < 2 or w[-1] != "id" or not is_primitive_id(f):
                continue
            stem = "_".join(w[:-1])
            if stem in typed:  # an association next to its key, such as belongs_to
                continue
            who = own.get(stem) or own.get(w[-2]) or set()
            if c in who or len(who) != 1:
                continue
            pairs[(c, next(iter(who)))].append((tid, f["name"], stem))
    out = []
    for (c, o), rows in sorted(pairs.items()):
        if len(rows) < params["min_fields"]:
            continue
        concepts = sorted({s for _, _, s in rows})
        fields = sorted({n for _, n, _ in rows})
        tid0 = rows[0][0]
        marked = {}
        for t, n, _ in rows:
            marked.setdefault(t, []).append(n)
        idname = "".join(x.capitalize() for x in concepts[0].split("_")) + "Id"
        out.append(ctx.finding(
            (c, o),
            f"{ctx.name(c)} holds {plural(len(concepts), 'concept')} of {ctx.name(o)} as raw ids: "
            + ", ".join(fields[:4]) + (", …" if len(fields) > 4 else "") + ".",
            "; ".join(f"{ctx.types[t].get('name') or t}.{n}" for t, n, _ in rows[:6])
            + (f"; and {len(rows) - 6} more" if len(rows) > 6 else ""),
            "medium" if len(rows) >= 3 else "weak",
            f"{plural(len(rows), 'field')} in {plural(len({t for t, _, _ in rows}), 'type')}",
            [c, o], [ctx.types[t].get("file") for t, _, _ in rows],
            {"component": c, "owner": o, "concepts": concepts,
             "fields": [{"type": t, "field": n, "concept": s} for t, n, s in rows[:EXAMPLES]],
             "moves": [f"Introduce a typed id ({idname}) in {ctx.name(o)} and use it in "
                       f"{ctx.types[tid0].get('name') or tid0} and the other fields.",
                       f"Or hold a reference to {ctx.name(o)}'s type instead of its id."]},
            focus=types_focus(marked, [c, o]),
            weight=Weight(25, "raw ids").repetition(len(rows), "fields", per=6, cap=18, single=0)
            .spread(len(concepts), "concepts", per=4, cap=8)))
    return out
