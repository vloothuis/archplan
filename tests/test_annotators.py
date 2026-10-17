import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import annotators, markers, modelcheck, project  # noqa: E402
from archplan.pyexport import export  # noqa: E402


def write(root: Path, files: dict):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))


SAMPLE = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "shop/__init__.py": "",
    "shop/ops/__init__.py": "",
    "shop/ops/views.py": """\
        def login_required(f):
            return f

        # archplan: auth=none reason="load balancer health check"
        @login_required
        def health(request):
            return "ok"

        def orders(request):
            return []
        """,
    "shop/accounts/__init__.py": "",
    "shop/accounts/guards.py": """\
        # archplan: guard
        # Halts the request without a user.
        def require_user(request):
            return request

        def helper():
            pass
        """,
    "shop/accounts/GLOSSARY.md": "---\nzone: internal\nowns:\n  Account: [user]\n---\n**Account**: a login.\n",
}


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class AnnotatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        write(self.root, SAMPLE)
        self.env = unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config")})
        self.env.start()
        self.p = project.init(self.root, export="archplan export-python -o .archplan/model.archmodel.json")
        project.write_json(self.p.model_path, export(self.root))
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def model(self):
        return project.read_json(self.p.model_path)

    def declared(self, res=None):
        res = res or annotators.read(self.p)
        return {(d["target"]["kind"], d["target"]["id"], d["key"]): d for d in res["declarations"]}

    def test_builtin_read(self):
        res = annotators.read(self.p)
        d = self.declared(res)
        self.assertEqual(set(d), {("component", "shop.accounts", "zone"),
                                  ("function", "shop.accounts.guards.require_user", "guard"),
                                  ("entrypoint", "shop.ops.views.health", "auth")})
        auth = d[("entrypoint", "shop.ops.views.health", "auth")]
        self.assertEqual((auth["value"], auth["reason"], auth["file"], auth["line"]),
                         ("none", "load balancer health check", "shop/ops/views.py", 4))
        self.assertEqual(auth["at"], {"file": "shop/ops/views.py", "line": 6, "name": "health"})
        self.assertEqual(d[("component", "shop.accounts", "zone")]["value"], "internal")
        self.assertEqual(res["annotators"], [{"id": "builtin", "keys": sorted(annotators.KEYS), "count": 3}])
        self.assertEqual(res["warnings"], [])

    def test_marker_warnings_and_unknown_functions(self):
        write(self.root, {"shop/ops/extra.py": """\
            # archplan: guard frobnicate
            def check(x): ...

            # archplan: auth=none

            def detached(): ...
            """})
        res = annotators.read(self.p)
        d = self.declared(res)
        self.assertIn(("function", "shop/ops/extra.py:check", "guard"), d)  # not in the model yet
        self.assertTrue(any("unknown marker 'frobnicate'" in w for w in res["warnings"]))
        self.assertTrue(any("not directly above a declaration" in w for w in res["warnings"]))

    def test_other_languages(self):
        text = "//archplan:guard\n// RequireUser halts.\nfunc (s *S) RequireUser(next http.Handler) {}\n"
        self.assertEqual(markers.scan(text, "//")[0][2:], ({"function.guard": True}, None, [], 2, "RequireUser"))
        ex = '  # archplan: auth=required\n  @doc """\n  Lists.\n\n  More.\n  """\n  def index(conn, _), do: 1\n'
        self.assertEqual(markers.scan(ex, "#")[0][5:], (6, "index"))
        ts = "// archplan: guard\nexport async function requireUser(req) {}\n"
        self.assertEqual(markers.scan(ts, "//")[0][6], "requireUser")
        py = 'SAMPLE = """\n# archplan: guard\ndef inside_a_string(): ...\n"""\n# archplan: guard\ndef real(): ...\n'
        self.assertEqual([x[6] for x in markers.scan(py, "#", python=True)], ["real"])

    def test_export_adds_declarations(self):
        self.p.trust()
        project.Project(self.root).export()
        m = self.model()
        self.assertEqual(len(m["declarations"]), 3)
        self.assertTrue(modelcheck.check(m, self.root)["ok"])

    def changes(self, *cs, apply=False):
        return annotators.write(self.p, list(cs), apply=apply)

    def test_write_builtin_round_trip(self):
        before = (self.root / "shop/ops/views.py").read_text()
        res = self.changes(
            {"op": "set", "target": {"kind": "function", "id": "shop.ops.views.orders"}, "key": "guard",
             "value": True},
            {"op": "set", "target": {"kind": "entrypoint", "id": "shop.ops.views.health"}, "key": "auth",
             "value": "required"},
            {"op": "remove", "target": {"kind": "function", "id": "shop.accounts.guards.require_user"},
             "key": "guard"},
            {"op": "set", "target": {"kind": "component", "id": "shop.ops"}, "key": "zone", "value": "internet"},
            {"op": "set", "target": {"kind": "component", "id": "shop.accounts"}, "key": "zone", "value": "BAD"},
            {"op": "set", "target": {"kind": "entrypoint", "id": "shop.ops.views.orders"}, "key": "auth",
             "value": "none", "reason": "x\nimport os; os.system('id')"})
        self.assertEqual([r["status"] for r in res["results"]],
                         ["applied", "applied", "applied", "applied", "unsupported", "unsupported"])
        self.assertIn("one line", res["results"][5]["note"])
        for bad in ("a ?> b", "coding: rot13", "frozen_string_literal: true", "-*- coding=latin-1 -*-"):
            self.assertIn("may hold only", annotators.check_change(
                {"op": "set", "target": {"kind": "entrypoint", "id": "x"}, "key": "auth", "value": "none",
                 "reason": bad}))
        self.assertEqual(markers.token_of("entrypoint.auth", "none", 'a ?> "b": c'), 'auth=none reason="a ?   b   c"')
        self.assertIsNone(annotators.check_change(
            {"op": "set", "target": {"kind": "entrypoint", "id": "x"}, "key": "auth", "value": "none",
             "reason": "load-balancer health check (k8s), café"}))
        self.assertFalse(res["applied"])
        self.assertEqual((self.root / "shop/ops/views.py").read_text(), before)  # nothing changed yet
        self.assertIn("+# archplan: guard\n def orders", res["patch"])
        self.assertIn("-# archplan: auth=none", res["patch"])
        self.assertEqual(sorted(res["files"]), ["shop/accounts/guards.py", "shop/ops/GLOSSARY.md",
                                                "shop/ops/views.py"])
        res = self.changes(*[r["change"] for r in res["results"][:4]], apply=True)
        self.assertTrue(res["applied"])
        self.assertEqual((self.root / "shop/ops/GLOSSARY.md").read_text(), "---\nzone: internet\n---\n")
        self.assertEqual((self.root / "shop/accounts/guards.py").read_text().splitlines()[:2],
                         ["# Halts the request without a user.", "def require_user(request):"])
        d = self.declared()
        self.assertEqual(d[("entrypoint", "shop.ops.views.health", "auth")]["value"], "required")
        self.assertIn(("function", "shop.ops.views.orders", "guard"), d)
        self.assertNotIn(("function", "shop.accounts.guards.require_user", "guard"), d)
        res = self.changes({"op": "remove", "target": {"kind": "component", "id": "shop.ops"}, "key": "zone"},
                           apply=True)
        self.assertEqual(res["results"][0]["status"], "applied")
        self.assertFalse((self.root / "shop/ops/GLOSSARY.md").exists())

    def test_write_keeps_uncommitted_work(self):
        write(self.root, {"shop/ops/views.py": "# local edit\n" + (self.root / "shop/ops/views.py").read_text(),
                          "shop/ops/new.py": "def fresh(): ...\n"})
        res = self.changes({"op": "set", "target": {"kind": "function", "id": "shop/ops/new.py:fresh"},
                            "key": "guard", "value": True}, apply=True)
        self.assertEqual(res["results"][0]["status"], "applied", res)
        self.assertEqual((self.root / "shop/ops/new.py").read_text(), "# archplan: guard\ndef fresh(): ...\n")
        self.assertTrue((self.root / "shop/ops/views.py").read_text().startswith("# local edit\n"))
        out = subprocess.run(["git", "-C", str(self.root), "worktree", "list"], capture_output=True, text=True)
        self.assertEqual(len(out.stdout.strip().splitlines()), 1)  # the scratch worktree is gone

    def test_guard_levels_and_sinks(self):
        text = "# archplan: guard=authz\ndef can_edit(): ...\n# archplan: sink=write guard=root\ndef save(): ...\n"
        found = markers.scan(text, "#", python=True)
        self.assertEqual([x[2] for x in found], [{"function.guard": "authz"}, {"function.sink": "write"}])
        self.assertEqual(found[1][4], ["guard=root"])
        for value, ok in (("authn", True), ("authz", True), ("root", False)):
            self.assertEqual(annotators.check_change({"op": "set", "target": {"kind": "function", "id": "x"},
                                                      "key": "guard", "value": value}) is None, ok)
        self.assertIn("cannot be", annotators.check_change(
            {"op": "set", "target": {"kind": "function", "id": "x"}, "key": "sink", "value": "delete"}))
        res = self.changes(
            {"op": "set", "target": {"kind": "function", "id": "shop.ops.views.orders"}, "key": "sink", "value": "read"},
            {"op": "set", "target": {"kind": "function", "id": "shop.accounts.guards.require_user"}, "key": "guard",
             "value": "authn"}, apply=True)
        self.assertEqual([r["status"] for r in res["results"]], ["applied", "applied"], res)
        self.assertIn("# archplan: sink=read\ndef orders", (self.root / "shop/ops/views.py").read_text())
        self.assertIn("# archplan: guard=authn\n", (self.root / "shop/accounts/guards.py").read_text())
        self.assertNotIn("# archplan: guard\n", (self.root / "shop/accounts/guards.py").read_text())
        res = self.changes({"op": "remove", "target": {"kind": "function", "id": "shop.ops.views.orders"},
                            "key": "sink"}, apply=True)
        self.assertEqual(res["results"][0]["status"], "applied", res)
        self.assertNotIn("sink", (self.root / "shop/ops/views.py").read_text())

    def plugin(self, read_out, write_out=None, keys=("function.guard",), touch=None):
        script = self.root / "tools" / "ann.py"
        write(self.root, {"tools/ann.py": f"""\
            import json, sys
            from pathlib import Path
            action, ctx = sys.argv[1], json.load(open(sys.argv[2]))
            if action == "read":
                print({json.dumps(json.dumps(read_out))})
            else:
                if {touch!r}:
                    Path(ctx["root"], {touch!r}).write_text("x")
                print({json.dumps(json.dumps(write_out))})
            """})
        write(self.root, {".archplan/annotators/ann.json": json.dumps({
            "id": "ann", "keys": list(keys), "read": f"{sys.executable} {script} read {{ctx}}",
            "write": f"{sys.executable} {script} write {{ctx}}"})})
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "plugin")
        return annotators.load(self.p)[0]["ann"]

    def test_plugin_needs_approval_and_owns_its_keys(self):
        spec = self.plugin({"declarations": [
            {"target": {"kind": "function", "file": "shop/ops/views.py", "name": "orders", "line": 9},
             "key": "guard", "value": True},
            {"target": {"kind": "component", "id": "shop.ops"}, "key": "zone", "value": "internet"},
            {"target": {"kind": "function", "file": "../x.py", "name": "x"}, "key": "guard", "value": True},
            "junk"]})
        res = annotators.read(self.p)
        self.assertIn("not approved", next(a for a in res["annotators"] if a["id"] == "ann")["error"])
        self.assertNotIn(("function", "shop.accounts.guards.require_user", "guard"), self.declared(res))
        annotators.trust(self.p.root, spec)
        res = annotators.read(self.p)
        d = self.declared(res)
        self.assertIn(("function", "shop.ops.views.orders", "guard"), d)
        self.assertEqual(d[("function", "shop.ops.views.orders", "guard")]["annotator"], "ann")
        self.assertIn(("component", "shop.accounts", "zone"), d)  # still the built-in owner
        self.assertNotIn(("component", "shop.ops", "zone"), d)
        self.assertTrue(any("does not own component.zone" in w for w in res["warnings"]))
        self.assertTrue(any("needs an id, or a file and a name" in w for w in res["warnings"]))

    def test_plugin_write_is_checked_by_reading_back(self):
        spec = self.plugin({"declarations": []}, {"results": [{"status": "applied"}]}, touch="junk.txt")
        annotators.trust(self.p.root, spec)
        res = self.changes({"op": "set", "target": {"kind": "function", "id": "shop.ops.views.orders"},
                            "key": "guard", "value": True}, apply=True)
        self.assertEqual(res["results"][0]["status"], "failed")
        self.assertEqual(res["results"][0]["annotator"], "ann")
        self.assertEqual((res["files"], res["patch"]), ([], ""))  # what it wrote is left out
        self.assertFalse((self.root / "junk.txt").exists())

    def test_plugin_must_not_touch_archplan(self):
        orders = {"target": {"kind": "function", "id": "shop.ops.views.orders"}, "key": "guard", "value": True}
        spec = self.plugin({"declarations": [orders]}, {"results": [{"status": "applied"}]},
                           touch=".archplan/config.json")
        annotators.trust(self.p.root, spec)
        with self.assertRaises(project.ProjectError):
            self.changes({"op": "set", "target": {"kind": "function", "id": "shop.ops.views.orders"},
                          "key": "guard", "value": True})

    def test_go_markers_go_where_gofmt_puts_directives(self):
        src = "package auth\n\n// Require checks the token.\nfunc Require() {}\n\n// Health says ok.\n//\n" \
              "//nolint:funlen\nfunc Health() {}\n"
        fp = self.root / "shop/auth.go"
        fp.write_text(src)
        guard = {"op": "set", "target": {"kind": "function", "id": "shop/auth.go:Require"}, "key": "guard", "value": True}
        auth = {"op": "set", "target": {"kind": "entrypoint", "id": "shop/auth.go:Health"}, "key": "auth", "value": "none"}
        res = self.changes(guard, auth, apply=True)
        self.assertEqual([r["status"] for r in res["results"]], ["applied", "applied"], res)
        self.assertEqual(fp.read_text(), "package auth\n\n// Require checks the token.\n//\n//archplan:guard\n"
                                         "func Require() {}\n\n// Health says ok.\n//\n//nolint:funlen\n"
                                         "//archplan:auth=none\nfunc Health() {}\n")
        res = self.changes(dict(guard, op="remove"), dict(auth, op="remove"), apply=True)
        self.assertEqual(fp.read_text(), src)

    def test_write_keeps_line_endings_and_finds_declarations(self):
        (self.root / "shop/ops/crlf.py").write_bytes(b"def a():\r\n    pass\r\n\r\nhealth(1)\r\n@app.route(\r\n"
                                                     b"    \"/h\",\r\n)\r\ndef health():\r\n    pass\r\n")
        git(self.root, "-c", "diff.noprefix=true", "config", "diff.noprefix", "true")  # a user setting
        res = self.changes({"op": "set", "target": {"kind": "function", "id": "shop/ops/crlf.py:health"},
                            "key": "guard", "value": True}, apply=True)
        self.assertEqual(res["results"][0]["status"], "applied", res)
        self.assertEqual((self.root / "shop/ops/crlf.py").read_bytes(),
                         b"def a():\r\n    pass\r\n\r\nhealth(1)\r\n# archplan: guard\r\n@app.route(\r\n"
                         b"    \"/h\",\r\n)\r\ndef health():\r\n    pass\r\n")
        d = self.declared()
        self.assertEqual(d[("function", "shop/ops/crlf.py:health", "guard")]["at"]["line"], 9)
        self.assertIsNone(markers.declared_name("    return health(x)"))
        self.assertEqual(markers.declared_name("  public User find(String id) {"), "find")

    def test_scratch_is_removed_when_setup_fails(self):
        with unittest.mock.patch.object(annotators.Scratch, "snapshot", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.changes({"op": "set", "target": {"kind": "function", "id": "shop.ops.views.orders"},
                              "key": "guard", "value": True})
        out = subprocess.run(["git", "-C", str(self.root), "worktree", "list"], capture_output=True, text=True)
        self.assertEqual(len(out.stdout.strip().splitlines()), 1)

    def test_owners(self):
        a = {"id": "a", "keys": ["function.guard"], "read": "x {ctx}"}
        b = {"id": "b", "keys": ["function.guard", "component.zone"], "read": "x {ctx}"}
        specs = {s["id"]: annotators.check_entry(s) for s in (a, b)}
        own, conflicts = annotators.owners(self.p, specs)
        self.assertNotIn("function.guard", own)
        self.assertEqual((own["component.zone"], own["entrypoint.auth"]), ("b", "builtin"))
        self.assertIn("choose one in config.json", conflicts[0])
        self.p.config["annotators"] = {"owners": {"function.guard": "a"}, "builtin": False}
        own, conflicts = annotators.owners(self.p, specs)
        self.assertEqual(own, {"function.guard": "a", "component.zone": "b"})
        self.assertEqual(conflicts, [])
        for bad in ({"id": "builtin", "keys": ["function.guard"], "read": "x {ctx}"},
                    {"id": "c", "keys": ["function.nope"], "read": "x {ctx}"},
                    {"id": "c", "keys": ["function.guard"], "read": "x"}):
            with self.assertRaises(ValueError):
                annotators.check_entry(bad)


if __name__ == "__main__":
    unittest.main()
