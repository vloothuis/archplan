"""boundaries.balance: dependencies whose coupling is out of balance, after
Vlad Khononov's balanced coupling (Balancing Coupling in Software Design,
2024).

Each dependency from a downstream component to an upstream one has three
dimensions:

  strength    how much the downstream knows of the upstream, from most to
              least: intrusive (it writes the upstream's tables, or reads
              them with SQL), functional (the two change together), model
              (its types use the upstream's concrete types, or it queries
              the upstream's tables through their ORM types) and contract
              (it uses the upstream's interfaces or functions only)
  distance    how far apart the two are: the steps through the folder tree
              from one component's path to the other's, plus a step for
              different owners when the cached hotspots name a clear owner
              for both
  volatility  how often the upstream changes: its commits in the cached
              hotspots

Coupling is in balance when strength and distance offset each other, or
when the upstream hardly changes:

  balance = (strength XOR distance) OR NOT volatility

A strong dependency over a long distance on a volatile upstream is out of
balance: every change to the upstream ripples across the codebase, to code
that its authors may not know. This detector reports those dependencies.
The other imbalance, weak coupling at a short distance, is low cohesion; it
costs less and the structure detectors cover it better.

A dependency counts when the model has it, or when the downstream reads or
writes a table the upstream owns (the schema section). The table of every
dependency's dimensions is in the result's extras, under balance."""

from __future__ import annotations

from .common import EXAMPLES, Weight, evidence_file, focus, plural

ID = "boundaries.balance"
NAME = "Balanced coupling"
GROUP = "boundaries"
NEEDS = {"deps", "git"}
LEVELS = ["contract", "model", "functional", "intrusive"]
PARAMS = {"min_strength": "functional", "far": 4, "owner_distance": 2, "owner_share": 0.5, "min_commits": 10,
          "volatile_share": 0.3, "degree": 0.5, "together": 5}
ABSTRACT = ("interface", "module", "function")
TABLE = {"write": ("intrusive", "writes"), "read": ("intrusive", "reads with SQL"),
         "orm": ("model", "queries")}
EFFECT = {"contract": 0, "model": 8, "functional": 14, "intrusive": 20}


def parts(model: dict, cid: str) -> list:
    """A component's path as folder names: its path, else its first match
    pattern without the glob, else its id."""
    v = (model.get("components") or {}).get(cid) or {}
    p = v.get("path") or next((m for m in v.get("match") or [] if isinstance(m, str)), "") or cid
    return [x for x in p.replace("\\", "/").split("/") if x and "*" not in x]


def hops(a: list, b: list) -> int:
    """Steps from one folder to the other through their common parent."""
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return len(a) - n + len(b) - n


def owners(res: dict, share: float) -> dict:
    """{component: author} for components where one author made share or more of the commits."""
    out = {}
    for c, authors in ((res or {}).get("owners") or {}).items():
        if not isinstance(authors, dict) or not authors:
            continue
        total = sum(n for n in authors.values() if isinstance(n, int))
        top = max(authors, key=lambda a: authors[a] if isinstance(authors[a], int) else 0)
        if total and authors[top] / total >= share:
            out[c] = top
    return out


def strengths(ctx, params) -> dict:
    """{(downstream, upstream): {"level", "reasons", "files"}} for each dependency."""
    types = ctx.types
    out = {}

    def at(a, b, level, reason, files=()):
        e = out.setdefault((a, b), {"level": "contract", "reasons": [], "files": []})
        if LEVELS.index(level) > LEVELS.index(e["level"]):
            e["level"] = level
        if (level, reason) not in e["reasons"]:
            e["reasons"].append((level, reason))
        e["files"] += [f for f in files if f and f not in e["files"]]

    for (a, b), e in ctx.graph.items():
        at(a, b, "contract", "depends on it", [evidence_file(x) for x in e["evidence"][:3]])
    # model: a type of a refers to a concrete type of b
    for t in types.values():
        a = (t or {}).get("component")
        refs = [x.get("ref") for x in t.get("fields") or [] if isinstance(x, dict)]
        refs += list(t.get("uses") or []) + list(t.get("implements") or [])
        for r in refs:
            u = types.get(r) if isinstance(r, str) else None
            b = (u or {}).get("component")
            if not b or b == a or a not in ctx.internal or b not in ctx.internal:
                continue
            if u.get("kind") in ABSTRACT or u.get("abstract"):
                at(a, b, "contract", f"uses interface {u.get('name') or r}", [t.get("file")])
            else:
                at(a, b, "model", f"uses type {u.get('name') or r}", [t.get("file")])
    # intrusive: a writes a table b owns, or reads it with SQL; model: a reads it through b's ORM type
    schema = ctx.schema or {}
    maps = schema.get("maps") or {}
    for acc in schema.get("access") or []:
        a, table, mode = acc.get("component"), acc.get("table"), acc.get("mode")
        b = (maps.get(table) or {}).get("component")
        if a and b and a != b and a in ctx.internal and b in ctx.internal and mode in TABLE:
            level, verb = TABLE[mode]
            at(a, b, level, f"{verb} its table {table}", [evidence_file(x) for x in acc.get("evidence") or []])
    # functional: the two change together (last: it only raises a dependency found above)
    for x in (ctx.hotspots or {}).get("pairs") or []:
        if x.get("degree", 0) < params["degree"] or x.get("together", 0) < params["together"]:
            continue
        for a, b in ((x["a"], x["b"]), (x["b"], x["a"])):
            if (a, b) in out:
                at(a, b, "functional", f"changes with it in {x['together']} commits ({round(x['degree'] * 100)}%)")
    for e in out.values():  # strongest first; the plain dependency says nothing once there is more
        rs = sorted(e["reasons"], key=lambda r: -LEVELS.index(r[0]))
        e["reasons"] = [r for _, r in rs if r != "depends on it" or len(rs) == 1]
    return out


