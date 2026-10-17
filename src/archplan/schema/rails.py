"""Read a Rails db/schema.rb, and map tables to ActiveRecord models in
app/models (self.table_name, or the plural of the class name) with their
`enum` values."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

from ..core.safeio import files as safe_files
from ..core.safeio import line_at, read_file
from .core import add_check, add_fk, add_index, find_col, get_table, plural, set_col, set_pk, snake
from .exterm import atom, atoms, call, statements

TYPES = {"string": "varchar", "text": "text", "integer": "integer", "bigint": "bigint", "float": "double precision",
         "decimal": "numeric", "datetime": "timestamp", "timestamp": "timestamp", "time": "time", "date": "date",
         "boolean": "boolean", "binary": "bytea", "json": "json", "jsonb": "jsonb", "uuid": "uuid",
         "references": "bigint", "belongs_to": "bigint", "citext": "citext", "inet": "inet", "hstore": "hstore",
         "enum": "enum", "virtual": "virtual"}
SCHEMA_MAX = 8 << 20  # bytes; a larger schema.rb is skipped
ON_DELETE = {"cascade": "CASCADE", "nullify": "SET NULL", "restrict": "RESTRICT"}


def read(root: Path, schema_rb: Path) -> dict:
    s = {"tables": {}, "enums": {}, "maps": {}}
    lines = read_file(schema_rb, SCHEMA_MAX).split("\n")
    cur = None
    for _, st in statements(lines):
        st = st.strip()
        if not st or st.startswith("#"):
            continue
        if st == "end":
            cur = None
            continue
        r = call(re.sub(r"\s+do(\s*\|\w+\|)?\s*$", "", st))
        if not r:
            continue
        name, pos, kw = r
        if name == "create_table" and pos and isinstance(pos[0], str):
            cur = pos[0]
            t = get_table(s["tables"], cur)
            if kw.get("id") is not False:
                pk = atom(kw.get("primary_key")) or "id"
                typ = TYPES.get(atom(kw.get("id")) or "", None) or "bigint"
                set_col(t, pk, typ, False, "autoincrement")
                set_pk(t, [pk])
            elif isinstance(kw.get("primary_key"), list):
                set_pk(t, atoms(kw["primary_key"]))
            if isinstance(kw.get("comment"), str):
                t["comment"] = kw["comment"]
        elif name == "create_enum" and len(pos) >= 2:
            s["enums"][atom(pos[0])] = atoms(pos[1])
        elif name == "add_foreign_key" and len(pos) >= 2:
            frm, to = atom(pos[0]), atom(pos[1])
            col = atom(kw.get("column")) or re.sub(r"s$", "", to) + "_id"
            add_fk(get_table(s["tables"], frm), [col], to, [atom(kw.get("primary_key")) or "id"],
                   ON_DELETE.get(atom(kw.get("on_delete")) or ""))
        elif cur and name.startswith("t."):
            column(s, cur, name[2:], pos, kw)
    for t in s["tables"].values():
        for c in t["columns"]:
            if c["type"] in s["enums"] and not c.get("values"):
                c["values"] = list(s["enums"][c["type"]])
    models(root, s)
    return s


def column(s: dict, table: str, kind: str, pos: list, kw: dict) -> None:
    t = get_table(s["tables"], table)
    if kind == "timestamps":
        for n in ("created_at", "updated_at"):
            set_col(t, n, "timestamp", kw.get("null") is True)
        return
    if kind == "index" and pos:
        cols = atoms(pos[0]) if isinstance(pos[0], list) else [atom(pos[0])]
        add_index(t, [c for c in cols if c], kw.get("unique") is True)
        return
    if kind == "check_constraint" and pos and isinstance(pos[0], str):
        add_check(t, (f"{kw['name']}: " if isinstance(kw.get("name"), str) else "") + pos[0])
        return
    names = [atom(x) for x in pos if atom(x)]
    for n in names:
        if kind in ("references", "belongs_to"):
            col = f"{n}_id"
            set_col(t, col, TYPES.get(atom(kw.get("type")) or "", "bigint"), kw.get("null") is not False)
            if kw.get("polymorphic") is True:
                set_col(t, f"{n}_type", "varchar", kw.get("null") is not False)
            if kw.get("index") is not False:
                add_index(t, [col], False)
            continue
        typ = atom(kw.get("enum_type")) if kind == "enum" and kw.get("enum_type") else TYPES.get(kind, kind)
        if kind == "string" and isinstance(kw.get("limit"), int):
            typ = f"varchar({kw['limit']})"
        if kw.get("array") is True:
            typ += "[]"
        d = kw.get("default")
        default = None
        if d is not None and not isinstance(d, (list, dict)):
            default = str(d).lower() if isinstance(d, bool) else d[1] if isinstance(d, tuple) else str(d)
        c = set_col(t, n, typ, kw.get("null") is not False, default)
        if isinstance(kw.get("comment"), str):
            c["comment"] = kw["comment"]
        if kw.get("primary_key") is True:
            set_pk(t, [n])


MAX_MODELS = 50  # classes and enums read per model file
MODEL = re.compile(r"^[ \t]*class\s+([\w:]+)\s*<\s*([\w:]+)", re.M)


def models(root: Path, s: dict) -> None:
    d = root / "app" / "models"
    if not d.is_dir():
        return
    if d.is_symlink():
        return
    for f in safe_files(d, ".rb"):
        try:
            text = read_file(f)
        except OSError:
            continue
        if "abstract_class = true" in text:
            continue
        tn = re.search(r"self\.table_name\s*=\s*[\"':](\w+)", text)
        enums = None
        for k, m in enumerate(MODEL.finditer(text)):
            if k >= MAX_MODELS:
                break  # a model file holds one class; many more is hostile or generated
            cls, base = m.group(1), m.group(2)
            if base.split("::")[-1] not in ("ApplicationRecord", "Base") and not base.endswith("Record"):
                continue
            table = tn.group(1) if tn else plural(snake(cls.split("::")[-1]))
            if table not in s["tables"]:
                continue
            line = line_at(text, m.start())
            s["maps"].setdefault(table, {"class": cls.split("::")[-1], "file": f.relative_to(root).as_posix(),
                                         "line": line})
            if enums is None:
                found = itertools.islice(re.finditer(r"^[ \t]*enum\s+(.+)$", text, re.M), MAX_MODELS)
                enums = [r for r in (call("enum " + em.group(1)) for em in found) if r]
            for r in enums:
                _, pos, kw = r
                if pos and atom(pos[0]) and len(pos) > 1:  # enum :status, {a: 0}
                    pairs = {atom(pos[0]): pos[1]}
                else:
                    pairs = {k: v for k, v in kw.items() if not k.startswith("_")}
                for col, vals in pairs.items():
                    if isinstance(vals, tuple):
                        vals = vals[0] if vals and isinstance(vals[0], dict) else list(vals)
                    c = find_col(s["tables"][table], col)
                    if c is not None and atoms(vals):
                        c["values"] = atoms(vals)
