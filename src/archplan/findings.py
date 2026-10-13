"""Findings: what archplan notices in the code, as a list the human works
through. Each finding can start a discussion, be dismissed with a reason, or
be accepted as is.

Detectors (the detectors package) make the findings. This module runs them
and keeps the baseline. Built-in kinds, by group:

  profile     profile.rule     a unit breaks a calls rule of the architecture profile
              profile.pair     a unit has files without their partner (a view without a view builder)
  structure   structure.cycle  two or three components that depend on each other in a cycle, with the cheapest cut
              structure.tangle four or more components that all reach each other: one finding, with
                               a small set of cuts that breaks every cycle
  layers      layers.upward    a component depends on a component in a higher layer
              layers.policy    a dependency the target's policy forbids
  vocabulary  vocabulary.leak  a component uses terms of a concept it should not (from the glossaries)
  types       types.fields     a type with many fields, or with groups of related fields
              types.synonyms   types in different components with mostly the same field names
  history     history.coupling components that change together without a dependency (cached hotspots)
  concepts    concepts.*       state flags, data clumps, raw ids, untyped bags, units in names,
                               strings used as enums, boolean flag parameters
  boundaries  boundaries.*     zones of pain and uselessness, public code nobody else uses
  evolution   evolution.*      components without a clear owner (cached hotspots)

A finding's id is a hash of its kind and stable keys (rule and unit, the
cycle's members, the two components), so it survives changes in counts. A
tangle's id is its hub, the member with the most dependencies inside it, so
it survives members joining or leaving.

The baseline in .archplan/baseline.json is committed. It holds the findings
the human accepted or dismissed, and Claude's labels:

  {"version": 1, "findings": {id: {"state", "reason", "at", "title", "size"?}},
   "labels": {id: {"label", "note", "at"}}}

Only git-free data is read: history findings come from .archplan/hotspots.json
as `archplan hotspots` left it.

For a tangle the baseline keeps its size. When the tangle grows beyond it,
the finding is new again.

Each finding has a weight from 0 to 100 and the factors behind it
(weight_basis; see detectors.common.Weight). Findings are listed by weight
within a state. A weight change does not reopen a baselined finding.
`archplan check --min-weight N`, or "check": {"min_weight": N} in
config.json, leaves out new findings below N."""

from __future__ import annotations

import re
from collections import Counter

from . import detectors
from .architecture import Architecture
from .detectors.common import (COMMON_FIELDS, EXAMPLES, GROUPS, STRENGTHS, comp_name, evidence_file,  # noqa: F401
                               finding, fingerprint, graph, namer, plural, strength_of)
from .detectors.cycles import (MAX_CYCLES, TANGLE, cycle_findings, feedback_edges, sccs,  # noqa: F401
                               shortest_cycle, small_cycle_findings, tangle_findings)
from .detectors.fields import is_flag  # noqa: F401
from .detectors.history_coupling import coupling_findings  # noqa: F401
from .detectors.layers_policy import policy_findings  # noqa: F401
from .detectors.layers_upward import layer_findings  # noqa: F401
from .detectors.profile_rule import profile_findings  # noqa: F401
from .detectors.types_fields import field_findings  # noqa: F401
from .detectors.types_synonyms import names_of, synonym_findings  # noqa: F401
from .detectors.vocabulary_leak import leak_findings  # noqa: F401
from .project import Project, etag, read_json, write_json
from .store import JsonStore, NotFound, Store, stamp

STATES = ("new", "accepted", "dismissed")


def run(p: Project, arch: Architecture | None = None, execute: bool = False) -> dict:
    """Every detector's findings, without states: see detectors.run."""
    return detectors.run(p, arch or Architecture(p), execute=execute)


def analyse(p: Project, arch: Architecture | None = None, execute: bool = False) -> tuple:
    """(findings, missing): every finding the data supports, without states,
    and the detectors left out for lack of data, with the reason."""
    res = run(p, arch, execute)
    return res["findings"], res["missing"]


TEXT_MAX = 500  # characters kept of a baseline string


def clip(x, n: int = TEXT_MAX) -> str | None:
    return None if x is None else str(x)[:n]


