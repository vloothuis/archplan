"""concepts.state_flags: types that keep their state in boolean flags (is_*, has_*,
or a flag next to a *_at timestamp of the same name). One finding per
component, listing its types; the move is one State enum per type."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, plural, types_focus
from .fields import is_flag, is_temporal, words

ID = "concepts.state_flags"
NAME = "State kept in flags"
GROUP = "concepts"
NEEDS = {"fields"}
PARAMS = {"min_flags": 3}
PREFIXES = ("is", "has", "was")
STAMPS = ("at", "on", "date", "time")
SKIP_KINDS = ("enum", "function", "module", "alias")


def state_flags(t: dict) -> list:
    """[(flag, timestamp or None)]: the type's boolean fields that name a state."""
    fields = [f for f in t.get("fields") or [] if f.get("name")]
    stamps = {}
    for f in fields:
        w = words(f["name"])
        if len(w) > 1 and w[-1] in STAMPS and is_temporal(f):
            stamps["_".join(w[:-1])] = f["name"]
    out = []
    for f in fields:
        if not is_flag(f):
            continue
        w = words(f["name"])
        stem = "_".join(w[1:] if len(w) > 1 and w[0] in PREFIXES else w)
        if len(w) > 1 and w[0] in PREFIXES or stem in stamps:
            out.append((f["name"], stamps.get(stem)))
    return out


def run(ctx, params) -> list:
    by_comp = defaultdict(list)
    for tid, t in sorted(ctx.types.items()):
        if t.get("kind") in SKIP_KINDS or t.get("component") not in ctx.internal:
            continue
        flags = state_flags(t)
        if len(flags) >= params["min_flags"]:
            by_comp[t["component"]].append((tid, t, flags))
    out = []
    for c, rows in sorted(by_comp.items()):
        rows.sort(key=lambda r: (-len(r[2]), r[0]))
        tid, t, flags = rows[0]
        tname = t.get("name") or tid
        paired = sum(1 for _, _, fs in rows for _, s in fs if s)
        names = ", ".join(n for n, _ in flags[:4]) + (", …" if len(flags) > 4 else "")
        if len(rows) == 1:
            title = f"{tname} in {ctx.name(c)} keeps its state in {len(flags)} boolean flags ({names})."
        else:
            title = (f"{plural(len(rows), 'type')} in {ctx.name(c)} keep their state in boolean flags; "
                     f"{tname} has {len(flags)} ({names}).")
        states = [n for n, _ in flags[:4]]
        moves = [f"Replace the flags of {tname} with one State enum (one value per valid combination of "
                 f"{', '.join(states)}), so impossible combinations cannot be stored.",
                 "Keep a timestamp per transition where the *_at fields need one."]
        out.append(ctx.finding(
            (c,), title,
            "; ".join(f"{t2.get('name') or t2id}: " + ", ".join(n + (f" (+{s})" if s else "") for n, s in fs)
                      for t2id, t2, fs in rows[:5]),
            "medium" if paired or len(flags) >= 4 else "weak",
            f"{plural(sum(len(r[2]) for r in rows), 'state flag')} in {plural(len(rows), 'type')}"
            + (f", {paired} paired with a timestamp" if paired else ""),
            [c], [r[1].get("file") for r in rows],
            {"component": c, "types": [{"type": r[0], "flags": [n for n, _ in r[2]],
                                        "timestamps": [s for _, s in r[2] if s]} for r in rows[:EXAMPLES]],
             "moves": moves},
            focus=types_focus({r[0]: [n for f in r[2] for n in f if n] for r in rows}, [c]),
            weight=Weight(25, "state flags").spread(len(flags), "flags", per=8, cap=12, free=params["min_flags"] - 1)
            .repetition(len(rows), "types", per=4, cap=10, single=0)
            .add("timestamps", paired, 6 if paired else 0)))
    return out
