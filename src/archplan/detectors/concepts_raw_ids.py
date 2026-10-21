"""concepts.raw_ids: fields such as invoice_id or invoiceId that hold another
component's concept as a plain string or number. One finding per pair of
components; the move is a typed id owned by the concept's component. When
the name does not tell the concept reliably, the finding names none."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, plural, types_focus
from .fields import PRIMITIVE_ID, is_primitive_id, key, type_text, words

ID = "concepts.raw_ids"
NAME = "Raw ids of other components' concepts"
GROUP = "concepts"
NEEDS = {"fields"}
PARAMS = {"min_fields": 1}
CONCEPT_KINDS = ("class", "struct", "record", "interface")
NUMBER_ID = {"id", "integer", "int", "int32", "int64", "uint", "uint32", "uint64", "long", "bigint", "number",
             "positiveinteger", "biginteger", "positivebiginteger", "autofield", "bigautofield", "pos_integer",
             "non_neg_integer"}


def owners(ctx) -> dict:
    """{name key: ({components}, {all components}, id field)}: who owns a
    concept, from the glossaries, else from the type names. The first set has
    the components whose type of that name has an id; the second has all that
    define a type of that name, with or without an id."""
    named = defaultdict(set)
    by_type = {}
    for t in ctx.types.values():
        if t.get("kind") in CONCEPT_KINDS and t.get("component") in ctx.internal and t.get("name"):
            named[key(t["name"])].add(t["component"])
            # Only a type with an id of its own can be what a foo_id points to.
            idf = next((f for f in t.get("fields") or [] if key(f.get("name") or "") in ("id", "uuid")), None)
            if idf:
                who, _, _ = by_type.get(key(t["name"]), (set(), None, None))
                by_type[key(t["name"])] = (who | {t["component"]}, named[key(t["name"])], idf)
    by_vocab = {}
    for c in (ctx.vocabulary.get("concepts") or {}).values():
        if isinstance(c, dict) and c.get("owner") in ctx.internal:
            for term in [c.get("name")] + list(c.get("terms") or []):
                if isinstance(term, str) and term:
                    by_vocab[key(term)] = ({c["owner"]}, {c["owner"]}, None)
    return {**by_type, **by_vocab}


def id_class(f) -> str | None:
    """number or text, or None when the type does not say."""
    t = type_text(f).lstrip(":")
    return "number" if t in NUMBER_ID else "text" if t in PRIMITIVE_ID else None


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
            if stem not in own:  # only the whole stem names a concept, not one word of it
                continue
            who, named, idf = own[stem]
            if c in named or len(who) != 1:  # its own concept, or one that several components share
                continue
            # Unclear: other components define a type of that name too, or one id is a number and the other text.
            if named != who or idf and None not in (id_class(f), id_class(idf)) and id_class(f) != id_class(idf):
                pairs[(c, None)].append((tid, f["name"], None))
                continue
            pairs[(c, next(iter(who)))].append((tid, f["name"], stem))
    out = []
    for (c, o), rows in sorted(pairs.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        if len(rows) < params["min_fields"]:
            continue
        concepts = sorted({s for _, _, s in rows if s})
        fields = sorted({n for _, n, _ in rows})
        names = ", ".join(fields[:4]) + (", …" if len(fields) > 4 else "")
        tid0 = rows[0][0]
        marked = {}
        for t, n, _ in rows:
            marked.setdefault(t, []).append(n)
        if o is None:  # the name does not tell which concept: say so rather than guess
            moves = [f"Give {fields[0]} in {ctx.types[tid0].get('name') or tid0} a typed id named after "
                     f"what it refers to, or a reference to that type."]
            title = f"{ctx.name(c)} holds raw ids whose concept is unclear: {names}."
        else:
            idname = "".join(x.capitalize() for x in concepts[0].split("_")) + "Id"
            moves = [f"Introduce a typed id ({idname}) in {ctx.name(o)} and use it in "
                     f"{ctx.types[tid0].get('name') or tid0} and the other fields.",
                     f"Or hold a reference to {ctx.name(o)}'s type instead of its id."]
            title = (f"{ctx.name(c)} holds raw ids of "
                     + ", ".join("".join(x.capitalize() for x in s.split("_")) for s in concepts)
                     + f" from {ctx.name(o)}: {names}.")
        out.append(ctx.finding(
            (c, o) if o else (c,),
            title,
            "; ".join(f"{ctx.types[t].get('name') or t}.{n}" for t, n, _ in rows[:6])
            + (f"; and {len(rows) - 6} more" if len(rows) > 6 else ""),
            "medium" if len(rows) >= 3 else "weak",
            f"{plural(len(rows), 'field')} in {plural(len({t for t, _, _ in rows}), 'type')}",
            [c, o], [ctx.types[t].get("file") for t, _, _ in rows],
            {"component": c, "owner": o, "concepts": concepts,
             "fields": [{"type": t, "field": n, "concept": s} for t, n, s in rows[:EXAMPLES]],
             "moves": moves},
            focus=types_focus(marked, [x for x in (c, o) if x]),
            weight=Weight(25, "raw ids").repetition(len(rows), "fields", per=6, cap=18, single=0)
            .spread(len(concepts), "concepts", per=4, cap=8)))
    return out
