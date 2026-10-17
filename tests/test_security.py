import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archplan.detectors import (security_open_entry, security_unguarded_entry,  # noqa: E402
                                security_unguarded_sink, security_untraced, security_zone_dependency)
from archplan.detectors.common import graph  # noqa: E402
from archplan.detectors.security_common import called, short  # noqa: E402


class Ctx:
    def __init__(self, model):
        self.model = model

    @property
    def components(self):
        return self.model.get("components") or {}

    @property
    def graph(self):
        return graph(self.model)


def decl(kind, tid, key, value, reason=None, file=None, name=None):
    d = {"target": {"kind": kind, "id": tid}, "key": key, "value": value, "annotator": "builtin"}
    if file:
        d.update(file=file, line=8, at={"file": file, "line": 9, "name": name or short(tid)})
    if reason:
        d["reason"] = reason
    return d


def msg(frm, to, label, depth):
    return {"id": f"{frm}>{to}:{label.split('(')[0]}#1", "from": frm, "to": to, "label": label, "depth": depth}


def model():
    comps = {c: {"name": c.title(), "path": f"internal/{c}", "kind": "internal"}
             for c in ("web", "auth", "orders", "vault")}
    return {
        "components": comps,
        "dependencies": [{"from": "web", "to": "auth"}, {"from": "web", "to": "orders"},
                         {"from": "orders", "to": "web"}, {"from": "web", "to": "vault"}],
        "types": {"internal/auth.Verifier": {"name": "Verifier", "component": "auth"}},
        "sequences": {
            "get-order": {"entry": "internal/web.(*App).getOrder", "messages": [
                msg("Caller", "web", "(*App).getOrder(w, r)", 0),
                msg("web", "internal/auth.Verifier", "(*Verifier).VerifySubject(ctx, raw)", 1),
                msg("web", "orders", "Find(id)", 1)]},
            "list-orders": {"entry": "internal/web.(*App).listOrders", "messages": [
                msg("Caller", "web", "(*App).listOrders(w, r)", 0), msg("web", "orders", "List()", 1)]},
            "export": {"entry": "internal/web.(*App).export", "messages": [
                msg("Caller", "web", "(*App).export(w, r)", 0), msg("web", "orders", "All()", 1)]},
            "health": {"entry": "internal/web.(*App).health", "messages": [
                msg("Caller", "web", "(*App).health(w, r)", 0)]},
        },
        "declarations": [
            decl("function", "internal/auth/verifier.go:VerifySubject", "guard", True,
                 file="internal/auth/verifier.go"),
            decl("entrypoint", "internal/web/orders.go:getOrder", "auth", "required", file="internal/web/orders.go"),
            decl("entrypoint", "internal/web/orders.go:listOrders", "auth", "required", file="internal/web/orders.go"),
            decl("entrypoint", "internal/web/health.go:health", "auth", "none", file="internal/web/health.go"),
            decl("entrypoint", "internal/web/hook.go:hook", "auth", "none", "signed by the IdP",
                 file="internal/web/hook.go"),
            decl("component", "web", "zone", "internet"),
            decl("component", "auth", "zone", "internal"),
            decl("component", "orders", "zone", "internal"),
            decl("component", "vault", "zone", "restricted"),
        ],
    }


def run(mod, m=None, **params):
    return mod.run(Ctx(m or model()), {**mod.PARAMS, **params})


class SecurityTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(short("internal/web/orders.go:getOrder"), "getOrder")
        self.assertEqual(short("internal/web.(*App).getOrder"), "getOrder")
        self.assertEqual(short("MyApp.Orders.ship/2"), "ship")
        self.assertEqual(called({"label": "(*Verifier).VerifySubject(ctx, raw)"}), "VerifySubject")
        self.assertEqual(called({"id": "web>orders:Find#2"}), "Find")

    def test_unguarded_entry(self):
        fs = {f["evidence"]["entry"]: f for f in run(security_unguarded_entry)}
        # getOrder calls the guard; health is auth=none.
        self.assertEqual(sorted(fs), ["internal/web.(*App).export", "internal/web.(*App).listOrders"])
        self.assertEqual(fs["internal/web.(*App).listOrders"]["strength"], "strong")
        self.assertEqual(fs["internal/web.(*App).listOrders"]["evidence"]["sequences"], ["list-orders"])
        self.assertEqual(fs["internal/web.(*App).export"]["strength"], "weak")  # no auth declaration
        self.assertEqual(fs["internal/web.(*App).export"]["group"], "security")
        self.assertEqual(len(run(security_unguarded_entry, report_undeclared=False)), 1)

    def test_unguarded_entry_needs_a_guard_in_the_right_component(self):
        m = model()
        m["sequences"]["get-order"]["messages"][1]["to"] = "orders"  # an unrelated VerifySubject
        fs = run(security_unguarded_entry, m)
        self.assertIn("internal/web.(*App).getOrder", [f["evidence"]["entry"] for f in fs])

    def test_no_guards_declared_reports_only_required(self):
        m = model()
        m["declarations"] = [d for d in m["declarations"] if d["key"] != "guard"]
        fs = run(security_unguarded_entry, m)
        self.assertEqual(sorted(f["strength"] for f in fs), ["strong", "strong"])

    def test_open_entry(self):
        fs = run(security_open_entry)
        self.assertEqual([f["evidence"]["entry"] for f in fs], ["internal/web/health.go:health"])
        self.assertEqual(fs[0]["components"], ["web"])
        self.assertEqual(fs[0]["evidence"]["declared_at"], "internal/web/health.go:8")

    def test_zone_dependency(self):
        fs = {(f["evidence"]["from"], f["evidence"]["to"]): f for f in run(security_zone_dependency)}
        self.assertEqual(sorted(fs), [("orders", "web"), ("web", "vault")])
        self.assertEqual(fs[("orders", "web")]["evidence"]["direction"], "outward")
        self.assertEqual(fs[("web", "vault")]["evidence"]["direction"], "skip")
        self.assertIn("past the internal zone", fs[("web", "vault")]["title"])
        self.assertEqual(len(run(security_zone_dependency, report_skip=False)), 1)
        self.assertEqual(run(security_zone_dependency, order=["a", "b"]), [])

    def test_unguarded_sink(self):
        m = model()
        seqs = m["sequences"]
        seqs["save-order"] = {"entry": "internal/web.(*App).saveOrder", "messages": [
            msg("Caller", "web", "(*App).saveOrder(w, r)", 0),
            msg("web", "internal/auth.Verifier", "(*Verifier).VerifySubject(ctx, raw)", 1),
            msg("web", "auth", "CanEdit(p, id)", 1), msg("web", "orders", "Save(o)", 1)]}
        seqs["save-settings"] = {"entry": "internal/web.(*App).saveSettings", "messages": [
            msg("Caller", "web", "(*App).saveSettings(w, r)", 0),
            msg("web", "internal/auth.Verifier", "(*Verifier).VerifySubject(ctx, raw)", 1),
            msg("web", "orders", "Save(o)", 1), msg("web", "auth", "CanEdit(p, id)", 1)]}
        m["declarations"][0]["value"] = "authn"
        m["declarations"] += [
            decl("function", "internal/auth/policy.go:CanEdit", "guard", "authz", file="internal/auth/policy.go"),
            decl("function", "internal/orders/store.go:Save", "sink", "write", file="internal/orders/store.go"),
            decl("function", "internal/orders/store.go:Find", "sink", "read", file="internal/orders/store.go")]
        fs = {(f["evidence"]["entry"], f["evidence"]["sink_kind"]): f for f in run(security_unguarded_sink, m)}
        # saveOrder checks before it saves; saveSettings checks after. getOrder only authenticates.
        self.assertEqual(sorted(fs), [("internal/web.(*App).getOrder", "read"),
                                      ("internal/web.(*App).saveSettings", "write")])
        self.assertEqual(fs[("internal/web.(*App).saveSettings", "write")]["strength"], "strong")
        self.assertEqual(fs[("internal/web.(*App).saveSettings", "write")]["components"], ["web", "orders"])
        self.assertEqual(fs[("internal/web.(*App).getOrder", "read")]["strength"], "medium")
        self.assertEqual(len(run(security_unguarded_sink, m, report_read=False)), 1)
        m["declarations"][0]["value"] = True  # a guard without a level also authorizes
        self.assertEqual(run(security_unguarded_sink, m), [])
        self.assertEqual(run(security_unguarded_sink), [])  # no sinks declared

    def test_sink_rules(self):
        m = model()
        m["sequences"] = {"login": {"entry": "internal/web.(*App).login", "messages": [
            msg("Caller", "web", "(*App).login(w, r)", 0),
            msg("web", "internal/auth.Verifier", "(*Verifier).VerifySubject(ctx, raw)", 1),
            msg("auth", "orders", "Find(id)", 2),  # the guard's own lookup
            msg("web", "orders", "List()", 1),  # read first, filter after
            msg("web", "auth", "CanEdit(p, id)", 1)]}}
        m["declarations"][0]["value"] = "authn"
        m["declarations"] += [
            decl("function", "internal/auth/policy.go:CanEdit", "guard", "authz", file="internal/auth/policy.go"),
            decl("function", "internal/orders/store.go:Find", "sink", "write", file="internal/orders/store.go"),
            decl("function", "internal/orders/store.go:List", "sink", "read", file="internal/orders/store.go")]
        self.assertEqual(run(security_unguarded_sink, m), [])
        del m["sequences"]["login"]["messages"][-1]
        self.assertEqual([f["title"] for f in run(security_unguarded_sink, m)],
                         ["login reads through List without any authorization check."])

    def test_untraced(self):
        m = model()
        m["declarations"] += [
            decl("function", "internal/auth/policy.go:CanEdit", "guard", "authz", file="internal/auth/policy.go"),
            decl("function", "internal/vault/seal.go:Seal", "guard", True, file="internal/vault/seal.go"),
            decl("entrypoint", "internal/web/admin.go:admin", "auth", "required", file="internal/web/admin.go")]
        fs = {(f["evidence"]["what"], f["evidence"]["target"]) for f in run(security_untraced, m)}
        # vault is in no trace: no finding for Seal. health has a trace; hook is in web, which the traces visit.
        self.assertEqual(fs, {("guard", "internal/auth/policy.go:CanEdit"), ("entry", "internal/web/admin.go:admin"),
                              ("entry", "internal/web/hook.go:hook")})
        self.assertEqual(len(run(security_untraced, m, report_entries=False)), 1)


if __name__ == "__main__":
    unittest.main()
