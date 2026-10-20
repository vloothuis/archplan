"""security.unguarded_entry: a traced entry point whose request passes no guard.

A sequence lists the calls of a test or a recorded run. Its steps at depth 0,
each with everything below it, make up requests: a request ends with the
step that calls a declared entry point, and holds the steps before it since
the previous request, such as a LiveView's on_mount hook. A test can make
several requests, so each is judged on its own. An entry point declared `auth=required` needs a guard on its
request; `auth=authz` needs an authorization guard (`guard` or
`guard=authz`). When no message calls a declared entry point, the sequence's
own entry is judged on the whole sequence. Entry points without an auth
declaration are reported too, as weak, once the project declares any guard:
their trace shows no guard either. `auth=none` entry points are skipped, and
so is an entry point with any request that passes the guard it needs.
Sequences marked `"guards": "off"` ran without guards and are left out. One
finding per entry point, with the sequences that show it."""

from __future__ import annotations

from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import CALL, call_file, called, decls, guards_off, matches, name, participant, short, target
from .security_common import guards as all_guards

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


def steps(msgs: list) -> list:
    """The messages split into steps: each starts at a message of depth 0.
    Without depths, the whole sequence is one step."""
    if any("depth" not in m for m in msgs):
        return [msgs]
    out = []
    for m in msgs:
        if (m.get("depth") or 0) == 0 or not out:
            out.append([])
        out[-1].append(m)
    return out


def hit(ctx, t: dict, m: dict) -> bool:
    """Whether message m calls the declared function t."""
    return matches(t, called(m), participant(ctx, m.get("to")), call_file(ctx, m))


def runs(ctx) -> list:
    """[(entry, declaration or None, sequence id, request messages)]: each
    traced run of an entry point, with the messages of its request."""
    auth = decls(ctx, "entrypoint", "auth")
    ts = {id(d): target(ctx, d) for d in auth}
    out = []
    for sid, seq in sorted((ctx.model.get("sequences") or {}).items()):
        entry, msgs = (seq or {}).get("entry"), [m for m in (seq or {}).get("messages") or [] if isinstance(m, dict)]
        if not msgs:
            continue
        hits, req = [], []
        for step in steps(msgs):
            req = req + step
            found = [(m, d) for m in step for d in auth if hit(ctx, ts[id(d)], m)]
            for m, d in found:
                name = entry if m is msgs[0] and entry else CALL.match((m.get("label") or "").strip()).group(1)
                hits.append((name.strip() or d["target"]["id"], d, sid, req))
            if found:
                req = []
        if hits:
            out.extend(hits)
        elif entry:
            out.append((entry, entry_auth(ctx, entry, participant(ctx, msgs[0].get("to")), auth), sid, msgs))
    return out


def guarded(ctx, d: dict | None, msgs: list) -> bool:
    """Whether the messages pass the guard that the entry point's auth needs."""
    need = all_guards(ctx, "authz") if d and d.get("value") == "authz" else all_guards(ctx)
    return any(hit(ctx, g, m) for m in msgs for g in need)


def run(ctx, params) -> list:
    guards = all_guards(ctx)
    by_entry = defaultdict(list)  # entry -> [(sequence id, components on its path)]
    declared, safe = {}, set()
    seqs = ctx.model.get("sequences") or {}
    for entry, d, sid, msgs in runs(ctx):
        if guards_off(seqs.get(sid)):
            continue
        value = d.get("value") if d else None
        if value == "none" or (value is None and not (params["report_undeclared"] and guards)):
            continue
        declared[entry] = d
        if guarded(ctx, d, msgs):
            safe.add(entry)
            continue
        comps = [c for c in dict.fromkeys(participant(ctx, m.get("to")) for m in msgs) if c]
        if sid not in [s for s, _ in by_entry[entry]]:
            by_entry[entry].append((sid, comps))
    out = []
    for entry, seqs in sorted(by_entry.items()):
        if entry in safe:
            continue  # another run of it passes the guard: this one may take an early exit
        d = declared[entry]
        value = d.get("value") if d else None
        authz = value == "authz"
        need = all_guards(ctx, "authz") if authz else guards
        comps = list(dict.fromkeys(c for _, cs in seqs for c in cs))
        ids = [s for s, _ in seqs]
        where = f"{d['file']}:{d['line']}" if d and d.get("file") else None
        through = ", ".join(name(ctx, c) for c in comps[:4]) or "no known component"
        kind = "authorization guard" if authz else "declared guard"
        out.append(finding(
            ID, (entry,),
            f"{short(entry)} needs an authorization check, but its traced requests pass none." if authz else
            f"{short(entry)} needs auth, but its traced calls pass no guard." if value else
            f"{short(entry)} has no auth declaration, and its traced calls pass no guard.",
            f"{plural(len(ids), 'sequence')} ({', '.join(ids[:3])}{', …' if len(ids) > 3 else ''}) "
            f"{'go' if len(ids) > 1 else 'goes'} through {through} and call none of the "
            f"{plural(len(need), kind)}.",
            "strong" if value else "weak",
            f"auth={value}, no {'authorization ' if authz else ''}guard in the trace" if value else
            "no auth declaration, no guard in the trace",
            comps[:1], [where.split(":")[0]] if where else [],
            {"entry": entry, "auth": value, "declared_at": where, "sequences": ids[:20],
             "guards": [g["id"] for g in need][:20],
             "moves": (["Check what the caller may do on this path, such as with an authorization guard.",
                        "If a plug or middleware checks it, mark that function `archplan: guard=authz`."]
                       if authz else
                       ["Call a guard on this path, or mark the function that checks auth with `archplan: guard`.",
                        "If the check runs in middleware the trace does not show, mark that middleware as a guard."]
                       if value else
                       ["Call a guard on this path, or mark the function that checks auth with `archplan: guard`.",
                        "Declare the entry point `auth=required` or `auth=none` with a reason."])},
            focus=[focus("sequences", ids[:20]), focus("components", comps[:20])],
            weight=Weight(70 if value else 30, f"auth={value}" if value else "no auth declaration")
            .spread(len(ids), "sequences", per=4, cap=10)))
    return out
