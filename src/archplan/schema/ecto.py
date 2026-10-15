"""Read Ecto schemas (`schema "table" do ... end`) and, when present,
priv/repo/migrations, with the standard library only.

Schemas give the table's module, its fields, belongs_to foreign keys,
many_to_many join tables, embeds (stored as jsonb) and Ecto.Enum values.
Migrations are replayed in order (create, alter, drop and rename table, add,
modify and remove column, indexes, references, check constraints and
`execute` DDL) and give the columns, nullability and indexes that schemas do
not declare."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..safeio import is_file, read_file, read_text
from ..safeio import walk as safe_walk
from . import ddl
from .core import add_check, add_fk, add_index, drop_col, find_col, get_table, set_col, set_pk, snake
from .exterm import atom, atoms, call, statements

SKIP_DIRS = {"deps", "_build", "node_modules", ".git", ".elixir_ls", "assets", "priv", ".archplan", "cover", "test"}

TYPES = {"id": "bigint", "binary_id": "uuid", "integer": "integer", "float": "double precision",
         "boolean": "boolean", "string": "varchar", "text": "text", "map": "jsonb", "binary": "bytea",
         "decimal": "numeric", "date": "date", "time": "time", "time_usec": "time",
         "naive_datetime": "timestamp", "naive_datetime_usec": "timestamp", "utc_datetime": "timestamp",
         "utc_datetime_usec": "timestamp", "uuid": "uuid", "serial": "serial", "bigserial": "bigserial",
         "bigint": "bigint", "jsonb": "jsonb", "json": "json", "citext": "citext", "timestamptz": "timestamptz",
         "duration": "interval", "smallint": "smallint"}

ON_DELETE = {"delete_all": "CASCADE", "nilify_all": "SET NULL", "restrict": "RESTRICT", "nothing": "NO ACTION",
             "nilify": "SET NULL"}


def db_type(v) -> str:
    """An Ecto or migration type, as a SQL-like type name."""
    a = atom(v)
    if a is not None and not isinstance(v, str):
        return TYPES.get(a, a)
    if isinstance(v, str):
        return v.lower()
    if isinstance(v, tuple) and len(v) >= 2 and atom(v[0]) == "array":
        return db_type(v[1]) + "[]"
    if isinstance(v, tuple) and len(v) >= 2 and atom(v[0]) == "map":
        return "jsonb"
    if isinstance(v, tuple) and v and v[0] == "alias":
        return "enum" if v[1] == "Ecto.Enum" else v[1]
    if isinstance(v, tuple) and v and v[0] == "call" and v[1].endswith("parameterized"):
        return "parameterized"
    return "unknown"


def source_files(root: Path, files: list | None = None) -> list:
    if files is not None:
        return [f for f in files if f.endswith(".ex")]
    from ..exclude import load
    out = []
    for d, dirs, names in safe_walk(root, SKIP_DIRS, exclude=load(root)):
        out += [os.path.relpath(os.path.join(d, n), root) for n in names if n.endswith(".ex")]
    return sorted(out)


# ---------- modules and aliases ----------

MAX_MODULES = 1000  # defmodules read per file
MAX_SCHEMAS = 20    # schema blocks read per file
DEFMODULE = re.compile(r"^([ \t]*)defmodule\s+([\w.]+)\s+do\b", re.M)


def modules_at(text: str) -> list:
    """(line, indent, full module name) for each defmodule, nested by indentation."""
    out, stack = [], []
    line, pos = 1, 0
    for m in DEFMODULE.finditer(text):
        if len(out) >= MAX_MODULES:
            break
        ind = len(m.group(1).expandtabs())
        line += text.count("\n", pos, m.start())
        pos = m.start()
        while stack and stack[-1][0] >= ind:
            stack.pop()
        name = m.group(2)
        full = f"{stack[-1][1]}.{name}" if stack and not name.startswith("Elixir.") else name
        stack.append((ind, full))
        out.append((line, ind, full))
    return out


def module_at(mods: list, line: int) -> str | None:
    best = None
    for ln, _, name in mods:
        if ln <= line:
            best = name
    return best


ALIAS = re.compile(r"^[ \t]*alias\s+([\w.]+)(?:\.\{([^}]{0,4000})\})?(?:\s*,\s*as:\s*([\w.]+))?", re.M)


def aliases(text: str, module: str | None) -> dict:
    out = {}
    for m in ALIAS.finditer(text):
        base = m.group(1).replace("__MODULE__", module or "__MODULE__")
        if m.group(2):
            for x in m.group(2).split(","):
                x = x.strip()
                if x:
                    out[x.split(".")[-1]] = f"{base}.{x}"
        elif m.group(3):
            out[m.group(3)] = base
        else:
            out[base.split(".")[-1]] = base
    return out


def resolve(name: str, als: dict, module: str | None) -> str:
    if name == "__MODULE__":
        return module or name
    if name.startswith("__MODULE__."):
        return (module or "__MODULE__") + name[len("__MODULE__"):]
    head, _, rest = name.partition(".")
    if head in als:
        return als[head] + ("." + rest if rest else "")
    return name


def attribute(text: str, name: str):
    """The value of `@name value` in a file, when it is a literal."""
    m = re.search(rf"^[ \t]*@{name}\s+(?!\w+\s*\()", text, re.M)
    if not m:
        return None
    lines = text[m.end():].split("\n")
    for _, stmt in statements(lines):
        r = call("x " + stmt)
        return r[1][0] if r and r[1] else None
    return None


# ---------- schemas ----------

SCHEMA = re.compile(r"^([ \t]*)schema\s*(?:\(\s*)?\"([^\"]+)\"\s*(?:,\s*)?(?:do\b|\))", re.M)


def block(lines: list, start: int, indent: int) -> list:
    """The lines after lines[start] up to the `end` at the given indentation."""
    out = []
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if s.strip() == "end" and len(s) - len(s.lstrip()) <= indent:
            break
        out.append(s)
    return out


def read_schemas(root: Path, files: list | None = None) -> dict:
    """{"tables", "maps": {table: {"module", "file", "line"}}, "modules": {module: table},
    "pending": [...]} from the Ecto schemas under root."""
    tables, maps, modules, pending = {}, {}, {}, []
    texts = {}
    for rel in source_files(root, files):
        try:
            text = read_text(root, rel)
        except OSError:
            continue
        if "schema " not in text and "schema(" not in text:
            continue
        texts[rel] = text
        mods = None
        lines = text.split("\n")
        line, quotes, pos = 1, 0, 0
        for k, m in enumerate(SCHEMA.finditer(text)):
            if k >= MAX_SCHEMAS:
                break  # a real file has one or two; more is hostile or generated
            if mods is None:
                mods = modules_at(text)
            line += text.count("\n", pos, m.start())
            quotes += text.count('"""', pos, m.start())
            pos = m.start()
            if quotes % 2:
                continue  # an example in a @moduledoc
            module = module_at(mods, line)
            if "Schema" not in text:
                continue
            table = m.group(2)
            als = aliases(text, module)
            body = block(lines, line - 1, len(m.group(1).expandtabs()))
            t = get_table(tables, table)
            maps.setdefault(table, {"module": module, "file": rel, "line": line})
            if module:
                modules[module] = table
            # primary key, from the attributes before the schema
            head = text[: m.start()]
            pk_attr = re.findall(r"@primary_key\s+(false|\{[^}]{0,500}\})", head)
            fk_type = re.findall(r"@foreign_key_type\s+(:\w+)", head)
            fk_t = db_type(("atom", fk_type[-1][1:])) if fk_type else "bigint"
            if pk_attr and pk_attr[-1] == "false":
                pass
            elif pk_attr:
                r = call("x " + pk_attr[-1])
                tup = r[1][0] if r and r[1] else ()
                if isinstance(tup, tuple) and len(tup) >= 2:
                    set_col(t, atom(tup[0]), db_type(tup[1]), False, replace=False)
                    set_pk(t, [atom(tup[0])])
            else:
                set_col(t, "id", "bigint", False, "autoincrement", replace=False)
                set_pk(t, ["id"])
            for ln, stmt in statements(body):
                r = call(stmt.strip())
                if not r:
                    continue
                schema_stmt(t, table, r, als, module, text, fk_t, pending, rel, line + ln)
    return {"tables": tables, "maps": maps, "modules": modules, "pending": pending, "texts": texts}


