"""history.coupling: components that change together without a dependency (cached hotspots)."""

from __future__ import annotations

from .common import Weight, comp_name, finding, focus

ID = "history.coupling"
NAME = "Hidden coupling"
GROUP = "history"
NEEDS = {"git"}
COUPLING_DEGREE = 0.5
COUPLING_TOGETHER = 5
# Sibling implementations of the same interface are expected to change together
# when the interface does, so such a pair needs a strong degree to be reported.
SIBLING_DEGREE = 0.8
PARAMS = {"degree": COUPLING_DEGREE, "together": COUPLING_TOGETHER, "sibling_degree": SIBLING_DEGREE}


def run(ctx, params) -> list:
    return coupling_findings(ctx.hotspots, ctx.model, params["degree"], params["together"],
                             params.get("sibling_degree", SIBLING_DEGREE))


def interfaces(model: dict) -> dict:
    """{component: interfaces its types implement that another component defines}."""
    types = model.get("types") or {}
    out = {}
    for t in types.values():
        c = t.get("component")
        for i in t.get("implements") or []:
            if isinstance(i, str) and (types.get(i) or {}).get("component") not in (None, c):
                out.setdefault(c, set()).add(i)
    return out


def coupling_findings(res: dict, model: dict, degree: float = COUPLING_DEGREE, together: int = COUPLING_TOGETHER,
                      sibling_degree: float = SIBLING_DEGREE) -> list:
    """Hidden coupling from the cached hotspots analysis. A pair whose
    components implement the same interface needs sibling_degree."""
    comps = model.get("components") or {}
    deps = {(d["from"], d["to"]) for d in model.get("dependencies") or []}
    impl = interfaces(model)
    out = []
    for x in res.get("pairs") or []:
        a, b = x["a"], x["b"]
        if a not in comps or b not in comps or (a, b) in deps or (b, a) in deps:
            continue
        if x["degree"] < degree or x["together"] < together:
            continue
        shared = sorted(impl.get(a, set()) & impl.get(b, set()))
        if shared and x["degree"] < sibling_degree:
            continue
        out.append(finding(
            "history.coupling", tuple(sorted((a, b))),
            f"{comp_name(model, a)} and {comp_name(model, b)} change together but do not depend on each other.",
            f"{x['together']} shared commits, degree {x['degree']:.2f}, in the last {res.get('since_days')} days."
            + (f" Both implement {shared[0]}{' and more' if len(shared) > 1 else ''}: sibling implementations "
               "change together when the interface does." if shared else ""),
            "strong" if x["degree"] >= 0.8 else "medium" if x["degree"] >= 0.65 else "weak",
            f"{x['together']} commits together, degree {x['degree']:.2f}", [a, b], [],
            {"a": a, "b": b, "together": x["together"], "degree": x["degree"], "head": (res.get("head") or "")[:10],
             "since_days": res.get("since_days"), **({"implement": shared[:5]} if shared else {})},
            focus=[focus("components", [a, b])],
            weight=Weight(30, "hidden coupling").add("degree", x["degree"], 50 * (x["degree"] - degree))
            .repetition(x["together"], "commits together", per=3, cap=10, single=0)
            .add("sibling implementations", len(shared), -25 if shared else 0)))
    return out
