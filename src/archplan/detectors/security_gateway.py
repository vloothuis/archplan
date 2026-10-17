"""security.gateway_bypass: a dependency that reaches a component around its gateways.

A component can declare the components through which others must reach it
(component.gateway), such as `gateway: [entitymanager]` on a store. Any other
component that depends on it bypasses them. Exempt are the gateways
themselves and components whose types implement the protected component's
interfaces: they are its backends, not its callers. A dependency may only
use the component's types, so on its own the finding is weak; a trace that
shows the bypassing call makes it strong.

One finding per dependency."""

from __future__ import annotations

from .common import Weight, finding, focus, plural
from .security_common import decls, name, participant

ID = "security.gateway_bypass"
NAME = "Dependency around a gateway"
GROUP = "security"
NEEDS = {"declarations", "deps"}
PARAMS = {}


def gateways(ctx) -> dict:
    """{component: [its gateways]}, for components in the model."""
    comps = ctx.components
    return {d["target"]["id"]: [g for g in d.get("value") or [] if g in comps]
            for d in decls(ctx, "component", "gateway") if d["target"].get("id") in comps}


def implementers(ctx, c: str) -> set:
    """Components whose types implement an interface that c defines."""
    types = ctx.model.get("types") or {}
    own = {tid for tid, t in types.items() if t.get("component") == c}
    return {t.get("component") for t in types.values()
            if t.get("component") != c and own & set(t.get("implements") or [])}


def traced(ctx) -> dict:
    """{(from, to): [sequence ids]} for calls between components in the traces."""
    out = {}
    for sid, s in sorted((ctx.model.get("sequences") or {}).items()):
        for m in s.get("messages") or []:
            a, b = participant(ctx, m.get("from")), participant(ctx, m.get("to"))
            if a and b and a != b and sid not in out.setdefault((a, b), []):
                out[(a, b)].append(sid)
    return out


def run(ctx, params) -> list:
    gate = gateways(ctx)
    if not gate:
        return []
    evidence = {(d["from"], d["to"]): d.get("evidence") or [] for d in ctx.model.get("dependencies") or []}
    seen = traced(ctx)
    out = []
    for c, gs in sorted(gate.items()):
        backends = implementers(ctx, c)
        for a, b in sorted(ctx.graph):
            if b != c or a in gs or a == c or a in backends:
                continue
            sids = seen.get((a, b), [])
            via = ", ".join(name(ctx, g) for g in gs) or "its gateways"
            w = Weight(35, "dependency around a declared gateway")
            if sids:
                w.add("traces", plural(len(sids), "trace"), 30)
            out.append(finding(
                ID, (a, b),
                f"{name(ctx, a)} depends on {name(ctx, b)} directly, not through {via}.",
                f"{name(ctx, b)} declares that it is reached through {via}."
                + (f" The call shows in {plural(len(sids), 'trace')}." if sids else ""),
                "weak", f"gateway: {', '.join(gs)}", [a, b], [e.split(":")[0] for e in evidence.get((a, b), [])],
                {"from": a, "to": b, "gateways": gs, "references": evidence.get((a, b), [])[:20],
                 "sequences": sids[:10],
                 "moves": [f"Call {via} instead.",
                           f"If {name(ctx, a)} may reach {name(ctx, b)}, add it to the gateway list "
                           "with a comment saying why."]},
                focus=[focus("components", [a, b, *gs], [(a, b)])],
                weight=w))
    return out
