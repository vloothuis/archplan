"""Refactor opportunities for the agent, from what archplan already knows.

The brief groups the new findings (and churn hotspots and misplaced
glossary terms) into opportunities, ranks them by value, and gives each a few
heuristic moves. The agent turns an opportunity into a refactor proposal
(propose_refactor): ops on the target model, which archplan verifies by
recomputing the findings it can compute from the model alone.

Suggestion requests: a thread anchored "suggest" asks the agent for
proposals. The human opens one in the editor, or archplan opens one in auto
mode (config.json "suggest": {"auto", "count"}) after an export that makes
new findings, or when no refactor suggestion is pending any more. At most one
request is open at a time, and archplan opens at most one an hour. Runtime
state is in .archplan/suggest-state.json (git-ignored)."""

from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict

from .findings import (EXAMPLES, Findings, comp_name, cycle_findings, graph, layer_findings,
                       policy_findings, sccs)
from .handover import lock
from .model import edited
from .project import Project, append_ignore, etag, read_json, write_json
from .store import JsonStore, NotFound, Store, find, stamp, who_next
from .suggestions import Suggestions

SEVERITY = {"strong": 3.0, "medium": 2.0, "weak": 1.0}
KIND_WEIGHT = {"structure.tangle": 1.6, "structure.cycle": 1.5, "layers.policy": 1.4, "layers.upward": 1.2, "profile.rule": 1.0,
               "history.coupling": 1.0, "vocabulary.leak": 0.8, "types.fields": 0.8, "types.synonyms": 0.7,
               "profile.pair": 0.5, "hotspot": 0.6, "vocabulary.misplaced": 0.6}
VERIFIED_KINDS = ("structure.cycle", "structure.tangle", "layers.upward", "layers.policy")
TANGLE_KEYS = 5         # a tangle's cheapest cuts group the findings on those edges with it
EFFORTS = ("S", "M", "L")
RISKS = ("low", "med", "high")
HOTSPOTS = 3            # churn-only opportunities at most
MISPLACED = 5           # misplaced-term opportunities at most
REJECTED = 10           # rejected proposals listed in the brief
AUTO_EVERY = 3600       # seconds between automatic requests
STATE = "suggest-state.json"
TITLE_MAX, TEXT_MAX = 200, 4000


def kind_of(fid: str) -> str:
    return fid.rsplit("-", 1)[0]


def opp_id(key: str) -> str:
    return "opp-" + hashlib.sha1(key.encode()).hexdigest()[:10]


# ---------- the model's own findings, for verification ----------

def effective(m: dict) -> dict:
    """m with the component dependencies the editor shows: the recorded ones
    plus those that type relations imply (after move_type, say)."""
    comps, types = m.get("components") or {}, m.get("types") or {}
    deps = {}
    for d in m.get("dependencies") or []:
        a, b = d.get("from"), d.get("to")
        if a in comps and b in comps:
            e = deps.setdefault((a, b), {"from": a, "to": b, "weight": 0, "evidence": []})
            e["weight"] += int(d.get("weight") or 1)
            e["evidence"] += d.get("evidence") or []
    structural = bool(m.get("dependencies"))
    via = defaultdict(int)
    for tid, t in types.items():
        a = t.get("component")
        refs = [("has", f.get("ref")) for f in t.get("fields") or []] + \
            [("implements", r) for r in t.get("implements") or []] + [("uses", r) for r in t.get("uses") or []]
        for kind, r in refs:
            if not r or r == tid or r not in types:
                continue
            b = types[r].get("component")
            if a == b or a not in comps or b not in comps:
                continue
            if structural and kind == "implements" and (a, b) not in deps and not via[(a, b)]:
                continue
            via[(a, b)] += 1
    for k, n in via.items():
        e = deps.setdefault(k, {"from": k[0], "to": k[1], "weight": 0, "evidence": []})
        e["weight"] = max(e["weight"], n)
    return {**m, "dependencies": list(deps.values())}


def profile_of(p: Project) -> dict:
    try:
        from .architecture import Architecture
        st = Architecture(p).state()
        return st.get("profile") or st.get("proposal") or {}
    except Exception:  # no profile: layer findings need one, the rest do not
        return {}


def model_findings(m: dict, prof: dict) -> tuple:
    """({id: finding}, edges, {component: its SCC}): the findings computed
    from the model alone."""
    eff = effective(m)
    fs = cycle_findings(eff) + layer_findings(prof, eff) + policy_findings(eff, m)
    edges = set(graph(eff))
    scc = {c: frozenset(s) for s in sccs({x for e in edges for x in e}, edges) for c in s}
    return {f["id"]: f for f in fs}, edges, scc


