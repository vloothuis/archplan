"""Several exporters per project, and calls between processes: span
stitching and the route join (core/merge.py, core/links.py)."""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import blast, modelcheck, project  # noqa: E402
from archplan.core import links, merge  # noqa: E402
from archplan.core.project import ProjectError  # noqa: E402
from archplan.detectors import security_unguarded_entry, security_untraced  # noqa: E402
from archplan.detectors.common import graph  # noqa: E402
from archplan.detectors.security_common import call_file  # noqa: E402


def msg(frm, to, label, depth, **kw):
    return {"id": f"{frm}>{to}:{label.split('(')[0]}#1", "from": frm, "to": to, "label": label, "depth": depth, **kw}


def server_part():
    """What archplan-go writes for a Go server: a route, a guard and a traced request."""
    return {
        "format": "archmodel/1",
        "source": {"generator": "archplan-go", "languages": ["go"], "capabilities": ["components", "calls"]},
        "components": {"api": {"name": "api", "path": "api", "match": ["api/**"]},
                       "store": {"name": "store", "path": "store", "match": ["store/**"]}},
        "dependencies": [{"from": "api", "to": "store", "weight": 1}],
        "policy": {"default": "deny", "rules": [{"from": "api", "to": "store", "effect": "allow"}]},
        "signatures": [
            {"id": "api.(*App).getOrder", "component": "api", "file": "api/orders.go", "line": 10,
             "name": "getOrder", "params": [], "returns": None, "public": False},
            {"id": "api.(*App).createOrder", "component": "api", "file": "api/orders.go", "line": 20,
             "name": "createOrder", "params": [], "returns": None, "public": False},
            {"id": "api.mayRead", "component": "api", "file": "api/auth.go", "line": 5,
             "name": "mayRead", "params": [], "returns": "bool", "public": False}],
        "routes": [
            {"method": "GET", "pattern": "/api/v1/orders/{id}", "handler": "api.(*App).getOrder",
             "component": "api", "file": "api/orders.go", "line": 10},
            {"method": "POST", "pattern": "/api/v1/orders", "handler": "api.(*App).createOrder",
             "component": "api", "file": "api/orders.go", "line": 20},
            {"method": "*", "pattern": "/static/", "component": "api", "file": "api/routes.go", "line": 3}],
        "sequences": {"get-order-server": {"entry": "GET /api/v1/orders/{id}", "participants": ["Caller", "api"],
                                           "fragments": {"L1": {"kind": "loop", "label": "x2", "parent": None}},
                                           "messages": [
                                               msg("Caller", "api", "(*App).getOrder(w, r)", 0, span="s-1"),
                                               msg("api", "api", "mayRead(r)", 1),
                                               msg("api", "store", "Get(id)", 1, fragment="L1")]}},
    }


def client_part():
    """What a TypeScript exporter writes for the SPA: component ids that
    collide with the server's, and two traced fetches."""
    return {
        "format": "archmodel/1",
        "source": {"generator": "archplan export-ts", "languages": ["typescript"],
                   "capabilities": ["components", "calls"]},
        "components": {"api": {"name": "api", "path": "web/src/api", "match": ["web/src/api/**"]}},
        "dependencies": [],
        "policy": {"default": "allow", "rules": []},
        "sequences": {
            "open-order": {"title": "open order", "participants": ["Browser", "api"], "messages": [
                msg("Browser", "api", "getOrder(id)", 0),
                msg("api", "Server", "fetch()", 1, **{"async": True, "via": "http", "span": "s-1",
                                                      "route": {"method": "GET", "path": "/api/v1/orders/:id"}})]},
            "new-order": {"title": "new order", "participants": ["Browser", "api"], "messages": [
                msg("Browser", "api", "create(o)", 0),
                msg("api", "Server", "fetch()", 1, **{"async": True, "via": "http",
                                                      "route": {"method": "POST", "path": "/api/v1/orders"}})]}},
    }


