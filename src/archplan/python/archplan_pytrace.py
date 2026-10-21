"""Record call trees of a Python project's own functions while its tests run,
one trace file per test (format archplan-python-trace/1). The Python export
turns them into sequences.

    python -m archplan_pytrace pytest|unittest|django [ARGS...]

archplan trace-python runs this with the project's python. Only the standard
library is used, so it runs in any project environment from Python 3.9.

What it records:

- Calls of functions in the project's own files: under ARCHPLAN_TRACE_ROOT,
  outside virtual environments and outside test files. Calls within one module
  are not kept; their calls hang under the caller. Lambdas, comprehensions
  and dunder methods other than __call__ are not kept either.
- Only while a test function runs: not its fixtures, setUp or setUpClass.
  Calls in other threads count for the running test, such as those of a live
  server the test sends requests to.
- A call that starts a tree gets the actor `Client` when a web framework's
  request handling is on the stack (a test client or a live server), and
  `Test` otherwise. When a test makes requests, only those are written, not
  the test's own calls such as factories, unless ARCHPLAN_TRACE_TEST=1.
- The trace's entry is the first view a framework dispatches to, such as a
  Django view or a DRF viewset action, else the first request. A test that
  makes no requests has no entry.
- Module and qualified name only, never argument or return values. A call that
  may have refused gets the kind of its outcome: `raise`, `false` or
  `status:4xx`. `raise` needs Python 3.12 or later.

Environment: ARCHPLAN_TRACE_ROOT (default: the current directory),
ARCHPLAN_TRACE_DIR (default: <root>/.archplan/sequences),
ARCHPLAN_TRACE_MAX_CALLS (calls kept per test, default 50000),
ARCHPLAN_TRACE_TEST=1 (keep a test's own calls next to its requests)."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

ROOT = os.path.realpath(os.environ.get("ARCHPLAN_TRACE_ROOT") or os.getcwd())
OUT = os.environ.get("ARCHPLAN_TRACE_DIR") or os.path.join(ROOT, ".archplan", "sequences")
MAX_CALLS = int(os.environ.get("ARCHPLAN_TRACE_MAX_CALLS") or 50000)
KEEP_TEST = os.environ.get("ARCHPLAN_TRACE_TEST") == "1"
SKIP = {"site-packages", "dist-packages", "venv", "env", "node_modules", "build", "dist", "migrations"}
TESTS = {"tests", "test", "testing"}

_kinds = {}  # code object -> "project", "test" or None
_names = {}  # code object -> (module, qualified name), or None to skip
current = None  # the running Test
written = set()


def classify(path: str):
    if not path or path.startswith("<"):
        return None
    path = os.path.realpath(path)
    if not path.startswith(ROOT + os.sep):
        return None
    parts = path[len(ROOT) + 1:].split(os.sep)
    if any(p in SKIP or p.startswith(".") for p in parts[:-1]):
        return None
    name = parts[-1]
    if (any(p in TESTS for p in parts[:-1]) or name.startswith("test_") or name.endswith("_test.py")
            or name in ("tests.py", "conftest.py")):
        return "test"
    return "project" if name.endswith(".py") else None


def kind(code):
    k = _kinds.get(code, 0)
    if k == 0:
        k = _kinds[code] = classify(code.co_filename)
    return k


def qualname(frame) -> str:
    code = frame.f_code
    q = getattr(code, "co_qualname", None)
    if q:
        return q
    # Before 3.11: the class that defines the method, from self or cls.
    obj = frame.f_locals.get("self", frame.f_locals.get("cls"))
    for c in (obj if isinstance(obj, type) else type(obj)).__mro__ if obj is not None else ():
        f = c.__dict__.get(code.co_name)
        f = getattr(f, "__func__", f)
        if getattr(f, "__code__", None) is code:
            return f"{c.__qualname__}.{code.co_name}"
    return code.co_name


def name_of(frame):
    code = frame.f_code
    n = _names.get(code, 0)
    if n == 0:
        mod, q = frame.f_globals.get("__name__") or "", re.sub(r"^.*<locals>\.", "", qualname(frame))
        dunder = code.co_name.startswith("__") and code.co_name != "__call__"
        skip = mod == "__main__" or code.co_name.startswith("<") or dunder
        n = _names[code] = None if skip else (mod, q)
    return n


class Test:
    def __init__(self, test_id: str, title: str, test: str, file: str | None, line: int | None):
        self.id, self.title, self.test, self.file, self.line = test_id, title, test, file, line
        self.trees, self.live, self.calls, self.truncated, self.view = [], {}, 0, False, None
        self.start = time.monotonic()
        self.thread = threading.get_ident()

    def enter(self, frame):
        n = name_of(frame)
        if n is None or id(frame) in self.live:
            return
        parent, f = None, frame.f_back
        while f is not None:
            p = self.live.get(id(f))
            if p is not None and p[0] is f:
                parent = p
                break
            f = f.f_back
        served = parent[2] if parent is not None else request(frame)  # inside a request
        if served and self.view is None:
            self.view = f"{n[0]}.{n[1]}" if dispatched(frame) else view_object(frame)
        if parent is not None and parent[1][0] == n[0]:
            return
        if self.calls >= MAX_CALLS:
            self.truncated = True
            return
        self.calls += 1
        node = [n[0], n[1], []]
        self.live[id(frame)] = (frame, node, served)
        if parent is not None:
            parent[1][2].append(node)
            return
        self.trees.append({"actor": "Client" if served else "Test", "root": node})

    def leave(self, frame, outcome):
        p = self.live.get(id(frame))
        if p is None or p[0] is not frame:
            return
        if outcome:
            p[1].append(outcome)
        del self.live[id(frame)]

    def write(self, state: str):
        trees = self.trees
        if not KEEP_TEST and any(t["actor"] == "Client" for t in trees):
            trees = [t for t in trees if t["actor"] == "Client"]  # the requests, not the test's setup
        if not trees:
            return
        root = trees[0]["root"] if trees[0]["actor"] == "Client" else None  # a test without requests has no entry
        data = {"format": "archplan-python-trace/1", "id": self.id, "title": self.title, "test": self.test,
                "file": self.file, "line": self.line, "state": state,
                "timeMs": int((time.monotonic() - self.start) * 1000), "calls": self.calls,
                "truncated": self.truncated, "entry": self.view or (root and f"{root[0]}.{root[1]}"), "trees": trees}
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, self.id + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))


REQUEST = re.compile(r"^(django\.core\.handlers|starlette|werkzeug|flask\.app|socketserver|http\.server|wsgiref"
                     r"|aiohttp\.web|tornado\.web|falcon|pyramid\.router|sanic|litestar|quart\.app)(\.|$)")


DISPATCH = {  # the framework functions that call a view, by module
    "django.core.handlers.base": {"_get_response", "_get_response_async"},
    "django.views.generic.base": {"dispatch"},
    "rest_framework.views": {"dispatch"},
    "rest_framework.decorators": {"handler"},
    "ninja.operation": {"run"},
    "fastapi.routing": {"run_endpoint_function"},
    "starlette.routing": {"app"},
    "flask.app": {"dispatch_request"},
    "http.server": {"handle_one_request"},
    "aiohttp.web_protocol": {"_handle_request"},
    "tornado.web": {"_execute"},
}
WRAPPERS = {"_wrapped_view", "_view_wrapper", "wrapped_view", "wrapper_view", "wrapper", "wrapped", "inner", "view",
            "_wrapper", "sensitive_variables_wrapper", "sensitive_post_parameters_wrapper"}


HOOKS = {"setup", "initialize_request", "initial", "finalize_response", "handle_exception", "options",
         "http_method_not_allowed"}  # what a class-based view's dispatch calls besides the handler


def view_object(frame):
    """The view a class-based view's dispatch, above this call, serves: its
    class with the DRF action or the HTTP method, such as ZaakViewSet.create.
    The handler need not be the project's own, and need not run at all."""
    f, i = frame.f_back, 0
    while f is not None and i < 80:
        if f.f_code.co_name == "dispatch" and f.f_globals.get("__name__") in ("rest_framework.views",
                                                                               "django.views.generic.base"):
            obj = f.f_locals.get("self")
            cls = type(obj)
            mod = sys.modules.get(cls.__module__)
            if obj is None or classify(getattr(mod, "__file__", None) or "") != "project":
                return None
            action = getattr(obj, "action", None) or getattr(f.f_locals.get("request"), "method", "") or "dispatch"
            return f"{cls.__module__}.{cls.__qualname__}.{str(action).lower()}"
        f, i = f.f_back, i + 1
    return None


