"""Hotspots and logical coupling from git history.

A hotspot is a component that changes often. Two components are coupled when
they change in the same commits. Coupled components without a dependency in
the model are hidden coupling: something ties them together that the code's
imports do not show.

The same is done per file for the files the model knows (its files section):
pairs of files that change together, with the commits behind them and their
subjects. The history.modularity_violation detector compares them with the
code's file references, and counts bug fixes from the subjects. It never runs
git itself.

Only git itself runs. Settings that would make git run other programs
(fsmonitor, hooks, signature checks, diff drivers) are switched off."""

from __future__ import annotations

import fnmatch
import subprocess
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from .project import Project, ProjectError, etag, read_json, write_json
from .safeio import GIT_SAFE

MAX_COMMITS = 3000
MAX_FILES = 50  # a commit that touches more files is a bulk change: skipped
MIN_TOGETHER = 3
MIN_DEGREE = 0.3
MAX_PAIRS = 200
MAX_OWNERS = 30  # authors kept per component, with their commit counts, for the ownership detector
VERSION = 3      # of the cached result: 2 added owners, 3 added cochange

# File-level co-change (cochange in the result). A commit with more files is a
# bulk change for this purpose; ignored files do not count towards its size.
FILE_MAX_FILES = 25
FILE_MIN_TOGETHER = 3
FILE_MIN_DEGREE = 0.3
MAX_FILE_PAIRS = 600
MAX_FILE_SHAS = 100    # commits kept per file and per pair
SUBJECT_MAX = 120
# Files that change with everything for reasons outside the design: lockfiles,
# vendored and generated code, migrations, translations, changelogs.
IGNORE = ("*.lock", "*-lock.json", "*-lock.yaml", "go.sum", "uv.lock", "Pipfile.lock", "*/vendor/*", "vendor/*",
          "*/node_modules/*", "*/third_party/*", "*.min.js", "*.min.css", "*.map", "*_pb2.py", "*_pb2_grpc.py",
          "*.pb.go", "*.generated.*", "*.gen.*", "*/generated/*", "*/__generated__/*", "*/migrations/*",
          "*/__snapshots__/*", "*.snap", "*.po", "*.pot", "CHANGELOG*", "*/CHANGELOG*", "*.svg", "*.png")

SAFE = GIT_SAFE


class NotGit(ProjectError):
    pass


