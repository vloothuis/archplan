"""concepts.untyped_bag: fields typed as a map, dict, Any, interface{}, object
or JSON on types that other components use. What such a field holds is a
contract nobody wrote down. One finding per component."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, plural, types_focus
from .fields import is_bag

ID = "concepts.untyped_bag"
NAME = "Untyped data across boundaries"
GROUP = "concepts"
NEEDS = {"fields"}
PARAMS = {"min_fields": 1}


def crossing(ctx) -> dict:
    """{type id: {components}}: the types other components refer to, by field,
    signature or file reference."""
    out = defaultdict(set)
    for tid, t in ctx.types.items():
        refs = [f.get("ref") for f in t.get("fields") or []] + list(t.get("uses") or []) \
            + list(t.get("implements") or [])
        for r in refs:
            if r in ctx.types and r != tid and ctx.types[r].get("component") != t.get("component"):
                out[r].add(t.get("component"))
    files = ctx.model.get("files") or {}
    if files:
        by_file = defaultdict(list)
        for tid, t in ctx.types.items():
            if t.get("file"):
                by_file[t["file"]].append(tid)
        for f, v in files.items():
            for r in v.get("refs") or []:
                for tid in by_file.get(r.get("to"), ()):
                    if v.get("component") and v.get("component") != ctx.types[tid].get("component"):
                        out[tid].add(v["component"])
    return out


def run(ctx, params) -> list:
    users = crossing(ctx)
    by_comp = defaultdict(list)
    for tid in sorted(users):
        t = ctx.types[tid]
        if t.get("component") not in ctx.internal:
            continue
        bags = [f for f in t.get("fields") or [] if f.get("name") and is_bag(f)]
        if bags:
            by_comp[t["component"]].append((tid, t, bags))
    out = []
    for c, rows in sorted(by_comp.items()):
        n = sum(len(b) for _, _, b in rows)
        if n < params["min_fields"]:
            continue
        rows.sort(key=lambda r: (-len(users[r[0]]), r[0]))
        fields = [f"{t.get('name') or tid}.{f['name']} ({f.get('type')})" for tid, t, bs in rows for f in bs]
        others = sorted({u for tid, _, _ in rows for u in users[tid]})
        out.append(ctx.finding(
            (c,),
            f"{plural(len(rows), 'type')} of {ctx.name(c)} that other components use "
            f"{'carries' if len(rows) == 1 else 'carry'} untyped data: " + ", ".join(fields[:3])
            + (", …" if len(fields) > 3 else "") + ".",
            f"Used by {', '.join(ctx.name(u) for u in others[:5])}" + (", …" if len(others) > 5 else "") + ".",
            "medium" if len(others) >= 2 or n >= 3 else "weak",
            f"{plural(n, 'untyped field')}, used by {plural(len(others), 'other component')}",
            [c] + others, [t.get("file") for _, t, _ in rows],
            {"component": c, "users": others[:EXAMPLES],
             "fields": [{"type": tid, "field": f["name"], "field_type": f.get("type"), "used_by": sorted(users[tid])}
                        for tid, _, bs in rows for f in bs][:EXAMPLES],
             "moves": ["Give each bag a named type (a struct, record or schema) that says which keys it holds.",
                       "Validate it where it enters the component, and keep the raw map inside."]},
            focus=types_focus({tid: [f["name"] for f in bs] for tid, _, bs in rows}, [c] + others),
            weight=Weight(25, "untyped data").spread(len(others), "other components using it", per=7, cap=18, free=0.5)
            .repetition(n, "untyped fields", per=3, cap=9, single=0)))
    return out
