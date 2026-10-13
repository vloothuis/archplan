"""concepts.string_enum: string values that act as an enum without being one,
from the model's literals section: a field compared or assigned with several
string values across components, or one string compared in many components.
When the model's enums section declares a value set that holds them, the
move is to use it.

literals: [{value, component, file, line, context: compare|case|assign|default|arg, field?, target?}]
enums:    [{name, component, values}]"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .common import EXAMPLES, Weight, focus, plural
from .fields import key

ID = "concepts.string_enum"
NAME = "Strings used as enums"
GROUP = "concepts"
NEEDS = {"literals"}
PARAMS = {"min_values": 2, "max_values": 20, "min_components": 2, "value_components": 3,
          "max_findings": 40}
CONTEXTS = ("compare", "case", "assign", "default", "arg")
COMMON = {"ok", "error", "nil", "none", "null", "true", "false", "undefined", "id", "name", "type", "data", "value",
          "get", "post", "put", "delete", "patch"}
# Field names too generic to hold one concept: a set of values under them mixes
# unrelated meanings from different components.
GENERIC = {"value", "values", "key", "item", "result", "method", "target", "input", "arg", "args", "opts", "options",
           "params", "param", "attrs", "x", "v", "s", "k", "get", "read", "fetch", "event", "msg", "message"}
WORDISH = re.compile(r"[A-Za-z][A-Za-z0-9_:\-]{0,59}")  # no dotted paths, no sentences


def run(ctx, params) -> list:
    by_field = defaultdict(lambda: {"values": Counter(), "comps": Counter(), "sites": [], "tests": 0})
    by_value = defaultdict(lambda: {"comps": Counter(), "sites": []})
    for x in ctx.literals:
        v, c = x.get("value"), x.get("component")
        if not isinstance(v, str) or not WORDISH.fullmatch(v) or x.get("context") not in CONTEXTS \
                or v.lower() in COMMON:
            continue
        site = f"{x.get('file')}:{x.get('line')}" if x.get("file") else None
        fld = x.get("field")
        if isinstance(fld, str) and key(fld) in GENERIC:
            continue
        if isinstance(fld, str) and key(fld):
            e = by_field[key(fld)]
            e["values"][v] += 1
            e["comps"][c] += 1
            e["sites"].append(site)
            e["tests"] += x.get("context") in ("compare", "case")
        elif x.get("context") in ("compare", "case"):
            e = by_value[v]
            e["comps"][c] += 1
            e["sites"].append(site)
    rows = []
    for f, e in by_field.items():
        # A field the code only assigns is configuration; one with many values is a name, not an enum.
        # A value set inside one component is that component's own business.
        if e["tests"] and params["min_values"] <= len(e["values"]) <= params["max_values"] \
                and len(e["comps"]) >= params["min_components"]:
            rows.append(("field", f, e))
    for v, e in by_value.items():
        if len(e["comps"]) >= params["value_components"]:
            rows.append(("value", v, e))
    rows.sort(key=lambda r: (-len(r[2]["comps"]), r[0], r[1]))
    enums = [e for e in ctx.model.get("enums") or [] if isinstance(e, dict) and isinstance(e.get("values"), list)]
    out = []
    for kind, name, e in rows[:params["max_findings"]]:
        comps = [c for c, _ in e["comps"].most_common() if c]
        where = ", ".join(ctx.name(c) for c in comps[:3]) + (", …" if len(comps) > 3 else "")
        enum = "".join(w.capitalize() for w in name.split("_")) if kind == "field" else None
        if kind == "field":
            vals = [v for v, _ in e["values"].most_common()]
            title = (f"{name} takes {plural(len(vals), 'string value')} in {plural(len(comps), 'component')} "
                     f"({where}): " + ", ".join(f"'{v}'" for v in vals[:4]) + (", …" if len(vals) > 4 else "") + ".")
            have = next((e for e in enums if set(vals) <= {str(v) for v in e["values"]}), None)
            moves = ([f"Use the declared enum {have.get('name')} instead of the strings at every compare and "
                      "assignment."] if have else
                     [f"Define a {enum} enum in the component that owns {name}, and use it at every compare and "
                      "assignment."]) + ["Map the strings at the edges (database, API) only."]
        else:
            title = f"The string '{name}' is compared in {plural(len(comps), 'component')} ({where})."
            moves = [f"Make '{name}' a named constant or an enum value owned by one component.",
                     "Let the other components ask that component instead of comparing the string."]
        n_sites = len(e["sites"])
        fc = [focus("components", comps)]
        if kind == "field":
            owners = {tid: [f["name"] for f in t.get("fields") or [] if f.get("name") and key(f["name"]) == name]
                      for tid, t in ctx.types.items() if t.get("component") in comps}
            owners = {tid: v for tid, v in owners.items() if v}
            if owners:
                fc.append(focus("types", list(owners), fields=owners))
        out.append(ctx.finding(
            (kind, name), title, "Sites: " + ", ".join(s for s in e["sites"][:4] if s) + ".",
            "medium" if len(comps) >= 3 else "weak", f"{plural(n_sites, 'site')} in {plural(len(comps), 'component')}",
            comps, [s.rsplit(":", 1)[0] for s in e["sites"] if s],
            {"kind": kind, "name": name, "components": comps[:EXAMPLES],
             **({"values": dict(e["values"].most_common(EXAMPLES))} if kind == "field" else {}),
             "sites": [s for s in e["sites"] if s][:EXAMPLES], "moves": moves}, focus=fc,
            weight=Weight(20, "string enum").spread(len(comps), per=7, cap=16)
            .repetition(n_sites, "sites", per=3, cap=10, single=-4)))
    return out
