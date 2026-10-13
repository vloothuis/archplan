"""data.enum_mismatch: a column's allowed values (a database enum, a check
constraint or Django choices) that differ from the code's value set for the
same thing: a declared enum for that table's column, or (with use_literals)
the string literals that components using the table compare with or assign
to a field of that name."""

from __future__ import annotations

import re
from collections import defaultdict

from .common import Weight, finding, plural
from .data_common import owner, schema, table_focus, tables, users

ID = "data.enum_mismatch"
NAME = "Database values differ from code values"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {"min_values": 2, "use_literals": False}
CONTEXTS = {"compare", "case", "assign"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def code_enums(model: dict) -> dict:
    """{normalized name: (name, values)} for the model's enums section."""
    out = {}
    for e in model.get("enums") or []:
        if isinstance(e, dict) and e.get("name") and isinstance(e.get("values"), list):
            vals = [str(v) for v in e["values"]]
            out.setdefault(norm(e["name"]), (e["name"], vals))
            out.setdefault(norm(e["name"].split(".")[-1]), (e["name"], vals))
    return out


def literal_sets(model: dict) -> dict:
    """{(component, field): set of values} from the literals section."""
    out = defaultdict(set)
    for x in model.get("literals") or []:
        if isinstance(x, dict) and x.get("field") and x.get("context") in CONTEXTS and x.get("value") is not None:
            out[(x.get("component"), str(x["field"]).split(".")[-1])].add(str(x["value"]))
    return out


def run(ctx, params) -> list:
    sch = schema(ctx)
    enums = code_enums(ctx.model)
    lits = literal_sets(ctx.model)
    maps = sch.get("maps") or {}
    out = []
    for tname, t in sorted(tables(ctx).items()):
        tid = (maps.get(tname) or {}).get("type")
        comps = set(users(ctx, tname)) | ({owner(ctx, tname)} - {None})
        for c in t.get("columns") or []:
            db = [str(v) for v in c.get("values") or []]
            if len(db) < params["min_values"]:
                continue
            dbs = set(db)
            # 1. a declared enum with a matching name
            match = None
            # Only names tied to this table: a bare column name such as status
            # would match the first enum of that name anywhere in the code.
            for key in (f"{tid}.{c['name']}" if tid else "", f"{tname}{c['name']}", c.get("type") or ""):
                hit = enums.get(norm(key))
                if hit and key:
                    match = hit
                    break
            if match and set(match[1]) != dbs and not (set(match[1]) & dbs == set() and len(match[1]) < 2):
                only_db, only_code = sorted(dbs - set(match[1])), sorted(set(match[1]) - dbs)
                if only_db or only_code:
                    out.append(mismatch(ctx, tname, c, db, match[0], only_db, only_code, "enum"))
                    continue
            # 2. literals that components using the table compare with or assign to this column.
            # Off by default: a field name such as status is shared by many tables, and
            # literals do not say which table they belong to.
            if not params["use_literals"]:
                continue
            used = set()
            for comp in comps:
                used |= lits.get((comp, c["name"]), set())
            extra = sorted(used - dbs)
            if extra and used & dbs:
                out.append(mismatch(ctx, tname, c, db, None, [], extra, "literals"))
    return out


def mismatch(ctx, tname, c, db, enum_name, only_db, only_code, source) -> dict:
    own = owner(ctx, tname)
    what = f"the enum {enum_name}" if enum_name else "the literals in the code"
    parts = []
    if only_code:
        parts.append(f"{what} also {'has' if enum_name else 'use'} {', '.join(only_code[:5])}")
    if only_db:
        parts.append(f"the database also allows {', '.join(only_db[:5])}")
    return finding(
        ID, (tname, c["name"]),
        f"{tname}.{c['name']} allows other values than {what}.",
        "; ".join(parts).capitalize() + ".",
        "strong" if only_code else "medium", f"{plural(len(only_code), 'code value')} not in the database, "
        f"{plural(len(only_db), 'database value')} not in the code", [own], [],
        {"table": tname, "column": c["name"], "database": db[:30], "code_enum": enum_name, "only_code": only_code[:30],
         "only_database": only_db[:30], "source": source,
         "moves": ["Generate one list from the other, or add a migration that changes the allowed values."]},
        focus=table_focus(ctx, {tname: [c["name"]]}, [own]),
        weight=Weight(35, "value mismatch").add("code values not in the database", len(only_code), 30 if only_code else 0)
        .add("database values not in the code", len(only_db), 8 if only_db else 0))
