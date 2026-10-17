"""A generic, regex-based exporter (archmodel/1) for languages without a
compiler-based exporter: Java, Kotlin, Scala, C#, PHP, Ruby, Rust and Swift.

It reads source text with the standard library only, so it is fast and
approximate:

- Components are packages or namespaces where the language declares them
  (Java, Kotlin, Scala, C#, PHP), else folders (Ruby, Rust, Swift).
- Types are class, interface, record, enum, struct, trait, protocol, module
  and object declarations, with fields where a simple pattern finds them.
- File references come from imports resolved to project files, and from
  type names used in a file that are declared in the same package or
  namespace, or in one the file imports. Ruby uses require, require_relative
  and Zeitwerk naming (app/models/admin/user.rb is Admin::User). PHP uses
  `use` with the PSR-4 map in composer.json. Rust uses `mod` and
  `use crate::`, `self::`, `super::` and workspace crate names.

What it misses: references through fully qualified names in code, reflection
and dependency injection by configuration, generated code, macros, and Ruby
constants resolved by nesting. Names are matched textually, so a local name
that equals a type name in the same package counts as a reference.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

from .safeio import GIT_SAFE, line_at, read_file
from .safeio import walk as walk_dirs

LANGS = {".java": "java", ".kt": "kotlin", ".scala": "scala", ".cs": "csharp", ".php": "php", ".rb": "ruby",
         ".rs": "rust", ".swift": "swift"}
SKIP = {".git", "node_modules", "vendor", "target", "build", "dist", "out", "bin", "obj", ".gradle", ".idea",
        "Pods", "DerivedData", ".build", "tmp", "log", "storage", "coverage", ".archplan", ".ignored", "__pycache__",
        "bootstrap", "public", "packages"}
TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__", "Tests", "UnitTests", "IntegrationTests", "androidTest",
             "testFixtures", "benches", "examples", "fixtures"}
TEST_FILE = re.compile(r"(Tests?|Spec|IT)\.(java|kt|scala|cs|php|swift)$|_(spec|test)\.rb$|^test_.*\.rb$")
MAX_BYTES = 1_000_000
MAX_LINE = 2000  # characters; a longer line is left out
MANIFEST_SOURCES = {"Package.swift", "Gemfile", "Rakefile", "build.gradle.kts", "settings.gradle.kts", "Dangerfile",
                    "Podfile", "Fastfile"}
C_LIKE = {"java", "kotlin", "scala", "csharp", "rust", "swift", "php"}

# Strings and comments, replaced by blanks (newlines kept) before matching.
# An unclosed comment or string runs to the end of its line or of the text:
# otherwise the regex scans on from every opening quote, which is quadratic.
STRIP = {
    "c": re.compile(r'//[^\n]*|/\*.*?(?:\*/|\\?\Z)|"(?:\\.|[^"\\\n])*(?:"|(?=\n)|\\?\Z)|\'(?:\\.|[^\'\\\n])\'', re.S),
    "php": re.compile(r'//[^\n]*|#(?!\[)[^\n]*|/\*.*?(?:\*/|\\?\Z)|"(?:\\.|[^"\\])*(?:"|\\?\Z)|\'(?:\\.|[^\'\\])*(?:\'|\\?\Z)',
                     re.S),
    "ruby": re.compile(r'#[^\n]*|^=begin.*?(?:^=end|\\?\Z)'
                       r'|"(?:\\.|[^"\\\n])*(?:"|(?=\n)|\\?\Z)', re.S | re.M),
}

TYPE_DECL = {
    "java": re.compile(r"^[ \t]*(?:@\w+(?:\([^)\n]*\))?[ \t]+)*(?:(?:public|protected|private|abstract|static|final|sealed|"
                       r"non-sealed|strictfp)[ \t]+)*(class|interface|enum|record|@interface)\s+([A-Z]\w*)"
                       r"([^{;]{0,2000})", re.M),
    "kotlin": re.compile(r"^[ \t]*(?:@\w+(?:\([^)\n]*\))?[ \t]+)*(?:(?:public|protected|private|internal|abstract|open|"
                         r"final|sealed|data|enum|annotation|inner|value|inline|companion|fun)[ \t]+)*"
                         r"(class|interface|object)\s+([A-Z]\w*)([^{\n]*)", re.M),
    "scala": re.compile(r"^[ \t]*(?:(?:private|protected|abstract|final|sealed|implicit|case|lazy)[ \t]+)*"
                        r"(class|trait|object|enum)\s+([A-Z]\w*)([^{\n]*)", re.M),
    "csharp": re.compile(r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*(?:(?:public|protected|private|internal|abstract|static|sealed|"
                         r"partial|readonly|ref|file|new|unsafe)[ \t]+)*(class|interface|struct|record(?:\s+struct|\s+class)?"
                         r"|enum)\s+([A-Z]\w*)([^{;]{0,2000})", re.M),
    "php": re.compile(r"^[ \t]*(?:(?:abstract|final|readonly)[ \t]+)*(class|interface|trait|enum)\s+([A-Z]\w*)([^{]{0,2000})",
                      re.M),
    "ruby": re.compile(r"^[ \t]*(class|module)\s+([A-Z][\w:]*)([^\n]*)", re.M),
    "rust": re.compile(r"^[ \t]*(?:pub(?:\([^)\n]*\))?[ \t]+)?(struct|enum|trait|union)\s+([A-Z]\w*)([^{;]{0,2000})", re.M),
    "swift": re.compile(r"^[ \t]*(?:@\w+[ \t]+)*(?:(?:public|private|fileprivate|internal|open|final|indirect)[ \t]+)*"
                        r"(class|struct|enum|protocol|actor)\s+([A-Z]\w*)([^{]{0,2000})", re.M),
}
KIND = {"class": "class", "interface": "interface", "enum": "enum", "record": "record", "@interface": "interface",
        "object": "class", "trait": "interface", "struct": "record", "protocol": "interface", "actor": "class",
        "module": "module", "union": "record", "record struct": "record", "record class": "record"}
NAMESPACE = {
    "java": re.compile(r"^[ \t]*package\s+([\w.]+)\s*;", re.M),
    "kotlin": re.compile(r"^[ \t]*package\s+([\w.]+)", re.M),
    "scala": re.compile(r"^[ \t]*package\s+([\w.]+)", re.M),
    "csharp": re.compile(r"^[ \t]*namespace\s+([\w.]+)", re.M),
    "php": re.compile(r"^[ \t]*namespace\s+([\w\\]+)\s*[;{]", re.M),
}
IMPORT = {
    "java": re.compile(r"^[ \t]*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.M),
    "kotlin": re.compile(r"^[ \t]*import\s+([\w.]+(?:\.\*)?)", re.M),
    "scala": re.compile(r"^[ \t]*import\s+(\w+(?:\.\w+)*)(?:\.\{(?=([^}]{0,4000}))\2\}|\._|\.\*)?", re.M),
    "csharp": re.compile(r"^[ \t]*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", re.M),
    "php": re.compile(r"^[ \t]*use\s+(?:function\s+|const\s+)?(?=([^;]{1,4000}))\1;", re.M),
    "swift": re.compile(r"^[ \t]*(?:@testable\s+)?import\s+(\w+)", re.M),
}
RUBY_REQ = re.compile(r"""^[ \t]*(require|require_relative|load)\s*(?:\(\s*)?["']([^"']{1,500})["']""", re.M)
RUST_MOD = re.compile(r"^[ \t]*(?:pub(?:\([^)\n]*\))?[ \t]+)?mod\s+(\w+)\s*;", re.M)
RUST_USE = re.compile(r"^[ \t]*(?:pub(?:\([^)\n]*\))?[ \t]+)?use\s+(?=([^;]{1,4000}))\1;", re.M)
IDENT = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")
RUBY_CONST = re.compile(r"\b([A-Z]\w*(?:::[A-Z]\w*)*)")

FIELD = {
    "java": re.compile(r"^[ \t]+(?:(?:private|protected|public|final|transient|volatile)[ \t]+)+"
                       r"([\w.<>\[\], ?]+?)\s+(\w+)\s*(?:=[^;\n]*)?;", re.M),
    "csharp": re.compile(r"^[ \t]+(?:(?:private|protected|public|internal|readonly|required|virtual|override)[ \t]+)+"
                         r"([\w.<>\[\], ?]+?)\s+(\w+)\s*(?:\{\s*get|=[^;\n]*;|;)", re.M),
    "kotlin": re.compile(r"(?:^|[(,])\s*(?:(?:private|protected|public|internal|override|lateinit|open)\s+)*"
                         r"(?:val|var)\s+(\w+)\s*:\s*([\w.<>?, ]+?)\s*(?=[=,)\n])", re.M),
    "scala": re.compile(r"(?:^|[(,])\s*(?:(?:private|protected|override|lazy)\s+)*(?:val|var)?\s*(\w+)\s*:\s*"
                        r"([\w.\[\], ]+?)\s*(?=[=,)\n])", re.M),
    "php": re.compile(r"(?:private|protected|public)\s+(?:readonly\s+)?\??([\w\\]+)\s+\$(\w+)"),
    "rust": re.compile(r"^[ \t]+(?:pub(?:\([^)\n]*\))?[ \t]+)?(\w+)\s*:\s*([^,\n]+),", re.M),
    "swift": re.compile(r"^[ \t]+(?:(?:public|private|fileprivate|internal|let|var|weak|lazy|static|@\w+)[ \t]+)*"
                        r"(?:let|var)\s+(\w+)\s*:\s*([\w.<>\[\]?: ]+)", re.M),
}
NAME_FIRST = {"kotlin", "scala", "rust", "swift"}  # fields written "name: Type"
MANY = re.compile(r"^(?:List|Set|Collection|Iterable|Seq|Vec|Array|IEnumerable|ICollection|IList|HashSet|"
                  r"MutableList|ArrayList|ImmutableList)\s*<|\[\]$|^\[")


def blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def strip(text: str, lang: str) -> str:
    """The text with strings and comments blanked, and lines longer than
    MAX_LINE emptied: such lines are generated or minified code, and they
    would make the per-line patterns slow on hostile input."""
    rx = STRIP["php"] if lang == "php" else STRIP["ruby"] if lang == "ruby" else STRIP["c"]
    out = rx.sub(blank, text)
    if len(out) > MAX_LINE:
        out = "\n".join("" if len(ln) > MAX_LINE else ln for ln in out.split("\n"))
    return out


def camelize(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in s.split("_"))


class Src:
    def __init__(self, rel: str, lang: str, text: str):
        self.rel, self.lang, self.text = rel, lang, text
        self.dir = rel.rsplit("/", 1)[0] if "/" in rel else "."
        self.ns: str | None = None
        self.types: list = []  # (name, kind, line, rest)
        self.imports: list = []  # (target, line)
        self.idents: dict = {}  # identifier -> first line
        self.refs: dict = {}  # file -> line
        self.code = ""  # text with strings and comments blanked
        self.visible: dict = {}  # type name -> file, for names used without qualification


def walk(root: Path, tests: bool) -> list:
    out = []
    for d, dirs, names in walk_dirs(root, SKIP):
        dirs[:] = [x for x in dirs if tests or x not in TEST_DIRS]
        for n in names:
            lang = LANGS.get(os.path.splitext(n)[1])
            if not lang or n in MANIFEST_SOURCES or (not tests and TEST_FILE.search(n)):
                continue
            p = Path(d) / n
            try:
                st = p.lstat()  # not a symlink, FIFO or device; nothing large
                if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            out.append((p, lang))
    return out


def parse(s: Src) -> None:
    raw = s.text
    code = s.code = strip(raw, s.lang)
    if s.lang in NAMESPACE:
        m = NAMESPACE[s.lang].search(code)
        if m:
            s.ns = m.group(1).replace("\\", ".")
    for m in TYPE_DECL[s.lang].finditer(code):
        kind = re.sub(r"\s+", " ", m.group(1))
        s.types.append((m.group(2), KIND.get(kind, "class"), line_at(code, m.start(2)), m.group(3) or ""))
    if s.lang in IMPORT:
        for m in IMPORT[s.lang].finditer(code):
            ln = line_at(code, m.start(1))
            if s.lang == "php":
                for t in php_uses(m.group(1)):
                    s.imports.append((t, ln))
            elif s.lang == "scala" and m.group(2):
                for x in m.group(2).split(","):
                    x = x.strip().split("=>")[0].strip()
                    if x and x != "_":
                        s.imports.append((m.group(1) + "." + x, ln))
            else:
                s.imports.append((m.group(1), ln))
    if s.lang == "ruby":
        for m in RUBY_REQ.finditer(raw):
            s.imports.append((m.group(1) + ":" + m.group(2), line_at(raw, m.start(1))))
        for m in RUBY_CONST.finditer(code):
            s.idents.setdefault(m.group(1), line_at(code, m.start()))
    else:
        if s.lang == "rust":
            for m in RUST_MOD.finditer(code):
                s.imports.append(("mod:" + m.group(1), line_at(code, m.start(1))))
            for m in RUST_USE.finditer(code):
                for path in rust_paths(m.group(1)):
                    s.imports.append(("use:" + path, line_at(code, m.start(1))))
        for m in IDENT.finditer(code):
            s.idents.setdefault(m.group(1), line_at(code, m.start()))


def php_uses(body: str) -> list:
    """`A\\B\\C`, `A\\{B, C as D}` and `A, B` forms of a PHP use statement."""
    body = body.strip()
    m = re.match(r"([\w\\]*)\\\{([^}]*)\}", body)
    if m:
        return [(m.group(1) + "\\" + x.strip().split(" as ")[0].strip()).strip("\\") for x in m.group(2).split(",")
                if x.strip()]
    return [x.strip().split(" as ")[0].strip().lstrip("\\") for x in body.split(",") if x.strip()]


def rust_paths(body: str) -> list:
    """crate::a::{b, c::D} -> [crate::a::b, crate::a::c::D]."""
    body = re.sub(r"\s+", "", re.sub(r"\s+as\s+\w+", "", body))
    out = []

    def expand(prefix: str, s: str):
        depth, start, parts = 0, 0, []
        for i, ch in enumerate(s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append(s[start:i])
                start = i + 1
        parts.append(s[start:])
        for p in parts:
            if not p:
                continue
            i = p.find("{")
            if i >= 0 and p.endswith("}"):
                expand(prefix + p[:i], p[i + 1:-1])
            else:
                out.append(prefix + p)

    expand("", body)
    return [re.sub(r"::(self|\*)$", "", p) for p in out]


# ---------- resolution ----------

class Index:
    def __init__(self, srcs: list, root: Path, prefix: str = ""):
        self.root = root
        self.by_rel = {s.rel: s for s in srcs}
        self.fqn = {}  # "a.b.Type" -> file (namespaced languages)
        self.ns_types = defaultdict(dict)  # namespace -> {Type: file}
        self.dir_types = defaultdict(dict)  # (lang, dir) -> {Type: file}
        self.module_types = defaultdict(dict)  # Swift module -> {Type: file}
        self.ruby_const = {}  # "Admin::User" -> file
        for s in srcs:
            for name, *_ in s.types:
                if s.ns is not None:
                    self.fqn.setdefault(f"{s.ns}.{name}", s.rel)
                    self.ns_types[(s.lang_group(), s.ns)].setdefault(name, s.rel)
                self.dir_types[(s.lang, s.dir)].setdefault(name, s.rel)
                if s.lang == "swift":
                    self.module_types[swift_module(s.rel)].setdefault(name, s.rel)
            if s.lang == "ruby":
                c = zeitwerk(s.rel)
                if c:
                    self.ruby_const.setdefault(c, s.rel)
                for name, *_ in s.types:
                    if "::" in name:
                        self.ruby_const.setdefault(name, s.rel)
        self.psr4 = [(p, prefix + d) for p, d in psr4(root)]
        self.crates = {k: prefix + v for k, v in rust_crates(root).items()}


def lang_group(self) -> str:
    return "jvm" if self.lang in ("java", "kotlin", "scala") else self.lang


Src.lang_group = lang_group


def swift_module(rel: str) -> str:
    parts = rel.split("/")
    if "Sources" in parts:
        i = parts.index("Sources")
        return "/".join(parts[:i + 2])
    return parts[0] if len(parts) > 1 else "."


def zeitwerk(rel: str) -> str | None:
    """The constant a Rails file defines by Zeitwerk naming, from app/*/ and lib/."""
    parts = rel[:-3].split("/")
    for i, p in enumerate(parts[:-1]):
        if p == "app" and i + 2 < len(parts) + 1 and i + 1 < len(parts) - 1:
            rest = parts[i + 2:]
            if rest and rest[0] == "concerns":
                rest = rest[1:]
            return "::".join(camelize(x) for x in rest) if rest else None
        if p == "lib":
            return "::".join(camelize(x) for x in parts[i + 1:])
    return None


def psr4(root: Path) -> list:
    """[(namespace prefix with trailing dot, folder)] from composer.json files."""
    out = []
    for cj in [root / "composer.json"] + sorted(root.glob("*/composer.json")):
        try:
            d = json.loads(read_file(cj, MAX_BYTES))
        except (OSError, ValueError):
            continue
        base = cj.parent.relative_to(root).as_posix()
        for sect in ("autoload", "autoload-dev"):
            m = (d.get(sect) or {}).get("psr-4") or {}
            for prefix, dirs in m.items() if isinstance(m, dict) else []:
                for x in dirs if isinstance(dirs, list) else [dirs]:
                    if isinstance(x, str) and isinstance(prefix, str):
                        folder = "/".join(p for p in (base if base != "." else "", x.strip("/")) if p)
                        out.append((prefix.replace("\\", ".").strip(".") + ".", folder))
    return sorted(out, key=lambda x: -len(x[0]))


def rust_crates(root: Path) -> dict:
    """{crate name: its src folder} for the Cargo packages under root."""
    out = {}
    for d, dirs, names in walk_dirs(root, SKIP):
        if "Cargo.toml" in names:
            try:
                text = read_file(Path(d) / "Cargo.toml", MAX_BYTES)
            except OSError:
                continue
            m = re.search(r'^\[package\][^\[]*?^name\s*=\s*"([^"]+)"', text, re.M | re.S)
            if m:
                rel = os.path.relpath(d, root).replace(os.sep, "/")
                out[m.group(1).replace("-", "_")] = ("" if rel == "." else rel + "/") + "src"
    return out


def rust_root(rel: str) -> str:
    """The src folder of the crate a Rust file belongs to."""
    parts = rel.split("/")
    i = len(parts) - 1 - parts[::-1].index("src") if "src" in parts else 0
    return "/".join(parts[:i + 1]) if "src" in parts else (rel.rsplit("/", 1)[0] if "/" in rel else "")


def rust_module_dir(rel: str) -> str:
    """The folder where a Rust file's child modules live."""
    d, name = (rel.rsplit("/", 1) if "/" in rel else ("", rel))
    stem = name[:-3]
    if stem in ("mod", "lib", "main"):
        return d
    return (d + "/" if d else "") + stem


def rust_file(idx: Index, folder: str, segs: list) -> str | None:
    """The deepest module file along folder/segs."""
    best, cur = None, folder
    for s in segs:
        for cand in (f"{cur}/{s}.rs", f"{cur}/{s}/mod.rs"):
            cand = cand.lstrip("/")
            if cand in idx.by_rel:
                best = cand
                break
        else:
            break
        cur = f"{cur}/{s}"
    return best


def resolve(s: Src, idx: Index) -> None:
    def add(f, line):
        if f and f != s.rel and f not in s.refs:
            s.refs[f] = line

    lg = s.lang_group()
    visible = {}  # Type -> file, for names used without qualification
    if s.lang in ("java", "kotlin", "scala", "csharp", "php"):
        if s.ns is not None:
            visible.update(idx.ns_types.get((lg, s.ns), {}))
            if s.lang == "csharp":  # enclosing namespaces are visible too
                parts = s.ns.split(".")
                for i in range(1, len(parts)):
                    for k, v in idx.ns_types.get((lg, ".".join(parts[:i])), {}).items():
                        visible.setdefault(k, v)
        else:
            visible.update(idx.dir_types.get((s.lang, s.dir), {}))
        for target, line in s.imports:
            t = target.replace("\\", ".")
            if t.endswith(".*") or (s.lang == "csharp" and t not in idx.fqn):
                ns = t[:-2] if t.endswith(".*") else t
                for k, v in idx.ns_types.get((lg, ns), {}).items():
                    visible.setdefault(k, v)
                continue
            f = idx.fqn.get(t) or (php_file(idx, t) if s.lang == "php" else None)
            if not f:  # a static import or a nested type: try the enclosing name
                f = idx.fqn.get(t.rsplit(".", 1)[0])
            if f:
                add(f, line)
                visible[t.rsplit(".", 1)[-1]] = f
    elif s.lang == "swift":
        for target, line in s.imports:  # another target of the same package
            for mod, ts in idx.module_types.items():
                if mod.rsplit("/", 1)[-1] == target and mod != swift_module(s.rel):
                    visible.update(ts)
        visible.update(idx.module_types.get(swift_module(s.rel), {}))
    elif s.lang == "rust":
        visible.update(idx.dir_types.get(("rust", s.dir), {}))
        for target, line in s.imports:
            kind, _, path = target.partition(":")
            if kind == "mod":
                add(rust_file(idx, rust_module_dir(s.rel), [path]), line)
                continue
            segs = path.split("::")
            if segs[0] == "crate":
                f = rust_file(idx, rust_root(s.rel), segs[1:])
            elif segs[0] == "self":
                f = rust_file(idx, rust_module_dir(s.rel), segs[1:])
            elif segs[0] == "super":
                base = rust_module_dir(s.rel)
                while segs and segs[0] == "super":
                    base = base.rsplit("/", 1)[0] if "/" in base else ""
                    segs = segs[1:]
                f = rust_file(idx, base, segs)
            elif segs[0] in idx.crates:
                root = idx.crates[segs[0]]
                f = rust_file(idx, root, segs[1:]) or next(
                    (x for x in (f"{root}/lib.rs",) if x in idx.by_rel), None)
            else:
                f = None
            add(f, line)
            if f and segs and segs[-1][:1].isupper():
                visible.setdefault(segs[-1], f)
    elif s.lang == "ruby":
        for target, line in s.imports:
            kind, _, path = target.partition(":")
            if kind == "require_relative":
                cand = os.path.normpath(os.path.join(s.dir, path)).replace(os.sep, "/")
            else:
                cand = next((c for c in (path, "lib/" + path, "app/" + path) if c + ".rb" in idx.by_rel
                             or c in idx.by_rel), path)
            add(next((c for c in (cand + ".rb", cand) if c in idx.by_rel), None), line)
        own = zeitwerk(s.rel) or ""
        scopes = own.split("::")[:-1] if own else []
        for name, line in s.idents.items():
            f = None
            for i in range(len(scopes), -1, -1):
                f = idx.ruby_const.get("::".join(scopes[:i] + [name]))
                if f:
                    break
            add(f, line)
        return
    s.visible = visible
    own = {t[0] for t in s.types}
    for name, line in s.idents.items():
        if name in visible and name not in own:
            add(visible[name], line)


def php_file(idx: Index, fqn: str) -> str | None:
    for prefix, folder in idx.psr4:
        if (fqn + ".").startswith(prefix):
            rest = fqn[len(prefix):].replace(".", "/")
            cand = f"{folder}/{rest}.php".lstrip("/")
            if cand in idx.by_rel:
                return cand
    return None


# ---------- the model ----------

def component_of(s: Src) -> str:
    if s.ns is not None and s.lang in ("java", "kotlin", "scala", "csharp", "php"):
        return s.ns
    return s.dir if s.dir != "." else "(root)"


def fields_of(s: Src, rest: str, start: int, end: int, resolve_name) -> list:
    """Fields of the type declared at start: from a record or primary
    constructor header and the body up to the next type declaration."""
    rx = FIELD.get(s.lang)
    if rx is None:
        return []
    body = s.code[start:end]
    head = rest if s.lang in ("java", "kotlin", "scala", "csharp", "swift") else ""
    out, seen = [], set()
    chunks = [head, body]
    if s.lang == "java" and "(" in rest:  # record components
        for part in rest.split("(", 1)[1].split(")", 1)[0].split(","):
            bits = part.strip().split()
            if len(bits) >= 2:
                chunks.append(f"  private {' '.join(bits[:-1])} {bits[-1]};\n")
    for chunk in chunks:
        for m in rx.finditer(chunk):
            a, b = m.group(1), m.group(2)
            fname, ftype = (a, b) if s.lang in NAME_FIRST else (b, a)
            ftype = ftype.strip()
            if fname in seen or not re.match(r"^\w+$", fname) or fname in ("return", "class", "new"):
                continue
            seen.add(fname)
            many = bool(MANY.search(ftype))
            inner = re.findall(r"[A-Z]\w*", ftype)
            ref = next((r for r in (resolve_name(x) for x in reversed(inner)) if r), None)
            f = {"name": fname, **({"ref": ref} if ref else {"type": ftype[:80]}), **({"many": True} if many else {})}
            out.append(f)
            if len(out) >= 40:
                return out
    return out


def bases_of(rest: str, lang: str) -> list:
    """Names a declaration extends or implements, from the text after its name."""
    r = rest
    for _ in range(3):
        r = re.sub(r"\([^()]*\)", "", r)
        if lang != "ruby":
            r = re.sub(r"<[^<>]*>", "", r)
    r = r.split(" where ")[0]
    if lang in ("java", "php", "scala"):
        m = re.search(r"\b(?:extends|implements)\b(.*)", r)
    elif lang == "ruby":
        m = re.search(r"<(.*)", r)
    elif lang == "rust":
        m = None
    else:
        m = re.search(r":(.*)", r)
    return re.findall(r"[A-Z]\w*", m.group(1)) if m else []


def git_ref(root: Path) -> tuple:
    try:
        ref = subprocess.run(["git", *GIT_SAFE, "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", *GIT_SAFE, "-C", str(root), "status", "--porcelain"], capture_output=True,
                                    text=True, timeout=30).stdout.strip())
        return ref or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, False


