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
per entry point and function, with the sequences that show it.

A trace recorded with a canary adds values after all (see "Canary" in
format/archmodel.md). A test sends the canary marker as input; the tracer
marks the calls whose arguments hold it (`taint`) and the library calls that
run or resolve input (`interprets`, with `canary: "raw"` when the whole
marker reached them unchanged, "escaped" when only its letters did). Then:

- the marker reached a library call of the function's kind unchanged: the
  finding is strong;
- the function received the marker, but the library calls of its kind under
  it never got it unchanged, such as a query with bound parameters: there is
  no finding;
- the function received the marker, with no library call recorded: the
  finding gains weight;
- the sequence carried the marker, but the function never received it: the
  finding loses weight.

The marker reaching a library call unchanged is a finding of its own, also
without declarations: one per entry point and library call."""

from __future__ import annotations

import re
from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import call_file, called, decls, participant, short, target
from .security_unguarded_entry import hit, steps

ID = "security.unchecked_input"
NAME = "Input interpreted before any check"
GROUP = "security"
NEEDS = {"sequences"}  # declarations, or a canary in the traces
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
    """[(origin, target, kind, chain, under)] for one sequence's messages: each
    interpreting function reached with input not checked for its kind, and
    each trusting function reached before any check. origin is the source the
    input came from, or else the declared entry point the step calls, or else
    the sequence's entry; chain holds the calls the function runs
    under, which pass the input on to it: the code to review first; under
    holds the call of the function and the calls it makes. Without depths,
    chain holds every call of its step that came first."""
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
                    hits.append((origin, t, t["kind"], chain, below(step, i, deep)))
                    inside = depth
            if not checked:
                hits += [(origin, t, "trusts", chain, below(step, i, deep)) for t in ins["trusts"] if hit(ctx, t, m)]
    return hits


def below(step: list, i: int, deep: bool) -> list:
    """The message at i and the calls made inside it."""
    if not deep:
        return step[i:i + 1]
    d, out = step[i].get("depth") or 0, [step[i]]
    for m in step[i + 1:]:
        if (m.get("depth") or 0) <= d:
            break
        out.append(m)
    return out


# ---------- canary: what the marker shows (see format/archmodel.md) ----------

RAW, TAINTED, CLEARED, UNMARKED, UNKNOWN = 4, 3, 2, 1, 0
PROOF = {RAW: "the canary reached a library call unchanged", TAINTED: "the canary reached the function",
         CLEARED: "the canary reached the function, but no library call got it unchanged",
         UNMARKED: "the sequence carried the canary, but the function never got it"}


def uses(m: dict) -> list:
    return [i for i in m.get("interprets") or [] if isinstance(i, dict) and i.get("kind") and i.get("call")]


def marked(msgs: list) -> bool:
    """Whether a sequence carried the canary marker anywhere."""
    return any(m.get("taint") or any(i.get("canary") for i in uses(m)) for m in msgs)


def proof(under: list, kind: str, carried: bool) -> tuple:
    """(rank, raw calls) for one call of a function declared interprets=kind
    or trusts, with the calls inside it."""
    raw = [i["call"] for m in under for i in uses(m) if i.get("canary") == "raw"
           and (kind == "trusts" or i["kind"] == kind)]
    if raw:
        return RAW, raw
    if under and under[0].get("taint"):
        same = [i for m in under for i in uses(m) if kind == "trusts" or i["kind"] == kind]
        return (CLEARED if same else TAINTED), []
    return (UNMARKED if carried else UNKNOWN), []


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
    found = defaultdict(list)  # (origin, target id, kind) -> [(sequence id, chain, rank, raw calls)]
    targets, covered, loose = {}, set(), defaultdict(list)
    for sid, seq in sorted((ctx.model.get("sequences") or {}).items()):
        msgs = [m for m in (seq or {}).get("messages") or [] if isinstance(m, dict)]
        if not msgs:
            continue
        carried = marked(msgs)
        hits = walk(ctx, ins, (seq or {}).get("entry"), msgs) if declared(ins) else []
        for origin, t, kind, chain, under in hits:
            rank, raw = proof(under, kind, carried)
            covered.update(id(m) for m in under)
            key = (origin, t["id"], kind)
            if not found[key] or found[key][-1][0] != sid:
                found[key].append((sid, chain, rank, raw))
            else:
                found[key][-1] = (sid, chain, max(rank, found[key][-1][2]), found[key][-1][3] + raw)
            targets[t["id"]] = t
        if carried:
            for origin, m, call, kind, chain in raw_calls(ctx, ins, seq, msgs):
                if id(m) not in covered:
                    loose[(origin, call, kind)].append((sid, m, chain))
    out = []
    checks = sorted({t["id"] for t in ins["validates"]})
    for (origin, tid, kind), runs in sorted(found.items()):
        rank = max(r for _, _, r, _ in runs)
        if rank == CLEARED:
            continue
        raw = list(dict.fromkeys(c for _, _, _, cs in runs for c in cs))
        t, seqs = targets[tid], [sid for sid, *_ in runs]
        trusts = kind == "trusts"
        first = participant(ctx, ((ctx.model["sequences"][seqs[0]] or {}).get("messages") or [{}])[0].get("to"))
        comps = [c for c in dict.fromkeys((first, t["component"])) if c]
        path = review(ctx, [m for _, chain, *_ in runs for m in chain])
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
             "review": path[:20], **({"canary": PROOF[rank]} if rank else {}),
             **({"reached": raw[:10]} if raw else {}),
             "moves": (["Check the input before this call, in a function declared `validates`.",
                        "If the function checks its input itself, remove its `trusts` declaration."] if trusts else
                       [f"Check or parse the input before this call, in a function declared `validates` or "
                        f"`validates={kind}`.",
                        "If a check runs where the trace does not show it, declare that function `validates`.",
                        "If the function never treats input as code, such as a query with bound parameters, "
                        "remove its `interprets` declaration."])},
            focus=[focus("sequences", seqs[:20]), focus("components", comps)],
            weight=canary(Weight(BASE.get(kind, 50), "trusts" if trusts else f"interprets={kind}")
                          .spread(len(seqs), "sequences", per=4, cap=10), rank)))
    for (origin, call, kind), runs in sorted(loose.items()):
        out.append(reached(ctx, origin, call, kind, runs))
    return out


def canary(w: Weight, rank: int) -> Weight:
    effect = {RAW: 30, TAINTED: 15, UNMARKED: -15}.get(rank, 0)
    return w.add("canary", PROOF[rank], effect) if effect else w


def raw_calls(ctx, ins: dict, seq: dict, msgs: list) -> list:
    """[(origin, message, call, kind, chain)]: each library call the canary
    marker reached unchanged. origin is the declared entry point of the step,
    else the sequence's entry, else its first call."""
    out = []
    for step in steps(msgs):
        origin = next((t["id"] for m in step for t in ins["entries"] if hit(ctx, t, m)), None) \
            or seq.get("entry") or called(step[0])
        open_ = {}
        for m in step:
            depth = m.get("depth") or 0
            open_ = {d: c for d, c in open_.items() if d < depth}
            chain = list(open_.values())
            open_[depth] = m
            for i in uses(m):
                if i.get("canary") == "raw":
                    out.append((origin, m, i["call"], i["kind"], chain + [m]))
    return out


