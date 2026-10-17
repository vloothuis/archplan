"""Read Django models with the Python ast module: table names (db_table, or
the default app_label_modelname), columns and their types, foreign keys,
many-to-many through tables, unique_together, indexes and constraints, and
choices as column values. Abstract models add their fields to subclasses;
proxy models have no table; a concrete parent adds a <parent>_ptr_id key."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from ..core.safeio import read_file
from ..core.safeio import walk as safe_walk
from .core import add_check, add_fk, add_index, find_col, get_table, set_col, set_pk

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "build", "dist", "__pycache__", ".tox", ".nox",
             "site-packages", ".archplan", "migrations", "tests", "test", "static", "media"}

TYPES = {"AutoField": "integer", "BigAutoField": "bigint", "SmallAutoField": "smallint",
         "IntegerField": "integer", "BigIntegerField": "bigint", "SmallIntegerField": "smallint",
         "PositiveIntegerField": "integer", "PositiveBigIntegerField": "bigint",
         "PositiveSmallIntegerField": "smallint", "BooleanField": "boolean", "NullBooleanField": "boolean",
         "CharField": "varchar", "SlugField": "varchar", "EmailField": "varchar", "URLField": "varchar",
         "TextField": "text", "DateTimeField": "timestamptz", "DateField": "date", "TimeField": "time",
         "DurationField": "interval", "UUIDField": "uuid", "JSONField": "jsonb", "DecimalField": "numeric",
         "FloatField": "double precision", "BinaryField": "bytea", "FileField": "varchar", "ImageField": "varchar",
         "FilePathField": "varchar", "GenericIPAddressField": "inet", "IPAddressField": "inet",
         "ArrayField": "array", "HStoreField": "hstore", "PointField": "geometry", "GeometryField": "geometry",
         "PolygonField": "geometry", "MultiPolygonField": "geometry", "CICharField": "citext",
         "CIEmailField": "citext", "CITextField": "citext", "PrivateMediaFileField": "varchar"}
RELATIONS = {"ForeignKey", "OneToOneField", "ManyToManyField", "ParentalKey", "TreeForeignKey"}
NOT_COLUMNS = {"GenericForeignKey", "GenericRelation", "FkOrServiceUrlField", "ServiceUrlField", "ManyToManyRel"}
MODEL_BASES = {"Model", "AbstractUser", "AbstractBaseUser", "PolymorphicModel", "MPTTModel", "TimeStampedModel",
               "SingletonModel", "OrderedModel", "Page", "AbstractBaseSession"}
ON_DELETE = {"CASCADE": "CASCADE", "SET_NULL": "SET NULL", "PROTECT": "PROTECT", "RESTRICT": "RESTRICT",
             "DO_NOTHING": "NO ACTION", "SET_DEFAULT": "SET DEFAULT", "SET": "SET"}


def py_files(root: Path) -> list:
    from ..exclude import load
    out = []
    for d, dirs, names in safe_walk(root, SKIP_DIRS, exclude=load(root)):
        out += [Path(d) / n for n in names if n.endswith(".py") and not n.startswith("test_")]
    return sorted(out)


def last(node) -> str:
    try:
        return ast.unparse(node).split("(")[0].split(".")[-1]
    except Exception:  # noqa: BLE001
        return ""


def const(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value
    return None


def kwargs(call: ast.Call) -> dict:
    return {k.arg: k.value for k in call.keywords if k.arg}


class Cls:
    def __init__(self, node: ast.ClassDef, rel: str, app: str, label: str, text_imports_django: bool):
        self.node, self.rel, self.app, self.label = node, rel, app, label
        self.name = node.name
        self.bases = [last(b) for b in node.bases]
        self.django = text_imports_django
        self.in_models = rel.endswith("models.py") or "/models/" in rel
        self.meta = {}
        for x in node.body:
            if isinstance(x, ast.ClassDef) and x.name == "Meta":
                for y in x.body:
                    if isinstance(y, ast.Assign) and len(y.targets) == 1 and isinstance(y.targets[0], ast.Name):
                        self.meta[y.targets[0].id] = y.value

    def has_fields(self) -> bool:
        return any(isinstance(x, ast.Assign) and isinstance(x.value, ast.Call)
                   and (last(x.value.func).endswith("Field") or last(x.value.func) in RELATIONS) for x in self.node.body)

    def meta_const(self, k):
        v = self.meta.get(k)
        return const(v) if v is not None else None


def app_of(root: Path, f: Path) -> Path:
    """The Django app directory of a models file: the parent of a models/
    package, or the file's own directory."""
    d = f.parent
    if d.name == "models":
        return d.parent
    return d