def remnant(f: dict, scc: dict) -> frozenset:
    """The largest SCC that the members of tangle f fall into on the other side."""
    parts = [scc.get(c, frozenset([c])) for c in f["evidence"]["members"]]
    return max(parts, key=lambda s: (len(s), sorted(s)), default=frozenset())


def ring_edges(f: dict) -> list:
    return [(e["from"], e["to"]) for e in (f.get("evidence") or {}).get("edges") or []]


def verify(p: Project, ops: list, fixes: list, before: dict | None = None) -> dict:
    """Apply ops to a copy of the target and compare the model-only findings
    on both sides. A cycle counts as fixed when one of its edges is gone, and
    as introduced when one of its edges is new. Fixes of other kinds are
    "claimed": archplan cannot check them from the model."""
    before = before if before is not None else p.target_or_model()
    after, _ = edited(p, ops)
    prof = profile_of(p)
    b, b_edges, b_scc = model_findings(before, prof)
    a, a_edges, a_scc = model_findings(after, prof)
    asis = {}
    if any(i not in b for i in fixes):
        try:
            asis = {f["id"]: f for f in Findings(p).raw()[0]}
        except Exception:
            asis = {}

    shrunk = []

    def gone(f):
        if f["kind"] == "structure.cycle":
            return any(e not in a_edges for e in ring_edges(f))
        if f["kind"] == "structure.tangle":
            rest = len(remnant(f, a_scc))
            if rest <= 1:
                return True
            if rest < f["evidence"]["size"]:
                shrunk.append({"id": f["id"], "from": f["evidence"]["size"], "to": rest})
            return False
        return f["id"] not in a

    def new(f):
        if f["kind"] == "structure.cycle":
            return any(e not in b_edges for e in ring_edges(f))
        if f["kind"] == "structure.tangle":  # new unless most of it was one SCC before
            return len(remnant(f, b_scc) & set(f["evidence"]["members"])) * 2 < f["evidence"]["size"]
        return f["id"] not in b
    fixed = [i for i, f in b.items() if gone(f)]
    for i in fixes:
        if i not in b and i in asis and kind_of(i) in VERIFIED_KINDS and gone(asis[i]):
            fixed.append(i)
    introduced = [f["title"] for f in a.values() if new(f)]
    done = set(fixed) | {x["id"] for x in shrunk}
    return {"fixed": list(dict.fromkeys(fixed))[:EXAMPLES], "introduced": introduced[:EXAMPLES],
            "shrunk": list({x["id"]: x for x in shrunk}.values())[:EXAMPLES],
            "claimed": [i for i in fixes if kind_of(i) not in VERIFIED_KINDS],
            "unverified": [i for i in fixes if kind_of(i) in VERIFIED_KINDS and i not in done]}


# ---------- opportunities ----------

def pair_keys(f: dict) -> list:
    """The component pairs a finding is about; findings that share one are grouped."""
    ev, k = f.get("evidence") or {}, f["kind"]
    if k == "structure.cycle":
        cut = ev.get("cut") or {}
        return [tuple(sorted((cut["from"], cut["to"])))] if cut else []
    if k == "structure.tangle":
        return [tuple(sorted((c["from"], c["to"]))) for c in (ev.get("cuts") or [])[:TANGLE_KEYS]]
    if k == "layers.upward":
        if "from" in ev:  # the first target only: more would chain unrelated groups together
            return [tuple(sorted((ev["from"], ev["to"][0])))] if ev.get("to") else []
        return [("layers", ev.get("from_layer") or "", ev.get("to_layer") or "")]
    if k in ("layers.policy",):
        return [tuple(sorted((ev["from"], ev["to"])))]
    if k == "history.coupling":
        return [tuple(sorted((ev["a"], ev["b"])))]
    if k == "vocabulary.leak" and ev.get("owner"):
        return [tuple(sorted((f["components"][0], ev["owner"])))]
    if k == "profile.rule":
        cs = list(dict.fromkeys(c for c in f["components"] if c))
        return [tuple(sorted(cs[:2]))] if len(cs) >= 2 else []
    return []


