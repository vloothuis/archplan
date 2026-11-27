import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from archplan import cli, presence, project  # noqa: E402
from archplan.presence import Presence, Summary, delivered, summarize, turn  # noqa: E402
from archplan.store import Store  # noqa: E402


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "app.py").write_text("x = 1\n")
        self.p = project.init(self.root)
        self.d = self.p.path("agents")

    def tearDown(self):
        self.tmp.cleanup()

    def sm(self, now=None):
        return summarize(self.d, now or time.time())

    def test_connected_while_the_server_runs(self):
        self.assertFalse(self.sm()["connected"])
        a = Presence(self.p)
        s = self.sm()
        self.assertTrue(s["connected"])
        self.assertFalse(s["listening"])
        self.assertIn("agents/", self.p.path(".gitignore").read_text())
        a.listening(True)
        self.assertTrue(self.sm()["listening"])
        a.saw({"t1": 2})
        a.saw({"t1": 3})
        self.assertEqual(self.sm()["seen"], {"t1": 3})
        k = self.sm()["key"]
        a._write()  # a heartbeat alone does not change the key
        self.assertEqual(self.sm()["key"], k)
        a.close()
        self.assertFalse(self.sm()["connected"])
        self.assertEqual(os.listdir(self.d), [])

    def test_stale_and_planted_files(self):
        a = Presence(self.p)
        self.assertFalse(self.sm(time.time() + 120)["connected"])  # missed beats
        a.close()
        os.makedirs(self.d, exist_ok=True)
        pid = os.getpid()
        (self.d / f"m-{pid}-0000000a.json").write_text(json.dumps({"beat": time.time() + 3600, "seen": {"x": 1}}))
        (self.d / f"m-{pid}-0000000b.json").write_text("[1, 2]")
        (self.d / f"m-{pid}-0000000c.json").write_text(json.dumps({"beat": "now"}))
        (self.d / "other.json").write_text(json.dumps({"beat": time.time()}))
        self.assertFalse(self.sm()["connected"])
        (self.d / f"m-{pid}-0000000d.json").write_text(json.dumps(
            {"beat": time.time(), "listening": "yes", "seen": {"__proto__": 1, "t": True, "u": -1, "v": 2}}))
        s = self.sm()
        self.assertTrue(s["connected"])
        self.assertFalse(s["listening"])
        self.assertEqual(s["seen"], {"__proto__": 1, "v": 2})

    @unittest.skipIf(sys.platform == "win32", "no pid check on Windows")
    def test_dead_pid(self):
        os.makedirs(self.d, exist_ok=True)
        (self.d / "m-999999999-0000000a.json").write_text(json.dumps({"beat": time.time()}))
        self.assertFalse(self.sm()["connected"])

    @unittest.skipIf(sys.platform == "win32", "symlinks")
    def test_symlinked_dir_is_refused(self):
        other = tempfile.mkdtemp()
        os.symlink(other, self.d)
        a = Presence(self.p)
        self.assertIsNone(a.file)
        self.assertEqual(os.listdir(other), [])
        self.assertFalse(self.sm()["connected"])
        a.close()

    def test_turns_from_hooks(self):
        a = Presence(self.p)
        turn(self.p, "s/1", "UserPromptSubmit", {"t": 1})
        self.assertTrue((self.d / "s-s1.json").exists())
        s = self.sm()
        self.assertTrue(s["busy"])
        self.assertEqual(s["seen"], {"t": 1})
        turn(self.p, "s/1", "Stop")
        self.assertFalse(self.sm()["busy"])
        self.assertEqual(self.sm()["seen"], {"t": 1})
        turn(self.p, "s/1", "SessionEnd")
        self.assertFalse((self.d / "s-s1.json").exists())
        turn(self.p, "", "UserPromptSubmit")
        self.assertEqual(sorted(os.listdir(self.d)), [a.file.name])
        a.close()

    def test_busy_needs_a_connected_agent(self):
        turn(self.p, "s1", "UserPromptSubmit")
        self.assertFalse(self.sm()["busy"])

    def test_summary_is_cached(self):
        s = Summary(self.p, every=60)
        self.assertFalse(s()["connected"])
        a = Presence(self.p)
        self.assertFalse(s()["connected"])
        a.close()

    def test_delivered(self):
        th = {"id": "t1", "messages": [{}, {}]}
        self.assertEqual(delivered("read_thread", th), {"t1": 2})
        self.assertEqual(delivered("list_threads", [{"id": "t1", "messages": 2}]), {"t1": 2})
        self.assertEqual(delivered("wait_for_comments", [{"id": "t1", "messages": 2}]), {"t1": 2})
        self.assertEqual(delivered("status", {"threads": {"waiting_on_agent": [{"id": "t2", "messages": 1}]}}),
                         {"t2": 1})
        big = [{"id": f"t{i}", "messages": 1, "last": {"text": "x" * 1000}} for i in range(100)]
        self.assertEqual(delivered("list_threads", big), {})  # may be cut before the model sees it
        self.assertEqual(delivered("get_plan", {"id": "t1", "messages": []}), {})


class HookPresenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = project.init(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, event):
        sys.stdin = io.StringIO(json.dumps({"session_id": "abc", "hook_event_name": event, "cwd": str(self.root)}))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                cli.main(["hook"])
        finally:
            sys.stdin = sys.__stdin__
        return buf.getvalue()

    def test_prompt_marks_busy_and_shown(self):
        th = Store(self.p.dir).create("comp:x", "x", "look", "human")
        a = Presence(self.p)
        self.assertIn("look", self.hook("UserPromptSubmit"))
        s = summarize(self.p.path("agents"), time.time())
        self.assertTrue(s["busy"])
        self.assertEqual(s["seen"], {th["id"]: 1})
        self.assertEqual(self.hook("Stop"), "")
        self.assertFalse(summarize(self.p.path("agents"), time.time())["busy"])
        a.close()


class ServeTest(unittest.TestCase):
    def test_mcp_loop_records_what_it_showed(self):
        from archplan import mcp
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = project.init(Path(tmp.name))
        th = Store(p.dir).create("comp:x", "x", "look", "human")
        d = p.path("agents")
        seen = {}
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "read_thread", "arguments": {"id": th["id"]}}}]
        real = presence.Presence.close

        def close(self):  # look before the file goes away
            seen.update(summarize(d, time.time()))
            real(self)
        sys.stdin = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        try:
            with unittest.mock.patch.object(presence.Presence, "close", close), redirect_stdout(io.StringIO()):
                mcp.serve(find=lambda: p)
        finally:
            sys.stdin = sys.__stdin__
        self.assertTrue(seen["connected"])
        self.assertEqual(seen["seen"], {th["id"]: 1})
        self.assertFalse(summarize(d, time.time())["connected"])


if __name__ == "__main__":
    unittest.main()
