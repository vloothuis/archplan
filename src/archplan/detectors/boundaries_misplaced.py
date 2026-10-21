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
from .common import EXAMPLES, Weight, evidence_file, focus, plural

ID = "boundaries.misplaced_file"
NAME = "Files in the wrong component"
GROUP = "boundaries"
NEEDS = {"files"}
PARAMS = {"min_links": 3.0, "ratio": 2.0, "min_share": 0.6, "incoming": 0.5, "hub": 4, "min_degree": 0.5,
          "terms": True, "max_findings": 20}
BARRELS = {"__init__.py", "index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs", "mod.rs", "lib.rs",
           "package-info.java"}
ENTRIES = {"__main__.py", "main.go", "main.rs", "main.py", "manage.py", "setup.py", "conftest.py"}


def base(f: str) -> str:
    return f.rsplit("/", 1)[-1]


def dicts(x) -> dict:
    """A model section keyed by id, with only the dict values (the model is repository data)."""
    return {k: v for k, v in x.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(x, dict) else {}


def strs(xs) -> list:
    return [x for x in xs if isinstance(x, str) and x] if isinstance(xs, list) else []


class FileGraph:
    """The links between the model's source files, and each file's component.

    links[f] = {g: {"out": 0|1, "in": 0|1, "cochange": degree}}: f refers to
    g, g refers to f, and how much they change together; comp has the files
    that take part (internal, no tests, no package entries). refs has every
    reference between two files of the model, also those that do not take
    part, as (from, to)."""

    def __init__(self, ctx, min_degree: float | None = 0.0):
        files = dicts(ctx.model.get("files"))
        comps = dicts(ctx.components)
        mapper = Mapper(comps)
        paths = {c: str(v.get("path") or "").rstrip("/") for c, v in comps.items()}
        self.inside = lambda c, k: bool(paths.get(k)) and paths.get(c, "").startswith(paths[k] + "/")
        self.of = {f: v.get("component") if isinstance(v.get("component"), str) else mapper(f)
                   for f, v in files.items()}
        self.comp = {f: c for f, c in self.of.items()
                     if c in ctx.internal and not is_test(f) and base(f) not in BARRELS}
        self.links = defaultdict(lambda: defaultdict(lambda: {"out": 0, "in": 0, "cochange": 0.0}))
        self.refs = []
        for f, v in files.items():
            for r in v.get("refs") or [] if isinstance(v.get("refs"), list) else []:
                self._ref(f, r.get("to") if isinstance(r, dict) else None)
        types = dicts(ctx.types)
        tfile = {t: v.get("file") for t, v in types.items() if isinstance(v.get("file"), str)}
        for v in types.values():
            fields = v.get("fields") if isinstance(v.get("fields"), list) else []
            for x in strs([x.get("ref") for x in fields if isinstance(x, dict)]) + strs(v.get("uses")) \
                    + strs(v.get("implements")):
                self._ref(v.get("file"), tfile.get(x))
        if min_degree is not None:
            for x in ((ctx.hotspots or {}).get("cochange") or {}).get("pairs") or []:
                if not isinstance(x, dict) or not isinstance(x.get("degree"), (int, float)) \
                        or x["degree"] < min_degree:
                    continue
                a, b = x.get("a"), x.get("b")
                if isinstance(a, str) and isinstance(b, str) and a in self.comp and b in self.comp:
                    for p, q in ((a, b), (b, a)):
                        self.links[p][q]["cochange"] = max(self.links[p][q]["cochange"], float(x["degree"]))
        # how many files each file uses, and how many use it
        self.uses = Counter(f for f, gs in self.links.items() for x in gs.values() if x["out"])
        self.users = Counter(f for f, gs in self.links.items() for x in gs.values() if x["in"])

    def _ref(self, a, b):
        if not isinstance(a, str) or not isinstance(b, str) or a == b or a not in self.of or b not in self.of:
            return
        self.refs.append((a, b))
        if a in self.comp and b in self.comp:
            self.links[a][b]["out"] = 1  # a link, however many references
            self.links[b][a]["in"] = 1

    def votes(self, f: str, incoming: float = 1.0, hub: int = 0) -> Counter:
        """{component: the strength of f's links to its files}. With hub, a
        reference to a file with n > hub users, or from a file with n > hub
        uses, counts hub / n."""
        damp = lambda n: min(1.0, hub / n) if hub > 0 and n else 1.0
        out = Counter()
        for g, link in self.links.get(f, {}).items():
            up = incoming if self.inside(self.comp[f], self.comp[g]) else 1.0
            out[self.comp[g]] += (up * link["out"] * damp(self.users[g]) + incoming * link["in"] * damp(self.uses[g])
                                  + link["cochange"])
        return out


def term_vectors(ctx, files: list) -> dict:
    """{file: {word: tf-idf weight}} from the words of identifiers, normalised;
    empty when the files cannot be read."""
    try:
        from ..core.vocab import Vocab
        v = Vocab(ctx.p)
        counts = v.file_counts(files, v.base(ctx.model), prune=False)
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


def total(vecs: list) -> Counter:
    out = Counter()
    for v in vecs:
        out.update(v)
    return out


def cosine(vec: dict, other: dict) -> float:
    """Cosine of vec (normalised) and other."""
    norm = math.sqrt(sum(x * x for x in other.values()))
    return sum(x * other.get(w, 0) for w, x in vec.items()) / norm if norm else 0.0


def run(ctx, params) -> list:
    fg = FileGraph(ctx, params["min_degree"])
    found = defaultdict(list)  # (own, other) -> [(file, votes)]
    for f, c in sorted(fg.comp.items()):
        if base(f) in ENTRIES:
            continue
        votes = fg.votes(f, params["incoming"], params["hub"])
        best = max((k for k in votes if k != c), key=lambda k: (votes[k], k), default=None)
        if best is None or votes[best] < max(params["min_links"], params["ratio"] * votes[c],
                                             params["min_share"] * sum(votes.values()), 1e-9):
            continue
        found[(c, best)].append((f, votes))
    if not found:
        return []
    vecs = term_vectors(ctx, sorted(fg.comp)) if params["terms"] else {}
    sums = defaultdict(Counter)  # component -> the sum of its files' word vectors
    if vecs:
        by_comp = defaultdict(list)
        for f, c in fg.comp.items():
            if f in vecs:
                by_comp[c].append(vecs[f])
        sums = {c: total(vs) for c, vs in by_comp.items()}
    rows = []
    for (c, k), fs in found.items():
        files = []
        for f, votes in fs:
            row = {"file": f, "own": round(votes[c], 2), "other": round(votes[k], 2)}
            if f in vecs:
                own = Counter(sums.get(c) or {})
                own.subtract(vecs[f])
                row["words_own"] = round(cosine(vecs[f], own), 2)
                row["words_other"] = round(cosine(vecs[f], sums.get(k) or {}), 2)
                if row["words_other"] < 0.5 * row["words_own"]:
                    continue
            files.append(row)
        if files:
            rows.append((c, k, files))
    rows.sort(key=lambda r: (-len(r[2]), -sum(x["other"] for x in r[2]), r[0], r[1]))
    rows = rows[:params["max_findings"]]
    # the references between two components, and the files the model's dependencies name as evidence
    between = defaultdict(list)
    for a, b in fg.refs:
        if fg.of[a] != fg.of[b]:
            between[frozenset((fg.of[a], fg.of[b]))].append((a, b))
    named = defaultdict(set)
    for (a, b), e in ctx.graph.items():
        named[frozenset((a, b))].update(evidence_file(x) for x in e.get("evidence") or [] if isinstance(x, str))
    types_of = defaultdict(list)
    for tid, t in dicts(ctx.types).items():
        if isinstance(t.get("file"), str):
            types_of[t["file"]].append(tid)
    out = []
    for c, k, files in rows:
        fs = [x["file"] for x in files]
        moved = set(fs)
        after = lambda f: k if f in moved else fg.of[f]
        pair = frozenset((c, k))
        # The move removes the dependency when no reference links c and k afterwards, and the model names
        # no evidence for it outside the files section (which this detector cannot see into).
        removes = not any({after(a), after(b)} == {c, k} for a, b in between[pair]) \
            and all(f in fg.of for f in named[pair])
        tids = [t for f in fs for t in sorted(types_of.get(f, []))]
        worded = [x for x in files if "words_own" in x]
        closer = sum(1 for x in worded if x["words_other"] > x["words_own"])
        short = lambda f: "/".join(f.split("/")[-2:])
        votes = lambda x: f"{x['other']:.1f} to {ctx.name(k)}, {x['own']:.1f} to {ctx.name(c)}"
        if len(fs) == 1:
            title = (f"{short(fs[0])} in {ctx.name(c)} has most of its links to {ctx.name(k)} "
                     f"(weighted links: {votes(files[0])}).")
        else:
            title = f"{plural(len(fs), 'file')} in {ctx.name(c)} have most of their links to {ctx.name(k)}."
        detail = "Weighted links: " + "; ".join(
            f"{short(x['file'])}: {votes(x)}" + (f", words closer to {ctx.name(k)}"
                                                 if x.get("words_other", 0) > x.get("words_own", 0) else "")
            for x in files[:3]) + (f"; and {len(files) - 3} more" if len(files) > 3 else "") + "."
        if removes:
            detail += (f" After the move, no file of {ctx.name(c)} refers to {ctx.name(k)} or the other way round: "
                       "the move removes that dependency.")
        moves = ([f"Move {', '.join(short(f) for f in fs[:3])} to {ctx.name(k)}"
                  + (f", with {', '.join((ctx.types[t].get('name') or t) for t in tids[:4])} (move_type {k})"
                     if tids else "") + "."]
                 + ([f"Then check that nothing else links {c} and {k}, and remove_dependency between them."]
                    if removes else [])
                 + ["If the file is shared on purpose, make it part of the public API of " + ctx.name(c)
                    + " and leave it."])
        other = sum(x["other"] for x in files)
        out.append(ctx.finding(
            (c, k), title, detail, "medium",
            f"{plural(len(fs), 'file')} with more links to {ctx.name(k)} than to {ctx.name(c)}",
            [c, k], fs,
            {"from": c, "to": k, "files": files[:EXAMPLES], "types": tids[:EXAMPLES], "removes_dependency": removes,
             "moves": moves},
            focus=[focus("components", [c, k], edges=[(c, k)], files=fs[:EXAMPLES], label="Components"),
                   *([focus("types", tids[:EXAMPLES])] if tids else [])],
            weight=Weight(25, "misplaced file").spread(len(fs), "files", per=5, cap=12)
            .add("links to the other component", round(other, 1),
                 min(10, 3 * math.log2(other / max(1.0, params["min_links"]))) if other > 0 else 0)
            .add("no links to its own component", sum(1 for x in files if not x["own"]),
                 6 if all(not x["own"] for x in files) else 0)
            .add("words", f"closer to {ctx.name(k)}", 6 if worded and closer * 2 >= len(worded) else 0)
            .add("removes the dependency", removes, 12 if removes else 0)))
    return out
