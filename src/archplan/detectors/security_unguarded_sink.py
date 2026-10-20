"""security.unguarded_sink: a trace that reaches a sink before any authorization guard.

A sink is a function declared `sink=write` or `sink=read`: it changes or
reads data that only some callers may change or read. In each sequence, a
write sink must come after a call of an authorization guard (`guard` or
`guard=authz`). A read sink needs one anywhere in the sequence: code often
reads candidates first and filters them after. An authentication guard (`guard=authn`) only says who the
caller is, so it does not count: a signed-in reader who reaches a write sink
without a permission check is what this detector is for. Calls inside any
guard are the guard's own: an authentication guard may read the caller's
record before anything is authorized. Sequences marked `"guards": "off"`
ran without guards and are left out.

One finding per entry point and sink, with the sequences that show it."""

from __future__ import annotations

from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import called, decls, guards, guards_off, matches, participant, short, target

ID = "security.unguarded_sink"
NAME = "Sink reached without an authorization guard"
GROUP = "security"
NEEDS = {"declarations", "sequences"}
PARAMS = {"report_read": True}


def run(ctx, params) -> list:
    authz, every = guards(ctx, "authz"), guards(ctx)
    sinks = [dict(target(ctx, d), kind=d.get("value")) for d in decls(ctx, "function", "sink")
             if d.get("value") == "write" or d.get("value") == "read" and params["report_read"]]
    if not sinks:
        return []
    hits = defaultdict(list)  # (entry, sink id) -> [sequence id]
    sink_of = {s["id"]: s for s in sinks}
    first = {}  # sequence id -> the component of its entry
    for sid, seq in sorted((ctx.model.get("sequences") or {}).items()):
        entry, msgs = (seq or {}).get("entry"), [m for m in (seq or {}).get("messages") or [] if isinstance(m, dict)]
        if not entry or not msgs or guards_off(seq):
            continue
        first[sid] = participant(ctx, msgs[0].get("to"))
        checked, seen, inside, reads = False, set(), None, []
        for m in msgs:
            fn, comp, depth = called(m), participant(ctx, m.get("to")), m.get("depth") or 0
            if inside is not None and depth > inside:
                continue  # the guard's own lookups, such as reading the caller's record
            inside = None
            if any(matches(g, fn, comp) for g in authz):
                checked = True
            if checked:
                continue
            if any(matches(g, fn, comp) for g in every):
                inside = depth
                continue
            for s in sinks:
                if s["id"] not in seen and matches(s, fn, comp):
                    seen.add(s["id"])
                    if s["kind"] == "write":
                        hits[(entry, s["id"])].append(sid)
                    else:
                        reads.append(s["id"])
        if not checked:  # no check anywhere: the reads are unfiltered too
            for skid in reads:
                hits[(entry, skid)].append(sid)
    out = []
    for (entry, skid), seqs in sorted(hits.items()):
        s = sink_of[skid]
        write = s["kind"] == "write"
        comps = [c for c in dict.fromkeys((first[seqs[0]], s["component"])) if c]
        out.append(finding(
            ID, (entry, skid),
            f"{short(entry)} {'writes' if write else 'reads'} through {s['name']} "
            f"{'before' if write else 'without'} any authorization check.",
            f"{plural(len(seqs), 'sequence')} ({', '.join(seqs[:3])}{', …' if len(seqs) > 3 else ''}) call "
            f"{s['name']} with none of the {plural(len(authz), 'authorization guard')} "
            f"{'before it' if write else 'in the sequence'}.",
            "strong" if write else "medium",
            "sink=write before any guard=authz" if write else "sink=read, no guard=authz", comps,
            [s["file"]] if s.get("file") else [],
            {"entry": entry, "sink": skid, "sink_kind": s["kind"], "sequences": seqs[:20],
             "guards": [g["id"] for g in authz][:20],
             "moves": ["Check the caller's permission before this call, with a function declared `guard=authz`.",
                       "If a check runs where the trace does not show it, declare that function `guard=authz`."]},
            focus=[focus("sequences", seqs[:20]), focus("components", comps)],
            weight=Weight(70 if write else 50, f"sink={s['kind']}").spread(len(seqs), "sequences", per=4, cap=10)))
    return out
