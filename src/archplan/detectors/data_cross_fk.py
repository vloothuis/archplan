"""data.cross_fk: foreign keys from a table one component owns to a table of
another component. Such a key ties the two components' data together below
the code: neither can change or move its table alone. With a profile that
has layers, a key from an inner layer to an outer one is reported as strong.
Keys that follow a code dependency and do not point up the layers mirror
coupling the code already has, and are skipped unless include_coded is set. One finding per pair of components."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, finding, plural
from .data_common import layer_rank, name, owner, table_focus, tables

ID = "data.cross_fk"
NAME = "Foreign key across components"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {"min_keys": 1, "include_coded": False}


def cross_focus(ctx, a: str, b: str, keys: list) -> list:
    """The ORM types with the key fields, then the edge a -> b in Components."""
    tbls = {}
    for k in keys:
        tbls.setdefault(k["table"], []).extend(k["columns"])
        tbls.setdefault(k["to"], [])
    return table_focus(ctx, tbls, [a, b], [(a, b)])


def run(ctx, params) -> list:
    pairs = defaultdict(list)
    for tname, t in sorted(tables(ctx).items()):
        a = owner(ctx, tname)
        for fk in t.get("fks") or []:
            b = owner(ctx, fk.get("to"))
            if a and b and a != b:
                pairs[(a, b)].append({"table": tname, "columns": fk.get("columns") or [], "to": fk["to"],
                                      **({"on_delete": fk["on_delete"]} if fk.get("on_delete") else {})})
    rank = layer_rank(ctx)
    deps = {(d.get("from"), d.get("to")) for d in ctx.model.get("dependencies") or []}
    out = []
    for (a, b), keys in sorted(pairs.items()):
        if len(keys) < params["min_keys"]:
            continue
        cascade = [k for k in keys if (k.get("on_delete") or "").upper() == "CASCADE"]
        direction = None
        if a in rank and b in rank:
            direction = "up" if rank[a][0] > rank[b][0] else "down" if rank[a][0] < rank[b][0] else "same"
        coded = (a, b) in deps
        # A key that follows a code dependency, down or across the layers, mirrors
        # coupling the code already has (a dependency back is a cycle, which the
        # structure detectors report); report it only on request.
        if coded and direction != "up" and not params["include_coded"]:
            continue
        strength = ("strong" if direction == "up" or (b, a) in deps and not coded else
                    "weak" if direction == "down" and not cascade else
                    "medium" if len(keys) >= 3 or cascade or not coded else "weak")
        why = [plural(len(keys), "foreign key")]
        if direction == "up":
            why.append(f"from {rank[a][1]} up to {rank[b][1]}")
        elif direction == "down":
            why.append(f"along the layers, {rank[a][1]} to {rank[b][1]}")
        if cascade:
            why.append(f"{len(cascade)} with ON DELETE CASCADE")
        if not coded:
            why.append(f"no code dependency {name(ctx, a)} → {name(ctx, b)}")
        sample = ", ".join(f"{k['table']}.{','.join(k['columns'])} → {k['to']}" for k in keys[:3])
        out.append(finding(
            ID, (a, b),
            f"{name(ctx, a)} has {plural(len(keys), 'foreign key')} to tables of {name(ctx, b)}.",
            sample + (", …" if len(keys) > 3 else "") + ".",
            strength, "; ".join(why), [a, b], [],
            {"from": a, "to": b, "keys": keys[:EXAMPLES], "direction": direction, "code_dependency": coded,
             **({"from_layer": rank[a][1], "to_layer": rank[b][1]} if direction else {}),
             "moves": ["Keep the id as a plain value and ask the other component for the record.",
                       "Or move the table to the component that owns the data it points at."]},
            focus=cross_focus(ctx, a, b, keys),
            weight=Weight(35, "foreign key across components")
            .add("layers", direction, {"up": 25, "down": -5}.get(direction or "", 0))
            .add("code dependency", "none" if not coded else "yes", 0 if coded else 8)
            .add("cycle", "the code depends back", 10 if (b, a) in deps and not coded else 0)
            .add("cascade", len(cascade), 5 if cascade else 0)
            .repetition(len(keys), "keys", per=3, cap=9, single=0)))
    return out
