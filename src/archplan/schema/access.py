"""Which component reads or writes which table.

- orm: a file that references the file of a table's ORM type (from the
  model's files section) uses the table through the ORM.
- write: such a file, or the type's own file, also calls a write function of
  the ORM (Repo.insert, Model.objects.create, record.save, prisma.x.create).
- read and write from raw SQL: table names after FROM, JOIN, INSERT INTO,
  UPDATE and DELETE FROM in upper case, outside migrations.

Then every table gets an owner: the component of its ORM type, else the
component whose code writes it most, else the owner of the table that
declares it as a join table."""

from __future__ import annotations

import re
from collections import defaultdict
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path

from ..core.safeio import line_at, read_text

EVIDENCE = 5
MAX_FILE = 2_000_000


class AccessMode(str, Enum):
    ORM = "orm"
    READ = "read"
    WRITE = "write"

WRITE = {
    "elixir": re.compile(r"\b(?:Repo|Multi)\.(?:insert|update|delete|insert_all|update_all|delete_all|"
                         r"insert_or_update|upsert)!?\b"),
    "python": re.compile(r"\.objects\.(?:create|bulk_create|get_or_create|update_or_create|bulk_update)\b|"
                         r"\.(?:save|delete)\(|\.update\("),
    "ruby": re.compile(r"\.(?:create|update|save|destroy|delete_all|update_all|insert_all|upsert_all|"
                       r"find_or_create_by|update_columns?)!?\b"),
    "ts": re.compile(r"\.(?:create|createMany|update|updateMany|upsert|delete|deleteMany|save|insert)\("),
}
# Files that only fill tables for tests or demos: their writes do not make a writer.
NOT_WRITERS = re.compile(r"(^|/)(test|tests|spec|e2e|seeds?|fixtures?|factories|factory)(/|$)|"
                         r"(factor(y|ies)|seeds?|fixtures?)\.\w+$", re.I)


def forms(text: str, mp: dict) -> list:
    """The names a file uses for a table's ORM type."""
    name = mp.get("module") or mp.get("class")
    if not name:
        return []
    parts = name.split(".")
    out = [name]
    if len(parts) >= 2:
        out.append(".".join(parts[-2:]))
        # alias A.B.{C, User}: the lookahead and backreference keep the regex
        # from backtracking through the braces, which is slow on many unclosed ones
        groups = re.finditer(rf"\balias\s+{re.escape('.'.join(parts[:-1]))}\.{{(?=([^}}]{{0,4000}}))\1}}", text)
        if re.search(rf"\balias\s+{re.escape(name)}\b(?!\.)", text) or any(
                re.search(rf"\b{re.escape(parts[-1])}\b", g.group(1)) for g in groups):
            out.append(parts[-1])
        m = re.search(rf"\balias\s+{re.escape(name)}\s*,\s*as:\s*(\w+)", text)
        if m:
            out.append(m.group(1))
    else:
        out.append(name)
    return list(dict.fromkeys(out))


def write_at(lang: str, text: str, mp: dict, own_file: bool) -> int | None:
    """The offset of a write to the table's ORM type in a file, or None."""
    if not WRITE[lang].search(text):
        return None
    fs = forms(text, mp)
    if not fs:
        return None
    alt = "|".join(re.escape(f) for f in sorted(fs, key=len, reverse=True))
    if lang == "elixir":
        pat = (rf"(?<![\w.])(?:{alt})\.\w*changeset\(|%(?:{alt})\{{\s*\}}\s*\|>|"
               rf"\b(?:insert|insert_all|insert_or_update)!?\(\s*(?:%(?:{alt})\{{|(?:{alt})\b)|"
               rf"_all\(\s*(?:{alt})\b")
        if own_file:
            return None  # a schema's own changeset is not a write
    elif lang == "python":
        pat = (rf"\b(?:{alt})\.objects\.(?:create|bulk_create|get_or_create|update_or_create|bulk_update)\b|"
               rf"\b(?:{alt})\.objects\.[^\n]*\.(?:update|delete)\(")
        if own_file:
            pat += r"|\bdef save\(|\.save\("
        # `model = X` followed somewhere later by .save(; endpos instead of a
        # lookahead to the end of the text, which is quadratic
        save = text.rfind(".save(")
        m = re.compile(rf"\bmodel\s*=\s*(?:{alt})\b").search(text, 0, save) if save >= 0 else None
        other = re.search(pat, text)
        starts = [x.start() for x in (m, other) if x]
        return min(starts) if starts else None
    elif lang == "ruby":
        pat = (rf"\b(?:{alt})\.(?:create|insert_all|upsert_all|update_all|delete_all|destroy_all|"
               rf"find_or_create_by|new)!?\b")
        if own_file:
            return WRITE[lang].search(text).start()
    else:
        pat = rf"\b(?:{alt})\b[^\n]*\.(?:save|insert|create|update|delete)\(|getRepository\((?:{alt})\)"
    m = re.search(pat, text)
    return m.start() if m else None


SQL = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|FROM|JOIN)\s+[\"`]?(?:public\.)?"
                 r"[\"`]?([A-Za-z_][\w]*)")
ECTO_FROM = re.compile(r"\bfrom\(?\s*\w+\s+in\s+\"(\w+)\"")
PRISMA = re.compile(r"\b\w+\.(\w+)\.(findMany|findUnique|findUniqueOrThrow|findFirst|findFirstOrThrow|count|"
                    r"aggregate|groupBy|create|createMany|update|updateMany|upsert|delete|deleteMany)\(")
