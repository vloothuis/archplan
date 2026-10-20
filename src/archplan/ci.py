"""archplan ci: the check for a pull request.

It uses the same findings, baseline and ratchet as `archplan check`, and
adds four things:

  export     it runs the export command first. In CI nobody has run
             `archplan trust`, so the job opts in with --trust-config or
             ARCHPLAN_CI_TRUST=1. Without either it needs an approved
             command or a model that is already there, as check does.
  base       with --base REF it also finds the findings of the merge base
             of REF and HEAD: it exports there in a temporary git worktree,
             or reads the model from --base-model PATH. A finding fails only
             when it is new against the baseline and not on the base. The
             settings (profile, config, baseline) are the head's on both
             sides, so only the code differs.
  reports    a markdown summary (--summary, default $GITHUB_STEP_SUMMARY)
             and SARIF 2.1.0 for code scanning (--sarif).
  reminder   a notice when a ratchet metric got better, with the command
             that keeps the better value.

Exit codes are check's: 1 on failing findings or a worse ratchet metric, 0
when neither, 2 on errors."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, findings, ratchet
from .core.architecture import Architecture, ArchitectureError, analyse
from .core.hotspots import git, toplevel
from .detectors.common import graph
from .core.safeio import inside
from .project import Project, ProjectError, etag, read_json, write_atomic

TRUST_ENV = "ARCHPLAN_CI_TRUST"
# Files in .archplan that belong to one machine or one run: not copied to the base.
LOCAL = ("server.json", "server.log", ".hook-seen.json", "activity.json", "vocab-cache.json",
         "suggest-state.json", "parts", "*.tmp", ".*.lock")
LIST_MAX = 50       # findings listed per section of the summary


def opted_in(flag: bool = False) -> bool:
    """Whether this run may run the export command without `archplan trust`."""
    return bool(flag) or os.environ.get(TRUST_ENV, "").strip().lower() in ("1", "true", "yes")


def evaluate(f: findings.Findings, given_min_weight: int | None = None) -> tuple:
    """(full listing, ratchet status, listing above the minimum weight): what
    `archplan check` and `archplan ci` both judge."""
    full = f.listing()
    rat = f.ratchet(full, given_min_weight)
    return full, rat, findings.above(full, findings.min_weight(f.p, given_min_weight))


def export(p: Project, trust: bool, what: str = "the model") -> str | None:
    """Run the export command through Project.export. Without trust it runs
    only an approved command; else it keeps a model that is already there and
    returns a note. Raises ProjectError when there is no model to use."""
    if not p.export_command():
        if p.model_path.exists():
            return f"no export command: {what} is used as it is"
        raise ProjectError("no export command and no model; set \"export\" in .archplan/config.json")
    if not trust and not p.trusted():
        why = (f"the export command is not approved here, so {what} was not exported again; "
               f"set {TRUST_ENV}=1 or pass --trust-config to run it")
        if p.model_path.exists():
            return why + ". The model in the repository is used as it is"
        raise ProjectError(why)
    print(f"running: {p.export_command()!r}", file=sys.stderr)
    p.export(require_trust=not trust)
    return None


# ---------- the base ----------

class ModelAt(Project):
    """The head's project with another model file: for --base-model without a checkout."""

    def __init__(self, root: Path, model: Path):
        super().__init__(root)
        self._model = Path(model).resolve()

    @property
    def model_path(self) -> Path:
        return self._model


def read_model(path) -> dict:
    try:
        m = read_json(Path(path), follow=True)
    except (OSError, ValueError) as e:
        raise ProjectError(f"cannot read the base model {path}: {e}") from e
    if not isinstance(m, dict) or m.get("format") != "archmodel/1":
        raise ProjectError(f"{path} is not an archmodel/1 file")
    return m