def enum_values(v, text: str, als: dict, module: str | None, root_texts: dict) -> list:
    if isinstance(v, tuple) and v and v[0] == "attr":
        v = attribute(text, v[1])
    if isinstance(v, tuple) and v and v[0] == "call" and "." in v[1]:
        mod, _, fn = v[1].rpartition(".")
        mod = resolve(mod, als, module)
        v = ("deferred", mod, fn)
        return [v]
    return [str(x) for x in atoms(v)]


def schema_stmt(t, table, r, als, module, text, fk_t, pending, rel, line):
    name, pos, kw = r
    first = atom(pos[0]) if pos else None
    if name == "field" and first:
        if kw.get("virtual") is True:
            return
        typ = db_type(pos[1]) if len(pos) > 1 else "varchar"
        col = atom(kw.get("source")) or first
        c = set_col(t, col, typ, replace=False)
        if typ == "unknown" and len(pos) > 1:
            c["type"] = "unknown"
        d = kw.get("default")
        if d is not None and not isinstance(d, (list, dict)):
            c.setdefault("default", str(atom(d) if atom(d) is not None else d).lower()
                         if isinstance(d, bool) else str(atom(d) if atom(d) is not None else d))
        if kw.get("primary_key") is True:
            set_pk(t, [*t["pk"], col] if col not in t["pk"] else t["pk"])
        if "values" in kw:
            vals = enum_values(kw["values"], text, als, module, {})
            if vals and isinstance(vals[0], tuple):
                pending.append({"table": table, "column": col, "module": vals[0][1], "fn": vals[0][2]})
            elif vals:
                c["values"] = vals
    elif name == "belongs_to" and first and len(pos) > 1:
        target = pos[1][1] if isinstance(pos[1], tuple) and pos[1][0] == "alias" else None
        fk = atom(kw.get("foreign_key")) or f"{first}_id"
        typ = db_type(kw["type"]) if "type" in kw else fk_t
        if kw.get("define_field") is not False:
            set_col(t, fk, typ, replace=False)
        if target:
            pending.append({"table": table, "fk": fk, "target": resolve(target, als, module),
                            "references": atom(kw.get("references")) or "id"})
    elif name in ("embeds_one", "embeds_many") and first:
        set_col(t, atom(kw.get("source")) or first, "jsonb", replace=False)
    elif name == "many_to_many" and first and len(pos) > 1:
        jt = kw.get("join_through")
        target = pos[1][1] if isinstance(pos[1], tuple) and pos[1][0] == "alias" else None
        if isinstance(jt, str):
            keys = kw.get("join_keys")
            if isinstance(keys, dict) and len(keys) == 2:
                (own, _), (other, _) = list(keys.items())
            else:
                own = snake((module or table).split(".")[-1]) + "_id"
                other = snake((target or first).split(".")[-1]) + "_id"
            pending.append({"join": jt, "owner_table": table, "own": own, "other": other,
                            "target": resolve(target, als, module) if target else None, "file": rel, "line": line,
                            "module": module})
    elif name == "timestamps":
        typ = db_type(kw["type"]) if "type" in kw else "timestamp"
        ins = kw.get("inserted_at", ("atom", "inserted_at"))
        upd = kw.get("updated_at", ("atom", "updated_at"))
        for x in (ins, upd):
            if x is not False and atom(x):
                set_col(t, atom(x), typ, False, replace=False)


