"""history.modularity_violation: files that change together in git history
while the code shows no structural relation between them.

This follows the modularity violations of Xiao, Cai and Kazman (DRSpaces,
ICSE 2014; DV8). When two files keep changing in the same commits but neither
imports, calls or refers to a type of the other, something they share is not
in the code: a hidden assumption, a duplicated rule, a protocol both sides
follow, or configuration that names code by string. Each change then has to
remember the other file.

The pairs come from the file-level co-change that `archplan hotspots` caches
(cochange in hotspots.json). This detector never runs git.

A structural relation is one of:
  - a file reference either way (the model's files section: imports, calls);
  - a reference through a package entry such as __init__.py or index.ts
    that re-exports the other file;
  - a type in one file that refers to a type in the other (a field, a type
    in a signature, an implemented interface).

Pairs inside one component are left out by default: a component is the
unit that may change as a whole. One finding covers the violating pairs
between two components. Files with the same name in different components
(say api/schema.py in five components) form one finding of their own: a
structure repeated across components, where a change to one needs the
others. Pairs of such a file with another file join that finding.

The weight grows with the commits the files share, how often they change
together (degree), the bug-fix commits on the cluster's files (the churn
the hidden link has cost, as DV8 ranks it) and the number of components.

When the files share string literals or field names that few other files
use, the finding names them: they are likely the knowledge the files share,
such as the values of a status or the fields of an address.

How it differs from history.coupling: that detector works per component
pair and only reports pairs without a component dependency. This one works
per file. It also finds files whose components do depend on each other, as
long as these files have no relation, and it ignores co-change between files
that do import each other. Where both report the same component pair, this
finding names the files and the bug-fix cost."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from ..core.hotspots import Mapper
from .common import COMMON_FIELDS, EXAMPLES, Weight, finding, focus, namer, plural
from .concepts_string_enum import COMMON, WORDISH
from .drift import FIX_WORDS, fix_test
from .fields import key

ID = "history.modularity_violation"
NAME = "Modularity violations"
GROUP = "history"
NEEDS = {"git", "files", "cochange"}
PARAMS = {"together": 3, "degree": 0.5, "within_component": False, "fix_words": FIX_WORDS, "rare_files": 0.1}
BARRELS = {"__init__.py", "index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs", "mod.rs", "lib.rs"}


def run(ctx, params) -> list:
    return modularity_findings(ctx.hotspots, ctx.model, params)


def relations(model: dict):
    """related(a, b): True when the code relates file a and file b."""
    refs = defaultdict(set)
    for f, v in (model.get("files") or {}).items():
        for r in (v or {}).get("refs") or []:
            if isinstance(r, dict) and r.get("to"):
                refs[f].add(r["to"])
    types = model.get("types") or {}
    tfile = {t: (v or {}).get("file") for t, v in types.items()}
    typed = set()
    for t, v in types.items():
        v = v or {}
        targets = [x.get("ref") for x in v.get("fields") or [] if isinstance(x, dict)]
        targets += list(v.get("uses") or []) + list(v.get("implements") or [])
        for x in targets:
            if v.get("file") and tfile.get(x) and tfile[x] != v["file"]:
                typed.add((v["file"], tfile[x]))

    def one_way(a, b):
        if b in refs[a] or (a, b) in typed:
            return True
        return any(x.rsplit("/", 1)[-1] in BARRELS and b in refs[x] for x in refs[a])
    return lambda a, b: one_way(a, b) or one_way(b, a)


def knowledge(model: dict, share: float):
    """shared(a, b): {"strings", "fields"} that file a and file b both use, from
    the literals section and the types' fields. A value in more than share of
    the files (at least 5) is an idiom, not knowledge, and is left out."""
    def rare(by_file: dict) -> dict:
        n = Counter(v for vs in by_file.values() for v in vs)
        cap = max(5, share * len(model.get("files") or {}))
        return {f: {v for v in vs if n[v] <= cap} for f, vs in by_file.items()}
    strings, fields = defaultdict(set), defaultdict(set)
    for x in model.get("literals") or []:
        v = x.get("value") if isinstance(x, dict) else None
        if isinstance(v, str) and WORDISH.fullmatch(v) and v.lower() not in COMMON and x.get("file"):
            strings[x["file"]].add(v)
    for t in (model.get("types") or {}).values():
        if isinstance(t, dict) and t.get("file"):
            fields[t["file"]].update(k for k in (key(f.get("name") or "") for f in t.get("fields") or []
                                                 if isinstance(f, dict)) if k and k not in COMMON_FIELDS)
    strings, fields = rare(strings), rare(fields)
    return lambda a, b: {"strings": sorted(strings.get(a, set()) & strings.get(b, set())),
                         "fields": sorted(fields.get(a, set()) & fields.get(b, set()))}


def quoted(xs: list, n: int = 4) -> str:
    return ", ".join(f"'{x}'" for x in xs[:n]) + (", …" if len(xs) > n else "")


def base(f: str) -> str:
    return f.rsplit("/", 1)[-1]


def groups(pairs: list) -> list:
    """Pairs of files with the same name in different components form one group
    per name: a structure repeated across components (schema.py, urls.py).
    A pair with one file of such a name joins that group too: often a central
    file that lists the copies. The other pairs form one group per component
    pair."""
    names = {base(x["a"]) for x in pairs if base(x["a"]) == base(x["b"])}
    out = defaultdict(list)
    for x in pairs:
        name = next((base(f) for f in (x["a"], x["b"]) if base(f) in names), None)
        out[("name", name) if name else ("components",) + tuple(sorted((x["ca"], x["cb"])))].append(x)
    return [out[k] for k in sorted(out)]


def modularity_findings(res: dict, model: dict, params: dict | None = None) -> list:
    params = {**PARAMS, **(params or {})}
    is_fix = fix_test(params["fix_words"])
    cc = res.get("cochange") or {}
    files = model.get("files") or {}
    comps = model.get("components") or {}
    mapper = Mapper(comps)
    comp_of = lambda f: (files.get(f) or {}).get("component") or mapper(f)
    related = relations(model)
    shared_by = knowledge(model, params["rare_files"])
    subjects = cc.get("subjects") or {}
    fixes = {s for s, subj in subjects.items() if is_fix(subj)}
    file_shas = {f: set(v.get("shas") or []) for f, v in (cc.get("files") or {}).items() if isinstance(v, dict)}
    pairs = []
    for x in cc.get("pairs") or []:
        a, b = x.get("a"), x.get("b")
        if a not in files or b not in files:
            continue
        if x.get("together", 0) < params["together"] or x.get("degree", 0) < params["degree"]:
            continue
        ca, cb = comp_of(a), comp_of(b)
        if not ca or not cb or ca not in comps or cb not in comps:
            continue
        if ca == cb and not params["within_component"]:
            continue
        if related(a, b):
            continue
        shas = set(x.get("shas") or [])
        pairs.append({**x, "ca": ca, "cb": cb, "fixes": len(shas & fixes)})
    name = namer(model)
    days = res.get("since_days")
    out = []
    for xs in groups(pairs):
        xs.sort(key=lambda x: (-x["together"] * x["degree"], x["a"], x["b"]))
        fs = sorted({f for x in xs for f in (x["a"], x["b"])}, key=lambda f: (comp_of(f), f))
        cs = sorted({comp_of(f) for f in fs})
        shared = set().union(*(set(x.get("shas") or []) for x in xs))
        churn = set().union(*(file_shas.get(f, set()) for f in fs)) & fixes
        fixed_together = shared & fixes
        top = xs[0]
        degree = max(x["degree"] for x in xs)
        tail = lambda f, n: "/".join(f.split("/")[-n:])
        two = lambda x: (tail(x["a"], 2), tail(x["b"], 2)) if tail(x["a"], 2) != tail(x["b"], 2) \
            else (tail(x["a"], 3), tail(x["b"], 3))
        short = lambda f: tail(f, 2)
        same = next((base(f) for x in xs for f in (x["a"], x["b"])
                     if base(x["a"]) == base(x["b"]) and base(f) == base(x["a"])), None)
        listing = lambda n: ", ".join(name(c) for c in cs[:n]) + (f" and {len(cs) - n} more" if len(cs) > n else "")
        if same:
            k = sum(1 for f in fs if base(f) == same)
            others = len(fs) - k
            more = " and " + plural(others, "other file") if others else ""
            title = f"{same} in {k} components{more} change together, but do not refer to each other."
        elif len(xs) == 1:
            title = (f"{short(top['a'])} and {short(top['b'])} change together in {top['together']} commits, "
                     f"but neither refers to the other.")
        else:
            title = (f"{plural(len(fs), 'file')} in {listing(2)} change together, "
                     f"but do not refer to each other.")
        detail = "; ".join(f"{' + '.join(two(x))}: {x['together']} commits, "
                           f"{round(x['degree'] * 100)}%" for x in xs[:3]) + (f"; and {len(xs) - 3} more pairs"
                                                                            if len(xs) > 3 else "") + "."
        if churn:
            detail += f" {plural(len(churn), 'bug-fix commit')} touched these files"
            detail += f", {len(fixed_together)} of them several at once." if fixed_together else "."
        examples = [subjects[s] for s in sorted(fixed_together) if isinstance(subjects.get(s), str)][:3]
        known = {"strings": set(), "fields": set()}
        for x in xs:
            for k, v in shared_by(x["a"], x["b"]).items():
                known[k].update(v)
        known = {k: sorted(v)[:EXAMPLES] for k, v in known.items() if v}
        n_known = sum(map(len, known.values()))
        if known:
            detail += " Both use " + " and ".join(
                f"the strings {quoted(v)}" if k == "strings" else
                f"the fields {', '.join(v[:4])}" + (", …" if len(v) > 4 else "") for k, v in known.items()) + "."
        named = ([f"Give the values {quoted(known['strings'])} one home, such as an enum or constants owned by one "
                  "component, and let the other files refer to it."] if known.get("strings") else []) + \
                ([f"Give the fields {', '.join(known['fields'][:4])} one type owned by one component, and use it "
                  "in both places."] if known.get("fields") else [])
        w = (Weight(20, "modularity violation")
             .repetition(len(shared), "commits together", per=4, cap=12, single=0)
             .add("degree", round(degree, 2), min(10, 25 * (degree - params["degree"])))
             .add("bug fixes together", len(fixed_together), min(10, 4 * len(fixed_together)))
             .add("bug-fix churn", len(churn), min(6, 1.5 * math.log2(1 + len(churn))))
             .spread(len(cs), "components", per=4, cap=8, free=2)
             .spread(len(xs), "file pairs", per=1.5, cap=4)
             .add("shared knowledge", n_known, min(6, 2 * n_known)))
        out.append(finding(
            ID, ("name", same) if same else ("components", *cs), title, detail, "medium",
            f"{len(shared)} commits together in the last {days} days, no file reference",
            cs, fs,
            {"pairs": [{"a": x["a"], "b": x["b"], "together": x["together"], "degree": x["degree"],
                        "fixes": x["fixes"]} for x in xs[:EXAMPLES]],
             "commits": len(shared), "bug_fixes": len(churn), "bug_fixes_together": len(fixed_together),
             "fixes_together": examples, **({"shared": known} if known else {}),
             "head": (res.get("head") or "")[:10], "since_days": days,
             "moves": named + ([f"Give the structure that each {same} repeats one shared definition, such as a base "
                        f"class, a helper or a registry, and let each component use it."] if same else []) + [
                       "Find what these files both know: a rule, a format, a list of names or a setting. "
                       "Give it one home and let both files refer to it.",
                       "If they are two halves of one feature, move them into one component.",
                       "If the link is configuration (a dotted path, a route, a registry), make it an "
                       "explicit reference the exporter can see."]},
            focus=[focus("components", cs, files=fs[:EXAMPLES], label="Components")],
            weight=w))
    return out