class Base:
    """The base side: a temporary git worktree at the merge base with the
    head's .archplan settings, or the head's checkout with another model.
    Use it as a context manager; the worktree goes away at the end."""

    def __init__(self, p: Project, ref: str | None, model: str | None, trust: bool):
        self.p, self.ref, self.model_file, self.trust = p, ref, model, trust
        self.tmp = self.wt = self.top = None
        self.commit = None
        self.note = None

    def __enter__(self) -> "Base":
        if self.model_file:
            read_model(self.model_file)
        if not self.ref:
            self.project = ModelAt(self.p.root, Path(self.model_file))
            return self
        self.top = toplevel(self.p.root)
        self.commit = git(self.top, "merge-base", self.ref, "HEAD").strip()
        self.tmp = Path(tempfile.mkdtemp(prefix="archplan-base-")).resolve()
        self.wt = self.tmp / "wt"
        git(self.top, "worktree", "add", "--detach", "--quiet", str(self.wt), self.commit)
        try:
            root = self.wt / self.p.root.relative_to(self.top)
            d = root / self.p.dir.name
            shutil.rmtree(d, ignore_errors=True)
            if d.is_symlink() or d.exists():
                d.unlink()
            # The head's settings, so that only the code differs; symlinks stay links.
            shutil.copytree(self.p.dir, d, symlinks=True, ignore=shutil.ignore_patterns(*LOCAL))
            self.project = bp = Project(root)
            # config.json comes from the branch: its model path must stay inside the worktree.
            try:
                rel = bp.model_path.relative_to(self.wt)
            except ValueError:
                rel = None
            if rel is None or inside(self.wt, rel) is None:
                raise ProjectError(f"the model path in {self.p.dir.name}/config.json is outside the repository")
            if bp.model_path.is_relative_to(d):
                # The head's model came along with the settings: put back the base's, when it is committed.
                bp.model_path.unlink(missing_ok=True)
                try:
                    write_atomic(bp.model_path, git(self.wt, "show", f"HEAD:{rel.as_posix()}"))
                except (ProjectError, ValueError):
                    pass
            if self.model_file:
                bp.model_path.parent.mkdir(parents=True, exist_ok=True)
                bp.model_path.unlink(missing_ok=True)
                shutil.copyfile(self.model_file, bp.model_path)
            else:
                self.note = export(bp, self.trust, "the base")
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        if self.wt is not None:
            try:
                git(self.top, "worktree", "remove", "--force", str(self.wt))
            except ProjectError:
                pass
            self.wt = None
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None
        return False


class HeadProfile(Architecture):
    """The base's architecture with the head's proposal. Without a saved
    profile, archplan infers one from the model; inferred from the base's
    model it could differ, and findings such as layers.upward would then
    differ for that reason alone."""

    def __init__(self, p: Project, proposal: dict):
        super().__init__(p)
        self.proposal = proposal

    def state(self) -> dict:
        if self.profile() is not None:
            return super().state()
        key = ("head", etag(self.p.model_path))
        if self._cache[0] != key:
            self._cache = (key, {"profile": None, "proposal": self.proposal,
                                 "analysis": analyse(self.proposal, self.model(), self.p.root)})
        return self._cache[1]


def base_findings(bp: Project, head: findings.Findings) -> findings.Findings:
    """Findings on the base with the head's profile, also when it is only inferred."""
    fb = findings.Findings(bp)
    fb.execute = head.execute
    if head.arch.profile() is None:
        try:
            proposal = head.arch.state().get("proposal")
        except ArchitectureError:
            proposal = None
        if proposal:
            fb.arch = HeadProfile(bp, proposal)
    return fb


def judged_by_base(f: dict, base_missing: set) -> bool:
    """Whether the base can say if f was there: its detector ran on the base."""
    return not any(f["kind"] == k or f["kind"].startswith(k + ".") for k in base_missing)


def compare(lst: dict, base_full: dict, head_full: dict) -> dict:
    """Split the new findings (against the baseline) into failing ones and
    ones already on the base; list the base's findings that are gone.
    Marks each new finding with on_base. A tangle that grew since the base
    counts as new."""
    pre = {f["id"]: f for f in base_full["findings"]}
    missing = {m["kind"] for m in base_full.get("missing") or []} - \
        {m["kind"] for m in head_full.get("missing") or []}
    out, failing, on_base = [], [], []
    for f in lst["findings"]:
        if f["state"] == "new":
            b = pre.get(f["id"])
            grew = b is not None and f["kind"] == "structure.tangle" and \
                f["evidence"].get("size", 0) > (b.get("evidence") or {}).get("size", 0)
            if b is not None and not grew and judged_by_base(f, missing):
                f = {**f, "state": "base"}
                on_base.append(f)
            else:
                failing.append(f)
        out.append(f)
    now = {f["id"] for f in head_full["findings"]}
    fixed = [f for f in base_full["findings"] if f["id"] not in now and f["state"] != "dismissed"]
    counts = {**lst["counts"], "new": len(failing), "on_base": len(on_base), "fixed_since_base": len(fixed)}
    return {"listing": {**lst, "findings": out, "counts": counts}, "failing": failing, "on_base": on_base,
            "fixed": fixed}


