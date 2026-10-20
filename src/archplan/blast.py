"""Blast radius: what each traced entry point reaches, and what a change touches.

For each entry point the traces run, the radius lists the components its
requests pass, the guards they call, the sinks they reach and the zones they
cross, and whether a test shows it refusing a caller. Its risk adds what it
exposes (a write sink most, then a read sink; auth=authz more than
auth=required) to its gaps (no guard on its traced requests, no traced
refusal).

Turned around, a changed file touches the entry points that declare it as
their entry point, guard or sink, or whose requests pass its component.

Three uses: a security test plan (the entry points with gaps, by risk), a
review order for a diff, and risk hotspots (security.risk_hotspot). Traces
only cover what the tests run: an entry point no test runs has no radius."""

from __future__ import annotations

import re

from .core.hotspots import NotGit, git
from .detectors.security_common import call_file, component_of, decls, guards, guards_off, participant, target, zones
from .detectors.security_unguarded_entry import guarded, hit, runs
from .detectors.security_untested_denial import PARAMS, shows_refusal

EXPOSURE = {"write": 30, "read": 20}
AUTH = {"authz": 20, "required": 15}
GAPS = {"unguarded": 30, "no refusal": 20}


def entries(ctx) -> list:
    """The traced entry points, by risk: [{entry, auth, component,
    components, guards, sinks, zones, files, sequences, gaps, risk}]."""
    sinks = [(target(ctx, d), d.get("value")) for d in decls(ctx, "function", "sink")]
    every, zone = guards(ctx), zones(ctx)
    seqs = ctx.model.get("sequences") or {}
    titles = re.compile(PARAMS["titles"], re.I)
    by = {}
    for name, d, sid, msgs in runs(ctx):
        title = (seqs.get(sid) or {}).get("title") or sid
        if title.endswith("(failed)"):
            continue
        t = target(ctx, d) if d else None
        key = d["target"]["id"] if d else name
        e = by.setdefault(key, {"entry": key, "auth": d.get("value") if d else None,
                                "component": t["component"] if t else participant(ctx, msgs[0].get("to")),
                                "components": [], "guards": [], "sinks": {}, "files": [], "sequences": [],
                                "guarded": False, "refused": False, "judged": False})
        if sid not in e["sequences"]:
            e["sequences"].append(sid)
        e["components"] = list(dict.fromkeys(e["components"] + [c for c in (participant(ctx, m.get("to"))
                                                                            for m in msgs) if c]))
        off = guards_off(seqs.get(sid))  # ran without guards: reach, but no guard evidence
        e["judged"] = e["judged"] or not off
        e["guarded"] = e["guarded"] or (not off and guarded(ctx, d, msgs))
        used = [g for g in every if any(hit(ctx, g, m) for m in msgs)]
        e["guards"] += [g["id"] for g in used if g["id"] not in e["guards"]]
        for s, v in sinks:
            if any(hit(ctx, s, m) for m in msgs):
                e["sinks"][s["id"]] = "write" if "write" in (v, e["sinks"].get(s["id"])) else v or "read"
        need = guards(ctx, "authz") if e["auth"] == "authz" else every
        e["refused"] = e["refused"] or not off and shows_refusal(ctx, t or {"name": None, "component": None}, need, msgs,
                                                     title, titles)
        files = [t["file"] if t else None] + [g["file"] for g in used] \
            + [s["file"] for s, _ in sinks if s["id"] in e["sinks"]] + [call_file(ctx, m) for m in msgs]
        e["files"] = list(dict.fromkeys(e["files"] + [f for f in files if f]))
    out = []
    for e in by.values():
        e["zones"] = list(dict.fromkeys(zone[c] for c in e["components"] if zone.get(c)))
        ok, judged = e.pop("guarded"), e.pop("judged")
        e["gaps"] = (["unguarded"] if every and judged and not ok and e["auth"] != "none" else []) \
            + (["no refusal"] if ok and e["auth"] in ("required", "authz") and not e["refused"] else [])
        worst = "write" if "write" in e["sinks"].values() else "read" if e["sinks"] else None
        e["risk"] = (EXPOSURE.get(worst, 5) + AUTH.get(e["auth"], 10) + 5 * max(0, len(e["zones"]) - 1)
                     + sum(GAPS[g] for g in e["gaps"]))
        e["sinks"] = [{"id": k, "kind": v} for k, v in e["sinks"].items()]
        out.append(e)
    return sorted(out, key=lambda e: (-e["risk"], e["entry"]))


