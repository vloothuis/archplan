"""Dependency cycles and tangles between components."""

from __future__ import annotations

from collections import Counter, defaultdict, deque

from .common import EXAMPLES, Weight, comp_name, evidence_file, finding, focus, graph, namer, plural

MAX_CYCLES = 40          # cycles reported; each one found after cutting the cheaper ones before it
TANGLE = 4               # an SCC of this many components or more is one tangle finding
TANGLE_CUTS = 15         # cut edges listed per tangle
SHORT_CYCLES = 100_000   # 2- and 3-cycles counted per tangle at most


def sccs(nodes, edges) -> list:
    """Strongly connected components (Tarjan, iterative), each a sorted list."""
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
    for a in adj:
        adj[a].sort()
    index, low, on, stack, out, n = {}, {}, set(), [], [], 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(adj[root]))]
        index[root] = low[root] = n
        n += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is not None:
                if w not in index:
                    index[w] = low[w] = n
                    n += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(adj[w])))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def shortest_cycle(members: list, edges) -> list:
    """The shortest cycle inside one SCC as [a, b, ..., a]; ties go to the
    lightest, then to the first by name."""
    inside = set(members)
    adj = defaultdict(list)
    for (a, b), w in edges.items():
        if a in inside and b in inside:
            adj[a].append(b)
    best, best_key = None, None
    for s in members:
        prev, q = {s: None}, deque([s])
        found = None
        while q and found is None:
            v = q.popleft()
            for w in sorted(adj[v]):
                if w == s:
                    found = v
                    break
                if w not in prev:
                    prev[w] = v
                    q.append(w)
        if found is None:
            continue
        path = [found]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        path = path[::-1] + [s]
        key = (len(path), sum(edges[(a, b)] for a, b in zip(path, path[1:])), path)
        if best_key is None or key < best_key:
            best, best_key = path, key
    return best


def tangle_hub(members: list, edges) -> str:
    """The member with the most dependencies inside the tangle, in and out."""
    inside = set(members)
    deg = Counter()
    for a, b in edges:
        if a in inside and b in inside:
            deg[a] += 1
            deg[b] += 1
    return min(members, key=lambda c: (-deg[c], c))


def eades_order(members: list, w: dict) -> list:
    """A vertex order with few backward edges (Eades, Lin and Smyth, weighted):
    peel sinks to the end and sources to the front; else take the vertex whose
    outgoing weight most exceeds its incoming weight."""
    left = set(members)
    out_e, in_e = defaultdict(dict), defaultdict(dict)
    for (a, b), x in w.items():
        out_e[a][b] = x
        in_e[b][a] = x
    head, tail = [], []

    def drop(v):
        left.discard(v)
    while left:
        changed = True
        while changed:
            changed = False
            for v in sorted(left):
                if not any(u in left for u in out_e[v]):
                    tail.append(v); drop(v); changed = True
                elif not any(u in left for u in in_e[v]):
                    head.append(v); drop(v); changed = True
        if left:
            v = max(sorted(left), key=lambda v: sum(x for u, x in out_e[v].items() if u in left)
                    - sum(x for u, x in in_e[v].items() if u in left))
            head.append(v)
            drop(v)
    return head + tail[::-1]


def sift(order: list, w: dict, passes: int = 8) -> list:
    """Improve an order by moving each vertex to the position where its
    backward edges weigh least, until no move helps."""
    out_e, in_e = defaultdict(list), defaultdict(list)
    for (a, b), x in w.items():
        out_e[a].append((b, x))
        in_e[b].append((a, x))
    order = list(order)
    for _ in range(passes):
        better = False
        for v in list(order):
            rest = [u for u in order if u != v]
            pos = {u: i for i, u in enumerate(rest)}
            # at slot 0 every edge into v is backward; moving v past u flips the edges between them
            delta = [0] * (len(rest) + 1)
            for u, x in out_e[v]:
                delta[pos[u] + 1] += x      # v -> u turns backward once v is after u
            for u, x in in_e[v]:
                delta[pos[u] + 1] -= x      # u -> v turns forward once v is after u
            cost = sum(x for _, x in in_e[v])
            best, best_i, cur_i = cost, 0, order.index(v)
            costs = [cost]
            for i in range(1, len(rest) + 1):
                cost += delta[i]
                costs.append(cost)
                if cost < best:
                    best, best_i = cost, i
            if best < costs[cur_i]:
                rest.insert(best_i, v)
                order = rest
                better = True
        if not better:
            break
    return order