def ratchet_delta(base: dict, head: dict) -> list:
    """[{metric, label, base, head}] for the metrics measured on either side."""
    return [{"metric": k, "label": ratchet.label(k), "base": base.get(k), "head": head.get(k)}
            for k in ratchet.NAMES if k in base or k in head]


def plan_gap(model: dict | None, target: dict | None) -> int | None:
    """How far the code is from the target: components, dependencies between
    components and types that one has and the other does not, plus
    components whose name or path differ. None without both models. This is
    not the editor's task list, which only the editor computes; it counts
    the same differences."""
    if not model or not target:
        return None
    ca, cb = model.get("components") or {}, target.get("components") or {}
    n = len(set(ca) ^ set(cb))
    n += sum(1 for c in set(ca) & set(cb)
             if any((ca[c] or {}).get(k) != (cb[c] or {}).get(k) for k in ("name", "path")))
    n += len(set(graph(model)) ^ set(graph(target)))
    return n + len(set(model.get("types") or {}) ^ set(target.get("types") or {}))


def in_diff(f: dict, files: set, comps: set) -> bool:
    return bool(set(f.get("files") or []) & files or set(f.get("components") or []) & comps)


# ---------- reports ----------

def md(s) -> str:
    """Text for a markdown table cell, with markdown and HTML escaped."""
    return re.sub(r"([\\`*_\[\]()!#~|])", r"\\\1", " ".join(str(s).split())).replace("<", "&lt;")


def code(s) -> str:
    """Text for an inline code span in a table cell."""
    return "`" + " ".join(str(s).split()).replace("`", "'").replace("|", "\\|") + "`"


def summary(res: dict) -> str:
    c = res["counts"]
    rat = res["ratchet"]
    ok = not res["failing"] and not rat["worse"]
    lines = ["## archplan", "",
             f"**{'Passed' if ok else 'Failed'}**: {plural(len(res['failing']), 'new finding')}, "
             f"{plural(rat['worse'], 'ratchet metric')} worse."]
    if res.get("base"):
        b = res["base"]
        lines.append(f"Compared with {code(b['ref'] or b['model'])}"
                     + (f" (merge base `{b['commit'][:10]}`)" if b.get("commit") else "")
                     + f": {c.get('on_base', 0)} findings were already there, "
                     + f"{c.get('fixed_since_base', 0)} are fixed.")
    lines.append("")

    def table(fs, diff_col):
        out = ["| Weight | Finding | Where |" + (" In the diff |" if diff_col else ""),
               "|---:|---|---|" + ("---|" if diff_col else "")]
        for f in fs[:LIST_MAX]:
            where = f["files"][0] if f.get("files") else ", ".join(f.get("components", [])[:3])
            ln = findings.line_of(f) if f.get("files") else None
            out.append(f"| {f.get('weight', '')} | {md(f['title'])} | {code(f'{where}:{ln}' if ln else where)} |"
                       + ((" yes |" if f.get("in_diff") else " |") if diff_col else ""))
        if len(fs) > LIST_MAX:
            out.append(f"\n{len(fs) - LIST_MAX} more; see the job log.")
        return out + [""]

    diff_col = res.get("changed") is not None
    if res["failing"]:
        lines += [f"### New findings ({len(res['failing'])})", ""] + table(res["failing"], diff_col)
    if res.get("fixed"):
        lines += [f"### Fixed since the base ({len(res['fixed'])})", ""] + table(res["fixed"], False)
    if res.get("on_base"):
        lines += ["<details><summary>" + f"Already on the base ({len(res['on_base'])})</summary>", ""] \
            + table(res["on_base"], False) + ["</details>", ""]
    rows = {r["metric"]: r for r in rat["rows"]}
    delta = {d["metric"]: d for d in res.get("ratchet_delta") or []}
    if rows or delta:
        lines += ["### Ratchet", "", "| Metric | Baseline |" + (" Base |" if delta else "") + " Now | |",
                  "|---|---:|" + ("---:|" if delta else "") + "---:|---|"]
        for k in ratchet.NAMES:
            r, d = rows.get(k), delta.get(k)
            if not r and not d:
                continue
            old = ratchet.fmt(r["old"]) if r else "none"
            now = ratchet.fmt((r or {}).get("new", (d or {}).get("head")))
            st = r["status"] if r and rat["stored"] and r["status"] != "same" else ""
            lines.append(f"| `{k}` | {old} |" + (f" {ratchet.fmt(d['base']) if d else 'none'} |" if delta else "")
                         + f" {now} | {st} |")
        if not rat["stored"]:
            lines += ["", "No ratchet yet: run `archplan check --tighten` and commit `.archplan/baseline.json`."]
        lines.append("")
    if res.get("plan"):
        pl = res["plan"]
        lines += ["### Plan", "", f"{pl['head']} differences between the code and the target"
                  + (f" (base: {pl['base']})." if pl.get("base") is not None else ".") , ""]
    bl = res.get("blast")
    if bl and bl.get("traced") and "touched" in bl:
        lines += ["### Blast radius", "", f"{len(bl['files'])} changed files touch {len(bl['touched'])} of "
                  f"{bl['entries']} traced entry points.", ""]
        if bl["touched"]:
            lines += ["| Risk | Entry point | Why |", "|---:|---|---|"]
            lines += [f"| {e['risk']} | {code(e['entry'])} | {md('; '.join(e['because'][:2]))} |"
                      for e in bl["touched"]]
            lines.append("")
    for n in res.get("notes") or []:
        lines.append(f"> {md(n)}")
    return "\n".join(lines).rstrip() + "\n"