def app_label(app_dir: Path, cache: dict) -> str:
    if app_dir in cache:
        return cache[app_dir]
    label = app_dir.name
    apps = app_dir / "apps.py"
    if apps.is_file():
        try:
            tree = ast.parse(read_file(apps))
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    for x in n.body:
                        if (isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)
                                and x.targets[0].id == "label" and isinstance(const(x.value), str)):
                            label = const(x.value)
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            pass
    cache[app_dir] = label
    return label


def choice_values(node: ast.ClassDef) -> list:
    """Values of a TextChoices, IntegerChoices, Enum or DjangoChoices class."""
    out = []
    for x in node.body:
        if not (isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)):
            continue
        if x.targets[0].id.startswith("_") or x.targets[0].id in ("choices", "labels", "values"):
            continue
        v = x.value
        if isinstance(v, ast.Tuple) and v.elts:
            v = v.elts[0]
        elif isinstance(v, ast.Call) and v.args and last(v.func) in ("ChoiceItem", "Choice"):
            v = v.args[0]
        c = const(v)
        if c is not None and not isinstance(c, bool):
            out.append(str(c))
    return out


def literal_choices(node) -> list:
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            if isinstance(e, (ast.Tuple, ast.List)) and e.elts:
                c = const(e.elts[0])
                if c is not None:
                    out.append(str(c))
                elif isinstance(e.elts[1] if len(e.elts) > 1 else None, (ast.Tuple, ast.List)):
                    out += literal_choices(e.elts[1])  # grouped choices
        return out
    return []


def read(root: Path) -> dict:
    """{"tables", "maps": {table: {"class", "file", "line"}}} for the Django
    models under root. Empty when there are none."""
    classes, choices, labels = [], {}, {}
    for f in py_files(root):
        try:
            text = read_file(f)
        except OSError:
            continue
        if "class " not in text:
            continue
        has_models = "django" in text or "models." in text
        is_choices = "Choices" in text or "Enum" in text or "ChoiceItem" in text
        if not has_models and not is_choices:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        rel = f.relative_to(root).as_posix()
        app = app_of(root, f)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {last(b) for b in node.bases}
            if any("Choices" in b or b.endswith("Enum") for b in bases):
                choices.setdefault(node.name, choice_values(node))
            classes.append(Cls(node, rel, app.relative_to(root).as_posix(), app_label(app, labels), has_models))
    return build(classes, choices)


