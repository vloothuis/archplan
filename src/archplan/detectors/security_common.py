"""Helpers for the security detectors, which read the model's declarations
(see format/annotator-plugins.md): zones per component, guard functions and
the auth of entry points, and how they match the calls in sequences."""

from __future__ import annotations

import re

from ..core.hotspots import Mapper
from .common import namer

CALL = re.compile(r"^(?:\([^)]*\)\.)?([^(#]*)")


def decls(ctx, kind: str, key: str) -> list:
    return [d for d in ctx.model.get("declarations") or []
            if isinstance(d, dict) and (d.get("target") or {}).get("kind") == kind and d.get("key") == key]


def zones(ctx) -> dict:
    """{component: zone}."""
    return {d["target"]["id"]: d.get("value") for d in decls(ctx, "component", "zone") if d["target"].get("id")}


def guards(ctx, level: str | None = None) -> list:
    """The declared guards as targets; with level ("authn" or "authz"), those
    of that level. A guard declared without a level counts as both."""
    return [target(ctx, d) for d in decls(ctx, "function", "guard")
            if d.get("value") is True or d.get("value") in ("authn", "authz") and level in (None, d["value"])]


def short(fid: str) -> str:
    """The bare name of a function id: `a/b.go:Name`, `pkg.(*T).Name`, `M.f/2` give Name or f."""
    s = fid.rsplit(":", 1)[-1] if ":" in fid and "/" in fid.split(":")[0] else fid
    s = re.sub(r"/\d+$", "", s)
    return re.split(r"[.#]", s)[-1]


_mappers = {}


def component_of(ctx, file: str | None) -> str | None:
    """The component that owns file: its match patterns first, then the longest path."""
    if not file:
        return None
    key = id(ctx.components)
    if key not in _mappers or _mappers[key][0] is not ctx.components:
        _mappers.clear()
        _mappers[key] = (ctx.components, Mapper(ctx.components))
    return _mappers[key][1](file.strip("/"))


def target(ctx, d: dict) -> dict:
    """{"id", "name", "component", "file", "line"} for a function or entry point declaration."""
    tid = d["target"].get("id") or ""
    at = d.get("at") or {}
    return {"id": tid, "name": at.get("name") or short(tid), "file": at.get("file"), "line": at.get("line"),
            "component": component_of(ctx, at.get("file"))}


def participant(ctx, p: str | None) -> str | None:
    """The component of a sequence participant: a component or type id; None for an outside actor."""
    if p in ctx.components:
        return p
    return ((ctx.model.get("types") or {}).get(p) or {}).get("component")


def called(message: dict) -> str:
    """The bare name of the function a sequence message calls."""
    label = message.get("label") or (message.get("id") or "").split(":", 1)[-1]
    return short(CALL.match(label.strip()).group(1).strip())


_files = {}
RECV_CALL = re.compile(r"^((?:\([^)]*\)\.)?[^(#]*)")


def tails(sid: str) -> list:
    """The ways a trace label can name the function sid: `pkg.(*T).Get`
    gives `(*T).Get` and `Get`, `pkg.T.Get` gives `T.Get` and `Get`."""
    m = re.search(r"(\([^()]*\)\.[^.()]+)$", sid)
    if m:
        return [m.group(1), sid.rsplit(".", 1)[-1]]
    segs = sid.rsplit("/", 1)[-1].split(".")
    return [".".join(segs[-2:]), segs[-1]] if len(segs) > 2 else [segs[-1]]


def call_file(ctx, message: dict) -> str | None:
    """The file of the function a sequence message calls, when its label is
    a function id the model's signatures know, such as `M.f/2`, or names a
    function of a type the model knows, such as `M.f` of type `M`. A Go trace
    labels a call `(*T).Get` on the component: the file is that of the one
    signature of that component the label names."""
    key = id(ctx.model)
    if key not in _files or _files[key][0] is not ctx.model:
        _files.clear()
        sigs = [x for x in ctx.model.get("signatures") or [] if isinstance(x, dict) and isinstance(x.get("id"), str)]
        by_tail = {}
        for x in sigs:
            for t in tails(x["id"]):
                by_tail.setdefault((x.get("component"), t), set()).add(x.get("file"))
        _files[key] = (ctx.model, {x["id"]: x.get("file") for x in sigs}, by_tail)
    label = (message.get("label") or (message.get("id") or "").split(":", 1)[-1]).strip()
    fid = CALL.match(label).group(1).strip()
    if fid in _files[key][1]:
        return _files[key][1][fid]
    owner = ((ctx.model.get("types") or {}).get(re.sub(r"/\d+$", "", fid).rpartition(".")[0]) or {})
    if isinstance(owner, dict) and owner.get("file"):
        return owner["file"]
    comp = participant(ctx, message.get("to"))
    found = _files[key][2].get((comp, RECV_CALL.match(label).group(1).strip())) if comp else None
    return next(iter(found)) if found and len(found) == 1 else None


def matches(t: dict, name: str, comp: str | None, file: str | None = None) -> bool:
    """Whether a call of name on participant comp, of a function in file when
    known, can be the declared function t. Many modules of a component can
    have a function of the same name, such as a LiveView's mount."""
    return (t["name"] == name and (not t["component"] or not comp or comp == t["component"])
            and (not file or not t.get("file") or file == t["file"]))


def name(ctx, cid: str | None) -> str:
    return namer(ctx.model)(cid) if cid else "?"
