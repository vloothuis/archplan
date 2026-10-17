"""boundaries.stability: Robert Martin's package metrics per component.

  Ca  afferent coupling: components that depend on it
  Ce  efferent coupling: components it depends on
  I   instability, Ce / (Ca + Ce): 0 is stable, 1 is unstable
  A   abstractness: interfaces (and behaviours, protocols) / types, without
      structs and records that only hold data
  D   distance from the main sequence, |A + I - 1|

A stable, concrete component (low I, low A) is in the zone of pain: many
depend on it and it has no abstractions to change behind. An unstable,
abstract one (high I, high A) is in the zone of uselessness: abstractions
nobody uses. The metrics are in the result's extras, and at GET /api/metrics."""

from __future__ import annotations

from collections import defaultdict

from .common import Weight, focus, graph

ID = "boundaries.stability"
NAME = "Stability and abstractness"
GROUP = "boundaries"
NEEDS = {"deps", "types"}
PARAMS = {"min_types": 3, "pain_dependents": 5, "low": 0.3, "high": 0.7, "distance": 0.6}
ABSTRACT = ("interface",)
COUNTED = ("class", "struct", "record", "interface", "module")


def data_only(t: dict) -> bool:
    """A struct or record with fields and no methods: a value, not something to put behind an interface."""
    return t.get("kind") in ("struct", "record") and bool(t.get("fields")) and not t.get("methods")


def ports(model: dict, c: str) -> bool:
    """Whether most of the interfaces of c are implemented in other components:
    c declares what it needs (as Go does), so its abstractions are in use."""
    types = model.get("types") or {}
    own = {tid for tid, t in types.items() if t.get("component") == c and t.get("kind") in ABSTRACT}
    used = {i for t in types.values() if t.get("component") != c for i in t.get("implements") or [] if i in own}
    return bool(own) and len(used) * 2 >= len(own)


def metrics(model: dict, min_types: int = 3) -> dict:
    """{component: {ca, ce, i, a, d, types, abstract}}; a and d are None for
    components with fewer than min_types types, i for isolated ones."""
    comps = {c for c, v in (model.get("components") or {}).items() if (v or {}).get("kind") != "external"}
    ca, ce = defaultdict(set), defaultdict(set)
    for a, b in graph(model):
        ce[a].add(b)
        ca[b].add(a)
    n, ab = defaultdict(int), defaultdict(int)
    for t in (model.get("types") or {}).values():
        c = t.get("component")
        if c in comps and t.get("kind") in COUNTED and not data_only(t):
            n[c] += 1
            ab[c] += t.get("kind") in ABSTRACT or bool(t.get("abstract"))
    out = {}
    for c in sorted(comps):
        i = len(ce[c]) / (len(ca[c]) + len(ce[c])) if ca[c] or ce[c] else None
        a = ab[c] / n[c] if n[c] >= min_types else None
        d = abs(a + i - 1) if a is not None and i is not None else None
        out[c] = {"ca": len(ca[c]), "ce": len(ce[c]), "i": None if i is None else round(i, 3),
                  "a": None if a is None else round(a, 3), "d": None if d is None else round(d, 3),
                  "types": n[c], "abstract": ab[c]}
    return out


def run(ctx, params) -> list:
    ms = metrics(ctx.model, params["min_types"])
    ctx.extras["metrics"] = ms
    out = []
    for c, m in ms.items():
        if m["d"] is None or m["d"] < params["distance"]:
            continue
        nums = f"Ca {m['ca']}, Ce {m['ce']}, I {m['i']:.2f}, A {m['a']:.2f}, D {m['d']:.2f}"
        # A component that depends on nothing (Ce 0) is a foundation, such as value types:
        # it changes for its own reasons only, so being stable and concrete costs little.
        if m["i"] <= params["low"] and m["a"] <= params["low"] and m["ca"] >= params["pain_dependents"] and m["ce"]:
            out.append(ctx.finding(
                (c, "pain"),
                f"{ctx.name(c)} is in the zone of pain: {m['ca']} components depend on it, and it has "
                f"{'no' if not m['abstract'] else 'few'} abstractions to change behind.",
                f"{nums}. {m['abstract']} of {m['types']} types are interfaces.",
                "medium" if m["ca"] >= 2 * params["pain_dependents"] else "weak", nums, [c], [],
                {"component": c, "zone": "pain", **m,
                 "moves": [f"Put the parts of {ctx.name(c)} that others need behind interfaces they own, "
                           "so it can change without them.",
                           f"Or split {ctx.name(c)} so the stable core is small and the rest can change freely."]},
                focus=[focus("components", [c])],
                weight=Weight(30, "zone of pain").spread(m["ca"], "dependents", per=5, cap=15,
                                                          free=params["pain_dependents"])
                .add("distance", m["d"], 20 * (m["d"] - params["distance"]))))
        elif m["i"] >= params["high"] and m["a"] >= params["high"] and not ports(ctx.model, c):
            out.append(ctx.finding(
                (c, "uselessness"),
                f"{ctx.name(c)} is in the zone of uselessness: mostly abstractions, and few components depend "
                f"on it ({m['ca']}).",
                f"{nums}.", "weak", nums, [c], [],
                {"component": c, "zone": "uselessness", **m,
                 "moves": [f"Remove the interfaces in {ctx.name(c)} that have one implementation and no other user.",
                           f"Or move them next to the code that implements them."]},
                focus=[focus("components", [c])],
                weight=Weight(20, "zone of uselessness").add("distance", m["d"], 20 * (m["d"] - params["distance"]))))
    return out