def group(findings: list) -> list:
    """Lists of findings that share a component pair."""
    parent = {f["id"]: f["id"] for f in findings}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_key = {}
    for f in findings:
        for k in pair_keys(f):
            if k in by_key:
                parent[root(f["id"])] = root(by_key[k])
            else:
                by_key[k] = f["id"]
    out = defaultdict(list)
    for f in findings:
        out[root(f["id"])].append(f)
    return list(out.values())


def severity(f: dict) -> float:
    """A finding's weight on a 0 to 5 scale. The weight already counts spread,
    centrality and churn; a pseudo-finding without one gets its kind's share."""
    if isinstance(f.get("weight"), (int, float)):
        return f["weight"] / 20
    return SEVERITY.get(f.get("strength"), 1.0) * KIND_WEIGHT.get(f["kind"], 0.6)


def group_key(fs: list) -> str:
    """Stable while the group's main finding stays: its cycle, its pair, or its id."""
    for f in fs:
        if f["kind"] == "structure.tangle":
            return "tangle:" + f["evidence"]["hub"]
    for f in fs:
        if f["kind"] == "structure.cycle":
            return "cycle:" + "|".join(sorted(f["evidence"]["cycle"]))
    keys = sorted(k for f in fs for k in pair_keys(f))
    if keys:
        return "pair:" + "|".join(keys[0])
    return "finding:" + min(f["id"] for f in fs)


def type_refs(model: dict, a: str, b: str) -> list:
    """Types of component b that types of component a refer to."""
    types = model.get("types") or {}
    out = []
    for tid, t in types.items():
        if t.get("component") != a:
            continue
        refs = [f.get("ref") for f in t.get("fields") or []] + list(t.get("uses") or []) + list(t.get("implements") or [])
        out += [r for r in refs if r in types and types[r].get("component") == b]
    return list(dict.fromkeys(out))


def moves(f: dict, model: dict) -> list:
    """One to three heuristic hints for the main finding of an opportunity."""
    k, ev = f["kind"], f.get("evidence") or {}
    n = lambda c: comp_name(model, c)
    tn = lambda t: (model.get("types") or {}).get(t, {}).get("name") or t
    if k == "structure.cycle":
        a, b, w = ev["cut"]["from"], ev["cut"]["to"], ev["cut"]["weight"]
        refs = type_refs(model, a, b)
        out = []
        if 0 < len(refs) <= 3:
            out.append(f"Cut {n(a)} → {n(b)} ({w} references): move {', '.join(map(tn, refs))} from {n(b)} to "
                       f"{n(a)} (move_type), then remove_dependency {a} → {b}.")
        out.append(f"Invert {n(a)} → {n(b)}: define the interface {n(a)} needs in {n(a)}, let {n(b)} implement it, "
                   f"then remove_dependency {a} → {b}.")
        if len(out) < 2 or len(ev.get("cycle") or []) > 2:
            out.append(f"Extract what {n(a)} uses of {n(b)} into a new component both may depend on "
                       "(add_component, move_type).")
        return out[:3]
    if k == "structure.tangle":
        out = []
        for c in (ev.get("cuts") or [])[:3]:
            a, b = c["from"], c["to"]
            site = f" ({c['evidence'][0]})" if c.get("evidence") else ""
            out.append(f"Cut {n(a)} → {n(b)}, {c['weight']} reference{'s' if c['weight'] != 1 else ''}{site}: "
                       f"invert it or move what {n(a)} uses into {n(a)}; remove_dependency {a} → {b}.")
        return out
    if k == "layers.upward":
        if "from" not in ev:
            return [f"Reconsider the layer order: {ev.get('from_layer')} may belong above {ev.get('to_layer')} "
                    "(a profile change, not a target op).",
                    "Or invert the upward dependencies one by one with interfaces in the lower layer."]
        a, ts = ev["from"], ev.get("to") or []
        t = ts[0] if ts else "?"
        return [f"Invert {n(a)} → {n(t)}: put the abstraction {n(a)} needs in {ev.get('from_layer')} and let "
                f"{n(t)} implement it; remove_dependency {a} → {t}.",
                f"Or move the part of {n(a)} that needs {n(t)} up into {ev.get('to_layer')} (move_type).",
                f"Then forbid {a} → {t} so it stays fixed."]
    if k == "layers.policy":
        a, b = ev["from"], ev["to"]
        refs = type_refs(model, a, b)
        out = [f"Remove {n(a)} → {n(b)}: route the call through a component the policy lets {n(a)} use "
               f"(remove_dependency {a} → {b})."]
        if 0 < len(refs) <= 3:
            out.append(f"Or move {', '.join(map(tn, refs))} out of {n(b)} into {n(a)} or a shared component (move_type).")
        out.append(f"If the dependency is intended, permit {a} → {b} instead.")
        return out
    if k == "profile.rule":
        return [f"Route the references through the public API the rule allows: {ev.get('text')}.",
                "Or move the referencing code into a role that may make the call."]
    if k == "profile.pair":
        return ["Add the missing partner file, or narrow the profile rule if the pair is optional."]
    if k == "types.fields":
        gs = list((ev.get("groups") or {}).keys())[:3]
        out = []
        if gs:
            out.append(f"Split {ev.get('name')} by its field groups: " + ", ".join(f"{g}_*" for g in gs)
                       + " become their own types.")
        if len(ev.get("flags") or []) >= 3:
            out.append(f"Replace the boolean flags of {ev.get('name')} with one status field.")
        return out or [f"Split {ev.get('name')} into smaller types by responsibility."]
    if k == "types.synonyms":
        ts = ev.get("types") or []
        return [f"Merge {', '.join(map(tn, ts))} into one type owned by one component (move_type, then update users).",
                "If they mean different things, rename one so the difference shows (rename_type)."]
    if k == "history.coupling":
        a, b = ev["a"], ev["b"]
        return [f"Extract what {n(a)} and {n(b)} both change into one component (add_component, move_type).",
                f"Or merge {n(a)} and {n(b)} if they are one feature.",
                "Look for a duplicated rule or data format first."]
    if k == "vocabulary.leak":
        c, k_ = f["components"][0], ev.get("concept")
        return [f"Rename the term in {n(c)}, or route through {ev.get('owner')}'s API.",
                f"Or move the concept {k_} to {n(c)} if it is the real owner (set_concept owner).",
                f"Or allow it: add {k_} to the uses of {c} (set_glossary)."]
    if k == "hotspot":
        c = f["components"][0]
        return [f"Find the part of {n(c)} that changes for a different reason and extract it (add_component, move_type)."]
    if k == "vocabulary.misplaced":
        c, t = f["components"][0], ev.get("type")
        return [f"Move {tn(t)} to {n(c)}, which uses the word most (move_type).",
                f"Or rename the term in {n(c)} if it means something else there."]
    return [m for m in ev.get("moves") or [] if isinstance(m, str)][:3]  # detectors may suggest their own


