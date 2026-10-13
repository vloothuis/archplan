"""layers.upward: a component depends on a component in a higher layer."""

from __future__ import annotations

from collections import Counter, defaultdict

from .common import EXAMPLES, Weight, evidence_file, finding, focus, graph, namer, plural, strength_of

ID = "layers.upward"
NAME = "Upward dependencies"
GROUP = "layers"
NEEDS = {"layers"}
PARAMS = {}



def run(ctx, params) -> list:
    return layer_findings(ctx.profile[0], ctx.model)


def layer_findings(prof: dict, model: dict) -> list:
    """Dependencies from a lower layer to a higher one, one finding per
    component and higher layer. Where most dependencies between two layers go
    up, the layer order itself is in doubt: one finding for the pair."""
    layers = [x for x in prof.get("layers") or [] if x.get("components")]
    rank = {c: i for i, layer in enumerate(layers) for c in layer["components"]}
    if len(layers) < 2:
        return []
    name = namer(model)
    g = graph(model)
    down, up = Counter(), defaultdict(list)  # (higher, lower) -> count / upward edges
    for (a, b) in sorted(g):
        if a in rank and b in rank and rank[a] != rank[b]:
            pair = tuple(sorted((rank[a], rank[b])))
            if rank[a] < rank[b]:
                down[pair] += 1
            else:
                up[pair].append((a, b))
    out = []
    for pair, edges in sorted(up.items()):
        hi, lo = layers[pair[0]], layers[pair[1]]
        total = down[pair] + len(edges)
        share = down[pair] / total
        basis = f"{down[pair]} of {total} dependencies between these layers go down"
        if share < 0.5:
            comps = sorted({a for a, _ in edges})
            ev = [x for a, b in edges for x in g[(a, b)]["evidence"][:1]]
            out.append(finding(
                "layers.upward", ("layers", lo.get("name"), hi.get("name")),
                f"Most dependencies between {lo.get('name')} and {hi.get('name')} go up: "
                f"{lo.get('name')} may not belong below {hi.get('name')}.",
                f"{plural(len(edges), 'dependency', 'dependencies')} from {plural(len(comps), 'component')} in "
                f"{lo.get('name')}, such as " + ", ".join(f"{name(a)} → {name(b)}" for a, b in edges[:3]) + ".",
                "weak", basis, comps[:EXAMPLES], [evidence_file(x) for x in ev],
                {"from_layer": lo.get("name"), "to_layer": hi.get("name"), "upward": len(edges), "down": down[pair],
                 "edges": [{"from": a, "to": b, "weight": g[(a, b)]["weight"]} for a, b in edges[:EXAMPLES]]},
                focus=[focus("components", sorted({x for e in edges for x in e}), edges)],
                weight=Weight(30, "layer order in doubt").spread(len(edges), "upward dependencies", per=4, cap=12)))
            continue
        by_comp = defaultdict(list)
        for a, b in edges:
            by_comp[a].append(b)
        for a, targets in sorted(by_comp.items()):
            refs = sum(g[(a, b)]["weight"] for b in targets)
            ev = [x for b in targets for x in g[(a, b)]["evidence"]]
            more = f" and {plural(len(targets) - 1, 'other')}" if len(targets) > 1 else ""
            out.append(finding(
                "layers.upward", (a, hi.get("name")),
                f"{name(a)} in {lo.get('name')} depends on {name(targets[0])}{more} in the higher layer {hi.get('name')}.",
                f"{plural(refs, 'reference')}: " + ", ".join(ev[:3]),
                strength_of(share), basis, [a, *targets], [evidence_file(x) for x in ev],
                {"from": a, "to": targets, "weight": refs, "evidence": ev[:EXAMPLES],
                 "from_layer": lo.get("name"), "to_layer": hi.get("name")},
                focus=[focus("components", [a, *targets], [(a, b) for b in targets])],
                weight=Weight(35, "upward dependency").layers("up")
                .add("conformance", f"{round(share * 100)}% go down", 10 if share >= 0.9 else 5 if share >= 0.7 else 0)
                .repetition(refs, "references", per=3, cap=12, single=-6)
                .spread(len(targets), "higher components", per=4, cap=8)))
    return out
