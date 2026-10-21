"""history.large_active_file: a large file that changes often, for several
reasons that the git history tells apart.

After Lefever, Cai, Kazman and Fang (2023, arXiv 2302.09153): large files
that change often sit at the centre of much technical debt, and the files
they change together with show their responsibilities. Each responsibility
is a group of partner files that change with the large file in the same
commits. Moving that part out, next to its partners, makes the file smaller
where it costs the most, and leaves alone what only looks large.

A candidate has at least min_lines lines and min_commits commits (cached
hotspots, cochange.files; the analysis reads the newest MAX_FILE_SHAS of
them). Its partners are the files it shares at least min_together of those
commits with, in at least min_degree of the commits of the one that changes
least. Partners that change with the file in the same commits form one
responsibility (see responsibilities). Each needs min_together commits. A
file with two or more responsibilities is a finding. This detector never
runs git."""

from __future__ import annotations

import math
from collections import defaultdict

from ..core.hotspots import Mapper
from ..core.vocab import is_test
from .boundaries_misplaced import dicts, strs
from .common import EXAMPLES, Weight, focus, plural
from .drift import fix_test

ID = "history.large_active_file"
NAME = "Large files that change for several reasons"
GROUP = "history"
NEEDS = {"git", "files", "cochange"}
PARAMS = {"min_lines": 400, "min_commits": 10, "min_together": 3, "min_degree": 0.3, "max_findings": 15}
EXAMPLES_PER = 3  # commit subjects per responsibility


def responsibilities(f: str, shas: dict, min_together: int, min_degree: float) -> list:
    """[{"partners", "commits"}] for file f, largest first: the groups of
    partner files that change with f, and f's commits that touch them.

    The partner that shares the most commits with f starts a group; each
    next partner joins the first group whose starting partner it changes
    with in at least half of its commits with f, else starts its own. A
    group grows from one seed, so a partner that changes with everything
    cannot chain all groups into one. A group that shares half or more of
    its commits with a larger one is the same reason, and joins it."""
    mine = shas.get(f) or set()
    shared = {}
    for g, s in shas.items():
        both = mine & s if g != f else set()
        if len(both) >= min_together and len(both) >= min_degree * min(len(mine), len(s)):
            shared[g] = both
    groups = []
    for g in sorted(shared, key=lambda g: (-len(shared[g]), g)):
        home = next((x for x in groups if len(shared[g] & shared[x[0]]) >= 0.5 * len(shared[g])), None)
        if home is None:
            groups.append([g])
        else:
            home.append(g)
    out = []
    for gs in sorted(groups, key=lambda gs: -len(set().union(*(shared[g] for g in gs)))):
        commits = set().union(*(shared[g] for g in gs))
        home = next((r for r in out if len(commits & r["commits"]) >= 0.5 * len(commits)), None)
        if home:
            home["partners"] += gs
            home["commits"] |= commits
        elif len(commits) >= min_together:
            out.append({"partners": list(gs), "commits": commits})
    return sorted(out, key=lambda r: (-len(r["commits"]), r["partners"]))


