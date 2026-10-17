"""Helpers every detector shares: the finding shape, stable ids, component
names and the component dependency graph."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

GROUPS = ["profile", "structure", "layers", "vocabulary", "types", "history", "concepts", "boundaries", "data",
          "evolution", "security"]
STRENGTHS = ["strong", "medium", "weak"]
EXAMPLES = 20            # breaks, files or fields kept as evidence
COMMON_FIELDS = {"id", "uuid", "pk", "inserted_at", "updated_at", "created_at", "modified_at", "deleted_at",
                 "created", "modified", "updated", "created_by", "updated_by", "lock_version", "version"}


def fingerprint(kind: str, *keys) -> str:
    h = hashlib.sha1("\x1f".join([kind, *map(str, keys)]).encode()).hexdigest()[:12]
    return f"{kind}-{h}"


def strength_of(share: float) -> str:
    return "strong" if share >= 0.9 else "medium" if share >= 0.7 else "weak"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def finding(kind, keys, title, detail, strength, basis, components=(), files=(), evidence=None, unit=None,
            focus=None, weight=None) -> dict:
    """A finding. focus says where the editor shows it (see clean_focus).
    weight (a Weight) says how much it matters; the strength follows from it.
    Without a weight, the strength sets it. Neither is part of the id."""
    f = {"id": fingerprint(kind, *keys), "kind": kind, "group": kind.split(".")[0], "title": title,
         "detail": detail, "strength": strength, "basis": basis,
         "components": list(dict.fromkeys(c for c in components if c)),
         "files": list(dict.fromkeys(x for x in files if x))[:EXAMPLES], "evidence": evidence or {}}
    set_weight(f, weight if weight is not None else Weight.of_strength(strength))
    if unit:
        f["unit"] = unit
    fc = clean_focus(focus)
    if fc:
        f["focus"] = fc
    return f


# ---------- weight: how much a finding matters ----------
#
# A weight is 0 to 100: a base per kind plus the effect of factors, each
# recorded in weight_basis as {"factor", "value", "effect"} so the editor and
# the agent can explain the score. The shared factors:
#
#   spread       components, files or sites involved
#   repetition   one occurrence or a series
#   occurrence   where a term or value occurs: a comment weighs least, a
#                public type or route most (OCCURRENCE)
#   centrality   fan-in of the components involved (added by the runner)
#   churn        commits to them, from the cached hotspots (added by the runner)
#   layers       a dependency that points up the layers
#
# The strength follows from the weight (STRONG, MEDIUM), so they cannot disagree.

STRONG, MEDIUM = 65, 40
WEIGHT_BASIS_MAX = 12
# Where a term occurs, from least to most weight: its effect on the weight.
OCCURRENCE = {"comment": -18, "string": -10, "local": 0, "field": 6, "function": 10, "type": 16, "public": 22}
OCCURRENCE_LABEL = {"comment": "comments", "string": "strings", "local": "local names", "field": "field names",
                    "function": "function names", "type": "type names", "public": "public API"}


def strength_for(weight: float) -> str:
    return "strong" if weight >= STRONG else "medium" if weight >= MEDIUM else "weak"


def clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


class Weight:
    """A finding's weight, built from a base and factors."""

    def __init__(self, base: int, what: str = "kind"):
        self.basis = [{"factor": "base", "value": what, "effect": int(base)}]

    @classmethod
    def of_strength(cls, strength: str) -> "Weight":
        return cls({"strong": 70, "medium": 50}.get(strength, 25), f"strength {strength}")

    @property
    def value(self) -> int:
        return clamp(sum(b["effect"] for b in self.basis))

    def add(self, factor: str, value, effect: float, keep: bool = False) -> "Weight":
        """Record a factor; one without effect only with keep."""
        e = int(round(effect))
        if e or keep:
            self.basis.append({"factor": factor, "value": value, "effect": e})
        return self

    def spread(self, n: int, what: str = "components", per: float = 6, cap: float = 18, free: int = 1) -> "Weight":
        """More for each doubling beyond free."""
        return self.add(f"spread: {what}", n, min(cap, per * math.log2(n / free)) if n > free else 0)

    def repetition(self, n: int, what: str = "occurrences", per: float = 5, cap: float = 15,
                   single: float = -8) -> "Weight":
        """Less for a single occurrence, more for a series."""
        return self.add(f"repetition: {what}", n, single if n <= 1 else min(cap, per * math.log2(n)))

    def occurrence(self, contexts: dict) -> "Weight":
        """By the weightiest place a term occurs; a little more when it occurs in several kinds of place."""
        seen = [k for k in OCCURRENCE if (contexts or {}).get(k)]
        if not seen:
            return self
        top = max(seen, key=OCCURRENCE.get)
        label = ("only in " if len(seen) == 1 else "up to ") + OCCURRENCE_LABEL[top]
        return self.add("occurrence", label, OCCURRENCE[top] + 2 * (len(seen) - 1), keep=True)

    def layers(self, direction: str | None) -> "Weight":
        return self.add("layers", direction, {"up": 15, "down": -5}.get(direction or "", 0))

    def centrality(self, fan_in: int) -> "Weight":
        return self.add("centrality: fan-in", fan_in, min(10, 2.5 * math.log2(1 + fan_in)) if fan_in >= 2 else 0)

    def churn(self, commits: int, most: int) -> "Weight":
        return self.add("churn: commits", commits,
                        10 * math.log1p(commits) / math.log1p(most) if commits and most else 0)


def set_weight(f: dict, w: Weight) -> dict:
    f["weight"] = w.value
    f["weight_basis"] = [dict(b) for b in w.basis]
    f["strength"] = strength_for(f["weight"])
    return f


