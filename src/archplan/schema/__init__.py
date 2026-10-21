"""The schema section of the as-is model: database tables, their owners and
which components read or write them.

Sources, found without a live database:

  django    Django models (Python ast)
  ecto      Ecto schemas, plus priv/repo/migrations for columns the schemas leave out
  rails     db/schema.rb, plus app/models for the table's class and enums
  prisma    schema.prisma
  sql       schema.sql or structure.sql (also pg_dump --schema-only output)

Live sources run only on request (`archplan schema --sqlite FILE` or
`archplan schema --postgres --url-env VAR`). Their result is kept in
.archplan/schema-live.json, so later exports keep it; the URL is not.

Owners: config.json's schema.owners first, then the ORM type's component
(or the declaring file's), the component that writes the table most, the
owner of a join table's table, and last the component whose files hold the
CREATE TABLE statement (see owners).

build() returns the section; enrich() adds it to the project's model after
an export."""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import access as access_mod
from . import ddl, django, ecto, prisma, rails
from . import owners as owners_mod
from ..core.safeio import model_base, prefix_of, read_file, read_text
from ..core.safeio import walk as safe_walk
from .core import finish, merge_table

INFRA = {"schema_migrations", "ar_internal_metadata", "django_migrations", "_prisma_migrations",
         "knex_migrations", "knex_migrations_lock", "flyway_schema_history", "databasechangelog",
         "databasechangeloglock", "goose_db_version"}
SKIP_DIRS = {".git", "node_modules", "deps", "_build", ".venv", "venv", "vendor", "dist", "build", ".archplan",
             "__pycache__", "tmp", "coverage", "test", "tests", "spec", "e2e", "fixtures", "fixture"}
SQL_NAMES = ("schema.sql", "structure.sql")
SQL_MAX = 8 << 20  # bytes; a larger SQL dump is skipped
LIVE = "schema-live.json"


def walk(root: Path, depth: int = 4):
    from ..exclude import load
    base = len(root.parts)
    for d, dirs, names in safe_walk(root, SKIP_DIRS, exclude=load(root)):
        if len(Path(d).parts) - base >= depth:
            dirs[:] = []
        yield Path(d), names


def detect(root: Path) -> list:
    """[{"kind", "path"}] for each schema source under root."""
    out = []
    mix, py = [], False
    for d, names in walk(root):
        rel = d.relative_to(root).as_posix()
        rel = "" if rel == "." else rel + "/"
        for n in names:
            if n in SQL_NAMES:
                out.append({"kind": "sql", "path": rel + n})
            elif n == "schema.prisma":
                out.append({"kind": "prisma", "path": rel + n})
            elif n == "schema.rb" and d.name == "db":
                out.append({"kind": "rails", "path": rel + n})
            elif n == "mix.exs":
                try:
                    if "ecto" in read_file(d / n):
                        mix.append(rel.rstrip("/") or ".")
                except OSError:
                    pass
            elif n == "manage.py" or (n in ("pyproject.toml", "setup.cfg", "setup.py", "Pipfile")
                                      or n.startswith("requirements")) and not py:
                try:
                    py = py or n == "manage.py" or re.search(r"(?i)\bdjango\b", read_file(d / n))
                except OSError:
                    pass
    if py:
        out.insert(0, {"kind": "django"})
    for m in mix:
        if not any(m != x and m.startswith(x + "/") for x in mix):
            out.insert(0, {"kind": "ecto", **({"path": m} if m != "." else {})})
    return out


def type_index(model: dict) -> dict:
    """(file, name) and module id -> type id."""
    idx = {}
    for tid, t in (model.get("types") or {}).items():
        if not isinstance(t, dict):
            continue
        idx[tid] = tid
        if t.get("file") and t.get("name"):
            idx.setdefault((t["file"], t["name"].split(".")[-1]), tid)
    return idx


