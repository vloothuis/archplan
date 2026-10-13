"""data.shared_table: a table that two or more components write. Each writer
can break the others' rules for that data; usually one component should own
the writes and offer them to the rest."""

from __future__ import annotations

from .common import EXAMPLES, Weight, evidence_file, finding, plural
from .data_common import access_by_table, name, owner, table_focus

ID = "data.shared_table"
NAME = "Table written by several components"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {"min_writers": 2}


def run(ctx, params) -> list:
    out = []
    for table, acc in sorted(access_by_table(ctx).items()):
        writes = sorted((a for a in acc if a.get("mode") == "write"), key=lambda a: (-a["weight"], a["component"]))
        writers = [a["component"] for a in writes]
        if len(set(writers)) < params["min_writers"]:
            continue
        own = owner(ctx, table)
        foreign = [c for c in writers if c != own]
        ev = [e for a in writes for e in a.get("evidence") or []]
        strength = "strong" if len(foreign) >= 2 else "medium"
        out.append(finding(
            ID, (table,),
            f"Table {table} is written by {plural(len(writers), 'component')}: "
            + ", ".join(name(ctx, c) for c in writers[:4]) + (", …" if len(writers) > 4 else "") + ".",
            (f"Owner: {name(ctx, own)}. " if own else "No owner. ")
            + "; ".join(f"{name(ctx, a['component'])} in {plural(a['weight'], 'file')}" for a in writes[:5]) + ".",
            strength, f"{plural(len(foreign), 'writer')} besides the owner",
            writers, [evidence_file(e) for e in ev],
            {"table": table, "owner": own,
             "writers": [{"component": a["component"], "weight": a["weight"], "evidence": a.get("evidence") or []}
                         for a in writes][:EXAMPLES],
             "moves": ["Let the owner do every write; the others call its public functions.",
                       "Or split the table, so each component writes its own part."]},
            focus=table_focus(ctx, {table: []}, writers),
            weight=Weight(35, "shared table").spread(len(foreign), "writers besides the owner", per=15, cap=30, free=0.5)
            .add("owner", own or "none", 0 if own else 5)
            .repetition(sum(a["weight"] for a in writes if a["component"] != own), "foreign writing files",
                        per=3, cap=9, single=0)))
    return out
