"""Table owners from the project's config and from CREATE TABLE statements.

- config: `"schema": {"owners": {"entities": "store", "comment*": "sqlitecomments"}}`
  in .archplan/config.json. A key is a table name or a glob on table names
  (`*`, `?`, `[...]`). An exact name wins over a glob; among globs, the first
  in the file wins. The config wins over every other way to find an owner.
- create: the component whose files hold the CREATE TABLE statement of the
  table, as a string literal in a source file of the model or in a .sql file,
  such as a migration. A file belongs to the component that owns its path.
  When several components create the table in as many files, none of them
  owns it. Database dumps (schema.sql, structure.sql), test files and Go
  comments do not count. This step only fills tables that have no owner yet.
- fts shadow: the tables SQLite keeps for a full-text virtual table x
  (x_data, x_idx, x_config, x_content, x_docsize for FTS5; x_segments,
  x_segdir, x_stat too for FTS3 and FTS4) belong to the owner of x, and
  count as used when x is."""

from __future__ import annotations

import re
from collections import defaultdict
from fnmatch import fnmatchcase
from pathlib import Path

from ..core.hotspots import Mapper
from .access import NOT_WRITERS, Reader, source_text

IDENT = r"(?:`[^`\n]{1,128}`|\"[^\"\n]{1,128}\"|\[[^\]\n]{1,128}\]|[A-Za-z_]\w{0,127})"
CREATE = re.compile(rf"(?i)\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:(?:TEMP|TEMPORARY|UNLOGGED|VIRTUAL)\s+)?TABLE\s+"
                    rf"(?:IF\s+NOT\s+EXISTS\s+)?({IDENT}(?:\s*\.\s*{IDENT})?)")
TESTS = re.compile(r"_test\.go$|\.(?:test|spec)\.\w+$|(?:^|/)test_[^/]*\.py$")
GLOB = re.compile(r"[*?\[]")


def configured(cfg) -> tuple:
    """([(pattern, component)] with exact names first, [warnings]) from the
    config's schema.owners."""
    if cfg is None:
        return [], []
    if not isinstance(cfg, dict):
        return [], ['config.json "schema": {"owners"} must map table names or globs to component ids']
    rules, warnings = [], []
    for k, v in cfg.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k or not v:
            warnings.append(f"schema owners: {str(k)[:60]!r} must map to a component id")
            continue
        rules.append((k, v))
    rules.sort(key=lambda r: bool(GLOB.search(r[0])))  # stable: exact names first, globs in file order
    return rules, warnings


def apply_config(maps: dict, tables: dict, cfg, components: dict) -> list:
    """Set the owner of each table a config rule names. Returns warnings."""
    rules, warnings = configured(cfg)
    bad = sorted({c for _, c in rules if c not in components})
    if bad:
        warnings.append(f"schema owners: no component {', '.join(repr(c) for c in bad[:5])} in the model")
    done, unused = set(), []
    for pat, comp in rules:
        hits = [t for t in tables if fnmatchcase(t, pat)]
        if not hits:
            unused.append(pat)
        if comp not in components:
            continue
        for table in hits:
            if table not in done:
                done.add(table)
                keep = {k: v for k, v in (maps.get(table) or {}).items() if k in ("type", "through")}
                maps[table] = {**keep, "component": comp, "via": "config"}
    if unused:
        warnings.append(f"schema owners: {', '.join(repr(p) for p in unused[:5])} match no table")
    return warnings


# SQLite full-text search: a virtual table x keeps its index in shadow tables
# x_<suffix>. (always there, sometimes there) per FTS version.
FTS_SHADOWS = ((("data", "idx", "config"), ("content", "docsize")),                 # fts5
               (("content", "segments", "segdir"), ("docsize", "stat")))            # fts3, fts4


def shadow_tables(tables: dict) -> dict:
    """{shadow table: its full-text virtual table} for SQLite FTS tables."""
    out = {}
    for v in tables:
        for always, sometimes in FTS_SHADOWS:
            if all(f"{v}_{s}" in tables for s in always):
                out.update({f"{v}_{s}": v for s in always + sometimes if f"{v}_{s}" in tables})
    return out


def apply_shadows(maps: dict, tables: dict) -> None:
    """Give each FTS shadow table the owner of its virtual table, and mark it
    used through that table. A config rule for the shadow table itself wins."""
    for shadow, v in shadow_tables(tables).items():
        if (maps.get(shadow) or {}).get("via") == "config":
            continue
        own = (maps.get(v) or {}).get("component")
        keep = {k: x for k, x in (maps.get(shadow) or {}).items() if k == "type"}
        maps[shadow] = {**keep, "through": v, **({"component": own, "via": "fts shadow"} if own else {})}


def unquote(name: str) -> str:
    return ".".join(p.strip().strip('`"[]') for p in name.split("."))


def table_of(name: str, tables: dict, lower: dict) -> str | None:
    """The table a CREATE TABLE name stands for: the name itself, or without its schema prefix."""
    name = unquote(name)
    for n in (name, name.rsplit(".", 1)[-1]):
        if n in tables:
            return n
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def sql_files(root: Path, walk, skip_names) -> list:
    """.sql files under root, relative to it, without the dumps the sql source reads."""
    out = []
    for d, names in walk(root, depth=8):
        rel = d.relative_to(root).as_posix()
        for n in names:
            if n.lower().endswith(".sql") and n not in skip_names:
                out.append(n if rel == "." else f"{rel}/{n}")
    return sorted(out)


def created(model: dict, base: Path, root: Path, pre: str, tables: dict, walk, skip_names) -> dict:
    """{table: component} from the CREATE TABLE statements in the model's
    source files and the .sql files under root. pre turns a path under root
    into a model path (relative to base)."""
    if not tables:
        return {}
    files = model.get("files") or {}
    mapper = Mapper(model.get("components") or {})
    lower = {t.lower(): t for t in tables}
    rd = Reader(base)
    paths = [p for p in files if isinstance(p, str)]
    paths += [pre + p for p in sql_files(root, walk, skip_names) if pre + p not in files]
    by = defaultdict(lambda: defaultdict(set))  # table -> component -> files
    for path in paths:
        if NOT_WRITERS.search(path) or TESTS.search(path):
            continue
        f = files.get(path)
        comp = (f.get("component") if isinstance(f, dict) else None) or mapper(path)
        if not comp:
            continue
        text = source_text(rd, path)
        if "table" not in text.lower():
            continue
        for m in CREATE.finditer(text):
            table = table_of(m.group(1), tables, lower)
            if table:
                by[table][comp].add(path)
    out = {}
    for table, comps in by.items():
        counts = sorted(((len(fs), c) for c, fs in comps.items()), reverse=True)
        if len(counts) == 1 or counts[0][0] > counts[1][0]:
            out[table] = counts[0][1]
    return out
