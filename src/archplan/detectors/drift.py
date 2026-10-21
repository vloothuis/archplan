"""How copies of one piece of knowledge changed in git history: together, or
one without the others.

A finding about duplicated knowledge (the same test in several components,
the same operation written by each caller, the same run of calls in several
entry points) names the files that hold the copies. The cached hotspots know
the commits of each file (cochange.files). A commit that changes two or more
of the copies' files is the cost of the duplication: each change has to be
made in every place. A bug fix that changes some of the files and misses
others is worse: the copies have probably drifted apart, and the files the
fix missed may still have the bug. Mondal et al. (2024, arXiv 2409.08555)
found that a large share of clone pairs change in this way.

This works per file, not per line: a commit to a file need not touch the
copy in it. A bug fix is the most likely kind of commit to touch the logic,
so only fixes count as drift. A file whose commits the cache cut off
(MAX_FILE_SHAS in core.hotspots) cannot show that a fix missed it, and is left out of the
missed files. This module never runs git."""

from __future__ import annotations

import math
import re

# Words in a commit subject that mark a bug fix. A word list, not a regular
# expression: config.json is repo data, and a crafted pattern could backtrack
# without end on the commit subjects.
FIX_WORDS = ["fix", "fixes", "fixed", "fixing", "bug", "bugs", "bugfix", "bugfixes", "hotfix", "hotfixes",
             "regression", "crash", "crashes", "crashed", "broken", ":bug:", ":ambulance:", ":adhesive_bandage:"]
WORD = re.compile(r":[a-z_]+:|[a-z0-9]+")
EXAMPLES = 3  # fixes that missed a copy, kept as evidence


def fix_test(words=FIX_WORDS):
    """is_fix(subject): True when the subject has one of the words."""
    ws = {w.lower() for w in words}
    return lambda subject: isinstance(subject, str) and not ws.isdisjoint(WORD.findall(subject.lower()))


def drift(hotspots: dict | None, files, is_fix=None) -> dict | None:
    """{"files", "together", "fixes_apart", "fixes"} for the files of the
    copies, or None without file-level history or with fewer than two files.
    together: commits that changed two or more of the files. fixes_apart: bug
    fixes that changed some of them and missed others. fixes: up to EXAMPLES
    of those, newest first, as {"subject", "changed", "missed"}."""
    cc = (hotspots or {}).get("cochange")
    fs = sorted({f for f in files if isinstance(f, str) and f})
    if not isinstance(cc, dict) or not isinstance(cc.get("files"), dict) or len(fs) < 2:
        return None
    is_fix = is_fix or fix_test()
    subjects = cc.get("subjects") if isinstance(cc.get("subjects"), dict) else {}
    rank = {s: i for i, s in enumerate(subjects)}  # the cache lists subjects newest first
    shas, complete = {}, set()
    for f in fs:
        v = cc["files"].get(f)
        xs = [s for s in (v.get("shas") or []) if isinstance(s, str)] if isinstance(v, dict) else []
        shas[f] = set(xs)
        n = v.get("commits") if isinstance(v, dict) else 0
        if not isinstance(n, int) or n <= len(xs):
            complete.add(f)
    together, apart = 0, []
    for s in sorted(set().union(*shas.values()), key=lambda s: (rank.get(s, len(rank)), s)):
        changed = [f for f in fs if s in shas[f]]
        if len(changed) >= 2:
            together += 1
        missed = [f for f in fs if f not in changed and f in complete]
        if missed and is_fix(subjects.get(s)):
            apart.append({"subject": subjects[s], "changed": changed, "missed": missed})
    return {"files": len(fs), "together": together, "fixes_apart": len(apart), "fixes": apart[:EXAMPLES]}


def tail(f: str) -> str:
    return "/".join(f.split("/")[-2:])


def sentence(d: dict | None) -> str:
    """What the history says about the copies, for a finding's detail; "" when nothing."""
    if not d:
        return ""
    out = []
    if d["together"]:
        out.append(f"The files of the copies changed together in {d['together']} "
                   f"commit{'s' if d['together'] != 1 else ''}.")
    if d["fixes_apart"]:
        x = d["fixes"][0]
        n = d["fixes_apart"]
        out.append(f"{n} bug fix{'es' if n != 1 else ''} changed some copies and missed others, such as "
                   f"'{x['subject']}' in {', '.join(tail(f) for f in x['changed'][:2])}, "
                   f"not in {', '.join(tail(f) for f in x['missed'][:2])}.")
    return " ".join(out)


def weigh(w, d: dict | None):
    """Add the history of the copies to a Weight: changing together costs,
    a fix that missed a copy costs more."""
    if not d:
        return w
    return (w.add("copies changed together", d["together"], min(8, 3 * math.log2(1 + d["together"])))
            .add("fixes that missed a copy", d["fixes_apart"], min(12, 5 * d["fixes_apart"])))


def move(d: dict | None) -> list:
    """A first move when a fix missed a copy: check the copies the newest such fix missed."""
    if not d or not d["fixes_apart"]:
        return []
    x = d["fixes"][0]
    return [f"Check {', '.join(tail(f) for f in x['missed'][:3])} first: the bug fix '{x['subject']}' changed "
            f"{', '.join(tail(f) for f in x['changed'][:3])}, not {'this copy' if len(x['missed']) == 1 else 'these'}."]
