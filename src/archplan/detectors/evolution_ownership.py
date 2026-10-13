"""evolution.ownership: components without a clear owner, and authors spread
over many components, from the author counts the hotspots pass caches. Many
active authors with none doing most of the work means nobody holds the
component's design; one author in many components often means the
boundaries do not match how work is split."""

from __future__ import annotations

from collections import defaultdict

from .common import EXAMPLES, Weight, focus, plural

ID = "evolution.ownership"
NAME = "Ownership"
GROUP = "evolution"
NEEDS = {"authors"}
PARAMS = {"active_commits": 2, "min_authors": 6, "max_top_share": 0.3, "min_commits": 20,
          "spread_components": 10, "spread_share": 0.3}


def run(ctx, params) -> list:
    res = ctx.hotspots
    owners = res.get("owners") or {}
    days = res.get("since_days")
    out = []
    per_author = defaultdict(dict)
    for c, authors in sorted(owners.items()):
        if c not in ctx.components or not isinstance(authors, dict):
            continue
        total = sum(authors.values())
        active = {a: n for a, n in authors.items() if n >= params["active_commits"]}
        for a, n in active.items():
            per_author[a][c] = n
        if total < params["min_commits"] or len(active) < params["min_authors"]:
            continue
        top = max(authors.values()) / total
        if top > params["max_top_share"]:
            continue
        out.append(ctx.finding(
            (c,),
            f"{ctx.name(c)} has {len(active)} active authors and no clear owner: the most active one made "
            f"{round(top * 100)}% of its commits.",
            f"{total} commits in the last {days} days.",
            "medium" if top <= params["max_top_share"] / 2 else "weak",
            f"{len(active)} authors with {params['active_commits']}+ commits, top share {top:.2f}",
            [c], [], {"component": c, "authors": len(active), "commits": total, "top_share": round(top, 3),
                      "moves": [f"Name an owner for {ctx.name(c)} who reviews changes to its interface.",
                                "If it changes for unrelated reasons, split it along those reasons."]},
            focus=[focus("components", [c])],
            weight=Weight(25, "no clear owner").spread(len(active), "active authors", per=5, cap=12,
                                                         free=params["min_authors"])
            .add("top share", round(top, 2), 8 if top <= params["max_top_share"] / 2 else 0)))
    n_comps = len([c for c in owners if c in ctx.components])
    limit = max(params["spread_components"], params["spread_share"] * n_comps)
    spread = sorted(((a, cs) for a, cs in per_author.items() if len(cs) >= limit), key=lambda x: (-len(x[1]), x[0]))
    if spread:
        out.append(ctx.finding(
            ("spread",),
            f"{plural(len(spread), 'author')} each changed {round(limit)} or more components in the last {days} days.",
            "The most: " + ", ".join(f"{len(cs)} components" for _, cs in spread[:3]) + ".",
            "weak", f"of {n_comps} components with commits", sorted({c for _, cs in spread for c in cs})[:EXAMPLES], [],
            {"authors": [{"author": a, "components": len(cs), "top": sorted(cs, key=lambda c: -cs[c])[:5]}
                         for a, cs in spread[:EXAMPLES]],
             "moves": ["Check whether a typical change touches many components: the boundaries may cut "
                       "through features.", "Group the components these authors change together."]},
            focus=[focus("components", sorted({c for _, cs in spread for c in cs})[:EXAMPLES])],
            weight=Weight(20, "authors spread out").spread(len(spread), "authors", per=4, cap=10)))
    return out