def clean_baseline(d) -> dict:
    """The baseline as committed repo data: untrusted. Only accepted and
    dismissed entries count; every value becomes a short string."""
    d = d if isinstance(d, dict) else {}
    fs, ls = d.get("findings"), d.get("labels")
    out = {"version": 1, "findings": {}, "labels": {}}
    for k, e in (fs if isinstance(fs, dict) else {}).items():
        if isinstance(e, dict) and e.get("state") in ("accepted", "dismissed"):
            out["findings"][clip(k, 100)] = {"state": e["state"], "reason": clip(e.get("reason")),
                                             "at": clip(e.get("at"), 40), "title": clip(e.get("title"))}
            size = e.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and 0 < size < 10 ** 6:
                out["findings"][clip(k, 100)]["size"] = size
    for k, e in (ls if isinstance(ls, dict) else {}).items():
        if isinstance(e, dict) and e.get("label"):
            out["labels"][clip(k, 100)] = {"label": clip(e["label"], 60), "note": clip(e.get("note")),
                                           "at": clip(e.get("at"), 40)}
    return out


def entry(f: dict, state: str, reason: str | None) -> dict:
    """A baseline entry; a tangle's keeps its size."""
    e = {"state": state, "reason": reason, "at": stamp(), "title": f["title"]}
    if f["kind"] == "structure.tangle":
        e["size"] = f["evidence"]["size"]
    return e


def grown(f: dict, e: dict) -> dict | None:
    """{from, to} when a baselined tangle has more members now than when decided."""
    if f["kind"] != "structure.tangle" or e.get("state") not in ("accepted", "dismissed") or not e.get("size"):
        return None
    now = f["evidence"]["size"]
    return {"from": e["size"], "to": now} if now > e["size"] else None


class Baseline(JsonStore):
    """.archplan/baseline.json. Its version is the file format, not a write counter."""
    name = "baseline"
    empty = {"version": 1, "findings": {}, "labels": {}}

    def read(self) -> dict:
        try:
            return clean_baseline(super().read())
        except ValueError:  # not JSON
            return clean_baseline(None)

    def update(self, fn):
        with self.locked():
            data = self.read()
            out = fn(data)
            data["version"] = 1
            write_json(self.path, data)
            return out


