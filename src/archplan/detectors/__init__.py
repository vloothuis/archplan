"""Detectors: the checks that turn a model into findings.

A built-in detector is a module in this package with

  ID      the finding kind it reports, "group.name"
  NAME    a short label
  GROUP   profile, structure, layers, vocabulary, types, history, concepts,
          boundaries, data or evolution
  NEEDS   the model capabilities or sections it reads (see REASONS)
  PARAMS  its settings and their defaults
  run(ctx, params) -> [finding dict]

An external detector is a JSON file in contrib/detectors/ (shipped) or in the
project's .archplan/detectors/ (repository data). See external.py.

.archplan/config.json can switch detectors off and change their settings:

  "detectors": {"<id>": {"enabled": false, "params": {"<name>": value}}}

A detector whose needs the model does not meet is not run. It goes into the
`missing` list with the reason."""

from __future__ import annotations

import importlib
import sys
import time

from ..architecture import Architecture, ArchitectureError, analyse as profile_analysis
from ..project import Project, etag, read_json, write_json
from ..store import NotFound
from . import external
from .common import GROUPS, STRENGTHS, Weight, comp_name, finding, fingerprint, graph, namer, reweigh

BUILTIN = ["profile_rule", "profile_pair", "structure_cycle", "structure_tangle", "layers_upward", "layers_policy",
           "vocabulary_leak", "types_fields", "types_synonyms", "history_coupling",
           "concepts_state_flags", "concepts_data_clump", "concepts_raw_ids", "concepts_untyped_bag",
           "concepts_units_in_names", "concepts_string_enum", "concepts_bool_params",
           "boundaries_stability", "boundaries_unused_public", "evolution_ownership",
           "data_shared_table", "data_cross_fk", "data_polymorphic", "data_json_bag", "data_flag_columns",
           "data_repeated_columns", "data_enum_mismatch", "data_orphan_table"]

REASONS = {
    "components": "the model has no components",
    "deps": "the model has no dependencies",
    "types": "the model has no types",
    "fields": "the model's types have no fields",
    "files": "the model has no file references (files section)",
    "layers": "the profile has fewer than two layers",
    "vocabulary": "the project has no glossaries (vocabulary usage)",
    "git": "no cached hotspots; run `archplan hotspots`",
    "authors": "the cached hotspots have no author counts; run `archplan hotspots --refresh`",
    "signatures": "the model has no signatures section; the exporter does not provide it",
    "literals": "the model has no literals section; the exporter does not provide it",
    "schema": "the model has no schema section",
}
KNOWN_NEEDS = set(REASONS) | {"profile"}
READ_MAX = 1 << 20  # bytes ctx.read returns at most


class DetectorError(ValueError):
    pass


def as_list(x) -> list:
    """A model section that is a list, or a dict keyed by id, as a list of dicts."""
    if isinstance(x, dict):
        return [dict(v, id=v.get("id", k)) for k, v in x.items() if isinstance(v, dict)]
    return [v for v in x if isinstance(v, dict)] if isinstance(x, list) else []


