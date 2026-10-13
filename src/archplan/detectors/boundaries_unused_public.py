"""boundaries.unused_public: files with public types in a component other
components depend on, which only that component itself references. Its
real interface is smaller than it looks; the rest can be made internal.

Files nobody references at all are left out: a framework finds those by
convention (admin modules, routes, plugins), or they are dead code. Only
components with dependents count, so entry points do not show up."""

from __future__ import annotations

import re
from collections import defaultdict

from ..hotspots import Mapper
from .common import EXAMPLES, Weight, plural, types_focus

ID = "boundaries.unused_public"
NAME = "Public code nobody else uses"
GROUP = "boundaries"
NEEDS = {"files", "types"}
PARAMS = {"min_files": 3, "min_share": 0.6, "max_findings": 20}
PUBLIC_KINDS = ("class", "struct", "record", "interface", "enum", "alias", "function", "module")
BARRELS = {"__init__.py", "index.ts", "index.tsx", "index.js", "index.mjs", "mod.rs", "package-info.java"}
TEST = re.compile(r"(^|/)(tests?|spec|__tests__|test_[^/]*|[^/]*_test\.[a-z]+|[^/]*\.(test|spec)\.[a-z]+)($|/)")


def run(ctx, params) -> list:
    files = ctx.model.get("files") or {}
    mapper = Mapper(ctx.components)
    comp = lambda f: (files.get(f) or {}).get("component") or mapper(f)
    used, inner, refs = set(), set(), defaultdict(list)
    for f, v in files.items():
        for r in v.get("refs") or []:
            to = r.get("to")
            refs[f].append(to)
            if to and comp(to) != comp(f):
                used.add(to)
            elif to and to != f:
                inner.add(to)
    # A barrel (__init__.py, index.ts) that others import passes the use on to what it re-exports.
    todo = [f for f in used if f.rsplit("/", 1)[-1] in BARRELS]
    while todo:
        b = todo.pop()
        for to in refs.get(b, ()):
            if to not in used and comp(to) == comp(b):
                used.add(to)
                if to.rsplit("/", 1)[-1] in BARRELS:
                    todo.append(to)
    by_file = defaultdict(list)
    for tid, t in ctx.types.items():
        f, c = t.get("file"), t.get("component")
        if f and c in ctx.internal and t.get("kind") in PUBLIC_KINDS and not (t.get("name") or "_").startswith("_") \
                and not TEST.search(f):
            by_file[f].append(tid)
    for tid, t in ctx.types.items():  # a type another component refers to makes its file used
        for r in [x.get("ref") for x in t.get("fields") or []] + list(t.get("uses") or []) + list(t.get("implements") or []):
            rt = ctx.types.get(r) if r else None
            if rt and rt.get("component") != t.get("component") and rt.get("file"):
                used.add(rt["file"])
    dependents = defaultdict(set)
    for a, b in ctx.graph:
        dependents[b].add(a)
    per = defaultdict(lambda: [[], 0])
    for f, tids in by_file.items():
        c = ctx.types[tids[0]]["component"]
        if not dependents[c]:
            continue
        per[c][1] += 1
        if f not in used and f in inner:
            per[c][0].append((f, tids))
    out = []
    for c, (unused, total) in sorted(per.items(), key=lambda kv: (-len(kv[1][0]), kv[0])):
        if len(unused) < params["min_files"] or len(unused) / total < params["min_share"] \
                or len(out) >= params["max_findings"]:
            continue
        unused.sort()
        names = [ctx.types[t].get("name") or t for _, ts in unused for t in ts]
        out.append(ctx.finding(
            (c,),
            f"{ctx.name(c)} has {len(unused)} of {plural(total, 'file')} with public types that only it uses, "
            f"though {plural(len(dependents[c]), 'component')} {'depends' if len(dependents[c]) == 1 else 'depend'} "
            "on it.",
            "Such as " + ", ".join(names[:5]) + (", …" if len(names) > 5 else "") + ".",
            "medium" if len(unused) / total >= 0.8 else "weak",
            f"{len(unused)} of {total} files used only inside, {plural(len(dependents[c]), 'dependent')}",
            [c], [f for f, _ in unused],
            {"component": c, "unused": [{"file": f, "types": ts[:5]} for f, ts in unused[:EXAMPLES]],
             "files": total, "dependents": sorted(dependents[c])[:EXAMPLES],
             "moves": [f"Make what only {ctx.name(c)} uses internal (private modules, no export), so its "
                       "interface shows what others may use.",
                       "Remove the code that nothing uses at all."]},
            focus=types_focus({}, [c], [t for _, ts in unused for t in ts]),
            weight=Weight(20, "unused public code").add("share", f"{round(len(unused) / total * 100)}%",
                                                         20 * (len(unused) / total - params["min_share"]))
            .spread(len(unused), "files", per=4, cap=10, free=params["min_files"] - 1)
            .spread(len(dependents[c]), "dependents", per=3, cap=6)))
    return out
