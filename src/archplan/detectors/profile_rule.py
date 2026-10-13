"""profile.rule: a unit breaks a calls rule of the architecture profile."""

from __future__ import annotations

from collections import defaultdict

from ..hotspots import Mapper
from .common import EXAMPLES, Weight, comp_name, finding, focus, plural, strength_of

ID = "profile.rule"
NAME = "Profile call rules"
GROUP = "profile"
NEEDS = {"profile", "files"}
PARAMS = {}



def run(ctx, params) -> list:
    prof, a = ctx.profile
    calls = lambda r: r.get("type") != "pair"
    return profile_findings({**prof, "rules": [r for r in prof.get("rules") or [] if calls(r)]},
                            {**a, "rules": [r for r in a["rules"] if calls(r)]}, ctx.model)


def profile_findings(prof: dict, analysis: dict, model: dict) -> list:
    roles = {r["id"]: r.get("name") or r["id"] for r in prof.get("roles") or []}
    rules = {r.get("id"): r for r in prof.get("rules") or []}
    units = {u["id"]: u for u in analysis["units"]}
    mapper = Mapper(model.get("components") or {})
    out = []
    for r in analysis["rules"]:
        by_unit = defaultdict(list)
        for b in r["breaks"]:
            by_unit[b["unit"]].append(b)
        checked = r.get("units_checked") or len(units)
        follow = max(checked - len(by_unit), 0)
        strength = strength_of(follow / checked) if checked else "weak"
        basis = f"{follow} of {checked} units follow it"
        rule = rules.get(r["id"]) or {}
        pair = r.get("type") == "pair"
        for uid, bs in sorted(by_unit.items()):
            u = units.get(uid) or {}
            where = comp_name(model, u["component"]) if u.get("component") else uid
            n = len(bs)
            if pair:
                kind = "profile.pair"
                title = (f"In {where}, {plural(n, roles.get(rule.get('role'), 'file') + ' file')} "
                         f"{'has' if n == 1 else 'have'} no matching {roles.get(rule.get('needs'), 'partner')}.")
            else:
                kind = "profile.rule"
                title = f"In {where}, {plural(n, 'reference')} {'breaks' if n == 1 else 'break'} the rule: {r['text']}."
            detail = "; ".join(f"{b['from']} -> {b.get('to') or '?'} ({b['detail']})" for b in bs[:3]) \
                + (f"; and {n - 3} more" if n > 3 else "")
            comps = [u.get("component")] + [mapper(b["from"]) for b in bs] + [mapper(b["to"]) for b in bs if b.get("to")]
            files = [b["from"] for b in bs] + [b["to"] for b in bs if b.get("to") and not pair]
            out.append(finding(kind, (r["id"], uid), title, detail, strength, basis, comps, files,
                               {"rule": r["id"], "text": r["text"], "count": n, "breaks": bs[:EXAMPLES]}, uid,
                               [focus("roles", [uid], files=files[:EXAMPLES]),
                                focus("components", [c for c in comps if c])],
                               Weight(25 if pair else 35, "file pair" if pair else "call rule")
                               .add("conformance", basis, 15 if strength == "strong" else 8 if strength == "medium" else 0)
                               .repetition(n, "breaks", per=4, cap=12, single=-5)
                               .spread(len({b["from"] for b in bs}), "files", per=3, cap=6)))
    return out
