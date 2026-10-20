"""Calls between processes: a browser's HTTP request and the server code
that serves it (see format/archmodel.md, "Calls between processes").

After an export, and after the parts of several exporters are merged,
link() does three things to the model's sequences:

1. A participant that names a folder or file of the repository, such as
   frontend/src/api, becomes the component that owns it.
2. Stitching: a client's HTTP call with a `span` is joined with the server
   sequence whose first message has the same `span`. The server's messages
   go under the call, and the server sequence is removed.
3. Route join: an HTTP call with a `route` is matched with the model's
   `routes`. The call then goes to the route's component, and, when the
   next message is not the handler already, a message that calls the
   handler is added under it, marked "joined": "route". The security
   detectors and the blast radius then see the browser call reach the
   server's entry point."""

from __future__ import annotations

import re

from .hotspots import Mapper

WILD = re.compile(r"^(?:\{[^{}]*\}|:[A-Za-z_]\w*|\$\{[^{}]*\}|\[[^\[\]]*\]|\*)$")
CALL = re.compile(r"^((?:\([^)]*\)\.)?[^(#]*)")
REST = re.compile(r"^(?:\{[^{}]*\.\.\.\}|\*\*|\[\.\.\.[^\[\]]*\])$")


def link(m: dict) -> dict:
    """Change m in place. Returns counts: {"participants", "stitched", "joined"}."""
    seqs = m.get("sequences")
    out = {"participants": 0, "stitched": 0, "joined": 0}
    if not isinstance(seqs, dict) or not seqs:
        return out
    out["participants"] = resolve_participants(m)
    out["stitched"] = stitch(m)
    out["joined"] = join_routes(m)
    return out


def known(m: dict, p) -> bool:
    return p in (m.get("components") or {}) or p in (m.get("types") or {})


def messages(s) -> list:
    return [x for x in (s.get("messages") or []) if isinstance(x, dict)] if isinstance(s, dict) else []


# ---------- participants ----------

def resolve_participants(m: dict) -> int:
    mapper = Mapper({c: v for c, v in (m.get("components") or {}).items() if isinstance(v, dict)})
    cache, n = {}, 0

    def of(p):
        if not isinstance(p, str) or "/" not in p or known(m, p):
            return p
        if p not in cache:
            q = p.strip("/")
            cache[p] = mapper(q) or mapper(q + "/_") or p
        return cache[p]

    for s in m["sequences"].values():
        if not isinstance(s, dict):
            continue
        for msg in messages(s):
            for k in ("from", "to"):
                new = of(msg.get(k))
                if new != msg.get(k):
                    msg[k] = new
                    n += 1
        if isinstance(s.get("participants"), list):
            s["participants"] = list(dict.fromkeys(of(p) for p in s["participants"]))
    return n


# ---------- message ids ----------

def call_part(msg: dict) -> str:
    """The function part of a message id: `a>b:Get#2` gives Get."""
    mid, prefix = msg.get("id") or "", f"{msg.get('_from', msg.get('from'))}>{msg.get('_to', msg.get('to'))}:"
    part = mid[len(prefix):].rsplit("#", 1)[0] if isinstance(mid, str) and mid.startswith(prefix) else ""
    return part or called(msg)


def called(msg: dict) -> str:
    """The call a message's label shows, without arguments: `(*T).Get(id)` gives `(*T).Get`."""
    return CALL.match((msg.get("label") or "").strip()).group(1).strip()


def renumber(msgs: list) -> None:
    """Give each message the id `from>to:call#n` again, after a stitch changed
    its endpoints or put it next to other messages."""
    count = {}
    for msg in msgs:
        key = f"{msg['from']}>{msg['to']}:{call_part(msg)}"
        count[key] = count.get(key, 0) + 1
        msg["id"] = f"{key}#{count[key]}"
        msg.pop("_from", None)
        msg.pop("_to", None)


# ---------- stitching ----------

def is_hop(msg: dict) -> bool:
    return msg.get("via") == "http" or (msg.get("async") and isinstance(msg.get("route"), dict))


def stitch(m: dict) -> int:
    seqs = m["sequences"]
    roots = {}  # span -> server sequence id
    for sid, s in seqs.items():
        ms = messages(s)
        if ms and isinstance(ms[0].get("span"), str) and ms[0]["span"] and not is_hop(ms[0]):
            roots.setdefault(ms[0]["span"], sid)
    n, gone = 0, set()
    for sid in list(seqs):
        s = seqs[sid]
        if sid in gone or not isinstance(s, dict):
            continue
        ms, out, changed = messages(s), [], False
        for msg in ms:
            out.append(msg)
            span = msg.get("span")
            server = roots.get(span) if isinstance(span, str) and is_hop(msg) else None
            if not server or server == sid or server in gone or server not in seqs:
                continue
            out += graft(m, s, msg, server, seqs[server])
            gone.add(server)
            s.setdefault("stitched", []).append(server)
            changed = True
            n += 1
        if changed:
            renumber(out)
            s["messages"] = out
    for sid in gone:
        seqs.pop(sid, None)
    return n


