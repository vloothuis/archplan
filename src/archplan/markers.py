"""The built-in annotator: declarations in marker comments and GLOSSARY.md.

A marker comment sits directly above a declaration, with decorators,
attributes and other comments allowed in between:

  # archplan: auth=none reason="load balancer health check"
  @require_GET
  def health(request): ...

`guard` declares function.guard, `auth=required|none` entrypoint.auth. A
component's zone is the `zone:` line in the front matter of the GLOSSARY.md
in its folder. See format/annotator-plugins.md.

read() returns declarations in the plugin output shape; write() changes
files under a root (the scratch copy) and returns one result per change.
Files are split on \\n only, keeping a \\r, so line numbers match the
tokenizer and a rewrite keeps the file's line endings.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tokenize
from pathlib import Path

from .core.safeio import inside, model_base, prefix_of, read_file
from .core.vocab import GLOSSARY, _scalar

MAX_FILE = 1 << 20
MAX_FILES = 50_000
LOOKAHEAD = 30  # lines between a marker and its declaration

HASH = {".py", ".pyi", ".rb", ".rake", ".ex", ".exs"}
SLASH = {".go", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".java", ".kt", ".kts", ".scala",
         ".cs", ".rs", ".swift", ".php", ".dart", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".groovy"}

MARKER = re.compile(r"^(?P<indent>[ \t]*)(?:#|//)[ \t]*archplan:[ \t]*(?P<body>.*?)\s*$")
TOKEN = re.compile(r'([a-z_]+)(?:=("(?:[^"\\]|\\.)*"|\S+))?')
BREAKS = re.compile("[\x00-\x1f\x7f\x85  ]")
AUTH = ("required", "none")
NOT_NAMES = frozenset("if for while switch return catch elif else when case unless until match new".split())
STATEMENTS = frozenset("return await yield throw raise new else elif if for while do case when not and or in is "
                       "print puts assert del go defer".split())
DECLS = [
    re.compile(r"^\s*(?:async\s+)?def\s+(?:self\.)?([A-Za-z_]\w*[?!]?)"),  # Python, Ruby
    re.compile(r"^\s*def(?:p|macro|macrop)?\s+([a-z_]\w*[?!]?)"),  # Elixir
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),  # Go
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=]"),
    re.compile(r"^\s*(?:(?:export|default|public|private|protected|internal|abstract|final|sealed|open|data)\s+)*"
               r"(?:class|interface|struct|trait|enum|object|module|defmodule|type)\s+([A-Za-z_][\w.]*)"),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"),
]
# A method or function with at least one modifier or type before its name,
# such as `public User find(` or `async requireUser(`; not a call like `find(x)`.
GENERIC = re.compile(r"^\s*((?:[\w<>\[\],.?]+\s+)+)([A-Za-z_]\w*)\s*\(")


def comment_prefix(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in HASH:
        return "#"
    if ext in SLASH:
        return "//"
    return None


def split(text: str) -> tuple:
    """(lines, eol, trailing newline): lines keep a \\r; eol is the file's line ending."""
    lines = text.split("\n") if text else []
    trailing = text.endswith("\n")
    if trailing:
        lines.pop()
    crlf = sum(1 for x in lines if x.endswith("\r"))
    return lines, "\r\n" if lines and crlf * 2 > len(lines) else "\n", trailing


def save(fp: Path, lines: list, trailing: bool) -> None:
    with open(fp, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + ("\n" if trailing else ""))


def declared_name(line: str) -> str | None:
    """The name a line declares, or None when it is no declaration."""
    for rx in DECLS:
        m = rx.match(line)
        if m:
            return m.group(1) if m.group(1) not in NOT_NAMES else None
    m = GENERIC.match(line)
    if m and m.group(2) not in NOT_NAMES and m.group(1).split()[0] not in STATEMENTS:
        return m.group(2)
    return None


# ---------- files ----------

