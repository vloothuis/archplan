"""security.untested_denial: an entry point whose traces show the guard
letting callers through, but never turning one away.

A test that passes the guard shows the path works for a caller who may use
it. It does not show that the guard refuses a caller who may not: a guard
that always says yes passes the same test. A traced request shows a refusal
when a guard it needs, or the entry point itself, refused: the trace records
the call's `outcome` (`raise`, `error`, `false`, `halt`, or `status:4xx`).
Traces without outcomes, such as those of other exporters, show a refusal by
their title only, such as "rejects another user's order": then findings are
weak. Entry points declared `auth=required` or `auth=authz` with a traced
request that passes the guard they need are judged; one with no such request
is for security.unguarded_entry. Failed tests are left out. The finding names
the guard to make fail and the sinks the request reaches, so a test can
check they are not reached."""

from __future__ import annotations

import re
from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import decls, guards, participant, target
from .security_unguarded_entry import guarded, hit, runs

ID = "security.untested_denial"
NAME = "Entry point whose traces never show its guard refusing"
GROUP = "security"
NEEDS = {"declarations", "sequences"}
PARAMS = {"titles": r"\b(?:forbidden|unauthori[sz]ed|unauthenticated|den(?:y|ies|ied)|refus\w*|reject\w*|"
                    r"disallow\w*|not allowed|not permitted|cannot|can't|without (?:access|permission|login)|"
                    r"another user|other user|non-?owner|not (?:the )?owner|anonymous|logged out|signed out|"
                    r"40[134])\b"}

REFUSED = re.compile(r"^(?:raise|error|false|halt|status:4\d\d)$")


def refused(m: dict) -> bool:
    return bool(REFUSED.match(str(m.get("outcome") or "")))


def shows_refusal(ctx, entry: dict, need: list, msgs: list, title: str, titles) -> bool:
    """Whether a request shows a refusal: the entry point or a guard of need
    refused, or its test's title says it does."""
    return (any(refused(m) and (hit(ctx, entry, m) or any(hit(ctx, g, m) for g in need)) for m in msgs)
            or bool(titles and titles.search(title)))


def run(ctx, params) -> list:
    seqs = ctx.model.get("sequences") or {}
    if not guards(ctx):
        return []
    outcomes = any("outcome" in m for s in seqs.values() if isinstance(s, dict)
                   for m in s.get("messages") or [] if isinstance(m, dict))
    titles = re.compile(params["titles"], re.I) if params["titles"] else None
    sinks = [(target(ctx, d), d.get("value")) for d in decls(ctx, "function", "sink")]
    passes = defaultdict(list)  # entry id -> [(sequence id, request messages)]
    denied, decl = set(), {}
    for _, d, sid, msgs in runs(ctx):
        if not d or d.get("value") not in ("required", "authz"):
            continue
        title = (seqs.get(sid) or {}).get("title") or sid
        if title.endswith("(failed)"):
            continue
        eid = d["target"]["id"]
        need = guards(ctx, "authz") if d["value"] == "authz" else guards(ctx)
        if shows_refusal(ctx, target(ctx, d), need, msgs, title, titles):
            denied.add(eid)
        elif guarded(ctx, d, msgs):
            decl[eid] = d
            passes[eid].append((sid, msgs))
    out = []
    for eid in sorted(set(passes) - denied):
        d, runs_ = decl[eid], passes[eid]
        t, authz = target(ctx, d), d["value"] == "authz"
        need = guards(ctx, "authz") if authz else guards(ctx)
        msgs = [m for _, ms in runs_ for m in ms]
        used = [g for g in need if any(hit(ctx, g, m) for m in msgs)]
        reached = [(s, v) for s, v in sinks if any(hit(ctx, s, m) for m in msgs)]
        writes = [s["name"] for s, v in reached if v == "write"]
        reads = [s["name"] for s, v in reached if v != "write"]
        comps = [c for c in dict.fromkeys(participant(ctx, m.get("to")) for m in msgs) if c]
        ids = [s for s, _ in runs_]
        guard = used[0]["name"] if used else need[0]["name"]
        caller = "a caller who may not act on what the request names" if authz else "a caller who is not signed in"
        expect = (f"; expect {', '.join(writes + reads)} not to be called" if reached else "")
        w = Weight(50 if authz else 40, f"auth={d['value']}")
        w.add("reaches", "write sink" if writes else "read sink", 10 if writes else 5 if reads else 0)
        w.add("evidence", "test titles only", 0 if outcomes else -15)
        out.append(finding(
            ID, (eid,),
            f"No test shows {t['name']} refusing a caller.",
            f"{plural(len(ids), 'traced request')} ({', '.join(ids[:3])}{', …' if len(ids) > 3 else ''}) "
            f"{'pass' if len(ids) > 1 else 'passes'} {', '.join(g['name'] for g in used[:3]) or 'the guard'}, "
            f"but no trace shows {'it' if len(used) <= 1 else 'them'} or {t['name']} refusing one"
            + ("." if outcomes else
               ", and the traces record no call outcomes, so only test titles were read.")
            + (f" The request reaches {', '.join(writes + reads)}." if reached else ""),
            "medium" if outcomes else "weak",
            f"auth={d['value']}, guard passed, no refusal traced",
            [t["component"]], [t["file"]] if t.get("file") else [],
            {"entry": eid, "auth": d["value"], "declared_at": f"{t['file']}:{t['line']}" if t.get("file") else None,
             "sequences": ids[:20], "guards": [g["id"] for g in used or need][:20],
             "sinks": [s["id"] for s, _ in reached][:20],
             "moves": [f"Add a test that calls {t['name']} as {caller}: make {guard} refuse{expect}.",
                       f"Start from {ids[0]}, the traced test that passes the guard, and run it with tracing "
                       "so the trace records the refusal."]},
            focus=[focus("sequences", ids[:20]), focus("components", comps[:20])],
            weight=w))
    return out
