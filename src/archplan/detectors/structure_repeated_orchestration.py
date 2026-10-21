"""structure.repeated_orchestration: several entry points that make the same
run of calls, such as load the order, check access, validate, save and
publish an event. The steps of one use case are repeated in each handler:
an application service, a pipeline step or a middleware is missing.

From the model's sequences. The steps of an entry point are the calls made
directly inside it (depth one below the entry message), in order; a call
repeated in a loop counts once. A run counts when it has min_steps or more
steps to at least two participants, and min_entries or more entry points
make it. A run that calls one function name throughout (call, call, call:
a plug or middleware chain) is already a pipeline and does not count. Runs
inside a longer reported run with the same entry points are left out.

A shape that only follows the layers (a handler that calls one service
method) is too short to count. Copies of the same steps can still mean
different things; the files of the entry points changing together in git
(the cached hotspots) is the evidence that they are one rule, and weighs."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, focus, plural
from .drift import drift, move
from .security_common import call_file, called, participant

ID = "structure.repeated_orchestration"
NAME = "Repeated orchestration"
GROUP = "structure"
NEEDS = {"sequences"}
PARAMS = {"min_steps": 3, "max_steps": 8, "min_entries": 3, "max_findings": 15}


def steps(msgs: list) -> list:
    """[(participant, function)] called directly inside the first message, repeats in a row once."""
    if not msgs:
        return []
    top = msgs[0].get("depth") or 0
    out = []
    for m in msgs[1:]:
        d = m.get("depth")
        if not isinstance(d, int) or d <= top:
            break
        if d == top + 1 and m.get("to"):
            s = (m["to"], called(m))
            if not out or out[-1] != s:
                out.append(s)
    return out


def runs(by_entry: dict, params) -> list:
    """[(run, entries)], longest and most used first, without runs inside a kept one."""
    seen = defaultdict(set)
    for e, ss in by_entry.items():
        for n in range(params["min_steps"], min(params["max_steps"], len(ss)) + 1):
            for i in range(len(ss) - n + 1):
                r = tuple(ss[i:i + n])
                if len({p for p, _ in r}) >= 2 and len({fn for _, fn in r}) >= 2:
                    seen[r].add(e)
    cands = sorted(((r, es) for r, es in seen.items() if len(es) >= params["min_entries"]),
                   key=lambda x: (-len(x[0]) * len(x[1]), -len(x[0]), x[0]))
    inside = lambda r, big: any(big[i:i + len(r)] == r for i in range(len(big) - len(r) + 1))
    kept = []
    for r, es in cands:
        if not any(es <= kes and inside(r, kr) for kr, kes in kept):
            kept.append((r, es))
    return kept[:params["max_findings"]]


def cochange(ctx, files: list) -> tuple:
    """(pairs of the files that change together, pairs of them) from the cached hotspots."""
    fs = sorted(set(f for f in files if f))
    pairs = {(x.get("a"), x.get("b")) for x in ((ctx.hotspots or {}).get("cochange") or {}).get("pairs") or []
             if isinstance(x, dict)}
    n = len(fs) * (len(fs) - 1) // 2
    return sum(1 for i, a in enumerate(fs) for b in fs[i + 1:] if (a, b) in pairs or (b, a) in pairs), n


def run(ctx, params) -> list:
    by_entry, seq_of, file_of, home = {}, defaultdict(list), {}, {}
    for sid, s in sorted((ctx.model.get("sequences") or {}).items()):
        msgs = [m for m in (s or {}).get("messages") or [] if isinstance(m, dict)] if isinstance(s, dict) else []
        if not msgs:
            continue
        e = s.get("entry") or msgs[0].get("label") or sid
        ss = steps(msgs)
        seq_of[e].append(sid)
        if len(ss) > len(by_entry.get(e, [])):
            by_entry[e] = ss
            file_of[e] = call_file(ctx, msgs[0])
            home[e] = participant(ctx, msgs[0].get("to"))
    out = []
    label = lambda p: ctx.name(p) if p in ctx.components else (ctx.types.get(p) or {}).get("name") or p
    for r, es in runs(by_entry, params):
        es = sorted(es)
        comps = list(dict.fromkeys(c for c in (participant(ctx, p) for p, _ in r) if c))
        entry_comps = sorted({home[e] for e in es if home.get(e)})
        shown = " → ".join(f"{label(p)}.{fn}" for p, fn in r)
        together, pairs = cochange(ctx, [file_of.get(e) for e in es])
        d = drift(ctx.hotspots, [file_of.get(e) for e in es])
        missed = d["fixes"][0] if d and d["fixes_apart"] else None
        title = f"{plural(len(es), 'entry point')} make the same {len(r)} calls: {shown}."
        out.append(ctx.finding(
            tuple(f"{p}:{fn}" for p, fn in r), title,
            "In " + ", ".join(es[:4]) + (f" and {len(es) - 4} more" if len(es) > 4 else "") + "."
            + (f" Their files change together in {together} of {pairs} pairs." if pairs and ctx.hotspots else "")
            + (f" {plural(d['fixes_apart'], 'bug fix', 'bug fixes')} changed some of them and missed others, "
               f"such as '{missed['subject']}'." if missed else ""),
            "medium", f"{plural(len(r), 'call')} repeated by {plural(len(es), 'entry point')}",
            entry_comps + comps, [file_of.get(e) for e in es],
            {"steps": [{"participant": p, "function": fn} for p, fn in r], "entries": es[:EXAMPLES],
             **({"cochange": {"pairs": pairs, "together": together}} if pairs and ctx.hotspots else {}),
             **({"history": d} if d else {}),
             "moves": move(d) + ["Give the use case one home: an application service or pipeline step that makes these "
                       "calls, and let each entry point call it.",
                       "If the calls are a cross-cutting concern (access checks, transactions, logging), move "
                       "them into a middleware or decorator instead.",
                       "Leave them if the entry points will grow apart: shared steps today are not one rule."]},
            focus=[focus("sequences", [sid for e in es for sid in seq_of[e]][:EXAMPLES]),
                   focus("components", entry_comps + comps)],
            weight=Weight(25, "repeated orchestration").spread(len(es), "entry points", per=6, cap=15,
                                                               free=params["min_entries"] - 1)
            .spread(len(r), "steps", per=6, cap=12, free=params["min_steps"] - 1)
            .add("changes together", f"{together} of {pairs} pairs",
                 15 * together / pairs - 5 if pairs and ctx.hotspots else 0)
            .add("fixes that missed a copy", d["fixes_apart"] if d else 0,
                 min(12, 5 * d["fixes_apart"]) if d else 0)))
    return out