def read_sources(root: Path, sources: list, live: dict | None, model: dict) -> tuple:
    """(tables, enums, maps, used sources)."""
    layers = []  # (priority, tables, enums), highest first
    maps, used = {}, []
    ecto_migrations = []
    for s in sources:
        k = s["kind"]
        try:
            if k == "django":
                r = django.read(root)
                if not r["tables"]:
                    continue
                layers.append((3, r["tables"], {}))
                maps.update(r["maps"])
            elif k == "ecto":
                base = root / s.get("path", ".")
                files = [str((base / f).relative_to(root)) for f in ecto.source_files(base)]
                r = ecto.read_schemas(root, files)
                # type files are relative to the model's base; here paths are relative to root
                pre = prefix_of(root, model_base(root, model))
                mods = {tid: t["file"][len(pre):] for tid, t in (model.get("types") or {}).items()
                        if isinstance(t, dict) and t.get("file") and t["file"].startswith(pre)}
                ecto.resolve_pending(r, root, mods)
                mig, n, latest = ecto.read_migrations(base)
                s = {**s, "schemas": len(r["tables"])}
                if n:
                    s["migrations"] = n
                    s["latest"] = latest
                    ecto_migrations.append((mig, bool(r["tables"])))
                if not r["tables"] and not n:
                    continue
                layers.append((3, r["tables"], {}))
                for table, mp in r["maps"].items():
                    maps.setdefault(table, mp)
            elif k == "rails":
                r = rails.read(root, root / s["path"])
                layers.append((1, r["tables"], r["enums"]))
                maps.update({t: m for t, m in r["maps"].items() if t not in maps})
            elif k == "prisma":
                r = prisma.read(read_text(root, s["path"]), s["path"])
                layers.append((1, r["tables"], r["enums"]))
                maps.update({t: m for t, m in r["maps"].items() if t not in maps})
            elif k == "sql":
                text = read_text(root, s["path"], SQL_MAX)
                ver = ecto.structure_version(text)
                latest = max((x.get("latest") or "" for x in used if x["kind"] == "ecto"), default="")
                if ver and latest and ver < latest:
                    used.append({**s, "skipped": f"older than the migrations ({ver} < {latest})"})
                    continue
                r = ddl.parse(text)
                if not r["tables"]:
                    continue
                layers.append((2, r["tables"], r["enums"]))
            else:
                continue
        except (OSError, UnicodeDecodeError) as e:
            used.append({**s, "error": str(e)[:200]})
            continue
        used.append(s)
    if live:
        layers.append((0, live.get("tables") or {}, live.get("enums") or {}))
        used.append({"kind": live.get("kind", "sqlite"), **({"path": live["path"]} if live.get("path") else {})})
    layers.sort(key=lambda x: x[0])
    tables, enums = {}, {}
    for _, tbls, ens in layers:
        for name, t in tbls.items():
            if name in INFRA:
                continue
            if name in tables:
                merge_table(tables[name], t)
            else:
                tables[name] = t
        for k_, v in ens.items():
            enums.setdefault(k_, v)
    # Migrations fill in the columns of known tables; they add tables only when
    # there are no schemas, as helper functions and string SQL make them unreliable.
    for mig, have_schemas in ecto_migrations:
        for name, t in mig.tables.items():
            if name in INFRA:
                continue
            if name in tables:
                merge_table(tables[name], t)
            elif not have_schemas:
                tables[name] = t
        for k_, v in mig.enums.items():
            enums.setdefault(k_, v)
    finish(tables)
    for t in tables.values():
        cols = {c["name"] for c in t["columns"]}
        t["fks"] = [f for f in t["fks"] if all(c in cols for c in f["columns"])]
        t["indexes"] = [i for i in t["indexes"] if all(c in cols or not re.fullmatch(r"\w+", c)
                                                       for c in i["columns"])]
    return tables, enums, maps, used


def build(root: Path, model: dict, sources: list | None = None, live: dict | None = None,
          owners=None) -> dict | None:
    """The schema section for the model, or None when there is no source.
    owners is config.json's schema.owners: {table name or glob: component}."""
    sources = detect(root) if sources is None else sources
    if not sources and not live:
        return None
    tables, enums, raw_maps, used = read_sources(root, sources, live, model)
    if not tables:
        return None
    # The sources give paths relative to root; the model's paths, and so the
    # access scan, are relative to its base: the repository root when the
    # project sits in a subfolder (mono/core has paths like core/lib/x.ex).
    base = model_base(root, model)
    pre = prefix_of(root, base)
    raw_maps = {t: {**mp, "file": pre + mp["file"]} if mp.get("file") else mp for t, mp in raw_maps.items()}
    idx = type_index(model)
    maps = {}
    for table, mp in raw_maps.items():
        if table not in tables:
            continue
        tid = None
        if mp.get("module") and mp["module"] in idx:
            tid = mp["module"]
        elif mp.get("file") and (mp.get("class") or mp.get("module")):
            tid = idx.get((mp["file"], (mp.get("class") or mp.get("module")).split(".")[-1]))
        comp = (model.get("types") or {}).get(tid, {}).get("component") if tid else None
        comp = comp or (access_mod.component_of(model, mp.get("file")) if not mp.get("through") else None)
        out = {k: v for k, v in (("type", tid), ("component", comp)) if v}
        if comp:
            out["via"] = "type" if tid else "file"
        maps[table] = {**out, **{k: mp[k] for k in ("file", "line", "module", "class", "through") if k in mp}}
    acc = access_mod.access(model, base, tables, maps, prisma=any(s["kind"] == "prisma" for s in used))
    access_mod.owners(maps, acc, tables)
    for table, comp in owners_mod.created(model, base, root, pre, tables, walk, SQL_NAMES).items():
        if not (maps.get(table) or {}).get("component"):
            maps.setdefault(table, {}).update({"component": comp, "via": "create"})
    warnings = owners_mod.apply_config(maps, tables, owners, model.get("components") or {})
    # the private keys used for access mapping stay out of the model
    clean = {t: {k: v for k, v in mp.items() if k in ("type", "component", "via", "through")}
             for t, mp in sorted(maps.items()) if mp.get("component") or mp.get("type")}
    return {"sources": used, "tables": dict(sorted(tables.items())), "enums": dict(sorted(enums.items())),
            "maps": clean, "access": acc, **({"warnings": warnings} if warnings else {})}