def git(cwd: Path, *args: str, timeout: float = 120) -> str:
    try:
        r = subprocess.run(["git", *SAFE, *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NotGit(f"git failed: {e}") from e
    if r.returncode != 0:
        raise NotGit(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout


def toplevel(root: Path) -> Path:
    return Path(git(root, "rev-parse", "--show-toplevel").strip())


def info(root: Path) -> dict | None:
    """The current branch and short HEAD, or None outside a git repository."""
    try:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=5).strip()
        head = git(root, "rev-parse", "--short", "HEAD", timeout=5).strip()
    except NotGit:
        return None
    return {"branch": None if branch == "HEAD" else branch, "head": head}  # no branch when detached


def history(top: Path, since_days: int, max_commits: int = MAX_COMMITS) -> list:
    """Commits newest first: (sha, author email, iso date, [paths], subject).
    The period ends at the HEAD commit, not today, so a repository that has
    been quiet for a while still has a history to read."""
    try:
        end = int(git(top, "log", "-1", "--format=%ct", "HEAD", timeout=10).strip())
    except (NotGit, ValueError):
        end = int(time.time())
    out = git(top, "log", "--no-merges", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
              f"--since=@{end - int(since_days) * 86400}", f"--max-count={int(max_commits)}",
              "--name-only", "--format=%x1e%H%x1f%aE%x1f%aI%x1f%s", timeout=300)
    commits = []
    for chunk in out.split("\x1e")[1:]:
        head, _, files = chunk.partition("\n")
        sha, email, date, subject = (head.split("\x1f", 3) + ["", "", ""])[:4]
        commits.append((sha, email.lower(), date, [f for f in files.splitlines() if f.strip()], subject.strip()))
    return commits


class Mapper:
    """Maps a file path (relative to the model's root) to a component.

    A component's `match` patterns decide first: an exact file, a `dir/**`
    prefix, or another glob. The longest literal part wins. Files no pattern
    covers fall back to the longest component `path` that contains them."""

    def __init__(self, components: dict):
        self.rules = []  # (literal length, kind, pattern, component)
        self.paths = []  # (path, component)
        for cid, c in components.items():
            if c.get("kind") == "external":
                continue
            for pat in c.get("match") or []:
                pat = pat.strip("/")
                if pat.endswith("/**") and "*" not in pat[:-3]:
                    self.rules.append((len(pat) - 3, "dir", pat[:-3], cid))
                elif "*" in pat or "?" in pat or "[" in pat:
                    lit = len(pat.split("*")[0].split("?")[0].split("[")[0])
                    self.rules.append((lit, "glob", pat, cid))
                else:
                    self.rules.append((len(pat) + 1, "file", pat, cid))
            if c.get("path"):
                self.paths.append((c["path"].strip("/"), cid))
        # A path that several components share says nothing about which one owns a file.
        shared = Counter(path for path, _ in self.paths)
        self.paths = [x for x in self.paths if shared[x[0]] == 1]
        self.rules.sort(key=lambda r: -r[0])
        self.paths.sort(key=lambda r: -len(r[0]))
        self.cache = {}

    def __call__(self, f: str) -> str | None:
        if f not in self.cache:
            self.cache[f] = self.lookup(f)
        return self.cache[f]

    def lookup(self, f: str) -> str | None:
        for _, kind, pat, cid in self.rules:
            if (kind == "file" and f == pat) or (kind == "dir" and f.startswith(pat + "/")) \
                    or (kind == "glob" and fnmatch.fnmatchcase(f, pat)):
                return cid
        for path, cid in self.paths:
            if f == path or f.startswith(path + "/"):
                return cid
        return None


def best_prefix(files, mapper: Mapper, hint: str = "") -> str:
    """The directory to strip from git paths so they line up with the model's
    paths. A model exported in a subdirectory (say with `-C core`) has paths
    like lib/x.ex, while git reports core/lib/x.ex. Tries no prefix, hint (the
    project root inside the repository), and the first one or two directories
    of the files; keeps the one that maps the most files."""
    files = sorted(files)[:2000]
    dirs = Counter()
    for f in files:
        parts = f.split("/")
        for n in (1, 2):
            if len(parts) > n:
                dirs["/".join(parts[:n]) + "/"] += 1
    cands = {"", hint} | {d for d, _ in dirs.most_common(12)}

    def score(pre):
        return sum(1 for f in files if f.startswith(pre) and mapper(f[len(pre):]))
    return max(sorted(cands, key=lambda c: (len(c), c)), key=score)


def analyse(commits: list, model: dict, prefix: str | None = None, hint: str = "",
            max_files: int = MAX_FILES, min_together: int = MIN_TOGETHER, min_degree: float = MIN_DEGREE,
            max_pairs: int = MAX_PAIRS) -> dict:
    """Change counts per component and coupled pairs, from commits as history()
    returns them."""
    mapper = Mapper(model.get("components") or {})
    used = [c for c in commits if len(c[3]) <= max_files]
    if prefix is None:
        prefix = best_prefix({f for c in used for f in c[3]}, mapper, hint)
    n_commits, authors, files, last = Counter(), defaultdict(Counter), defaultdict(set), {}
    together, unmapped = Counter(), set()
    for sha, email, date, paths, *_ in used:
        comps = set()
        for f in paths:
            cid = mapper(f[len(prefix):]) if f.startswith(prefix) else None
            if cid is None:
                unmapped.add(f)
                continue
            comps.add(cid)
            files[cid].add(f)
        for cid in comps:
            n_commits[cid] += 1
            authors[cid][email] += 1
            last.setdefault(cid, date)  # newest first
        together.update(combinations(sorted(comps), 2))
    deps = {(d["from"], d["to"]) for d in model.get("dependencies") or []}
    pairs = []
    for (a, b), n in together.items():
        deg = n / min(n_commits[a], n_commits[b])
        if n >= min_together and deg >= min_degree:
            pairs.append({"a": a, "b": b, "together": n, "degree": round(deg, 3),
                          "dependency": (a, b) in deps or (b, a) in deps})
    pairs.sort(key=lambda x: (-x["together"] * x["degree"], x["a"], x["b"]))
    return {"commits": len(used), "skipped": len(commits) - len(used), "unmapped": len(unmapped),
            "prefix": prefix,
            "components": {cid: {"commits": n, "authors": len(authors[cid]), "files": len(files[cid]),
                                 "last": last[cid]} for cid, n in n_commits.most_common()},
            "pairs": pairs[:max_pairs],
            "owners": {cid: dict(authors[cid].most_common(MAX_OWNERS)) for cid in n_commits},
            "cochange": cochange(commits, model, prefix)}


def ignored(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pat)
               for pat in IGNORE)


def cochange(commits: list, model: dict, prefix: str = "", max_files: int = FILE_MAX_FILES,
             min_together: int = FILE_MIN_TOGETHER, min_degree: float = FILE_MIN_DEGREE,
             max_pairs: int = MAX_FILE_PAIRS) -> dict:
    """Pairs of model files that change in the same commits.

    Only files in the model's files section count (paths as the model has
    them), so tests, templates and configuration stay out: the model says
    nothing about their structure. A commit whose files, ignored ones left
    out, number more than max_files is skipped. A pair needs min_together
    shared commits, and degree (shared commits over the commits of the file
    that changes least) of at least min_degree. The strongest max_pairs are
    kept, with their commits; subjects lists those commits' subjects, so a
    detector can count bug fixes without git.

    {"commits", "skipped", "max_files", "files": {path: {"commits", "shas"}},
     "pairs": [{"a", "b", "together", "degree", "shas"}], "subjects": {sha: subject}}"""
    known = set(model.get("files") or {})
    n_file, shas_of, together, skipped, used = Counter(), defaultdict(list), defaultdict(list), 0, 0
    for c in commits:
        sha, paths = c[0][:10], c[3]
        rel = [f[len(prefix):] for f in paths if f.startswith(prefix)]
        size = sum(1 for f in rel if not ignored(f))
        if size > max_files:
            skipped += 1
            continue
        fs = sorted({f for f in rel if f in known and not ignored(f)})
        if not fs:
            continue
        used += 1
        for f in fs:
            n_file[f] += 1
            shas_of[f].append(sha)
        for pair in combinations(fs, 2):
            together[pair].append(sha)
    pairs = []
    for (a, b), shas in together.items():
        n = len(shas)
        deg = n / min(n_file[a], n_file[b])
        if n >= min_together and deg >= min_degree:
            pairs.append({"a": a, "b": b, "together": n, "degree": round(deg, 3), "shas": shas[:MAX_FILE_SHAS]})
    pairs.sort(key=lambda x: (-x["together"] * x["degree"], x["a"], x["b"]))
    pairs = pairs[:max_pairs]
    in_pairs = sorted({f for x in pairs for f in (x["a"], x["b"])})
    files = {f: {"commits": n_file[f], "shas": shas_of[f][:MAX_FILE_SHAS]} for f in in_pairs}
    wanted = {s for x in pairs for s in x["shas"]} | {s for v in files.values() for s in v["shas"]}
    subjects = {c[0][:10]: c[4][:SUBJECT_MAX] for c in commits if len(c) > 4 and c[0][:10] in wanted}
    return {"commits": used, "skipped": skipped, "max_files": max_files, "files": files, "pairs": pairs,
            "subjects": subjects}


def hotspots(p: Project, since_days: int = 365, *, refresh: bool = False, **kw) -> dict:
    """The analysis for this project, from .archplan/hotspots.json when HEAD,
    the period, the model and the thresholds are unchanged. Raises NotGit
    outside a git repository."""
    top = toplevel(p.root)
    head = git(top, "rev-parse", "HEAD").strip()
    key = {"v": VERSION, "head": head, "since_days": int(since_days), "model": etag(p.model_path),
           **{k: kw[k] for k in sorted(kw)}}
    cache = p.path("hotspots.json")
    old = read_json(cache) or {}
    if not refresh and old.get("key") == key:
        return old["result"]
    model = read_json(p.model_path) or p.target_or_model() or {}
    t = time.monotonic()
    commits = history(top, since_days)
    rel = p.root.relative_to(top).as_posix() if p.root != top else ""
    res = {"head": head, "since_days": int(since_days),
           **analyse(commits, model, hint=rel + "/" if rel else "", **kw),
           "seconds": round(time.monotonic() - t, 2)}
    write_json(cache, {"key": key, "result": res})
    return res


def summary(res: dict, limit: int = 10) -> dict:
    """The top hotspots and couplings, small enough for an agent's context."""
    comps = sorted(res["components"].items(), key=lambda kv: -kv[1]["commits"])
    return {"head": res["head"][:10], "since_days": res["since_days"], "commits": res["commits"],
            "skipped_bulk_commits": res["skipped"], "unmapped_files": res["unmapped"],
            "hotspots": [{"component": c, **v} for c, v in comps[:limit]],
            "hidden_coupling": [x for x in res["pairs"] if not x["dependency"]][:limit],
            "coupling_with_dependency": [x for x in res["pairs"] if x["dependency"]][:limit]}


def text(res: dict, limit: int = 10) -> str:
    s = summary(res, limit)
    lines = [f"{s['commits']} commits in the last {s['since_days']} days (HEAD {s['head']}); "
             f"{s['skipped_bulk_commits']} bulk commits skipped, {s['unmapped_files']} files outside any component.",
             "", "Hotspots (commits, authors, files, last change):"]
    for h in s["hotspots"]:
        lines.append(f"  {h['commits']:5}  {h['authors']:3}  {h['files']:5}  {h['last'][:10]}  {h['component']}")
    lines += ["", "Hidden coupling: change together, no dependency in the model (together, degree):"]
    for x in s["hidden_coupling"] or []:
        lines.append(f"  {x['together']:5}  {x['degree']:.2f}  {x['a']} <-> {x['b']}")
    if not s["hidden_coupling"]:
        lines.append("  none above the thresholds")
    return "\n".join(lines)
