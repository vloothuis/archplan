"""The editor server: serves the bundled editor and a small JSON API."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote

from . import __version__, brief, detectors, handover, hotspots, reload
from .architecture import Architecture, ArchitectureError
from .findings import Findings
from .vocab import Vocab
from .project import Project, ProjectError, UntrustedExport, etag, read_json, write_atomic, write_json
from .handover import Handovers
from .store import Conflict, NotFound, Store, wait
from .suggestions import Suggestions, pick, preview

FINDING_ACTIONS = {"dismiss": "dismissed", "accept": "accepted", "reopen": "new", "discuss": None}
LONG_POLL = 25.0  # seconds the editor's request for changes may wait
STATIC = Path(str(resources.files("archplan") / "static"))


def changes(p: Project, store: Store) -> dict:
    """What the editor polls for: threads, the as-is model, the target revision,
    the agent's suggestions, the handover, the architecture profile, the
    findings baseline and the agent's last activity. key
    changes when any does."""
    st = p.state()
    c = {"version": store.version(), "model_etag": etag(p.model_path),
         "target_rev": st.get("target_rev", 0), "target_by": st.get("target_by"),
         "suggestions_version": Suggestions(p.dir).version(), "handover_version": Handovers(p.dir).version(),
         "architecture_etag": etag(p.path("architecture.json")), "findings_etag": etag(p.path("baseline.json")),
         "vocabulary_etag": etag(p.path("vocabulary.json")), "suggest_etag": etag(p.path("config.json")),
         "activity_etag": etag(p.path("activity.json")), "detectors_version": detectors.version(p),
         "build": reload.BUILD}
    c["key"] = ".".join(str(c[k]) for k in ("version", "model_etag", "target_rev", "suggestions_version",
                                            "handover_version", "architecture_etag", "findings_etag", "vocabulary_etag",
                                            "suggest_etag", "activity_etag", "detectors_version", "build"))
    return c


def make_handler(p: Project, store: Store):
    export_lock = threading.Lock()
    hotspot_lock = threading.Lock()
    suggestions = Suggestions(p.dir)
    handovers = Handovers(p.dir)
    arch = Architecture(p)
    arch_lock = threading.Lock()
    findings = Findings(p)
    status = lambda: {"name": p.name, "root": str(p.root), "pid": os.getpid(), "archplan_version": __version__,
                      "build": reload.BUILD}
    findings.arch = arch  # share the profile analysis cache
    vocab = Vocab(p)
    vocab_lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(STATIC), **kw)

        def log_message(self, fmt, *args):
            if os.environ.get("ARCHPLAN_DEBUG"):
                sys.stderr.write("%s\n" % (fmt % args))

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def send_json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, path: Path):
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                return self.send_json({"error": f"{path.name} not found"}, HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"null")

        def local_origin(self) -> bool:
            """Refuse writes from other sites' pages (a browser on this machine
            could otherwise be made to post to the API)."""
            o = self.headers.get("Origin")
            return o is None or o == f"http://{self.headers.get('Host')}"

        def local_host(self) -> bool:
            """Answer only requests addressed to this machine by name or loopback
            address. A web page that rebinds its own domain to 127.0.0.1 then
            cannot read or write the project (DNS rebinding)."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def refuse(self) -> bool:
            if not self.local_host():
                self.send_json({"error": "request for a non-local host refused"}, HTTPStatus.FORBIDDEN)
                return True
            if self.command != "GET" and not self.local_origin():
                self.send_json({"error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
                return True
            return False

        def tick(self):
            try:
                brief.auto_tick(p, store)
            except Exception as e:  # auto mode must never fail the request that triggered it
                sys.stderr.write(f"archplan: auto suggest: {e}\n")

        def tick_enabled(self):
            try:
                brief.auto_tick(p, store, enabled=True)
            except Exception as e:
                sys.stderr.write(f"archplan: auto suggest: {e}\n")

        def do_GET(self):
            if self.refuse():
                return
            path, _, query = self.path.partition("?")
            params = dict(x.split("=", 1) for x in query.split("&") if "=" in x)
            if path == "/api/status":
                return self.send_json(status())
            if path == "/api/info":
                st = p.state()
                return self.send_json({**status(), "collab": True, "dir": str(p.dir),
                                       "model": "/api/model" if p.model_path.exists() else None,
                                       "can_export": bool(p.export_command()),
                                       "git": hotspots.info(p.root),
                                       "intent": st.get("intent", ""), **changes(p, store)})
            if path == "/api/model":
                return self.send_file(p.model_path)
            if path == "/api/target":
                return self.send_file(p.target_path)
            if path == "/api/base":
                return self.send_file(p.base_path)
            if path == "/api/state":
                st = p.state()
                return self.send_json({**st, "has_target": p.target_path.exists(),
                                       "base_current": st.get("base_etag") == etag(p.model_path)})
            if path == "/api/threads":
                since = params.get("since", "-1")
                if since == changes(p, store)["key"]:
                    # A server about to restart answers at once, so it need not wait for this request.
                    draining = getattr(self.server, "draining", None) or threading.Event()
                    wait(lambda: "restart" if draining.is_set() else changes(p, store)["key"], since, LONG_POLL)
                return self.send_json({**store.read(), **changes(p, store)})
            if path == "/api/activity":
                return self.send_json(read_json(p.path("activity.json"), {}) or {})
            if path == "/api/handover":
                return self.send_json({"current": handovers.current(), "history": handovers.history(10),
                                       **changes(p, store)})
            if path == "/api/architecture":
                try:
                    with arch_lock:
                        return self.send_json({**arch.state(), **changes(p, store)})
                except ArchitectureError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            if path == "/api/schema":
                from . import schema as schema_mod
                sch = (read_json(p.model_path) or {}).get("schema")
                if not sch:
                    return self.send_json({"error": "the model has no schema section; no database schema source "
                                                    "was found (see `archplan schema`)"}, HTTPStatus.NOT_FOUND)
                return self.send_json({**sch, "summary": schema_mod.summary(sch),
                                       "cross_fks": schema_mod.cross_fks(sch)})
            if path == "/api/findings":
                try:
                    with arch_lock:
                        return self.send_json({**findings.listing(store), **changes(p, store)})
                except ArchitectureError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            if path in ("/api/detectors", "/api/metrics"):
                try:
                    with arch_lock:
                        res = findings.result()
                except ArchitectureError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
                if path == "/api/metrics":
                    return self.send_json({"metrics": res["extras"].get("metrics") or {}, **changes(p, store)})
                return self.send_json({"detectors": res["detectors"], "missing": res["missing"],
                                       "capabilities": res["capabilities"], **changes(p, store)})
            if path == "/api/vocabulary/candidates":
                try:
                    with vocab_lock:
                        return self.send_json({**vocab.candidates(unquote(params.get("component", "")) or None,
                                                                  int(params["top"]) if params.get("top") else None,
                                                                  params.get("ignored") == "1"),
                                               **changes(p, store)})
                except NotFound as e:
                    return self.send_json({"error": str(e).strip("'\"")}, HTTPStatus.NOT_FOUND)
                except KeyError as e:
                    return self.send_json({"error": str(e).strip("'\"")}, HTTPStatus.NOT_FOUND)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            if path == "/api/suggest":
                return self.send_json({**brief.status(p, store), **changes(p, store)})
            if path == "/api/brief":
                try:
                    lst = lambda k: [x for x in unquote(params[k]).split(",") if x] if params.get(k) else None
                    with arch_lock:
                        return self.send_json({**brief.brief(p, int(params.get("top") or 8), lst("focus"),
                                                             lst("kinds"), store), **changes(p, store)})
                except ArchitectureError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            if path == "/api/suggestions":
                return self.send_json({"suggestions": suggestions.newest(), **changes(p, store)})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] == "preview":
                try:
                    sg = suggestions.get(parts[2])
                    ops = params.get("ops")
                    idx = None if ops is None else [int(x) for x in unquote(ops).split(",") if x.strip()]
                    m, done = preview(p, sg, idx)
                    return self.send_json({"target": m, "applied": done, "ops": pick(sg, idx)})
                except NotFound as e:
                    return self.send_json({"error": str(e).strip("'\"")}, HTTPStatus.NOT_FOUND)
                except Conflict as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            if path == "/api/hotspots":
                try:
                    with hotspot_lock:
                        kw = {k: t(params[q]) for q, k, t in (("together", "min_together", int),
                                                               ("degree", "min_degree", float)) if q in params}
                        return self.send_json(hotspots.hotspots(p, int(params.get("since", 365)),
                                                                refresh=params.get("refresh") == "1", **kw))
                except hotspots.NotGit as e:
                    return self.send_json({"error": f"no git history: {e}"}, HTTPStatus.NOT_FOUND)
                except ValueError as e:
                    return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return super().do_GET()

        def do_POST(self):
            if self.refuse():
                return
            parts = self.path.strip("/").split("/")
            try:
                b = self.body() or {}
                if parts == ["api", "threads"]:
                    return self.send_json(store.create(b["anchor"], b.get("label", ""), b["text"],
                                                       b.get("author", "human"), b.get("kind"), b.get("options"),
                                                       b.get("snapshot")))
                if parts == ["api", "threads", "merge"]:
                    store.merge(b.get("threads", []))
                    return self.send_json(store.read())
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "messages":
                    return self.send_json(store.reply(parts[2], b.get("text", ""), b.get("author", "human"),
                                                      b.get("status"), b.get("kind"), b.get("options")))
                if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] in ("accept", "reject", "plan"):
                    idx = [] if parts[3] == "reject" else b.get("ops")
                    if parts[3] != "reject" and idx is not None and (not isinstance(idx, list) or not idx):
                        raise ValueError("ops must be a non-empty list of op indices; reject to accept none")
                    if parts[3] == "plan":
                        out = brief.plan(p, store, parts[2], idx)
                    else:
                        out = {"suggestion": suggestions.decide(p, parts[2], idx, b.get("reason"))}
                    self.tick()
                    return self.send_json({**out, **changes(p, store)})
                if len(parts) == 4 and parts[:2] == ["api", "suggestions"] and parts[3] == "discuss":
                    th, created = brief.discuss(p, store, parts[2], b.get("text"))
                    return self.send_json({"thread": th, "created": created, **changes(p, store)})
                if parts == ["api", "suggest"]:
                    th = brief.request(p, store, b.get("count"), b.get("focus"), b.get("kinds"), b.get("note"))
                    return self.send_json({"thread": th, **changes(p, store)})
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "lock":
                    return self.send_json(handover.lock(store, parts[2], b.get("title", ""), b.get("summary", "")))
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "unlock":
                    return self.send_json(handover.unlock(store, handovers, parts[2]))
                if parts == ["api", "architecture", "lock"]:
                    with arch_lock:
                        return self.send_json({"profile": arch.lock(), **changes(p, store)})
                if parts == ["api", "architecture", "infer"]:
                    with arch_lock:
                        proposal, saved = arch.infer()
                    return self.send_json({"proposal": proposal, "saved": saved, **changes(p, store)})
                if len(parts) == 4 and parts[:2] == ["api", "findings"] and parts[3] in FINDING_ACTIONS:
                    with arch_lock:
                        if parts[3] == "discuss":
                            th, created = findings.discuss(store, parts[2], b.get("text"))
                            return self.send_json({"thread": th, "created": created,
                                                   "finding": findings.get(parts[2], store), **changes(p, store)})
                        f = findings.decide(parts[2], FINDING_ACTIONS[parts[3]], b.get("reason"))
                    return self.send_json({"finding": f, **changes(p, store)})
                if parts[:2] == ["api", "vocabulary"] and len(parts) == 3 and parts[2] in ("ignore", "unignore"):
                    if not isinstance(b.get("term"), str) or (b.get("component") is not None
                                                             and not isinstance(b.get("component"), str)):
                        raise ValueError("term (and component, when given) must be strings")
                    with vocab_lock:
                        d = vocab.set_ignored(b["term"], b.get("component") or None, parts[2] == "ignore")
                    return self.send_json({**d, **changes(p, store)})
                if parts == ["api", "vocabulary", "draft"]:
                    return self.send_json({"thread": draft_thread(p, store, b.get("components"), b.get("note")),
                                           **changes(p, store)})
                if parts == ["api", "handover"]:
                    return self.send_json(handovers.create(store, b.get("mode", "all"), b.get("note", "")))
                if len(parts) == 4 and parts[:2] == ["api", "handover"] and parts[3] == "approve":
                    return self.send_json(handovers.approve(store, parts[2], b.get("batches")))
                if len(parts) == 4 and parts[:2] == ["api", "handover"] and parts[3] == "cancel":
                    return self.send_json(handovers.cancel(store, parts[2]))
                if parts == ["api", "export"]:
                    if not export_lock.acquire(blocking=False):
                        return self.send_json({"error": "an export is already running"}, HTTPStatus.CONFLICT)
                    try:
                        n = p.export(require_trust=True)
                    finally:
                        export_lock.release()
                    self.tick()
                    return self.send_json({"ok": True, "counts": n, **changes(p, store)})
            except UntrustedExport as e:
                return self.send_json({"error": str(e)}, HTTPStatus.FORBIDDEN)
            except ArchitectureError as e:
                return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except NotFound as e:
                return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except Conflict as e:
                return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except ProjectError as e:
                return self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except (KeyError, TypeError, ValueError) as e:
                return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_PUT(self):
            if self.refuse():
                return
            parts = self.path.strip("/").split("/")
            try:
                b = self.body() or {}
                if self.path == "/api/target":
                    rev = p.save_target(b["target"], "editor", b.get("base_etag"))
                    if "intent" in b:
                        p.set_state(intent=b["intent"])
                    return self.send_json({"ok": True, "target_rev": rev})
                if self.path == "/api/plan":
                    write_atomic(p.path("plan.md"), b.get("md", ""))
                    write_atomic(p.path("plan.json"), b.get("json", "{}"))
                    return self.send_json({"ok": True})
                if len(parts) == 4 and parts[:2] == ["api", "threads"] and parts[3] == "refactor":
                    return self.send_json(handover.edit(store, parts[2], b.get("title"), b.get("summary")))
                if parts == ["api", "architecture"]:
                    with arch_lock:
                        return self.send_json({"profile": arch.save(b["profile"]), **changes(p, store)})
                if parts == ["api", "refactors", "order"]:
                    return self.send_json({"refactors": handover.reorder(store, b.get("ids")), **changes(p, store)})
                if len(parts) == 3 and parts[:2] == ["api", "detectors"]:
                    e = detectors.configure(p, unquote(parts[2]), b.get("enabled"), b.get("params"))
                    return self.send_json({"detector": parts[2], "config": e, **changes(p, store)})
                if parts == ["api", "suggest", "auto"]:
                    brief.set_auto(p, b.get("on"), b.get("count"))
                    if b.get("on"):
                        self.tick_enabled()
                    return self.send_json({**brief.status(p, store), **changes(p, store)})
            except NotFound as e:
                return self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except Conflict as e:
                return self.send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except (KeyError, TypeError, ValueError) as e:
                return self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def draft_thread(p: Project, store: Store, components=None, note=None) -> dict:
    """A thread asking the agent to draft glossaries from the candidates."""
    if components is not None and (not isinstance(components, list)
                                   or not all(isinstance(c, str) for c in components)):
        raise ValueError("components must be a list of component ids")
    model = read_json(p.model_path) or {}
    bad = [c for c in components or [] if c not in (model.get("components") or {})]
    if bad:
        raise NotFound(f"no component {bad[0]}")
    which = ("for " + ", ".join(f"@[{c}](comp:{c})" for c in components[:30])) if components else \
        "for the components with the most code"
    text = (f"Please draft glossaries {which}. Read the candidates with get_vocabulary_candidates, keep the real "
            "domain concepts, write a one-line definition for each, decide owners and uses/avoid from the "
            "cross-component counts, and send them with propose_glossary.")
    if note:
        text += "\n\n" + str(note)[:2000]
    return store.create("glossary", "Draft glossaries", text, "human")


