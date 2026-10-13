"""data.json_bag: json and jsonb columns. What they hold is a contract the
database does not check. Weighted by how many components use the table: a
bag that several components read is a shared, unwritten schema."""

from __future__ import annotations

from .common import Weight, finding, plural
from .data_common import is_json, name, owner, table_focus, tables, users

ID = "data.json_bag"
NAME = "JSON column"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {"min_readers": 1}


def run(ctx, params) -> list:
    out = []
    for tname, t in sorted(tables(ctx).items()):
        cols = [c for c in t.get("columns") or [] if is_json(c)]
        if not cols:
            continue
        own = owner(ctx, tname)
        readers = users(ctx, tname)
        if len(readers) < params["min_readers"] and params["min_readers"] > 0:
            continue
        n = len(readers)
        others = sorted(c for c in readers if c != own)
        strength = "strong" if n >= 3 else "medium" if n == 2 else "weak"
        out.append(finding(
            ID, (tname,),
            f"Table {tname} keeps data in {plural(len(cols), 'JSON column')} ({', '.join(c['name'] for c in cols[:4])}"
            + (", …" if len(cols) > 4 else "") + f"), used by {plural(n, 'component')}.",
            (f"Owner: {name(ctx, own)}. " if own else "")
            + (f"Also used by {', '.join(name(ctx, c) for c in others[:5])}." if others else "Only the owner uses it."),
            strength, f"{plural(len(cols), 'JSON column')}, {plural(n, 'component')} using the table",
            [own, *others], [],
            {"table": tname, "columns": [{"name": c["name"], "type": c.get("type")} for c in cols],
             "components": sorted(readers), "moves": [
                "Name the shape: a typed embedded schema or record that says which keys it holds.",
                "Move keys that queries filter on into real columns."]},
            focus=table_focus(ctx, {tname: [c["name"] for c in cols]}, [own, *others]),
            weight=Weight(25, "JSON column").spread(n, "components using the table", per=25, cap=45)
            .repetition(len(cols), "JSON columns", per=3, cap=6, single=0)))
    return out