def dispatched(frame) -> bool:
    """Whether a framework calls this function as a view: a dispatching
    function is its caller, or calls it through decorator wrappers. Django
    middleware hooks such as process_view are not views."""
    if frame.f_code.co_name.startswith("process_") or frame.f_code.co_name in HOOKS:
        return False
    f = frame.f_back
    for _ in range(6):
        if f is None or kind(f.f_code):
            return False
        name = f.f_code.co_name
        if name in DISPATCH.get(f.f_globals.get("__name__"), ()):
            return True
        if name not in WRAPPERS:
            return False
        f = f.f_back
    return False



def request(frame) -> bool:
    """Whether a web framework's request handling is on the stack: the call
    serves a request, from a test client or a live server."""
    f = frame.f_back
    while f is not None:
        if REQUEST.match(f.f_globals.get("__name__") or ""):
            return True
        f = f.f_back
    return False


def outcome(value):
    if value is False:
        return "false"
    s = getattr(value, "status_code", None)
    if isinstance(s, int) and 400 <= s < 500:
        return f"status:{s}"
    return None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:120] or "test"


def start(name: str, test: str, path: str | None, line: int | None):
    """Start recording a test. name is the test function's name, test its full id."""
    global current
    file = os.path.relpath(os.path.realpath(path), ROOT) if path else None
    stem = re.sub(r"^test_|_test$|^tests$", "", os.path.splitext(os.path.basename(path or ""))[0])
    title = re.sub(r"^test_?", "", name).replace("_", " ").strip() or name
    sid = base = slug(f"{stem} {title}")
    i = 1
    while sid in written:
        i += 1
        sid = f"{base}-{i}"
    written.add(sid)
    current = Test(sid, title, test, file, line)


