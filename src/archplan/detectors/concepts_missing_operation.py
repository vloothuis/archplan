"""concepts.missing_operation: functions in several components that each do
the same thing to another component's type, such as order_total(order) in
billing and calc_total(o: Order) in shipping. The owner of the type is
missing the operation, so every caller writes its own. From the model's
signatures section.

Two functions match when
  - their parameters of model types all refer to the same types, owned by
    one component, not their own. An untyped parameter refers to the type it
    is named after (order, sales_order), which weighs a little less;
  - their names have the same words once the type's own words and generic
    verbs (get, calc, compute) are left out: order_total, calc_total and
    total_for_order are all "total";
  - they return the same type, or both declare no return type.

Conversions (to_json, from_row, render, serialize) are left out: mapping a
type to a format is the job of each boundary, not duplication. So are
tests, and methods that an interface of the model declares: Save on every
store that implements one is polymorphism.

The move is to give the owner the operation. It needs no new dependency:
the parameters are the owner's types, and a return type of the model must
belong to the owner or to a component it already depends on."""

from __future__ import annotations

import re
from collections import defaultdict

from ..core.vocab import is_test
from .common import EXAMPLES, Weight, focus, plural
from .drift import drift, move, sentence, weigh
from .fields import words

ID = "concepts.missing_operation"
NAME = "Missing operations"
GROUP = "concepts"
NEEDS = {"signatures", "types"}
PARAMS = {"min_components": 2, "max_findings": 20}
GENERIC = {"get", "calc", "calculate", "compute", "make", "build", "do", "for", "of", "from", "with", "by", "on",
           "in", "the", "a", "an", "and", "find", "fetch", "load", "read", "handle", "process", "run", "helper",
           "util", "impl", "internal", "my", "new"}
