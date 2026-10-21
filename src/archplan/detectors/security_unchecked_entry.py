"""security.unchecked_entry: an entry point that must check what the caller
may do, whose code names no authorization guard.

An entry point declared `auth=authz` acts on something the request names,
such as a record by its id. This check reads its code, not a trace, so it
also covers entry points no test runs. It reads the function, the functions
of the same file it calls, and the lines of that file outside any function,
where plugs, decorators and filters go. With the model's signatures, it also
reads the functions it calls in other files of the same package: the
signatures of its component in its folder, matched by name and, for a call
on a value, by the receiver type (see follow). It reads at most
`max_functions` functions in all. When none of them names a function
declared `guard` or `guard=authz`, the entry point may hand out what the
caller may not see. The check runs elsewhere when a middleware, a plug in
another file or a callee in another package does it; a trace of a request
that passes the guard then clears the finding (see
security.unguarded_entry). A framework hook can run the guard for every
entry point that defines a callback, such as a LiveView on_mount hook that
asks the view what to check: list such names in `hooks`, and an entry point
whose file names one is left out. Text matching, so findings are medium."""

from __future__ import annotations

import re

from ..markers import declared_name
from .common import Weight, finding, plural
from .security_common import decls, guards, target
from .security_unguarded_entry import guarded, runs

ID = "security.unchecked_entry"
NAME = "Entry point that needs an authorization check, with none in its code"
GROUP = "security"
NEEDS = {"declarations"}
PARAMS = {"max_functions": 30, "hooks": []}


CONTAINER = re.compile(r"^\s*(?:(?:export|public|private|internal|abstract|final|sealed|open)\s+)*"
                       r"(?:defmodule|class|module|interface|struct|trait|object|impl|namespace)\b")
ONLY = re.compile(r"\b(?:action\s+in|only)\b")
EXCEPT = re.compile(r"\b(?:not\s+in|except)\b")


def indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def functions(lines: list) -> tuple:
    """({name: [(start, end)]}, set of line indexes outside any function).
    A function runs from its declaration to the next declaration of another
    name at its indentation or less, or to a line indented less."""
    starts = [(i, n, indent(x)) for i, x in enumerate(lines) if not CONTAINER.match(x) and (n := declared_name(x))]
    out, inside = {}, set()
    for k, (i, n, ind) in enumerate(starts):
        if k and starts[k - 1][1] == n and starts[k - 1][2] == ind:
            continue  # another clause of the function above
        end = len(lines)
        for j in range(i + 1, len(lines)):
            x = lines[j]
            if not x.strip():
                continue
            other = not CONTAINER.match(x) and declared_name(x)
            if indent(x) < ind or (other and other != n and indent(x) <= ind):
                end = j
                break
        out.setdefault(n, []).append((i, end))
        inside.update(range(i, end))
    return out, set(range(len(lines))) - inside


def word(n: str) -> re.Pattern:
    return re.compile(rf"(?<![\w?!]){re.escape(n)}(?![\w?!])")


def applies(line: str, entry: str) -> bool:
    """Whether a module-level line, such as a plug or a filter, applies to the
    entry point: `when action in [...]` and `only:` name the ones it does,
    `not in` and `except:` the ones it does not."""
    if EXCEPT.search(line):
        return not word(entry).search(line)
    if ONLY.search(line):
        return bool(word(entry).search(line))
    return True


def reached(lines: list, entry: str, line: int | None, cap: int) -> tuple:
    """(text the entry point runs, names of the functions read, the line
    ranges of those functions)."""
    fns, outside = functions(lines)
    if entry not in fns:
        return None, [], []
    first = min(fns[entry], key=lambda r: abs(r[0] + 1 - (line or r[0] + 1)))
    outside = [i for i in sorted(outside) if applies(lines[i], entry)]
    seen, text, read = {entry}, [lines[i] for i in outside], []
    todo = [first] + [r for r in fns[entry] if r != first]
    while todo:
        a, b = todo.pop(0)
        read.append((a, b))
        body = "\n".join(lines[a:b])
        text.append(body)
        for n, ranges in fns.items():
            if n not in seen and len(seen) < cap and word(n).search(body):
                seen.add(n)
                todo.extend(ranges)
    # Plugs and filters name their function at the module level.
    for n, ranges in fns.items():
        if n not in seen and len(seen) < cap and any(word(n).search(lines[i]) for i in outside):
            seen.add(n)
            read.extend(ranges)
            text.extend("\n".join(lines[a:b]) for a, b in ranges)
    return "\n".join(text), sorted(seen), read


