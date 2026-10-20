"""security.risk_hotspot: a component that changes often and lies on the
traced path of entry points that reach sinks.

A change there can open or close access to data, so it deserves the closest
review and the security tests first. The risk is that of the riskiest entry
point through it (see blast.py: what it exposes and its gaps), scaled by how
often the component changes. Only components in the top quarter by commits,
with at least `min_commits`, are reported. One finding per component, with
the entry points and their gaps."""

from __future__ import annotations

from .. import blast
from .common import Weight, finding, focus, plural
from .security_common import name as comp

ID = "security.risk_hotspot"
NAME = "Often-changed component on the path to sensitive data"
GROUP = "security"
NEEDS = {"declarations", "sequences", "git"}
PARAMS = {"min_commits": 5, "limit": 10}


def run(ctx, params) -> list:
    churn = sorted(ctx.churn.values())
    if not churn:
        return []
    floor = max(params["min_commits"], churn[(3 * len(churn)) // 4])
    es = {e["entry"]: e for e in blast.entries(ctx)}
    out = []
    for h in blast.hotspots(ctx, list(es.values())):
        if h["commits"] < floor or len(out) >= params["limit"]:
            continue
        on = [es[x] for x in h["entries"]]
        writes = sorted({s["id"] for e in on for s in e["sinks"] if s["kind"] == "write"})
        gaps = [e["entry"] for e in on if e["gaps"]]
        name = comp(ctx, h["component"])
        out.append(finding(
            ID, (h["component"],),
            f"{name} changes often and lies on the path to sensitive data.",
            f"{h['commits']} commits. {plural(len(on), 'traced entry point')} {'pass' if len(on) > 1 else 'passes'} "
            f"it to reach " + (plural(len(writes), "write sink") if writes else "read sinks")
            + ("." if not gaps else "; it lacks a guard or a traced refusal." if len(on) == 1 else
               f"; {len(gaps)} of them {'lack' if len(gaps) > 1 else 'lacks'} a guard or a traced refusal."),
            "medium", "on paths to sinks, changes often", [h["component"]], [],
            {"commits": h["commits"], "risk": h["risk"], "entries": h["entries"][:20], "writes": writes[:20],
             "gaps": {e["entry"]: e["gaps"] for e in on if e["gaps"]},
             "moves": ["Review changes to it with the entry points above in mind.",
                       "Add the security tests `archplan blast` lists for them first."
                       if gaps else "Keep their refusal tests in the suite that runs on every change."]},
            focus=[focus("components", [h["component"]])],
            weight=Weight(20, "risk hotspot").add("exposure", h["risk"], min(30, h["risk"] / 4))
            .add("gaps", len(gaps), 10 if gaps else 0)))
    return out