def stop(state: str | None):
    """Stop recording; state is passed, failed, or None for a skipped test."""
    global current
    t, current = current, None
    if t is not None and state:
        t.write(state)


# --------------------------------------------------------------- tracing

def install():
    if getattr(sys, "monitoring", None):
        install_monitoring()
    else:
        sys.setprofile(profile)
        threading.setprofile(profile)
    patch_unittest()


def install_monitoring():
    m = sys.monitoring
    tool = next((t for t in (4, 3, m.PROFILER_ID) if m.get_tool(t) is None), None)
    if tool is None:
        sys.setprofile(profile)
        threading.setprofile(profile)
        return
    m.use_tool_id(tool, "archplan")
    DISABLE = m.DISABLE

    def on_start(code, _offset):
        if kind(code) != "project":
            return DISABLE
        t = current
        if t is not None:
            t.enter(sys._getframe(1))

    def on_return(code, _offset, value):
        if kind(code) != "project":
            return DISABLE
        t = current
        if t is not None:
            t.leave(sys._getframe(1), outcome(value))

    def on_unwind(code, _offset, _exc):
        t = current
        if t is not None and kind(code) == "project":
            t.leave(sys._getframe(1), "raise")

    E = m.events
    m.register_callback(tool, E.PY_START, on_start)
    m.register_callback(tool, E.PY_RETURN, on_return)
    m.register_callback(tool, E.PY_UNWIND, on_unwind)
    m.set_events(tool, E.PY_START | E.PY_RETURN | E.PY_UNWIND)


def profile(frame, event, arg):
    t = current
    if t is None or kind(frame.f_code) != "project":
        return
    if event == "call":
        t.enter(frame)
    elif event == "return":
        if frame.f_code.co_flags & 0x2A0:  # a generator or coroutine also returns when it yields
            return
        t.leave(frame, outcome(arg))


def patch_unittest():
    """Record each unittest test method; Django's tests and pytest's
    unittest classes run through it too."""
    import unittest
    case = unittest.TestCase
    call = getattr(case, "_callTestMethod", None)
    if call is None or getattr(call, "archplan", False):
        return

    def traced(self, method):
        if current is not None:
            return call(self, method)
        code = getattr(getattr(method, "__func__", method), "__code__", None)
        start(self._testMethodName, self.id(), code and code.co_filename, code and code.co_firstlineno)
        state = "failed"
        try:
            call(self, method)
            state = "passed"
        except unittest.SkipTest:
            state = None
            raise
        finally:
            stop(state)

    traced.archplan = True
    case._callTestMethod = traced


# --------------------------------------------------------------- runners

def main(argv: list) -> int:
    runner, args = (argv[0], argv[1:]) if argv else ("unittest", [])
    sys.path.insert(0, os.getcwd())
    if runner == "pytest":
        import pytest
        return pytest.main(["-p", "archplan_pytrace_pytest", *args])
    install()
    if runner == "unittest":
        import unittest
        sys.argv = ["python -m unittest", *args]
        unittest.main(module=None)
        return 0
    if runner == "django":
        import runpy
        sys.argv = ["manage.py", "test", *args]
        runpy.run_path("manage.py", run_name="__main__")
        return 0
    print(f"archplan_pytrace: unknown runner {runner}; use pytest, unittest or django", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.modules.setdefault("archplan_pytrace", sys.modules[__name__])
    sys.exit(main(sys.argv[1:]))