def list_files(root: Path, exclude=None) -> list:
    """Files under root relative to it: those git tracks and untracked ones it
    does not ignore, else a walk. exclude is a core.exclude.Exclude."""
    out = None
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        if r.returncode == 0:
            out = [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]
    except (OSError, subprocess.SubprocessError):
        pass
    if out is None:
        out = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "_build", "deps",
                                                                             "vendor", "__pycache__")]
            for f in files:
                out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    out = [f for f in out if comment_prefix(f) and not (exclude and exclude.match(f))]
    return sorted(out)[:MAX_FILES]


# ---------- markers ----------

def parse_marker(body: str) -> tuple:
    """({key: value}, reason, unknown tokens) for the text after `archplan:`."""
    keys, reason, bad = {}, None, []
    for m in TOKEN.finditer(body):
        k, v = m.group(1), m.group(2)
        if v is not None and v.startswith('"'):
            v = _scalar(v)
        if k == "guard" and v is None:
            keys["function.guard"] = True
        elif k == "auth" and v in AUTH:
            keys["entrypoint.auth"] = v
        elif k == "reason" and v:
            reason = v
        else:
            bad.append(m.group(0))
    return keys, reason, bad


def is_skippable(line: str, prefix: str) -> bool:
    s = line.strip()
    return s.startswith(prefix) or s.startswith("@") or s.startswith("#[") or s.startswith("[") \
        or s.startswith("/*") or s.startswith("*")


def depth(s: str) -> int:
    """Open brackets minus closed ones, outside simple string literals."""
    d, q = 0, None
    for ch in s:
        if q:
            q = None if ch == q else q
        elif ch in "\"'":
            q = ch
        elif ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
    return d


def skip(lines: list, start: int, prefix: str) -> int | None:
    """The index of the first line from start that is not a comment,
    decorator or attribute (with its continuation lines and an Elixir @doc
    \"\"\" block); None at a blank line or after LOOKAHEAD lines."""
    j, heredoc, open_ = start, False, 0
    while j < len(lines) and j <= start + LOOKAHEAD:
        s = lines[j].strip()
        if heredoc:
            heredoc = s.count('"""') % 2 == 0
        elif open_ > 0:
            open_ += depth(s)
        elif not s:
            return None
        elif is_skippable(lines[j], prefix):
            heredoc = s.startswith("@") and s.count('"""') % 2 == 1
            open_ = max(depth(s), 0) if not s.startswith(prefix) else 0
        else:
            return j
        j += 1
    return None


def declaration(lines: list, i: int, prefix: str) -> tuple | None:
    """(line index, name) of the declaration a marker at index i belongs to."""
    j = skip(lines, i + 1, prefix)
    name = declared_name(lines[j]) if j is not None else None
    return (j, name) if name else None


def python_comments(text: str) -> set | None:
    """The 0-based indexes of lines that start with a comment, by the Python
    tokenizer, so a marker inside a string does not count; None when the
    text does not tokenize."""
    out = set()
    try:
        for t in tokenize.generate_tokens(io.StringIO(text, newline="").readline):
            if t.type == tokenize.COMMENT and not t.line[:t.start[1]].strip():
                out.add(t.start[0] - 1)
    except (tokenize.TokenError, SyntaxError):
        return None
    return out


def is_marker(line: str, prefix: str):
    m = MARKER.match(line)
    return m if m and line.lstrip().startswith(prefix) else None


def scan(text: str, prefix: str, python: bool = False) -> list:
    """[(marker index, indent, keys, reason, bad, declaration index, name)] in one file."""
    lines = split(text)[0]
    comments = python_comments(text) if python else None
    out = []
    for i, line in enumerate(lines):
        if "archplan:" not in line:
            continue
        m = is_marker(line, prefix)
        if not m or (comments is not None and i not in comments):
            continue
        keys, reason, bad = parse_marker(m.group("body"))
        d = declaration(lines, i, prefix)
        out.append((i, m.group("indent"), keys, reason, bad, *(d or (None, None))))
    return out


# ---------- GLOSSARY.md zones ----------

def glossary_path(base: Path, comp: dict) -> str | None:
    """The GLOSSARY.md next to a component's code, relative to base."""
    path = ((comp or {}).get("path") or "").strip("/")
    if not path or ".." in path.split("/"):
        return None
    fp = base / path
    folder = fp if fp.is_dir() or not fp.suffix else fp.parent
    try:
        rel = (folder / GLOSSARY).relative_to(base).as_posix()
    except ValueError:
        return None
    return rel if inside(base, rel) is not None else None