def export(root: Path, tests: bool = False, languages: list | None = None, rel_to: Path | None = None) -> dict:
    """The model of the code under root. Paths are relative to rel_to (default:
    root), so a subfolder export keeps repository-relative paths."""
    root = root.resolve()
    base = (rel_to or root).resolve()
    if base != root and base not in root.parents:
        raise ValueError(f"{root} is not inside {base}")
    prefix = "" if base == root else root.relative_to(base).as_posix() + "/"
    srcs = []
    for p, lang in walk(root, tests):
        if languages and lang not in languages:
            continue
        try:
            text = read_file(p, MAX_BYTES)
        except OSError:
            continue
        s = Src(p.relative_to(base).as_posix(), lang, text)
        parse(s)
        srcs.append(s)
    idx = Index(srcs, root, prefix)
    for s in srcs:
        resolve(s, idx)
    comp = {s.rel: component_of(s) for s in srcs}

    # Types, with ids "<component>.<Name>".
    tid_of, used = {}, set()
    for s in srcs:
        for name, *_ in s.types:
            tid = f"{comp[s.rel]}.{name}" if comp[s.rel] != "(root)" else name
            if tid in used:
                tid = f"{tid}@{s.rel}"
            tid_of[(s.rel, name)] = tid
            used.add(tid)
    types = {}
    for s in srcs:
        starts = sorted(m.start() for m in TYPE_DECL[s.lang].finditer(s.code))
        at = {m.group(2): m.start() for m in TYPE_DECL[s.lang].finditer(s.code)}

        def resolve_name(x, s=s):
            return tid_of.get((s.rel, x)) or tid_of.get((s.visible.get(x), x))

        for name, kind, line, rest in s.types:
            tid = tid_of[(s.rel, name)]
            start = at.get(name, 0)
            end = next((x for x in starts if x > start), len(s.code))
            fields = fields_of(s, rest, start, end, resolve_name)
            bases = bases_of(rest, s.lang)
            implements = [r for r in dict.fromkeys(resolve_name(b) for b in bases if b != name) if r and r != tid]
            types[tid] = {"name": name.split("::")[-1], "component": comp[s.rel], "kind": kind, "file": s.rel,
                          "line": line, "fields": fields, "methods": [], "implements": implements, "uses": []}

    # Dependencies between components, weighted by the number of referring files.
    edges = defaultdict(lambda: {"files": set(), "evidence": []})
    files = {}
    for s in srcs:
        refs = sorted(s.refs.items(), key=lambda x: (x[1], x[0]))
        files[s.rel] = {"component": comp[s.rel], "refs": [{"to": f, "line": ln} for f, ln in refs]}
        for f, ln in refs:
            a, b = comp[s.rel], comp[f]
            if a == b:
                continue
            e = edges[(a, b)]
            if s.rel not in e["files"]:
                e["files"].add(s.rel)
                if len(e["evidence"]) < 5:
                    e["evidence"].append(f"{s.rel}:{ln}")
    deps = [{"from": a, "to": b, "weight": len(e["files"]), "evidence": e["evidence"]}
            for (a, b), e in sorted(edges.items())]

    comps = {}
    by_comp = defaultdict(list)
    comps_in_dir = defaultdict(set)
    for s in srcs:
        by_comp[comp[s.rel]].append(s)
        comps_in_dir[s.dir].add(comp[s.rel])
    for c, ss in sorted(by_comp.items()):
        dirs = Counter(s.dir for s in ss)
        match = []
        for d in sorted(dirs):  # a folder of its own, else its files
            if len(comps_in_dir[d]) == 1:
                match.append(d + "/*" if d != "." else "*")
            else:
                match += sorted(s.rel for s in ss if s.dir == d)
        comps[c] = {"name": re.split(r"[./]", c)[-1] or c, "path": dirs.most_common(1)[0][0], "match": match[:500],
                    "kind": "internal", "language": Counter(s.lang for s in ss).most_common(1)[0][0]}
    langs = [l for l, _ in Counter(s.lang for s in srcs).most_common()]
    ref, dirty = git_ref(root)
    return {
        "format": "archmodel/1",
        "source": {"repo": base.name, "ref": ref, "dirty": dirty, "languages": langs,
                   "generator": "archplan export-lite",
                   "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "capabilities": ["components", "types", "fields", "deps", "files"]},
        "layers": [],
        "components": comps,
        "dependencies": deps,
        "files": dict(sorted(files.items())),
        "policy": {"default": "allow", "rules": []},
        "types": types,
        "sequences": {},
    }
