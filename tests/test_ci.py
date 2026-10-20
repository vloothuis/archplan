import io
import json
import os
import subprocess
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from test_archplan import TempProject, findings_model, write

from archplan import ci, cli, project
from archplan.core import exporters

EXPORT = "archplan export-python -o .archplan/model.archmodel.json"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


class CiTest(TempProject):
    def run_ci(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e, unittest.mock.patch("sys.stderr", err), \
                unittest.mock.patch.dict(os.environ, env or {}):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            if not env or "ARCHPLAN_CI_TRUST" not in env:
                os.environ.pop("ARCHPLAN_CI_TRUST", None)
            cli.main(["ci", "--dir", str(self.root), *args])
        return e.exception.code, out.getvalue(), err.getvalue()

    def model(self, m):
        p = project.Project(self.root)
        p.model_path.write_text(json.dumps(m))
        t = time.time() + len(json.dumps(m)) % 97
        os.utime(p.model_path, (t, t))

    # ---------- exporter versions ----------

    def test_go_exporter_is_pinned(self):
        self.assertEqual(exporters.go_version("26.10.15"), "v0.2610.15")
        self.assertEqual(exporters.go_version("27.1.0"), "v0.2701.0")
        for dev in ("0.8.1", "26.10.15.dev1", "26.13.0", "", "main"):
            self.assertEqual(exporters.go_version(dev), "latest")
        write(self.root, {"go.mod": "module example.com/x\n"})
        with unittest.mock.patch("archplan.__version__", "26.10.15"):
            cmd = exporters.survey(self.root)["exporters"]
        go = next(r for r in cmd if r["name"] == "go")["command"]
        self.assertIn("archplan-go@v0.2610.15 ", go)
        with unittest.mock.patch("archplan.__version__", "0.0.0.dev0"):
            go = next(r for r in exporters.survey(self.root)["exporters"] if r["name"] == "go")["command"]
        self.assertIn("archplan-go@latest ", go)

    # ---------- without a base ----------

    def test_baseline_mode_reports(self):
        p = project.init(self.root, export=EXPORT)
        self.model(findings_model())
        summary, sarif = Path(self.tmp.name) / "summary.md", Path(self.tmp.name) / "out" / "x.sarif"
        code, out, err = self.run_ci("--no-export", "--summary", str(summary), "--sarif", str(sarif))
        self.assertEqual(code, 1)
        self.assertIn("form a dependency cycle", out)
        md = summary.read_text()
        self.assertIn("**Failed**", md)
        self.assertIn("### New findings", md)
        self.assertIn("### Ratchet", md)
        s = json.loads(sarif.read_text())
        self.assertEqual(s["version"], "2.1.0")
        run = s["runs"][0]
        self.assertIn("structure.cycle", {r["id"] for r in run["tool"]["driver"]["rules"]})
        res = next(r for r in run["results"] if r["ruleId"] == "structure.cycle")
        loc = res["locations"][0]["physicalLocation"]
        self.assertTrue(loc["artifactLocation"]["uri"].startswith("lib/"))
        self.assertIn("startLine", loc["region"])
        self.assertEqual(res["level"], "warning")
        code, out, _ = self.run_ci("--no-export", "--format", "github")
        self.assertIn("::warning file=lib/", out)
        # GITHUB_STEP_SUMMARY is appended to
        gh = Path(self.tmp.name) / "step.md"
        gh.write_text("before\n")
        with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(gh)}):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit), \
                    unittest.mock.patch("sys.stderr", io.StringIO()):
                cli.main(["ci", "--dir", str(self.root), "--no-export"])
        self.assertTrue(gh.read_text().startswith("before\n## archplan"))
        # check and ci share the baseline
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            cli.main(["check", "--dir", str(self.root), "--update-baseline"])
        self.assertEqual(self.run_ci("--no-export")[0], 0)
        # a better metric: a notice, not a failure
        m = findings_model()
        m["dependencies"] = [d for d in m["dependencies"] if (d["from"], d["to"]) != ("C", "A")]
        self.model(m)
        code, out, err = self.run_ci("--no-export", "--format", "github")
        self.assertEqual(code, 0)
        self.assertIn("::notice title=archplan::", out)
        self.assertIn("archplan check --tighten", out)
        code, _, err = self.run_ci("--no-export")
        self.assertIn("note: ", err)
        self.assertIn("archplan check --tighten", err)
        # plan progress against a target
        t = findings_model()
        t["components"]["K"] = {"name": "K", "path": "lib/k"}
        p.target_path.write_text(json.dumps(t))
        self.run_ci("--no-export", "--summary", str(summary))
        self.assertIn("differences between the code and the target", summary.read_text())
        self.assertEqual(ci.plan_gap(m, t), 2)  # K, and the dependency C -> A

    def test_export_needs_trust(self):
        p = project.init(self.root, export=EXPORT)
        self.assertTrue(p.trusted())  # approved on this machine by init
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "ci-config")  # a CI runner: nothing approved
        code, _, err = self.run_ci()
        self.assertEqual(code, 2)
        self.assertIn("ARCHPLAN_CI_TRUST", err)
        self.assertFalse(p.model_path.exists())
        code, out, err = self.run_ci(env={"ARCHPLAN_CI_TRUST": "1"})
        self.assertIn(code, (0, 1))
        self.assertTrue(p.model_path.exists())
        p.model_path.unlink()
        code, _, _ = self.run_ci("--trust-config")
        self.assertIn(code, (0, 1))
        self.assertTrue(p.model_path.exists())
        # without trust a model that is there is used, with a note
        code, _, err = self.run_ci()
        self.assertIn(code, (0, 1))
        self.assertIn("not approved", err)

    # ---------- against a base ----------

    def base_repo(self):
        """main: SAMPLE, with the orders <-> billing cycle. feat: a new cycle x <-> y."""
        p = project.init(self.root, export=EXPORT)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "checkout", "-q", "-b", "feat")
        write(self.root, {"shop/x/__init__.py": "from shop.y import b\n", "shop/x/a.py": "A = 1\n",
                          "shop/y/__init__.py": "", "shop/y/b.py": "from shop.x import a\n"})
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "x and y")
        return p

    def test_base_worktree(self):
        self.base_repo()
        code, out, err = self.run_ci("--base", "main", "--json", "--all", env={"ARCHPLAN_CI_TRUST": "1"})
        self.assertEqual(code, 1, err)
        d = json.loads(out)
        new = [f for f in d["findings"] if f["state"] == "new"]
        base = [f for f in d["findings"] if f["state"] == "base"]
        self.assertEqual(d["counts"]["new"], len(new))
        self.assertTrue(any(set(f["components"]) >= {"shop.x", "shop.y"} for f in new), new)
        self.assertTrue(all(f["in_diff"] for f in new if f["kind"] == "structure.cycle"))
        self.assertTrue(any(set(f["components"]) >= {"shop.orders", "shop.billing"} for f in base), base)
        self.assertFalse(any(set(f["components"]) >= {"shop.orders", "shop.billing"} for f in new))
        self.assertEqual(d["base"]["ref"], "main")
        self.assertIn("shop/x/a.py", d["changed"]["files"])
        self.assertIn("weight.structure", {r["metric"] for r in d["ratchet_delta"]})
        # the temporary worktree is gone
        self.assertEqual(len([x for x in git(self.root, "worktree", "list").splitlines() if x.strip()]), 1)
        # the summary marks the diff and the findings on the base
        summary = Path(self.tmp.name) / "s.md"
        self.run_ci("--base", "main", "--summary", str(summary), env={"ARCHPLAN_CI_TRUST": "1"})
        md = summary.read_text()
        self.assertIn("Compared with `main`", md)
        self.assertIn("Already on the base", md)
        self.assertIn("| In the diff |", md)
        # the base is not approved, so without trust it cannot export there
        code, _, err = self.run_ci("--base", "main")
        self.assertEqual(code, 2)
        self.assertIn("ARCHPLAN_CI_TRUST", err)
        self.assertEqual(len([x for x in git(self.root, "worktree", "list").splitlines() if x.strip()]), 1)
        # fixing the new cycle passes, though the base's cycle is still there
        write(self.root, {"shop/y/b.py": "A = 2\n"})
        code, out, err = self.run_ci("--base", "main", env={"ARCHPLAN_CI_TRUST": "1"})
        self.assertEqual(code, 0, out + err)

    def test_base_model(self):
        p = self.base_repo()
        git(self.root, "checkout", "-q", "main")
        self.assertIn(self.run_ci("--trust-config")[0], (0, 1))
        cached = Path(self.tmp.name) / "base.archmodel.json"
        cached.write_text(p.model_path.read_text())
        git(self.root, "checkout", "-q", "feat")
        code, out, err = self.run_ci("--base-model", str(cached), "--trust-config", "--json")
        self.assertEqual(code, 1, err)
        d = json.loads(out)
        self.assertTrue(d["findings"])
        self.assertTrue(all("shop.x" in f["components"] or "shop.y" in f["components"] for f in d["findings"]))
        self.assertGreaterEqual(d["counts"]["on_base"], 1)
        # with --base too, the model comes from the file and nothing runs at the base
        code, out, err = self.run_ci("--base", "main", "--base-model", str(cached), "--no-export", "--json")
        self.assertEqual(code, 1, err)
        self.assertGreaterEqual(json.loads(out)["counts"]["on_base"], 1)
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{}")
        self.assertEqual(self.run_ci("--base-model", str(bad), "--no-export")[0], 2)
        # the branch's config.json cannot make the base write outside its worktree
        cfg = json.loads(p.path("config.json").read_text())
        cfg["model"] = "../outside.archmodel.json"
        p.path("config.json").write_text(json.dumps(cfg))
        (self.root.parent / "outside.archmodel.json").write_text(cached.read_text())
        code, _, err = self.run_ci("--base", "main", "--base-model", str(cached), "--no-export")
        self.assertEqual(code, 2)
        self.assertIn("outside the repository", err)

    def test_summary_escapes(self):
        self.assertEqual(ci.md("[x](http://e) *a* <b>|"), "\\[x\\]\\(http://e\\) \\*a\\* &lt;b>\\|")
        self.assertEqual(ci.code("a`b|c"), "`a'b\\|c`")


if __name__ == "__main__":
    unittest.main()
