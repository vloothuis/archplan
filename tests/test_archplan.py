import io
import json
import os
import shutil
import unittest.mock
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import cli, hotspots, model, project  # noqa: E402
from archplan.mcp import Agent, record, snapshot_text  # noqa: E402
from archplan.suggestions import Conflict, Suggestions  # noqa: E402
from archplan.pyexport import export  # noqa: E402
from archplan.store import Store, mentions, needs, who_next  # noqa: E402


def write(root: Path, files: dict):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))


SAMPLE = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "shop/__init__.py": "",
    "shop/orders/__init__.py": '"""Orders."""\n',
    "shop/orders/model.py": """\
        from dataclasses import dataclass
        from shop.billing.invoice import Invoice

        @dataclass
        class Order:
            id: str
            invoices: list[Invoice]

            @property
            def total(self) -> int: ...
            @total.setter
            def total(self, v: int) -> None: ...
        """,
    "shop/billing/__init__.py": "",
    "shop/billing/invoice.py": """\
        from typing import Protocol
        from ..orders import model

        class Payer(Protocol):
            def pay(self, amount: int) -> bool: ...

        class Invoice:
            def __init__(self, payer: Payer):
                self.payer: Payer = payer
            def order(self) -> "model.Order": ...
        """,
    "tests/__init__.py": "",
    "tests/test_x.py": "import shop\n",
}


class TempProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        write(self.root, SAMPLE)
        self.env = unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.tmp.name) / "config")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class PyExportTest(TempProject):
    def test_components_dependencies_types(self):
        m = export(self.root)
        self.assertEqual(m["format"], "archmodel/1")
        self.assertEqual(set(m["components"]), {"shop", "shop.orders", "shop.billing"})
        deps = {(d["from"], d["to"]) for d in m["dependencies"]}
        self.assertEqual(deps, {("shop.orders", "shop.billing"), ("shop.billing", "shop.orders")})
        order = m["types"]["shop.orders.model.Order"]
        self.assertEqual(order["kind"], "record")
        self.assertEqual(order["fields"][1], {"name": "invoices", "ref": "shop.billing.invoice.Invoice", "many": True})
        self.assertEqual([x["name"] for x in order["methods"]], ["total"])
        self.assertEqual(m["types"]["shop.billing.invoice.Payer"]["kind"], "interface")
        inv = m["types"]["shop.billing.invoice.Invoice"]
        self.assertEqual(inv["fields"], [{"name": "payer", "ref": "shop.billing.invoice.Payer"}])
        self.assertEqual(inv["uses"], ["shop.orders.model.Order"])

    def test_signatures_literals_enums(self):
        write(self.root, {"shop/status.py": textwrap.dedent("""\
            import enum
            from django.db import models
            class Status(enum.Enum):
                OPEN = "open"
                SHUT = "shut"
            class Kind(models.TextChoices):
                A = "a", "A"
                B = "b", "B"
            def close(order, reason: str = "manual") -> bool:
                if order.status in ("open", "held"):
                    order.status = "shut"
                    return save(order, mode="fast")
                return order.kind != "b"
            """)})
        m = export(self.root)
        sig = next(s for s in m["signatures"] if s["id"] == "shop.status.close")
        self.assertEqual((sig["params"], sig["returns"], sig["public"]),
                         ([{"name": "order", "type": None}, {"name": "reason", "type": "str"}], "bool", True))
        lits = {(x["value"], x["context"], x.get("field")) for x in m["literals"] if x["file"].endswith("status.py")}
        self.assertTrue({("manual", "default", "reason"), ("open", "compare", "status"), ("held", "compare", "status"),
                         ("shut", "assign", "status"), ("fast", "arg", "mode"), ("b", "compare", "kind")} <= lits)
        self.assertEqual({e["name"]: e["values"] for e in m["enums"]}, {"Status": ["open", "shut"], "Kind": ["a", "b"]})

    def test_tests_are_skipped_by_default(self):
        self.assertNotIn("tests", export(self.root)["components"])
        self.assertIn("tests.test_x", export(self.root, tests=True, granularity="module")["components"])


class ProjectTest(TempProject):
    def test_init_export_and_find(self):
        p = project.init(self.root)
        self.assertIn("export-python", p.config["export"])
        self.assertEqual(p.export()["components"], 3)
        sub = self.root / "shop" / "orders"
        self.assertEqual(project.Project.find(start=sub).root, self.root.resolve())
        with self.assertRaises(project.ProjectError):
            project.init(self.root)

    def test_save_target_keeps_base(self):
        p = project.init(self.root)
        p.export()
        rev = p.save_target({"format": "archmodel/1"}, "editor", project.etag(p.model_path))
        self.assertEqual(rev, 1)
        self.assertTrue(p.base_path.exists())
        self.assertEqual(p.state()["target_by"], "editor")


class DetectTest(unittest.TestCase):
    def test_elixir_in_root_or_largest_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(project.detect_export(root))
            write(root, {"core/mix.exs": "", "core/lib/a.ex": "", "core/lib/b/c.ex": "",
                         "proxy/mix.exs": "", "proxy/lib/a.ex": ""})
            self.assertEqual(project.detect_export(root),
                             "archplan export-elixir -C core -o .archplan/model.archmodel.json")
            write(root, {"mix.exs": ""})
            self.assertEqual(project.detect_export(root), "archplan export-elixir -o .archplan/model.archmodel.json")

    def test_typescript_in_root_or_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write(root, {"frontend/package.json": "{}", "frontend/src/a.ts": "", "docs/package.json": "{}"})
            self.assertEqual(project.detect_export(root), "archplan export-ts -C frontend -o .archplan/model.archmodel.json")
            write(root, {"package.json": "{}", "tsconfig.json": "{}"})
            self.assertEqual(project.detect_export(root), "archplan export-ts -o .archplan/model.archmodel.json")


TS_MODULES = Path(os.environ.get("ARCHPLAN_TS_MODULES", "/Users/jeroen/Work/sourcehaven/rela/frontend/node_modules"))
TS_SAMPLE = {
    "package.json": '{"name": "shop", "dependencies": {"react": "*"}}',
    "tsconfig.json": '{"compilerOptions": {"jsx": "preserve", "strict": true, "moduleResolution": "bundler",'
                     ' "module": "esnext", "paths": {"@/*": ["./src/*"]}}, "include": ["src"]}',
    "src/main.tsx": "import { Cart } from '@/components/Cart';\nexport const app = Cart;\n",
    "src/models/index.ts": "export { Order } from './order';\nexport type { Payer } from './payer';\n",
    "src/models/order.ts": "import type { Payer } from './payer';\n"
                           "export class Order implements Payer {\n  lines: Line[] = [];\n  pay(): void {}\n}\n"
                           "export interface Line { sku: string }\n",
    "src/models/payer.ts": "export interface Payer { pay(): void }\n",
    "src/components/Cart.tsx": "import React from 'react';\nimport { Order } from '../models';\n"
                               "import type { Payer } from '@/models/payer';\n"
                               "export function Cart(props: { order: Order; payer?: Payer }) {\n"
                               "  return <div>{props.order.lines.length}</div>;\n}\n",
    "src/components/Cart.test.tsx": "import { Cart } from './Cart';\nCart;\n",
    "src/models/status.ts": "export type Status = 'open' | 'closed';\n"
                            "export function isOpen(s: Status = 'open', n = 1) {\n"
                            "  switch (s) { case 'closed': return false; }\n  return s === 'open' && n > 0;\n}\n",
}


