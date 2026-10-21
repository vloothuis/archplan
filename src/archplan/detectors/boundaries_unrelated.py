"""boundaries.unrelated_parts: a component whose files fall into groups that
neither refer to each other nor change together. It holds two or more
things that only share a folder, and could be split.

This is the other half of PairSmell (Zhong et al., 2024, arXiv 2411.01012):
"inapt collocated" pairs, files kept in one module that have nothing to do
with each other. Those pairs changed together 35% less than other pairs in
one module. The most telling signals for them were dependencies and shared
words (2026, arXiv 2606.22576).

The links are those of boundaries.misplaced_file: file and type references
either way, and co-change of at least min_degree (cached hotspots). Package
entries (__init__.py, index.ts) do not link: a re-export is packaging, not
cohesion. A group needs min_group files; single files that link to nothing
in the component are listed, but do not make a finding on their own.

When the words of the groups' identifiers (tf-idf) are alike, the groups may
be one concept written twice, and the finding weighs less; when they differ,
it weighs more."""

from __future__ import annotations

from collections import defaultdict

from .boundaries_misplaced import ENTRIES, FileGraph, base, similarity, term_vectors
from .common import EXAMPLES, Weight, focus, plural

ID = "boundaries.unrelated_parts"
NAME = "Components with unrelated parts"
GROUP = "boundaries"
NEEDS = {"files"}
PARAMS = {"min_group": 2, "min_degree": 0.5, "terms": True, "max_findings": 20}


def parts(fg: FileGraph, files: list) -> list:
    """The connected groups of files, by their links inside the list; largest first."""
    inside = set(files)
    seen, out = set(), []
    for f in sorted(files):
        if f in seen:
            continue
        group, todo = [], [f]
        seen.add(f)
        while todo:
            x = todo.pop()
            group.append(x)
            for g, link in fg.links.get(x, {}).items():
                if g in inside and g not in seen and (link["out"] or link["in"] or link["cochange"]):
                    seen.add(g)
                    todo.append(g)
        out.append(sorted(group))
    return sorted(out, key=lambda g: (-len(g), g))


def run(ctx, params) -> list:
    fg = FileGraph(ctx, params["min_degree"])
    by_comp = defaultdict(list)
    for f, c in fg.comp.items():
        if base(f) not in ENTRIES:
            by_comp[c].append(f)
    rows = []
    for c, files in by_comp.items():
        groups = parts(fg, files)
        big = [g for g in groups if len(g) >= params["min_group"]]
        if len(big) >= 2:
            rows.append((c, big, [g[0] for g in groups if len(g) == 1]))
    if not rows:
        return []
    vecs = term_vectors(ctx, sorted(f for _, big, _ in rows for g in big for f in g)) if params["terms"] else {}
    rows.sort(key=lambda r: (-len(r[1]), -sum(map(len, r[1])), r[0]))
    out = []
    for c, big, loose in rows[:params["max_findings"]]:
        alike = None
        if vecs:
            a = [vecs[f] for f in big[0] if f in vecs]
            b = [vecs[f] for g in big[1:] for f in g if f in vecs]
            if a and b:
                total = defaultdict(float)
                for v in a:
                    for w, x in v.items():
                        total[w] += x
                alike = round(similarity(dict(total), b), 2)
        short = lambda f: f.rsplit("/", 1)[-1]
        shown = lambda g: ", ".join(short(f) for f in g[:3]) + (f" and {len(g) - 3} more" if len(g) > 3 else "")
        title = (f"{ctx.name(c)} holds {len(big)} groups of files that neither refer to each other nor change "
                 f"together.")
        detail = "; ".join(f"({shown(g)})" for g in big[:3]) + (f"; and {len(big) - 3} more groups"
                                                                 if len(big) > 3 else "") + "."
        if loose:
            detail += f" {plural(len(loose), 'file')} link to nothing in it: {', '.join(map(short, loose[:3]))}" \
                      + (", …" if len(loose) > 3 else "") + "."
        types_of = defaultdict(list)
        for tid, t in ctx.types.items():
            if (t or {}).get("file"):
                types_of[t["file"]].append(tid)
        moves = [f"Split {ctx.name(c)}: give the group of {shown(big[1])} its own component "
                 "(add_component, then move_type for its types).",
                 "If the groups share a concept the code does not show (a format, a rule), make that link explicit "
                 "instead."]
        out.append(ctx.finding(
            (c,), title, detail, "weak",
            f"{len(big)} unlinked groups of {params['min_group']} or more files",
            [c], [f for g in big for f in g],
            {"component": c, "groups": [{"files": g[:EXAMPLES], "types": [t for f in g for t in types_of[f]][:EXAMPLES]}
                                        for g in big[:EXAMPLES]],
             "loose": loose[:EXAMPLES], **({"words_alike": alike} if alike is not None else {}), "moves": moves},
            focus=[focus("components", [c], files=[f for g in big for f in g][:EXAMPLES], label="Component")],
            weight=Weight(25, "unrelated parts").spread(len(big), "groups", per=8, cap=16, free=1)
            .spread(min(len(g) for g in big[:2]), "files in the second group", per=4, cap=10,
                    free=params["min_group"])
            .add("words", "alike" if alike is not None and alike >= 0.3 else "different" if alike is not None else None,
                 -10 if alike is not None and alike >= 0.3 else 6 if alike is not None and alike < 0.1 else 0)))
    return out