def build(classes: list, choices: dict) -> dict:
    by_name = {}
    for c in classes:
        by_name.setdefault(c.name, []).append(c)
    models = {}

    def is_model(c: Cls, seen=()) -> bool:
        if id(c) in models:
            return models[id(c)]
        res = False
        for b in c.bases:
            if b in MODEL_BASES and c.django:
                res = True
            elif b in by_name and b not in seen:
                res = any(is_model(p, (*seen, c.name)) for p in by_name[b] if p is not c)
            if res:
                break
        if not res and c.django and c.in_models and not re.search(r"Mixin$|^Base|^Abstract", c.name) \
                and any(b not in by_name for b in c.bases) and c.has_fields():
            res = True  # a model on a base class from a library, in a models module
        models[id(c)] = res
        return res

    model_classes = [c for c in classes if is_model(c)]

    def find(name: str, prefer: Cls) -> Cls | None:
        if "." in name:
            label, _, name = name.rpartition(".")
            cands = [c for c in by_name.get(name, []) if c in model_set and c.label == label.split(".")[-1]]
            if cands:
                return cands[0]
        cands = [c for c in by_name.get(name, []) if c in model_set]
        if not cands:
            return None
        same = [c for c in cands if c.app == prefer.app]
        return (same or cands)[0]

    model_set = set(model_classes)

    def abstract(c: Cls) -> bool:
        return c.meta_const("abstract") is True

    def table_of(c: Cls) -> str | None:
        if abstract(c):
            return None
        if c.meta_const("proxy") is True:
            for b in c.bases:
                p = find(b, c)
                if p is not None and p is not c:
                    return table_of(p)
            return None
        t = c.meta_const("db_table")
        if isinstance(t, str):
            return t
        label = c.meta_const("app_label") or c.label
        return f"{label}_{c.name.lower()}"

    tables, maps, pending = {}, {}, []

    def field_stmts(c: Cls, seen=()):
        """Field assignments of c, with those of its abstract parents first."""
        out = []
        for b in c.bases:
            p = find(b, c)
            if p is not None and p is not c and p.name not in seen and abstract(p):
                out += field_stmts(p, (*seen, c.name))
        for x in c.node.body:
            if (isinstance(x, ast.Assign) and len(x.targets) == 1 and isinstance(x.targets[0], ast.Name)
                    and isinstance(x.value, ast.Call)):
                out.append((x.targets[0].id, x.value))
            elif isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name) and isinstance(x.value, ast.Call):
                out.append((x.target.id, x.value))
        return out

    for c in model_classes:
        if c.meta_const("proxy") is True:
            continue
        tname = table_of(c)
        if not tname:
            continue
        t = get_table(tables, tname)
        maps.setdefault(tname, {"class": c.name, "file": c.rel, "line": c.node.lineno})
        cols = {}  # field name -> column
        stmts = field_stmts(c)
        parent_link = any(last(call.func) == "OneToOneField" and const(kwargs(call).get("parent_link")) is True
                          for _, call in stmts)
        for b in c.bases:
            p = find(b, c)
            if p is not None and p is not c and not abstract(p) and table_of(p) and not parent_link:
                col = f"{p.name.lower()}_ptr_id"
                set_col(t, col, "bigint", False)
                set_pk(t, [col])
                pending.append((tname, [col], p, c, "CASCADE"))
                add_index(t, [col], True)
        for name, call in stmts:
            kind = last(call.func)
            if kind in NOT_COLUMNS or not (kind.endswith("Field") or kind in RELATIONS or kind.endswith("Key")):
                continue
            kw = kwargs(call)
            col = const(kw.get("db_column")) or name
            if kind == "ManyToManyField":
                target = call.args[0] if call.args else kw.get("to")
                pending.append(("m2m", tname, name, target, kw.get("through"), c))
                continue
            null = const(kw.get("null")) is True
            if kind in RELATIONS:
                target = call.args[0] if call.args else kw.get("to")
                col = const(kw.get("db_column")) or f"{name}_id"
                set_col(t, col, "bigint", null)
                od = kw.get("on_delete") or (call.args[1] if len(call.args) > 1 else None)
                pending.append((tname, [col], target, c, ON_DELETE.get(last(od)) if od is not None else None))
                add_index(t, [col], kind == "OneToOneField" or const(kw.get("unique")) is True)
                if const(kw.get("primary_key")) is True or const(kw.get("parent_link")) is True:
                    set_pk(t, [col])
                cols[name] = col
                continue
            typ = TYPES.get(kind, kind)
            if typ == "varchar" and isinstance(const(kw.get("max_length")), int):
                typ = f"varchar({const(kw['max_length'])})"
            if kind == "DecimalField" and const(kw.get("max_digits")) is not None:
                typ = f"numeric({const(kw.get('max_digits'))},{const(kw.get('decimal_places')) or 0})"
            if kind == "ArrayField" and (call.args or kw.get("base_field")):
                base = call.args[0] if call.args else kw["base_field"]
                typ = TYPES.get(last(base.func) if isinstance(base, ast.Call) else last(base), "unknown") + "[]"
            if kind == "NullBooleanField":
                null = True
            d = kw.get("default")
            default = None
            if d is not None:
                default = const(d)
                default = ast.unparse(d) if default is None and not isinstance(d, ast.Constant) else default
                if isinstance(default, bool):
                    default = str(default).lower()
            vals = []
            ch = kw.get("choices")
            if ch is not None:
                if isinstance(ch, (ast.List, ast.Tuple)):
                    vals = literal_choices(ch)
                else:
                    src = ast.unparse(ch).split(".")
                    nm = src[-2] if src[-1] in ("choices", "CHOICES") and len(src) > 1 else src[-1]
                    vals = choices.get(nm) or []
            colrec = set_col(t, col, typ, null, default, vals or None)
            if const(kw.get("primary_key")) is True:
                set_pk(t, [col])
                colrec["null"] = False
            if const(kw.get("unique")) is True:
                add_index(t, [col], True)
            elif const(kw.get("db_index")) is True:
                add_index(t, [col], False)
            cols[name] = col
        if not t["pk"]:
            set_col(t, "id", "bigint", False, "autoincrement")
            t["columns"].insert(0, t["columns"].pop())
            set_pk(t, ["id"])
        meta(c, t, cols)

    def target_of(target, c: Cls) -> Cls | None:
        if target is None:
            return None
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            return c if target.value == "self" else find(target.value, c)
        return find(last(target), c)

    for p in pending:
        if p[0] == "m2m":
            _, tname, name, target, through, c = p
            other = target_of(target, c)
            if through is not None:
                th = target_of(through, c)
                if th is not None and table_of(th):
                    maps.setdefault(table_of(th), {"class": th.name, "file": th.rel, "line": th.node.lineno})
                continue
            jt = f"{tname}_{name}"
            t = get_table(tables, jt)
            set_col(t, "id", "bigint", False, "autoincrement")
            set_pk(t, ["id"])
            own = f"{c.name.lower()}_id"
            oth = f"{other.name.lower()}_id" if other is not None else f"{name}_id"
            if other is c:
                own, oth = f"from_{c.name.lower()}_id", f"to_{c.name.lower()}_id"
            set_col(t, own, "bigint", False)
            set_col(t, oth, "bigint", False)
            add_fk(t, [own], tname, ["id"], "CASCADE")
            if other is not None and table_of(other):
                add_fk(t, [oth], table_of(other), ["id"], "CASCADE")
            add_index(t, [own, oth], True)
            maps.setdefault(jt, {"through": tname, "file": c.rel, "line": c.node.lineno})
            continue
        tname, cols, target, c, on_delete = p
        other = target if isinstance(target, Cls) else target_of(target, c)
        if other is not None and table_of(other):
            pk = tables.get(table_of(other), {}).get("pk") or ["id"]
            add_fk(tables[tname], cols, table_of(other), pk[:1], on_delete)
    return {"tables": tables, "maps": maps}


