"""The ratchet: a few project-level numbers that may only get better.

The numbers live in the committed baseline, next to the accepted and
dismissed findings:

  "ratchet": {"metrics": {name: number}, "min_weight": N, "at": time,
              "log": [{"at", "change", "reason", "metrics": {name: [old, new]}}]}

`archplan check` measures them again and fails when one got worse than the
baseline, beyond its tolerance ("check": {"tolerance": {name: number}} in
config.json). When one got better, check says so; `archplan check --tighten`
(and --update-baseline) writes the better values. Only
`archplan check --loosen REASON` writes worse values, and the log keeps the
reason.

The metrics, all lower is better:

  weight.<group>    the sum of the weights of the open findings (new and
                    accepted, not dismissed) in a group, at or above the
                    minimum weight. Churn is left out of the weight, and the
                    history and evolution groups are left out, so the numbers
                    do not depend on the git history.
  largest_tangle    components in the largest group that all reach each other
                    (0 without cycles)
  cycle_edges       dependencies between two components of such a group
  upward_edges      dependencies from a lower layer to a higher one (needs
                    layers in the profile)
  propagation_cost  the share of ordered component pairs (a, b) where a
                    reaches b through dependencies, a reaching itself
                    included, in percent (MacCormack et al., as in DV8)

All of them come from the model and the profile, so they are the same on
every machine with the same model."""

from __future__ import annotations

import math
from collections import defaultdict

from .detectors.common import GROUPS, clamp, graph, namer
from .detectors.cycles import sccs
from .project import read_json
from .store import stamp

GIT_GROUPS = ("history", "evolution")   # need the git history: not in the ratchet
WEIGHT_GROUPS = [g for g in GROUPS if g not in GIT_GROUPS]
GRAPH = {
    "largest_tangle": "components in the largest tangle",
    "cycle_edges": "dependencies inside cycles",
    "upward_edges": "upward layer dependencies",
    "propagation_cost": "propagation cost (% of component pairs reachable)",
}
NAMES = [f"weight.{g}" for g in WEIGHT_GROUPS] + list(GRAPH)
HIGHER_IS_BETTER: set = set()           # every metric now is lower is better
LOG_MAX = 20
CAUSES = 5                              # findings or components named per worse metric
VALUE_MAX = 10 ** 9


def label(name: str) -> str:
    if name.startswith("weight."):
        return f"weight of open {name[7:]} findings"
    return GRAPH.get(name, name)


# ---------- validation: the baseline is untrusted repo data ----------

def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x < VALUE_MAX


def _clip(x, n: int) -> str | None:
    return None if x is None else str(x)[:n]


def clean(d) -> dict | None:
    """The ratchet section, read as untrusted data: only known metric names
    with finite numbers from 0, a minimum weight from 0 to 100, short strings
    and at most LOG_MAX log entries. None when there is nothing usable."""
    if not isinstance(d, dict):
        return None
    ms = d.get("metrics")
    metrics = {k: v for k, v in (ms if isinstance(ms, dict) else {}).items() if k in NAMES and _num(v)}
    if not metrics:
        return None
    mw = d.get("min_weight")
    out = {"metrics": {k: metrics[k] for k in NAMES if k in metrics},
           "min_weight": int(mw) if _num(mw) and mw <= 100 else 0, "at": _clip(d.get("at"), 40), "log": []}
    for e in (d.get("log") if isinstance(d.get("log"), list) else [])[-LOG_MAX:]:
        if not isinstance(e, dict) or e.get("change") not in ("start", "tighten", "loosen"):
            continue
        em = e.get("metrics")
        em = {k: [v[0], v[1]] for k, v in (em if isinstance(em, dict) else {}).items()
              if k in NAMES and isinstance(v, list) and len(v) == 2 and all(x is None or _num(x) for x in v)}
        out["log"].append({"at": _clip(e.get("at"), 40), "change": e["change"],
                           "reason": _clip(e.get("reason"), 500), "metrics": em})
    return out


def tolerances(p) -> dict:
    """{metric: tolerance} from "check": {"tolerance": {...}} in config.json;
    unknown names and bad numbers are left out."""
    c = (read_json(p.path("config.json"), {}) or {}).get("check")
    t = c.get("tolerance") if isinstance(c, dict) else None
    return {k: v for k, v in (t if isinstance(t, dict) else {}).items() if k in NAMES and _num(v)}


# ---------- measuring ----------

def plain_weight(f: dict) -> int:
    """A finding's weight without churn, which comes from the git history."""
    basis = f.get("weight_basis")
    if not basis:
        return int(f.get("weight") or 0)
    return clamp(sum(b.get("effect") or 0 for b in basis if not str(b.get("factor", "")).startswith("churn")))