def facts(f: dict, model: dict) -> dict:
    """Compact evidence for the main finding."""
    ev, k = f.get("evidence") or {}, f["kind"]
    if k == "structure.cycle":
        cut = ev["cut"]
        return {"cycle": ev["cycle"], "cut": f"{cut['from']} → {cut['to']}", "cut_weight": cut["weight"],
                "sites": cut.get("evidence", [])[:3], "tangle": ev.get("tangle")}
    if k == "structure.tangle":
        return {"hub": ev["hub"], "size": ev["size"], "cycles": ev.get("cycles"),
                "cuts": [f"{c['from']} → {c['to']} ({c['weight']})" for c in (ev.get("cuts") or [])[:5]],
                "breaks_all": f"{ev.get('cut_count')} cuts, {ev.get('cut_weight')} references",
                "two_cycles": [f"{x['a']} ↔ {x['b']} {x['weights']}" for x in (ev.get("two_cycles") or [])[:3]]}
    if k == "layers.upward":
        return {x: ev[x] for x in ("from", "to", "weight", "from_layer", "to_layer", "upward", "down") if x in ev} | \
            ({"sites": ev["evidence"][:3]} if ev.get("evidence") else {})
    if k == "layers.policy":
        return {"edge": f"{ev['from']} → {ev['to']}", "weight": ev["weight"], "sites": ev["evidence"][:3]}
    if k in ("profile.rule", "profile.pair"):
        return {"rule": ev.get("text"), "count": ev.get("count"),
                "breaks": [f"{b['from']} -> {b.get('to') or '?'}" for b in (ev.get("breaks") or [])[:3]]}
    if k == "types.fields":
        return {"type": ev.get("type"), "fields": ev.get("fields"),
                "groups": {g: len(v) for g, v in list((ev.get("groups") or {}).items())[:4]},
                "flags": len(ev.get("flags") or [])}
    if k == "types.synonyms":
        return {"types": ev.get("types"), "shared": ev.get("shared", [])[:8]}
    if k == "history.coupling":
        return {x: ev.get(x) for x in ("a", "b", "together", "degree")}
    if k == "vocabulary.leak":
        return {x: ev.get(x) for x in ("concept", "owner", "why", "count") if ev.get(x) is not None} | \
            ({"sites": f["files"][:3]} if f.get("files") else {})
    return {x: v for x, v in ev.items() if isinstance(v, (str, int, float))}


