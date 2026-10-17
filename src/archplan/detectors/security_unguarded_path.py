"""security.unguarded_path: a dependency path from an entry point's component to
protected data on which no component can call a guard.

This is a check on the static component graph, so it also covers code that no
trace runs. A component on the path can check access when it is a guard's
component, depends directly on one, or is a declared gateway of the target.
The entry point's own component is left out: it usually calls the guard and
the store itself, and only a trace can tell in which order (see
security.unguarded_sink). What remains are paths through other components,
such as an export or a script runner, that reach the data while none of them
can check access. A dependency may only use types, so findings are weak:
candidates to confirm with a trace.

Entry components are those of declared entry points with auth=required and
those in the `entry_zones`. Targets are the components in the `target_zones`
and, with `sink_targets`, those of declared sinks. Sinks are off by default:
a sink often sits in a hub component, such as a Repo in Core, that every
component depends on. One finding per first step and target."""

from __future__ import annotations

from collections import deque

from .common import Weight, finding, focus, plural
from .security_common import decls, guards, name, target, zones
from .security_gateway import gateways

ID = "security.unguarded_path"
NAME = "Path to protected data with no guard on it"
GROUP = "security"
NEEDS = {"declarations", "deps"}
PARAMS = {"entry_zones": ["internet"], "target_zones": ["restricted"], "sink_targets": False, "max_hops": 4}


def path(succ: dict, start: str, goal: str, blocked, max_hops: int) -> list | None:
    """The shortest path from start to goal over nodes not blocked, or None."""
    prev, todo = {start: None}, deque([(start, 0)])
    while todo:
        n, hops = todo.popleft()
        if n == goal:
            out = []
            while n is not None:
                out.append(n)
                n = prev[n]
            return out[::-1]
        if hops >= max_hops:
            continue
        for m in sorted(succ.get(n, ())):
            if m not in prev and (m == goal or not blocked(m)):
                prev[m] = n
                todo.append((m, hops + 1))
    return None


def run(ctx, params) -> list:
    zone = zones(ctx)
    guard = {g["component"] for g in guards(ctx) if g["component"]}
    if not guard:
        return []
    entries = {target(ctx, d)["component"] for d in decls(ctx, "entrypoint", "auth") if d.get("value") == "required"}
    entries |= {c for c, z in zone.items() if z in (params["entry_zones"] or [])}
    goals = {c for c, z in zone.items() if z in (params["target_zones"] or [])}
    if params["sink_targets"]:
        goals |= {target(ctx, d)["component"] for d in decls(ctx, "function", "sink")}
    entries.discard(None)
    goals.discard(None)
    succ = {}
    for a, b in ctx.graph:
        succ.setdefault(a, set()).add(b)
    checks = guard | {a for a, bs in succ.items() if bs & guard}
    gate = gateways(ctx)
    hits = {}
    for e in sorted(entries):
        for x in sorted(succ.get(e, ())):
            if x in checks or x in goals or x in entries:
                continue
            for t in sorted(goals - {e}):
                p = path(succ, x, t, lambda n: n in checks or n in gate.get(t, ()), params["max_hops"])
                if p:
                    hits.setdefault((x, t), {"path": p, "entries": []})["entries"].append(e)
    # A path through a component that has its own finding repeats it.
    hits = {(x, t): h for (x, t), h in hits.items() if not any((y, t) in hits for y in h["path"][1:-1])}
    out = []
    for (x, t), h in sorted(hits.items()):
        p, es = h["path"], h["entries"]
        chain = " → ".join(name(ctx, c) for c in p)
        w = Weight(35, "unguarded path").add("length", plural(len(p) - 1, "step"), -6 * (len(p) - 2))
        w.spread(len(es), "entry components", per=4, cap=12)
        out.append(finding(
            ID, (x, t),
            f"{name(ctx, x)} reaches {name(ctx, t)} with no component on the way that can check access.",
            f"Path: {chain}. It starts from {', '.join(name(ctx, e) for e in es)}, which "
            f"{'has' if len(es) == 1 else 'have'} entry points. No component on it is a guard's, depends on "
            f"one ({', '.join(name(ctx, g) for g in sorted(guard))}) or is a gateway of {name(ctx, t)}.",
            "weak", f"static path: {chain}", [*es, *p], [],
            {"from": x, "to": t, "path": p, "entries": es, "guards": sorted(guard),
             "moves": [f"Let {name(ctx, x)} reach {name(ctx, t)} through a component that checks access.",
                       f"If {name(ctx, x)} checks access through a caller, declare that caller a gateway of "
                       f"{name(ctx, t)}."]},
            focus=[focus("components", [*es, *p], list(zip(p, p[1:])))],
            weight=w))
    return out