class Findings:
    def __init__(self, p: Project):
        self.p = p
        self.arch = Architecture(p)
        self.baseline = Baseline(p.dir)
        self._cache = (None, None)
        self.execute = False

    def result(self) -> dict:
        """The detectors' result (findings, missing, detectors, extras), cached
        while the model, the target, the profile, the hotspots cache and the
        detector setup are unchanged. With self.execute (the CLI), external
        detectors run their commands; else their cached results count."""
        key = tuple(etag(x) for x in (self.p.model_path, self.p.target_path, self.arch.path,
                                      self.p.path("hotspots.json"))) + (detectors.version(self.p),)
        if self._cache[0] != key:
            self._cache = (key, run(self.p, self.arch, self.execute))
        return self._cache[1]

    def raw(self) -> tuple:
        res = self.result()
        return res["findings"], res["missing"]

    def listing(self, store: Store | None = None) -> dict:
        """{"findings", "counts", "missing"}: every current finding with its
        state, label and thread."""
        fs, missing = self.raw()
        b = self.baseline.read()
        base, labels = b.get("findings") or {}, b.get("labels") or {}
        threads = {}
        for th in (store or Store(self.p.dir)).read()["threads"]:
            if th["anchor"].startswith("finding:"):
                threads.setdefault(th["anchor"][8:], th["id"])
        out = []
        for f in fs:
            e = base.get(f["id"]) or {}
            f = {**f, "state": e.get("state", "new"), "reason": e.get("reason")}
            grew = grown(f, e)
            if grew:
                f.update(state="new", reason=None, grew=grew,
                         detail=f"Grew from {grew['from']} to {grew['to']} components. {f['detail']}")
            if f["id"] in labels:
                f["label"] = labels[f["id"]].get("label")
                f["label_note"] = labels[f["id"]].get("note")
            if f["id"] in threads:
                f["thread"] = threads[f["id"]]
            out.append(f)
        out.sort(key=lambda f: (STATES.index(f["state"]), -f.get("weight", 0)))  # stable: ties keep their order
        now = {f["id"] for f in fs}
        by_group, by_kind = Counter(), Counter()
        for f in out:
            if f["state"] == "new":
                by_group[f["group"]] += 1
                by_kind[f["kind"]] += 1
        counts = {s: sum(1 for f in out if f["state"] == s) for s in STATES}
        counts.update({"fixed": sum(1 for i in base if i not in now),
                       "by_group": {g: by_group[g] for g in GROUPS}, "by_kind": dict(sorted(by_kind.items()))})
        return {"findings": out, "counts": counts, "missing": missing}

    def get(self, fid: str, store: Store | None = None) -> dict:
        for f in self.listing(store)["findings"]:
            if f["id"] == fid:
                return f
        raise NotFound(f"no finding {fid}")

    def decide(self, fid: str, state: str, reason: str | None = None) -> dict:
        """Accept or dismiss a finding (it goes into the baseline), or reopen it."""
        if state not in STATES:
            raise ValueError(f"bad state {state!r}")
        reason = (reason or "").strip()
        if state == "dismissed" and not reason:
            raise ValueError("a dismissal needs a reason")
        if state == "new":
            def fn(d):
                if fid not in d["findings"]:
                    raise NotFound(f"finding {fid} is not in the baseline")
                del d["findings"][fid]
            self.baseline.update(fn)
            return self.get(fid)
        f = self.get(fid)
        self.baseline.update(lambda d: d["findings"].__setitem__(fid, entry(f, state, reason or None)))
        return {**f, "state": state, "reason": reason or None}

    def label(self, fid: str, label: str, note: str = "") -> dict:
        """Claude's judgment on a finding, shown as a label. An empty label removes it."""
        self.get(fid)
        label = (label or "").strip()[:60]

        def fn(d):
            if label:
                d["labels"][fid] = {"label": label, "note": (note or "").strip()[:500] or None, "at": stamp()}
            else:
                d["labels"].pop(fid, None)
        self.baseline.update(fn)
        return self.get(fid)

    def discuss(self, store: Store, fid: str, text: str | None = None) -> tuple:
        """(thread, created): the thread on finding:<id>, made with the
        finding's evidence as its snapshot when there is none yet."""
        f = self.get(fid, store)
        if f.get("thread"):
            return next(t for t in store.read()["threads"] if t["id"] == f["thread"]), False
        model = self.arch.model()
        snap = {"view": "findings", "focus": [f"finding:{fid}"],
                "nodes": [{"id": c, "label": comp_name(model, c)} for c in f["components"][:30]],
                "finding": {k: f[k] for k in ("id", "kind", "group", "title", "detail", "strength", "basis", "files")},
                "evidence": f["evidence"]}
        cyc = f["evidence"].get("edges") if f["kind"] == "structure.cycle" else None
        if cyc:
            snap["edges"] = cyc
        th = store.create(f"finding:{fid}", f["title"], (text or "").strip() or f"{f['title']}\n\n{f['detail']}",
                          "human", snapshot=snap)
        return th, True

    def update_baseline(self) -> dict:
        """Accept every current new finding with the reason "baseline", and
        drop entries for findings that are gone."""
        fs, _ = self.raw()
        now = {f["id"]: f for f in fs}

        def fn(d):
            gone = [i for i in d["findings"] if i not in now]
            for i in gone:
                del d["findings"][i]
            added = 0
            for i, f in now.items():
                e = d["findings"].get(i)
                if e is None or grown(f, e):
                    d["findings"][i] = entry(f, "accepted", "baseline")
                    added += 1
            return {"added": added, "removed": len(gone), "total": len(d["findings"])}
        return self.baseline.update(fn)


def min_weight(p: Project, given=None) -> int:
    """The weight below which `archplan check` leaves new findings out: the
    given value, else "check": {"min_weight": N} in config.json, else 0."""
    if given is None:
        c = (read_json(p.path("config.json"), {}) or {}).get("check")
        given = c.get("min_weight") if isinstance(c, dict) else None
    ok = isinstance(given, (int, float)) and not isinstance(given, bool) and 0 <= given <= 100
    return int(given) if ok else 0