def graft(m: dict, client: dict, hop: dict, server_id: str, server: dict) -> list:
    """The server's messages, ready to follow hop in the client sequence."""
    sm = [dict(x) for x in messages(server)]
    root = sm[0]
    if not known(m, hop.get("to")) and root.get("to"):
        hop["_to"], hop["to"] = hop.get("to"), root["to"]
    root["_from"], root["from"] = root.get("from"), hop["to"]
    base, top = (hop.get("depth") or 0) + 1, min(x.get("depth") or 0 for x in sm)
    frags = client.setdefault("fragments", {}) if (server.get("fragments") or {}) else client.get("fragments") or {}
    names = {f: f"{server_id}:{f}" for f in (server.get("fragments") or {})}
    for f, v in (server.get("fragments") or {}).items():
        v = dict(v)
        v["parent"] = names.get(v.get("parent")) or hop.get("fragment")
        frags[names[f]] = v
    for x in sm:
        x["depth"] = base + (x.get("depth") or 0) - top
        if x.get("fragment"):
            x["fragment"] = names.get(x["fragment"], x["fragment"])
        elif hop.get("fragment"):
            x["fragment"] = hop["fragment"]
    parts = client.setdefault("participants", [])
    for p in [hop["to"]] + [p for x in sm for p in (x["from"], x["to"])]:
        if p not in parts:
            parts.append(p)
    return sm


# ---------- routes ----------

def segments(path: str) -> list:
    path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", path.strip(), flags=re.I)
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    return path.split("/")[1:] if path.startswith("/") else path.split("/")


def route_score(pattern: str, path: str) -> int | None:
    """How well a route pattern matches a request path pattern, or None.
    In both, {x}, :x, ${x}, [x] and * match one segment; {x...} and ** the
    rest. A route pattern that ends in / matches the paths below it. A
    literal segment of the request matches a literal or a wildcard of the
    route; a wildcard of the request only a wildcard of the route."""
    pat, req = segments(pattern), segments(path)
    subtree = len(pat) > 1 and pat[-1] == ""
    if subtree:
        pat = pat[:-1]
    if pat and pat[-1] == "{$}":
        pat = pat[:-1] + [""]
    score = 0
    for i, p in enumerate(pat):
        if REST.match(p):
            return score
        if i >= len(req):
            return None
        q = req[i]
        if WILD.match(p):
            score += 1
        elif p == q and not WILD.match(q):
            score += 3
        else:
            return None
    if len(req) > len(pat):
        return score if subtree else None
    return score + 1


def find_route(routes: list, method, path: str) -> dict | None:
    best, top = None, -1
    want = method.upper() if isinstance(method, str) and method.strip() else None
    for r in routes:
        rm = (r.get("method") or "*").upper()
        if want and rm not in ("*", want) and not (want == "HEAD" and rm == "GET"):
            continue
        s = route_score(r.get("pattern") or "", path)
        if s is not None and (s * 2 + (rm != "*")) > top:
            best, top = r, s * 2 + (rm != "*")
    return best


def handler_label(handler: str) -> str:
    """The call a message to a handler shows: `internal/api.(*App).get` gives
    `(*App).get()`, `app.views.health` gives `views.health()`."""
    tail = handler.rsplit("/", 1)[-1]
    head, dot, rest = tail.partition(".")
    return (rest if dot and rest else head) + "()"


def join_routes(m: dict) -> int:
    routes = [r for r in m.get("routes") or [] if isinstance(r, dict) and isinstance(r.get("pattern"), str)]
    if not routes:
        return 0
    n = 0
    for s in m["sequences"].values():
        ms = messages(s)
        out, changed = [], False
        for i, msg in enumerate(ms):
            out.append(msg)
            rt = msg.get("route")
            if not is_hop(msg) or not isinstance(rt, dict) or not isinstance(rt.get("path"), str):
                continue
            r = find_route(routes, rt.get("method"), rt["path"])
            if not r:
                continue
            n += 1
            comp = r.get("component")
            if comp and not known(m, msg.get("to")):
                msg["_to"], msg["to"] = msg.get("to"), comp
                changed = True
                if comp not in s.setdefault("participants", []):
                    s["participants"].append(comp)
            h = r.get("handler")
            if not isinstance(h, str) or not h:
                continue
            msg["handler"] = h
            label = handler_label(h)
            nxt = ms[i + 1] if i + 1 < len(ms) else None
            depth = (msg.get("depth") or 0) + 1
            if nxt and (nxt.get("depth") or 0) == depth and called(nxt) == label[:-2]:
                continue
            out.append({"id": "", "from": msg["to"], "to": comp or msg["to"], "label": label, "depth": depth,
                        "joined": "route", **({"fragment": msg["fragment"]} if msg.get("fragment") else {})})
            changed = True
        if changed:
            renumber(out)
            s["messages"] = out
    return n
