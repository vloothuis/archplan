"""A project is a code repository with an .archplan directory.

The directory holds everything archplan shares between the human (in the
editor) and an agent (over MCP):

  config.json            how to export the code's model, which port to use, auto suggestion mode
  model.archmodel.json   the as-is model, written by the exporter
  target.archmodel.json  the target model the human (or agent) edits
  base.archmodel.json    the as-is model the target was last edited against
  state.json             target revision, who wrote it last, the plan intent
  plan.md, plan.json     the change plan, written by the editor
  threads.json           comment threads between human and agent
  suggestions.json       target edits the agent proposed, and the human's decisions
  handovers.json         planned refactors the human sent the agent, with batches and progress
  activity.json          the agent's latest MCP call, for the editor; not committed
  hotspots.json          cached analysis of the git history; not committed
  architecture.json      the architecture profile: units, roles, rules, layers
  baseline.json          findings the human accepted or dismissed, and Claude's labels on findings
  vocabulary.json        terms to leave out of the glossary candidates
  vocab-cache.json       per-file word counts for the candidates; not committed
  suggest-state.json     when archplan last asked for refactor proposals; not committed
  server.json            the running editor server (url, pid); not committed
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DIR = ".archplan"
DEFAULT_PORT = 8765

GITIGNORE = """\
# Local runtime files. Commit the rest to share the target, plan and threads.
server.json
server.log
.hook-seen.json
activity.json
hotspots.json
vocab-cache.json
suggest-state.json
*.tmp
.*.lock
"""


class ProjectError(Exception):
    pass


class UntrustedExport(ProjectError):
    pass


def trust_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "archplan" / "trusted.json"


def command_hash(root: Path, cmd: str) -> str:
    return hashlib.sha256(f"{root}\0{cmd}".encode()).hexdigest()


# A trust record approves a command for a directory, plus the content of the
# repository files the command names. In trusted.json, "commands" and
# "detectors" list the approved command hashes and "files" maps a hash to
# {repository path: sha256}. When one of those files changes (after a git
# pull, say), the command needs a new approval.

OUTPUT_FLAGS = {"-o", "--output", "--out"}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path}: not a regular file")
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def arg_files(root: Path, cmd: str) -> dict:
    """{repository path: sha256} for each argument of cmd that names a regular
    file inside root, relative to root. An argument such as --script=x.py
    counts with its value; the output after -o does not count."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return {}
    base = Path(root).resolve()
    out = {}
    for i, t in enumerate(toks):
        if i and toks[i - 1] in OUTPUT_FLAGS:
            continue
        for cand in {t, t.split("=", 1)[1] if "=" in t else ""}:
            if not cand or len(cand) > 4096:
                continue
            try:
                fp = (base / cand).resolve()
                rel = fp.relative_to(base).as_posix()
                if rel == "." or not stat.S_ISREG(os.stat(fp).st_mode):
                    continue
                out[rel] = file_sha256(str(fp))
            except (OSError, ValueError, RuntimeError):
                continue
    return out


