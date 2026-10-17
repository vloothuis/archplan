"""security.untraced: a declared guard or entry point that no trace reaches.

A guard that no sequence calls may be wired nowhere: configured, but inert.
An entry point that no sequence calls may be unreachable, such as a
route registered on a router that never receives it. Both are reported only
when the traces do pass through the declaration's component, so a component
the traces never visit gives no findings."""

from __future__ import annotations

from .common import Weight, finding, focus
from .security_common import called, decls, guards, matches, name, participant, target

ID = "security.untraced"
NAME = "Declared guard or entry point that no trace reaches"
GROUP = "security"
NEEDS = {"declarations", "sequences"}
PARAMS = {"report_guards": True, "report_entries": True}


def run(ctx, params) -> list:
    seqs = [s for s in (ctx.model.get("sequences") or {}).values() if isinstance(s, dict) and s.get("messages")]
    if not seqs:
        return []
    visited, calls = set(), set()
    for s in seqs:
        msgs = [m for m in s["messages"] if isinstance(m, dict)]
        for m in msgs:
            comp = participant(ctx, m.get("to"))
            visited.add(comp)
            calls.add((called(m), comp))
    out = []
    if params["report_guards"]:
        for g in guards(ctx):
            if g["component"] in visited and not any(matches(g, n, c) for n, c in calls):
                out.append(make(ctx, "guard", g, len(seqs)))
    if params["report_entries"]:
        for d in decls(ctx, "entrypoint", "auth"):
            e = target(ctx, d)
            if e["component"] in visited and not any(matches(e, n, c) for n, c in calls):
                out.append(make(ctx, "entry", e, len(seqs)))
    return out


def make(ctx, what: str, t: dict, n: int) -> dict:
    guard = what == "guard"
    where = f"{t['file']}:{t['line']}" if t.get("file") and t.get("line") else t.get("file")
    return finding(
        ID, (what, t["id"]),
        f"The guard {t['name']} is never called in the traces." if guard else
        f"The entry point {t['name']} is never called in the traces.",
        f"Some of the {n} traces pass through {name(ctx, t['component'])}, "
        f"but none calls {t['name']}.",
        "weak", "declared, not in any trace", [t["component"]], [t["file"]] if t.get("file") else [],
        {"what": what, "target": t["id"], "declared_at": where,
         "moves": (["Check that the guard is wired into the paths it should protect.",
                    "Or add a trace of a path that passes it."] if guard else
                   ["Check that the entry point is registered where requests reach it.",
                    "Or add a trace that calls it."])},
        focus=[focus("components", [t["component"]])],
        weight=Weight(30, "untraced guard" if guard else "untraced entry point"))