class Ctx:
    def __init__(self, model):
        self.model = model
        self.files = {}

    @property
    def components(self):
        return self.model.get("components") or {}

    @property
    def graph(self):
        return graph(self.model)

    churn = None


def decl(kind, tid, key, value, file, name):
    return {"target": {"kind": kind, "id": tid}, "key": key, "value": value, "annotator": "builtin",
            "file": file, "line": 1, "at": {"file": file, "line": 2, "name": name}}


class EntriesTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(merge.entries("go run x -o {out}"), [{"cmd": "go run x -o {out}", "prefix": None}])
        self.assertEqual(merge.entries(None), [])
        self.assertEqual(merge.entries(["a {out}", {"cmd": "b", "prefix": "web"}]),
                         [{"cmd": "a {out}", "prefix": None}, {"cmd": "b", "prefix": "web"}])
        for bad in ([], [1], [{"cmd": ""}], [{"cmd": "a", "prefix": "a b"}], [{"cmd": "a", "dir": "x"}],
                    [{"cmd": "a", "prefix": "w"}, {"cmd": "b", "prefix": "w"}], {"cmd": "a"}):
            with self.assertRaises(merge.MergeError, msg=repr(bad)):
                merge.entries(bad)

    def test_trust_text_covers_the_list(self):
        self.assertEqual(merge.trust_text("cmd"), "cmd")
        a = merge.trust_text(["x", {"cmd": "y", "prefix": "web"}])
        self.assertNotEqual(a, merge.trust_text(["x", {"cmd": "y", "prefix": "app"}]))
        self.assertNotEqual(a, merge.trust_text(["x"]))
        self.assertEqual(a, merge.trust_text([{"cmd": "x"}, {"prefix": "web", "cmd": "y"}]))


class MergeTest(unittest.TestCase):
    def test_collision_names_both_commands(self):
        parts = [({"cmd": "go-export", "prefix": None}, server_part()),
                 ({"cmd": "ts-export", "prefix": None}, client_part())]
        with self.assertRaises(merge.MergeError) as cm:
            merge.merge(parts)
        self.assertIn("'api'", str(cm.exception))
        self.assertIn("go-export", str(cm.exception))
        self.assertIn("ts-export", str(cm.exception))
        self.assertIn("prefix", str(cm.exception))

    def test_prefix_renames_components_everywhere(self):
        m = merge.merge([({"cmd": "go", "prefix": None}, server_part()),
                         ({"cmd": "ts", "prefix": "web"}, client_part())])
        self.assertEqual(set(m["components"]), {"api", "store", "web/api"})
        seq = m["sequences"]["open-order"]
        self.assertEqual(seq["participants"], ["Browser", "web/api"])
        self.assertEqual([x["from"] for x in seq["messages"]], ["Browser", "web/api"])
        self.assertEqual(m["source"]["languages"], ["go", "typescript"])
        self.assertEqual(m["source"]["generator"], "archplan-go + archplan export-ts")
        self.assertEqual(len(m["routes"]), 3)
        # The Go part denies by default; the SPA's components keep their dependencies allowed.
        self.assertEqual(m["policy"]["default"], "deny")
        self.assertIn({"from": "web/api", "to": "*", "effect": "allow", "note": "its exporter allows by default"},
                      m["policy"]["rules"])
        self.assertTrue(modelcheck.check(m)["ok"], modelcheck.check(m)["errors"])