def def_values(text: str, fn: str):
    """The literal list a `def fn` returns, directly or through an attribute."""
    m = re.search(rf"^[ \t]*def\s+{re.escape(fn)}(?:\(\))?\s*(?:,\s*do:|do\b)", text, re.M)
    if not m:
        return None
    rest = text[m.end():].split("\n")
    for _, stmt in statements(rest):
        stmt = stmt.strip()
        if not stmt or stmt == "end":
            continue
        r = call("x " + stmt)
        v = r[1][0] if r and r[1] else None
        if isinstance(v, tuple) and v and v[0] == "attr":
            v = attribute(text, v[1])
        return atoms(v) or None
    return None


def resolve_pending(found: dict, root: Path, module_files: dict) -> None:
    """Foreign keys to other schemas' tables, join tables, and enum values
    from functions in other modules."""
    tables, modules = found["tables"], found["modules"]
    by_last = {}
    for mod, tbl in modules.items():
        by_last.setdefault(mod.split(".")[-1], set()).add(tbl)

    def table_of(mod: str | None) -> str | None:
        if not mod:
            return None
        if mod in modules:
            return modules[mod]
        cands = [m for m in modules if m.endswith("." + mod)]
        if len(cands) == 1:
            return modules[cands[0]]
        return None

    for p in found["pending"]:
        if "fk" in p:
            to = table_of(p["target"])
            if to:
                add_fk(tables[p["table"]], [p["fk"]], to, [p["references"]])
        elif "join" in p:
            jt = get_table(tables, p["join"])
            set_col(jt, p["own"], "bigint", False, replace=False)
            set_col(jt, p["other"], "bigint", False, replace=False)
            add_fk(jt, [p["own"]], p["owner_table"], ["id"])
            to = table_of(p["target"])
            if to:
                add_fk(jt, [p["other"]], to, ["id"])
            found["maps"].setdefault(p["join"], {"through": p["owner_table"], "file": p["file"], "line": p["line"]})
        elif "fn" in p:
            f = module_files.get(p["module"])
            text = None
            if f:
                text = found["texts"].get(f)
                if text is None:
                    try:
                        text = read_text(root, f)
                    except OSError:
                        text = None
            vals = def_values(text, p["fn"]) if text else None
            c = find_col(tables[p["table"]], p["column"])
            if vals and c is not None:
                c["values"] = vals