def reached(ctx, origin: str, call: str, kind: str, runs: list) -> dict:
    seqs = list(dict.fromkeys(sid for sid, _, _ in runs))
    m = runs[0][1]
    path = review(ctx, [x for _, _, chain in runs for x in chain])
    comps = [c for c in dict.fromkeys(participant(ctx, x.get("to")) for _, x, _ in runs) if c]
    return finding(
        ID, (origin, call, kind, "canary"),
        f"{owned(origin)} passes test input unchanged to {call}, which reads it as {kind}.",
        f"{plural(len(seqs), 'sequence')} ({', '.join(seqs[:3])}{', …' if len(seqs) > 3 else ''}) sent the "
        f"canary marker as input, and it reached {call} unchanged, under {called(m)}. Escaping or bound "
        f"parameters would have changed or removed it. The input reaches it through "
        f"{plural(len(path), 'function')}: " + (", ".join(r["function"] for r in path[:5])
                                                + (", …" if len(path) > 5 else "") or "none") + ".",
        "strong", f"canary reached {kind} call", comps, [r.get("file") for r in path if r.get("file")],
        {"entry": origin, "call": call, "kind": kind, "sequences": seqs[:20], "review": path[:20],
         "canary": PROOF[RAW],
         "moves": [f"Pass the input as data, such as a bound parameter, or escape it for {kind} before this call.",
                   "If the input must hold this kind of code, check it against an allow-list first."]},
        focus=[focus("sequences", seqs[:20]), focus("components", comps)],
        weight=canary(Weight(BASE.get(kind, 50), f"interprets={kind}"), RAW).spread(len(seqs), "sequences",
                                                                                     per=4, cap=10))