class Context:
    """What a detector can read. Everything is computed once, on first use."""

    def __init__(self, p: Project, arch: Architecture | None = None, model: dict | None = None,
                 target: dict | None = None):
        self.p = p
        self.root = p.root
        self.arch = arch or Architecture(p)
        self.model = model if model is not None else self.arch.model()
        self.target = target if target is not None else (p.target_or_model() or self.model)
        self.detector = None  # the id of the detector that runs, set by the runner
        self.extras = {}      # data detectors publish for the editor, such as metrics
        self._memo = {}

    def _get(self, k, fn):
        if k not in self._memo:
            self._memo[k] = fn()
        return self._memo[k]

    # ids and findings

    def fid(self, kind: str, *keys) -> str:
        return fingerprint(kind, *keys)

    def finding(self, keys, title, detail, strength, basis, components=(), files=(), evidence=None,
                kind: str | None = None, unit=None, focus=None, weight: Weight | None = None) -> dict:
        return finding(kind or self.detector, keys, title, detail, strength, basis, components, files, evidence, unit,
                       focus, weight)

    def name(self, cid: str) -> str:
        return namer(self.model)(cid)

    # the model

    @property
    def components(self) -> dict:
        return self.model.get("components") or {}

    @property
    def internal(self) -> set:
        return self._get("internal", lambda: {c for c, v in self.components.items()
                                              if (v or {}).get("kind") != "external"})

    @property
    def types(self) -> dict:
        return self.model.get("types") or {}

    @property
    def graph(self) -> dict:
        return self._get("graph", lambda: graph(self.model))

    @property
    def fan_in(self) -> dict:
        """{component: number of internal components that depend on it}."""
        def fn():
            out = {}
            for _, b in self.graph:
                out[b] = out.get(b, 0) + 1
            return out
        return self._get("fan_in", fn)

    @property
    def churn(self) -> dict:
        """{component: commits} from the cached hotspots; empty without them."""
        def fn():
            comps = (self.hotspots or {}).get("components")
            return {c: x.get("commits") or 0 for c, x in comps.items() if isinstance(x, dict)
                    and isinstance(x.get("commits"), int)} if isinstance(comps, dict) else {}
        return self._get("churn", fn)

    @property
    def schema(self) -> dict | None:
        return self.model.get("schema") or None

    @property
    def signatures(self) -> list:
        return self._get("signatures", lambda: as_list(self.model.get("signatures")))

    @property
    def literals(self) -> list:
        return self._get("literals", lambda: as_list(self.model.get("literals")))

    @property
    def vocabulary(self) -> dict:
        return (self.target or {}).get("vocabulary") or self.model.get("vocabulary") or {}

    @property
    def files_by_component(self) -> dict:
        """{component: [repository-relative source files]}, tests left out."""
        def fn():
            from ..vocab import Vocab
            return dict(Vocab(self.p).files_by_component(self.model))
        return self._get("files", fn)

    @property
    def profile(self) -> tuple:
        """(profile, analysis): the saved profile, or the inferred proposal.
        Raises ArchitectureError without one."""
        def fn():
            try:
                st = self.arch.state()
            except ArchitectureError as e:
                return e
            prof = st.get("profile") or st.get("proposal") or {}
            return prof, profile_analysis(prof, self.model, self.root, cap=10 ** 6)
        r = self._get("profile", fn)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def hotspots(self) -> dict | None:
        """The cached hotspots analysis, as `archplan hotspots` left it. Never runs git."""
        return self._get("hotspots", lambda: (read_json(self.p.path("hotspots.json")) or {}).get("result") or None)

    def vocab_candidates(self) -> dict:
        """Glossary candidates per component (tf-idf over the code; cached per file)."""
        def fn():
            from ..vocab import Vocab
            return Vocab(self.p).analyse(self.model)
        return self._get("candidates", fn)

    @property
    def base(self):
        """The folder the model's paths are relative to: the repository root
        when the project sits in a subfolder of it."""
        from ..safeio import model_base
        return self._get("base", lambda: model_base(self.root, self.model))

    def read(self, path: str, limit: int = READ_MAX) -> str | None:
        """A repository file's text, or None when the path leaves the repository
        (paths come from the model, which is repository data) or cannot be read."""
        from ..safeio import read_text
        try:
            return read_text(self.base, path, limit, truncate=True)
        except OSError:
            return None

    # capabilities

    def lacks(self, need: str) -> str | None:
        """Why the data for need is missing, or None when it is there."""
        m = self.model
        if need == "profile":
            try:
                self.profile
            except ArchitectureError as e:
                return str(e)
            return None
        if need == "layers":
            try:
                prof = self.profile[0]
            except ArchitectureError as e:
                return str(e)
            ok = len([x for x in prof.get("layers") or [] if x.get("components")]) >= 2
        elif need == "components":
            ok = bool(self.components)
        elif need == "deps":
            ok = bool(m.get("dependencies"))
        elif need == "types":
            ok = bool(self.types)
        elif need == "fields":
            ok = any(t.get("fields") for t in self.types.values())
        elif need == "files":
            ok = bool(m.get("files"))
        elif need == "vocabulary":
            ok = bool(self.vocabulary.get("usage"))
        elif need == "git":
            ok = self.hotspots is not None
        elif need == "authors":
            if self.hotspots is None:
                return REASONS["git"]
            ok = isinstance(self.hotspots.get("owners"), dict)
        elif need == "signatures":
            ok = bool(self.signatures)
        elif need == "literals":
            ok = bool(self.literals)
        elif need == "schema":
            ok = bool(self.schema)
        else:
            return f"unknown need {need!r}"
        return None if ok else REASONS[need]

    @property
    def capabilities(self) -> list:
        return self._get("caps", lambda: sorted(n for n in KNOWN_NEEDS if self.lacks(n) is None))