def reach(model: dict) -> tuple:
    """(internal components, {component: set of components it reaches}), itself included."""
    comps = model.get("components") or {}
    nodes = sorted(c for c, v in comps.items() if (v or {}).get("kind") != "external")
    adj = defaultdict(list)
    for a, b in graph(model):
        adj[a].append(b)
    out = {}
    for c in nodes:
        seen, todo = {c}, [c]
        while todo:
            for w in adj[todo.pop()]:
                if w not in seen:
                    seen.add(w)
                    todo.append(w)
        out[c] = seen
    return nodes, out


def graph_metrics(model: dict) -> dict:
    nodes, r = reach(model)
    if not nodes:
        return {}
    g = graph(model)
    groups = [s for s in sccs(nodes, list(g)) if len(s) > 1]
    where = {c: i for i, s in enumerate(groups) for c in s}
    return {"largest_tangle": max((len(s) for s in groups), default=0),
            "cycle_edges": sum(1 for a, b in g if a in where and where.get(b) == where[a]),
            "propagation_cost": round(100 * sum(len(x) for x in r.values()) / len(nodes) ** 2, 1)}


def measure(listing: dict, model: dict, min_weight: int = 0) -> dict:
    """{metric: number} now. upward_edges only when the layers detector ran."""
    out = {f"weight.{g}": 0 for g in WEIGHT_GROUPS}
    upward, layers_ran = 0, not any(m.get("kind") == "layers.upward" for m in listing.get("missing") or [])
    for f in listing["findings"]:
        if f["kind"] == "layers.upward":
            ev = f.get("evidence") or {}
            upward += ev["upward"] if isinstance(ev.get("upward"), int) else len(ev.get("to") or []) or 1
        if f.get("state") == "dismissed" or f["group"] not in WEIGHT_GROUPS:
            continue
        w = plain_weight(f)
        if w >= min_weight:
            out[f"weight.{f['group']}"] += w
    out.update(graph_metrics(model))
    if layers_ran:
        out["upward_edges"] = upward
    return {k: out[k] for k in NAMES if k in out}


# ---------- comparing ----------

def worse(name: str, old, new, tol: float = 0) -> bool:
    return new < old - tol if name in HIGHER_IS_BETTER else new > old + tol


def better(name: str, old, new) -> bool:
    return new > old if name in HIGHER_IS_BETTER else new < old


def compare(stored: dict | None, now: dict, tol: dict, min_weight: int) -> dict:
    """{"stored", "at", "min_weight", "rows": [{metric, label, old, new, status, tolerance?, note?}],
    "worse", "better"}. status: worse, better, same (within tolerance too),
    new (not in the ratchet yet), gone (not measured now) or skipped."""
    rows = []
    old = (stored or {}).get("metrics") or {}
    mw_changed = stored is not None and stored.get("min_weight", 0) != min_weight
    for k in NAMES:
        if k not in old and k not in now:
            continue
        row = {"metric": k, "label": label(k), "old": old.get(k), "new": now.get(k)}
        if k in tol:
            row["tolerance"] = tol[k]
        if k not in now:
            row["status"] = "gone"
        elif k not in old:
            row["status"] = "new"
        elif mw_changed and k.startswith("weight."):
            row["status"] = "skipped"
            row["note"] = (f"the minimum weight is {min_weight}, the ratchet was set at {stored.get('min_weight', 0)}; "
                           "`archplan check --loosen REASON` sets it again")
        elif worse(k, old[k], now[k], tol.get(k, 0)):
            row["status"] = "worse"
        elif better(k, old[k], now[k]):
            row["status"] = "better"
        else:
            row["status"] = "same"
        rows.append(row)
    return {"stored": stored is not None, "at": (stored or {}).get("at"),
            "min_weight": (stored or {}).get("min_weight", min_weight), "rows": rows,
            "worse": sum(1 for r in rows if r["status"] == "worse"),
            "better": sum(1 for r in rows if r["status"] == "better")}


def causes(metric: str, listing: dict, model: dict, min_weight: int = 0) -> list:
    """Up to CAUSES short lines on what makes a metric what it is: the
    findings behind it, new ones first, then by weight; for the propagation
    cost the components that reach the most others."""
    if metric == "propagation_cost":
        nodes, r = reach(model)
        name = namer(model)
        top = sorted(nodes, key=lambda c: (-len(r[c]), c))[:CAUSES]
        return [{"component": c, "text": f"{name(c)} reaches {len(r[c]) - 1} of {len(nodes) - 1} components"}
                for c in top if len(r[c]) > 1]
    if metric.startswith("weight."):
        pick = lambda f: (f["group"] == metric[7:] and f.get("state") != "dismissed"
                          and plain_weight(f) >= min_weight)
    elif metric == "upward_edges":
        pick = lambda f: f["kind"] == "layers.upward"
    else:
        pick = lambda f: f["kind"] in ("structure.tangle", "structure.cycle")
    fs = sorted((f for f in listing["findings"] if pick(f)),
                key=lambda f: (f.get("state") != "new", -plain_weight(f)))
    return [{"id": f["id"], "state": f.get("state"), "weight": plain_weight(f), "text": f["title"]}
            for f in fs[:CAUSES]]