# ---------- callees in other files of the package ----------
#
# The model's signatures say which functions a package has, in which file,
# and on which receiver. A call in the code read so far resolves to one of
# them only within the entry point's package: the signatures of its
# component in the same folder. A call on a value, such as `ops.run(...)`,
# resolves by the receiver: the type of the value when the code shows it
# (a receiver, a parameter, `x := T{...}`, `var x T`, or `x := newT(...)` of
# a function that returns T), else the methods of that name whose receiver
# type the code read so far names, else the one method of that name.

CALLS = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
QUALIFIER = re.compile(r"(\w+)?\s*(\))?\s*\.\s*$")
GO_IMPORT = re.compile(r'^\s*(?:import\s+)?(?:([\w.]+)\s+)?"([^"\s]+)"\s*(?://.*)?$', re.M)
GO_RECEIVER = re.compile(r"^\s*func\s*\(\s*(\w+)\s+\*?\s*(\w+)")
VAR_LITERAL = re.compile(r"\b(\w+)\s*:?=\s*&?\s*(\w+)\s*\{")
VAR_DECL = re.compile(r"\bvar\s+(\w+)\s+\*?\s*(\w+)\b")
VAR_CALL = re.compile(r"\b(\w+)\s*(?:,\s*\w+\s*)*:?=\s*(\w+)\s*\(")
TYPE_NAME = re.compile(r"^[\s*&\[\]]*(\w+)$")


def local_type(text: str | None) -> str | None:
    """The type name in a type of the package itself: `*T` gives T; `pkg.T`, `[]T`, `map[...]` give None."""
    m = TYPE_NAME.match(text or "")
    return m.group(1) if m and "[]" not in (text or "") else None


def receiver(ctx, sig: dict) -> str | None:
    """The type a signature's function is a method of, or None for a plain function."""
    sid = re.sub(r"/\d+$", "", sig["id"])
    name = sig.get("name") or short(sid)
    if not sid.endswith(name):
        return None
    head = sid[: -len(name)].rstrip(".")
    if not head:
        return None
    if (sig.get("file") or "").endswith(".go"):
        # internal/store.(*Store).Get, internal/store.Value.String, internal/store.New; the root package has no
        # package part: (*Store).Get.
        segs = head.rsplit("/", 1)[-1].split(".")
        folder = (sig.get("file") or "").rsplit("/", 2)
        pkg = "/" in head or (len(folder) > 1 and segs[0] == folder[-2])
        segs = segs[1:] if pkg else segs
        return segs[-1].strip("(*)") or None if segs else None
    return head.rsplit(".", 1)[-1] if head in (ctx.model.get("types") or {}) else None