AUTO_POLL = 15.0  # seconds between auto mode checks for model changes made outside the server


def auto_loop(p: Project, store: Store) -> None:
    """Auto mode for changes the server does not see itself: an export from a
    terminal, or a suggestion decided through another process."""
    import time
    while True:
        time.sleep(AUTO_POLL)
        try:
            if brief.settings(p)["auto"]:
                brief.auto_tick(p, store)
        except Exception as e:
            sys.stderr.write(f"archplan: auto suggest: {e}\n")


def free_port(host: str, start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket() as s:
            # Match the server's own option, so a port in TIME_WAIT after a restart is reused.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise ProjectError(f"no free port in {start}..{start + tries - 1}")


class Server(ThreadingHTTPServer):
    """Counts the requests in flight, so a restart can wait for them."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.draining = threading.Event()
        self.busy = 0
        self.busy_lock = threading.Lock()

    def process_request_thread(self, request, client_address):
        with self.busy_lock:
            self.busy += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.busy_lock:
                self.busy -= 1

    def drain(self, timeout: float = 30.0) -> None:
        """Stop taking requests, end long polls early, and wait for the rest."""
        import time
        self.draining.set()
        self.shutdown()
        end = time.monotonic() + timeout
        while self.busy and time.monotonic() < end:
            time.sleep(0.05)
        self.server_close()


def serve(p: Project, host: str = "127.0.0.1", port: int | None = None) -> None:
    store = Store(p.dir)
    port = free_port(host, port or p.port)
    srv = Server((host, port), make_handler(p, store))
    url = f"http://{host}:{port}/"
    threading.Thread(target=auto_loop, args=(p, store), daemon=True).start()
    write_json(p.path("server.json"), {"url": url, "pid": os.getpid(), "port": port,
                                       "archplan_version": __version__, "build": reload.BUILD})
    print(f"archplan: {url}  ({p.root}, {reload.BUILD})", file=sys.stderr, flush=True)
    changed = []
    if not reload.disabled():
        # Restart with the new code when it changes on disk (a git pull in a checkout).
        threading.Thread(target=reload.watch, args=(lambda fp: (changed.append(fp), srv.shutdown()),),
                         daemon=True).start()
    try:
        srv.serve_forever()
        if changed:
            srv.drain()
            argv = reload.restart_argv(port)
            print(f"archplan: the code changed on disk; restarting ({reload.disk_version() or '?'}+{changed[0]})",
                  file=sys.stderr, flush=True)
            os.execv(argv[0], argv)
    finally:
        info = read_json(p.path("server.json"), {}) or {}
        if info.get("pid") == os.getpid():
            p.path("server.json").unlink(missing_ok=True)
