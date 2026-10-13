"""concepts.units_in_names: one quantity named with different units across
components, such as amount and amount_cents, or timeout_ms and
timeout_seconds. The move is one type that carries its unit."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, focus, plural, types_focus
from .fields import words

ID = "concepts.units_in_names"
NAME = "Units in names"
GROUP = "concepts"
NEEDS = {"fields"}
PARAMS = {"min_components": 2, "max_findings": 40}
UNITS = {"ms": "ms", "millis": "ms", "milliseconds": "ms", "msec": "ms", "msecs": "ms",
         "s": "s", "sec": "s", "secs": "s", "seconds": "s", "mins": "minutes", "minutes": "minutes",
         "hours": "hours", "hrs": "hours", "days": "days", "us": "µs", "micros": "µs", "microseconds": "µs",
         "ns": "ns", "nanos": "ns", "nanoseconds": "ns", "cents": "cents", "pence": "cents",
         "bytes": "bytes", "kb": "kb", "mb": "mb", "gb": "gb", "pct": "percent", "percent": "percent",
         "percentage": "percent", "px": "px"}
NONE = "no unit"


def split(name: str):
    """(base, unit or None): timeout_ms -> (timeout, ms); amountInCents -> (amount, cents)."""
    w = words(name)
    if len(w) >= 2 and w[-1] in UNITS:
        base = w[:-1]
        if len(base) >= 2 and base[-1] == "in":
            base = base[:-1]
        return "_".join(base), UNITS[w[-1]]
    return "_".join(w), None


def names(ctx):
    """(name, component, where, type id or None) for every field and, when
    present, every parameter."""
    for tid, t in ctx.types.items():
        c = t.get("component")
        if c in ctx.internal:
            for f in t.get("fields") or []:
                if f.get("name"):
                    yield f["name"], c, f"{t.get('name') or tid}.{f['name']}", tid
    for s in ctx.signatures:
        c = s.get("component")
        if c in ctx.internal:
            for p in s.get("params") or []:
                if isinstance(p, dict) and isinstance(p.get("name"), str):
                    yield p["name"], c, f"{s.get('id')}({p['name']})", None


def run(ctx, params) -> list:
    seen = defaultdict(lambda: defaultdict(dict))  # base -> unit -> {component: example}
    fields = defaultdict(lambda: defaultdict(list))  # base -> {type id: [field names]}
    for n, c, where, tid in names(ctx):
        base, unit = split(n)
        if base and not base.isdigit():
            seen[base][unit or NONE].setdefault(c, where)
            if tid:
                fields[base][tid].append(n)
    rows = []
    for base, units in seen.items():
        if len(units) < 2 or set(units) == {NONE}:
            continue
        comps = {c for cs in units.values() for c in cs}
        if len(comps) < params["min_components"]:
            continue
        rows.append((base, units, comps))
    rows.sort(key=lambda r: (-len(r[1]), -len(r[2]), r[0]))
    out = []
    for base, units, comps in rows[:params["max_findings"]]:
        explicit = sorted(u for u in units if u != NONE)
        parts = []
        for u in explicit + ([NONE] if NONE in units else []):
            cs = ", ".join(ctx.name(c) for c in sorted(units[u])[:3]) + (", …" if len(units[u]) > 3 else "")
            parts.append(f"{'without a unit' if u == NONE else 'in ' + u} in {cs}")
        title = f"{base} is named " + "; ".join(parts) + "."
        out.append(ctx.finding(
            (base,), title,
            "Examples: " + ", ".join(w for u in units for w in list(units[u].values())[:2]) + ".",
            "medium" if len(explicit) >= 2 else "weak",
            f"{plural(len(units), 'spelling')} in {plural(len(comps), 'component')}",
            sorted(comps), [],
            {"base": base, "units": {u: sorted(cs)[:EXAMPLES] for u, cs in units.items()},
             "examples": [w for u in units for w in list(units[u].values())[:3]][:EXAMPLES],
             "moves": [f"Pick one unit for {base} and convert at the edges, or use a type that carries its unit "
                       "(a Duration or Money value).",
                       "Name the unit in every field and parameter that keeps a plain number."]},
            focus=types_focus(dict(fields[base]), sorted(comps)) if fields[base] else
            [focus("components", sorted(comps))],
            weight=Weight(20, "units in names").spread(len(explicit), "explicit units", per=10, cap=15)
            .spread(len(comps), per=5, cap=12, free=params["min_components"] - 1)))
    return out
