"""Paths left out of the as-is model: "exclude" in .archplan/config.json.

"exclude" is a list of glob patterns relative to the project root (the
folder that holds .archplan, normally the repository root). `**` matches
any number of folders, `*` and `?` match within one folder name. A pattern
that matches a folder excludes everything under it, so `prototype`,
`prototype/` and `prototype/**` all leave out the prototype folder.

The built-in exporters, folder walks and the git history analysis all use
this one matcher, so excluded files do not show up anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_PATTERNS = 200


def clean(patterns) -> list:
    """The usable patterns: strings, without a leading ./ or / and a trailing /."""
    if not isinstance(patterns, list):
        return []
    out = []
    for p in patterns[:MAX_PATTERNS]:
        if not isinstance(p, str) or len(p) > 500:
            continue
        p = p.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.strip("/")
        if p and p not in out:
            out.append(p)
    return out


class Exclude:
    """Matches paths against the exclude patterns. Call it with a path (absolute,
    or relative to base); match() takes a posix path relative to base."""

    def __init__(self, base, patterns=()):
        self.base = Path(base).resolve()
        self.patterns = clean(list(patterns))
        self._cache: dict = {}

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def rel(self, path) -> str | None:
        """path relative to base, as a posix path; None when it is outside base."""
        p = os.path.abspath(os.path.join(self.base, path))
        for cand in (p, os.path.realpath(p)):
            r = os.path.relpath(cand, self.base)
            if r != ".." and not r.startswith(".." + os.sep):
                return "" if r == "." else r.replace(os.sep, "/")
        return None

    def match(self, rel: str) -> bool:
        """Whether rel, or a folder it is in, matches a pattern."""
        if not self.patterns or not rel:
            return False
        rel = rel.strip("/")
        hit = self._cache.get(rel)
        if hit is None:
            from .architecture import glob_match
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            hit = (bool(parent) and self.match(parent)) or any(glob_match(rel, p) for p in self.patterns)
            if len(self._cache) < 100_000:
                self._cache[rel] = hit
        return hit

    def __call__(self, path) -> bool:
        if not self.patterns:
            return False
        r = self.rel(path)
        return r is not None and self.match(r)

    def prune(self, d, dirs: list) -> None:
        """Drop excluded folders from an os.walk dirs list, in place."""
        if self.patterns:
            dirs[:] = [x for x in dirs if not self(os.path.join(d, x))]

    def model_path(self, path: str, prefix: str = "") -> bool:
        """Whether a model path is excluded. prefix is the project root relative
        to the folder the model's paths are relative to ("" when the same)."""
        if not self.patterns:
            return False
        if prefix:
            if not path.startswith(prefix):
                return False
            path = path[len(prefix):]
        return self.match(path)


def patterns_of(config: dict) -> list:
    return clean((config or {}).get("exclude"))


def load(start=None) -> Exclude:
    """The excludes of the project at or above start (default: the current
    folder). Without a project there are none."""
    from .project import DIR, find_root, read_json
    start = Path(start or ".").resolve()
    root = find_root(start)
    if root is None:
        return Exclude(start)
    try:
        cfg = read_json(root / DIR / "config.json", {}) or {}
    except (OSError, ValueError):
        cfg = {}
    return Exclude(root, patterns_of(cfg if isinstance(cfg, dict) else {}))


