"""Field names and field types, as the concept detectors read them.

Exporters write a field's type as free text in the source language, such as
`boolean()`, `BooleanField`, `bool` or `map[string]interface{}`. These helpers
sort that text into the few classes the detectors need."""

from __future__ import annotations

import re

from .common import COMMON_FIELDS

NIL = re.compile(r"\|\s*(nil|none|null|undefined)\b")  # no leading \s*: quadratic on long blank runs
TYPE_MAX = 300  # characters of a type string the checks look at
PRIMITIVE_ID = {"id", "integer", "int", "int32", "int64", "uint", "uint32", "uint64", "long", "bigint", "str",
                "string", "text", "uuid", "binary_id", "ecto.uuid", "guid", "number", "char", "positiveinteger",
                "biginteger", "positivebiginteger", "autofield", "bigautofield", "varchar", "bytes", "binary",
                "pos_integer", "non_neg_integer", "string.t", "uuid.uuid"}
BAG_HEADS = {"map", "dict", "object", "hash", "mapping", "%{}", "record", "hstore", "json", "jsonb"}
TEMPORAL = re.compile(r"date|time|instant|timestamp")


def words(name: str) -> list:
    """snake_case, camelCase and PascalCase names as lower-case words."""
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", name or ""):
        out += re.findall(r"[A-Z]+(?=[A-Z][a-z0-9]|$)|[A-Z]?[a-z0-9]+|[A-Z]+", part)
    return [w.lower() for w in out if w]


def key(name: str) -> str:
    """A name in one spelling: invoiceId, invoice_id and InvoiceID are invoice_id."""
    return "_".join(words(name))


def type_text(f: dict) -> str:
    """The field's type, lower case, without `| nil`, `()` and a Django `Field` suffix."""
    t = NIL.sub("", str(f.get("type") or "")[:TYPE_MAX]).strip().lower().replace("()", "")
    t = re.sub(r"^(optional|option)\[(.*)\]$", r"\2", t)
    return re.sub(r"field$", "", t.rstrip("?")).strip()


def is_flag(f: dict) -> bool:
    t = (f.get("type") or "").lower()
    return "bool" in t and "|" not in t.replace("| nil", "").replace("| none", "")


def is_temporal(f: dict) -> bool:
    return bool(TEMPORAL.search(type_text(f)))


def is_primitive_id(f: dict) -> bool:
    """A field that holds an id as a plain string or number, not as a reference."""
    if f.get("ref"):
        return False
    t = type_text(f)
    return t.lstrip(":") in PRIMITIVE_ID


def is_bag(f: dict) -> bool:
    """An untyped bag: a map, dict, any, interface{}, object or JSON value."""
    if f.get("ref"):
        return False
    t = type_text(f)
    if not t:
        return False
    if re.search(r"\b(any|unknown|jsonb?|hstore)\b(?!\.)", t) or "interface{}" in t or "interface {}" in t:
        return True
    inner = re.sub(r"^(\{:array,\s*|\[|list\[|array<|set\[)", "", t)
    inner = re.sub(r"(\}|\]|>)$", "", inner).strip()
    head = re.split(r"[<\[(\s]", inner, maxsplit=1)[0]
    return head in BAG_HEADS and not re.search(r"[<\[]", inner)


def plain_names(t: dict) -> set:
    """A type's field names without the ones every record has (id, timestamps)."""
    return {f["name"] for f in t.get("fields") or [] if f.get("name") and key(f["name"]) not in COMMON_FIELDS}