def status(listing: dict, model: dict, stored: dict | None, tol: dict, min_weight: int) -> dict:
    """compare() with the current values, and the causes of each worse metric."""
    now = measure(listing, model, min_weight)
    out = compare(stored, now, tol, min_weight)
    for r in out["rows"]:
        if r["status"] == "worse":
            r["causes"] = causes(r["metric"], listing, model, min_weight)
    out["metrics"] = now
    return out


# ---------- writing ----------

def _log(d: dict, change: str, reason: str | None, changed: dict) -> None:
    d["log"] = (d.get("log") or [])[-(LOG_MAX - 1):] + [
        {"at": stamp(), "change": change, "reason": reason, "metrics": changed}]


def tighten(baseline, now: dict, min_weight: int) -> dict:
    """Write the metrics that got better or are not in the ratchet yet; start
    the ratchet when there is none. Never writes a worse value. Weight metrics
    stay as they are when the minimum weight changed. {"changed": {metric: [old, new]}}"""
    def fn(b):
        r = b.get("ratchet")
        if r is None:
            b["ratchet"] = r = {"metrics": dict(now), "min_weight": min_weight, "at": stamp(), "log": []}
            _log(r, "start", None, {})
            return {"changed": {k: [None, v] for k, v in now.items()}, "started": True}
        mw_changed = r.get("min_weight", 0) != min_weight
        changed = {}
        for k, v in now.items():
            old = r["metrics"].get(k)
            if mw_changed and k.startswith("weight.") and old is not None:
                continue
            if old is None or better(k, old, v):
                changed[k] = [old, v]
                r["metrics"][k] = v
        if changed:
            r["metrics"] = {k: r["metrics"][k] for k in NAMES if k in r["metrics"]}
            r["at"] = stamp()
            _log(r, "tighten", None, changed)
        return {"changed": changed, "started": False}
    return baseline.update(fn)


def loosen(baseline, now: dict, min_weight: int, reason: str) -> dict:
    """Write every current value, worse ones too, with the reason in the log."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("loosening the ratchet needs a reason")

    def fn(b):
        r = b.get("ratchet") or {"metrics": {}, "log": []}
        changed = {k: [r["metrics"].get(k), v] for k, v in now.items() if r["metrics"].get(k) != v}
        changed.update({k: [v, None] for k, v in r["metrics"].items() if k not in now})
        if not changed and r.get("min_weight") == min_weight:
            return {"changed": {}}
        b["ratchet"] = {"metrics": dict(now), "min_weight": min_weight, "at": stamp(), "log": r.get("log") or []}
        _log(b["ratchet"], "loosen", reason[:500], changed)
        return {"changed": changed}
    return baseline.update(fn)


# ---------- output ----------

def fmt(x) -> str:
    return "none" if x is None else str(int(x)) if float(x).is_integer() else str(x)


def text(st: dict) -> str:
    if not st["stored"]:
        return (f"Ratchet: none yet. {len(st['metrics'])} metrics measured; "
                "`archplan check --tighten` starts the ratchet in the baseline.")
    rows = st["rows"]
    lines = [f"Ratchet (set {st['at'] or '?'}): {st['worse']} worse, {st['better']} better, "
             f"{sum(1 for r in rows if r['status'] == 'same')} the same."]
    for r in rows:
        if r["status"] == "same":
            continue
        tol = f" (tolerance {fmt(r['tolerance'])})" if r.get("tolerance") else ""
        lines.append(f"  {r['status']:<7} {r['metric']}: {fmt(r['old'])} -> {fmt(r['new'])}{tol}, {r['label']}")
        if r.get("note"):
            lines.append(f"          {r['note']}")
        for c in r.get("causes") or []:
            what = f"[{c['state']}, weight {c['weight']}] " if "weight" in c else ""
            lines.append(f"          {what}{c['text']}"[:200] + (f" (id {c['id']})" if c.get("id") else ""))
    if st["worse"]:
        lines.append("  Fix the cause, or run `archplan check --loosen REASON` to accept the worse values.")
    if st["better"] or any(r["status"] == "new" for r in rows):
        lines.append("  Run `archplan check --tighten` to keep the better values in the baseline.")
    return "\n".join(lines)


def github(st: dict) -> str:
    from .findings import github_escape
    out = []
    for r in st["rows"]:
        if r["status"] == "worse":
            top = "; ".join(c["text"].rstrip(".") for c in (r.get("causes") or [])[:3])
            out.append("::error title=" + github_escape(f"archplan ratchet {r['metric']}", True) + "::"
                       + github_escape(f"{r['label']} got worse: {fmt(r['old'])} -> {fmt(r['new'])}."
                                       + (f" Top: {top}." if top else "")))
    return "\n".join(out)

