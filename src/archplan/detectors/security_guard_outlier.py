"""security.guard_outlier: a traced entry point that skips a check its siblings make.

Entry points in the same file, or else the same component, are siblings.
When most of them call the same check on their traced requests and one does
not, that one is the outlier: the others show what the code's own rule is,
so this needs no auth declaration. A check is a declared guard, or, when
`use_names` is on, a function whose name contains one of the fragments in
`names`, in any letter case.

An entry point calls a check when any of its traced requests does: one
request may take an early exit. Entry points declared `auth=none` are
skipped and do not count as siblings. Sequences marked `"guards": "off"`
and requests refused before a sink are left out, as in
security.unguarded_entry. One finding per entry point, with the checks it
skips. It is a candidate: the outlier may be public on purpose."""

from __future__ import annotations

from collections import defaultdict

from .common import Weight, finding, focus, plural
from .security_common import call_file, called, guards, guards_off, matches, name, participant, short, trace_hops
from .security_unguarded_entry import runs, turned_away

ID = "security.guard_outlier"
NAME = "Entry point that skips a check its siblings make"
GROUP = "security"
NEEDS = {"sequences"}
PARAMS = {"min_siblings": 4, "share": 0.75, "use_names": True,
          "names": ["auth", "permi", "allow", "can_", "polic", "guard", "verif", "require", "ensure", "access",
                    "check"]}
RESOLVED = ("A traced request of the entry point calls the check its siblings call, or the entry point is "
            "declared auth=none with a reason.")


def checks(ctx, params, msgs: list, own: str) -> dict:
    """{check key: label} for the checks the request's messages call. A
    declared guard's key is its id; a check by name has (component, name)."""
    declared = guards(ctx)
    parts = [s.lower() for s in params["names"] if s] if params["use_names"] else []
    out = {}
    for m in msgs:
        fn, comp = called(m), participant(ctx, m.get("to"))
        if not fn or fn == own:
            continue
        g = next((g for g in declared if matches(g, fn, comp, call_file(ctx, m))), None)
        if g:
            out[g["id"]] = g["name"]
        elif any(s in fn.lower() for s in parts):
            out[(comp, fn)] = f"{name(ctx, comp)}.{fn}" if comp else fn
    return out


def run(ctx, params) -> list:
    seqs = ctx.model.get("sequences") or {}
    entries = {}  # entry -> {"group", "component", "checks", "sequences", "hops"}
    for entry, d, sid, msgs in runs(ctx):
        if (d and d.get("value") == "none") or guards_off(seqs.get(sid)) or turned_away(ctx, msgs):
            continue
        comp = participant(ctx, msgs[0].get("to"))
        group = (d or {}).get("file") or call_file(ctx, msgs[0]) or comp
        if not group:
            continue
        e = entries.setdefault(entry, {"group": group, "component": comp, "checks": {}, "sequences": [],
                                       "hops": trace_hops(ctx, sid, msgs)})
        e["checks"].update(checks(ctx, params, msgs, short(entry)))
        if sid not in e["sequences"]:
            e["sequences"].append(sid)
    groups = defaultdict(list)
    for entry, e in entries.items():
        groups[e["group"]].append(entry)
    out = []
    for group, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        n = len(members)
        if n < max(2, params["min_siblings"]):
            continue
        users = defaultdict(list)  # check key -> the entries that call it
        for entry in members:
            for k in entries[entry]["checks"]:
                users[k].append(entry)
        common = {k: es for k, es in users.items() if len(es) < n and len(es) / n >= params["share"]}
        for entry in sorted(members):
            e = entries[entry]
            missing = sorted((k for k in common if k not in e["checks"]), key=lambda k: -len(common[k]))
            if not missing:
                continue
            labels = [entries[common[k][0]]["checks"][k] for k in missing]
            most = len(common[missing[0]])
            comps = [e["component"]] if e["component"] else []
            where = group if isinstance(group, str) and "/" in group else None
            out.append(finding(
                ID, (entry,),
                f"{short(entry)} does not call {labels[0]}, which {most} of its {n - 1} siblings call.",
                f"{plural(n, 'traced entry point')} share {group}. {most} of them call {labels[0]} on their "
                f"requests; {short(entry)} does not in {plural(len(e['sequences']), 'sequence')} "
                f"({', '.join(e['sequences'][:3])}{', …' if len(e['sequences']) > 3 else ''})."
                + (f" It also skips {', '.join(labels[1:4])}." if labels[1:] else ""),
                "weak", f"{most} of {n - 1} siblings call {labels[0]}", comps, [where] if where else [],
                {"entry": entry, "group": group, "siblings": n - 1, "missing": labels[:10],
                 "callers": sorted(common[missing[0]])[:10], "sequences": e["sequences"][:20], "hops": e["hops"],
                 "moves": [f"Call {labels[0]} on this path, as its siblings do.",
                           "If the entry point is public on purpose, declare it `auth=none` with a reason."]},
                focus=[focus("sequences", e["sequences"][:20]), focus("components", comps)],
                weight=Weight(40, "sibling consistency").add("share", f"{most} of {n - 1}", 20 * (most / (n - 1)) - 10)
                .spread(len(missing), "checks", per=4, cap=8)))
    return out