# ---------- migrations ----------

def migration_dirs(root: Path) -> list:
    from ..exclude import load
    out = []
    for d, dirs, _ in safe_walk(root, SKIP_DIRS - {"priv"}, exclude=load(root)):
        if d.endswith(os.path.join("priv", "repo", "migrations")) or re.search(r"priv/\w*repo\w*/migrations$", d):
            out.append(Path(d))
            dirs[:] = []
    return out


def table_name(v) -> str | None:
    if isinstance(v, tuple) and v and v[0] == "call" and v[1] == "table" and v[2]:
        return atom(v[2][0])
    return None


def migration_type(v) -> tuple:
    """(type, fk) for an `add` type argument: references(:t, ...) gives an fk."""
    if isinstance(v, tuple) and v and v[0] == "call" and v[1] == "references" and v[2]:
        kw = v[3]
        to = atom(v[2][0])
        fk = (to, atom(kw.get("column")) or "id", ON_DELETE.get(atom(kw.get("on_delete")) or ""))
        return (db_type(kw["type"]) if "type" in kw else "bigint"), fk
    return db_type(v), None


def read_migrations(root: Path, s: ddl.Schema | None = None) -> tuple:
    """(schema, number of migration files, latest version) from every
    priv/repo/migrations directory under root."""
    s = s or ddl.Schema()
    files = sorted((f for d in migration_dirs(root) for f in d.glob("*.exs") if is_file(f)), key=lambda f: f.name)
    for f in files:
        try:
            text = read_file(f)
        except OSError:
            continue
        migration(s, text)
    latest = max((f.name.split("_")[0] for f in files), default=None)
    return s, len(files), latest