def filter_model(model: dict, ex: Exclude, root: Path) -> int:
    """Remove excluded paths from a model in place, for exporters that do not
    read the exclude list themselves. Drops files, refs, types, signatures, routes,
    literals and enums in excluded files, components whose path is excluded,
    and the dependencies and evidence that came from them. Returns the number
    of items removed."""
    if not ex:
        return 0
    from .safeio import model_base, prefix_of
    pre = prefix_of(root, model_base(root, model))

    def gone(f) -> bool:
        return isinstance(f, str) and ex.model_path(f, pre)

    n = 0
    from .grouping import EVIDENCE, file_dependency_evidence
    original_files = model.get("files")
    original_files = original_files if isinstance(original_files, dict) else {}
    modules_before = {v["module"] for v in original_files.values()
                      if isinstance(v, dict) and isinstance(v.get("module"), str)}
    original_evidence = list(file_dependency_evidence(original_files)) if modules_before else []
    module_edges = {(original_files[f]["component"], b)
                    for f, b, _ in original_evidence if original_files[f].get("module")}
    before = {}
    for f, b, line in original_evidence:
        before.setdefault((original_files[f]["component"], b), {})[f] = line
    comps = model.get("components") or {}
    dropped = {c for c, v in comps.items()
               if isinstance(v, dict) and v.get("kind") != "external" and v.get("path") and gone(v["path"])}
    for c in dropped:
        del comps[c]
    n += len(dropped)
    files = model.get("files")
    if isinstance(files, dict):
        for f in [f for f in files if gone(f)]:
            del files[f]
            n += 1
        modules_after = {v["module"] for v in files.values()
                         if isinstance(v, dict) and isinstance(v.get("module"), str)}
        removed_modules = modules_before - modules_after
        for v in files.values():
            if isinstance(v, dict) and isinstance(v.get("refs"), list):
                v["refs"] = [r for r in v["refs"] if not (isinstance(r, dict) and gone(r.get("to")))]
            if isinstance(v, dict) and isinstance(v.get("moduleImports"), list):
                v["moduleImports"] = [imp for imp in v["moduleImports"]
                                      if not (isinstance(imp, dict) and isinstance(imp.get("to"), str)
                                              and imp["to"] in removed_modules)]
    types = model.get("types")
    if isinstance(types, dict):
        for t in [t for t, v in types.items() if isinstance(v, dict) and (gone(v.get("file"))
                                                                          or v.get("component") in dropped)]:
            del types[t]
            n += 1
    for key in ("signatures", "literals", "enums", "routes"):
        lst = model.get(key)
        if isinstance(lst, list):
            keep = [x for x in lst
                    if not (isinstance(x, dict) and (gone(x.get("file")) or x.get("component") in dropped))]
            n += len(lst) - len(keep)
            model[key] = keep
    deps = model.get("dependencies")
    if isinstance(deps, list):
        out = []
        for d in deps:
            if not isinstance(d, dict) or d.get("from") in dropped or d.get("to") in dropped:
                n += 1
                continue
            ev = d.get("evidence")
            if isinstance(ev, list) and ev:
                kept = [e for e in ev if not gone(str(e).rsplit(":", 1)[0])]
                if not kept and int(d.get("weight") or 0) <= len(ev):
                    n += 1
                    continue  # every cause of the edge is excluded
                if len(kept) < len(ev):
                    d = dict(d, evidence=kept, weight=max(1, int(d.get("weight") or 0) - (len(ev) - len(kept))))
            out.append(d)
        model["dependencies"] = out
        if modules_before:
            per = {}
            evidence = list(file_dependency_evidence(files))
            module_edges.update((files[f]["component"], b) for f, b, _ in evidence if files[f].get("module"))
            for f, b, line in evidence:
                a = files[f]["component"]
                if (a, b) in module_edges and a in comps and b in comps:
                    per.setdefault((a, b), {})[f] = line
            changed_edges = {pair for pair in module_edges if before.get(pair, {}) != per.get(pair, {})}
            kept = [d for d in out if (d.get("from"), d.get("to")) not in changed_edges]
            rebuilt = [{"from": a, "to": b, "weight": len(srcs),
                        "evidence": [f"{f}:{line}" if line else f
                                     for f, line in list(srcs.items())[:EVIDENCE]]}
                       for (a, b), srcs in sorted(per.items()) if (a, b) in changed_edges]
            model["dependencies"] = sorted(kept + rebuilt, key=lambda d: (d["from"], d["to"]))
    return n