def run(ctx, params) -> list:
    if params["min_strength"] not in LEVELS:
        params = {**params, "min_strength": PARAMS["min_strength"]}
    res = ctx.hotspots or {}
    churn = ctx.churn
    counted = sorted(n for c, n in churn.items() if c in ctx.internal)
    # volatile: at least min_commits, and among the volatile_share most changed components
    cut = counted[min(len(counted) - 1, int(len(counted) * (1 - params["volatile_share"])))] if counted else 0
    volatile = lambda c: churn.get(c, 0) >= max(params["min_commits"], cut)
    own = owners(res, params["owner_share"])
    most = max(counted, default=0)
    days = res.get("since_days")
    table, out = [], []
    by_pair = strengths(ctx, params)
    for (a, b), s in sorted(by_pair.items()):
        steps = hops(parts(ctx.model, a), parts(ctx.model, b))
        split = a in own and b in own and own[a] != own[b]
        dist = steps + (params["owner_distance"] if split else 0)
        row = {"from": a, "to": b, "strength": s["level"], "distance": dist, "steps": steps,
               "owners": [own.get(a), own.get(b)] if split else None, "commits": churn.get(b, 0),
               "volatile": volatile(b)}
        far = dist >= params["far"]
        strong = LEVELS.index(s["level"]) >= LEVELS.index(params["min_strength"])
        row["balanced"] = not (strong and far and row["volatile"])
        table.append(row)
        if row["balanced"]:
            continue
        na, nb = ctx.name(a), ctx.name(b)
        rs = s["reasons"]
        why = "; ".join(rs[:4]) + (f"; and {len(rs) - 4} more" if len(rs) > 4 else "")
        apart = f"{steps} folder steps apart" + (", with different owners" if split else "")
        out.append(ctx.finding(
            (a, b),
            f"{na} is coupled to {nb} by {s['level']} coupling across a long distance, and {nb} changes often.",
            f"{na} {why}. "
            f"They are {apart}. {nb} had {plural(churn.get(b, 0), 'commit')} in the last {days} days.",
            "medium", f"{s['level']} strength, distance {dist}, {churn.get(b, 0)} commits upstream",
            [a, b], s["files"],
            {"from": a, "to": b, "strength": s["level"], "reasons": s["reasons"][:EXAMPLES], "distance": dist,
             "steps": steps, "owners": row["owners"], "commits": churn.get(b, 0), "since_days": days,
             "moves": moves(s["level"], na, nb)},
            focus=[focus("components", [a, b], [(a, b)], label="Dependency")],
            weight=Weight(25, "unbalanced coupling")
            .add("strength", s["level"], EFFECT[s["level"]], keep=True)
            .add("distance", dist, min(12, 3 * (dist - params["far"] + 1)))
            .add("volatility: upstream commits", churn.get(b, 0),
                 8 * churn.get(b, 0) / most if most else 0)
            .spread(len(s["reasons"]), "reasons", per=2, cap=6)))
    ctx.extras["balance"] = table
    return out


def moves(level: str, a: str, b: str) -> list:
    weaker = {"intrusive": f"Stop reading {b}'s tables from {a}: let {b} offer the data through its functions.",
              "functional": f"Find the rule {a} and {b} both implement and give it one home, so a change "
                            f"to it changes one component.",
              "model": f"Let {b} publish a small contract for {a}, such as an interface or a data type made "
                       f"for this purpose, instead of its own domain types."}[level]
    return [weaker,
            f"Or bring {a} and {b} closer: into one parent folder, under one owner, or into one component "
            f"if they are one feature.",
            f"If {b}'s churn is a phase that has ended, this coupling is fine: baseline the finding."]