# ---------- the registry ----------

_MODULES = {}


def builtins() -> dict:
    """{id: module} for the built-in detectors."""
    if not _MODULES:
        for name in BUILTIN:
            m = importlib.import_module(f"{__name__}.{name}")
            _MODULES[m.ID] = m
    return _MODULES


def registry(p: Project | None = None) -> tuple:
    """({id: spec}, [(file, error)]). A spec: id, name, group, needs, params
    (defaults), origin (built-in, contrib or project) and, for a built-in, its
    module; for an external one, its command and timeout."""
    out = {}
    for did, m in builtins().items():
        out[did] = {"id": did, "name": m.NAME, "group": m.GROUP, "needs": sorted(m.NEEDS),
                    "params": dict(m.PARAMS), "origin": "built-in", "module": m}
    ext, errors = external.load(p.dir if p else None)
    for did, e in ext.items():
        if did in out and out[did]["origin"] == "built-in":
            errors.append((e["file"], f"{did} is a built-in detector"))
            continue
        out[did] = e
    return out, errors


def settings(p: Project) -> dict:
    """{id: {"enabled"?, "params"?}} from config.json, read as untrusted data."""
    d = (read_json(p.path("config.json"), {}) or {}).get("detectors")
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(d, dict) else {}


def coerce(defaults: dict, given, strict: bool = False) -> dict:
    """defaults updated with the given values that have the right type. With
    strict, a wrong name or type raises DetectorError instead of being skipped."""
    out = dict(defaults)
    if not isinstance(given, dict):
        if strict and given is not None:
            raise DetectorError("params must be an object")
        return out
    for k, v in given.items():
        if k not in defaults:
            if strict:
                raise DetectorError(f"unknown parameter {k!r}")
            continue
        d = defaults[k]
        ok = (isinstance(v, bool) if isinstance(d, bool) else
              isinstance(v, int) and not isinstance(v, bool) and abs(v) < 10 ** 9 if isinstance(d, int) else
              isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 10 ** 9 if isinstance(d, float) else
              isinstance(v, str) and len(v) <= 200 if isinstance(d, str) else
              isinstance(v, list) and len(v) <= 200 and all(isinstance(x, str) and len(x) <= 200 for x in v)
              if isinstance(d, list) else False)
        if not ok:
            if strict:
                raise DetectorError(f"parameter {k!r} must be like {d!r}")
            continue
        out[k] = float(v) if isinstance(d, float) else v
    return out


def configure(p: Project, did: str, enabled=None, params=None) -> dict:
    """Switch a detector on or off, or change its settings, in config.json."""
    reg, _ = registry(p)
    if did not in reg:
        raise NotFound(f"no detector {did}")
    if enabled is not None and not isinstance(enabled, bool):
        raise DetectorError("enabled must be true or false")
    if params is not None:
        params = coerce(reg[did]["params"], params, strict=True)
    path = p.path("config.json")
    cfg = read_json(path, {}) or {}
    ds = cfg.get("detectors") if isinstance(cfg.get("detectors"), dict) else {}
    e = ds.get(did) if isinstance(ds.get(did), dict) else {}
    if enabled is not None:
        e["enabled"] = enabled
    if params is not None:
        e["params"] = {k: v for k, v in params.items() if v != reg[did]["params"][k]}
        if not e["params"]:
            del e["params"]
    if e:
        ds[did] = e
    else:
        ds.pop(did, None)
    cfg["detectors"] = ds
    write_json(path, cfg)
    return e


def version(p: Project) -> str:
    """Changes when the detector setup or the external detectors' results change."""
    parts = [etag(p.path("config.json")), etag(p.path(external.CACHE))]
    for d in (external.BUILTIN, p.path("detectors")):
        if d.is_dir():
            parts += [f"{f.name}:{etag(f)}" for f in sorted(d.glob("*.json"))]
    return external.short_hash("|".join(map(str, parts)))