def read_live(p) -> dict | None:
    from ..core.project import read_json
    return read_json(p.path(LIVE))


def enrich(p, live: dict | None = None, force: bool = False) -> dict | None:
    """Add the schema section to the project's as-is model. Returns it, or
    None when the model has one from the exporter or there is no source."""
    from ..core.project import read_json, write_json
    model = read_json(p.model_path)
    if not model:
        return None
    if model.get("schema") and not force and not (model["schema"].get("generator") == "archplan"):
        return None
    if live is not None:
        write_json(p.path(LIVE), live)
    cfg = p.config.get("schema")
    sch = build(p.root, model, live=live if live is not None else read_live(p),
                owners=cfg.get("owners") if isinstance(cfg, dict) else None)
    if sch is None:
        return None
    sch["generator"] = "archplan"
    model["schema"] = sch
    src = model.setdefault("source", {})
    caps = src.get("capabilities")
    if isinstance(caps, list) and "schema" not in caps:
        caps.append("schema")
    write_json(p.model_path, model)
    return sch


# ---------- views ----------

def summary(sch: dict) -> dict:
    tables = sch.get("tables") or {}
    maps = sch.get("maps") or {}
    owned = [t for t in tables if (maps.get(t) or {}).get("component")]
    cross = cross_fks(sch)
    return {"tables": len(tables), "owned": len(owned), "enums": len(sch.get("enums") or {}),
            "fks": sum(len(t.get("fks") or []) for t in tables.values()), "cross_component_fks": len(cross),
            "access": len(sch.get("access") or []),
            "sources": [s["kind"] + (f" {s['path']}" if s.get("path") else "") +
                        (f" (skipped: {s['skipped']})" if s.get("skipped") else "") for s in sch.get("sources") or []]}


def owner(sch: dict, table: str) -> str | None:
    return ((sch.get("maps") or {}).get(table) or {}).get("component")


def cross_fks(sch: dict) -> list:
    out = []
    for name, t in (sch.get("tables") or {}).items():
        a = owner(sch, name)
        for f in t.get("fks") or []:
            b = owner(sch, f["to"])
            if a and b and a != b:
                out.append({"from_table": name, "to_table": f["to"], "columns": f["columns"], "from": a, "to": b,
                            **({"on_delete": f["on_delete"]} if f.get("on_delete") else {})})
    return out


def text(sch: dict, what: str = "show") -> str:
    s = summary(sch)
    lines = [f"{s['tables']} tables, {s['owned']} with an owner, {s['fks']} foreign keys "
             f"({s['cross_component_fks']} across components), {s['enums']} enums",
             "sources: " + (", ".join(s["sources"]) or "none")]
    tables, maps = sch.get("tables") or {}, sch.get("maps") or {}
    if what == "tables":
        for name, t in tables.items():
            mp = maps.get(name) or {}
            lines.append(f"\n{name}  owner {mp.get('component') or '-'}"
                         + (f" ({mp['via']})" if mp.get("via") else "") + (f"  type {mp['type']}" if mp.get("type") else ""))
            for c in t["columns"]:
                flags = [*(["pk"] if c["name"] in t["pk"] else []), *(["null"] if c.get("null") else [])]
                fk = next((f"-> {f['to']}" for f in t["fks"] if f["columns"] == [c["name"]]), "")
                vals = f" in {{{', '.join(c['values'][:8])}{', ...' if len(c['values']) > 8 else ''}}}" \
                    if c.get("values") else ""
                lines.append(f"  {c['name']:<32} {c['type']:<24} {' '.join(flags):<8} {fk}{vals}")
        return "\n".join(lines)
    by_owner = {}
    for name in tables:
        by_owner.setdefault((maps.get(name) or {}).get("component") or "(no owner)", []).append(name)
    lines.append("")
    for comp, names in sorted(by_owner.items(), key=lambda kv: (kv[0] == "(no owner)", kv[0])):
        lines.append(f"{comp}: {', '.join(sorted(names))}")
    cross = cross_fks(sch)
    if cross:
        lines.append("\nforeign keys across components:")
        for x in cross[:40]:
            lines.append(f"  {x['from_table']}.{','.join(x['columns'])} -> {x['to_table']}  ({x['from']} -> {x['to']})")
        if len(cross) > 40:
            lines.append(f"  ... {len(cross) - 40} more")
    return "\n".join(lines)