def extra_findings(p: Project, model: dict, churn: dict, covered: set) -> list:
    """Pseudo-findings for the brief: churn hotspots and misplaced glossary terms."""
    out = []
    if churn:
        for c, n in sorted(churn.items(), key=lambda kv: -kv[1])[:HOTSPOTS]:
            if c in covered or c not in (model.get("components") or {}):
                continue
            out.append({"id": "hotspot-" + hashlib.sha1(c.encode()).hexdigest()[:12], "kind": "hotspot",
                        "group": "history", "title": f"{comp_name(model, c)} changed in {n} commits, more than "
                        "any other component.", "strength": "medium", "components": [c], "files": [],
                        "evidence": {"component": c, "commits": n}})
    try:
        from .vocab import Vocab, words
        res = Vocab(p).candidates(None, 10)
    except Exception:  # no sources to read: skip the glossary signal
        return out
    types = model.get("types") or {}
    for c, x in res["components"].items():
        for t in x["terms"]:
            key = t["term"].replace(" ", "")
            own = [tid for tid in t["types"] if "".join(words(types[tid].get("name") or tid)) == key]
            if not own or any(types[tid].get("component") == c for tid in own):
                continue
            d = types[own[0]].get("component")
            theirs = next((o["count"] for o in t["others"] if o["component"] == d), 0)
            if not d or t["owner_guess"] != c or theirs * 2 > t["counts"]["ident"]:
                continue
            out.append({"id": "vocabulary.misplaced-" + hashlib.sha1(f"{c}|{own[0]}".encode()).hexdigest()[:12],
                        "kind": "vocabulary.misplaced", "group": "vocabulary",
                        "title": f"{comp_name(model, c)} uses \"{t['term']}\" most, but the type "
                                 f"{types[own[0]].get('name')} lives in {comp_name(model, d)}.",
                        "strength": "weak", "components": [c, d], "files": [],
                        "evidence": {"type": own[0], "term": t["term"], "uses_here": t["counts"]["ident"],
                                     "uses_owner": theirs, "samples": t["samples"][:3]}})
    out.sort(key=lambda f: (f["kind"] != "hotspot", f["id"]))
    return [f for f in out if f["kind"] == "hotspot"] + [f for f in out if f["kind"] != "hotspot"][:MISPLACED]