# ---------- running ----------

def status(spec: dict, cfg: dict) -> dict:
    c = cfg.get(spec["id"]) or {}
    return {"id": spec["id"], "name": spec["name"], "group": spec["group"], "needs": spec["needs"],
            "origin": spec["origin"], "enabled": c.get("enabled", True) is not False,
            "params": coerce(spec["params"], c.get("params"))}


def run(p: Project, arch: Architecture | None = None, only=None, execute: bool = False,
        include_disabled: bool = False) -> dict:
    """Run the enabled detectors (or only the given ids).

    External detectors run their command only with execute (the CLI); else
    their cached result counts when the model and settings are unchanged.

    {"findings", "missing": [{kind, detector, reason}], "detectors": [status
    with count, missing and seconds], "extras": {detector: data}}"""
    ctx = Context(p, arch)
    reg, errors = registry(p)
    cfg = settings(p)
    found, missing, rows = [], [], []
    for did, spec in reg.items():
        st = status(spec, cfg)
        if only is not None and did not in only:
            continue
        st.update(count=0, missing=[])
        rows.append(st)
        if not st["enabled"] and not include_disabled:
            continue
        lacks = [(n, ctx.lacks(n)) for n in spec["needs"]]
        lacks = [(n, r) for n, r in lacks if r]
        if lacks:
            st["missing"] = [n for n, _ in lacks]
            missing.append({"kind": did, "detector": did, "reason": lacks[0][1]})
            continue
        t = time.monotonic()
        ctx.detector = did
        try:
            if spec["origin"] == "built-in":
                fs = spec["module"].run(ctx, st["params"])
            else:
                fs = external.run(ctx, spec, st["params"], execute)
        except external.NotRun as e:
            st["missing"] = ["run"]
            missing.append({"kind": did, "detector": did, "reason": str(e)})
            continue
        except Exception as e:  # one broken detector must not take the others down
            sys.stderr.write(f"archplan: detector {did} failed: {e!r}\n")
            st["error"] = str(e)[:300]
            missing.append({"kind": did, "detector": did, "reason": f"the detector failed: {e}"[:300]})
            continue
        if spec["origin"] == "built-in":
            fs = [reweigh(f, ctx.fan_in, ctx.churn) for f in fs]
        st["seconds"] = round(time.monotonic() - t, 3)
        st["count"] = len(fs)
        found += fs
    for f, err in errors:
        missing.append({"kind": "detector", "detector": None, "reason": f"{f}: {err}"[:300]})
    seen, uniq = set(), []
    for f in found:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    uniq.sort(key=lambda f: (-f.get("weight", 0), GROUPS.index(f["group"]) if f["group"] in GROUPS else len(GROUPS),
                             STRENGTHS.index(f["strength"]), f["kind"], f["title"]))
    return {"findings": uniq, "missing": missing, "detectors": rows, "extras": ctx.extras,
            "capabilities": ctx.capabilities}


def text(res: dict) -> str:
    """The detector table for `archplan detectors`."""
    lines = [f"{'detector':<28} {'group':<11} {'on':<4} {'found':>5}  needs"]
    for d in res["detectors"]:
        miss = f"  (missing: {', '.join(d['missing'])})" if d.get("missing") else ""
        err = f"  (failed: {d['error']})" if d.get("error") else ""
        lines.append(f"{d['id']:<28} {d['group']:<11} {'yes' if d['enabled'] else 'no':<4} "
                     f"{d['count'] if d['enabled'] and not d.get('missing') else '-':>5}  "
                     f"{', '.join(d['needs']) or '-'}{miss}{err}"
                     + ("" if d["origin"] == "built-in" else f"  [{d['origin']}]"))
    for m in res["missing"]:
        lines.append(f"Not run, {m['kind']}: {m['reason']}." if m is not res["missing"][0] else
                     f"\nNot run, {m['kind']}: {m['reason']}.")
    lines.append(f"\nModel capabilities: {', '.join(res.get('capabilities') or []) or 'none'}.")
    return "\n".join(lines)


__all__ = ["Context", "DetectorError", "GROUPS", "builtins", "comp_name", "configure", "registry", "run",
           "settings", "text", "version"]
