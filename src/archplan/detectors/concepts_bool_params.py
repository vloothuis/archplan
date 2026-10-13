"""concepts.bool_params: components whose functions take boolean flags, from
the model's signatures section. A flag argument means one function does two
things, and every caller must know which. One finding per component.

signatures: [{id, component, file, line, params: [{name, type}], returns, public?}]
Private functions (public: false) do not count."""

from __future__ import annotations

from collections import Counter, defaultdict

from .common import EXAMPLES, Weight, focus, plural
from .fields import is_flag

ID = "concepts.bool_params"
NAME = "Boolean flag parameters"
GROUP = "concepts"
NEEDS = {"signatures"}
PARAMS = {"min_functions": 5, "many_flags": 2}


def run(ctx, params) -> list:
    by_comp = defaultdict(list)
    for s in ctx.signatures:
        c = s.get("component")
        if c not in ctx.internal or s.get("public") is False:
            continue
        ps = [p for p in s.get("params") or [] if isinstance(p, dict)]
        flags = [p.get("name") for p in ps if isinstance(p.get("type"), str) and is_flag(p)]
        if flags:
            by_comp[c].append((s, flags))
    out = []
    for c, rows in sorted(by_comp.items()):
        many = [r for r in rows if len(r[1]) >= params["many_flags"]]
        if len(rows) < params["min_functions"] and not many:
            continue
        rows.sort(key=lambda r: (-len(r[1]), str(r[0].get("id"))))
        names = Counter(n for _, fs in rows for n in fs if n)
        out.append(ctx.finding(
            (c,),
            f"{plural(len(rows), 'function')} in {ctx.name(c)} take boolean flags"
            + (f"; {len(many)} take {params['many_flags']} or more." if many else "."),
            "Such as " + ", ".join(f"{r[0].get('id')}({', '.join(map(str, r[1][:3]))})" for r in rows[:3]) + ".",
            "medium" if many else "weak",
            f"{plural(len(rows), 'function')} with flags" + (f", most used: {names.most_common(1)[0][0]}" if names else ""),
            [c], [r[0].get("file") for r in rows],
            {"component": c, "functions": [{"id": r[0].get("id"), "flags": r[1], "file": r[0].get("file"),
                                            "line": r[0].get("line")} for r in rows[:EXAMPLES]],
             "names": dict(names.most_common(10)),
             "moves": ["Split each function with a flag into two named functions, or pass an enum or an options "
                       "type that names the choice.",
                       f"Start with the flag used most: {names.most_common(1)[0][0]}." if names else
                       "Start with the functions called from other components."]},
            focus=[focus("components", [c])],
            weight=Weight(25, "flag parameters").repetition(len(rows), "functions", per=4, cap=12, single=-4)
            .spread(len(many), "functions with several flags", per=8, cap=12, free=0.5)))
    return out