PRISMA_WRITES = {"create", "createMany", "update", "updateMany", "upsert", "delete", "deleteMany"}


def lang_of(path: str) -> str | None:
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    return {"ex": "elixir", "exs": "elixir", "py": "python", "rb": "ruby", "ts": "ts", "tsx": "ts", "js": "ts",
            "mjs": "ts", "jsx": "ts"}.get(ext)


def component_of(model: dict, path: str | None) -> str | None:
    if not path:
        return None
    f = (model.get("files") or {}).get(path)
    if isinstance(f, dict) and f.get("component"):
        return f["component"]
    best, size = None, -1
    for cid, c in (model.get("components") or {}).items():
        for pat in (c or {}).get("match") or []:
            hit = fnmatch(path, pat) or (pat.endswith("/**") and path.startswith(pat[:-2]))
            if hit and len(pat) > size:
                best, size = cid, len(pat)
    return best


class Reader:
    def __init__(self, root: Path):
        self.root, self.cache = root, {}

    def text(self, rel: str) -> str:
        if rel not in self.cache:
            try:
                self.cache[rel] = read_text(self.root, rel, MAX_FILE)
            except OSError:
                self.cache[rel] = ""
        return self.cache[rel]


def access(model: dict, root: Path, tables: dict, maps: dict, prisma: bool = False) -> list:
    """The access list: [{"component", "table", "mode", "weight", "evidence"}]."""
    files = model.get("files") or {}
    rd = Reader(root)
    hits = defaultdict(lambda: {"files": set(), "evidence": []})

    def hit(comp, table, mode, path, line):
        if not comp:
            return
        h = hits[(comp, table, mode)]
        if path not in h["files"]:
            h["files"].add(path)
            if len(h["evidence"]) < EVIDENCE:
                h["evidence"].append(f"{path}:{line}")

    by_file = defaultdict(list)  # type file -> tables
    for table, mp in maps.items():
        if mp.get("file") and (mp.get("module") or mp.get("class")):
            by_file[mp["file"]].append(table)
    users = defaultdict(list)  # type file -> [(user file, line)]
    for path, f in files.items():
        if not isinstance(f, dict):
            continue
        for r in f.get("refs") or []:
            if r.get("to") in by_file:
                users[r["to"]].append((path, r.get("line") or 1))
    for tfile, tbls in by_file.items():
        for path, line in [(tfile, None), *users.get(tfile, [])]:
            comp = component_of(model, path)
            for table in tbls:
                mp = maps[table]
                hit(comp, table, AccessMode.ORM, path, line or mp.get("line") or 1)
            lang = lang_of(path)
            if lang not in WRITE or NOT_WRITERS.search(path):
                continue
            text = rd.text(path)
            for table in tbls:
                at = write_at(lang, text, maps[table], path == tfile)
                if at is not None:
                    hit(comp, table, AccessMode.WRITE, path, line_at(text, at))

    known = set(tables)
    lower = {t.lower(): t for t in tables}
    prisma_models = {}
    if prisma:
        for table, mp in maps.items():
            if mp.get("class"):
                prisma_models[mp["class"][:1].lower() + mp["class"][1:]] = table
    for path in files:
        if "migrations" in path.split("/") or "migrate" in path.split("/") or lang_of(path) is None:
            continue
        text = rd.text(path)
        comp = component_of(model, path)
        if not comp:
            continue
        for m in SQL.finditer(text):
            name = m.group(2)
            table = name if name in known else lower.get(name.lower())
            if table is None:
                continue
            kw = m.group(1).split()[0]
            mode = AccessMode.READ if kw in ("FROM", "JOIN") else AccessMode.WRITE
            hit(comp, table, mode, path, line_at(text, m.start()))
        if lang_of(path) == "elixir":
            for m in ECTO_FROM.finditer(text):
                if m.group(1) in known:
                    hit(comp, m.group(1), AccessMode.READ, path, line_at(text, m.start()))
        if prisma_models:
            for m in PRISMA.finditer(text):
                table = prisma_models.get(m.group(1))
                if table:
                    hit(comp, table, AccessMode.WRITE if m.group(2) in PRISMA_WRITES else AccessMode.READ, path,
                        line_at(text, m.start()))
    out = [{"component": c, "table": t, "mode": mode.value, "weight": len(h["files"]), "evidence": h["evidence"]}
           for (c, t, mode), h in hits.items()]
    out.sort(key=lambda a: (a["table"], a["mode"], -a["weight"], a["component"]))
    return out


def owners(maps: dict, acc: list, tables: dict) -> None:
    """Give every table an owner component in maps: its type's component,
    else the component that writes it most, else the owner of the table that
    declares it as a join table."""
    writes = defaultdict(lambda: defaultdict(int))
    for a in acc:
        if AccessMode(a["mode"]) is AccessMode.WRITE:
            writes[a["table"]][a["component"]] += a["weight"]
    for table in tables:
        mp = maps.get(table)
        if mp and mp.get("component"):
            continue
        w = writes.get(table)
        if w:
            comp = max(sorted(w), key=lambda c: w[c])
            maps.setdefault(table, {})
            maps[table].update({"component": comp, "via": "writes"})
    for table, mp in list(maps.items()):
        if mp.get("component") or not mp.get("through"):
            continue
        owner = (maps.get(mp["through"]) or {}).get("component")
        if owner:
            mp.update({"component": owner, "via": "join table"})
