"""security.unchecked_input: a trace that passes outside input to a function
that interprets it, before any function checks it.

Input comes from outside where a sequence's entry point is called, at the
start of each step (a message at depth 0), and where it calls a function
declared `source`, such as a webhook body or a queue message. It stays
unchecked until a function declared `validates` runs: `validates` checks every
kind, `validates=sql` only input read as SQL. A function declared
`interprets=<kind>` (sql, shell, path, html, url, code or deserialize) runs or
resolves its input as that kind; reaching it with input not checked for that
kind is a finding. So is reaching a function declared `trusts`, which assumes
its caller checked the input, before any check. Calls inside a validator, an
interpreting function or a source are their own.

A trace shows calls, not values: the check may have run on another value than
the one the function receives. The finding says where to look, not that the
input is unsafe. Its evidence lists the calls the function runs under, which
pass the unchecked input on to it, for a reviewer to read first. One finding
per entry point and function, with the sequences that show it."""

from __future__ import annotations

import re
from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import call_file, called, decls, participant, short, target
from .security_unguarded_entry import hit, steps

ID = "security.unchecked_input"
NAME = "Input interpreted before any check"
GROUP = "security"
NEEDS = {"declarations", "sequences"}
PARAMS = {"report_trusts": True}

ALL = "*"
BASE = {"shell": 70, "code": 70, "sql": 60, "deserialize": 60, "path": 55, "html": 50, "url": 45, "trusts": 45}


def inputs(ctx) -> dict:
    """The input declarations as targets: {"validates", "interprets", "source", "trusts", "entries"};
    validators and interpreting functions carry their kind. entries are the
    entry points declared with an auth, which name where a request's input comes in."""
    def of(key, kind=False, what="function"):
        return [dict(target(ctx, d), kind=d.get("value") if kind else None) for d in decls(ctx, what, key)]
    return {"validates": of("validates", True), "interprets": of("interprets", True), "source": of("source"),
            "trusts": of("trusts"), "entries": of("auth", what="entrypoint")}


def declared(ins: dict) -> bool:
    return bool(ins["interprets"] or ins["trusts"])


def walk(ctx, ins: dict, entry: str | None, msgs: list) -> list:
    """[(origin, target, kind, chain)] for one sequence's messages: each
    interpreting function reached with input not checked for its kind, and
    each trusting function reached before any check. origin is the source the
    input came from, or else the declared entry point the step calls, or else
    the sequence's entry; chain holds the calls the function runs
    under, which pass the input on to it: the code to review first. Without
    depths, chain holds every call of its step that came first."""
    hits, deep = [], all("depth" in m for m in msgs)
    names = {t["name"] for ts in ins.values() for t in ts}  # a call of another name matches none
    for step in steps(msgs):
        origin, checked, inside, sourced, open_ = entry, set(), None, False, {}  # open_: depth -> the call there
        for i, m in enumerate(step):
            depth = m.get("depth") or 0
            if inside is not None and depth > inside:
                continue  # the validator's, interpreter's or source's own calls
            inside = None
            open_ = {d: c for d, c in open_.items() if d < depth}
            chain = list(open_.values()) if deep else step[:i]
            open_[depth] = m
            if called(m) not in names:
                continue
            v = next((t for t in ins["validates"] if hit(ctx, t, m)), None)
            if v:
                checked.add(v["kind"] if v["kind"] is not True else ALL)
                inside = depth
                continue
            s = next((t for t in ins["source"] if hit(ctx, t, m)), None)
            if s:
                origin, checked, inside, sourced = s["id"], set(), depth, True
                continue
            if origin is None:
                continue
            if not sourced:  # name the request by its declared entry point, such as a controller action
                origin = next((t["id"] for t in ins["entries"] if hit(ctx, t, m)), origin)
            for t in ins["interprets"]:
                if ALL not in checked and t["kind"] not in checked and hit(ctx, t, m):
                    hits.append((origin, t, t["kind"], chain))
                    inside = depth
            if not checked:
                hits += [(origin, t, "trusts", chain) for t in ins["trusts"] if hit(ctx, t, m)]
    return hits