def reaches(adj: dict, src: str, dst: str) -> bool:
    seen, q = {src}, [src]
    while q:
        v = q.pop()
        if v == dst:
            return True
        for u in adj.get(v, ()):
            if u not in seen:
                seen.add(u)
                q.append(u)
    return False


def feedback_edges(members: list, w: dict) -> list:
    """An approximately minimal set of edges whose removal leaves no cycle:
    the backward edges of an Eades order, minus any that can go back without
    closing a cycle (heaviest first). Sorted cheapest first."""
    pos = {v: i for i, v in enumerate(sift(eades_order(members, w), w))}
    cuts = [e for e in w if pos[e[0]] > pos[e[1]]]
    keep = defaultdict(set)
    for a, b in w:
        if pos[a] < pos[b]:
            keep[a].add(b)
    for a, b in sorted(cuts, key=lambda e: (-w[e], e)):
        if not reaches(keep, b, a):
            keep[a].add(b)
    return sorted(((a, b) for a, b in cuts if b not in keep[a]), key=lambda e: (w[e], e))


def short_cycles(members: list, w: dict) -> tuple:
    """(count, samples): the 2- and 3-cycles inside, and the cheapest few."""
    inside = sorted(members)
    out_e = defaultdict(set)
    for a, b in w:
        out_e[a].add(b)
    found, n = [], 0
    for a in inside:
        for b in out_e[a]:
            if b > a and a in out_e[b]:
                n += 1
                found.append([a, b])
            for c in out_e[b] if b > a else ():
                if c > a and c != b and a in out_e[c]:
                    n += 1
                    if len(found) < SHORT_CYCLES:
                        found.append([a, b, c])
        if n >= SHORT_CYCLES:
            break
    cost = lambda r: sum(w[(x, y)] for x, y in zip(r, r[1:] + r[:1]))
    return n, sorted(found, key=lambda r: (len(r), cost(r), r))


def tangle_finding(model: dict, g: dict, members: list) -> dict:
    w = {k: v["weight"] for k, v in g.items() if k[0] in members and k[1] in members}
    hub = tangle_hub(members, w)
    cuts = feedback_edges(members, w)
    n_cycles, cycles = short_cycles(members, w)
    two = [c for c in cycles if len(c) == 2]
    name = namer(model)
    total = sum(w[e] for e in cuts)
    cut_rows = [{"from": a, "to": b, "weight": w[(a, b)], "evidence": g[(a, b)]["evidence"][:3]} for a, b in cuts]
    detail = (f"Cutting {plural(len(cuts), 'dependency', 'dependencies')} ({plural(total, 'reference')}) breaks every "
              "cycle; the cheapest: " + ", ".join(f"{name(a)} → {name(b)} ({w[(a, b)]})" for a, b in cuts[:3]) + ".")
    return finding(
        "structure.tangle", (hub,),
        f"{name(hub)} and {plural(len(members) - 1, 'other component')} form one dependency tangle.",
        detail, "strong", f"{len(members)} components, {plural(n_cycles, 'short cycle')}",
        [hub] + [c for c in members if c != hub], [evidence_file(x) for r in cut_rows for x in r["evidence"]],
        {"members": members, "size": len(members), "hub": hub, "cuts": cut_rows[:TANGLE_CUTS],
         "cut_count": len(cuts), "cut_weight": total, "cycles": n_cycles,
         "sample_cycles": [r for r in cycles if len(r) == 3][:3] or cycles[:3],
         "two_cycles": [{"a": a, "b": b, "weights": [w[(a, b)], w[(b, a)]]}
                        for a, b in sorted(two, key=lambda r: (min(w[(r[0], r[1])], w[(r[1], r[0])]), r))][:10]},
        focus=[focus("components", [hub] + [c for c in members if c != hub], cuts[:TANGLE_CUTS])],
        weight=Weight(65, "tangle").spread(len(members), per=5, cap=15, free=4)
        .repetition(len(cuts), "cuts needed", per=2, cap=8, single=0))