def plural(n: int, what: str) -> str:
    return f"{n} {what}{'' if n == 1 else 's'}"


def sarif(fs: list, paths, failing: set) -> dict:
    """SARIF 2.1.0 for GitHub code scanning: a rule per finding kind, a result
    per finding at its first file and line. paths maps a model path to a path
    from the repository root. Findings without a file point at the project's
    config.json, since code scanning needs a location."""
    rules, results = {}, []
    for f in fs:
        if f["kind"] not in rules:
            rules[f["kind"]] = {"id": f["kind"], "name": f["kind"],
                                "shortDescription": {"text": f"archplan {f['kind']}"},
                                "helpUri": "https://github.com/vloothuis/archplan#findings-and-archplan-check",
                                "properties": {"tags": ["architecture", f["group"]]}}
        loc = {"artifactLocation": {"uri": paths(f["files"][0] if f.get("files") else None),
                                    "uriBaseId": "%SRCROOT%"}}
        ln = findings.line_of(f) if f.get("files") else None
        if ln:
            loc["region"] = {"startLine": ln}
        results.append({"ruleId": f["kind"], "ruleIndex": list(rules).index(f["kind"]),
                        "level": "warning" if f["id"] in failing else "note",
                        "message": {"text": f"{f['title']} {f['basis']}."},
                        "locations": [{"physicalLocation": loc}],
                        "partialFingerprints": {"archplanFinding/v1": f["id"]},
                        "properties": {k: f[k] for k in ("weight", "strength", "state", "in_diff") if k in f}})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "archplan", "version": __version__,
                                          "informationUri": "https://github.com/vloothuis/archplan",
                                          "rules": list(rules.values())}},
                      "results": results}]}


def repo_paths(p: Project, base: Path):
    """A function from a model path (relative to base) to a path from the
    repository root; None gives the project's config.json."""
    try:
        top = toplevel(p.root).resolve()
    except ProjectError:
        top = p.root

    def fn(path):
        full = (Path(base) / path) if path else p.path("config.json")
        try:
            return full.resolve().relative_to(top).as_posix()
        except ValueError:
            return path or ".archplan/config.json"
    return fn


def tighten_notice(rat: dict) -> str | None:
    if not rat["stored"]:
        return None
    better = [r["metric"] for r in rat["rows"] if r["status"] == "better"]
    if not better:
        return None
    return (f"{plural(len(better), 'ratchet metric')} got better ({', '.join(better)}). "
            "Run `archplan check --tighten` in this branch and commit .archplan/baseline.json to keep them.")


def write_summary(path: str, text: str, append: bool) -> None:
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)


def write_sarif(path: str, data: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