@unittest.skipUnless(shutil.which("mix"), "needs mix")
class ElixirValuesTest(unittest.TestCase):
    def test_signatures_literals_enums(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            write(root, {"mix.exs": "defmodule Shop.MixProject do\n  use Mix.Project\n"
                                    "  def project, do: [app: :shop, version: \"0.1.0\"]\nend\n",
                         "lib/order.ex": textwrap.dedent("""\
                             defmodule Shop.Order do
                               @type status :: :draft | :paid
                               @spec advance(map(), String.t()) :: atom()
                               def advance(%{status: :draft}, "pay"), do: :paid
                               def advance(order, _event), do: order.status
                               def label(order) do
                                 case order.kind do
                                   :retail -> "Retail"
                                   _ -> nil
                                 end
                               end
                             end
                             """)})
            wrapper = ROOT / "src" / "archplan" / "elixir" / "archplan-elixir"
            out = subprocess.run(["sh", str(wrapper), "export", "-o", "m.json", "--root", str(root)], cwd=root,
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            m = json.loads((root / "m.json").read_text())
        sig = next(s for s in m["signatures"] if s["id"] == "Shop.Order.advance/2")
        self.assertEqual((sig["params"], sig["returns"]),
                         ([{"name": "order", "type": "map()"}, {"name": "event", "type": "String.t()"}], "atom()"))
        lits = {(x["value"], x["context"], x.get("field") or x.get("target")) for x in m["literals"]}
        self.assertEqual(lits, {("draft", "case", "status"), ("pay", "head", "advance/2"), ("retail", "case", "kind")})
        self.assertEqual([e["values"] for e in m["enums"]], [["draft", "paid"]])


@unittest.skipUnless(shutil.which("node") and (TS_MODULES / "typescript").is_dir(), "needs node and typescript")
class TsExportTest(TempProject):
    def test_components_dependencies_types_files(self):
        root = Path(self.tmp.name) / "web"
        write(root, TS_SAMPLE)
        out = subprocess.run([sys.executable, "-m", "archplan", "export-ts"], cwd=root, capture_output=True, text=True,
                             env={**os.environ, "NODE_PATH": str(TS_MODULES), "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(out.returncode, 0, out.stderr)
        m = json.loads(out.stdout)
        comps = m["components"]
        self.assertEqual({c for c, v in comps.items() if v["kind"] == "internal"}, {"src", "models", "components"})
        self.assertEqual(comps["react"]["kind"], "external")
        self.assertEqual(comps["src"]["match"], ["src/main.tsx"])
        deps = {(d["from"], d["to"]): d for d in m["dependencies"]}
        self.assertEqual(set(deps), {("src", "components"), ("components", "models"), ("components", "react")})
        self.assertEqual(deps[("components", "models")]["evidence"], ["src/components/Cart.tsx:2"])
        # the barrel import resolves to order.ts; the test file is in files, not in dependencies
        self.assertEqual(m["files"]["src/components/Cart.tsx"]["refs"],
                         [{"to": "src/models/order.ts", "line": 2}, {"to": "src/models/payer.ts", "line": 3}])
        self.assertIn("src/components/Cart.test.tsx", m["files"])
        self.assertNotIn("typeOnly", deps[("components", "models")])  # Order is a value import
        order = m["types"]["models/order.Order"]
        self.assertEqual(order["implements"], ["models/payer.Payer"])
        self.assertEqual(order["fields"], [{"name": "lines", "ref": "models/order.Line", "many": True}])
        cart = m["types"]["components/Cart.Cart"]
        self.assertEqual((cart["kind"], cart.get("doc")), ("function", "React component"))
        self.assertEqual(cart["fields"], [{"name": "order", "ref": "models/order.Order"},
                                          {"name": "payer", "ref": "models/payer.Payer"}])
        sig = next(s for s in m["signatures"] if s["id"] == "models/status.isOpen")
        self.assertEqual((sig["params"], sig["returns"], sig["public"]),
                         ([{"name": "s", "type": "Status"}, {"name": "n", "type": "number"}], "boolean", True))
        lits = {(x["value"], x["context"], x.get("field")) for x in m["literals"] if x["file"] == "src/models/status.ts"}
        self.assertEqual(lits, {("open", "default", "s"), ("closed", "case", "s"), ("open", "compare", "s")})
        self.assertIn({"name": "Status", "component": "models", "file": "src/models/status.ts", "line": 1,
                       "values": ["open", "closed"]}, m["enums"])
        self.assertIn("literals", m["source"]["capabilities"])


class TrustTest(TempProject):
    def test_export_needs_approval(self):
        p = project.init(self.root)
        self.assertTrue(p.trusted())  # init approves the command it writes
        cfg = json.loads((p.dir / "config.json").read_text())
        cfg["export"] = "echo pwned > owned.txt"
        (p.dir / "config.json").write_text(json.dumps(cfg))
        p = project.Project(self.root)
        self.assertFalse(p.trusted())
        with self.assertRaises(project.UntrustedExport):
            p.export(require_trust=True)
        self.assertFalse((self.root / "owned.txt").exists())
        with self.assertRaises(project.UntrustedExport):
            Agent(p).refresh_model()
        # without a terminal, the command line cannot approve it either
        r = subprocess.run([str(ROOT / "bin" / "archplan"), "trust"], cwd=self.root, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(p.trusted())

    def test_prompt_shows_hidden_characters(self):
        self.assertEqual(cli.shown("ls\x1b[2K\rmake\u202e"), "ls\\u001b[2K\\u000dmake\\u202e")

    def test_own_exporter_ignores_planted_package(self):
        write(self.root, {"archplan/__init__.py": "", "archplan/__main__.py": "open('owned.txt', 'w')\n",
                          "archplan/cli.py": "open('owned.txt', 'w')\ndef main(argv=None): pass\n"})
        p = project.init(self.root)
        self.assertIn("archplan", p.export_command())
        p.export()
        self.assertFalse((self.root / "owned.txt").exists())


class ServerTest(TempProject):
    def test_host_and_origin_checks(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        from archplan.server import make_handler
        p = project.init(self.root)
        cfg = json.loads((p.dir / "config.json").read_text())
        cfg["export"] = "echo pwned > owned.txt"
        (p.dir / "config.json").write_text(json.dumps(cfg))
        p = project.Project(self.root)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p, Store(p.dir)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        def code(path, method="GET", headers=None, body=None):
            req = urllib.request.Request(url + path, method=method, headers=headers or {},
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code
        try:
            self.assertEqual(code("/api/info"), 200)
            self.assertEqual(code("/api/info", headers={"Host": "evil.example:80"}), 403)
            self.assertEqual(code("/api/threads", "POST", {"Origin": "http://evil.example"}, {"anchor": "a", "text": "b"}), 403)
            self.assertEqual(code("/api/threads", "POST", {"Content-Type": "application/json"}, {"anchor": "a", "text": "b"}), 200)
            self.assertEqual(code("/api/export", "POST", {}, {}), 403)  # an unapproved command is refused
            self.assertFalse((self.root / "owned.txt").exists())
        finally:
            srv.shutdown()
            srv.server_close()


class StoreTest(unittest.TestCase):
    def test_threads(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            th = s.create("comp:a", "a", "why?", "human")
            self.assertEqual(who_next(th), "agent")
            th = s.reply(th["id"], "pick one", "agent", kind="decision", options=["x", "y"])
            self.assertEqual((who_next(th), needs(th)), ("human", "decision"))
            th = s.reply(th["id"], "", "human", status="resolved")
            self.assertEqual(who_next(th), "nobody")
            with self.assertRaises(ValueError):
                s.reply(th["id"], "", "human", status="bogus")


class EditTest(TempProject):
    def test_edit_target(self):
        p = project.init(self.root)
        p.export()
        out = model.edit_target(p, [
            {"op": "add_component", "id": "shop.ports", "path": "shop/ports"},
            {"op": "move_type", "type": "shop.billing.invoice.Payer", "to": "shop.ports"},
            {"op": "forbid", "from": "shop.billing", "to": "shop.orders"},
        ])
        self.assertEqual(out["violations"], 1)
        t = json.loads(p.target_path.read_text())
        self.assertEqual(t["types"]["shop.billing.invoice.Payer"]["component"], "shop.ports")
        self.assertEqual(p.state()["target_by"], "agent")
        # all or nothing
        with self.assertRaises(ValueError):
            model.edit_target(p, [{"op": "add_component", "id": "x"}, {"op": "move_type", "type": "nope", "to": "x"}])
        self.assertNotIn("x", json.loads(p.target_path.read_text())["components"])
        self.assertEqual(model.get_item(p, "cedge:shop.billing->shop.orders")["allowed"], False)

    def test_agent_status_and_find(self):
        p = project.init(self.root)
        p.export()
        a = Agent(p)
        Store(p.dir).create("comp:shop.orders", "orders", "split this?", "human")
        st = a.status()
        self.assertEqual(len(st["threads"]["waiting_on_agent"]), 1)
        self.assertEqual(a.find("invoice")[0]["anchor"], "type:shop.billing.invoice.Invoice")


class HookTest(TempProject):
    def run_hook(self, ev):
        buf = io.StringIO()
        sys.stdin = io.StringIO(json.dumps(ev))
        try:
            with redirect_stdout(buf):
                cli.main(["hook"])
        finally:
            sys.stdin = sys.__stdin__
        return buf.getvalue()

    def test_tells_once_per_session(self):
        p = project.init(self.root)
        Store(p.dir).create("comp:shop", "shop", "look at this", "human")
        ev = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": str(self.root)}
        self.assertIn("look at this", self.run_hook(ev))
        self.assertEqual(self.run_hook(ev), "")
        self.assertIn("look at this", self.run_hook({**ev, "session_id": "s2"}))
        self.assertEqual(self.run_hook({**ev, "cwd": tempfile.gettempdir()}), "")

    def test_handover_and_planned(self):
        from archplan import handover
        p = project.init(self.root)
        s = Store(p.dir)
        th = s.create("comp:shop", "shop", "split it", "human")
        handover.lock(s, th["id"], "Split shop")
        ev = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": str(self.root)}
        self.assertEqual(self.run_hook(ev), "")  # planned: not for the agent yet
        h = handover.Handovers(p.dir).create(s, "batches")
        out = self.run_hook(ev)
        self.assertIn(f"handover {h['id']}", out)
        self.assertIn("propose batches first", out)
        self.assertIn("split it", out)  # the thread now waits on the agent
        self.assertEqual(self.run_hook(ev), "")


class McpTest(TempProject):
    def test_stdio(self):
        project.init(self.root).export()
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_item", "arguments": {"anchor": "comp:shop.orders"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}]
        r = subprocess.run([str(ROOT / "bin" / "archplan"), "mcp"], cwd=self.root, capture_output=True, text=True,
                           input="\n".join(json.dumps(x) for x in reqs) + "\n", timeout=30)
        out = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3])
        self.assertEqual(json.loads(out[1]["result"]["content"][0]["text"])["path"], "shop/orders")
        self.assertIn("error", out[2])

    def test_unconfigured(self):
        with tempfile.TemporaryDirectory() as d:
            reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status"}}]
            r = subprocess.run([str(ROOT / "bin" / "archplan"), "mcp"], cwd=d, capture_output=True, text=True,
                               input="\n".join(json.dumps(x) for x in reqs) + "\n", timeout=30,
                               env={"PATH": "/usr/bin:/bin"})
            out = [json.loads(l) for l in r.stdout.splitlines()]
            self.assertIn("archplan init", out[1]["result"]["content"][0]["text"])


def serve_in_thread(p):
    """An editor server on a free port; returns (server, base url)."""
    import threading
    from http.server import ThreadingHTTPServer
    from archplan.server import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(p, Store(p.dir)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call(url, path, method="GET", body=None):
    """(status, JSON body) of an API request."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.load(e)


class HotspotTest(unittest.TestCase):
    COMPS = {"orders": {"path": "lib/orders", "match": ["lib/orders/**"]},
             "billing": {"path": "lib/billing", "match": ["lib/billing/**", "lib/billing.ex"]},
             "web": {"path": "lib/web"},
             "core": {"path": "lib", "match": ["lib/core.ex"]},
             "ext": {"path": "deps/x", "kind": "external"}}

    def test_mapping_longest_match_and_prefix_offset(self):
        m = hotspots.Mapper(self.COMPS)
        self.assertEqual(m("lib/orders/a.ex"), "orders")
        self.assertEqual(m("lib/billing.ex"), "billing")
        self.assertEqual(m("lib/web/x/y.ex"), "web")  # path, when there are no match patterns
        self.assertEqual(m("lib/core.ex"), "core")
        self.assertEqual(m("lib/other.ex"), "core")  # the longest component path that contains it
        self.assertIsNone(m("deps/x/a.ex"))  # external code is not ours
        self.assertIsNone(m("README.md"))
        # git reports paths from the repository root; this model was exported in core/
        files = ["core/lib/orders/a.ex", "core/lib/billing/b.ex", "core/lib/web/c.ex", "README.md", "assets/x.js"]
        self.assertEqual(hotspots.best_prefix(files, m), "core/")
        self.assertEqual(hotspots.best_prefix(["lib/orders/a.ex", "lib/web/b.ex"], m), "")

    def test_pairs_from_git(self):
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            self.skipTest("no git")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            sub = root / "core"
            sub.mkdir(parents=True)
            env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                   "GIT_COMMITTER_EMAIL": "a@x", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            git = lambda *a: subprocess.run(["git", *a], cwd=root, env=env, check=True, capture_output=True)
            git("init", "-q")
            n = [0]

            def commit(*paths, email="a@x"):
                n[0] += 1
                for f in paths:
                    write(root, {f: f"v{n[0]}\n"})
                git("add", "-A")
                subprocess.run(["git", "commit", "-q", "-m", f"c{n[0]}"], cwd=root, check=True, capture_output=True,
                               env={**env, "GIT_AUTHOR_EMAIL": email})
            for _ in range(3):
                commit("core/lib/orders/a.ex", "core/lib/billing/b.ex")
            commit("core/lib/orders/a.ex", email="b@x")
            commit("core/lib/web/w.ex", "core/lib/orders/c.ex")
            commit(*[f"core/lib/web/f{i}.ex" for i in range(6)])  # bulk: skipped with max_files=5
            commit("README.md")
            write(root, {"core/.archplan/config.json": "{}"})
            m = {"components": self.COMPS, "dependencies": [{"from": "web", "to": "orders"}]}
            res = hotspots.analyse(hotspots.history(root, 30), m, max_files=5, min_together=1, min_degree=0.3)
            self.assertEqual((res["commits"], res["skipped"], res["unmapped"], res["prefix"]), (6, 1, 1, "core/"))
            self.assertEqual(res["components"]["orders"]["commits"], 5)
            self.assertEqual(res["components"]["orders"]["authors"], 2)
            self.assertEqual(res["components"]["orders"]["files"], 2)
            pairs = {(x["a"], x["b"]): x for x in res["pairs"]}
            self.assertEqual(pairs[("billing", "orders")], {"a": "billing", "b": "orders", "together": 3,
                                                            "degree": 1.0, "dependency": False})
            self.assertEqual(pairs[("orders", "web")]["dependency"], True)
            self.assertEqual(res["pairs"][0]["a"], "billing")  # sorted by together * degree
            # through the project, with the cache
            p = project.Project(sub)
            (sub / ".archplan" / "model.archmodel.json").write_text(json.dumps({"format": "archmodel/1", **m}))
            r1 = hotspots.hotspots(p, 30, max_files=5, min_together=3)
            self.assertEqual([(x["a"], x["b"]) for x in r1["pairs"]], [("billing", "orders")])
            self.assertTrue((sub / ".archplan" / "hotspots.json").exists())
            self.assertEqual(hotspots.hotspots(p, 30, max_files=5, min_together=3), r1)
            self.assertEqual(hotspots.info(sub)["head"], r1["head"][:len(hotspots.info(sub)["head"])])
            self.assertIn("billing <-> orders", hotspots.text(r1))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(hotspots.info(Path(d)))


class SuggestionTest(TempProject):
    OPS = [{"op": "add_component", "id": "shop.ports", "path": "shop/ports"},
           {"op": "forbid", "from": "shop.billing", "to": "shop.orders"},
           {"op": "move_type", "type": "shop.billing.invoice.Payer", "to": "shop.ports"}]

    def test_propose_preview_accept_reject(self):
        p = project.init(self.root)
        p.export()
        a = Agent(p)
        th = Store(p.dir).create("comp:shop.billing", "billing", "split ports out?", "human")
        with self.assertRaises(ValueError):  # an op that does not apply saves nothing
            a.edit_target([{"op": "move_type", "type": "nope", "to": "shop"}])
        out = a.edit_target(self.OPS, note="Ports out of billing", thread=th["id"])
        self.assertEqual((out["status"], out["ops"]), ("pending", 3))
        self.assertFalse(p.target_path.exists())  # the target is unchanged
        self.assertIn(out["suggestion"], Store(p.dir).read()["threads"][0]["messages"][-1]["text"])
        sid = out["suggestion"]
        self.assertEqual(a.status()["suggestions"]["pending"], 1)
        srv, url = serve_in_thread(p)
        try:
            st, d = call(url, "/api/suggestions")
            self.assertEqual([x["id"] for x in d["suggestions"]], [sid])
            key = d["key"]
            st, d = call(url, f"/api/suggestions/{sid}/preview?ops=0,1")
            self.assertEqual(st, 200)
            self.assertIn("shop.ports", d["target"]["components"])
            self.assertEqual(d["target"]["types"]["shop.billing.invoice.Payer"]["component"], "shop.billing")
            self.assertEqual(call(url, f"/api/suggestions/{sid}/preview?ops=2")[0], 409)  # needs op 0 first
            self.assertEqual(call(url, f"/api/suggestions/{sid}/preview?ops=7")[0], 400)
            self.assertEqual(call(url, "/api/suggestions/sg-nope/preview")[0], 404)
            st, d = call(url, f"/api/suggestions/{sid}/accept", "POST", {"ops": [1, 0]})
            self.assertEqual(st, 200)
            sg = d["suggestion"]
            self.assertEqual((sg["status"], sg["accepted"]), ("partial", [0, 1]))
            self.assertIsNotNone(sg["decided"])
            self.assertNotEqual(d["key"], key)  # the editor's poll sees the change
            t = json.loads(p.target_path.read_text())
            self.assertIn("shop.ports", t["components"])
            self.assertEqual(p.state()["target_by"], "agent-suggestion")
            self.assertEqual(call(url, f"/api/suggestions/{sid}/reject", "POST", {})[0], 409)  # already decided
            # a second suggestion, made against the new target, is rejected
            sid2 = a.edit_target(self.OPS[2:])["suggestion"]
            st, d = call(url, f"/api/suggestions/{sid2}/reject", "POST", {})
            self.assertEqual((d["suggestion"]["status"], d["suggestion"]["accepted"]), ("rejected", []))
            self.assertEqual(json.loads(p.target_path.read_text()), t)
            # an op that no longer applies cannot be accepted
            sid3 = a.edit_target([{"op": "add_component", "id": "shop.x"}])["suggestion"]
            model.edit_target(p, [{"op": "add_component", "id": "shop.x"}], by="editor")
            self.assertEqual(call(url, f"/api/suggestions/{sid3}/accept", "POST", {})[0], 409)
            self.assertEqual(Suggestions(p.dir).get(sid3)["status"], "pending")
        finally:
            srv.shutdown()
            srv.server_close()
        from archplan.watch import suggestion_line
        self.assertEqual(suggestion_line(sg), f"suggestion {sid} in thread {th['id']} partly accepted "
                                              "(2 of 3 ops: 0, 1): Ports out of billing")


class ThreadExtrasTest(TempProject):
    def test_snapshot_and_mentions(self):
        p = project.init(self.root)
        p.export()
        snap = {"view": "Components", "mode": "Diff", "focus": "shop.orders", "filters": {"hideLeaves": True},
                "nodes": [{"id": "shop.orders", "label": "Orders"}, "shop.billing"],
                "edges": [{"from": "shop.orders", "to": "shop.billing"}]}
        srv, url = serve_in_thread(p)
        try:
            st, th = call(url, "/api/threads", "POST", {"anchor": "comp:shop.orders", "snapshot": snap,
                                                        "text": "Why does @[Orders](comp:shop.orders) call "
                                                                "@[Invoice](type:shop.billing.invoice.Invoice)?"})
            self.assertEqual((st, th["snapshot"]), (200, snap))
            big = {"nodes": ["x" * 100] * 1000}
            self.assertEqual(call(url, "/api/threads", "POST", {"anchor": "a", "text": "b", "snapshot": big})[0], 400)
            self.assertEqual(call(url, "/api/activity")[1], {})
        finally:
            srv.shutdown()
            srv.server_close()
        r = Agent(p).read_thread(th["id"])
        self.assertEqual(r["snapshot"], "snapshot: Components view; Diff; focus: shop.orders; filters: hideLeaves; "
                                        "nodes (2): Orders, shop.billing; edges (1): shop.orders -> shop.billing")
        self.assertEqual(r["messages"][0]["text"],
                         "Why does @Orders (comp:shop.orders) call @Invoice (type:shop.billing.invoice.Invoice)?")
        self.assertEqual(mentions("see @[](task:t1) and @[a]b"), "see @task:t1 (task:t1) and @[a]b")
        self.assertEqual(snapshot_text({}), "snapshot: empty")

    def test_activity(self):
        p = project.init(self.root)
        p.export()
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "open_thread", "arguments": {"anchor": "task:split-orders", "text": "done?"}}}]
        subprocess.run([str(ROOT / "bin" / "archplan"), "mcp"], cwd=self.root, capture_output=True, text=True,
                       input="\n".join(json.dumps(x) for x in reqs) + "\n", timeout=30)
        act = json.loads(p.path("activity.json").read_text())
        self.assertEqual((act["tool"], act["summary"], act["task"]),
                         ("open_thread", "Opened a thread on task:split-orders", "split-orders"))
        th = Store(p.dir).read()["threads"][0]
        record(p, Store(p.dir), "reply", {"id": th["id"], "text": "x"})
        act = json.loads(p.path("activity.json").read_text())
        self.assertEqual((act["summary"], act["thread"], act["task"]),
                         (f"Replied in thread {th['id']}", th["id"], "split-orders"))
        record(p, Store(p.dir), "edit_target", {"ops": [{}, {}]})
        self.assertEqual(json.loads(p.path("activity.json").read_text())["summary"], "Proposed 2 change(s)")


class RefactorTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.export()
        self.s = Store(self.p.dir)
        self.ids = [self.s.create(f"comp:shop.{x}", x, f"refactor {x}?", "human")["id"] for x in ("a", "b", "c")]
        self.srv, self.url = serve_in_thread(self.p)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_lock_unlock_order(self):
        a, b, c = self.ids
        st, th = call(self.url, f"/api/threads/{a}/lock", "POST", {"title": "Split A", "summary": "Move x"})
        self.assertEqual((st, th["status"], th["refactor"]["order"], th["refactor"]["state"]), (200, "planned", 1, "planned"))
        self.s.reply(b, "", "human", status="resolved")
        self.assertEqual(call(self.url, f"/api/threads/{b}/lock", "POST", {"title": "B"})[1]["refactor"]["order"], 2)
        self.assertEqual(call(self.url, f"/api/threads/{a}/lock", "POST", {})[0], 409)
        self.assertEqual(call(self.url, "/api/threads/th-nope/lock", "POST", {})[0], 404)
        # a reply keeps it planned, and planned threads do not wait on the agent
        self.s.reply(a, "one more thing", "human", status="open")
        self.assertEqual(self.s.read()["threads"][0]["status"], "planned")
        self.assertEqual([t["id"] for t in Agent(self.p).list_threads(waiting_on="agent")], [c])
        self.assertEqual([t["id"] for t in Agent(self.p).status()["threads"]["waiting_on_agent"]], [c])
        st, th = call(self.url, f"/api/threads/{a}/refactor", "PUT", {"summary": "Move x and y"})
        self.assertEqual((th["refactor"]["title"], th["refactor"]["summary"]), ("Split A", "Move x and y"))
        self.assertEqual(call(self.url, f"/api/threads/{c}/refactor", "PUT", {"title": "x"})[0], 409)
        st, d = call(self.url, "/api/refactors/order", "PUT", {"ids": [b, a]})
        self.assertEqual([(t["id"], t["refactor"]["order"]) for t in d["refactors"]], [(b, 1), (a, 2)])
        self.assertEqual(call(self.url, "/api/refactors/order", "PUT", {"ids": [c]})[0], 400)
        st, th = call(self.url, f"/api/threads/{b}/unlock", "POST", {})
        self.assertEqual((th["status"], "refactor" in th), ("open", False))

    def test_handover_batches(self):
        a, b, c = self.ids
        self.assertEqual(call(self.url, "/api/handover", "POST", {"mode": "batches"})[0], 400)  # nothing planned
        for t in (a, b):
            call(self.url, f"/api/threads/{t}/lock", "POST", {"title": t})
        key = call(self.url, "/api/handover")[1]["key"]
        st, h = call(self.url, "/api/handover", "POST", {"mode": "batches", "note": "small PRs"})
        self.assertEqual((st, h["status"], h["refactors"]), (200, "sent", [a, b]))
        self.assertEqual(call(self.url, "/api/handover", "POST", {"mode": "all"})[0], 409)
        d = call(self.url, "/api/handover")[1]
        self.assertEqual((d["current"]["id"], d["history"]), (h["id"], []))
        self.assertNotEqual(d["key"], key)
        self.assertEqual(call(self.url, f"/api/threads/{a}/unlock", "POST", {})[0], 409)
        ag = Agent(self.p)
        self.assertEqual(ag.status()["handover"]["status"], "sent")
        self.assertEqual({t["id"] for t in ag.list_threads("all", "agent")}, {a, b, c})  # now they wait on the agent
        g = ag.get_handover()
        self.assertEqual([r["thread"] for r in g["refactors"]], [a, b])
        self.assertIn("propose_batches", g["todo"])
        with self.assertRaises(Conflict):  # batches first
            ag.set_refactor_state(a, "in-progress")
        for bad in ([{"title": "x", "refactors": [a]}], [{"title": "x", "refactors": [a, b, a]}],
                    [{"title": "x", "refactors": [a, b, c]}], []):
            with self.assertRaises(ValueError):
                ag.propose_batches(bad)
        self.assertEqual(ag.propose_batches([{"title": "one", "refactors": [b]}, {"title": "two", "refactors": [a]}])["status"],
                         "proposed")
        self.assertEqual(call(self.url, "/api/handover/ho-nope/approve", "POST", {})[0], 404)
        st, h2 = call(self.url, f"/api/handover/{h['id']}/approve", "POST",
                      {"batches": [{"title": "both", "refactors": [a, b], "notes": "edited"}]})
        self.assertEqual((h2["status"], len(h2["batches"]), h2["decided"] is not None), ("approved", 1, True))
        self.assertEqual(call(self.url, f"/api/handover/{h['id']}/approve", "POST", {})[0], 409)
        out = ag.set_refactor_state(a, "in-progress")
        self.assertEqual((out["status"], out["progress"]), ("in-progress", f"Working on refactor 1 of 2: {a}"))
        ag.set_refactor_state(a, "done", note="moved")
        out = ag.set_refactor_state(b, "done")
        self.assertEqual(out["status"], "done")
        d = call(self.url, "/api/handover")[1]
        self.assertEqual((d["current"], d["history"][0]["status"]), (None, "done"))
        self.assertEqual(ag.status()["threads"]["waiting_on_agent"][0]["id"], c)

    def test_handover_all_and_cancel(self):
        from archplan.watch import handover_line
        a, b, c = self.ids
        for t in (a, b):
            call(self.url, f"/api/threads/{t}/lock", "POST", {"title": t})
        st, h = call(self.url, "/api/handover", "POST", {"mode": "all", "note": "go"})
        self.assertEqual(handover_line(h, None), f"handover {h['id']} sent: 2 planned refactors, all in one go: go. "
                                                 "Call get_handover.")
        ag = Agent(self.p)
        with self.assertRaises(Conflict):
            ag.propose_batches([{"title": "x", "refactors": [a, b]}])
        self.assertEqual(ag.set_refactor_state(a, "done")["status"], "in-progress")
        ag.set_refactor_state(b, "in-progress")
        st, h = call(self.url, f"/api/handover/{h['id']}/cancel", "POST", {})
        self.assertEqual(h["status"], "cancelled")
        self.assertEqual(handover_line(h, "in-progress"), f"handover {h['id']} cancelled")
        states = {t["id"]: t["refactor"]["state"] for t in self.s.read()["threads"] if t.get("refactor")}
        self.assertEqual(states, {a: "done", b: "planned"})  # done work stays done
        self.assertEqual(call(self.url, f"/api/handover/{h['id']}/cancel", "POST", {})[0], 409)
        self.assertEqual([t["id"] for t in ag.list_threads(waiting_on="agent")], [c])
        # a new handover takes only the refactors that are not done
        self.assertEqual(call(self.url, "/api/handover", "POST", {"mode": "all"})[1]["refactors"], [b])


def synthetic_model(n=6, skip_builder=(), extra=()):
    """A model with n systems, each with a public API, queries, a view with a
    builder and a model; file refs follow the convention unless told otherwise."""
    files = {}
    for i in range(n):
        d, t = f"core/systems/s{i}", f"thing{i}"
        files[f"{d}/_public.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/_queries.ex", "line": 3}]}
        files[f"{d}/_queries.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/{t}_model.ex", "line": 4}]}
        files[f"{d}/{t}_model.ex"] = {"component": f"S{i}", "refs": []}
        files[f"{d}/{t}_view.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/{t}_view_builder.ex", "line": 5}]}
        if i not in skip_builder:
            files[f"{d}/{t}_view_builder.ex"] = {"component": f"S{i}", "refs": [{"to": f"{d}/_public.ex", "line": 6}]}
        files[f"{d}/h{i}.ex"] = {"component": f"S{i}", "refs": []}
        if i:  # each system calls the previous one's public API, several times over
            files[f"{d}/_public.ex"]["refs"] += [{"to": f"core/systems/s{i - 1}/_public.ex", "line": 9}]
            for k in range(4):
                files[f"{d}/x{i}{k}.ex"] = {"component": f"S{i}", "refs": [{"to": f"core/systems/s{i - 1}/_public.ex"}]}
    files["core/web/page.ex"] = {"component": "Web", "refs": []}
    for src, dst in extra:
        files[src]["refs"].append({"to": dst, "line": 1})
    comps = {f"S{i}": {"path": f"core/systems/s{i}", "match": [f"core/systems/s{i}/**"], "layer": "systems"}
             for i in range(n)}
    comps["Web"] = {"path": "core/web", "match": ["core/web/**"], "layer": "web"}
    return {"format": "archmodel/1", "components": comps, "dependencies": [], "types": {}, "files": files,
            "layers": [{"id": "web", "name": "Web"}, {"id": "systems", "name": "Systems"}]}


class ArchitectureTest(unittest.TestCase):
    def test_globs(self):
        from archplan.architecture import glob_match, role_match
        self.assertTrue(glob_match("core/systems/lab", "core/systems/*"))
        self.assertFalse(glob_match("core/systems/lab/x", "core/systems/*"))
        self.assertTrue(glob_match("src/a/b/app", "src/**"))
        self.assertTrue(role_match("models/zaak.py", "models/**"))
        self.assertTrue(role_match("sub/day_view.ex", "*_view.ex"))
        self.assertFalse(role_match("day_view_builder.ex", "*_view.ex"))

    def test_infer_and_breaks(self):
        from archplan import architecture as A
        m = synthetic_model(skip_builder=(2,), extra=[("core/systems/s3/thing3_view.ex", "core/systems/s3/_queries.ex"),
                                                      ("core/web/page.ex", "core/systems/s1/_queries.ex")])
        p = A.infer(m)
        self.assertEqual(p["source"], "inferred")
        self.assertEqual(p["units"], [{"kind": "System", "glob": "core/systems/*"}])
        roles = {r["id"]: r["patterns"] for r in p["roles"]}
        self.assertEqual(roles, {"public": ["_public.ex"], "queries": ["_queries.ex"], "model": ["*_model.ex"],
                                 "view": ["*_view.ex"], "view_builder": ["*_view_builder.ex"]})
        rules = {r["id"]: r for r in p["rules"]}
        self.assertEqual(rules["other-units-call-public"]["to"], "public")
        self.assertEqual(rules["view-has-view_builder"]["match"], ["<stem>_view.ex -> <stem>_view_builder.ex"])
        self.assertEqual([x["name"] for x in p["layers"]], ["Web", "Systems"])
        a = A.analyse(p, m)
        self.assertEqual(len(a["units"]), 6)
        self.assertEqual(a["roles"][0], {"id": a["roles"][0]["id"], "present": 6, "of": 6})
        self.assertEqual(a["unmatched"], 6 + 5 * 4)  # h<i>.ex everywhere, x*.ex in five
        res = {r["id"]: r for r in a["rules"]}
        pair = res["view-has-view_builder"]
        self.assertEqual((pair["count"], pair["breaks"][0]["to"]), (1, "core/systems/s2/thing2_view_builder.ex"))
        calls = res["other-units-call-public"]
        self.assertEqual(calls["count"], 1)
        self.assertEqual(calls["breaks"][0]["from"], "core/web/page.ex")  # code outside units counts as another unit
        self.assertEqual(a["layers"]["unassigned"], [])
        s3 = next(u for u in a["units"] if u["id"] == "core/systems/s3")
        self.assertEqual(s3["component"], "S3")
        self.assertLess(next(u for u in a["units"] if u["id"] == "core/systems/s2")["conformance"], 1)
        # a model without file references still gets roles, and says why rules cannot be checked
        m2 = dict(m, files={})
        m2["types"] = {f"T{i}": {"file": f} for i, f in enumerate(m["files"])}
        self.assertTrue(A.analyse(p, m2)["warnings"])

    def test_django(self):
        from archplan import architecture as A
        files = ["src/proj/settings.py", "src/proj/urls.py",
                 "src/proj/shop/apps.py", "src/proj/shop/models.py", "src/proj/shop/views.py",
                 "src/proj/shop/api/serializers.py", "src/proj/shop/api/viewsets.py",
                 "src/proj/shop/api/helpers.py",
                 "src/proj/billing/apps.py", "src/proj/billing/models/__init__.py",
                 "src/proj/billing/models/invoice.py", "src/proj/billing/admin.py",
                 "src/proj/billing/templatetags/money.py"]
        refs = {"src/proj/shop/models.py": ["src/proj/shop/views.py"],  # models import a view: a break
                "src/proj/billing/admin.py": ["src/proj/shop/views.py"],  # another app's views: a break
                "src/proj/shop/api/serializers.py": ["src/proj/shop/api/viewsets.py", "src/proj/billing/models/invoice.py"]}
        m = {"components": {}, "types": {}, "files": {f: {"refs": [{"to": t} for t in refs.get(f, [])]} for f in files}}
        from archplan import frameworks as F
        dj = F.load()[0]["django"]
        self.assertTrue(F.detects(dj, F.Context(files)))
        self.assertFalse(F.detects(dj, F.Context(["src/a/apps.py", "src/a/models.py"])))
        p = A.infer(m)
        self.assertEqual(p["source"], "django")
        self.assertEqual(p["units"][0]["glob"], "src/proj/**")
        a = A.analyse(p, m)
        self.assertEqual([u["id"] for u in a["units"]], ["src/proj/billing", "src/proj/shop"])
        shop = {f["path"].rsplit("/", 1)[-1]: f["role"] for f in a["units"][1]["files"]}
        self.assertEqual(shop, {"apps.py": None, "models.py": "models", "views.py": "views",
                                "serializers.py": "serializers", "viewsets.py": "viewsets", "helpers.py": None})
        billing = {f["path"].rsplit("/", 1)[-1]: f["role"] for f in a["units"][0]["files"]}
        self.assertEqual((billing["invoice.py"], billing["money.py"]), ("models", "templatetags"))
        counts = {r["id"]: r["count"] for r in a["rules"]}
        self.assertEqual(counts, {"models-stay-low": 1, "apps-keep-views-private": 1, "serializers-not-views": 1})

    def test_hostile_patterns(self):
        from archplan.architecture import glob_match, pair_specs
        self.assertFalse(glob_match("/".join(["a"] * 40), "/".join(["**", "a"] * 20) + "/b"))
        self.assertTrue(glob_match("a", "a/**"))
        self.assertEqual(pair_specs({"match": ["<stem><stem>.ex -> x"]}), [])

    def test_colors(self):
        from archplan import architecture as A
        bad = {"roles": [{"id": "x", "patterns": [], "color": '"/><script>'}]}
        with self.assertRaises(ValueError):
            A.validate(bad)
        self.assertNotIn("color", A.clean_colors(bad)["roles"][0])
        self.assertEqual(A.validate({"roles": [{"id": "x", "patterns": [], "color": "#3b82f6"}]})["roles"][0]["color"], "#3b82f6")


class ArchitectureApiTest(TempProject):
    def test_round_trip(self):
        p = project.init(self.root)
        p.model_path.write_text(json.dumps(synthetic_model()))
        srv, url = serve_in_thread(p)
        try:
            st, d = call(url, "/api/architecture")
            self.assertEqual((st, d["profile"]), (200, None))
            prop = d["proposal"]
            self.assertEqual(len(d["analysis"]["units"]), 6)
            key = d["key"]
            prop["roles"][0]["name"] = "Public API"
            st, d = call(url, "/api/architecture", "PUT", {"profile": prop})
            self.assertEqual((st, d["profile"]["status"]), (200, "proposed"))
            self.assertNotEqual(d["key"], key)
            self.assertEqual(call(url, "/api/architecture", "PUT", {"profile": {"roles": [{"name": "x"}]}})[0], 400)
            st, d = call(url, "/api/architecture/lock", "POST", {})
            self.assertEqual(d["profile"]["status"], "locked")
            st, d = call(url, "/api/architecture/infer", "POST", {})
            self.assertEqual((st, d["saved"]), (200, False))  # a locked profile stays
            st, d = call(url, "/api/architecture")
            self.assertEqual((d["profile"]["status"], d["profile"]["roles"][0]["name"]), ("locked", "Public API"))
            self.assertNotIn("proposal", d)
        finally:
            srv.shutdown()
            srv.server_close()
        a = Agent(p)
        self.assertEqual(a.get_architecture()["status"], "locked")
        with self.assertRaises(ValueError):
            a.update_profile(d["profile"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["profile", "--dir", str(self.root)])
        self.assertIn("_public.ex", buf.getvalue())

    def test_python_exporter_files(self):
        m = export(self.root)
        f = m["files"]["shop/billing/invoice.py"]
        self.assertEqual(f["component"], "shop.billing")
        self.assertEqual([r["to"] for r in f["refs"]], ["shop/orders/model.py"])  # "from ..orders import model" is the module
        self.assertEqual([r["to"] for r in m["files"]["shop/orders/model.py"]["refs"]], ["shop/billing/invoice.py"])


def findings_model(ab=5):
    """Five components with a 2-cycle (D, E) and a 3-cycle (A, B, C), a type
    with groups of related fields and two types that share their fields."""
    comps = {c: {"name": c, "path": f"lib/{c.lower()}", "match": [f"lib/{c.lower()}/**"]} for c in "ABCDEFGHIJ"}
    deps = [("A", "B", ab), ("B", "C", 1), ("C", "A", 3), ("D", "E", 2), ("E", "D", 4)]
    fld = lambda *names: [{"name": n, "type": "string"} for n in names]
    types = {
        "A.User": {"name": "User", "component": "A", "kind": "struct", "file": "lib/a/user.ex", "line": 3,
                   "fields": fld("id", "payout_iban", "payout_name", "payout_status", "verified_email", "verified_phone",
                                 "verified_at", "notify_mail", "notify_sms", "notify_push", "name")},
        "B.Member": {"name": "Member", "component": "B", "kind": "struct", "file": "lib/b/member.ex",
                     "fields": fld("id", "user_id", "public_id", "status", "role", "joined_at", "inserted_at")},
        "C.Participant": {"name": "Participant", "component": "C", "kind": "struct", "file": "lib/c/participant.ex",
                          "fields": fld("id", "user_id", "public_id", "status", "role", "joined_at", "updated_at")},
    }
    return {"format": "archmodel/1", "components": comps, "types": types,
            "dependencies": [{"from": a, "to": b, "weight": w, "evidence": [f"lib/{a.lower()}/x.ex:{w}"]}
                             for a, b, w in deps]}


class FindingsTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(findings_model()))

    def test_cycles_cheapest_cut(self):
        from archplan.findings import cycle_findings, sccs
        m = findings_model()
        self.assertEqual(sorted(sccs("ABCDE", {("A", "B"), ("B", "A"), ("C", "D")})), [["A", "B"], ["C"], ["D"], ["E"]])
        cyc = {tuple(f["evidence"]["cycle"]): f for f in cycle_findings(m)}
        self.assertEqual(set(cyc), {("D", "E"), ("A", "B", "C")})
        self.assertEqual(cyc[("D", "E")]["evidence"]["cut"]["from"], "D")
        abc = cyc[("A", "B", "C")]
        self.assertEqual((abc["evidence"]["cut"]["from"], abc["evidence"]["cut"]["to"], abc["evidence"]["cut"]["weight"]),
                         ("B", "C", 1))
        self.assertEqual(abc["title"], "A → B → C → A form a dependency cycle.")
        self.assertEqual(abc["files"], ["lib/b/x.ex"])

    def test_fingerprints_stable(self):
        from archplan.findings import analyse
        ids = lambda m: {f["id"]: f["kind"] for f in analyse(self.p)[0]}
        before = ids(None)
        self.assertEqual(sorted(set(before.values())),
                         ["concepts.raw_ids", "structure.cycle", "types.fields", "types.synonyms"])
        self.p.model_path.write_text(json.dumps(findings_model(ab=50)))  # counts change, the findings do not
        os.utime(self.p.model_path, (1, 1))
        self.assertEqual(ids(None), before)

    def test_types(self):
        from archplan.findings import field_findings, synonym_findings
        m = findings_model()
        (f,) = field_findings(m)
        self.assertEqual(f["title"], "A.User has 3 groups of related fields.")
        self.assertEqual(sorted(f["evidence"]["groups"]), ["notify", "payout", "verified"])
        (s,) = synonym_findings(m)
        self.assertEqual(s["evidence"]["types"], ["B.Member", "C.Participant"])
        self.assertEqual(s["strength"], "strong")  # id and timestamps do not count

    def test_baseline_round_trip(self):
        from archplan.findings import Findings
        F = Findings(self.p)
        fs = F.listing()["findings"]
        a, b = fs[0]["id"], fs[1]["id"]
        with self.assertRaises(ValueError):
            F.decide(a, "dismissed", " ")  # a reason is required
        F.decide(a, "dismissed", "known, fixed next quarter")
        F.decide(b, "accepted")
        st = {f["id"]: (f["state"], f["reason"]) for f in Findings(self.p).listing()["findings"]}
        self.assertEqual(st[a], ("dismissed", "known, fixed next quarter"))
        self.assertEqual(st[b], ("accepted", None))
        saved = json.loads(self.p.path("baseline.json").read_text())
        self.assertEqual((saved["version"], saved["findings"][a]["title"]), (1, fs[0]["title"]))
        F.decide(a, "new")
        self.assertEqual(Findings(self.p).get(a)["state"], "new")
        F.label(b, "likely the same", "both hold a user's membership")
        self.assertEqual(Findings(self.p).get(b)["label"], "likely the same")
        # a baselined finding that leaves the code counts as fixed
        m = findings_model()
        m["dependencies"] = [d for d in m["dependencies"] if (d["from"], d["to"]) != ("E", "D")]
        self.p.model_path.write_text(json.dumps(m))
        cyc = next(f for f in fs if f["kind"] == "structure.cycle" and f["evidence"]["cycle"] == ["D", "E"])
        F.decide(cyc["id"], "accepted", "") if cyc["id"] != b else None
        os.utime(self.p.model_path, (2, 2))
        self.assertEqual(Findings(self.p).listing()["counts"]["fixed"], 1)
        r = Findings(self.p).update_baseline()
        self.assertEqual(r["removed"], 1)
        self.assertEqual(Findings(self.p).listing()["counts"]["new"], 0)

    def test_untrusted_baseline(self):
        from archplan.findings import Findings
        fid = Findings(self.p).listing()["findings"][0]["id"]
        self.p.path("baseline.json").write_text(json.dumps({"version": 9, "findings": {
            fid: {"state": "<script>", "reason": "x"}, "other": {"state": "dismissed", "reason": ["r" * 900], "at": 5},
            "bad": "nope"}, "labels": {fid: {"label": 7, "note": {"a": 1}}}}))
        lst = Findings(self.p).listing()
        f = next(x for x in lst["findings"] if x["id"] == fid)
        self.assertEqual((f["state"], f["label"], f["label_note"]), ("new", "7", "{'a': 1}"))
        e = Findings(self.p).baseline.read()["findings"]["other"]
        self.assertEqual((len(e["reason"]), e["at"], e["title"]), (500, "5", None))
        self.p.path("baseline.json").write_text("not json")
        self.assertEqual(Findings(self.p).listing()["counts"]["accepted"], 0)

    def test_check_exit_codes(self):
        def run(*args):
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as e, \
                    unittest.mock.patch("sys.stderr", io.StringIO()):
                cli.main(["check", "--dir", str(self.root), *args])
            return e.exception.code, buf.getvalue()
        code, out = run()
        self.assertEqual(code, 1)
        self.assertIn("form a dependency cycle", out)
        code, out = run("--format", "github")
        self.assertIn("::warning file=lib/b/x.ex,line=1,title=archplan structure.cycle::", out)
        code, out = run("--json")
        self.assertEqual(json.loads(out)["counts"]["new"], len(json.loads(out)["findings"]))
        self.assertEqual(run("--update-baseline")[0], 0)
        self.assertEqual(run()[0], 0)
        self.assertIn("[accepted: baseline]", run("--all")[1])
        self.p.model_path.unlink()
        self.assertEqual(run()[0], 2)

    def test_api_discuss(self):
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, "/api/findings")
            self.assertEqual(st, 200)
            self.assertEqual(d["counts"]["by_group"]["structure"], 2)
            self.assertIn("findings_etag", d)
            key = d["key"]
            f = next(x for x in d["findings"] if x["kind"] == "types.fields")
            fid = f["id"]
            st, r = call(url, f"/api/findings/{fid}/discuss", "POST", {})
            self.assertTrue(r["created"])
            th = r["thread"]
            self.assertEqual(th["anchor"], f"finding:{fid}")
            self.assertEqual(th["snapshot"]["evidence"]["groups"]["payout"][0], "payout_iban")
            st, r2 = call(url, f"/api/findings/{fid}/discuss", "POST", {"text": "again"})
            self.assertEqual((r2["created"], r2["thread"]["id"]), (False, th["id"]))
            self.assertEqual(r2["finding"]["thread"], th["id"])
            self.assertEqual(len(Store(self.p.dir).read()["threads"]), 1)
            self.assertEqual(call(url, f"/api/findings/{fid}/dismiss", "POST", {})[0], 400)
            st, r = call(url, f"/api/findings/{fid}/dismiss", "POST", {"reason": "legacy"})
            self.assertEqual((st, r["finding"]["state"]), (200, "dismissed"))
            self.assertNotEqual(r["key"], key)
            self.assertEqual(call(url, f"/api/findings/{fid}/reopen", "POST", {})[1]["finding"]["state"], "new")
            self.assertEqual(call(url, "/api/findings/nope/accept", "POST", {})[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()
        a = Agent(self.p)
        item = a.read_thread(th["id"])["item"]
        self.assertEqual(item["id"], fid)
        self.assertEqual(a.label_finding(fid, "worth splitting")["label"], "worth splitting")
        got = a.get_findings(group="types")
        self.assertEqual({x["kind"] for x in got["findings"]}, {"types.fields", "types.synonyms"})


VOCAB_TREE = {
    "lib/billing/invoice.py": """\
        class Invoice:
            def send_invoice(self, invoice_line, payment_term):
                # an invoice goes to the debtor
                return invoice_line.debtor_name
        def invoice_total(invoices): ...
        def overdue_invoices(debtor): ...
        """,
    "lib/billing/debtor.py": "def remind_debtor(debtor, invoice): return debtor.invoice_line\n",
    "lib/shop/cart.py": """\
        class Cart:
            def add_product(self, product, cart_line): ...
            def checkout(self, cart, voucher): return self.invoice
        def empty_cart(cart): ...
        def apply_voucher(cart, voucher_code): ...
        """,
    "lib/shop/product.py": "class Product:\n    def price(self, product_variant): return product_variant.price\n",
    "lib/users/user.py": "class User:\n    def login(self, password, user_session): ...\n    def avatar(self, user): ...\n",
    "lib/users/profile.py": "def user_profile(user, avatar_url, nickname): return nickname\n",
    "lib/search/index.py": "def rebuild_index(index, shard): return shard.tokens\n",
    "lib/mail/outbox.py": "def queue_mail(outbox, mail, recipient): return recipient\n",
}


def vocab_model():
    comps = {c: {"name": c, "path": f"lib/{c}", "match": [f"lib/{c}/**"]} for c in ("billing", "shop", "users", "search", "mail")}
    types = {"billing.Invoice": {"name": "Invoice", "component": "billing", "file": "lib/billing/invoice.py"},
             "shop.Cart": {"name": "Cart", "component": "shop", "file": "lib/shop/cart.py"}}
    files = {f: {"component": f.split("/")[1], "refs": []} for f in VOCAB_TREE}
    return {"format": "archmodel/1", "components": comps, "dependencies": [], "types": types, "files": files}


class VocabTest(TempProject):
    def setUp(self):
        super().setUp()
        write(self.root, VOCAB_TREE)
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(vocab_model()))

    def test_paths_stay_inside(self):
        from archplan.vocab import inside
        outside = Path(tempfile.mkdtemp())
        (outside / "secret.py").write_text("secret_token = 1\n")
        os.symlink(outside / "secret.py", self.root / "link.py")
        for rel in ("../secret.py", "/etc/passwd", str(outside / "secret.py"), "link.py"):
            self.assertIsNone(inside(self.root, rel), rel)
        self.assertIsNotNone(inside(self.root, "a/b.py"))

    def test_words(self):
        from archplan.vocab import fold, fold_map, split, term_key, words
        self.assertEqual(split("parseHTTPRequest_v2"), ["parse", "http", "request", "v", "2"])
        self.assertEqual(words("Systems.Assignment.CrewTasks"), ["system", "assignment", "crew", "task"])
        self.assertEqual([fold(w) for w in ("entities", "batches", "carts", "status", "address", "boxes", "bus")],
                         ["entity", "batch", "cart", "status", "address", "box", "bus"])
        self.assertEqual(term_key("CART_ITEMS"), term_key("cart item"))
        self.assertEqual(fold_map({"zaken", "adres", "carts", "cart"}),
                         {"zaken": "zaken", "adres": "adres", "carts": "cart", "cart": "cart"})

    def test_tfidf_and_ignore(self):
        from archplan.vocab import Vocab, clean_ignore
        v = Vocab(self.p)
        res = v.candidates("billing")
        terms = [t["term"] for t in res["components"]["billing"]["terms"]]
        # debtor only occurs in billing, so its idf beats invoice, which shop also uses
        self.assertEqual(terms[:2], ["invoice", "debtor"])
        self.assertIn("invoice line", terms)  # a bigram inside one identifier
        self.assertNotIn("self", terms)
        inv = res["components"]["billing"]["terms"][0]
        self.assertEqual(inv["others"], [{"component": "shop", "count": 1}])
        self.assertEqual(inv["owner_guess"], "billing")
        self.assertIn("billing.Invoice", inv["types"])
        self.assertTrue(inv["sites"][0].startswith("lib/billing/"))
        self.assertTrue(self.p.path("vocab-cache.json").exists())
        self.assertIn("vocab-cache.json", self.p.path(".gitignore").read_text())
        v.set_ignored("Debtors", "billing", True)
        v.set_ignored("voucher", None, True)
        self.assertNotIn("debtor", [t["term"] for t in v.candidates("billing")["components"]["billing"]["terms"]])
        self.assertNotIn("voucher", [t["term"] for t in v.candidates("shop")["components"]["shop"]["terms"]])
        flagged = v.candidates("billing", ignored=True)["components"]["billing"]
        self.assertEqual(next(t for t in flagged["terms"] if t["term"] == "debtor")["ignored"], "here")
        self.assertEqual(flagged["ignored"], ["debtor"])
        v.set_ignored("debtor", "billing", False)
        self.assertEqual(v.ignored(), {"ignore": ["voucher"], "ignore_in": {}})
        self.assertEqual(clean_ignore({"ignore": ["X" * 500, 3, {"a": 1}, None], "ignore_in": {"c": "nope", "d": ["Carts"]}}),
                         {"ignore": ["x" * 60], "ignore_in": {"d": ["cart"]}})
        self.assertEqual(clean_ignore("junk"), {"ignore": [], "ignore_in": {}})

    def test_glossary_enrichment(self):
        from archplan.vocab import enrich, parse_glossary
        g = parse_glossary("---\nowns:\n  Invoice: [bill]  # the main one\n  debtor:\n    - payer\nuses: []\n"
                           "avoid:\n  - cart\n---\n**Invoice**: what a debtor owes.\n")
        self.assertEqual(g["owns"], {"Invoice": ["bill"], "debtor": ["payer"]})
        self.assertEqual((g["avoid"], g["definitions"]["invoice"]), (["cart"], "what a debtor owes."))
        write(self.root, {"lib/billing/GLOSSARY.md": "---\nowns:\n  Invoice: []\nuses: []\navoid: [cart]\n---\n"
                                                     "**Invoice**: what a debtor owes.\n",
                          "lib/shop/GLOSSARY.md": "---\nowns:\n  Cart: []\n---\n"})
        self.assertTrue(enrich(self.p))
        v = json.loads(self.p.model_path.read_text())["vocabulary"]
        self.assertEqual(v["concepts"]["invoice"]["definition"], "what a debtor owes.")
        self.assertEqual(v["components"]["billing"]["avoid"], ["cart"])
        self.assertIn("debtor", [x["term"] for x in v["components"]["billing"]["suggested"]])
        rows = {(u["component"], u["concept"]): u for u in v["usage"]}
        self.assertEqual(rows[("shop", "invoice")]["idents"], 1)
        self.assertEqual(rows[("shop", "invoice")]["evidence"], ["lib/shop/cart.py:3"])
        m = json.loads(self.p.model_path.read_text())
        self.assertEqual([(x["component"], x["concept"], x["why"]) for x in model.leaks(m)], [])
        m["vocabulary"]["components"]["shop"] = {"avoid": ["invoice"]}
        self.assertEqual([(x["component"], x["concept"], x["why"]) for x in model.leaks(m)],
                         [("shop", "invoice", "avoid")])
        self.assertFalse(enrich(self.p))  # a model with a vocabulary is left alone

    def test_glossary_kinds(self):
        from archplan.vocab import enrich, parse_glossary
        g = parse_glossary("---\nowns:\n  Invoice: []\n  Stripe: []\n  Webhook: []\nvendor: [Stripe]\n"
                           "technical:\n  - Webhook\n---\n")
        self.assertEqual((g["vendor"], g["technical"]), (["Stripe"], ["Webhook"]))
        write(self.root, {"lib/billing/GLOSSARY.md": "---\nowns:\n  Invoice: []\n  Stripe: []\n  Webhook: []\n"
                                                     "vendor: [Stripe]\ntechnical: [Webhook, Ghost]\n---\n"})
        self.assertTrue(enrich(self.p))
        v = json.loads(self.p.model_path.read_text())["vocabulary"]
        self.assertEqual({k: v["concepts"][k]["kind"] for k in ("invoice", "stripe", "webhook")},
                         {"invoice": "domain", "stripe": "vendor", "webhook": "technical"})
        self.assertNotIn("ghost", v["concepts"])  # a kind for a concept the file does not own adds nothing

    def test_propose_glossary(self):
        a = Agent(self.p)
        draft = {"billing": {"concepts": [{"name": "Invoice", "terms": ["invoice", "bill"],
                                           "definition": "What a debtor owes.", "kind": "domain"},
                                          {"name": "Debtor"}]},
                 "shop": {"concepts": [{"name": "Cart"}], "uses": ["invoice"], "avoid": ["Debtor"]}}
        out = a.propose_glossary(draft, "first glossaries")
        sg = Suggestions(self.p.dir).get(out["suggestion"])
        self.assertEqual([o["op"] for o in sg["ops"]], ["add_concept", "add_concept", "add_concept", "set_glossary"])
        self.assertEqual(sg["ops"][0], {"op": "add_concept", "id": "invoice", "name": "Invoice", "owner": "billing",
                                        "terms": ["invoice", "bill"], "kind": "domain",
                                        "definition": "What a debtor owes."})
        self.assertEqual(sg["ops"][3], {"op": "set_glossary", "component": "shop", "uses": ["invoice"],
                                        "avoid": ["debtor"]})
        Suggestions(self.p.dir).decide(self.p, sg["id"], None)
        t = json.loads(self.p.target_path.read_text())["vocabulary"]
        self.assertEqual(t["concepts"]["debtor"]["terms"], ["debtor"])
        self.assertEqual(t["components"]["shop"], {"uses": ["invoice"], "avoid": ["debtor"]})
        for bad, msg in [({"nope": {"concepts": [{"name": "X"}]}}, "no component"),
                         ({"shop": {"concepts": [{"name": "Invoice"}]}}, "already owned by billing"),
                         ({"shop": {"concepts": [{"name": "A"}]}, "users": {"concepts": [{"name": "a"}]}}, "twice"),
                         ({"users": {"uses": ["ghost"]}}, "unknown concept"),
                         ({"billing": {"uses": ["Invoice"]}}, "owns itself")]:
            with self.assertRaises(ValueError) as e:
                a.propose_glossary(bad)
            self.assertIn(msg, str(e.exception))

    def test_api(self):
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, "/api/vocabulary/candidates?top=3")
            self.assertEqual((st, d["n_components"], set(d["components"])), (200, 5, {"billing", "shop", "users", "search", "mail"}))
            self.assertLessEqual(len(d["components"]["billing"]["terms"]), 3)
            key = d["key"]
            self.assertEqual(call(url, "/api/vocabulary/candidates?component=nope")[0], 404)
            st, d = call(url, "/api/vocabulary/ignore", "POST", {"term": "invoice"})
            self.assertEqual((st, d["ignore"]), (200, ["invoice"]))
            self.assertNotEqual(d["key"], key)
            self.assertEqual(call(url, "/api/vocabulary/ignore", "POST", {"term": ["x"]})[0], 400)
            st, d = call(url, "/api/vocabulary/unignore", "POST", {"term": "invoice"})
            self.assertEqual(d["ignore"], [])
            st, d = call(url, "/api/vocabulary/draft", "POST", {"components": ["billing"], "note": "start here"})
            self.assertEqual((st, d["thread"]["anchor"]), (200, "glossary"))
            self.assertIn("@[billing](comp:billing)", d["thread"]["messages"][0]["text"])
            self.assertEqual(call(url, "/api/vocabulary/draft", "POST", {"components": ["ghost"]})[0], 404)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(Agent(self.p).list_threads(waiting_on="agent")[0]["anchor"], "glossary")


class BriefTest(TempProject):
    """Refactor brief, proposals, verification, plan/discuss/reject and auto mode."""

    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        m = findings_model()
        m["policy"] = {"default": "allow", "rules": [{"from": "B", "to": "C", "effect": "deny"}]}
        self.p.model_path.write_text(json.dumps(m))
        project.write_json(self.p.path("hotspots.json"), {"result": {
            "head": "x", "since_days": 365, "components": {"A": {"commits": 40}, "B": {"commits": 30},
                                                           "C": {"commits": 2}, "D": {"commits": 1},
                                                           "E": {"commits": 1}},
            "pairs": []}})
        self.agent = Agent(self.p)

    def ids(self):
        from archplan.findings import Findings
        return {f["kind"] + ":" + "".join(f["components"]): f["id"] for f in Findings(self.p).raw()[0]}

    def test_grouping_and_ranking(self):
        from archplan.brief import brief
        b = brief(self.p)
        ops = {o["kind"] + ":" + "".join(o["components"]): o for o in b["opportunities"]}
        ids = self.ids()
        abc = ops["structure.cycle:ABC"]
        # the policy break on the cut edge B -> C joins the cycle it would cut
        self.assertEqual(set(abc["findings"]), {ids["structure.cycle:ABC"], ids["layers.policy:BC"]})
        self.assertEqual(abc["facts"]["cut"], "B → C")
        self.assertTrue(any("remove_dependency B → C" in m for m in abc["moves"]))
        self.assertIn("structure.cycle:DE", ops)
        self.assertIn("types.fields:A", ops)
        self.assertEqual(b["opportunities"][0]["id"], abc["id"])  # cycle + policy, most churn
        self.assertEqual(abc["churn"], {"A": 40, "B": 30, "C": 2})
        self.assertTrue(any(m.startswith("Split User by its field groups") for m in ops["types.fields:A"]["moves"]))
        self.assertEqual(brief(self.p)["opportunities"][0]["id"], abc["id"])  # stable id
        self.assertEqual({o["kind"] for o in brief(self.p, kinds=["types"])["opportunities"]},
                         {"types.fields", "types.synonyms"})
        self.assertEqual({tuple(o["components"]) for o in brief(self.p, focus=["D"])["opportunities"]}, {("D", "E")})
        self.assertLess(len(json.dumps(b)), 24000)

    def test_verify_cycle(self):
        from archplan.brief import brief, verify
        ids = self.ids()
        cyc, pol = ids["structure.cycle:ABC"], ids["layers.policy:BC"]
        out = self.agent.propose_refactor("Cut B -> C", "lib/b/x.ex:1 is the only reference.",
                                          [{"op": "remove_dependency", "from": "B", "to": "C"}],
                                          [cyc, pol, ids["types.fields:A"]], brief(self.p)["opportunities"][0]["id"],
                                          "S", "low")
        v = out["verified"]
        self.assertEqual(sorted(v["fixed"]), sorted([cyc, pol]))
        self.assertEqual((v["introduced"], v["claimed"], v["unverified"]), ([], [ids["types.fields:A"]], []))
        sg = Suggestions(self.p.dir).get(out["suggestion"])
        self.assertEqual((sg["kind"], sg["title"], sg["effort"], sg["risk"], sg["status"]),
                         ("refactor", "Cut B -> C", "S", "low", "pending"))
        # an opportunity with a pending proposal leaves the brief
        self.assertNotIn(cyc, [i for o in brief(self.p)["opportunities"] for i in o["findings"]])
        self.assertEqual(brief(self.p)["excluded_with_pending_proposal"], 2)  # the cycle and the large type
        # A move that makes a new cycle: E.Thing refers to B.Member, so moving Thing into C adds the
        # type edge C -> B next to B -> C.
        t = json.loads(self.p.model_path.read_text())
        t["types"]["E.Thing"] = {"name": "Thing", "component": "E", "kind": "struct",
                                 "fields": [{"name": "member", "ref": "B.Member"}]}
        self.p.target_path.write_text(json.dumps(t))
        v = verify(self.p, [{"op": "move_type", "type": "E.Thing", "to": "C"}], [])
        self.assertIn("B → C → B form a dependency cycle.", v["introduced"])
        for bad in ([], "x"):
            with self.assertRaises(ValueError):
                self.agent.propose_refactor("t", "r", bad)
        with self.assertRaises(ValueError):
            self.agent.propose_refactor("t", "r", [{"op": "remove_dependency", "from": "B", "to": "C"}], ["nope"])
        with self.assertRaises(ValueError):
            self.agent.propose_refactor("t", "r", [{"op": "remove_dependency", "from": "B", "to": "C"}], effort="XL")

    def test_plan_discuss_reject(self):
        from archplan.brief import brief
        ids = self.ids()
        mk = lambda title: self.agent.propose_refactor(
            title, "Because.", [{"op": "remove_dependency", "from": "B", "to": "C"}],
            [ids["structure.cycle:ABC"]])["suggestion"]
        a, b = mk("Cut B -> C"), mk("Cut B -> C again")
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, f"/api/suggestions/{a}/discuss", "POST", {})
            self.assertEqual((st, d["created"], d["thread"]["anchor"]), (200, True, f"suggestion:{a}"))
            tid = d["thread"]["id"]
            self.assertEqual(call(url, f"/api/suggestions/{a}/discuss", "POST", {})[1]["thread"]["id"], tid)
            self.assertEqual(Suggestions(self.p.dir).get(a)["status"], "pending")
            st, d = call(url, f"/api/suggestions/{a}/plan", "POST", {})
            self.assertEqual(st, 200)
            th = d["thread"]
            self.assertEqual((th["id"], th["status"], th["refactor"]["title"], th["refactor"]["summary"]),
                             (tid, "planned", "Cut B -> C", "Because."))
            self.assertEqual((d["suggestion"]["status"], d["suggestion"]["plan_thread"]), ("accepted", tid))
            target = json.loads(self.p.target_path.read_text())
            self.assertNotIn(("B", "C"), [(x["from"], x["to"]) for x in target["dependencies"]])
            self.assertEqual(call(url, f"/api/suggestions/{b}/reject", "POST", {})[0], 400)
            st, d = call(url, f"/api/suggestions/{b}/reject", "POST", {"reason": "B needs C for billing"})
            self.assertEqual((st, d["suggestion"]["status"], d["suggestion"]["reason"]),
                             (200, "rejected", "B needs C for billing"))
            # glossary and plain edits still reject without a reason
            g = self.agent.edit_target([{"op": "forbid", "from": "C", "to": "D"}])["suggestion"]
            self.assertEqual(call(url, f"/api/suggestions/{g}/reject", "POST", {})[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()
        rej = brief(self.p)["rejected_before"]
        self.assertEqual(rej, [{"title": "Cut B -> C again", "reason": "B needs C for billing",
                                "fixes": [ids["structure.cycle:ABC"]]}])
        # a planned proposal still keeps its opportunity out of the brief
        self.assertNotIn(ids["structure.cycle:ABC"], [i for o in brief(self.p)["opportunities"] for i in o["findings"]])
        self.assertEqual(model.get_item(self.p, f"suggestion:{b}")["reason"], "B needs C for billing")

    def test_auto_mode(self):
        from archplan import brief
        store = Store(self.p.dir)
        t0 = 1_000_000.0
        self.assertIsNone(brief.auto_tick(self.p, store, t0))  # off: only records the state
        self.assertEqual(brief.set_auto(self.p, True, 2), {"auto": True, "count": 2})
        self.assertEqual(json.loads(self.p.path("config.json").read_text())["suggest"], {"auto": True, "count": 2})
        th = brief.auto_tick(self.p, store, t0, enabled=True)
        self.assertEqual((th["anchor"], th["messages"][0]["author"]), ("suggest", "archplan"))
        self.assertIn("up to 2 refactors", th["messages"][0]["text"])
        self.assertEqual(Agent(self.p).list_threads(waiting_on="agent")[0]["id"], th["id"])
        # one open request at a time, even much later
        self.assertIsNone(brief.auto_tick(self.p, store, t0 + 10_000, enabled=True))
        self.assertEqual(brief.status(self.p, store)["open_request"], th["id"])
        store.reply(th["id"], "Proposed two.", "agent")
        self.assertIsNone(brief.status(self.p, store)["open_request"])
        # a model change with new findings, within the hour: rate limited
        m = json.loads(self.p.model_path.read_text())
        m["dependencies"].append({"from": "F", "to": "G", "weight": 1})
        m["dependencies"].append({"from": "G", "to": "F", "weight": 1})
        self.p.model_path.write_text(json.dumps(m))
        self.assertIsNone(brief.auto_tick(self.p, store, t0 + 60))
        # the findings were seen; a later tick has no new reason
        self.assertIsNone(brief.auto_tick(self.p, store, t0 + 7200))
        # pending refactors dropping to zero is a reason
        ids = self.ids()
        sid = self.agent.propose_refactor("Cut", "Why.", [{"op": "remove_dependency", "from": "G", "to": "F"}],
                                          [ids["structure.cycle:FG"]])["suggestion"]
        self.assertIsNone(brief.auto_tick(self.p, store, t0 + 7300))
        Suggestions(self.p.dir).decide(self.p, sid, [], "no")
        th2 = brief.auto_tick(self.p, store, t0 + 7400)
        self.assertIn("No refactor suggestion is pending", th2["messages"][0]["text"])
        # a new model with new findings opens one too, once the hour has passed
        store.reply(th2["id"], "Done.", "agent")
        m["dependencies"].append({"from": "H", "to": "I", "weight": 1})
        m["dependencies"].append({"from": "I", "to": "H", "weight": 1})
        self.p.model_path.write_text(json.dumps(m))
        self.assertIsNone(brief.auto_tick(self.p, store, t0 + 7500))
        m["dependencies"].append({"from": "I", "to": "J", "weight": 1})
        m["dependencies"].append({"from": "J", "to": "I", "weight": 1})
        self.p.model_path.write_text(json.dumps(m))
        th3 = brief.auto_tick(self.p, store, t0 + 7400 + 3601)
        self.assertIn("new finding", th3["messages"][0]["text"])
        self.assertIn("suggest-state.json", self.p.path(".gitignore").read_text())
        brief.set_auto(self.p, False)
        store.reply(th3["id"], "Done.", "agent")
        m["dependencies"].append({"from": "D", "to": "B", "weight": 2})
        self.assertIsNone(brief.auto_tick(self.p, store, t0 + 20000, enabled=True))

    def test_api(self):
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, "/api/suggest")
            self.assertEqual((st, d["auto"], d["count"], d["open_request"]), (200, False, 3, None))
            key = d["key"]
            st, d = call(url, "/api/suggest", "POST", {"count": 2, "focus": ["A"], "note": "Cycles first."})
            self.assertEqual((st, d["thread"]["anchor"], d["thread"]["messages"][0]["author"]), (200, "suggest", "human"))
            self.assertIn("@[A](comp:A)", d["thread"]["messages"][0]["text"])
            self.assertEqual(call(url, "/api/suggest")[1]["open_request"], d["thread"]["id"])
            self.assertEqual(call(url, "/api/suggest", "POST", {"focus": ["nope"]})[0], 404)
            self.assertEqual(call(url, "/api/suggest", "POST", {"count": 99})[0], 400)
            st, d = call(url, "/api/suggest/auto", "PUT", {"on": True, "count": 4})
            self.assertEqual((st, d["auto"], d["count"]), (200, True, 4))
            self.assertNotEqual(d["key"], key)
            self.assertEqual(call(url, "/api/suggest/auto", "PUT", {"on": "yes"})[0], 400)
            st, d = call(url, "/api/brief?top=2&kinds=structure")
            self.assertEqual((st, d["shown"], {o["kind"] for o in d["opportunities"]}), (200, 2, {"structure.cycle"}))
        finally:
            srv.shutdown()
            srv.server_close()


def tangle_model(extra=()):
    """A hub H in 2-cycles with M1..M5 (plus M1 -> M2), and extra members."""
    names = ["H", "M1", "M2", "M3", "M4", "M5", *extra]
    comps = {c: {"name": c, "path": f"lib/{c.lower()}"} for c in names}
    deps = [("H", c, 3) for c in names[1:]] + [(c, "H", 1 + i) for i, c in enumerate(names[1:])] + [("M1", "M2", 2)]
    return {"format": "archmodel/1", "components": comps, "types": {},
            "dependencies": [{"from": a, "to": b, "weight": w, "evidence": [f"lib/{a.lower()}/x.ex:{w}"]}
                             for a, b, w in deps]}


class TangleTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(tangle_model()))

    def tangle(self, fs):
        (t,) = [f for f in fs if f["kind"] == "structure.tangle"]
        self.assertEqual([f for f in fs if f["kind"] == "structure.cycle"], [])
        return t

    def test_one_finding_per_tangle(self):
        from archplan.findings import cycle_findings, sccs
        t = self.tangle(cycle_findings(tangle_model()))
        e = t["evidence"]
        self.assertEqual(t["title"], "H and 5 other components form one dependency tangle.")
        self.assertEqual((e["size"], e["hub"], e["members"], t["strength"]), (6, "H", ["H", "M1", "M2", "M3", "M4", "M5"],
                                                                              "strong"))
        self.assertEqual((e["cycles"], len(e["two_cycles"]), e["cut_count"]), (6, 5, 5))
        self.assertEqual(e["cut_weight"], sum(c["weight"] for c in e["cuts"]))
        self.assertEqual(e["cuts"][0], {"from": "M1", "to": "H", "weight": 1, "evidence": ["lib/m1/x.ex:1"]})
        self.assertIn("Cutting 5 dependencies (12 references) breaks every cycle", t["detail"])
        # an SCC of three stays cycles
        small = {**tangle_model(), "dependencies": [d for d in tangle_model()["dependencies"]
                                                    if {d["from"], d["to"]} <= {"H", "M1", "M2"}]}
        self.assertEqual({f["kind"] for f in cycle_findings(small)}, {"structure.cycle"})

    def test_cuts_break_all_cycles(self):
        import random
        from archplan.findings import feedback_edges, sccs
        for seed in range(30):
            rnd = random.Random(seed)
            n = rnd.randint(4, 25)
            nodes = [f"c{i}" for i in range(n)]
            w = {(a, b): rnd.randint(1, 9) for a in nodes for b in nodes if a != b and rnd.random() < 0.25}
            cuts = feedback_edges(nodes, w)
            rest = {e: x for e, x in w.items() if e not in set(cuts)}
            self.assertTrue(all(len(c) == 1 for c in sccs(nodes, rest)), seed)
            for e in cuts:  # minimal: every cut is needed
                back = {**rest, e: w[e]}
                self.assertTrue(any(len(c) > 1 for c in sccs(nodes, back)), seed)
            self.assertEqual(cuts, sorted(cuts, key=lambda e: (w[e], e)))

    def test_id_stable_and_baseline_growth(self):
        from archplan.findings import Findings
        F = Findings(self.p)
        t = self.tangle(F.listing()["findings"])
        F.decide(t["id"], "accepted")
        self.assertEqual(json.loads(self.p.path("baseline.json").read_text())["findings"][t["id"]]["size"], 6)
        self.p.model_path.write_text(json.dumps(tangle_model(["X"])))
        os.utime(self.p.model_path, (5, 5))
        t2 = self.tangle(Findings(self.p).listing()["findings"])
        self.assertEqual((t2["id"], t2["state"], t2["grew"]), (t["id"], "new", {"from": 6, "to": 7}))
        self.assertTrue(t2["detail"].startswith("Grew from 6 to 7 components. "))
        # back to six members (one swapped): not grown, still accepted
        m = tangle_model(["X"])
        m["dependencies"] = [d for d in m["dependencies"] if d["from"] != "M5" and d["to"] != "M5"]
        self.p.model_path.write_text(json.dumps(m))
        os.utime(self.p.model_path, (6, 6))
        self.assertEqual(self.tangle(Findings(self.p).listing()["findings"])["state"], "accepted")
        # update_baseline takes the larger size
        self.p.model_path.write_text(json.dumps(tangle_model(["X"])))
        os.utime(self.p.model_path, (7, 7))
        self.assertEqual(Findings(self.p).update_baseline()["added"], 1)
        self.assertEqual(self.tangle(Findings(self.p).listing()["findings"])["state"], "accepted")

    def test_verify_shrunk(self):
        from archplan.brief import brief, verify
        tid = self.tangle(Findings_(self.p))["id"]
        v = verify(self.p, [{"op": "remove_dependency", "from": "M5", "to": "H"}], [tid])
        self.assertEqual((v["shrunk"], v["fixed"], v["unverified"]), ([{"id": tid, "from": 6, "to": 5}], [], []))
        ops = [{"op": "remove_dependency", "from": f"M{i}", "to": "H"} for i in range(1, 6)]
        self.assertEqual(verify(self.p, ops, [tid])["fixed"], [tid])
        o = brief(self.p)["opportunities"][0]
        self.assertEqual((o["kind"], o["facts"]["hub"], o["facts"]["breaks_all"]), ("structure.tangle", "H",
                                                                                  "5 cuts, 12 references"))
        self.assertTrue(o["moves"][0].startswith("Cut M1 → H, 1 reference"))


def Findings_(p):
    from archplan.findings import Findings
    return Findings(p).listing()["findings"]


LITE = {'java': {'pom.xml': '<project><artifactId>spring-boot-starter-web</artifactId></project>',
          'src/main/java/com/acme/shop/core/Order.java': 'package com.acme.shop.core;\n'
                                                         '\n'
                                                         'import java.util.List;\n'
                                                         '\n'
                                                         '/** An order. */\n'
                                                         'public class Order extends Base implements Payable {\n'
                                                         '    private final List<Line> lines;\n'
                                                         '    private Customer customer;\n'
                                                         '    private String note = "x; y";\n'
                                                         '    public Order() {}\n'
                                                         '}\n',
          'src/main/java/com/acme/shop/core/Line.java': 'package com.acme.shop.core;\n'
                                                        'public record Line(String sku, int qty) {}\n',
          'src/main/java/com/acme/shop/core/Base.java': 'package com.acme.shop.core;\n'
                                                        'public abstract class Base {}\n',
          'src/main/java/com/acme/shop/core/Payable.java': 'package com.acme.shop.core;\n'
                                                           'public interface Payable { void pay(); }\n',
          'src/main/java/com/acme/shop/core/Customer.java': 'package com.acme.shop.core;\npublic class Customer {}\n',
          'src/main/java/com/acme/shop/web/OrderController.java': 'package com.acme.shop.web;\n'
                                                                  '\n'
                                                                  'import com.acme.shop.core.Order;\n'
                                                                  'import com.acme.shop.core.*;\n'
                                                                  '\n'
                                                                  '// Customer is mentioned in a comment only\n'
                                                                  'public class OrderController {\n'
                                                                  '    private Order order;\n'
                                                                  '    String s = "Line";\n'
                                                                  '    Payable p;\n'
                                                                  '}\n',
          'src/test/java/com/acme/shop/core/OrderTest.java': 'package com.acme.shop.core;\n'
                                                             'class OrderTest { Order o; }\n'},
 'kotlin': {'src/main/kotlin/a/model/User.kt': 'package a.model\n'
                                               '\n'
                                               'data class User(val id: Long, val address: Address)\n',
            'src/main/kotlin/a/model/Address.kt': 'package a.model\n\nclass Address(val street: String)\n',
            'src/main/kotlin/a/api/UserApi.kt': 'package a.api\n'
                                                '\n'
                                                'import a.model.User\n'
                                                '\n'
                                                'class UserApi {\n'
                                                '    fun get(): User? = null\n'
                                                '}\n'},
 'csharp': {'Shop.Domain/Order.cs': 'namespace Shop.Domain;\n'
                                    '\n'
                                    'public class Order : Entity\n'
                                    '{\n'
                                    '    public Customer Buyer { get; set; }\n'
                                    '    public List<OrderLine> Lines { get; } = new();\n'
                                    '}\n',
            'Shop.Domain/Entity.cs': 'namespace Shop.Domain;\n'
                                     'public abstract class Entity { }\n'
                                     'public record OrderLine(string Sku);\n',
            'Shop.Domain/Customer.cs': 'namespace Shop.Domain\n{\n    public class Customer { }\n}\n',
            'Shop.Web/Controllers/OrdersController.cs': 'using Shop.Domain;\n'
                                                        'using System;\n'
                                                        '\n'
                                                        'namespace Shop.Web.Controllers;\n'
                                                        '\n'
                                                        'public class OrdersController : ControllerBase\n'
                                                        '{\n'
                                                        '    private readonly Order _order;\n'
                                                        '}\n',
            'Shop.Web/Shop.Web.csproj': '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>'},
 'php': {'composer.json': '{"require": {"laravel/framework": "^11"}, "autoload": {"psr-4": {"App\\\\": "app/"}}}',
         'app/Models/User.php': '<?php\n'
                                'namespace App\\Models;\n'
                                '\n'
                                'class User extends Model\n'
                                '{\n'
                                '    private ?Team $team;\n'
                                '}\n',
         'app/Models/Team.php': '<?php\nnamespace App\\Models;\nclass Team {}\n',
         'app/Http/Controllers/UserController.php': '<?php\n'
                                                    'namespace App\\Http\\Controllers;\n'
                                                    '\n'
                                                    'use App\\Models\\User;\n'
                                                    'use App\\Models\\{Team};\n'
                                                    '\n'
                                                    'class UserController extends Controller\n'
                                                    '{\n'
                                                    '    public function show(User $u) {}\n'
                                                    '}\n'},
 'ruby': {'Gemfile': "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\n",
          'config/routes.rb': 'Rails.application.routes.draw do\nend\n',
          'app/models/user.rb': 'class User < ApplicationRecord\n'
                                '  has_many :posts\n'
                                '  def admin? = Admin::Role.exists?\n'
                                'end\n',
          'app/models/admin/role.rb': 'module Admin\n  class Role < ApplicationRecord\n  end\nend\n',
          'app/models/application_record.rb': 'class ApplicationRecord < ActiveRecord::Base\nend\n',
          'app/controllers/users_controller.rb': "require_relative '../../lib/tools/clock'\n"
                                                 '\n'
                                                 'class UsersController < ApplicationController\n'
                                                 '  def show\n'
                                                 '    # User in a comment\n'
                                                 '    @user = User.find(params[:id])\n'
                                                 '  end\n'
                                                 'end\n',
          'lib/tools/clock.rb': 'module Tools\n  class Clock\n  end\nend\n'},
 'rust': {'Cargo.toml': '[package]\nname = "geo-kit"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n',
          'src/lib.rs': 'pub mod shapes;\nmod util;\n\nuse crate::shapes::{circle::Circle, Square as Sq};\n',
          'src/shapes/mod.rs': 'pub mod circle;\npub struct Square { pub side: f64 }\n',
          'src/shapes/circle.rs': 'use super::Square;\n'
                                  'use crate::util::round;\n'
                                  '\n'
                                  'pub struct Circle {\n'
                                  '    pub r: f64,\n'
                                  '    pub bound: Square,\n'
                                  '}\n',
          'src/util.rs': 'pub fn round(x: f64) -> f64 { x }\n'},
 'swift': {'Package.swift': '// swift-tools-version:5.9\n',
           'Sources/Kit/Model.swift': 'public struct Note {\n    let id: Int\n    var tags: [Tag]\n}\n',
           'Sources/Kit/Tag.swift': 'public struct Tag { let name: String }\n',
           'Sources/App/Main.swift': 'import Kit\nlet n: Note? = nil\n'},
 'scala': {'build.sbt': 'name := "x"\n',
           'src/main/scala/a/core/Item.scala': 'package a.core\n'
                                               '\n'
                                               'case class Item(name: String, price: Price)\n'
                                               'case class Price(v: Int)\n',
           'src/main/scala/a/app/Main.scala': 'package a.app\n'
                                              '\n'
                                              'import a.core.{Item, Price}\n'
                                              '\n'
                                              'object Main { val i: Item = null }\n'}}


def lite_model(tmp: str, lang: str, **kw) -> dict:
    from archplan.liteexport import export as lite
    root = Path(tmp) / lang
    write(root, LITE[lang])
    return lite(root, **kw)


def refs_of(m: dict) -> dict:
    return {f: [r["to"] for r in v["refs"]] for f, v in m["files"].items()}


class LiteExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def model(self, lang):
        from archplan import modelcheck
        m = lite_model(self.tmp.name, lang)
        res = modelcheck.check(m, Path(self.tmp.name) / lang)
        self.assertTrue(res["ok"], res["errors"])
        self.assertLessEqual(set(res["capabilities"]["found"]), set(res["capabilities"]["declared"]))
        return m, refs_of(m)

    def test_java(self):
        m, refs = self.model("java")
        web, core = "src/main/java/com/acme/shop/web/", "src/main/java/com/acme/shop/core/"
        # the import and the wildcard import; not the comment, not the string
        self.assertEqual(refs[web + "OrderController.java"], [core + "Order.java", core + "Payable.java"])
        self.assertNotIn("src/test/java/com/acme/shop/core/OrderTest.java", m["files"])
        self.assertEqual(set(m["components"]), {"com.acme.shop.core", "com.acme.shop.web"})
        self.assertEqual(m["dependencies"], [{"from": "com.acme.shop.web", "to": "com.acme.shop.core", "weight": 1,
                                             "evidence": [web + "OrderController.java:3"]}])
        order = m["types"]["com.acme.shop.core.Order"]
        self.assertEqual(order["implements"], ["com.acme.shop.core.Base", "com.acme.shop.core.Payable"])
        self.assertEqual(order["fields"][:2], [{"name": "lines", "ref": "com.acme.shop.core.Line", "many": True},
                                               {"name": "customer", "ref": "com.acme.shop.core.Customer"}])
        self.assertEqual(m["types"]["com.acme.shop.core.Line"]["kind"], "record")
        self.assertEqual([f["name"] for f in m["types"]["com.acme.shop.core.Line"]["fields"]], ["sku", "qty"])

    def test_kotlin_scala_csharp(self):
        m, refs = self.model("kotlin")
        self.assertEqual(refs["src/main/kotlin/a/api/UserApi.kt"], ["src/main/kotlin/a/model/User.kt"])
        self.assertEqual(m["types"]["a.model.User"]["fields"][1], {"name": "address", "ref": "a.model.Address"})
        m, refs = self.model("scala")
        self.assertEqual(refs["src/main/scala/a/app/Main.scala"], ["src/main/scala/a/core/Item.scala"])
        m, refs = self.model("csharp")
        self.assertEqual(refs["Shop.Web/Controllers/OrdersController.cs"], ["Shop.Domain/Order.cs"])
        order = m["types"]["Shop.Domain.Order"]
        self.assertEqual(order["implements"], ["Shop.Domain.Entity"])
        self.assertEqual(order["fields"][1], {"name": "Lines", "ref": "Shop.Domain.OrderLine", "many": True})

    def test_php_psr4(self):
        m, refs = self.model("php")
        self.assertEqual(refs["app/Http/Controllers/UserController.php"], ["app/Models/User.php", "app/Models/Team.php"])
        self.assertEqual(m["types"]["App.Models.User"]["fields"], [{"name": "team", "ref": "App.Models.Team"}])

    def test_ruby_zeitwerk_and_require(self):
        m, refs = self.model("ruby")
        self.assertEqual(refs["app/controllers/users_controller.rb"], ["lib/tools/clock.rb", "app/models/user.rb"])
        self.assertEqual(refs["app/models/user.rb"], ["app/models/application_record.rb", "app/models/admin/role.rb"])

    def test_rust_modules(self):
        m, refs = self.model("rust")
        self.assertEqual(refs["src/lib.rs"], ["src/shapes/mod.rs", "src/util.rs", "src/shapes/circle.rs"])
        self.assertEqual(refs["src/shapes/circle.rs"], ["src/shapes/mod.rs", "src/util.rs"])
        self.assertEqual(m["types"]["src/shapes.Circle"]["fields"][1], {"name": "bound", "ref": "src/shapes.Square"})

    def test_swift_targets(self):
        m, refs = self.model("swift")
        self.assertEqual(refs["Sources/App/Main.swift"], ["Sources/Kit/Model.swift"])
        self.assertNotIn("Package.swift", m["files"])

    def test_subfolder_keeps_repository_paths(self):
        from archplan.liteexport import export as lite
        root = Path(self.tmp.name) / "repo"
        write(root / "backend", LITE["php"])
        m = lite(root / "backend", rel_to=root)
        self.assertEqual(refs_of(m)["backend/app/Http/Controllers/UserController.php"],
                         ["backend/app/Models/User.php", "backend/app/Models/Team.php"])


class FrameworkProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builtins_load_and_validate(self):
        from archplan import frameworks as F
        ts, errors = F.load()
        self.assertEqual(errors, [])
        self.assertTrue({"django", "fastapi", "flask", "phoenix", "rails", "laravel", "spring", "aspnetcore",
                         "go-standard", "go-hexagonal", "react", "nextjs", "angular", "vue", "nestjs",
                         "express"} <= set(ts))

    def test_priority_and_manifests(self):
        from archplan import frameworks as F
        write(self.root, {"package.json": '{"dependencies": {"next": "14", "react": "18"}}',
                          "src/app/layout.tsx": "", "src/app/page.tsx": "", "src/components/Nav.tsx": ""})
        files = ["src/app/layout.tsx", "src/app/page.tsx", "src/components/Nav.tsx"]
        m = {"components": {}, "files": {f: {"refs": []} for f in files}}
        got = [t["name"] for t, _, _ in F.matches(m, files, self.root)]
        self.assertEqual(got[:2], ["nextjs", "react"])
        self.assertEqual(F.matches(m, files, None), [])  # no manifest, no dependency: no match

    def test_project_override_and_untrusted_files(self):
        from archplan import frameworks as F
        pd = self.root / ".archplan"
        mine = {"name": "react", "title": "Our React", "detect": {"priority": 99, "deps": ["react"]},
                "units": [{"kind": "App", "glob": "web"}], "roles": [{"id": "ui", "patterns": ["*.tsx"]}], "rules": []}
        write(pd / "profiles", {"react.json": json.dumps(mine),
                                "bad-color.json": json.dumps(dict(mine, name="bad", roles=[
                                    {"id": "ui", "patterns": ["*.tsx"], "color": "red\" onload=x"}])),
                                "bad-pattern.json": json.dumps(dict(mine, name="bad2", roles=[
                                    {"id": "ui", "patterns": [{"x": 1}]}])),
                                "bad-rule.json": json.dumps(dict(mine, name="bad3", rules=[
                                    {"id": "r", "from": "ui", "to": "nope"}])),
                                "huge.json": "{" + " " * (300 * 1024) + "}"})
        ts, errors = F.load(pd)
        self.assertEqual((ts["react"]["title"], ts["react"]["origin"]), ("Our React", "project"))
        self.assertEqual(ts["vue"]["origin"], "built-in")
        self.assertEqual(sorted(Path(f).name for f, _ in errors),
                         ["bad-color.json", "bad-pattern.json", "bad-rule.json", "huge.json"])

    def test_generic_wins_when_it_fits_much_better(self):
        from archplan import architecture as A
        m = synthetic_model()
        m["files"]["lib/shop_web/router.ex"] = {"refs": []}
        write(self.root, {"mix.exs": "defp deps, do: [{:phoenix, \"~> 1.7\"}]\n", "lib/shop_web/router.ex": ""})
        self.assertEqual(A.infer(m, self.root)["source"], "inferred")
        self.assertEqual(A.use(m, "phoenix", self.root)["source"], "phoenix")
        with self.assertRaises(A.ArchitectureError):
            A.use(m, "nope", self.root)

    def test_vue_profile(self):
        from archplan import architecture as A
        write(self.root, {"package.json": '{"dependencies": {"vue": "3"}}'})
        refs = {"src/utils/format.ts": ["src/components/Badge.vue"],  # a break
                "src/views/Home.vue": ["src/components/Badge.vue", "src/composables/useCart.ts"],
                "src/components/Badge.vue": ["src/utils/format.ts"],
                "src/components/__tests__/Badge.test.ts": ["src/views/Home.vue"]}  # tests are left out
        files = [*refs, "src/main.ts", "src/stores/cart.ts", "src/types/index.ts", "src/composables/useCart.ts"]
        m = {"components": {}, "types": {}, "files": {f: {"refs": [{"to": t} for t in refs.get(f, [])]} for f in files}}
        p = A.infer(m, self.root)
        self.assertEqual(p["source"], "vue")
        a = A.analyse(p, m)
        roles = {f["path"]: f["role"] for u in a["units"] for f in u["files"]}
        self.assertEqual(roles, {"src/utils/format.ts": "utils", "src/views/Home.vue": "views",
                                 "src/components/Badge.vue": "components", "src/main.ts": "entry",
                                 "src/stores/cart.ts": "stores", "src/types/index.ts": "types",
                                 "src/composables/useCart.ts": "composables"})
        self.assertEqual({r["id"]: r["count"] for r in a["rules"]},
                         {"utils-services-no-ui": 1, "stores-no-ui": 0, "types-stay-low": 0, "components-not-views": 0})


class ExporterRegistryTest(TempProject):
    def entry(self, name, prio, **kw):
        e = {"name": name, "languages": ["java"], "priority": prio, "detect": {"files": ["pom.xml"]},
             "command": f"{name}-export -o {{out}}", "capabilities": ["components"]}
        e.update(kw)
        return json.dumps(e)

    def test_lite_for_jvm_and_project_entries(self):
        from archplan import exporters
        root = Path(self.tmp.name) / "jvm"
        write(root, LITE["java"])
        res = exporters.survey(root, root / ".archplan")
        self.assertEqual((res["pick"], res["command"], res["trusted_source"]),
                         ("lite", "archplan export-lite . -o .archplan/model.archmodel.json", True))
        write(root / ".archplan" / "exporters", {
            "missing.json": self.entry("missing", 90, check={"programs": ["archplan-no-such-program"]}),
            "runs.json": self.entry("runs", 80, check="touch pwned"),
            "bad.json": self.entry("bad", 70, command="no output placeholder")})
        res = exporters.survey(root, root / ".archplan")
        rows = {r["name"]: r for r in res["exporters"]}
        self.assertEqual((rows["missing"]["installed"], rows["runs"]["installed"]), (False, None))
        self.assertFalse((root / "pwned").exists())  # a project entry's check command never runs
        self.assertEqual((res["pick"], res["trusted_source"]), ("runs", False))
        self.assertEqual([Path(e["file"]).name for e in res["errors"]], ["bad.json"])
        # init does not approve a command that came from the repository
        p = project.init(root)
        self.assertEqual(p.export_command(), "runs-export -o .archplan/model.archmodel.json")
        self.assertFalse(p.trusted())

    def test_builtin_init_is_trusted(self):
        p = project.init(self.root)
        self.assertEqual(p.export_command(), "archplan export-python -o .archplan/model.archmodel.json")
        self.assertTrue(p.trusted())

    def test_export_check(self):
        from archplan import modelcheck
        m = export(self.root)
        res = modelcheck.check(m, self.root)
        self.assertTrue(res["ok"], res["errors"])
        self.assertLessEqual(set(res["capabilities"]["found"]), set(res["capabilities"]["declared"]))
        bad = json.loads(json.dumps(m))
        bad["dependencies"].append({"from": "shop", "to": "nowhere", "weight": 1, "evidence": ["../etc/passwd:1"]})
        bad["files"]["/abs/x.py"] = {"refs": []}
        bad["source"]["capabilities"].append("magic")
        res = modelcheck.check(bad)
        self.assertFalse(res["ok"])
        text = "\n".join(res["errors"])
        for want in ("unknown component 'nowhere'", "'../etc/passwd' is leaves the root", "'/abs/x.py' is absolute",
                     "unknown capabilities: magic"):
            self.assertIn(want, text)
        f = Path(self.tmp.name) / "bad.json"
        f.write_text(json.dumps(bad))
        out = subprocess.run([sys.executable, "-m", "archplan", "export-check", str(f)], capture_output=True,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(out.returncode, 1)

    def test_findings_without_calls_or_methods(self):
        from archplan.findings import Findings
        from archplan.liteexport import export as lite
        root = Path(self.tmp.name) / "jvm"
        write(root, LITE["java"])
        p = project.init(root, export="true")
        p.model_path.write_text(json.dumps(lite(root)))
        res = Findings(p).listing()
        self.assertIsInstance(res["findings"], list)


# ---------- detectors ----------

def rich_model():
    """findings_model plus a tangle (F, G, H, I), a forbidden dependency (J -> A)
    and a leaked term (B uses A's payout), for the ported detectors."""
    m = findings_model()
    for a, b, w in [("F", "G", 2), ("G", "H", 1), ("H", "I", 3), ("I", "F", 1), ("G", "F", 2), ("J", "A", 1)]:
        m["dependencies"].append({"from": a, "to": b, "weight": w, "evidence": [f"lib/{a.lower()}/y.ex:{w}"]})
    m["policy"] = {"default": "allow", "rules": [{"from": "J", "to": "A", "effect": "deny"}]}
    m["vocabulary"] = {"concepts": {"payout": {"name": "Payout", "owner": "A", "terms": ["payout"]}},
                       "components": {"B": {"avoid": ["payout"]}},
                       "usage": [{"component": "B", "concept": "payout", "idents": 2, "evidence": ["lib/b/x.ex:1"]}]}
    return m


PORTED_IDS = [
    ('history.coupling', 'history.coupling-e1753921f02a'),
    ('layers.policy', 'layers.policy-4d84c8f032f0'),
    ('structure.cycle', 'structure.cycle-02505cd3c1c0'),
    ('structure.cycle', 'structure.cycle-ab1a5fabc25f'),
    ('structure.tangle', 'structure.tangle-dd508edff90b'),
    ('types.fields', 'types.fields-f313818d434b'),
    ('types.synonyms', 'types.synonyms-a8a6462a07ae'),
    ('vocabulary.leak', 'vocabulary.leak-a01d5aac216d'),
]  # from findings.py before the detectors; baselines depend on them


def concept_model():
    """Small fixtures for the concept, boundary and evolution detectors."""
    comps = {c: {"name": c, "path": f"lib/{c}", "match": [f"lib/{c}/**"]} for c in ("orders", "billing", "shipping", "core")}
    f = lambda n, t="string", **kw: {"name": n, "type": t, **kw}
    types = {
        "orders.Order": {"name": "Order", "component": "orders", "kind": "struct", "file": "lib/orders/order.ex",
                         "fields": [f("is_paid", "boolean"), f("is_shipped", "boolean"), f("cancelled", "boolean"),
                                    f("cancelled_at", "utc_datetime"), f("invoice_id", "integer"),
                                    f("street"), f("city"), f("zip_code"), f("timeout_ms", "integer"),
                                    f("amount", "integer"), f("meta", "map")]},
        "billing.Invoice": {"name": "Invoice", "component": "billing", "kind": "struct", "file": "lib/billing/invoice.ex",
                            "fields": [f("street"), f("city"), f("zip_code"), f("total"), f("amount_cents", "integer"),
                                       f("order", None, ref="orders.Order")]},
        "shipping.Parcel": {"name": "Parcel", "component": "shipping", "kind": "struct", "file": "lib/shipping/parcel.ex",
                            "fields": [f("street"), f("city"), f("zip_code"), f("weight"), f("label"),
                                       f("timeout_seconds", "integer")], "uses": ["orders.Order"]},
        "core.Repo": {"name": "Repo", "component": "core", "kind": "module", "file": "lib/core/repo.ex"},
        "core.Cache": {"name": "Cache", "component": "core", "kind": "module", "file": "lib/core/cache.ex"},
        "core.Clock": {"name": "Clock", "component": "core", "kind": "module", "file": "lib/core/clock.ex"},
        "core.Util": {"name": "Util", "component": "core", "kind": "module", "file": "lib/core/util.ex"},
    }
    deps = [("orders", "core"), ("billing", "core"), ("shipping", "core"), ("billing", "orders"),
            ("shipping", "orders")]
    files = {"lib/billing/invoice.ex": {"component": "billing", "refs": [{"to": "lib/core/repo.ex"},
                                                                          {"to": "lib/orders/order.ex"}]},
             "lib/shipping/parcel.ex": {"component": "shipping", "refs": [{"to": "lib/core/repo.ex"},
                                                                          {"to": "lib/orders/order.ex"}]},
             "lib/orders/order.ex": {"component": "orders", "refs": [{"to": "lib/core/repo.ex"}]},
             "lib/core/repo.ex": {"component": "core", "refs": [{"to": "lib/core/cache.ex"}, {"to": "lib/core/clock.ex"},
                                                                 {"to": "lib/core/util.ex"}]},
             "lib/core/cache.ex": {"component": "core", "refs": []}, "lib/core/clock.ex": {"component": "core", "refs": []},
             "lib/core/util.ex": {"component": "core", "refs": []}}
    sig = lambda i, c, *ps: {"id": i, "component": c, "file": f"lib/{c}/x.ex", "line": 1,
                             "params": [{"name": n, "type": t} for n, t in ps], "returns": "ok"}
    signatures = [sig("orders.place/3", "orders", ("order", "Order"), ("notify", "boolean"), ("force", "boolean")),
                  sig("orders.cancel/2", "orders", ("order", "Order"), ("refund", "bool"))]
    lit = lambda v, c, ctx, field=None: {"value": v, "component": c, "file": f"lib/{c}/x.ex", "line": 3, "context": ctx,
                                         **({"field": field} if field else {})}
    literals = [lit("draft", "orders", "assign", "status"), lit("paid", "orders", "compare", "status"),
                lit("paid", "billing", "compare", "status"), lit("shipped", "shipping", "case", "status"),
                lit("admin", "orders", "compare"), lit("admin", "billing", "compare"), lit("admin", "shipping", "case"),
                lit("x y z", "orders", "compare", "status")]
    return {"format": "archmodel/1", "components": comps, "types": types, "files": files,
            "dependencies": [{"from": a, "to": b, "weight": 1, "evidence": []} for a, b in deps],
            "signatures": signatures, "literals": literals}


class DetectorsTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(rich_model()))
        self.p.path("hotspots.json").write_text(json.dumps({"key": {}, "result": {
            "head": "abc", "since_days": 365, "pairs": [{"a": "B", "b": "J", "together": 9, "degree": 0.9}]}}))

    def run_detectors(self, **kw):
        from archplan import detectors
        return detectors.run(self.p, **kw)

    def test_ported_ids_unchanged(self):
        res = self.run_detectors()
        ported = sorted((f["kind"], f["id"]) for f in res["findings"] if f["kind"] in {k for k, _ in PORTED_IDS})
        self.assertEqual(ported, PORTED_IDS)
        counts = {d["id"]: d["count"] for d in res["detectors"]}
        for kind in {k for k, _ in PORTED_IDS}:
            self.assertEqual(counts[kind], sum(1 for k, _ in PORTED_IDS if k == kind))
        # findings.analyse gives the same as before for the brief and the editor
        from archplan.findings import analyse
        self.assertEqual(sorted(f["id"] for f in analyse(self.p)[0]), sorted(f["id"] for f in res["findings"]))

    def test_missing_needs(self):
        res = self.run_detectors()
        why = {m["kind"]: m["reason"] for m in res["missing"]}
        self.assertIn("signatures", why["concepts.bool_params"])
        self.assertIn("literals", why["concepts.string_enum"])
        self.assertIn("hotspots --refresh", why["evolution.ownership"])
        row = next(d for d in res["detectors"] if d["id"] == "concepts.bool_params")
        self.assertEqual(row["missing"], ["signatures"])

    def test_enable_disable(self):
        from archplan import detectors
        from archplan.findings import Findings
        F = Findings(self.p)
        self.assertTrue(any(f["kind"] == "types.fields" for f in F.listing()["findings"]))
        with redirect_stdout(io.StringIO()):
            cli.main(["detectors", "disable", "types.fields", "--dir", str(self.root)])
        cfg = json.loads(self.p.path("config.json").read_text())
        self.assertEqual(cfg["detectors"]["types.fields"], {"enabled": False})
        self.assertFalse(any(f["kind"] == "types.fields" for f in F.listing()["findings"]))  # cache key follows config
        row = next(d for d in self.run_detectors()["detectors"] if d["id"] == "types.fields")
        self.assertEqual((row["enabled"], row["count"]), (False, 0))
        detectors.configure(self.p, "types.fields", enabled=True, params={"large_type": 5, "cluster_size": 2})
        self.assertEqual(json.loads(self.p.path("config.json").read_text())["detectors"]["types.fields"],
                         {"enabled": True, "params": {"large_type": 5, "cluster_size": 2}})
        titles = [f["title"] for f in F.listing()["findings"] if f["kind"] == "types.fields"]
        self.assertIn("B.Member has 7 fields.", titles)
        with self.assertRaises(detectors.DetectorError):
            detectors.configure(self.p, "types.fields", params={"large_type": "many"})
        with self.assertRaises(detectors.DetectorError):
            detectors.configure(self.p, "types.fields", params={"nope": 1})
        with self.assertRaises(KeyError):
            detectors.configure(self.p, "no.such")
        # a hand-edited config with bad values falls back to the defaults
        cfg = json.loads(self.p.path("config.json").read_text())
        cfg["detectors"]["types.fields"] = {"enabled": "no", "params": {"large_type": [1]}}
        self.p.path("config.json").write_text(json.dumps(cfg))
        row = next(d for d in self.run_detectors()["detectors"] if d["id"] == "types.fields")
        self.assertEqual((row["enabled"], row["params"]["large_type"]), (True, 25))

    def test_detectors_cli_and_api(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["detectors", "--dir", str(self.root)])
        out = buf.getvalue()
        self.assertRegex(out, r"structure\.tangle\s+structure\s+yes\s+1\s+deps")
        self.assertIn("concepts.bool_params", out)
        buf = io.StringIO()
        with redirect_stdout(buf), unittest.mock.patch("sys.stderr", io.StringIO()):
            cli.main(["detectors", "run", "structure.cycle", "--json", "--dir", str(self.root)])
        res = json.loads(buf.getvalue())
        self.assertEqual([d["id"] for d in res["detectors"]], ["structure.cycle"])
        self.assertEqual(len(res["findings"]), 2)
        srv, url = serve_in_thread(self.p)
        try:
            st, d = call(url, "/api/detectors")
            self.assertEqual(st, 200)
            key = d["key"]
            self.assertEqual(next(x for x in d["detectors"] if x["id"] == "layers.policy")["count"], 1)
            st, d = call(url, "/api/detectors/layers.policy", "PUT", {"enabled": False})
            self.assertEqual((st, d["config"]), (200, {"enabled": False}))
            self.assertNotEqual(d["key"], key)
            st, d = call(url, "/api/findings")
            self.assertFalse(any(f["kind"] == "layers.policy" for f in d["findings"]))
            self.assertEqual(call(url, "/api/detectors/layers.policy", "PUT", {"params": {"x": 1}})[0], 400)
            self.assertEqual(call(url, "/api/detectors/no.such", "PUT", {"enabled": True})[0], 404)
            st, d = call(url, "/api/metrics")
            self.assertEqual(st, 200)
            self.assertIn("A", d["metrics"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_external_detector(self):
        from archplan.detectors import external
        script = Path(self.tmp.name) / "det.py"
        script.write_text(textwrap.dedent("""\
            import json, sys
            ctx = json.load(open(sys.argv[1]))
            assert ctx["model"]["format"] == "archmodel/1" and ctx["params"] == {"limit": 3}
            fs = [{"key": "a", "title": "T" * 999, "components": ["A", "nowhere"], "strength": "huge",
                   "files": ["lib/a/user.ex:3", "../../etc/passwd", "/etc/passwd", 7], "evidence": {"n": 1}},
                  {"key": ["b", 2], "title": "second", "evidence": {"x": "y" * 20000}}, "junk", {"title": ""}]
            fs += [{"key": str(i), "title": f"more {i}"} for i in range(500)]
            print(json.dumps({"findings": fs}))
            """))
        d = self.p.path("detectors")
        d.mkdir()
        (d / "ext.json").write_text(json.dumps({
            "id": "concepts.ext_check", "name": "Ext", "group": "concepts", "needs": ["fields"],
            "command": f"{sys.executable} {script} {{ctx}}", "params": {"limit": 3}, "timeout": 30}))
        (d / "clash.json").write_text(json.dumps({"id": "types.fields", "group": "types", "command": "true {ctx}"}))
        (d / "bad.json").write_text(json.dumps({"id": "Bad Id", "group": "types", "command": "true {ctx}"}))
        res = self.run_detectors(execute=True)
        why = {m["kind"]: m["reason"] for m in res["missing"]}
        self.assertIn("not approved", why["concepts.ext_check"])
        self.assertEqual(sum(1 for m in res["missing"] if m["kind"] == "detector"), 2)  # clash and bad id
        self.assertFalse(any(f["kind"] == "concepts.ext_check" for f in res["findings"]))
        spec = external.load(self.p.dir)[0]["concepts.ext_check"]
        external.trust(self.p.root, spec)
        res = self.run_detectors(execute=True)
        fs = [f for f in res["findings"] if f["kind"] == "concepts.ext_check"]
        self.assertEqual(len(fs), external.MAX_FINDINGS - 2)  # the junk entry and the empty title are dropped
        self.assertTrue(all(f["id"].startswith("concepts.ext_check-") and f["group"] == "concepts" for f in fs))
        first = next(f for f in fs if f["title"].startswith("TTT"))
        self.assertEqual((len(first["title"]), first["components"], first["files"], first["strength"]),
                         (external.TITLE_MAX, ["A"], ["lib/a/user.ex"], "weak"))
        self.assertEqual(next(f for f in fs if f["title"] == "second")["evidence"], {"truncated": True})
        # the editor shows the cached result without running the command
        ids = sorted(f["id"] for f in fs)
        res = self.run_detectors()
        self.assertEqual(sorted(f["id"] for f in res["findings"] if f["kind"] == "concepts.ext_check"), ids)
        self.assertIn(external.CACHE, self.p.path(".gitignore").read_text())
        # a changed command is not approved any more
        (d / "ext.json").write_text(json.dumps({**json.loads((d / "ext.json").read_text()),
                                                "command": f"{sys.executable} {script} {{ctx}} --x"}))
        why = {m["kind"]: m["reason"] for m in self.run_detectors(execute=True)["missing"]}
        self.assertIn("not approved", why["concepts.ext_check"])
        # a slow command times out
        (d / "ext.json").write_text(json.dumps({"id": "concepts.ext_check", "group": "concepts",
                                                "command": f"{sys.executable} -c 'import time; time.sleep(5)' {{ctx}}",
                                                "timeout": 0.5}))
        external.trust(self.p.root, external.load(self.p.dir)[0]["concepts.ext_check"])
        why = {m["kind"]: m["reason"] for m in self.run_detectors(execute=True)["missing"]}
        self.assertIn("timed out", why["concepts.ext_check"])


class NewDetectorsTest(TempProject):
    def setUp(self):
        super().setUp()
        self.p = project.init(self.root)
        self.p.model_path.write_text(json.dumps(concept_model()))

    def found(self, did):
        from archplan import detectors
        res = detectors.run(self.p, only={did})
        self.assertEqual(res["missing"], [])
        return res["findings"]

    def test_state_flags(self):
        (f,) = self.found("concepts.state_flags")
        self.assertEqual(f["evidence"]["types"][0]["flags"], ["is_paid", "is_shipped", "cancelled"])
        self.assertEqual(f["evidence"]["types"][0]["timestamps"], ["cancelled_at"])
        self.assertIn("Order in orders keeps its state in 3 boolean flags", f["title"])

    def test_data_clump(self):
        (f,) = self.found("concepts.data_clump")
        self.assertEqual(f["evidence"]["fields"], ["city", "street", "zip_code"])
        self.assertEqual(f["components"], ["billing", "orders", "shipping"])

    def test_raw_ids(self):
        (f,) = self.found("concepts.raw_ids")
        self.assertEqual((f["evidence"]["component"], f["evidence"]["owner"]), ("orders", "billing"))
        self.assertIn("InvoiceId", f["evidence"]["moves"][0])

    def test_untyped_bag(self):
        (f,) = self.found("concepts.untyped_bag")  # Order is used by billing and shipping; Order.meta is a map
        self.assertEqual(f["components"], ["orders", "billing", "shipping"])
        self.assertEqual(f["evidence"]["fields"][0]["field"], "meta")
        from archplan.detectors.fields import is_bag
        for t, want in [("map", True), ("map() | nil", True), ("{:array, map}", True), ("Dict[str, Any]", True),
                        ("JSONField", True), ("map[string]interface{}", True), ("Record<string, unknown>", True),
                        ("map[string]string", False), ("Map<String, Order>", False), ("string", False)]:
            self.assertEqual(is_bag({"type": t}), want, t)

    def test_units_in_names(self):
        fs = {f["evidence"]["base"]: f for f in self.found("concepts.units_in_names")}
        self.assertEqual(set(fs), {"amount", "timeout"})
        self.assertEqual(fs["timeout"]["evidence"]["units"], {"ms": ["orders"], "s": ["shipping"]})
        self.assertEqual(fs["amount"]["strength"], "weak")

    def test_stability(self):
        from archplan import detectors
        fs = detectors.run(self.p, only={"boundaries.stability"})
        self.assertEqual([], fs["findings"])  # core has 3 dependents, fewer than pain_dependents
        detectors.configure(self.p, "boundaries.stability", params={"pain_dependents": 3})
        res = detectors.run(self.p, only={"boundaries.stability"})
        (f,) = res["findings"]
        self.assertEqual((f["evidence"]["component"], f["evidence"]["zone"]), ("core", "pain"))
        m = res["extras"]["metrics"]["core"]
        self.assertEqual((m["ca"], m["ce"], m["i"], m["a"], m["d"]), (3, 0, 0.0, 0.0, 1.0))

    def test_unused_public(self):
        (f,) = self.found("boundaries.unused_public")
        self.assertEqual(f["components"], ["core"])
        self.assertEqual(f["files"], ["lib/core/cache.ex", "lib/core/clock.ex", "lib/core/util.ex"])

    def test_ownership(self):
        res = {"since_days": 365, "head": "abc", "pairs": [],
               "owners": {"core": {f"dev{i}@x": 3 for i in range(8)},
                          **{c: {"solo@x": 30, "dev0@x": 2} for c in ("orders", "billing", "shipping")}}}
        self.p.path("hotspots.json").write_text(json.dumps({"key": {}, "result": res}))
        from archplan import detectors
        detectors.configure(self.p, "evolution.ownership", params={"spread_components": 3})
        fs = {tuple(f["components"]): f for f in self.found("evolution.ownership")}
        self.assertIn("core has 8 active authors and no clear owner", fs[("core",)]["title"])
        spread = next(f for f in fs.values() if f["evidence"].get("authors") and isinstance(f["evidence"]["authors"], list))
        self.assertEqual([a["author"] for a in spread["evidence"]["authors"]], ["dev0@x", "solo@x"])

    def test_string_enum(self):
        fs = {f["evidence"]["kind"]: f for f in self.found("concepts.string_enum")}
        self.assertEqual(fs["field"]["evidence"]["values"], {"paid": 2, "draft": 1, "shipped": 1})
        self.assertIn("Status enum", fs["field"]["evidence"]["moves"][0])
        self.assertEqual(fs["value"]["title"], "The string 'admin' is compared in 3 components (orders, billing, shipping).")

    def test_bool_params(self):
        (f,) = self.found("concepts.bool_params")
        self.assertEqual(f["evidence"]["functions"][0], {"id": "orders.place/3", "flags": ["notify", "force"],
                                                         "file": "lib/orders/x.ex", "line": 1})
        self.assertEqual(f["strength"], "medium")

    def test_hotspots_cache_owners(self):
        commits = [("1", "a@x", "2026-01-02", ["lib/core/repo.ex", "lib/orders/order.ex"]),
                   ("2", "b@x", "2026-01-01", ["lib/core/repo.ex"])]
        res = hotspots.analyse(commits, concept_model())
        self.assertEqual(res["owners"], {"core": {"a@x": 1, "b@x": 1}, "orders": {"a@x": 1}})


class RepoCodeTrustTest(TempProject):
    """Exporters that run repository code are never approved by init, and an
    approval lapses when a file the command names changes."""

    def init_out(self, root):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["init", str(root)])
        return buf.getvalue()

    def test_init_does_not_trust_ts_elixir_go(self):
        from archplan import exporters
        cases = {
            "typescript": ({"package.json": '{"name": "x"}', "tsconfig.json": "{}", "src/a.ts": "export const a = 1\n",
                            "node_modules/typescript/package.json": "{}"}, "export-ts"),
            "elixir": ({"mix.exs": "defmodule X.MixProject do\nend\n", "lib/x.ex": "defmodule X do\nend\n"},
                       "export-elixir"),
            "go": ({"go.mod": "module example.com/x\n", "main.go": "package main\n"}, "go run"),
        }
        for name, (files, want) in cases.items():
            with self.subTest(name):
                root = Path(self.tmp.name) / name
                write(root, files)
                res = exporters.survey(root, root / ".archplan")
                self.assertEqual(res["pick"], name)
                self.assertTrue(res["runs_repo_code"])
                out = self.init_out(root)
                p = project.Project(root)
                self.assertIn(want, p.export_command())
                self.assertFalse(p.trusted())
                self.assertIn("archplan trust", out)
                self.assertIn("runs code from this repository", out)
                with self.assertRaises(project.UntrustedExport):
                    p.export(require_trust=True)
                with self.assertRaises(project.UntrustedExport):
                    Agent(p).refresh_model()
                p.trust()  # what `archplan trust` does after the person said yes
                self.assertTrue(p.trusted())
        # the registry marks exactly these three
        es, _ = exporters.load()
        self.assertEqual(sorted(n for n, e in es.items() if e.get("runs_repo_code")), ["elixir", "go", "typescript"])

    def test_python_and_lite_stay_auto_approved(self):
        out = self.init_out(self.root)
        self.assertTrue(project.Project(self.root).trusted())
        self.assertNotIn("not approved", out)
        root = Path(self.tmp.name) / "jvm"
        write(root, LITE["java"])
        project.init(root)
        self.assertTrue(project.Project(root).trusted())

    def test_runs_repo_code_must_be_boolean(self):
        from archplan import exporters
        e = {"name": "x", "detect": {"files": ["a"]}, "command": "x -o {out}", "runs_repo_code": "yes"}
        with self.assertRaises(exporters.ExporterError):
            exporters.check_entry(e)

    def test_changed_script_revokes_export_approval(self):
        write(self.root, {"tools/exp.py": "print('v1')\n"})
        p = project.init(self.root, export="python3 tools/exp.py -o .archplan/model.archmodel.json", force=True)
        self.assertTrue(p.trusted())
        record = project.load_trust()["files"]
        self.assertEqual([list(v) for v in record.values()], [["tools/exp.py"]])
        # the output after -o does not count
        p.model_path.write_text("{}")
        self.assertTrue(p.trusted())
        (self.root / "tools/exp.py").write_text("import os; os.system('touch owned')\n")  # a git pull
        self.assertEqual(p.trust_state(), (False, ["tools/exp.py"]))
        with self.assertRaises(project.UntrustedExport) as cm:
            p.export(require_trust=True)
        self.assertIn("tools/exp.py", str(cm.exception))
        self.assertFalse((self.root / "owned").exists())
        r = subprocess.run([str(ROOT / "bin" / "archplan"), "trust"], cwd=self.root, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tools/exp.py", r.stderr)
        self.assertIn("changed", r.stderr)
        p.trust()
        self.assertTrue(p.trusted())

    def test_old_record_without_hashes_needs_new_approval(self):
        write(self.root, {"tools/exp.py": "print('v1')\n"})
        p = project.init(self.root, export="python3 tools/exp.py -o out.json", force=True)
        data = project.load_trust()
        del data["files"]
        project.write_json(project.trust_path(), data)
        self.assertEqual(p.trust_state(), (False, ["tools/exp.py"]))

    def test_argument_outside_repo_or_symlinked_is_not_hashed_outside(self):
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text("x")
        os.symlink(outside, self.root / "link.py")
        self.assertEqual(project.arg_files(self.root, f"python3 {outside} link.py --x=../outside.py"), {})

    def test_changed_script_revokes_detector_approval(self):
        from archplan.detectors import external
        write(self.root, {"tools/check.py": "print('{\"findings\": []}')\n"})
        p = project.init(self.root)
        d = p.path("detectors")
        d.mkdir()
        (d / "x.json").write_text(json.dumps({"id": "concepts.x", "group": "concepts",
                                              "command": "python3 tools/check.py {ctx}"}))
        p.export()
        spec = external.load(p.dir)[0]["concepts.x"]
        self.assertFalse(external.trusted(p.root, spec))
        external.trust(p.root, spec)
        self.assertTrue(external.trusted(p.root, spec))
        (self.root / "tools/check.py").write_text("import os; os.system('touch owned')\n")
        self.assertEqual(external.trust_state(p.root, spec), (False, ["tools/check.py"]))
        from archplan import detectors
        res = detectors.run(p, only={"concepts.x"}, execute=True, include_disabled=True)
        why = {m["kind"]: m["reason"] for m in res["missing"]}
        self.assertIn("tools/check.py", why["concepts.x"])
        self.assertFalse((self.root / "owned").exists())


@unittest.skipUnless(hasattr(os, "mkfifo"), "needs FIFOs")
class HostileFilesTest(TempProject):
    """Repository files are read only when they are regular files inside the
    repository and not too large; walks do not follow symlinked folders."""

    def setUp(self):
        super().setUp()
        self.outside = Path(self.tmp.name) / "secret"
        self.outside.mkdir()
        (self.outside / "Secret.java").write_text("package x;\npublic class Secret {}\n")

    def plant(self, root: Path, name: str):
        """name as a symlink to /dev/zero, a symlink out of the repo, and a FIFO."""
        stem, ext = os.path.splitext(name)
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        os.symlink("/dev/zero", root / f"{stem}Zero{ext}")
        os.symlink(self.outside / "Secret.java", root / f"{stem}Out{ext}")
        os.mkfifo(root / f"{stem}Fifo{ext}")

    def timed(self, fn, limit=10.0):
        import time
        t = time.monotonic()
        r = fn()
        self.assertLess(time.monotonic() - t, limit)
        return r

    def test_safeio(self):
        from archplan import safeio
        self.plant(self.root, "x/a.txt")
        (self.root / "x/ok.txt").write_text("ok")
        (self.root / "x/big.txt").write_bytes(b"a" * 100)
        self.assertEqual(safeio.read_text(self.root, "x/ok.txt"), "ok")
        for rel in ("x/aZero.txt", "x/aOut.txt", "x/aFifo.txt", "../secret/Secret.java", "/etc/passwd"):
            with self.subTest(rel), self.assertRaises(OSError):
                self.timed(lambda: safeio.read_text(self.root, rel), 2)
        with self.assertRaises(safeio.Unsafe):
            safeio.read_text(self.root, "x/big.txt", limit=10)
        self.assertEqual(safeio.read_text(self.root, "x/big.txt", limit=10, truncate=True), "a" * 10)
        os.symlink(self.outside, self.root / "x" / "linked")
        walked = [d for d, _, _ in safeio.walk(self.root)]
        self.assertFalse(any("linked" in d for d in walked))

    def test_lite_export_skips_planted_files(self):
        from archplan import liteexport
        root = Path(self.tmp.name) / "jvm"
        write(root, LITE["java"])
        self.plant(root, "src/main/java/com/acme/A.java")
        os.symlink(self.outside, root / "src" / "main" / "java" / "linked")
        m = self.timed(lambda: liteexport.export(root))
        self.assertTrue(m["types"])
        self.assertFalse([t for t in m["types"] if "Secret" in t])
        self.assertFalse([f for f in m["files"] if "Zero" in f or "Fifo" in f or "Out" in f or "linked" in f])

    def test_python_export_skips_planted_files(self):
        self.plant(self.root, "src/shop/a.py")
        os.symlink(self.outside, self.root / "src" / "shop" / "linked")
        m = self.timed(lambda: export(self.root))
        self.assertFalse([f for f in m.get("files") or {} if "Zero" in f or "Fifo" in f or "linked" in f])

    def test_model_paths_and_manifests(self):
        from archplan import frameworks
        from archplan.detectors import Context
        from archplan.schema import access
        self.plant(self.root, "lib/a.ex")
        os.symlink("/dev/zero", self.root / "package.json")
        self.assertEqual(self.timed(lambda: frameworks.read_deps(self.root / "package.json"), 2), set())
        rd = access.Reader(self.root)
        for rel in ("lib/aZero.ex", "lib/aOut.ex", "lib/aFifo.ex", "../secret/Secret.java"):
            self.assertEqual(self.timed(lambda: rd.text(rel), 2), "")
        p = project.init(self.root)
        ctx = Context(p, model={"components": {}, "files": {}}, target={})
        for rel in ("lib/aZero.ex", "lib/aFifo.ex", "../secret/Secret.java"):
            self.assertIsNone(self.timed(lambda: ctx.read(rel), 2))

    def test_archplan_dir_planted_files(self):
        p = project.init(self.root)
        (p.dir / "threads.json").unlink(missing_ok=True)
        os.mkfifo(p.dir / "threads.json")
        os.symlink("/dev/zero", p.dir / "plan.md")
        with self.assertRaises(OSError):
            self.timed(lambda: project.read_json(p.dir / "threads.json"), 2)
        with self.assertRaises(OSError):
            self.timed(lambda: Agent(p).get_plan(), 2)
        # a planted temporary file cannot redirect a write out of the repository
        target = self.outside / "victim"
        target.write_text("keep")
        os.symlink(target, p.dir / "state.json.tmp")
        p.set_state(x=1)
        self.assertEqual(target.read_text(), "keep")


@unittest.skipUnless(shutil.which("node"), "needs node")
class GlossaryRoundTripTest(unittest.TestCase):
    """A glossary written as planned must not show up as a change: reading
    GLOSSARY.md back puts the name first in its own case (issue #1)."""

    def concept_terms(self, *concepts):
        src = (ROOT / "src/archplan/static/index.html").read_text()
        body = src[src.index("// ---------- concept terms"):src.index("// ---------- diff")]
        out = subprocess.run(["node", "-e", body + f"""
            console.log(JSON.stringify({json.dumps(list(concepts))}.map(conceptTerms)));"""],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def glossary_md(self, model, component):
        src = (ROOT / "src/archplan/static/index.html").read_text()
        lookup = src[src.index("const vcomp = "):src.index("\n", src.index("const conceptName = "))]
        writer = src[src.index("const yamlStr = "):src.index("const plural = ")]
        out = subprocess.run(["node", "-e", "const toBe = {}, asIs = {};\n" + lookup + "\n" + writer + f"""
            process.stdout.write(glossaryMD({json.dumps(model)}, {json.dumps(component)}));"""],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_kinds_survive_the_glossary_file(self):
        from archplan.vocab import parse_glossary
        concepts = {"invoice": {"name": "Invoice", "owner": "billing", "terms": ["invoice"], "kind": "domain"},
                    "opp": {"name": "OPP", "owner": "billing", "terms": ["opp"], "kind": "vendor"},
                    "payment-webhook": {"name": "Payment webhook", "owner": "billing",
                                        "terms": ["webhook"], "kind": "technical"}}
        text = self.glossary_md({"vocabulary": {"concepts": concepts, "components": {}}}, "billing")
        g = parse_glossary(text)
        self.assertEqual((g["vendor"], g["technical"]), (["OPP"], ["Payment webhook"]))
        plain = self.glossary_md({"vocabulary": {"concepts": {"invoice": concepts["invoice"]}}}, "billing")
        self.assertNotIn("vendor:", plain)  # an all-domain glossary keeps its old shape
        self.assertNotIn("technical:", plain)

    def test_terms_read_back_from_glossary_match_the_proposal(self):
        proposed = {"name": "Consent agreement", "terms": ["consent", "agreement", "consent agreement"]}
        read_back = {"name": "Consent agreement", "terms": ["Consent agreement", "consent", "agreement"]}
        a, b = self.concept_terms(proposed, read_back)
        self.assertEqual(a, b)

    def test_spelling_and_case_fold_to_the_same_term(self):
        a, b = self.concept_terms({"name": "Pay-in", "terms": ["payIn", "PAY_IN request"]},
                                  {"name": "Pay-in", "terms": ["pay in", "pay in request"]})
        self.assertEqual(a, b)

    def test_a_real_term_change_still_shows(self):
        a, b = self.concept_terms({"name": "Reward", "terms": ["reward"]},
                                  {"name": "Reward", "terms": ["reward", "fee"]})
        self.assertNotEqual(a, b)

    def test_the_diff_compares_concept_terms(self):
        src = (ROOT / "src/archplan/static/index.html").read_text()
        diff = src[src.index("function diffModels"):src.index("function dslDiff")]
        self.assertIn("conceptTerms(", diff)
        self.assertNotIn("JSON.stringify(x.terms", diff)



class SubfolderVocabTest(TempProject):
    """The project sits in core/ of its repository; model paths are relative
    to the repository root."""

    def setUp(self):
        super().setUp()
        (self.root / ".git").mkdir()
        self.core = self.root / "core"
        write(self.core, VOCAB_TREE)
        self.p = project.init(self.core)
        m = vocab_model()
        pre = lambda s: "core/" + s
        for c in m["components"].values():
            c["path"], c["match"] = pre(c["path"]), [pre(x) for x in c["match"]]
        for t in m["types"].values():
            t["file"] = pre(t["file"])
        m["files"] = {pre(f): v for f, v in m["files"].items()}
        self.p.model_path.write_text(json.dumps(m))

    def test_candidates(self):
        from archplan.vocab import Vocab
        res = Vocab(self.p).candidates("billing")
        terms = [t["term"] for t in res["components"]["billing"]["terms"]]
        self.assertEqual(terms[:2], ["invoice", "debtor"])
        self.assertTrue(res["components"]["billing"]["terms"][0]["sites"][0].startswith("core/lib/billing/"))
        self.assertNotIn("warning", res)

    def test_glossary_in_subfolder(self):
        from archplan.vocab import enrich
        write(self.core, {"lib/billing/GLOSSARY.md": "---\nowns:\n  Invoice: []\n---\n**Invoice**: what a debtor owes.\n"})
        self.assertTrue(enrich(self.p))
        v = json.loads(self.p.model_path.read_text())["vocabulary"]
        self.assertEqual(v["concepts"]["invoice"]["definition"], "what a debtor owes.")
        self.assertEqual(v["components"]["billing"]["file"], "core/lib/billing/GLOSSARY.md")
        self.assertIn("debtor", [x["term"] for x in v["components"]["billing"]["suggested"]])

    def test_missing_files_warn(self):
        from archplan.vocab import Vocab
        shutil.rmtree(self.core / "lib")
        res = Vocab(self.p).candidates()
        self.assertIn("none of the 8 source files", res["warning"])


if __name__ == "__main__":
    unittest.main()
