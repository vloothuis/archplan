import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archplan import ci, detectors, findings, provenance  # noqa: E402
from archplan.detectors import (security_guard_outlier, security_unguarded_entry, security_unguarded_path,  # noqa: E402
                                security_unguarded_sink, weakness)
from test_security import Ctx, decl, model, msg  # noqa: E402


def run(mod, m, **over):
    return [weakness.tag(f) for f in mod.run(Ctx(m), {**mod.PARAMS, **over})]


def handlers(names, checked):
    """A model with handlers in one file of web; those in checked call Permit first."""
    m = model()
    m["declarations"] = []
    m["signatures"] = [{"id": f"internal/web.(*App).{n}", "file": "internal/web/orders.go", "component": "web"}
                       for n in names]
    m["sequences"] = {
        n: {"entry": f"internal/web.(*App).{n}", "messages": [
            msg("Caller", "web", f"(*App).{n}(w, r)", 0),
            *([msg("web", "auth", "Permit(ctx)", 1)] if n in checked else []),
            msg("web", "orders", "Find(id)", 1)]}
        for n in names}
    return m


class ProvenanceTest(unittest.TestCase):
    def test_edges_are_static_dynamic_or_both(self):
        m = model()
        m["sequences"]["get-order"]["messages"].append(msg("orders", "vault", "Read(id)", 2))
        m["sequences"]["get-order"]["messages"].append({**msg("web", "vault", "Joined()", 1), "joined": "route"})
        res = provenance.analyse(m)
        by = {(e["from"], e["to"]): e for e in res["edges"]}
        self.assertEqual(by[("web", "auth")]["provenance"], "both")       # through the type internal/auth.Verifier
        self.assertEqual(by[("web", "orders")]["provenance"], "both")
        self.assertEqual(by[("web", "orders")]["sequence_count"], 3)
        self.assertEqual(by[("orders", "web")]["provenance"], "static")
        self.assertEqual(by[("web", "vault")]["provenance"], "static")    # a joined message is not an observation
        self.assertEqual(by[("orders", "vault")], {"from": "orders", "to": "vault", "provenance": "dynamic",
                                                   "calls": 1, "sequence_count": 1, "sequences": ["get-order"]})
        self.assertEqual(res["summary"]["edges"], {"both": 2, "static": 2, "dynamic": 1})
        self.assertEqual(res["summary"]["components"], {"observed": 4, "total": 4})
        self.assertEqual(res["components"]["web"], {"observed": True, "sequences": 4})
        self.assertIn("orders -> vault", provenance.text(res, m).lower())

    def test_edge_for_get_item(self):
        m = model()
        self.assertEqual(provenance.edge(m, "web", "orders")["provenance"], "both")
        self.assertIn("Not observed", provenance.edge(m, "web", "vault")["note"])
        self.assertEqual(provenance.edge(m, "vault", "web"), {"provenance": "none"})
        m["sequences"] = {}
        self.assertEqual(provenance.edge(m, "web", "orders")["provenance"], "unknown")
        self.assertIn("no sequences", provenance.text(provenance.analyse(m), m))