CONVERSIONS = {"to", "from", "as", "into", "encode", "decode", "marshal", "unmarshal", "serialize", "deserialize",
               "render", "map", "convert", "parse", "dump", "dumps", "load", "loads", "new"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
# Parameter names too generic to name one type, even when one type happens to have that name.
GENERIC_PARAMS = {"entity", "element", "item", "context", "ctx", "data", "record", "model", "object", "obj", "node",
                  "state", "socket", "conn", "params", "value", "result", "event", "message", "msg", "request",
                  "response", "session", "config", "options", "opts", "args", "assigns", "step", "view", "form",
                  "changeset", "query", "schema", "field", "type", "kind", "name", "key", "id", "path", "file",
                  "attrs", "entry", "info", "meta", "payload", "body", "input", "output", "source", "target",
                  "list", "map", "dict", "set", "string", "str", "array", "tuple", "keyword", "enum", "int",
                  "integer", "number", "date", "time", "datetime", "struct", "module", "fun", "function", "callback",
                  "error", "text", "content", "child", "parent", "items", "values", "entries", "acc", "x", "y"}


def fn_name(s: dict) -> str:
    """The function's own name: the signature's name, or the id's last part without an arity."""
    if s.get("name"):
        return str(s["name"])
    return re.split(r"[.#:]", str(s.get("id") or "").split("/")[0])[-1]


def resolver(types: dict):
    """(resolve, by_param): resolve(type text) is the id of the model type the
    text names, or None; by_param(name) the type an untyped parameter is named
    after (order, sales_order). A name that several types share does not resolve."""
    exact, lower = defaultdict(set), defaultdict(set)
    for tid, t in types.items():
        name = t.get("name") or tid.rsplit(".", 1)[-1]
        exact[name].add(tid)
        lower[name.lower()].add(tid)
    one = lambda ids: next(iter(ids)) if ids and len(ids) == 1 else None

    def resolve(text):
        """The type a type text names, in its exact case (string is not String).
        A qualified name (context.Context) needs the qualifier in the type's id."""
        if not isinstance(text, str):
            return None
        for ident in IDENT.findall(text[:200]):
            parts = [x for x in ident.split(".") if x and x != "t"]
            if not parts:
                continue
            ids = exact.get(parts[-1], set())
            if len(parts) > 1:
                ids = {t for t in ids if parts[-2] in re.split(r"[./\\]", t)}
            if one(ids):
                return one(ids)
        return None

    def by_param(name):
        ws = words(name) if isinstance(name, str) else []
        return one(lower.get("".join(ws))) if ws and "_".join(ws) not in GENERIC_PARAMS else None
    return resolve, by_param


def type_name(ctx, tid: str) -> str:
    return ctx.types[tid].get("name") or tid.rsplit(".", 1)[-1]


def run(ctx, params) -> list:
    resolve, by_param = resolver(ctx.types)
    deps = defaultdict(set)
    for a, b in ctx.graph:
        deps[a].add(b)
    named = set()  # ids of signatures whose subject comes from parameter names
    # Implementations of an interface method (Save on every store) are polymorphism, not copies.
    declared = {str(m.get("name")).lower() for t in ctx.types.values() if t.get("kind") in ("interface", "protocol")
                for m in t.get("methods") or [] if isinstance(m, dict) and m.get("name")}

    def shape(s):
        """(subject type ids, the owner of them all or None, the operation's words)."""
        ps = [p for p in s.get("params") or [] if isinstance(p, dict)]
        typed = {resolve(p["type"]) for p in ps if p.get("type")} - {None}
        untyped = {by_param(p.get("name")) for p in ps if not p.get("type")} - {None}
        if untyped - typed:
            named.add(id(s))
        subject = tuple(sorted(typed | untyped))
        owners = {ctx.types[t].get("component") for t in subject}
        own = {w for t in subject for w in words(type_name(ctx, t))}
        verb = frozenset(w for w in words(fn_name(s)) if w not in own and w not in GENERIC and not w.isdigit())
        return subject, next(iter(owners)) if len(owners) == 1 else None, verb

    groups = defaultdict(list)
    existing = defaultdict(list)  # the owners' own functions on their types
    for s in ctx.signatures:
        c = s.get("component")
        if c not in ctx.internal or is_test(str(s.get("file") or "")):
            continue
        subject, owner, verb = shape(s)
        if len("".join(verb)) < 3 or owner not in ctx.internal or fn_name(s).lower() in declared:
            continue
        if owner == c:
            existing[(owner, subject, verb)].append(s)
            continue
        ret = resolve(s.get("returns"))
        if words(fn_name(s))[0] in CONVERSIONS or ret and ctx.types[ret].get("component") not in {owner} | deps[owner]:
            continue
        returns = ret or re.sub(r"\s+", "", str(s.get("returns") or "")).lower()
        groups[(owner, subject, verb, returns)].append(s)
    rows = []
    for (owner, subject, verb, returns), sigs in groups.items():
        comps = sorted({s["component"] for s in sigs})
        if len(comps) >= params["min_components"]:
            rows.append((owner, subject, verb, returns, sigs, comps))
    rows.sort(key=lambda r: (-len(r[5]), -len(r[4]), r[0], sorted(r[2])))
    out = []
    for owner, subject, verb, returns, sigs, comps in rows[:params["max_findings"]]:
        sigs.sort(key=lambda s: (s["component"], str(s.get("id"))))
        tnames = [type_name(ctx, t) for t in subject]
        op = "_".join(sorted(verb))
        have = existing.get((owner, subject, verb)) or []
        where = ", ".join(ctx.name(c) for c in comps[:3]) + (", …" if len(comps) > 3 else "")
        title = (f"{plural(len(comps), 'component')} ({where}) each write their own {op} for "
                 f"{' and '.join(tnames)}, a type of {ctx.name(owner)}.")
        moves = ([f"Call {have[0].get('id')} in {ctx.name(owner)}, which already does this, and delete the copies."]
                 if have else
                 [f"Give {ctx.name(owner)} one operation {op}({', '.join(tnames)}) and replace these functions "
                  "with calls to it."]) + \
            ["Check the bodies first: if the copies compute different things, give each its own name instead."]
        d = drift(ctx.hotspots, [s.get("file") for s in sigs])
        moves = move(d) + moves
        out.append(ctx.finding(
            (owner, *subject, op, returns), title,
            " ".join(x for x in ("Such as " + ", ".join(f"{s.get('id')} ({ctx.name(s['component'])})"
                                                         for s in sigs[:4]) + ".", sentence(d)) if x),
            "medium", f"{plural(len(sigs), 'function')} in {plural(len(comps), 'component')}, on "
            f"{' and '.join(tnames)}" + (f", returning {returns}" if returns else ""),
            [owner] + comps, [s.get("file") for s in sigs],
            {"owner": owner, "types": list(subject), "operation": op, "returns": returns or None,
             "functions": [{"id": s.get("id"), "component": s["component"], "file": s.get("file"),
                            "line": s.get("line")} for s in sigs[:EXAMPLES]],
             **({"existing": [s.get("id") for s in have[:3]]} if have else {}), **({"history": d} if d else {}),
             "moves": moves},
            focus=[focus("components", [owner] + comps), focus("types", list(subject))],
            weight=weigh(Weight(30, "missing operation").spread(len(comps), per=8, cap=16)
                         .repetition(len(sigs), "functions", per=3, cap=6, single=0)
                         .add("the owner already has it", have[0].get("id") if have else None, 10 if have else 0)
                         .add("no return types", "unknown", 0 if returns else -6)
                         .add("types from parameter names", sum(id(s) in named for s in sigs),
                              -6 if any(id(s) in named for s in sigs) else 0), d)))
    return out