def run(ctx, params) -> list:
    cc = ctx.hotspots.get("cochange") or {}
    files = dicts(ctx.model.get("files"))
    mapper = Mapper(dicts(ctx.components))
    comp_of = lambda f: files[f].get("component") if isinstance(files[f].get("component"), str) else mapper(f)
    entries = dicts(cc.get("files"))
    shas = {f: set(strs(v.get("shas"))) for f, v in entries.items() if f in files and not is_test(f)}
    count = lambda f: max(entries[f].get("commits") if isinstance(entries[f].get("commits"), int) else 0,
                          len(shas[f]))
    subjects = cc.get("subjects") if isinstance(cc.get("subjects"), dict) else {}
    rank = {x: i for i, x in enumerate(subjects)}  # newest first
    newest = lambda commits: [subjects[x] for x in sorted(commits, key=lambda x: (rank.get(x, len(rank)), x))
                              if isinstance(subjects.get(x), str)]
    is_fix = fix_test()
    types_of = defaultdict(list)
    for tid, t in dicts(ctx.types).items():
        if isinstance(t.get("file"), str):
            types_of[t["file"]].append(tid)
    rows = []
    for f in sorted(shas):
        if count(f) < max(1, params["min_commits"]) or comp_of(f) not in ctx.internal:
            continue
        text = ctx.read(f)
        lines = len(text.splitlines()) if text else 0
        if lines < max(1, params["min_lines"]):
            continue
        rs = responsibilities(f, shas, max(1, params["min_together"]), params["min_degree"])
        if len(rs) >= 2:
            rows.append((f, lines, rs))
    rows.sort(key=lambda r: (-count(r[0]) * len(r[2]), r[0]))
    out = []
    short = lambda f: "/".join(f.split("/")[-2:])
    for f, lines, rs in rows[:params["max_findings"]]:
        c = comp_of(f)
        n = count(f)
        fixes = sum(1 for s in shas[f] if is_fix(subjects.get(s)))
        alone = len(shas[f] - set().union(*(r["commits"] for r in rs)))
        where = lambda r: sorted({comp_of(g) for g in r["partners"]} - {None})
        title = (f"{short(f)} ({lines} lines, {plural(n, 'commit')}) changes for {len(rs)} reasons: "
                 + "; ".join(f"with {', '.join(short(g) for g in r['partners'][:2])}"
                             + (f" and {len(r['partners']) - 2} more" if len(r["partners"]) > 2 else "")
                             for r in rs[:2]) + (f"; and {len(rs) - 2} more" if len(rs) > 2 else "") + ".")
        detail = " ".join(
            f"With {', '.join(short(g) for g in r['partners'][:3])}: {plural(len(r['commits']), 'commit')}"
            + (", such as '" + "', '".join(newest(r["commits"])[:2]) + "'" if newest(r["commits"]) else "") + "."
            for r in rs[:3])
        if alone:
            detail += (f" {alone} of its {'last ' if len(shas[f]) < n else ''}{plural(len(shas[f]), 'commit')} "
                       "changed none of these partners.")
        moves = []
        for r in rs[:2]:
            other = [x for x in where(r) if x != c]
            target = f" in {ctx.name(other[0])}" if len(other) == 1 and len(where(r)) == 1 else " next to them"
            moves.append(f"Extract the part of {short(f)} that changes with "
                         f"{', '.join(short(g) for g in r['partners'][:2])} into its own module{target}.")
        moves.append("Leave the part that changes with none of them: large but stable code costs little.")
        out.append(ctx.finding(
            (f,), title, detail, "medium",
            f"{lines} lines, {plural(n, 'commit')}, {len(rs)} groups of partner files",
            [c] + [x for r in rs for x in where(r)], [f] + [g for r in rs for g in r["partners"]],
            {"file": f, "lines": lines, "commits": n, "bug_fixes": fixes, "alone": alone,
             "types": sorted(types_of.get(f, []))[:EXAMPLES],
             "responsibilities": [{"partners": r["partners"][:EXAMPLES], "commits": len(r["commits"]),
                                   "components": where(r),
                                   "subjects": newest(r["commits"])[:EXAMPLES_PER]} for r in rs[:EXAMPLES]],
             "moves": moves},
            focus=[focus("components", list(dict.fromkeys([c] + [x for r in rs for x in where(r)])),
                         files=[f] + [g for r in rs for g in r["partners"]][:EXAMPLES - 1], label="Components")],
            weight=Weight(25, "large active file").spread(len(rs), "responsibilities", per=8, cap=16, free=1)
            .add("commits", n, min(12, 4 * math.log2(n / max(1, params["min_commits"]))))
            .add("lines", lines, min(8, 4 * math.log2(lines / max(1, params["min_lines"]))))
            .add("bug fixes", fixes, min(8, 2 * math.log2(1 + fixes)))))
    return out