def owned(fid: str) -> str:
    """A function id's name with its owner, so that several controllers'
    actions of one name differ: `M.Controller.show/2` gives Controller.show,
    `pkg.(*T).Get` gives (*T).Get; a `<file>:<name>` id gives the name."""
    if ":" in fid:
        return short(fid)
    return ".".join(re.sub(r"/\d+$", "", fid).rsplit("/", 1)[-1].split(".")[-2:])


def review(ctx, calls: list) -> list:
    """The functions of calls, once each: `component: name`, and the file when known."""
    out = {}
    for m in calls:
        comp = participant(ctx, m.get("to"))
        out.setdefault(f"{comp or m.get('to')}: {called(m)}", call_file(ctx, m))
    return [{"function": k, **({"file": f} if f else {})} for k, f in out.items()]


def run(ctx, params) -> list:
    ins = inputs(ctx)
    if not params["report_trusts"]:
        ins["trusts"] = []
    if not declared(ins):
        return []
    found = defaultdict(list)  # (origin, target id, kind) -> [(sequence id, chain)]
    targets = {}
    for sid, seq in sorted((ctx.model.get("sequences") or {}).items()):
        msgs = [m for m in (seq or {}).get("messages") or [] if isinstance(m, dict)]
        if not msgs:
            continue
        hits = walk(ctx, ins, (seq or {}).get("entry"), msgs)
        for origin, t, kind, chain in hits:
            key = (origin, t["id"], kind)
            if not found[key] or found[key][-1][0] != sid:
                found[key].append((sid, chain))
            targets[t["id"]] = t
    out = []
    checks = sorted({t["id"] for t in ins["validates"]})
    for (origin, tid, kind), runs in sorted(found.items()):
        t, seqs = targets[tid], [sid for sid, _ in runs]
        trusts = kind == "trusts"
        first = participant(ctx, ((ctx.model["sequences"][seqs[0]] or {}).get("messages") or [{}])[0].get("to"))
        comps = [c for c in dict.fromkeys((first, t["component"])) if c]
        path = review(ctx, [m for _, chain in runs for m in chain])
        out.append(finding(
            ID, (origin, tid, kind),
            f"{owned(origin)} reaches {t['name']}, which trusts its input, before any check." if trusts else
            f"{owned(origin)} passes input to {t['name']}, which reads it as {kind}, before any check.",
            f"{plural(len(seqs), 'sequence')} ({', '.join(seqs[:3])}{', …' if len(seqs) > 3 else ''}) call "
            f"{t['name']} with no {'validator' if trusts else f'validator for {kind}'} before it. The input "
            f"reaches it through {plural(len(path), 'function')}: " + (", ".join(r["function"] for r in path[:5])
                                                                     + (", …" if len(path) > 5 else "") or "none")
            + ".",
            "medium", "trusts before any validates" if trusts else f"interprets={kind} before validates",
            comps, [x for x in [t.get("file")] + [r.get("file") for r in path] if x],
            {"entry": origin, "function": tid, "kind": kind, "sequences": seqs[:20], "validators": checks[:20],
             "review": path[:20],
             "moves": (["Check the input before this call, in a function declared `validates`.",
                        "If the function checks its input itself, remove its `trusts` declaration."] if trusts else
                       [f"Check or parse the input before this call, in a function declared `validates` or "
                        f"`validates={kind}`.",
                        "If a check runs where the trace does not show it, declare that function `validates`.",
                        "If the function never treats input as code, such as a query with bound parameters, "
                        "remove its `interprets` declaration."])},
            focus=[focus("sequences", seqs[:20]), focus("components", comps)],
            weight=Weight(BASE.get(kind, 50), "trusts" if trusts else f"interprets={kind}")
            .spread(len(seqs), "sequences", per=4, cap=10)))
    return out