class LinkTest(unittest.TestCase):
    def linked(self):
        m = merge.merge([({"cmd": "go", "prefix": None}, server_part()),
                         ({"cmd": "ts", "prefix": "web"}, client_part())])
        counts = links.link(m)
        return m, counts

    def test_stitch_by_span(self):
        m, counts = self.linked()
        self.assertEqual(counts["stitched"], 1)
        self.assertNotIn("get-order-server", m["sequences"])
        seq = m["sequences"]["open-order"]
        self.assertEqual(seq["stitched"], ["get-order-server"])
        rows = [(x["id"], x["depth"], x.get("fragment")) for x in seq["messages"]]
        self.assertEqual(rows, [
            ("Browser>web/api:getOrder#1", 0, None),
            ("web/api>api:fetch#1", 1, None),
            ("api>api:(*App).getOrder#1", 2, None),
            ("api>api:mayRead#1", 3, None),
            ("api>store:Get#1", 3, "get-order-server:L1")])
        self.assertEqual(seq["fragments"]["get-order-server:L1"]["parent"], None)
        self.assertEqual(seq["participants"], ["Browser", "web/api", "api", "store"])
        # The stitched handler call is the route's handler: no joined message is added.
        self.assertFalse(any(x.get("joined") for x in seq["messages"]))
        self.assertEqual(seq["messages"][1]["handler"], "api.(*App).getOrder")

    def test_route_join_adds_the_handler_call(self):
        m, counts = self.linked()
        self.assertEqual(counts["joined"], 2)
        seq = m["sequences"]["new-order"]
        self.assertEqual([(x["id"], x["depth"], x.get("joined")) for x in seq["messages"]], [
            ("Browser>web/api:create#1", 0, None),
            ("web/api>api:fetch#1", 1, None),
            ("api>api:(*App).createOrder#1", 2, "route")])
        self.assertIn("api", seq["participants"])
        self.assertTrue(modelcheck.check(m)["ok"], modelcheck.check(m)["errors"])

    def test_route_score(self):
        s = links.route_score
        self.assertIsNotNone(s("/api/v1/orders/{id}", "/api/v1/orders/T-1"))
        self.assertIsNotNone(s("/api/v1/orders/{id}", "/api/v1/orders/${id}"))
        self.assertIsNone(s("/api/v1/orders/_search", "/api/v1/orders/:id"))
        self.assertIsNone(s("/api/v1/orders", "/api/v1/orders/1"))
        self.assertIsNotNone(s("/api/v1/_schema/", "/api/v1/_schema/a/b"))
        self.assertIsNotNone(s("/files/{path...}", "/files/a/b/c"))
        self.assertIsNotNone(s("/api/v1/orders", "https://example.com/api/v1/orders?x=1"))
        self.assertIsNone(s("/{$}", "/a"))
        routes = [{"method": "*", "pattern": "/api/"}, {"method": "GET", "pattern": "/api/orders/{id}"},
                  {"method": "GET", "pattern": "/api/orders/_search"}]
        self.assertEqual(links.find_route(routes, "GET", "/api/orders/_search"), routes[2])
        self.assertEqual(links.find_route(routes, "get", "/api/orders/7"), routes[1])
        self.assertEqual(links.find_route(routes, "POST", "/api/orders/7"), routes[0])

    def test_participant_paths_become_components(self):
        m = {"components": {"web/api": {"path": "web/src/api", "match": ["web/src/api/**"]}},
             "sequences": {"s": {"participants": ["Browser", "web/src/api"], "messages": [
                 msg("Browser", "web/src/api", "load()", 0)]}}}
        self.assertEqual(links.link(m)["participants"], 1)
        self.assertEqual(m["sequences"]["s"]["participants"], ["Browser", "web/api"])
        self.assertEqual(m["sequences"]["s"]["messages"][0]["to"], "web/api")