def migration(s: ddl.Schema, text: str) -> None:
    lines = text.split("\n")
    stack = []  # (kind, table) for each open do-block
    skip_depth = None
    for _, stmt in statements(lines):
        st = stmt.strip()
        if not st or st.startswith("#"):
            continue
        opens = 1 if re.search(r"\bdo\s*(#.*)?$", st) else 0
        closes = 1 if st == "end" or re.match(r"^end\b", st) else 0
        if closes and not opens:
            if stack:
                kind, _ = stack.pop()
                if skip_depth is not None and len(stack) < skip_depth:
                    skip_depth = None
            continue
        if re.match(r"defp?\s+down\b", st):
            stack.append(("def", None))
            skip_depth = len(stack)
            continue
        if skip_depth is not None:
            stack += [("other", None)] * opens
            continue
        cur = stack[-1] if stack else (None, None)
        r = call(re.sub(r"\s+do\s*$", "", st))
        if r is None:
            stack += [("other", None)] * opens
            continue
        name, pos, kw = r
        if name in ("create", "create_if_not_exists", "alter") and pos and table_name(pos[0]):
            tbl = table_name(pos[0])
            t = get_table(s.tables, tbl)
            if name != "alter" and pos[0][3].get("primary_key") is not False and not t["pk"]:
                set_col(t, "id", "bigint", False, "autoincrement", replace=False)
                set_pk(t, ["id"])
            if opens:
                stack.append(("table", tbl))
                opens -= 1
        elif name in ("create", "create_if_not_exists") and pos and isinstance(pos[0], tuple) \
                and pos[0][0] == "call" and pos[0][1] in ("index", "unique_index"):
            _, fn, a, k = pos[0]
            if len(a) >= 2:
                cols = atoms(a[1]) if isinstance(a[1], list) else [atom(a[1])]
                add_index(get_table(s.tables, atom(a[0])), [c for c in cols if c],
                          fn == "unique_index" or k.get("unique") is True)
        elif name == "create" and pos and isinstance(pos[0], tuple) and pos[0][0] == "call" \
                and pos[0][1] == "constraint" and len(pos[0][2]) >= 2:
            _, _, a, k = pos[0]
            if isinstance(k.get("check"), str):
                add_check(get_table(s.tables, atom(a[0])), f"{atom(a[1])}: {k['check']}")
        elif name in ("drop", "drop_if_exists") and pos and table_name(pos[0]):
            s.tables.pop(table_name(pos[0]), None)
        elif name == "rename" and pos and table_name(pos[0]):
            old = table_name(pos[0])
            if "to" in kw and len(pos) == 1 and table_name(kw["to"]):
                new = table_name(kw["to"])
                if old in s.tables:
                    s.tables[new] = s.tables.pop(old)
            elif len(pos) == 2 and "to" in kw:
                t = s.tables.get(old)
                c = find_col(t, atom(pos[1])) if t else None
                if c is not None:
                    c["name"] = atom(kw["to"])
        elif name == "execute" and pos and isinstance(pos[0], str):
            ddl.parse(pos[0], s)
        elif cur[0] == "table" and name in ("add", "add_if_not_exists", "modify") and len(pos) >= 2:
            t = get_table(s.tables, cur[1])
            col = atom(pos[0])
            typ, fk = migration_type(pos[1])
            null = False if kw.get("null") is False else (True if name != "modify" or kw.get("null") is True
                                                         else None)
            c = set_col(t, col, typ, null)
            if "default" in kw and kw["default"] is not None:
                d = kw["default"]
                c["default"] = (str(d).lower() if isinstance(d, bool) else
                                d[1] if isinstance(d, tuple) and d[0] in ("expr", "atom") else
                                "fragment" if isinstance(d, tuple) else str(d))
            if kw.get("primary_key") is True:
                if t["pk"] == ["id"] and col != "id":
                    t["pk"] = []
                set_pk(t, [*t["pk"], col])
            if fk and fk[0]:
                add_fk(t, [col], fk[0], [fk[1]], fk[2] or None)
        elif cur[0] == "table" and name in ("remove", "remove_if_exists") and pos:
            drop_col(get_table(s.tables, cur[1]), atom(pos[0]))
        elif cur[0] == "table" and name == "timestamps":
            t = get_table(s.tables, cur[1])
            typ = db_type(kw["type"]) if "type" in kw else "timestamp"
            for k_, dflt in (("inserted_at", "inserted_at"), ("updated_at", "updated_at")):
                x = kw.get(k_, ("atom", dflt))
                if x is not False and atom(x):
                    set_col(t, atom(x), typ, False if kw.get("null") is not True else True)
        stack += [("other", None)] * opens


def structure_version(text: str) -> str | None:
    """The latest migration version recorded in a structure.sql dump."""
    vs = re.findall(r"\((\d{14})\)", text)
    return max(vs) if vs else None
