"""security.zone_dependency: a dependency that crosses trust zones the wrong way.

Zones are ordered from the least trusted to the most trusted (the `order`
setting). Code that handles outside input sits in an outer zone and calls
inward. Two kinds of dependency break that:

- outward: a component in a more trusted zone depends on one in a less
  trusted zone, so trusted code relies on code that handles raw input;
- skip: a component depends on one two or more zones further in, past the
  zone that should check its calls. Only zones the project declares count.

Zones not in `order` are left out. One finding per dependency."""

from __future__ import annotations

from .common import Weight, finding, focus
from .security_common import name, zones

ID = "security.zone_dependency"
NAME = "Dependency across trust zones"
GROUP = "security"
NEEDS = {"declarations", "deps"}
PARAMS = {"order": ["internet", "dmz", "internal", "restricted"], "report_skip": True}


def run(ctx, params) -> list:
    zone = zones(ctx)
    used = set(zone.values())
    rank = {z: i for i, z in enumerate(z for z in params["order"] or [] if z in used)}
    out = []
    for a, b in sorted(ctx.graph):
        za, zb = zone.get(a), zone.get(b)
        if za not in rank or zb not in rank or za == zb:
            continue
        step = rank[zb] - rank[za]
        if step < 0:
            what, strength, base = "outward", "medium", 55
            title = f"{name(ctx, a)} ({za}) depends on {name(ctx, b)} ({zb}), a less trusted zone."
        elif step >= 2 and params["report_skip"]:
            skipped = [z for z, i in rank.items() if rank[za] < i < rank[zb]]
            what, strength, base = "skip", "weak", 35
            title = f"{name(ctx, a)} ({za}) depends on {name(ctx, b)} ({zb}), past the {', '.join(skipped)} zone."
        else:
            continue
        out.append(finding(
            ID, (a, b),
            title,
            f"Zones in order of trust: {' → '.join(rank)}.",
            strength, f"{what}: {za} → {zb}", [a, b], [],
            {"from": a, "to": b, "from_zone": za, "to_zone": zb, "direction": what,
             "moves": ["Invert the dependency: let the outer component call in, "
                       "through an interface the inner one owns." if what == "outward" else
                       "Route the call through a component in the zone between, which checks it."]},
            focus=[focus("components", [a, b], [(a, b)])],
            weight=Weight(base, f"{what} dependency").add("zones", f"{za} → {zb}", 5 * (abs(step) - 1))))
    return out
