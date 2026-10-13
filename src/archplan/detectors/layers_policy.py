"""layers.policy: a dependency the target's policy forbids."""

from __future__ import annotations

from ..model import allowed
from .common import EXAMPLES, Weight, comp_name, evidence_file, finding, focus, graph, plural

ID = "layers.policy"
NAME = "Policy violations"
GROUP = "layers"
NEEDS = {"deps"}
PARAMS = {}



def run(ctx, params) -> list:
    return policy_findings(ctx.model, ctx.target)


def policy_findings(model: dict, target: dict) -> list:
    pol = (target or {}).get("policy") or {}
    if not pol.get("rules") and pol.get("default", "allow") == "allow":
        return []
    out = []
    for (a, b), e in sorted(graph(model).items()):
        if allowed(target, a, b):
            continue
        rules = [r for r in pol.get("rules", []) if r.get("from") in (a, "*") and r.get("to") in (b, "*")]
        out.append(finding(
            "layers.policy", (a, b),
            f"{comp_name(model, a)} depends on {comp_name(model, b)}, which the policy forbids.",
            f"{plural(e['weight'], 'reference')}: " + ", ".join(e["evidence"][:3]),
            "strong", "an explicit rule in the target's policy" if rules else "the policy's default is deny",
            [a, b], [evidence_file(x) for x in e["evidence"]],
            {"from": a, "to": b, "weight": e["weight"], "evidence": e["evidence"][:EXAMPLES], "rules": rules},
            focus=[focus("components", [a, b], [(a, b)])],
            weight=Weight(50, "policy break").add("rule", "explicit" if rules else "default deny", 8 if rules else 0)
            .repetition(e["weight"], "references", per=3, cap=12, single=-6)))
    return out