def opportunities(p: Project, store: Store | None = None) -> dict:
    """{"opportunities": [...] ranked, "excluded", "rejected_before", "missing"}."""
    store = store or Store(p.dir)
    fl = Findings(p)
    listing = fl.listing(store)
    model = fl.arch.model()
    new = [f for f in listing["findings"] if f["state"] == "new"]
    hot = (read_json(p.path("hotspots.json")) or {}).get("result") or {}
    churn = {c: x.get("commits", 0) for c, x in (hot.get("components") or {}).items()}
    covered = {c for f in new for c in f["components"]}
    new += extra_findings(p, model, churn, covered)
    fan = defaultdict(set)
    for a, b in graph(model):
        fan[b].add(a)
    max_churn = max(churn.values(), default=0)
    max_fan = max((len(v) for v in fan.values()), default=0)
    sgs = [s for s in Suggestions(p.dir).read()["suggestions"] if s.get("kind") == "refactor"]
    active = [s for s in sgs if s["status"] != "rejected"]
    rejected = [s for s in sgs if s["status"] == "rejected"]
    busy_opps = {s.get("opportunity") for s in active if s.get("opportunity")}
    busy_ids = {i for s in active for i in s.get("fixes") or []}
    out, excluded = [], 0
    for fs in group(new):
        fs.sort(key=lambda f: (-severity(f), f["id"]))
        main = fs[0]
        oid = opp_id(group_key(fs))
        ids = [f["id"] for f in fs]
        if oid in busy_opps or busy_ids & set(ids):
            excluded += 1
            continue
        comps = list(dict.fromkeys(c for f in fs for c in f["components"] if c))
        sev = severity(main) + 0.3 * sum(severity(f) for f in fs[1:])
        sev = min(sev, 2 * severity(main))
        ch = max((churn.get(c, 0) for c in comps), default=0)
        fi = max((len(fan.get(c, ())) for c in comps), default=0)
        cf = 1 + (math.log1p(ch) / math.log1p(max_churn) if max_churn else 0)
        ff = 1 + (math.log1p(fi) / math.log1p(max_fan) if max_fan else 0)
        if "weight" in main:  # the weight counts churn and fan-in already
            cf = ff = 1
        o = {"id": oid, "kind": main["kind"], "title": main["title"] + (f" (+{len(fs) - 1} related)" if len(fs) > 1 else ""),
             "score": round(sev * cf * ff, 2), "components": comps[:6],
             **({"weight": main["weight"]} if "weight" in main else {}),
             "findings": [i for i in ids if not i.startswith(("hotspot-", "vocabulary.misplaced-"))],
             "facts": facts(main, model),
             "related": [{"id": f["id"], "kind": f["kind"], "title": f["title"]} for f in fs[1:4]],
             "churn": {c: churn[c] for c in comps[:4] if churn.get(c)},
             "fan_in": {c: len(fan[c]) for c in comps[:4] if fan.get(c)},
             "moves": moves(main, model)}
        rej = [f"{s.get('title') or s.get('note')}: {s.get('reason') or 'no reason'}" for s in rejected
               if s.get("opportunity") == oid or set(s.get("fixes") or []) & set(ids)]
        if rej:
            o["rejected"] = rej[:3]
        out.append(o)
    out = diverse(out)
    rej = [{"title": s.get("title") or s.get("note") or s["id"], "reason": s.get("reason") or "",
            **({"opportunity": s["opportunity"]} if s.get("opportunity") else {}),
            **({"fixes": s["fixes"][:5]} if s.get("fixes") else {})}
           for s in sorted(rejected, key=lambda s: s.get("decided") or "", reverse=True)[:REJECTED]]
    return {"opportunities": out, "excluded": excluded, "rejected_before": rej, "missing": listing["missing"],
            "new_findings": sum(1 for f in listing["findings"] if f["state"] == "new")}


def diverse(opps: list, decay: float = 0.7) -> list:
    """Order by score, but let each further opportunity of a kind count for
    less, so one kind (cycles, typically) does not fill the whole brief."""
    left = sorted(opps, key=lambda o: (-o["score"], o["id"]))
    seen, out = defaultdict(int), []
    while left:
        best = max(range(len(left)), key=lambda i: (left[i]["score"] * decay ** seen[left[i]["kind"]], -i))
        o = left.pop(best)
        seen[o["kind"]] += 1
        out.append(o)
    return out


def brief(p: Project, top: int = 8, focus: list | None = None, kinds: list | None = None,
          store: Store | None = None) -> dict:
    """The compact form for get_refactor_brief."""
    if focus is not None and (not isinstance(focus, list) or not all(isinstance(c, str) for c in focus)):
        raise ValueError("focus must be a list of component ids")
    if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
        raise ValueError("kinds must be a list of finding kinds or groups")
    res = opportunities(p, store)
    os_ = res["opportunities"]
    if focus:
        os_ = [o for o in os_ if set(o["components"]) & set(focus)]
    if kinds:
        hit = lambda k: any(k == x or k.split(".")[0] == x for x in kinds)
        os_ = [o for o in os_ if hit(o["kind"]) or any(hit(r["kind"]) for r in o["related"])]
    top = max(1, min(int(top or 8), 30))
    return {"opportunities": os_[:top], "shown": min(top, len(os_)), "of": len(os_),
            "excluded_with_pending_proposal": res["excluded"], "new_findings": res["new_findings"],
            "rejected_before": res["rejected_before"],
            "missing": [m["kind"] for m in res["missing"]],
            "next": "Read the code around the top opportunities, then propose_refactor: one concern per proposal, "
                    "with ops on the target and the finding ids it fixes. Do not repeat a rejected idea."}


# ---------- proposals ----------