def reweigh(f: dict, fan_in: dict, churn: dict) -> dict:
    """Add centrality and churn of the finding's components, and set weight and strength again."""
    w = Weight(0)
    w.basis = [dict(b) for b in f.get("weight_basis") or []]
    comps = [c for c in f.get("components") or [] if c]
    w.centrality(max((fan_in.get(c, 0) for c in comps), default=0))
    w.churn(max((churn.get(c, 0) for c in comps), default=0), max(churn.values(), default=0))
    return set_weight(f, w)


def clean_weight(weight, basis) -> tuple:
    """(weight, basis) from untrusted output: a number clamped to 0..100 and at
    most WEIGHT_BASIS_MAX factors of short strings and numbers; (None, None)
    without a number."""
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight != weight:
        return None, None
    out = []
    for b in (basis if isinstance(basis, list) else [])[:WEIGHT_BASIS_MAX]:
        if not isinstance(b, dict) or not _text(b.get("factor")):
            continue
        v, e = b.get("value"), b.get("effect")
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e9 or _text(v)):
            v = None
        e = int(round(e)) if isinstance(e, (int, float)) and not isinstance(e, bool) and abs(e) <= 100 else 0
        out.append({"factor": b["factor"][:60], "value": v[:100] if isinstance(v, str) else v, "effect": e})
    return clamp(weight if abs(weight) < 1e9 else 0), out


# ---------- focus: where the editor shows a finding ----------

VIEWS = ("types", "components", "roles", "sequences", "glossary")
FOCUS_MAX = 5        # entries
FOCUS_NODES = 100    # nodes, edges or typed fields per entry
FOCUS_FIELDS = 50    # field names per type
FOCUS_STR = 300      # characters of an id, field name, path or label


def _text(x) -> bool:
    return isinstance(x, str) and 0 < len(x) <= FOCUS_STR and x.isprintable()


def _texts(x, n: int) -> list:
    return list(dict.fromkeys(s for s in (x if isinstance(x, list) else [])[:n] if _text(s)))


def clean_focus(x) -> list:
    """A finding's focus, read as untrusted data: a list of
    {"view", "label"?, "nodes", "edges"?, "fields"?, "files"?}. Entries with an
    unknown view or nothing to show are dropped; lists are capped and every
    value must be a short string."""
    out = []
    for e in (x if isinstance(x, list) else [])[:FOCUS_MAX]:
        if not isinstance(e, dict) or e.get("view") not in VIEWS:
            continue
        c = {"view": e["view"], "nodes": _texts(e.get("nodes"), FOCUS_NODES)}
        if _text(e.get("label")):
            c["label"] = e["label"]
        edges = [[p[0], p[1]] for p in (e.get("edges") if isinstance(e.get("edges"), list) else [])[:FOCUS_NODES]
                 if isinstance(p, (list, tuple)) and len(p) == 2 and _text(p[0]) and _text(p[1])]
        if edges:
            c["edges"] = edges
        fs = e.get("fields")
        if isinstance(fs, dict):
            fs = {k: _texts(v, FOCUS_FIELDS) for k, v in list(fs.items())[:FOCUS_NODES] if _text(k)}
            fs = {k: v for k, v in fs.items() if v}
            if fs:
                c["fields"] = fs
        files = _texts(e.get("files"), EXAMPLES)
        if files:
            c["files"] = files
        if c["nodes"] or edges or files:
            out.append(c)
    return out


def focus(view: str, nodes=(), edges=(), fields=None, files=(), label=None) -> dict:
    """One focus entry; nodes are the model's ids of that view's elements."""
    e = {"view": view, "nodes": list(dict.fromkeys(n for n in nodes if n))}
    if edges:
        e["edges"] = [[a, b] for a, b in edges if a and b]
    if fields:
        e["fields"] = {k: list(dict.fromkeys(v)) for k, v in fields.items() if k and v}
    if files:
        e["files"] = list(dict.fromkeys(f for f in files if f))
    if label:
        e["label"] = label
    return e


def types_focus(fields: dict, components=(), types=()) -> list:
    """The Types view with these types and their fields ({type id: [field
    names]}), and the components second."""
    nodes = list(fields) + [t for t in types if t not in fields]
    out = [focus("types", nodes, fields={k: v for k, v in fields.items() if v})]
    if components:
        out.append(focus("components", components))
    return out


def comp_name(model: dict, cid: str) -> str:
    return namer(model)(cid)


_NAMERS = {}


def namer(model: dict):
    """cid -> its name, or its id when other components share the name."""
    key = id(model)
    hit = _NAMERS.get(key)
    if hit and hit[0] is model:
        return hit[1]
    comps = model.get("components") or {}
    names = {c: (v or {}).get("name") or c for c, v in comps.items()}
    dup = Counter(names.values())
    fn = lambda c: names.get(c, c) if dup[names.get(c, c)] == 1 else c
    _NAMERS.clear()
    _NAMERS[key] = (model, fn)
    return fn


def evidence_file(ev: str) -> str:
    """'path/file.ex:12' -> 'path/file.ex'."""
    return re.sub(r":\d+$", "", ev)


def graph(model: dict) -> dict:
    """{(from, to): {weight, evidence}} between internal components."""
    comps = model.get("components") or {}
    internal = {c for c, v in comps.items() if (v or {}).get("kind") != "external"}
    out = {}
    for d in model.get("dependencies") or []:
        a, b = d.get("from"), d.get("to")
        if a == b or a not in internal or b not in internal:
            continue
        e = out.setdefault((a, b), {"weight": 0, "evidence": []})
        e["weight"] += int(d.get("weight") or 1)
        e["evidence"] += d.get("evidence") or []
    return out