class SecurityAcrossProcessesTest(unittest.TestCase):
    def model(self):
        m = merge.merge([({"cmd": "go", "prefix": None}, server_part()),
                         ({"cmd": "ts", "prefix": "web"}, client_part())])
        links.link(m)
        m["declarations"] = [
            decl("function", "api.mayRead", "guard", "authz", "api/auth.go", "mayRead"),
            decl("entrypoint", "api.(*App).getOrder", "auth", "authz", "api/orders.go", "getOrder"),
            decl("entrypoint", "api.(*App).createOrder", "auth", "required", "api/orders.go", "createOrder")]
        return m

    def test_browser_call_reaches_the_go_entry_point(self):
        ctx = Ctx(self.model())
        out = security_unguarded_entry.run(ctx, dict(security_unguarded_entry.PARAMS))
        self.assertEqual([f["evidence"]["entry"] for f in out], ["createOrder"])
        self.assertEqual(security_untraced.run(ctx, dict(security_untraced.PARAMS)), [])
        es = {e["entry"]: e for e in blast.entries(ctx)}
        self.assertEqual(es["api.(*App).createOrder"]["components"], ["web/api", "api"])
        self.assertEqual(es["api.(*App).createOrder"]["sequences"], ["new-order"])
        self.assertIn("web/api", es["api.(*App).getOrder"]["components"])
        self.assertEqual(es["api.(*App).getOrder"]["gaps"], ["no refusal"])
        touched = blast.touched(ctx, list(es.values()), ["web/src/api/client.ts"])
        self.assertEqual({e["entry"] for e in touched}, set(es))

    def test_call_file_of_a_go_label(self):
        ctx = Ctx(self.model())
        self.assertEqual(call_file(ctx, msg("api", "api", "(*App).createOrder(w, r)", 1)), "api/orders.go")
        self.assertEqual(call_file(ctx, msg("api", "api", "mayRead(r)", 1)), "api/auth.go")
        self.assertIsNone(call_file(ctx, msg("api", "store", "(*App).createOrder()", 1)))


SERVER = """import json, sys
m = %s
open(sys.argv[sys.argv.index("-o") + 1], "w").write(json.dumps(m))
"""
CLIENT = """import json
print(json.dumps(%s))
"""


class ExportListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "server.py").write_text(SERVER % repr(server_part()))
        (self.root / "client.py").write_text(CLIENT % repr(client_part()))
        self.env = unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def configure(self, export):
        p = project.init(self.root, export=export, force=True)
        return project.Project(p.root)

    def test_merges_parts_from_out_and_stdout(self):
        p = self.configure([f"{sys.executable} server.py -o {{out}}",
                            {"cmd": f"{sys.executable} client.py", "prefix": "web"}])
        self.assertTrue(p.trusted())
        counts = p.export(require_trust=True)
        self.assertEqual(counts["components"], 3)
        m = json.loads(p.model_path.read_text())
        self.assertEqual(set(m["components"]), {"api", "store", "web/api"})
        self.assertEqual(set(m["sequences"]), {"open-order", "new-order"})
        self.assertTrue((self.root / ".archplan" / "parts" / "1.archmodel.json").is_file())
        self.assertIn("parts/", (self.root / ".archplan" / ".gitignore").read_text().split())

    def test_collision_is_an_error(self):
        p = self.configure([f"{sys.executable} server.py -o {{out}}", f"{sys.executable} client.py"])
        with self.assertRaises(ProjectError) as cm:
            p.export()
        self.assertIn("comes from two exporters", str(cm.exception))

    def test_a_changed_list_needs_a_new_approval(self):
        p = self.configure([f"{sys.executable} server.py -o {{out}}",
                            {"cmd": f"{sys.executable} client.py", "prefix": "web"}])
        self.assertTrue(p.trusted())
        cfg = json.loads((p.dir / "config.json").read_text())
        cfg["export"][1]["prefix"] = "spa"
        (p.dir / "config.json").write_text(json.dumps(cfg))
        p = project.Project(self.root)
        self.assertFalse(p.trusted())
        with self.assertRaises(project.UntrustedExport):
            p.export(require_trust=True)
        p.trust()
        self.assertTrue(p.trusted())
        # A changed file that one of the commands names also needs a new approval.
        (self.root / "client.py").write_text(CLIENT % repr(client_part()) + "\n")
        self.assertEqual(project.Project(self.root).trust_state(), (False, ["client.py"]))

    def test_string_form_still_works(self):
        p = self.configure(f"{sys.executable} server.py -o {{out}}")
        self.assertEqual(p.export_command(), f"{sys.executable} server.py -o {{out}}")
        p.export(require_trust=True)
        m = json.loads(p.model_path.read_text())
        self.assertEqual(set(m["components"]), {"api", "store"})


if __name__ == "__main__":
    unittest.main()
