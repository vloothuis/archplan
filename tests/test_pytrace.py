"""archplan trace-python: the tracer runs a small project's tests, and the
Python export turns its trace files into sequences."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan.core import pyexport, pytrace  # noqa: E402

FILES = {
    "shop/__init__.py": "",
    "shop/rules.py": """
def allowed(user):
    return user == "admin"


def check(user):
    if not allowed(user):
        raise PermissionError(user)


def items():
    yield 1
    yield 2
""",
    "shop/store.py": """
from shop import rules


class Store:
    def save(self, n):
        return n

    def total(self):
        return sum(rules.items())
""",
    "shop/web.py": """
from http.server import BaseHTTPRequestHandler

from shop import rules
from shop.store import Store


def order(user):
    rules.check(user)
    s = Store()
    for n in range(3):
        s.save(n)
    return s.total()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = rules.allowed(self.path.strip("/"))
        self.send_response(200 if ok else 403)
        self.end_headers()

    def log_message(self, *args):
        pass
""",
    "tests/__init__.py": "",
    "tests/test_shop.py": """
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from shop import web


class ShopTest(unittest.TestCase):
    def setUp(self):
        web.Store().save(0)  # setUp is not recorded

    def test_order(self):
        self.assertEqual(web.order("admin"), 3)

    def test_refused(self):
        with self.assertRaises(PermissionError):
            web.order("guest")

    def test_request(self):
        web.Store().save(1)  # not kept: the test makes a request
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/guest", timeout=10)
        finally:
            srv.shutdown()
            srv.server_close()

    @unittest.skip("not now")
    def test_skipped(self):
        web.order("admin")
""",
    "tests/test_native.py": """
from shop import web


def test_native_order():
    assert web.order("admin") == 3
""",
}


def has_pytest() -> bool:
    return subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode == 0


class TracePythonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for name, text in FILES.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text.lstrip())
        (self.root / ".archplan").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def trace(self, *args):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        env.pop("VIRTUAL_ENV", None)
        out = subprocess.run([sys.executable, "-m", "archplan", "trace-python", "-C", str(self.root),
                              "--python", sys.executable, *args], capture_output=True, text=True, env=env,
                             timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return {p.stem: json.loads(p.read_text()) for p in (self.root / ".archplan" / "sequences").glob("*.json")}

    def test_unittest(self):
        traces = self.trace("unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_shop.py")
        self.assertEqual(set(traces), {"shop-order", "shop-refused", "shop-request"})
        t = traces["shop-order"]
        self.assertEqual((t["format"], t["title"], t["test"], t["file"], t["state"]),
                         ("archplan-python-trace/1", "order", "tests.test_shop.ShopTest.test_order",
                          "tests/test_shop.py", "passed"))
        self.assertIsNone(t["entry"])  # no request
        [tree] = t["trees"]
        self.assertEqual(tree["actor"], "Test")
        mod, name, kids = tree["root"][:3]
        self.assertEqual((mod, name), ("shop.web", "order"))
        # Calls within one module (rules.check -> rules.allowed) are not kept;
        # the generator runs once, though it resumes.
        self.assertEqual([k[:2] for k in kids], [["shop.rules", "check"]] + [["shop.store", "Store.save"]] * 3
                         + [["shop.store", "Store.total"]])
        self.assertEqual(kids[0][2], [])
        self.assertEqual([k[:2] for k in kids[-1][2]], [["shop.rules", "items"]])
        refused = traces["shop-refused"]["trees"][0]["root"][2][0]
        if sys.version_info >= (3, 12):
            self.assertEqual(refused[3:], ["raise"])
        req = traces["shop-request"]
        self.assertEqual(req["entry"], "shop.web.Handler.do_GET")
        [tree] = req["trees"]
        self.assertEqual(tree["actor"], "Client")
        self.assertEqual(tree["root"][2], [["shop.rules", "allowed", [], "false"]])

        seqs = pyexport.export(self.root)["sequences"]
        s = seqs["shop-order"]
        self.assertEqual(s["source"], "pytest-trace")
        self.assertEqual(s["participants"], ["Test", "shop.web", "shop.rules", "shop.store"])
        self.assertEqual([(m["from"], m["to"], m["label"], m["depth"], m.get("fragment")) for m in s["messages"]], [
            ("Test", "shop.web", "shop.web.order()", 0, None),
            ("shop.web", "shop.rules", "shop.rules.check()", 1, None),
            ("shop.web", "shop.store", "shop.store.Store.save()", 1, "L1"),
            ("shop.web", "shop.store", "shop.store.Store.total()", 1, None),
            ("shop.store", "shop.rules", "shop.rules.items()", 2, None)])
        self.assertEqual(s["fragments"], {"L1": {"kind": "loop", "label": "×3", "parent": None}})
        self.assertEqual(s["messages"][0]["id"], "Test>shop.web:shop.web.order()#1")
        r = seqs["shop-request"]["messages"]
        self.assertEqual([(m["from"], m["to"], m.get("outcome")) for m in r],
                         [("Client", "shop.web", None), ("shop.web", "shop.rules", "false")])

    @unittest.skipUnless(has_pytest(), "needs pytest")
    def test_pytest(self):
        traces = self.trace("pytest", "-q", "-p", "no:cacheprovider", "tests")
        self.assertEqual(set(traces), {"native-native-order", "shop-order", "shop-refused", "shop-request"})
        t = traces["native-native-order"]
        self.assertEqual((t["test"], t["file"], t["line"], t["entry"]),
                         ("tests/test_native.py::test_native_order", "tests/test_native.py", 4, None))

    def test_picks_runner(self):
        self.assertEqual(pytrace.pick(sys.executable, self.root), "pytest" if has_pytest() else "unittest")
        (self.root / "manage.py").write_text("")
        self.assertIn(pytrace.pick(sys.executable, self.root), ("pytest", "django"))


if __name__ == "__main__":
    unittest.main()