class AgentFindingsTest(unittest.TestCase):
    def test_weakness_tags(self):
        fs = run(security_unguarded_entry, model())
        self.assertEqual({(tuple(f["cwe"]), f["owasp"]["2021"], f["owasp"]["2025"]) for f in fs},
                         {(("CWE-306",), "A07", "A07")})
        f = weakness.tag({"kind": "security.unguarded_entry", "evidence": {"auth": "authz"}})
        self.assertEqual((f["cwe"], f["owasp"]), (["CWE-862"], {"2021": "A01", "2025": "A01"}))
        self.assertEqual(weakness.names(weakness.tag({"kind": "security.unchecked_input"})),
                         {"cwe": ["CWE-20: Improper Input Validation"], "owasp": ["A03:2021 Injection", "A05:2025 Injection"]})
        self.assertNotIn("cwe", weakness.tag({"kind": "security.untraced"}))
        self.assertNotIn("cwe", weakness.tag({"kind": "structure.cycle"}))
        for kind, (cwes, owasp) in weakness.KINDS.items():
            self.assertIn(kind, detectors.builtins())
            self.assertTrue(all(c in weakness.CWE for c in cwes))
            self.assertTrue(all(k in weakness.OWASP[y] for y, k in owasp.items()))

    def test_trace_hops(self):
        fs = {f["evidence"]["entry"]: f for f in run(security_unguarded_entry, model())}
        self.assertEqual(fs["internal/web.(*App).listOrders"]["evidence"]["hops"],
                         [{"component": "web", "call": "(*App).listOrders", "sequence": "list-orders"},
                          {"component": "orders", "call": "List", "sequence": "list-orders"}])
        m = model()
        m["declarations"].append(decl("function", "internal/orders/store.go:All", "sink", "write",
                                      file="internal/orders/store.go"))
        hops = run(security_unguarded_sink, m)[0]["evidence"]["hops"]
        self.assertEqual([h["call"] for h in hops], ["(*App).export", "All"])
        self.assertIn("the sink (write)", hops[-1]["note"])

    def test_dependency_hops(self):
        m = model()
        m["dependencies"] = [{"from": "web", "to": "orders", "evidence": ["internal/web/orders.go:12"]},
                             {"from": "orders", "to": "vault", "evidence": ["internal/orders/store.go"]},
                             {"from": "web", "to": "auth"}]
        f = run(security_unguarded_path, m)[0]
        self.assertEqual(f["evidence"]["hops"], [
            {"component": "web", "to": "orders", "note": "Web depends on Orders", "file": "internal/web/orders.go",
             "line": 12},
            {"component": "orders", "to": "vault", "note": "Orders depends on Vault",
             "file": "internal/orders/store.go"}])
        flow = ci.code_flow(f, lambda p: "repo/" + p)
        locs = flow["threadFlows"][0]["locations"]
        self.assertEqual([x["location"]["physicalLocation"]["artifactLocation"]["uri"] for x in locs],
                         ["repo/internal/web/orders.go", "repo/internal/orders/store.go"])
        self.assertEqual(locs[0]["location"]["physicalLocation"]["region"], {"startLine": 12})
        self.assertEqual(locs[0]["location"]["message"]["text"], "Web depends on Orders")
        s = ci.sarif([f], lambda p: p or "cfg", {f["id"]})
        self.assertIn("external/cwe/cwe-862", s["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["tags"])
        self.assertIn("codeFlows", s["runs"][0]["results"][0])
        self.assertEqual(s["runs"][0]["results"][0]["properties"]["cwe"], ["CWE-862"])
        self.assertIsNone(ci.code_flow({"evidence": {"hops": [{"file": "a"}, {"file": 3}, "x"]}}, str))

    def test_long_paths_are_clipped(self):
        from archplan.detectors.security_common import clip_hops
        out = clip_hops([{"n": i} for i in range(20)])
        self.assertEqual(len(out), 8)
        self.assertEqual((out[0], out[1], out[2], out[-1]), ({"n": 0}, {"n": 1}, {"note": "13 more calls"}, {"n": 19}))

    def test_compact_detail(self):
        f = run(security_unguarded_entry, model())[0]
        f.update(state="new")
        short = findings.compact(f)
        self.assertEqual(short["cwe"], ["CWE-306"])
        self.assertFalse({"rule", "path", "moves", "resolved_when"} & set(short))
        full = findings.compact(f, True)
        self.assertEqual(full["rule"]["name"], "Entry point without a guard on its traced path")
        self.assertEqual(full["rule"]["rationale"], "a traced entry point whose request passes no guard.")
        self.assertEqual(full["path"], f["evidence"]["hops"])
        self.assertEqual(len(full["moves"]), 2)
        self.assertIn("calls a declared guard", full["resolved_when"])
        self.assertIn(f"no longer lists {f['id']}", full["resolved_when"])
        other = findings.compact({"id": "structure.cycle-1", "kind": "structure.cycle", "title": "t", "detail": "d",
                                  "strength": "weak", "basis": "b", "state": "new", "components": [], "files": [],
                                  "evidence": {}}, True)
        self.assertTrue(other["resolved_when"].startswith("Check: after the model is exported again"))
        self.assertNotIn("path", other)


class GuardOutlierTest(unittest.TestCase):
    NAMES = ["getOrder", "listOrders", "putOrder", "delOrder", "export"]

    def test_outlier_by_name(self):
        m = handlers(self.NAMES, self.NAMES[:4])
        fs = run(security_guard_outlier, m)
        self.assertEqual([f["evidence"]["entry"] for f in fs], ["internal/web.(*App).export"])
        f = fs[0]
        self.assertEqual(f["title"], "export does not call Auth.Permit, which 4 of its 4 siblings call.")
        self.assertEqual((f["evidence"]["group"], f["files"]), ("internal/web/orders.go", ["internal/web/orders.go"]))
        self.assertEqual(f["evidence"]["missing"], ["Auth.Permit"])
        self.assertEqual([h["call"] for h in f["evidence"]["hops"]], ["(*App).export", "Find"])
        self.assertEqual(f["cwe"], ["CWE-862"])
        self.assertEqual(run(security_guard_outlier, m, use_names=False), [])

    def test_declared_guard_and_open_entry(self):
        m = handlers(self.NAMES, self.NAMES[:4])
        m["sequences"]["export"]["messages"].insert(1, msg("web", "auth", "Screen(ctx)", 1))
        for n in self.NAMES[:4]:
            m["sequences"][n]["messages"][1] = msg("web", "auth", "Screen(ctx)", 1)
        m["sequences"]["getOrder"]["messages"].insert(1, msg("web", "internal/auth.Verifier", "VerifySubject(c)", 1))
        m["sequences"]["listOrders"]["messages"].insert(1, msg("web", "internal/auth.Verifier", "VerifySubject(c)", 1))
        m["sequences"]["putOrder"]["messages"].insert(1, msg("web", "internal/auth.Verifier", "VerifySubject(c)", 1))
        m["sequences"]["delOrder"]["messages"].insert(1, msg("web", "internal/auth.Verifier", "VerifySubject(c)", 1))
        m["declarations"] = [decl("function", "internal/auth/verifier.go:VerifySubject", "guard", True,
                                  file="internal/auth/verifier.go")]
        fs = run(security_guard_outlier, m, use_names=False)
        self.assertEqual([f["evidence"]["missing"] for f in fs], [["VerifySubject"]])
        m["declarations"].append(decl("entrypoint", "internal/web/orders.go:export", "auth", "none", "public"))
        self.assertEqual(run(security_guard_outlier, m, use_names=False), [])

    def test_needs_enough_agreeing_siblings(self):
        self.assertEqual(run(security_guard_outlier, handlers(self.NAMES[:3], self.NAMES[:2])), [])
        self.assertEqual(run(security_guard_outlier, handlers(self.NAMES, self.NAMES[:2])), [])
        self.assertEqual(run(security_guard_outlier, handlers(self.NAMES, self.NAMES)), [])


if __name__ == "__main__":
    unittest.main()
