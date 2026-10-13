"""Helpers for the data detectors, which read the model's schema section:
table owners, who reads and writes a table, and layer ranks."""

from __future__ import annotations

from collections import defaultdict

from .common import COMMON_FIELDS, focus, namer

TIMESTAMPS = ("timestamp", "datetime", "utc_datetime", "naive_datetime", "timestamptz")


def schema(ctx) -> dict:
    return ctx.model.get("schema") or {}


def tables(ctx) -> dict:
    return schema(ctx).get("tables") or {}


def owner(ctx, table: str) -> str | None:
    return ((schema(ctx).get("maps") or {}).get(table) or {}).get("component")


def users(ctx, table: str, modes=("orm", "read", "write")) -> dict:
    """{component: weight} for the components that use a table in these modes."""
    out = defaultdict(int)
    for a in schema(ctx).get("access") or []:
        if a.get("table") == table and a.get("mode") in modes:
            out[a["component"]] += a.get("weight") or 1
    return dict(out)


def access_by_table(ctx) -> dict:
    out = defaultdict(list)
    for a in schema(ctx).get("access") or []:
        out[a.get("table")].append(a)
    return out


def name(ctx, cid: str) -> str:
    return namer(ctx.model)(cid) if cid else "?"


def layer_rank(ctx) -> dict:
    """{component: index} from the profile's layers, outermost first; empty
    without a profile with two or more layers."""
    try:
        prof = ctx.profile[0]
    except Exception:  # noqa: BLE001 - no profile, or no project behind the context
        prof = getattr(ctx, "profile_dict", None) or {}
    layers = [x for x in (prof or {}).get("layers") or [] if x.get("components")]
    if len(layers) < 2:
        return {}
    return {c: (i, x.get("name") or x.get("id")) for i, x in enumerate(layers) for c in x["components"]}


def is_json(c: dict) -> bool:
    t = (c.get("type") or "").lower()
    return t in ("json", "jsonb", "map", "hstore", "json[]", "jsonb[]") or t.startswith("json")


def is_bool(c: dict) -> bool:
    t = (c.get("type") or "").lower()
    return t.startswith("bool") or t == "tinyint(1)"


def is_stamp(c: dict) -> bool:
    t = (c.get("type") or "").lower()
    return t.startswith(TIMESTAMPS)


def table_focus(ctx, tbls: dict, comps=(), edges=()) -> list:
    """Where the editor shows a finding about tables ({table: [column names]}):
    the Types view with the ORM types that map them (schema maps), with the
    fields named like the columns, then the components. Without such types,
    only the components."""
    maps = schema(ctx).get("maps") or {}
    fields = {}
    for tname, cols in tbls.items():
        tid = (maps.get(tname) or {}).get("type")
        t = (ctx.model.get("types") or {}).get(tid) if isinstance(tid, str) else None
        if not isinstance(t, dict):
            continue
        names = {f.get("name") for f in t.get("fields") or [] if isinstance(f, dict)}
        hit = fields.setdefault(tid, [])
        for c in cols:
            if c in names:
                hit.append(c)
            elif c.endswith("_id") and c[:-3] in names:  # belongs_to user for user_id
                hit.append(c[:-3])
    comps = [c for c in dict.fromkeys(comps) if c]
    out = [focus("types", list(fields), fields=fields)] if fields else []
    if comps or edges:
        out.append(focus("components", comps, edges))
    return out


def plain_columns(t: dict) -> set:
    return {c["name"] for c in t.get("columns") or []} - COMMON_FIELDS
