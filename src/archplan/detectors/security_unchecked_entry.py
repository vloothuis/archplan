"""security.unchecked_entry: an entry point that must check what the caller
may do, whose code names no authorization guard.

An entry point declared `auth=authz` acts on something the request names,
such as a record by its id. This check reads its code, not a trace, so it
also covers entry points no test runs. It reads the function, the functions
of the same file it calls (to `max_functions`), and the lines of that file
outside any function, where plugs, decorators and filters go. When none of
them names a function declared `guard` or `guard=authz`, the entry point may
hand out what the caller may not see. The check runs elsewhere when a
middleware, a plug in another file or a callee in another file does it; a
trace of a request that passes the guard then clears the finding (see
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
    """(text the entry point runs, names of the functions read)."""
    fns, outside = functions(lines)
    if entry not in fns:
        return None, []
    first = min(fns[entry], key=lambda r: abs(r[0] + 1 - (line or r[0] + 1)))
    outside = [i for i in sorted(outside) if applies(lines[i], entry)]
    seen, text = {entry}, [lines[i] for i in outside]
    todo = [first] + [r for r in fns[entry] if r != first]
    while todo:
        a, b = todo.pop(0)
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
            text.extend("\n".join(lines[a:b]) for a, b in ranges)
    return "\n".join(text), sorted(seen)


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
        text, read = reached(src.split("\n"), t["name"], t["line"], params["max_functions"])
        if text is None or any(word(g["name"]).search(text) for g in authz):
            continue
        where = f"{t['file']}:{t['line']}"
        names = ", ".join(g["name"] for g in authz[:5])
        out.append(finding(
            ID, (t["id"],),
            f"{t['name']} needs an authorization check, but its code calls no authorization guard.",
            f"It is declared auth=authz{' (' + d['reason'] + ')' if d.get('reason') else ''}. "
            f"{plural(len(read), 'function')} of {t['file']} ({', '.join(read[:6])}"
            f"{', …' if len(read) > 6 else ''}) and its module-level lines name none of {names}.",
            "medium", "auth=authz, no authorization guard in its code", [t["component"]], [t["file"]],
            {"entry": t["id"], "declared_at": where, "read": read[:30], "guards": [g["id"] for g in authz][:20],
             "moves": ["Check what the caller may do before the entry point reads or changes data, "
                       f"with {authz[0]['name']} or another authorization guard.",
                       "If a check elsewhere covers it, add a test that makes this request: a trace that "
                       "passes the guard clears this finding.",
                       "If the caller may act on anything the request names, declare `auth=required`."]},
            weight=Weight(55, "auth=authz")))
    return out