def front_bounds(lines: list) -> tuple | None:
    """(first, closing) line index of the front matter, or None."""
    if not lines or lines[0].rstrip() != "---":
        return None
    for j in range(1, len(lines)):
        if lines[j].rstrip() == "---":
            return 0, j
    return None


ZONE_LINE = re.compile(r"^zone:\s*(.*?)\s*(?:#.*)?$")


def zone_of(text: str) -> tuple | None:
    """(zone, line number) from GLOSSARY.md front matter."""
    lines = split(text)[0]
    b = front_bounds(lines)
    if not b:
        return None
    for j in range(1, b[1]):
        m = ZONE_LINE.match(lines[j])
        if m:
            return _scalar(m.group(1)), j + 1
    return None


# ---------- read ----------

def read(root: Path, model: dict, keys, exclude=None) -> tuple:
    """(declarations, warnings) for the keys the built-in annotator owns."""
    root = Path(root)
    base = model_base(root, model)
    pre = prefix_of(root, base)
    decls, warnings = [], []
    keys = set(keys)
    if "component.zone" in keys:
        for cid, c in sorted((model.get("components") or {}).items()):
            if (c or {}).get("kind") == "external":
                continue
            rel = glossary_path(base, c)
            fp = rel and inside(base, rel)
            if not fp or not fp.is_file():
                continue
            try:
                z = zone_of(read_file(fp, MAX_FILE, errors="strict"))
            except (OSError, UnicodeDecodeError):
                continue
            if z:
                decls.append({"target": {"kind": "component", "id": cid}, "key": "zone", "value": z[0],
                              "file": rel, "line": z[1]})
    if keys & {"function.guard", "entrypoint.auth"}:
        for f in list_files(root, exclude):
            prefix = comment_prefix(f)
            try:
                text = read_file(root / f, MAX_FILE, errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            if "archplan:" not in text:
                continue
            rel = pre + f
            for i, _, ks, reason, bad, j, name in scan(text, prefix, f.endswith((".py", ".pyi"))):
                if bad:
                    warnings.append(f"{rel}:{i + 1}: unknown marker {' '.join(bad)!r}")
                if not ks:
                    continue
                if j is None:
                    warnings.append(f"{rel}:{i + 1}: the marker is not directly above a declaration")
                    continue
                for k, v in ks.items():
                    if k not in keys:
                        continue
                    kind, key = k.split(".")
                    decls.append({"target": {"kind": kind, "file": rel, "line": j + 1, "name": name},
                                  "key": key, "value": v, **({"reason": reason} if reason else {}),
                                  "file": rel, "line": i + 1})
    return decls, warnings


# ---------- write ----------

def marker_line(indent: str, prefix: str, path: str, body: str) -> str:
    if path.endswith(".go"):
        return f"{indent}//archplan:{body}"
    return f"{indent}{prefix} archplan: {body}"


def token_of(key: str, value, reason) -> str:
    token = "guard" if key == "function.guard" else f"auth={value}"
    if reason:
        reason = BREAKS.sub(" ", str(reason))  # never a second line
        token += ' reason="' + reason.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return token


def write(root: Path, model: dict, changes: list) -> list:
    """Apply changes under root; one {"status", "note"?} per change."""
    root = Path(root)
    base = model_base(root, model)
    return [write_one(base, model, ch) for ch in changes]


def write_one(base: Path, model: dict, ch: dict) -> dict:
    full = f"{ch['target']['kind']}.{ch['key']}"
    try:
        if full == "component.zone":
            return write_zone(base, model, ch)
        if full in ("function.guard", "entrypoint.auth"):
            return write_marker(base, ch, full)
    except (OSError, UnicodeDecodeError) as e:
        return {"status": "conflict", "note": str(e)[:200]}
    return {"status": "unsupported", "note": f"the built-in annotator does not write {full}"}


def write_zone(base: Path, model: dict, ch: dict) -> dict:
    comp = (model.get("components") or {}).get(ch["target"]["id"])
    rel = glossary_path(base, comp) if comp else None
    fp = rel and inside(base, rel)
    if not fp:
        return {"status": "unsupported", "note": "the component has no folder for a GLOSSARY.md"}
    lines, eol, _ = split(read_file(fp, MAX_FILE, errors="strict") if fp.is_file() else "")
    cr = "\r" if eol == "\r\n" else ""
    b = front_bounds(lines)
    at = None
    if b:
        at = next((j for j in range(1, b[1]) if ZONE_LINE.match(lines[j])), None)
    if ch["op"] == "set":
        line = f"zone: {ch['value']}{cr}"
        if at is not None:
            lines[at] = line
        elif b:
            lines.insert(b[1], line)
        else:
            lines = [f"---{cr}", line, f"---{cr}"] + lines
    else:
        if at is None:
            return {"status": "applied", "note": "no zone was set"}
        del lines[at]
        b = front_bounds(lines)
        if b and all(not x.strip() for x in lines[1:b[1]]):
            lines = lines[b[1] + 1:]
        if not any(x.strip() for x in lines):
            fp.unlink()
            return {"status": "applied"}
    fp.parent.mkdir(parents=True, exist_ok=True)
    save(fp, lines, True)
    return {"status": "applied"}


def find_declaration(lines: list, at: dict) -> int | None:
    """The index of the declaration at names, nearest to its line."""
    name, line = at.get("name"), at.get("line")
    if not name:
        return line - 1 if isinstance(line, int) and 0 < line <= len(lines) else None
    hits = [j for j, text in enumerate(lines) if name in text and declared_name(text) == name]
    if not hits:
        return None
    return min(hits, key=lambda j: abs(j + 1 - line)) if isinstance(line, int) else hits[0]


def preamble(lines: list, j: int, prefix: str) -> int:
    """The first line of the comments, decorators and attributes directly above line j."""
    return min((k for k in range(max(j - LOOKAHEAD, 0), j) if skip(lines, k, prefix) == j), default=j)


def write_marker(base: Path, ch: dict, full: str) -> dict:
    at = ch.get("at") or {}
    rel = at.get("file")
    fp = rel and inside(base, rel)
    if not fp or not fp.is_file():
        return {"status": "unsupported", "note": "archplan does not know where this function is declared"}
    prefix = comment_prefix(rel)
    if not prefix:
        return {"status": "unsupported", "note": f"no comment syntax known for {rel}"}
    lines, eol, trailing = split(read_file(fp, MAX_FILE, errors="strict"))
    cr = "\r" if eol == "\r\n" else ""
    j = find_declaration(lines, at)
    if j is None:
        return {"status": "conflict", "note": f"{at.get('name') or 'the declaration'} is not in {rel} anymore"}
    top, word = preamble(lines, j, prefix), "guard" if full == "function.guard" else "auth"
    existing = []
    for k in range(top, j):
        m = is_marker(lines[k], prefix)
        if m and full in parse_marker(m.group("body"))[0]:
            existing.append((k, parse_marker(m.group("body"))[0], m))
    if ch["op"] == "remove":
        for i, _, m in sorted(existing, key=lambda x: -x[0]):
            rest = [t.group(0) for t in TOKEN.finditer(m.group("body")) if t.group(1) != word]
            if all(t.startswith("reason=") for t in rest):
                del lines[i]
            else:
                lines[i] = marker_line(m.group("indent"), prefix, rel, " ".join(rest)) + cr
    else:
        indent = re.match(r"[ \t]*", lines[j]).group(0)
        new = marker_line(indent, prefix, rel, token_of(full, ch.get("value"), ch.get("reason"))) + cr
        if len(existing) == 1 and len(existing[0][1]) == 1:
            lines[existing[0][0]] = new
        elif existing:
            return {"status": "conflict", "note": f"{rel}:{existing[0][0] + 1} has several keys on one marker line"}
        else:
            lines.insert(top, new)
    save(fp, lines, trailing)
    return {"status": "applied"}
