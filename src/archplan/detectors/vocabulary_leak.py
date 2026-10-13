"""vocabulary.leak: a component uses terms of a concept it should not (from the glossaries)."""

from __future__ import annotations

from ..model import leaks
from .common import OCCURRENCE, Weight, comp_name, evidence_file, finding, focus

ID = "vocabulary.leak"
NAME = "Vocabulary leaks"
GROUP = "vocabulary"
NEEDS = {"vocabulary"}
PARAMS = {}



def run(ctx, params) -> list:
    return leak_findings(ctx.target)


def contexts(x: dict) -> dict:
    """{context: count} for a usage row; rows written before contexts were
    recorded count identifiers as local names."""
    cx = x.get("contexts")
    if isinstance(cx, dict):
        return {k: int(cx.get(k) or 0) for k in OCCURRENCE if isinstance(cx.get(k), int) and cx.get(k) > 0}
    old = {"local": x.get("idents"), "string": x.get("strings"), "comment": x.get("comments")}
    return {k: v for k, v in old.items() if isinstance(v, int) and v > 0}


def leak_findings(target: dict) -> list:
    out = []
    concepts = ((target or {}).get("vocabulary") or {}).get("concepts", {})
    for x in leaks(target or {}):
        c, k = x.get("component"), x.get("concept")
        name = concepts.get(k, {}).get("name") or k
        n = x.get("count") or len(x.get("evidence") or x.get("terms") or []) or None
        why = "it lists the concept under avoid" if x["why"] == "avoid" else \
            f"the policy forbids it to depend on {x.get('owner')}, which owns it"
        cx = contexts(x)
        total = sum(cx.values()) or n or 1
        files = {evidence_file(e) for e in x.get("evidence") or [] if isinstance(e, str)}
        w = (Weight(30, "leak").add("rule", x["why"], 5 if x["why"] == "avoid" else 0)
             .occurrence(cx).repetition(total, per=4, cap=14, single=-6).spread(len(files), "files", per=3, cap=9))
        only = [k for k, v in cx.items() if v]
        note = "Only in comments. " if only == ["comment"] else "Only in strings. " if only == ["string"] else ""
        out.append(finding(
            "vocabulary.leak", (c, k),
            f"{comp_name(target, c)} uses the term \"{name}\", owned by {x.get('owner') or 'no component'}.",
            f"{note}It should not: {why}.", "strong" if x["why"] == "avoid" else "medium",
            "listed under avoid in the glossary" if x["why"] == "avoid" else "follows from the policy",
            [c, x.get("owner")], [evidence_file(e) for e in x.get("evidence") or [] if isinstance(e, str)],
            {"anchor": f"leak:{c}:{k}", "concept": k, "owner": x.get("owner"), "why": x["why"],
             "contexts": cx, **({"occurrence": note.strip(" .").lower()} if note else {}),
             **({"count": n} if n else {})},
            focus=[focus("glossary", [k], [(c, k)]), focus("components", [c, x.get("owner")])], weight=w))
    return out
