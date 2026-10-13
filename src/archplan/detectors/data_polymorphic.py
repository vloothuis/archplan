"""data.polymorphic: a pair of columns that together point at a row in any
of several tables, such as subject_type and subject_id, or Django's
content_type_id and object_id. The database cannot check such a reference,
and every reader must know the list of possible tables."""

from __future__ import annotations

from .common import Weight, finding, plural
from .data_common import name, owner, table_focus, tables

ID = "data.polymorphic"
NAME = "Polymorphic reference"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {}
KINDS = ("_type", "_kind", "_model", "_table", "_class", "_entity")
IDS = ("_id", "_key", "_uuid", "_pk")


def pairs(t: dict) -> list:
    cols = {c["name"] for c in t.get("columns") or []}
    fk_cols = {c for f in t.get("fks") or [] for c in f.get("columns") or [] if len(f.get("columns") or []) == 1}
    out = []
    for c in sorted(cols):
        for k in KINDS:
            if c.endswith(k) and len(c) > len(k):
                stem = c[: -len(k)]
                for i in IDS:
                    if stem + i in cols and stem + i not in fk_cols:
                        out.append((c, stem + i))
                        break
    if {"content_type_id", "object_id"} <= cols:
        out.append(("content_type_id", "object_id"))
    for k in ("type", "kind", "entity_type", "resource_type"):
        if k in cols and "entity_id" in cols and (k, "entity_id") not in out and "entity_id" not in fk_cols:
            out.append((k, "entity_id"))
    return list(dict.fromkeys(out))


def run(ctx, params) -> list:
    out = []
    for tname, t in sorted(tables(ctx).items()):
        ps = pairs(t)
        if not ps:
            continue
        own = owner(ctx, tname)
        vals = {c["name"]: c.get("values") for c in t.get("columns") or []}
        known = [v for a, _ in ps for v in (vals.get(a) or [])]
        out.append(finding(
            ID, (tname,),
            f"Table {tname} points at other tables through "
            + ", ".join(f"{a} + {b}" for a, b in ps) + ".",
            (f"Owner: {name(ctx, own)}. " if own else "") + "The database cannot check these references"
            + (f"; the type column allows {', '.join(known[:6])}." if known else "."),
            "medium" if len(ps) > 1 or not known else "weak",
            plural(len(ps), "type and id pair"), [own], [],
            {"table": tname, "pairs": [{"type": a, "id": b} for a, b in ps], "values": known[:20],
             "moves": ["Use one nullable foreign key per target table, with a check that one is set.",
                       "Or give each target its own join table."]},
            focus=table_focus(ctx, {tname: [x for p in ps for x in p]}, [own]),
            weight=Weight(35, "polymorphic reference").repetition(len(ps), "pairs", per=8, cap=10, single=0)
            .add("known targets", len(known), 0 if known else 5)))
    return out
