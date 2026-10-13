"""data.repeated_columns: the same group of columns in three or more tables,
such as street, postcode and city. The group is a value the code has no
shared type for, and each table may validate it in its own way."""

from __future__ import annotations

from itertools import combinations

from .common import EXAMPLES, Weight, finding, plural
from .data_common import name, owner, plain_columns, table_focus, tables

ID = "data.repeated_columns"
NAME = "Repeated column group"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {"min_columns": 3, "min_tables": 3}
MAX_TABLES = 3000


def groups(tbls: dict, min_columns: int, min_tables: int) -> list:
    """[(columns, tables)]: maximal column groups shared by enough tables."""
    cols = {}
    for n, t in tbls.items():
        fk = {c for f in t.get("fks") or [] for c in f.get("columns") or []}
        s = frozenset(plain_columns(t) - fk)
        if len(s) >= min_columns:
            cols[n] = s
    names = sorted(cols)[:MAX_TABLES]
    cands = set()
    for a, b in combinations(names, 2):
        common = cols[a] & cols[b]
        if len(common) >= min_columns:
            cands.add(common)
    found = {}
    for g in cands:
        users = frozenset(n for n in names if g <= cols[n])
        if len(users) >= min_tables:
            if users not in found or len(g) > len(found[users]):
                found[users] = g
    # keep a group unless a bigger one covers the same tables or more
    items = sorted(found.items(), key=lambda kv: (-len(kv[1]), -len(kv[0])))
    out = []
    for users, g in items:
        if any(g < g2 and users <= u2 for u2, g2 in out):
            continue
        if any(g <= g2 and users <= u2 for u2, g2 in out):
            continue
        out.append((users, g))
    return [(sorted(g), sorted(u)) for u, g in out]


def run(ctx, params) -> list:
    out = []
    for cols, tbls in groups(tables(ctx), params["min_columns"], params["min_tables"]):
        owners = sorted({o for t in tbls if (o := owner(ctx, t))})
        out.append(finding(
            ID, tuple(cols),
            f"{plural(len(tbls), 'table')} repeat the columns {', '.join(cols[:5])}" + (", …" if len(cols) > 5 else "")
            + ".",
            "Tables: " + ", ".join(tbls[:6]) + (", …" if len(tbls) > 6 else "")
            + (f". Owners: {', '.join(name(ctx, o) for o in owners[:5])}." if owners else "."),
            "strong" if len(tbls) >= 5 or len(owners) >= 3 else "medium" if len(owners) >= 2 else "weak",
            f"{plural(len(cols), 'column')} in {plural(len(tbls), 'table')}", owners, [],
            {"columns": cols, "tables": tbls[:EXAMPLES], "owners": owners,
             "moves": ["Give the group one type in the code (a value object or embedded schema) and reuse it.",
                       "If the rows are the same thing, keep them in one table that the others reference."]},
            focus=table_focus(ctx, {t: cols for t in tbls}, owners),
            weight=Weight(25, "repeated columns").spread(len(tbls), "tables", per=12, cap=20, free=2)
            .spread(len(owners), "owners", per=12, cap=20)
            .spread(len(cols), "columns", per=4, cap=8, free=3)))
    return out
