"""Helpers for the security detectors, which read the model's declarations
(see format/annotator-plugins.md): zones per component, guard functions and
the auth of entry points, and how they match the calls in sequences."""

from __future__ import annotations

import re

from .common import namer

CALL = re.compile(r"^(?:\([^)]*\)\.)?([^(#]*)")


def decls(ctx, kind: str, key: str) -> list:
    return [d for d in ctx.model.get("declarations") or []
            if isinstance(d, dict) and (d.get("target") or {}).get("kind") == kind and d.get("key") == key]


def zones(ctx) -> dict:
    """{component: zone}."""
    return {d["target"]["id"]: d.get("value") for d in decls(ctx, "component", "zone") if d["target"].get("id")}


def short(fid: str) -> str:
    """The bare name of a function id: `a/b.go:Name`, `pkg.(*T).Name`, `M.f/2` give Name or f."""
    s = fid.rsplit(":", 1)[-1] if ":" in fid and "/" in fid.split(":")[0] else fid
    s = re.sub(r"/\d+$", "", s)
    return re.split(r"[.#]", s)[-1]


def component_of(ctx, file: str | None) -> str | None:
    """The component whose path holds file, the longest match."""
    if not file:
        return None
    best = None
    for cid, c in ctx.components.items():
        path = ((c or {}).get("path") or "").strip("/")
        if path and (file == path or file.startswith(path + "/")) and (not best or len(path) > len(best[1])):
            best = (cid, path)
    return best[0] if best else None


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


def matches(t: dict, name: str, comp: str | None) -> bool:
    """Whether a call of name on participant comp can be the declared function t."""
    return t["name"] == name and (not t["component"] or not comp or comp == t["component"])


def name(ctx, cid: str | None) -> str:
    return namer(ctx.model)(cid) if cid else "?"