def touched(ctx, es: list, files: list) -> list:
    """The entry points a change of files touches, by risk, each with why:
    [{**entry, "because": [...]}]."""
    comps = {f: component_of(ctx, f) for f in files}
    out = []
    for e in es:
        why = [f"{f} is on its path" for f in files if f in e["files"]]
        why += [f"{f} is in {comps[f]}, which its requests pass" for f in files
                if f not in e["files"] and comps[f] in e["components"]]
        if why:
            out.append({**e, "because": why})
    return out


def plan(es: list) -> list:
    """The security test plan: the entry points with gaps, by risk, with the tests to add."""
    out = []
    for e in es:
        if not e["gaps"]:
            continue
        writes = [s["id"] for s in e["sinks"] if s["kind"] == "write"]
        tests = []
        if "unguarded" in e["gaps"]:
            tests.append("Call it as a caller who is not signed in, or may not act on what it names; expect a "
                         "refusal. If it succeeds, add the guard.")
        if "no refusal" in e["gaps"]:
            tests.append(f"Make {e['guards'][0] if e['guards'] else 'its guard'} refuse the caller"
                         + (f"; expect {', '.join(s['id'] for s in e['sinks'])} not to be called." if e["sinks"]
                            else "."))
        out.append({"entry": e["entry"], "auth": e["auth"], "risk": e["risk"], "gaps": e["gaps"],
                    "writes": writes, "start_from": e["sequences"][0], "tests": tests})
    return out


def hotspots(ctx, es: list) -> list:
    """Components that change often and lie on paths to sinks:
    [{component, commits, risk, entries, gaps}], by risk. Needs cached hotspots."""
    churn = ctx.churn
    if not churn:
        return []
    most = max(churn.values()) or 1
    out = []
    for c, n in churn.items():
        on = [e for e in es if c in e["components"] and e["sinks"]]
        if not on or not n:
            continue
        exposure = max(e["risk"] for e in on)
        out.append({"component": c, "commits": n, "risk": round(exposure * (0.5 + 0.5 * n / most)),
                    "entries": [e["entry"] for e in on],
                    "gaps": sorted({g for e in on for g in e["gaps"]})})
    return sorted(out, key=lambda x: (-x["risk"], x["component"]))


def changed(ctx, ref: str = "HEAD") -> list:
    """The files changed since the merge base of ref and HEAD, in the work
    tree too, and new files git does not ignore; relative to the model's base."""
    try:
        base = git(ctx.base, "merge-base", ref, "HEAD").strip()
        out = git(ctx.base, "diff", "--name-only", "--relative", base, "--") \
            + git(ctx.base, "ls-files", "--others", "--exclude-standard")
    except NotGit as e:
        raise ValueError(f"cannot diff against {ref}: {e}") from e
    return list(dict.fromkeys(f for f in out.splitlines() if f))


def report(ctx, files: list | None = None, limit: int = 20) -> dict:
    es = entries(ctx)
    out = {"entries": len(es), "traced": bool(ctx.model.get("sequences"))}
    if files is not None:
        hit_ = touched(ctx, es, files)
        out.update(files=files[:50], touched=hit_[:limit], plan=plan(hit_)[:limit])
    else:
        out.update(top=es[:limit], plan=plan(es)[:limit], hotspots=hotspots(ctx, es)[:limit])
    return out


def text(rep: dict) -> str:
    lines = []
    if not rep["traced"]:
        return "The model has no sequences: trace the tests first (see `archplan trace-elixir`)."
    if "touched" in rep:
        lines.append(f"{len(rep['files'])} changed files touch {len(rep['touched'])} of {rep['entries']} "
                     "traced entry points (risk, entry, why):")
        for e in rep["touched"]:
            lines.append(f"  {e['risk']:4}  {e['entry']}  {'; '.join(e['because'][:2])}")
    else:
        lines.append(f"{rep['entries']} traced entry points (risk, auth, sinks, gaps):")
        for e in rep["top"]:
            sinks = ", ".join(f"{s['kind']} {s['id']}" for s in e["sinks"][:3]) or "-"
            lines.append(f"  {e['risk']:4}  {e['auth'] or '?':8}  {e['entry']}  sinks: {sinks}"
                         + (f"  gaps: {', '.join(e['gaps'])}" if e["gaps"] else ""))
        if rep["hotspots"]:
            lines += ["", "Risk hotspots: change often, on paths to sinks (risk, commits, component):"]
            lines += [f"  {h['risk']:4}  {h['commits']:5}  {h['component']}" for h in rep["hotspots"]]
    lines += ["", "Security tests to add:" if rep["plan"] else "No gaps found on these entry points."]
    for p in rep["plan"]:
        lines.append(f"  {p['entry']} (start from {p['start_from']}):")
        lines += [f"    - {t}" for t in p["tests"]]
    return "\n".join(lines)