def cycle_findings(model: dict) -> list:
    """One tangle finding per strongly connected component of TANGLE or more
    components. In the smaller ones, cycles found greedily: take the shortest
    cycle of each SCC, cut its lightest edge, and repeat."""
    return tangle_findings(model) + small_cycle_findings(model)


def tangle_findings(model: dict, size: int = TANGLE) -> list:
    g = graph(model)
    weights = {k: v["weight"] for k, v in g.items()}
    return [tangle_finding(model, g, s) for s in sccs({x for k in g for x in k}, weights) if len(s) >= size]


def small_cycle_findings(model: dict, size: int = TANGLE, max_cycles: int = MAX_CYCLES) -> list:
    """The cycles in strongly connected components of fewer than size members."""
    g = graph(model)
    weights = {k: v["weight"] for k, v in g.items()}
    nodes = {x for k in g for x in k}
    out = []
    small = set()
    for s in sccs(nodes, weights):
        if 1 < len(s) < size:
            small.update(s)
    weights = {k: w for k, w in weights.items() if k[0] in small and k[1] in small}
    nodes = small
    size = {c: len(s) for s in sccs(nodes, weights) for c in s}
    seen, n_tangles = set(), len(out)
    while len(out) - n_tangles < max_cycles:
        groups = [s for s in sccs(nodes, weights) if len(s) > 1]
        if not groups:
            break
        for s in groups:
            cyc = shortest_cycle(s, weights)
            if not cyc:
                continue
            pairs = list(zip(cyc, cyc[1:]))
            cut = min(pairs, key=lambda e: (weights[e], e))
            del weights[cut]
            key = tuple(sorted(cyc[:-1]))
            if key in seen:
                continue
            seen.add(key)
            # Start the cycle at its first member by name, so the title is stable.
            ring = cyc[:-1]
            i = ring.index(min(ring))
            ring = ring[i:] + ring[:i]
            names = [comp_name(model, c) for c in ring + ring[:1]]
            w = g[cut]["weight"]
            ev = g[cut]["evidence"][:EXAMPLES]
            out.append(finding(
                "structure.cycle", key,
                f"{' → '.join(names)} form a dependency cycle.",
                f"Cheapest cut: {comp_name(model, cut[0])} → {comp_name(model, cut[1])}, {plural(w, 'reference')}.",
                "strong", f"{plural(len(ring), 'component')}" + (f", part of a tangle of {size[ring[0]]}"
                                                                if size.get(ring[0], 0) > len(ring) else ""),
                ring, [evidence_file(x) for x in ev],
                {"cycle": ring, "edges": [{"from": a, "to": b, "weight": g[(a, b)]["weight"]}
                                          for a, b in zip(ring, ring[1:] + ring[:1])],
                 "cut": {"from": cut[0], "to": cut[1], "weight": w, "evidence": ev},
                 "tangle": size.get(ring[0], len(ring))},
                focus=[focus("components", ring, list(zip(ring, ring[1:] + ring[:1])))],
                weight=Weight(60, "cycle").spread(len(ring), per=4, free=2)
                .repetition(w, "references on the cheapest cut", per=4, cap=12, single=-4)))
            if len(out) - n_tangles >= max_cycles:
                break
    return out
