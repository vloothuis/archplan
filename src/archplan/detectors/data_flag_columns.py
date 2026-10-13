"""data.flag_columns: tables that keep their state in three or more boolean
or nullable timestamp columns (is_active, archived, deleted_at, sent_at).
Flags allow combinations that mean nothing; one state column says which
state a row is in."""

from __future__ import annotations

from .common import COMMON_FIELDS, Weight, finding, plural
from .data_common import is_bool, is_stamp, name, owner, table_focus, tables

ID = "data.flag_columns"
NAME = "State in flag columns"
GROUP = "data"
NEEDS = {"schema"}
PARAMS = {"min_flags": 3}
NOT_STATE = COMMON_FIELDS | {"last_login", "last_login_at", "date_joined", "expires_at", "starts_at", "ends_at",
                             "start_at", "end_at", "birth_date", "scheduled_at", "valid_from", "valid_until"}


def flags(t: dict) -> list:
    out = []
    for c in t.get("columns") or []:
        if c["name"] in NOT_STATE or c["name"] in (t.get("pk") or []):
            continue
        if is_bool(c) or (is_stamp(c) and c.get("null") is not False):
            out.append(c["name"])
    return out


def run(ctx, params) -> list:
    out = []
    for tname, t in sorted(tables(ctx).items()):
        fs = flags(t)
        if len(fs) < params["min_flags"]:
            continue
        own = owner(ctx, tname)
        bools = [c["name"] for c in t["columns"] if c["name"] in fs and is_bool(c)]
        stamps = [x for x in fs if x not in bools]
        out.append(finding(
            ID, (tname,),
            f"Table {tname} keeps state in {plural(len(fs), 'flag column')}: {', '.join(fs[:5])}"
            + (", …" if len(fs) > 5 else "") + ".",
            (f"Owner: {name(ctx, own)}. " if own else "")
            + f"{plural(len(bools), 'boolean')}, {plural(len(stamps), 'nullable timestamp')}.",
            "strong" if len(fs) >= 2 * params["min_flags"] else "medium" if len(fs) > params["min_flags"] else "weak",
            plural(len(fs), "flag column"), [own], [],
            {"table": tname, "booleans": bools, "timestamps": stamps,
             "moves": ["Add one state column (an enum) and derive the flags from it.",
                       "Keep timestamps for when a state began, not for which state the row is in."]},
            focus=table_focus(ctx, {tname: fs}, [own]),
            weight=Weight(30, "flag columns").spread(len(fs), "flag columns", per=35, cap=40,
                                                     free=params["min_flags"])))
    return out
