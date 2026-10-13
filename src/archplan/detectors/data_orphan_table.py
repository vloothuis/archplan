"""data.orphan_table: a table no component owns, or that no code uses
besides its own definition. It may be left over from an old feature, or used
by code outside the model (scripts, another service)."""

from __future__ import annotations

from .common import Weight, finding, plural
from .data_common import access_by_table, owner, table_focus, tables

ID = "data.orphan_table"
NAME = "Table without owner or use"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {}


def run(ctx, params) -> list:
    acc = access_by_table(ctx)
    out = []
    for tname in sorted(tables(ctx)):
        own = owner(ctx, tname)
        uses = acc.get(tname) or []
        real = [a for a in uses if a["mode"] in ("read", "write")] or \
            [a for a in uses if a["mode"] == "orm" and (a["component"] != own or a["weight"] >= 2)]
        if own and real:
            continue
        through = ((ctx.model.get("schema") or {}).get("maps") or {}).get(tname, {}).get("through")
        if own and through and any(a["mode"] != "orm" or a["weight"] >= 2 or a["component"] != own
                                   for a in acc.get(through) or []):
            continue  # a join table, used through its owner's many-to-many field
        why = ([] if own else ["no owner"]) + ([] if real else ["no use in the code"])
        out.append(finding(
            ID, (tname,),
            f"Table {tname} has {' and '.join(why)}.",
            "No ORM type, write or SQL query maps it to a component." if not own and not real else
            "Only its own definition refers to it." if own else
            f"Used by {plural(len({a['component'] for a in real}), 'component')}, but none owns it.",
            "medium" if not own and not real else "weak", "; ".join(why), [own], [],
            {"table": tname, "owner": own, "uses": len(real),
             "moves": ["Drop the table if nothing uses it.", "Or give it an owner: an ORM type in one component."]},
            focus=table_focus(ctx, {tname: []}, [own]),
            weight=Weight(25, "orphan table").add("owner", own or "none", 0 if own else 8)
            .add("use", "none" if not real else len(real), 0 if real else 8)))
    return out