def load_trust() -> dict:
    try:
        data = read_json(trust_path(), {}, follow=True) or {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def trust_state(root: Path, kind: str, key: str, cmd: str) -> tuple:
    """(approved, changed): whether key is approved under kind ("commands" or
    "detectors") and the files named in cmd still match; changed lists the
    repository files that differ from the approval."""
    data = load_trust()
    if key not in (data.get(kind) or []):
        return False, []
    stored = (data.get("files") or {}).get(key) or {}
    cur = arg_files(root, cmd)
    changed = sorted(f for f in set(stored) | set(cur) if stored.get(f) != cur.get(f))
    return not changed, changed


def record_trust(root: Path, kind: str, key: str, cmd: str) -> None:
    data = load_trust()
    lst = data.setdefault(kind, [])
    if key not in lst:
        lst.append(key)
    data.setdefault("files", {})[key] = arg_files(root, cmd)
    tp = trust_path()
    tp.parent.mkdir(parents=True, exist_ok=True)
    write_json(tp, data)


def changed_note(changed: list) -> str:
    return ("; changed since it was approved: " + ", ".join(changed[:10])) if changed else ""


def find_root(start: Path | None = None) -> Path | None:
    """The nearest directory, from start upwards, that has an .archplan directory."""
    p = (start or Path.cwd()).resolve()
    for d in (p, *p.parents):
        if (d / DIR / "config.json").is_file():
            return d
    return None


def write_atomic(p: Path, text: str) -> None:
    """Write p through a new temporary file, so a planted symlink at p or at a
    fixed temporary name cannot redirect the write outside the repository."""
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(p: Path, obj) -> None:
    write_atomic(p, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


MAX_JSON = 256 << 20


def read_json(p: Path, default=None, follow: bool = False):
    """The JSON in p, or default when p does not exist. Files in .archplan are
    repository data: p must be a regular file of at most MAX_JSON bytes, and
    not a symlink unless follow is set (for files outside the repository)."""
    from .safeio import read_bytes
    try:
        target = os.path.realpath(p) if follow else p
        return json.loads(read_bytes(target, MAX_JSON))
    except FileNotFoundError:
        return default


def append_ignore(p: "Project", name: str) -> None:
    """Add name to .archplan/.gitignore when it is not listed there yet."""
    from .safeio import read_file
    gi = p.path(".gitignore")
    try:
        text = read_file(gi, 1 << 16)
    except OSError:
        return
    if name not in text.split():
        write_atomic(gi, text.rstrip("\n") + "\n" + name + "\n")


def etag(p: Path) -> str | None:
    """A cheap change marker for a file: size and modification time."""
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return f"{st.st_size:x}-{st.st_mtime_ns:x}"


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.dir = self.root / DIR
        self.config = read_json(self.dir / "config.json", {}) or {}

    @classmethod
    def find(cls, start: Path | None = None, dir: str | None = None) -> "Project":
        """The project at dir (a project root or its .archplan directory), or the
        nearest one above start."""
        if dir:
            d = Path(dir).resolve()
            root = d.parent if d.name == DIR else d
            if not (root / DIR / "config.json").is_file():
                raise ProjectError(f"no archplan project at {root} (run `archplan init` there)")
            return cls(root)
        if start is None and os.environ.get("CLAUDE_PROJECT_DIR"):
            start = Path(os.environ["CLAUDE_PROJECT_DIR"])
        root = find_root(start)
        if root is None:
            raise ProjectError("no .archplan directory here or above; run `archplan init` in the repository root")
        return cls(root)

    # paths

    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def model_path(self) -> Path:
        return self.root / self.config.get("model", f"{DIR}/model.archmodel.json")

    @property
    def target_path(self) -> Path:
        return self.path("target.archmodel.json")

    @property
    def base_path(self) -> Path:
        return self.path("base.archmodel.json")

    @property
    def name(self) -> str:
        return self.config.get("name") or self.root.name

    @property
    def port(self) -> int:
        return int(self.config.get("port", DEFAULT_PORT))

    # state

    def state(self) -> dict:
        return read_json(self.path("state.json"), {}) or {}

    def set_state(self, **kw) -> dict:
        st = self.state()
        st.update(kw)
        write_json(self.path("state.json"), st)
        return st

    def save_target(self, target: dict, by: str, base_etag: str | None = None) -> int:
        """Write the target model and bump its revision. by is 'editor' or 'agent'.

        base_etag is the as-is model the editor edited against. When it is the
        current model, a copy becomes the base, so the editor can later rebase
        the target onto a newer export."""
        write_json(self.target_path, target)
        st = self.state()
        rev = int(st.get("target_rev", 0)) + 1
        upd = {"target_rev": rev, "target_by": by}
        cur = etag(self.model_path)
        from .safeio import read_file
        if base_etag and base_etag == cur and st.get("base_etag") != cur:
            write_atomic(self.base_path, read_file(self.model_path, MAX_JSON))
            upd["base_etag"] = cur
        self.set_state(**upd)
        return rev

    def target_or_model(self) -> dict | None:
        """The target model, or the as-is model when nobody edited one yet."""
        return read_json(self.target_path) or read_json(self.model_path)

    # export

    def export_command(self) -> str | None:
        return self.config.get("export")

    # The export command comes from config.json, which a cloned repository
    # supplies. It runs in a shell, so the agent (refresh_model) and the editor
    # (From code) run it only after a person approved this exact command for this
    # directory, outside the repository: `archplan init`, `archplan trust`, or
    # answering yes in `archplan export`.

    def trusted(self) -> bool:
        return self.trust_state()[0]

    def trust_state(self) -> tuple:
        """(approved, changed files): see project.trust_state."""
        cmd = self.export_command()
        if not cmd:
            return False, []
        return trust_state(self.root, "commands", command_hash(self.root, cmd), cmd)

    def trust(self) -> None:
        cmd = self.export_command()
        if not cmd:
            raise ProjectError("no export command to trust")
        record_trust(self.root, "commands", command_hash(self.root, cmd), cmd)

    def export(self, timeout: float = 600, require_trust: bool = False) -> dict:
        """Run the configured exporter in the project root. The command writes the
        as-is model to the configured model path (or prints it on stdout)."""
        cmd = self.export_command()
        if not cmd:
            raise ProjectError(f"no export command in {self.dir / 'config.json'}")
        ok, changed = self.trust_state() if require_trust else (True, [])
        if not ok:
            raise UntrustedExport(
                f"the export command in .archplan/config.json is not approved on this machine: {cmd!r}"
                f"{changed_note(changed)}. "
                "A person must run `archplan trust` (or `archplan export`) in a terminal once.")
        env = os.environ.copy()
        pkg_parent = str(Path(__file__).resolve().parents[2])
        if cmd.startswith("archplan "):
            # Our own exporter: run it with this interpreter, so it works when
            # archplan is not on the PATH (as in a plugin's bin/). Isolated mode
            # (-I) keeps the repository's own files off sys.path, so a planted
            # archplan package in the root cannot take its place.
            boot = f"import sys; sys.path.insert(0, {pkg_parent!r}); from archplan.cli import main; sys.exit(main())"
            cmd = f"{shlex.quote(sys.executable)} -I -c {shlex.quote(boot)} {cmd[len('archplan '):]}"
        before = etag(self.model_path)
        r = subprocess.run(cmd, shell=True, cwd=self.root, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise ProjectError(f"export failed ({r.returncode}): {cmd}\n{r.stderr.strip()[-2000:]}")
        if etag(self.model_path) == before and r.stdout.lstrip().startswith("{"):
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.model_path, r.stdout)
        m = read_json(self.model_path)
        if not m or m.get("format") != "archmodel/1":
            raise ProjectError(f"export did not produce an archmodel/1 file at {self.model_path}")
        return counts(m)


def counts(m: dict) -> dict:
    return {k: len(m.get(k) or {}) for k in ("components", "dependencies", "types", "sequences")}


def detect_export(root: Path) -> str | None:
    """A default export command, from the exporter registry (contrib/exporters
    and the project's .archplan/exporters): the highest-priority match that is
    installed, else export-lite, else the highest-priority match."""
    return detect(root)["command"]


def detect(root: Path) -> dict:
    from .exporters import survey
    return survey(root, root / DIR, f"{DIR}/model.archmodel.json")


JS_EXT = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"}
JS_SKIP = {"node_modules", "dist", "build", "coverage", "out", "vendor"}


def js_sources(d: Path, limit: int = 5000) -> int:
    """The number of JS and TS source files under d, outside node_modules and
    build output, counted up to limit."""
    n = 0
    for dirpath, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in JS_SKIP and not x.startswith(".")]
        n += sum(1 for x in names if os.path.splitext(x)[1] in JS_EXT and not x.endswith(".config.js"))
        if n >= limit:
            break
    return n


def is_js_project(d: Path) -> bool:
    return (d / "package.json").is_file() and ((d / "tsconfig.json").is_file() or js_sources(d, 1) > 0)


def js_dir(root: Path) -> Path | None:
    """The TypeScript or JavaScript project: the root when it has package.json
    and tsconfig.json or sources, or else the direct subdirectory (such as
    frontend/) that is one and has the most source files."""
    if is_js_project(root):
        return Path(".")
    subs = [d for d in sorted(root.iterdir())
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and d.name not in JS_SKIP
            and is_js_project(d)]
    if not subs:
        return None
    return max(subs, key=js_sources).relative_to(root)


def mix_dir(root: Path) -> Path | None:
    """The Mix project: the root, or else the direct subdirectory with a mix.exs
    and the most .ex files under lib/."""
    if (root / "mix.exs").is_file():
        return Path(".")
    subs = [d for d in sorted(root.iterdir())
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and (d / "mix.exs").is_file()]
    if not subs:
        return None
    from .safeio import files
    return max(subs, key=lambda d: len(files(d / "lib", ".ex"))).relative_to(root)


def init(root: Path, export: str | None = None, port: int | None = None, force: bool = False) -> Project:
    d = root.resolve() / DIR
    cfg_path = d / "config.json"
    if cfg_path.exists() and not force:
        raise ProjectError(f"{cfg_path} exists (use --force to overwrite)")
    d.mkdir(parents=True, exist_ok=True)
    found = None if export else detect(root.resolve())
    cfg = {"name": root.resolve().name, "model": f"{DIR}/model.archmodel.json",
           "export": export or found["command"], "port": port or DEFAULT_PORT}
    write_json(cfg_path, cfg)
    # Approved here: a command the person typed, or a built-in exporter that only
    # reads the code. Not approved: a command from the repository's own
    # .archplan/exporters, or a built-in exporter that runs repository code (the
    # project's typescript package, mix.exs, the go toolchain). The person
    # approves those with `archplan trust`.
    if cfg["export"] and (export or (found["trusted_source"] and not found["runs_repo_code"])):
        Project(root).trust()
    gi = d / ".gitignore"
    if not os.path.lexists(gi):
        write_atomic(gi, GITIGNORE)
    p = Project(root)
    p.found = found
    return p
