"""history.coupling: components that change together without a dependency (cached hotspots)."""

from __future__ import annotations

from .common import Weight, comp_name, finding, focus

ID = "history.coupling"
NAME = "Hidden coupling"
GROUP = "history"
NEEDS = {"git"}
COUPLING_DEGREE = 0.5
COUPLING_TOGETHER = 5
PARAMS = {"degree": COUPLING_DEGREE, "together": COUPLING_TOGETHER}



def run(ctx, params) -> list:
    return coupling_findings(ctx.hotspots, ctx.model, params["degree"], params["together"])


def coupling_findings(res: dict, model: dict, degree: float = COUPLING_DEGREE, together: int = COUPLING_TOGETHER) -> list:
    """Hidden coupling from the cached hotspots analysis."""
    comps = model.get("components") or {}
    deps = {(d["from"], d["to"]) for d in model.get("dependencies") or []}
    out = []
    for x in res.get("pairs") or []:
        a, b = x["a"], x["b"]
        if a not in comps or b not in comps or (a, b) in deps or (b, a) in deps:
            continue
        if x["degree"] < degree or x["together"] < together:
            continue
        out.append(finding(
            "history.coupling", tuple(sorted((a, b))),
            f"{comp_name(model, a)} and {comp_name(model, b)} change together but do not depend on each other.",
            f"{x['together']} shared commits, degree {x['degree']:.2f}, in the last {res.get('since_days')} days.",
            "strong" if x["degree"] >= 0.8 else "medium" if x["degree"] >= 0.65 else "weak",
            f"{x['together']} commits together, degree {x['degree']:.2f}", [a, b], [],
            {"a": a, "b": b, "together": x["together"], "degree": x["degree"], "head": (res.get("head") or "")[:10],
             "since_days": res.get("since_days")},
            focus=[focus("components", [a, b])],
            weight=Weight(30, "hidden coupling").add("degree", x["degree"], 50 * (x["degree"] - degree))
            .repetition(x["together"], "commits together", per=3, cap=10, single=0)))
    return out
