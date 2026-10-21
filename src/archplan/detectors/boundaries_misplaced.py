"""boundaries.misplaced_file: files whose links point to another component
more than to their own. Their neighbours vote: the files a file refers to,
the files that refer to it, and the files it changes together with in git.

This follows two lines of work. Code-to-architecture mapping (Jabeen et al.,
2026, arXiv 2610.19769) places a file by the component of its neighbours in
the dependency graph, with the words of its identifiers as extra evidence.
PairSmell (Zhong et al., 2024, arXiv 2411.01012; 2026, arXiv 2606.22576)
calls a pair of files that should live together but are separated "inapt
separated": such pairs change together 190% more than other separated
pairs. The most telling signals were dependencies and shared words.

The votes for a component are the sum of the links to its files:
  - 1 when the file refers to one of them (an import, a call or a type);
  - incoming (0.5) when one of them refers to the file: a file that another
    component uses is often that component's provider, and belongs with its
    own dependencies, so what it uses counts more than who uses it;
  - the co-change degree for a pair that changes together in at least
    min_degree of their commits (from the cached hotspots, when there are
    any).
A reference to a file that many files use (utils, compat, models) says
little about where a file belongs, as a common word says little about a
text. So does a reference from a file that uses many (an app's main module).
Such a reference counts hub / n, for a file with n users or uses above hub.
A reference to a file of an enclosing component (httpie/output/streams.py
to httpie/models.py) counts as incoming: a package that uses its parent's
core is layered, not misplaced.
Package entries (__init__.py, index.ts) neither vote nor get votes: a
re-export is packaging, not cohesion.

A file is misplaced when one other component gets at least min_links votes,
ratio times the votes of its own component, and min_share of all its votes.
A file that many components use is shared, not misplaced: the share keeps it
out. With the words of identifiers (tf-idf, from the vocabulary cache) the
file is compared to the other files of both components. When its words are
less than half as close to the other component as to its own, the links
alone do not make it misplaced, and it is left out; when they are closer to
the other component, the finding weighs more.

One finding per pair of components: the files of one component that belong
in another. The move is to move them, with their types (move_type). When the
moved files are the only link between the two components, the move removes
that dependency."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from ..core.hotspots import Mapper
from ..core.vocab import is_test
from .common import EXAMPLES, Weight, focus, plural

ID = "boundaries.misplaced_file"
NAME = "Files in the wrong component"
GROUP = "boundaries"
NEEDS = {"files"}
PARAMS = {"min_links": 3.0, "ratio": 2.0, "min_share": 0.6, "incoming": 0.5, "hub": 4, "min_degree": 0.5, "terms": True, "max_findings": 20}
BARRELS = {"__init__.py", "index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs", "mod.rs", "lib.rs",
           "package-info.java"}
ENTRIES = {"__main__.py", "main.go", "main.rs", "main.py", "manage.py", "setup.py", "conftest.py"}


def base(f: str) -> str:
    return f.rsplit("/", 1)[-1]


class FileGraph:
    """The links between the model's source files, and each file's component.

    links[f] = {g: {"out": 0|1, "in": 0|1, "cochange": degree}}: f refers to
    g, g refers to f, and how much they change together; comp has the files
    that take part (internal, no tests, no package entries)."""

    def __init__(self, ctx, min_degree: float | None = 0.0):
        files = ctx.model.get("files") or {}
        mapper = Mapper(ctx.components)
        self.comp = {}
        paths = {c: ((v or {}).get("path") or "").rstrip("/") for c, v in ctx.components.items()}
        self.inside = lambda c, k: bool(paths.get(k)) and (paths.get(c) or "").startswith(paths[k] + "/")
        for f, v in files.items():
            c = (v or {}).get("component") or mapper(f)
            if c in ctx.internal and not is_test(f) and base(f) not in BARRELS:
                self.comp[f] = c
        self.links = defaultdict(lambda: defaultdict(lambda: {"out": 0, "in": 0, "cochange": 0.0}))
        for f, v in files.items():
            for r in (v or {}).get("refs") or []:
                g = r.get("to") if isinstance(r, dict) else None
                self._ref(f, g)
        tfile = {t: (v or {}).get("file") for t, v in ctx.types.items()}
        for t, v in ctx.types.items():
            v = v or {}
            for x in [x.get("ref") for x in v.get("fields") or [] if isinstance(x, dict)] \
                    + list(v.get("uses") or []) + list(v.get("implements") or []):
                self._ref(v.get("file"), tfile.get(x))
        if min_degree is not None:
            for x in ((ctx.hotspots or {}).get("cochange") or {}).get("pairs") or []:
                a, b = (x.get("a"), x.get("b")) if isinstance(x, dict) else (None, None)
                if a in self.comp and b in self.comp and isinstance(x.get("degree"), (int, float)) \
                        and x["degree"] >= min_degree:
                    for p, q in ((a, b), (b, a)):
                        self.links[p][q]["cochange"] = max(self.links[p][q]["cochange"], float(x["degree"]))

    def _ref(self, a, b):
        if a and b and a != b and a in self.comp and b in self.comp:
            self.links[a][b]["out"] = 1  # a link, however many references
            self.links[b][a]["in"] = 1

    def votes(self, f: str, incoming: float = 1.0, hub: int = 0) -> Counter:
        """{component: the strength of f's links to its files}. With hub, a
        reference to a file with n > hub users, or from a file with n > hub
        uses, counts hub / n."""
        if hub and not hasattr(self, "_fan"):
            self._fan = (Counter(g for gs in self.links.values() for g, x in gs.items() if x["in"]),
                         Counter(g for gs in self.links.values() for g, x in gs.items() if x["out"]))
        damp = lambda n: min(1.0, hub / n) if hub and n else 1.0
        out = Counter()
        for g, link in self.links.get(f, {}).items():
            users, uses = (self._fan[0][g], self._fan[1][g]) if hub else (0, 0)
            up = incoming if self.inside(self.comp[f], self.comp[g]) else 1.0
            out[self.comp[g]] += up * link["out"] * damp(users) + incoming * link["in"] * damp(uses) + link["cochange"]
        return out


def term_vectors(ctx, files: list) -> dict:
    """{file: {word: tf-idf weight}} from the words of identifiers, normalised;
    empty when the files cannot be read."""
    try:
        from ..core.vocab import Vocab
        v = Vocab(ctx.p)
        counts = v.file_counts(files, v.base(ctx.model))
    except Exception:  # the words only add weight; a finding does not need them
        return {}
    tf = {f: {w: c[0] for w, c in (e.get("w") or {}).items() if c and c[0]} for f, e in counts.items()}
    df = Counter(w for ws in tf.values() for w in ws)
    n = len(tf)
    out = {}
    for f, ws in tf.items():
        vec = {w: (1 + math.log(c)) * math.log(n / df[w]) for w, c in ws.items() if df[w] < n}
        norm = math.sqrt(sum(x * x for x in vec.values()))
        if norm:
            out[f] = {w: x / norm for w, x in vec.items()}
    return out


def similarity(vec: dict, others: list) -> float:
    """Cosine of vec and the sum of the others' vectors."""
    total = Counter()
    for o in others:
        total.update(o)
    norm = math.sqrt(sum(x * x for x in total.values()))
    return sum(x * total.get(w, 0) for w, x in vec.items()) / norm if norm else 0.0


def run(ctx, params) -> list:
    fg = FileGraph(ctx, params["min_degree"])
    found = defaultdict(list)  # (own, other) -> [(file, votes)]
    for f, c in sorted(fg.comp.items()):
        if base(f) in ENTRIES:
            continue
        votes = fg.votes(f, params["incoming"], params["hub"])
        total = sum(votes.values())
        best = max((k for k in votes if k != c), key=lambda k: (votes[k], k), default=None)
        if best is None or votes[best] < params["min_links"] or votes[best] < params["ratio"] * votes[c] \
                or votes[best] < params["min_share"] * total:
            continue
        found[(c, best)].append((f, votes))
    if not found:
        return []
    vecs = term_vectors(ctx, sorted(fg.comp)) if params["terms"] else {}
    by_comp = defaultdict(list)
    for f, c in fg.comp.items():
        by_comp[c].append(f)
    types_of = defaultdict(list)
    for tid, t in ctx.types.items():
        if (t or {}).get("file"):
            types_of[t["file"]].append(tid)
    rows = []
    for (c, k), fs in found.items():
        files = []
        for f, votes in fs:
            row = {"file": f, "own": round(votes[c], 2), "other": round(votes[k], 2)}
            if f in vecs:
                row["words_own"] = round(similarity(vecs[f], [vecs[g] for g in by_comp[c] if g != f and g in vecs]), 2)
                row["words_other"] = round(similarity(vecs[f], [vecs[g] for g in by_comp[k] if g in vecs]), 2)
                if row["words_other"] < 0.5 * row["words_own"]:
                    continue
            files.append(row)
        if not files:
            continue
        moved = {x["file"] for x in files}
        # after the move, does any reference still link c and k?
        after = lambda f: k if f in moved else fg.comp[f]
        removes = not any(link["out"] and {after(a), after(b)} == {c, k}
                          for a, gs in fg.links.items() for b, link in gs.items())
        rows.append((c, k, files, removes))
    rows.sort(key=lambda r: (-len(r[2]), -sum(x["other"] for x in r[2]), r[0], r[1]))
    out = []
    for c, k, files, removes in rows[:params["max_findings"]]:
        fs = [x["file"] for x in files]
        tids = [t for f in fs for t in sorted(types_of.get(f, []))]
        worded = [x for x in files if "words_own" in x]
        closer = sum(1 for x in worded if x["words_other"] > x["words_own"])
        short = lambda f: "/".join(f.split("/")[-2:])
        if len(fs) == 1:
            x = files[0]
            title = (f"{short(fs[0])} in {ctx.name(c)} has most of its links to {ctx.name(k)} "
                     f"({x['other']:g} against {x['own']:g}).")
        else:
            title = f"{plural(len(fs), 'file')} in {ctx.name(c)} have most of their links to {ctx.name(k)}."
        detail = "; ".join(f"{short(x['file'])}: {x['other']:g} to {ctx.name(k)}, {x['own']:g} to {ctx.name(c)}"
                           + (f", words closer to {ctx.name(k)}" if x.get("words_other", 0) > x.get("words_own", 0)
                              else "") for x in files[:3]) \
            + (f"; and {len(files) - 3} more" if len(files) > 3 else "") + "."
        if removes:
            detail += f" No other code links {ctx.name(c)} and {ctx.name(k)}: the move removes that dependency."
        moves = ([f"Move {', '.join(short(f) for f in fs[:3])} to {ctx.name(k)}"
                  + (f", with {', '.join((ctx.types[t].get('name') or t) for t in tids[:4])} (move_type {k})"
                     if tids else "") + "."]
                 + ([f"Then remove_dependency between {c} and {k}: nothing else needs it."] if removes else [])
                 + ["If the file is shared on purpose, make it part of the public API of " + ctx.name(c)
                    + " and leave it."])
        out.append(ctx.finding(
            (c, k), title, detail, "medium",
            f"{plural(len(fs), 'file')} with more links to {ctx.name(k)} than to {ctx.name(c)}",
            [c, k], fs,
            {"from": c, "to": k, "files": files[:EXAMPLES], "types": tids[:EXAMPLES], "removes_dependency": removes,
             "moves": moves},
            focus=[focus("components", [c, k], edges=[(c, k)], files=fs[:EXAMPLES], label="Components"),
                   *([focus("types", tids[:EXAMPLES])] if tids else [])],
            weight=Weight(25, "misplaced file").spread(len(fs), "files", per=5, cap=12)
            .add("links to the other component", round(sum(x["other"] for x in files), 1),
                 min(10, 3 * math.log2(sum(x["other"] for x in files) / params["min_links"])))
            .add("no links to its own component", sum(1 for x in files if not x["own"]),
                 6 if all(not x["own"] for x in files) else 0)
            .add("words", f"closer to {ctx.name(k)}", 6 if worded and closer * 2 >= len(worded) else 0)
            .add("removes the dependency", removes, 12 if removes else 0)))
    return out
