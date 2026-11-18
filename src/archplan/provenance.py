"""Provenance: which calls between components the code shows, which the
traces show, and which both show.

The model's dependencies come from reading the code (static). Its sequences
come from running it, usually the tests (dynamic). Each can miss what the
other sees: static analysis misses calls it cannot resolve, such as dynamic
dispatch, and a trace misses every path no test runs. So an edge between two
components is

  both      a dependency that a traced call confirms
  static    a dependency no trace shows: not observed, which is not the same
            as never called
  dynamic   a traced call without a dependency: the static model misses it

A dynamic edge lists the sequences that produced it. A component is observed
when a traced call enters or leaves it. Messages that archplan added itself
when it joined a route (`"joined"`) are not observations and do not count."""

from __future__ import annotations

from .detectors.common import graph, namer

SEQUENCES = 10  # sequence ids kept per edge


def analyse(model: dict) -> dict:
    """{"traced", "edges": [{from, to, provenance, calls, sequence_count,
    sequences}], "components": {id: {observed, sequences}}, "summary"}."""
    comps = model.get("components") or {}
    types = model.get("types") or {}
    internal = {c for c, v in comps.items() if (v or {}).get("kind") != "external"}

    def comp(p):
        c = p if p in comps else (types.get(p) or {}).get("component") if isinstance(p, str) else None
        return c if c in internal else None

    static = set(graph(model))
    dynamic, seen = {}, {c: set() for c in internal}
    seqs = model.get("sequences") or {}
    for sid, seq in sorted(seqs.items()):
        for m in (seq or {}).get("messages") or []:
            if not isinstance(m, dict) or m.get("joined"):
                continue
            a, b = comp(m.get("from")), comp(m.get("to"))
            for c in (a, b):
                if c:
                    seen[c].add(sid)
            if a and b and a != b:
                e = dynamic.setdefault((a, b), {"calls": 0, "sequences": []})
                e["calls"] += 1
                if sid not in e["sequences"]:
                    e["sequences"].append(sid)
    edges = []
    for a, b in sorted(static | set(dynamic)):
        d = dynamic.get((a, b))
        edges.append({"from": a, "to": b,
                      "provenance": "both" if d and (a, b) in static else "dynamic" if d else "static",
                      "calls": d["calls"] if d else 0, "sequence_count": len(d["sequences"]) if d else 0,
                      "sequences": d["sequences"][:SEQUENCES] if d else []})
    count = {k: sum(1 for e in edges if e["provenance"] == k) for k in ("both", "static", "dynamic")}
    observed = sum(1 for c in internal if seen[c])
    return {"traced": bool(seqs), "edges": edges,
            "components": {c: {"observed": bool(seen[c]), "sequences": len(seen[c])} for c in sorted(internal)},
            "summary": {"edges": count, "components": {"observed": observed, "total": len(internal)},
                        "sequences": len(seqs)}}


def edge(model: dict, a: str, b: str) -> dict:
    """The provenance of one edge, for get_item: {provenance, calls, sequences}, or
    {"provenance": "none"} when neither the code nor a trace shows it."""
    res = analyse(model)
    if not res["traced"]:
        return {"provenance": "unknown", "note": "The model has no sequences, so no call is observed."}
    e = next((e for e in res["edges"] if e["from"] == a and e["to"] == b), None)
    if e is None:
        return {"provenance": "none"}
    out = {k: e[k] for k in ("provenance", "calls", "sequence_count", "sequences")}
    if e["provenance"] == "static":
        out["note"] = "Not observed in any trace. No test may run this path, so it is not proof the call never happens."
    elif e["provenance"] == "dynamic":
        out["note"] = "Traced calls without a dependency in the model: the static analysis does not resolve them."
    return out


def text(res: dict, model: dict, limit: int = 20) -> str:
    name = namer(model)
    if not res["traced"]:
        return "The model has no sequences: trace the tests first, then export. Every edge is static only."
    s = res["summary"]
    e, c = s["edges"], s["components"]
    lines = [f"{sum(e.values())} edges between components: {e['both']} in the code and traced, "
             f"{e['static']} in the code only (not observed), {e['dynamic']} traced only.",
             f"{c['observed']} of {c['total']} components are observed in {s['sequences']} sequences."]
    dyn = [x for x in res["edges"] if x["provenance"] == "dynamic"]
    if dyn:
        lines += ["", "Traced only (the static model misses these):"]
        lines += [f"  {name(x['from'])} -> {name(x['to'])}  {x['calls']} calls in {x['sequence_count']} sequences, "
                  f"such as {x['sequences'][0]}" for x in sorted(dyn, key=lambda x: -x["calls"])[:limit]]
        if len(dyn) > limit:
            lines.append(f"  {len(dyn) - limit} more; use --json for all.")
    dark = [k for k, v in res["components"].items() if not v["observed"]]
    if dark:
        lines += ["", f"Not observed ({len(dark)}): " + ", ".join(name(k) for k in dark[:limit])
                  + (f", and {len(dark) - limit} more" if len(dark) > limit else "") + "."]
    return "\n".join(lines)