def names(node) -> list:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [const(e) for e in node.elts if isinstance(const(e), str)]
    return []


def meta(c: Cls, t: dict, cols: dict) -> None:
    col = lambda f: cols.get(f.lstrip("-"), f.lstrip("-"))
    ut = c.meta.get("unique_together")
    if isinstance(ut, (ast.List, ast.Tuple)):
        groups = [ut] if all(isinstance(const(e), str) for e in ut.elts) else ut.elts
        for g in groups:
            if names(g):
                add_index(t, [col(f) for f in names(g)], True)
    it = c.meta.get("index_together")
    if isinstance(it, (ast.List, ast.Tuple)):
        for g in ([it] if all(isinstance(const(e), str) for e in it.elts) else it.elts):
            if names(g):
                add_index(t, [col(f) for f in names(g)], False)
    for key in ("indexes", "constraints"):
        lst = c.meta.get(key)
        if not isinstance(lst, (ast.List, ast.Tuple)):
            continue
        for e in lst.elts:
            if not isinstance(e, ast.Call):
                continue
            kind = last(e.func)
            kw = kwargs(e)
            if kind == "CheckConstraint":
                chk = kw.get("check") or kw.get("condition")
                nm = const(kw.get("name"))
                if chk is not None:
                    add_check(t, (f"{nm}: " if nm else "") + ast.unparse(chk))
                    for q in ast.walk(chk):
                        if isinstance(q, ast.keyword) and q.arg and q.arg.endswith("__in"):
                            vals = [str(const(x)) for x in getattr(q.value, "elts", []) if const(x) is not None]
                            cc = find_col(t, col(q.arg[:-4]))
                            if cc is not None and vals:
                                cc["values"] = vals
                continue
            fields = names(kw.get("fields")) or [const(a) for a in e.args if isinstance(const(a), str)]
            if fields:
                add_index(t, [col(f) for f in fields], kind == "UniqueConstraint")
