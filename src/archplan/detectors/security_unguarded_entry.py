"""security.unguarded_entry: a traced entry point whose calls pass no guard.

A sequence starts at an entry point and lists the calls it makes. When that
entry point is declared `auth=required` and none of the calls is a function
declared `guard`, either the guard runs where the trace does not see it (a
middleware, a decorator) or the entry point is open. Entry points without an
auth declaration are reported too, as weak, once the project declares any
guard: their trace shows no guard either. `auth=none` entry points are skipped, and so is an entry point with any trace
that passes a guard. One finding per entry point, with the sequences that show it."""

from __future__ import annotations

from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import called, decls, matches, name, participant, short, target

ID = "security.unguarded_entry"
NAME = "Entry point without a guard on its traced path"
GROUP = "security"
NEEDS = {"declarations", "sequences"}
PARAMS = {"report_undeclared": True}


def entry_auth(ctx, entry: str, comp: str | None, auth: list) -> dict | None:
    for d in auth:
        if d["target"].get("id") == entry:
            return d
    for d in auth:
        if matches(target(ctx, d), short(entry), comp):
            return d
    return None


def run(ctx, params) -> list:
    guards = [target(ctx, d) for d in decls(ctx, "function", "guard") if d.get("value") is True]
    auth = decls(ctx, "entrypoint", "auth")
    by_entry = defaultdict(list)  # entry -> [(sequence id, components on its path)]
    declared, guarded = {}, set()
    for sid, seq in sorted((ctx.model.get("sequences") or {}).items()):
        entry, msgs = (seq or {}).get("entry"), [m for m in (seq or {}).get("messages") or [] if isinstance(m, dict)]
        if not entry or not msgs:
            continue
        first = participant(ctx, msgs[0].get("to"))
        d = entry_auth(ctx, entry, first, auth)
        value = d.get("value") if d else None
        if value == "none" or (value is None and not (params["report_undeclared"] and guards)):
            continue
        declared[entry] = d
        if any(matches(g, called(m), participant(ctx, m.get("to"))) for m in msgs for g in guards):
            guarded.add(entry)
            continue
        comps = [c for c in dict.fromkeys(participant(ctx, m.get("to")) for m in msgs) if c]
        by_entry[entry].append((sid, comps))
    out = []
    for entry, seqs in sorted(by_entry.items()):
        if entry in guarded:
            continue  # another trace of it passes a guard: this one may take an early exit
        d = declared[entry]
        comps = list(dict.fromkeys(c for _, cs in seqs for c in cs))
        ids = [s for s, _ in seqs]
        required = d is not None
        where = f"{d['file']}:{d['line']}" if d and d.get("file") else None
        through = ", ".join(name(ctx, c) for c in comps[:4]) or "no known component"
        out.append(finding(
            ID, (entry,),
            f"{short(entry)} needs auth, but its traced calls pass no guard." if required else
            f"{short(entry)} has no auth declaration, and its traced calls pass no guard.",
            f"{plural(len(ids), 'sequence')} ({', '.join(ids[:3])}{', …' if len(ids) > 3 else ''}) "
            f"{'go' if len(ids) > 1 else 'goes'} through {through} and call none of the "
            f"{plural(len(guards), 'declared guard')}.",
            "strong" if required else "weak",
            "auth=required, no guard in the trace" if required else "no auth declaration, no guard in the trace",
            comps[:1], [where.split(":")[0]] if where else [],
            {"entry": entry, "auth": d.get("value") if d else None, "declared_at": where, "sequences": ids[:20],
             "guards": [g["id"] for g in guards][:20],
             "moves": ["Call a guard on this path, or mark the function that checks auth with `archplan: guard`.",
                       "If the check runs in middleware the trace does not show, mark that middleware as a guard."
                       if required else
                       "Declare the entry point `auth=required` or `auth=none` with a reason."]},
            focus=[focus("sequences", ids[:20]), focus("components", comps[:20])],
            weight=Weight(70 if required else 30, "auth=required" if required else "no auth declaration")
            .spread(len(ids), "sequences", per=4, cap=10)))
    return out