def check_proposal(p: Project, title, rationale, ops, fixes, opportunity, effort, risk) -> tuple:
    """Validated (title, rationale, fixes, effort, risk)."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("rationale must name the evidence")
    if not isinstance(ops, list) or not ops:
        raise ValueError("ops must be a non-empty list")
    fixes = fixes or []
    if not isinstance(fixes, list) or not all(isinstance(i, str) for i in fixes):
        raise ValueError("fixes must be a list of finding ids")
    if fixes:
        known = {f["id"] for f in Findings(p).raw()[0]}
        bad = [i for i in fixes if i not in known]
        if bad:
            raise ValueError(f"no current finding {bad[0]}; take the ids from get_refactor_brief")
    if opportunity is not None and (not isinstance(opportunity, str) or not opportunity.startswith("opp-")):
        raise ValueError("opportunity must be an id from get_refactor_brief (opp-...)")
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    if risk not in RISKS:
        raise ValueError(f"risk must be one of {', '.join(RISKS)}")
    return title.strip()[:TITLE_MAX], rationale.strip()[:TEXT_MAX], list(dict.fromkeys(fixes)), effort, risk


def propose_refactor(p: Project, title, rationale, ops, fixes=None, opportunity=None, effort="M", risk="med",
                     thread=None) -> dict:
    title, rationale, fixes, effort, risk = check_proposal(p, title, rationale, ops, fixes, opportunity, effort, risk)
    before = p.target_or_model()
    if not before:
        raise ValueError("no model yet; run the export first (refresh_model)")
    v = verify(p, ops, fixes, before)  # raises ValueError when an op does not apply
    sg = Suggestions(p.dir).create(ops, title, thread, kind="refactor", title=title, rationale=rationale,
                                   fixes=fixes, verified=v, effort=effort, risk=risk, opportunity=opportunity)
    return sg


# ---------- plan and discuss ----------

def suggestion_anchor(sid: str) -> str:
    return "suggestion:" + sid


def thread_on(store: Store, sid: str) -> dict | None:
    a = suggestion_anchor(sid)
    return next((t for t in store.read()["threads"] if t["anchor"] == a), None)


def proposal_text(sg: dict) -> str:
    title = sg.get("title") or sg.get("note") or sg["id"]
    lines = [f"**{title}**", "", sg.get("rationale") or sg.get("note") or ""]
    if sg.get("fixes"):
        lines += ["", "Fixes: " + ", ".join(f"@[{i}](finding:{i})" for i in sg["fixes"][:10])]
    v = sg.get("verified") or {}
    if v.get("introduced"):
        lines += ["", "Introduces: " + "; ".join(v["introduced"][:5])]
    return "\n".join(lines).strip()


def plan(p: Project, store: Store, sid: str, indices: list | None = None) -> dict:
    """Accept a suggestion into the target and lock a thread holding its title
    and rationale as a planned refactor."""
    sugs = Suggestions(p.dir)
    sg = sugs.get(sid)
    title = (sg.get("title") or sg.get("note") or sid)[:TITLE_MAX]
    sg = sugs.decide(p, sid, indices)
    th = thread_on(store, sid)
    if th is None:
        th = store.create(suggestion_anchor(sid), title, proposal_text(sg), "agent")
    if th["status"] != "planned":
        th = lock(store, th["id"], title, (sg.get("rationale") or sg.get("note") or "")[:TEXT_MAX])
    sugs.update(lambda d: find_sg(d, sid).__setitem__("plan_thread", th["id"]))
    return {"suggestion": sugs.get(sid), "thread": th}


def discuss(p: Project, store: Store, sid: str, text: str | None = None) -> tuple:
    """(thread, created): the thread on suggestion:<id>, without deciding."""
    sg = Suggestions(p.dir).get(sid)
    th = thread_on(store, sid)
    if th:
        return th, False
    title = (sg.get("title") or sg.get("note") or sid)[:TITLE_MAX]
    body = (text or "").strip() if isinstance(text, str) else ""
    return store.create(suggestion_anchor(sid), title, body or f"Let's discuss this proposal.\n\n{proposal_text(sg)}",
                        "human"), True


def find_sg(d: dict, sid: str) -> dict:
    for sg in d["suggestions"]:
        if sg["id"] == sid:
            return sg
    raise NotFound(f"no suggestion {sid}")


# ---------- requests and auto mode ----------

def settings(p: Project) -> dict:
    cfg = read_json(p.path("config.json"), {}) or {}
    s = cfg.get("suggest") if isinstance(cfg.get("suggest"), dict) else {}
    count = s.get("count")
    return {"auto": s.get("auto") is True,
            "count": count if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 10 else 3}


def set_auto(p: Project, on, count=None) -> dict:
    if not isinstance(on, bool):
        raise ValueError("on must be true or false")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10):
        raise ValueError("count must be a number from 1 to 10")
    path = p.path("config.json")
    cfg = read_json(path, {}) or {}
    s = cfg.get("suggest") if isinstance(cfg.get("suggest"), dict) else {}
    cfg["suggest"] = {**s, "auto": on, **({"count": count} if count is not None else {})}
    write_json(path, cfg)
    p.config = cfg
    return settings(p)


class SuggestState(JsonStore):
    name = "suggest-state"
    empty = {"model_etag": None, "seen": [], "pending": 0, "last_at": None}


def open_request(store: Store) -> dict | None:
    """The request thread that still waits on the agent."""
    return next((t for t in reversed(store.read()["threads"])
                 if t["anchor"] == "suggest" and t["status"] == "open" and who_next(t) == "agent"), None)


def request_text(count: int, focus=None, kinds=None, note=None, why: str | None = None) -> str:
    where = (" for " + ", ".join(f"@[{c}](comp:{c})" for c in focus[:20])) if focus else ""
    which = f" Only {', '.join(kinds[:10])}." if kinds else ""
    text = (f"Please propose up to {count} refactors{where}.{which} Call get_refactor_brief"
            + (f"(focus={list(focus[:20])})" if focus else "") + ", read the code around the top opportunities, and "
            "send each as propose_refactor: small, one concern each, with ops on the target, the finding ids it "
            "fixes, effort and risk. Pass this thread's id as thread. Do not repeat ideas under rejected_before. "
            "Then reply here with a short list.")
    if why:
        text = why + "\n\n" + text
    if note:
        text += "\n\n" + str(note)[:2000]
    return text


def request(p: Project, store: Store, count=None, focus=None, kinds=None, note=None, author: str = "human",
            why: str | None = None) -> dict:
    """Open a request thread anchored "suggest"."""
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 10):
        raise ValueError("count must be a number from 1 to 10")
    if focus is not None:
        if not isinstance(focus, list) or not all(isinstance(c, str) for c in focus):
            raise ValueError("focus must be a list of component ids")
        comps = (read_json(p.model_path) or {}).get("components") or {}
        bad = [c for c in focus if c not in comps]
        if bad:
            raise NotFound(f"no component {bad[0]}")
    if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)):
        raise ValueError("kinds must be a list of finding kinds or groups")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string")
    th = store.create("suggest", "Suggest refactors", request_text(count or settings(p)["count"], focus, kinds, note,
                                                                   why), author)
    SuggestState(p.dir).update(lambda d: d.__setitem__("last_at", time.time()))
    return th


def pending_refactors(p: Project) -> int:
    return sum(1 for s in Suggestions(p.dir).read()["suggestions"]
               if s.get("kind") == "refactor" and s["status"] == "pending")


def status(p: Project, store: Store) -> dict:
    s = settings(p)
    st = SuggestState(p.dir).read()
    o = open_request(store)
    last = st.get("last_at")
    return {**s, "open_request": o["id"] if o else None,
            "last_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last)) if isinstance(last, (int, float)) else None}


def auto_tick(p: Project, store: Store | None = None, now: float | None = None, enabled: bool = False) -> dict | None:
    """Open a request in auto mode when the model changed and made new
    findings, or when the pending refactor suggestions dropped to zero (or
    auto mode was just switched on with none pending). Returns the thread it
    opened, if any. Cheap when nothing changed: two etags and one file read."""
    store = store or Store(p.dir)
    now = time.time() if now is None else now
    s = settings(p)
    ss = SuggestState(p.dir)
    ensure_ignored(p)

    def fn(d):
        why = None
        me = etag(p.model_path)
        if me and me != d.get("model_etag"):
            try:
                ids = sorted(f["id"] for f in Findings(p).listing(store)["findings"] if f["state"] == "new")
            except Exception:
                ids = list(d.get("seen") or [])
            fresh = set(ids) - set(d.get("seen") or [])
            if fresh and d.get("model_etag"):
                why = f"The model changed and has {len(fresh)} new finding{'s' if len(fresh) != 1 else ''}."
            d["model_etag"], d["seen"] = me, ids
        n = pending_refactors(p)
        if (d.get("pending") or 0) > 0 and n == 0:
            why = why or "No refactor suggestion is pending any more."
        if enabled and n == 0:
            why = why or "Auto mode is on."
        d["pending"] = n
        if not (why and s["auto"]) or open_request(store):
            return None
        last = d.get("last_at")
        if isinstance(last, (int, float)) and now - last < AUTO_EVERY:
            return None
        d["last_at"] = now
        return store.create("suggest", "Suggest refactors", request_text(s["count"], why=why), "archplan")
    return ss.update(fn)


def ensure_ignored(p: Project) -> None:
    append_ignore(p, STATE)