def folder(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def package(ctx, t: dict) -> list:
    """[(signature, receiver)] of the entry point's package: its component, in its folder."""
    out = []
    for s in ctx.model.get("signatures") or []:
        if not (isinstance(s, dict) and isinstance(s.get("id"), str) and isinstance(s.get("file"), str)):
            continue
        if folder(s["file"]) != folder(t["file"]) or (t["component"] and s.get("component")
                                                     and s["component"] != t["component"]):
            continue
        out.append((s, receiver(ctx, s)))
    return out


def imports(src: str) -> set:
    """The names a Go file gives the packages it imports."""
    out = set()
    for alias, path in GO_IMPORT.findall(src):
        segs = path.split("/")
        last = segs[-1] if not (re.fullmatch(r"v\d+", segs[-1]) and len(segs) > 1) else segs[-2]
        out.add(alias or last.split(".")[-1].replace("-", "_"))
    return out


def var_types(body: str, sig: dict | None, recv: str | None, plain: dict) -> dict:
    """{variable: type name} the code shows; a type of another package is ""."""
    out = {}
    m = GO_RECEIVER.match(body)
    if m:
        out[m.group(1)] = m.group(2)
    elif recv:
        out.setdefault("self", recv)
    for p in (sig or {}).get("params") or []:
        if isinstance(p, dict) and isinstance(p.get("name"), str):
            out[p["name"]] = local_type(p.get("type")) or ""
    for rx in (VAR_LITERAL, VAR_DECL):
        for v, typ in rx.findall(body):
            out.setdefault(v, typ)
    for v, fn in VAR_CALL.findall(body):
        if fn in plain:
            out.setdefault(v, local_type(plain[fn]) or "")
    return out


def follow(ctx, t: dict, src: str, ranges: list, cap: int) -> tuple:
    """([texts], [names], [files]) of the functions in other files of the
    entry point's package that the code read so far calls, to cap functions."""
    pkg = package(ctx, t)
    if not pkg or cap <= 0:
        return [], [], []
    files = {t["file"]: src.split("\n")}

    def lines_of(f):
        if f not in files:
            text = ctx.read(f)
            files[f] = text.split("\n") if text is not None else None
        return files[f]

    plain = {s["name"]: s.get("returns") for s, r in pkg if r is None and s.get("name")}
    receivers = {r for _, r in pkg if r}
    own = next((x for x in pkg if x[0]["file"] == t["file"] and x[0].get("name") == t["name"]), None)
    is_go = t["file"].endswith(".go")
    skip = imports(src) if is_go else set()
    bodies = [("\n".join(files[t["file"]][a:b]), own) for a, b in ranges]
    done = {(t["file"], a) for a, _ in ranges}
    texts, names, used = [], [], []
    k = 0
    while k < len(bodies) and len(names) < cap:
        body, me = bodies[k]
        k += 1
        if is_go and me and me[0]["file"] != t["file"]:
            skip |= imports("\n".join(lines_of(me[0]["file"]) or []))
        types = var_types(body, me[0] if me else None, me[1] if me else None, plain)
        named = {w for w in receivers if word(w).search("\n".join(b for b, _ in bodies))}
        for m in CALLS.finditer(body):
            name = m.group(1)
            q = QUALIFIER.search(body, max(0, m.start() - 80), m.start())
            if q is None:
                hits = [x for x in pkg if x[1] is None and x[0].get("name") == name]
            else:
                var = q.group(1) if not q.group(2) else None
                if var in skip and var not in types:
                    continue  # a function of an imported package
                hits = [x for x in pkg if x[1] and x[0].get("name") == name]
                if var and var in types:
                    hits = [x for x in hits if x[1] == types[var]]
                elif var and var in receivers:
                    hits = [x for x in hits if x[1] == var]  # T.method, Module.fun
                elif len(hits) > 1:
                    hits = [x for x in hits if x[1] in named]
            for s, r in hits:
                if len(names) >= cap:
                    break
                lines = lines_of(s["file"])
                fns = functions(lines)[0] if lines else {}
                if not fns.get(s.get("name")):
                    continue
                a, b = min(fns[s["name"]], key=lambda x: abs(x[0] + 1 - (s.get("line") or x[0] + 1)))
                if (s["file"], a) in done:
                    continue
                done.add((s["file"], a))
                text = "\n".join(lines[a:b])
                bodies.append((text, (s, r)))
                texts.append(text)
                names.append(f"{r}.{s['name']}" if r else s["name"])
                if s["file"] not in used and s["file"] != t["file"]:
                    used.append(s["file"])
    return texts, names, used


def run(ctx, params) -> list:
    authz = guards(ctx, "authz")
    if not authz:
        return []
    safe = {d["target"].get("id") for _, d, _, msgs in runs(ctx) if d and guarded(ctx, d, msgs)}
    out = []
    for d in sorted(decls(ctx, "entrypoint", "auth"), key=lambda d: d["target"].get("id") or ""):
        if d.get("value") != "authz" or d["target"].get("id") in safe:
            continue
        t = target(ctx, d)
        src = ctx.read(t["file"]) if t["file"] else None
        if src is None or any(word(h).search(src) for h in params["hooks"] or []):
            continue
        text, read, ranges = reached(src.split("\n"), t["name"], t["line"], params["max_functions"])
        if text is None or any(word(g["name"]).search(text) for g in authz):
            continue
        more, also, files = follow(ctx, t, src, ranges, params["max_functions"] - len(read))
        if any(word(g["name"]).search(x) for x in more for g in authz):
            continue
        read += also
        where = f"{t['file']}:{t['line']}"
        names = ", ".join(g["name"] for g in authz[:5])
        out.append(finding(
            ID, (t["id"],),
            f"{t['name']} needs an authorization check, but its code calls no authorization guard.",
            f"It is declared auth=authz{' (' + d['reason'] + ')' if d.get('reason') else ''}. "
            f"{plural(len(read), 'function')} ({', '.join(read[:6])}{', …' if len(read) > 6 else ''}) of "
            f"{t['file']}{' and ' + ', '.join(files[:3]) if files else ''}{', …' if len(files) > 3 else ''}"
            f" and the module-level lines of {t['file']} name none of {names}.",
            "medium", "auth=authz, no authorization guard in its code", [t["component"]], [t["file"], *files],
            {"entry": t["id"], "declared_at": where, "read": read[:30], "guards": [g["id"] for g in authz][:20],
             **({"files": files[:20]} if files else {}),
             "moves": ["Check what the caller may do before the entry point reads or changes data, "
                       f"with {authz[0]['name']} or another authorization guard.",
                       "If a check elsewhere covers it, add a test that makes this request: a trace that "
                       "passes the guard clears this finding.",
                       "If the caller may act on anything the request names, declare `auth=required`."]},
            weight=Weight(55, "auth=authz")))
    return out