def above(listing: dict, n: int) -> dict:
    """The listing without the new findings that weigh less than n; counts["new"]
    and counts["below_min_weight"] say how many are left and left out."""
    if n <= 0:
        return listing
    keep = [f for f in listing["findings"] if f["state"] != "new" or f.get("weight", 0) >= n]
    new = sum(1 for f in keep if f["state"] == "new")
    return {**listing, "findings": keep,
            "counts": {**listing["counts"], "new": new, "below_min_weight": listing["counts"]["new"] - new}}


def compact(f: dict) -> dict:
    """A finding in a few lines, for an agent."""
    out = {k: f[k] for k in ("id", "kind", "title", "detail", "strength", "basis", "state") if f.get(k) is not None}
    out["components"] = f["components"][:6]
    if f["files"]:
        out["files"] = f["files"][:4]
    if f.get("weight") is not None:
        out["weight"] = f["weight"]
    for k in ("unit", "reason", "label", "thread"):
        if f.get(k):
            out[k] = f[k]
    return out


def github_escape(s: str, prop: bool = False) -> str:
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


def line_of(f: dict) -> int | None:
    ev = f.get("evidence") or {}
    if ev.get("line"):
        return ev["line"]
    for b in ev.get("breaks") or []:
        m = re.search(r"\(line (\d+)\)", b.get("detail", ""))
        if m and b.get("from") == (f["files"] or [None])[0]:
            return int(m.group(1))
    for x in (ev.get("cut") or {}).get("evidence") or ev.get("evidence") or []:
        m = re.match(r"(.*):(\d+)$", x) if isinstance(x, str) else None
        if m and m.group(1) == (f["files"] or [None])[0]:
            return int(m.group(2))
    return None


def text(listing: dict, show_all: bool = False) -> str:
    c = listing["counts"]
    fs = [f for f in listing["findings"] if show_all or f["state"] == "new"]
    fs.sort(key=lambda f: (GROUPS.index(f["group"]) if f["group"] in GROUPS else len(GROUPS),
                           STATES.index(f["state"]) if f["state"] in STATES else 0, -f.get("weight", 0)))
    lines = [f"{c['new']} new, {c['accepted']} accepted, {c['dismissed']} dismissed"
             + (f", {c['fixed']} baselined findings fixed" if c["fixed"] else "")
             + (f", {c['below_min_weight']} new below the minimum weight" if c.get("below_min_weight") else "") + "."]
    group = None
    for f in fs:
        if f["group"] != group:
            group = f["group"]
            lines += ["", group.capitalize() + ":"]
        st = "" if f["state"] == "new" else f" [{f['state']}" + (f": {f['reason']}" if f.get("reason") else "") + "]"
        lab = f" (Claude: {f['label']})" if f.get("label") else ""
        lines.append(f"  {f['strength']:<6}  {f['title']}{lab}{st}")
        lines.append(f"          {f['basis']}. {f['detail']}"[:300])
        lines.append(f"          id {f['id']}, weight {f.get('weight', '?')}" + (f", {f['files'][0]}" if f["files"] else ""))
    for m in listing.get("missing") or []:
        lines += ["", f"Not checked, {m['kind']}: {m['reason']}."] if m is listing["missing"][0] else \
            [f"Not checked, {m['kind']}: {m['reason']}."]
    return "\n".join(lines)


def github(listing: dict, show_all: bool = False) -> str:
    out = []
    for f in listing["findings"]:
        if not show_all and f["state"] != "new":
            continue
        props = []
        if f["files"]:
            props.append("file=" + github_escape(f["files"][0], True))
            ln = line_of(f)
            if ln:
                props.append(f"line={ln}")
        props.append("title=" + github_escape(f"archplan {f['kind']}", True))
        out.append(f"::warning {','.join(props)}::{github_escape(f['title'] + ' ' + f['basis'] + '.')}")
    return "\n".join(out)
