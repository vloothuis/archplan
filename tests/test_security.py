import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archplan import blast  # noqa: E402
from archplan.detectors import (security_gateway, security_open_entry, security_risk_hotspot,  # noqa: E402
                                security_unguarded_entry,
                                security_unchecked_entry, security_unguarded_path, security_unguarded_sink,
                                security_unchecked_input, security_untested_denial, security_untraced,
                                security_zone_dependency)
from archplan.detectors.common import graph  # noqa: E402
from archplan.detectors.security_common import called, short  # noqa: E402


class Ctx:
    def __init__(self, model, files=None):
        self.model = model
        self.files = files or {}

    def read(self, path):
        return self.files.get(path)

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


def run(mod, m=None, files=None, **params):
    return mod.run(Ctx(m or model(), files), {**mod.PARAMS, **params})


def run_ctx(mod, ctx, **params):
    return mod.run(ctx, {**mod.PARAMS, **params})


CONTROLLER = '''defmodule Shop.ExportController do
  use Shop, :controller
  plug :load_order when action in [:show]

  def export(conn, %{"id" => id}) do
    send_resp(conn, 200, Orders.list(id))
  end

  def submissions(conn, %{"id" => id}) do
    if allowed?(conn, id), do: send_resp(conn, 200, "ok"), else: forbidden(conn)
  end

  def show(conn, _params), do: send_resp(conn, 200, conn.assigns.order)

  defp allowed?(%{assigns: %{user: user}}, id),
    do: Policy.can_access?(user, id)

  defp load_order(conn, _opts) do
    if Policy.can_access?(conn.assigns.user, conn.params["id"]), do: conn, else: halt(conn)
  end

  defp forbidden(conn), do: send_resp(conn, 403, "")
end
'''

VIEWS = '''@login_required
def report(request, id):
    return render(request, Report.objects.get(pk=id))


@login_required
def invoice(request, id):
    invoice = Invoice.objects.get(pk=id)
    check_owner(request.user, invoice)
    return render(request, invoice)
'''


GO_ACTIONS = '''package dataentry

import (
	"net/http"
	"strings"

	"example.com/rela/internal/jobs"
)

func (h *Handler) handleV1Action(w http.ResponseWriter, r *http.Request) {
	ops := actionOps{store: h.store}
	name := strings.TrimSpace(r.URL.Path)
	if err := ops.run(r.Context(), name); err != nil {
		w.WriteHeader(http.StatusForbidden)
	}
}

func (h *Handler) handleV1Bulk(w http.ResponseWriter, r *http.Request) {
	ops := newActionOps(h.store)
	_ = ops.run(r.Context(), "bulk")
}

func (h *Handler) handleV1Export(w http.ResponseWriter, r *http.Request) {
	_ = h.exporter.run(r.Context())
}

func (h *Handler) handleV1Jobs(w http.ResponseWriter, r *http.Request, runner *jobs.Runner) {
	runner.run(r.Context())
}
'''

GO_OPS = '''package dataentry

type actionOps struct{ store Store }

func newActionOps(s Store) *actionOps { return &actionOps{store: s} }

func (o actionOps) run(ctx context.Context, name string) error {
	if err := o.authorize(ctx, name); err != nil {
		return err
	}
	return o.store.Do(name)
}
'''

GO_PERMS = '''package dataentry

func (o *actionOps) authorize(ctx context.Context, name string) error {
	if !holdsActionPermission(ctx, name) {
		return errDenied
	}
	return nil
}

func holdsActionPermission(ctx context.Context, name string) bool { return name != "" }
'''

GO_EXPORT = '''package dataentry

type exportOps struct{}

func (e *exportOps) run(ctx context.Context) error { return nil }
'''


def go_sig(sid, file, line, params=(), returns=None, comp="dataentry"):
    return {"id": sid, "component": comp, "file": file, "line": line, "name": short(sid),
            "params": [{"name": n, "type": t} for n, t in params], "returns": returns, "public": False}


GO_SIGS = [
    *[go_sig(f"internal/dataentry.(*Handler).{n}", "internal/dataentry/actions.go", line,
             [("w", "http.ResponseWriter"), ("r", "*http.Request")]
             + ([("runner", "*jobs.Runner")] if n == "handleV1Jobs" else []))
      for n, line in (("handleV1Action", 10), ("handleV1Bulk", 18), ("handleV1Export", 23), ("handleV1Jobs", 27))],
    go_sig("internal/dataentry.newActionOps", "internal/dataentry/actions_ops.go", 5, [("s", "Store")], "*actionOps"),
    go_sig("internal/dataentry.actionOps.run", "internal/dataentry/actions_ops.go", 7,
           [("ctx", "context.Context"), ("name", "string")], "error"),
    go_sig("internal/dataentry.(*actionOps).authorize", "internal/dataentry/perms.go", 3,
           [("ctx", "context.Context"), ("name", "string")], "error"),
    go_sig("internal/dataentry.holdsActionPermission", "internal/dataentry/perms.go", 10,
           [("ctx", "context.Context"), ("name", "string")], "bool"),
    go_sig("internal/dataentry.(*exportOps).run", "internal/dataentry/export_ops.go", 5,
           [("ctx", "context.Context")], "error"),
    go_sig("internal/jobs.(*Runner).run", "internal/jobs/run.go", 3, [("ctx", "context.Context")], comp="jobs"),
]


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

    def test_unguarded_entry_authz_per_request(self):
        m = model()
        m["declarations"][0]["value"] = "authn"
        m["declarations"].append(decl("function", "internal/auth/policy.go:Allow", "guard", "authz",
                                      file="internal/auth/policy.go"))
        m["declarations"][1]["value"] = "authz"  # getOrder
        # one test, two requests: the second checks the policy, the first does not
        m["sequences"] = {"orders": {"entry": "test", "messages": [
            msg("browser", "web", "Router.call(conn)", 0),
            msg("web", "auth", "VerifySubject(ctx, raw)", 1),
            msg("web", "web", "(*App).getOrder(w, r)", 1),
            msg("web", "orders", "Find(id)", 2),
            msg("browser", "web", "Router.call(conn)", 0),
            msg("web", "web", "(*App).listOrders(w, r)", 1),
            msg("web", "auth", "Allow(user, order)", 2)]}}
        fs = {f["evidence"]["entry"]: f for f in run(security_unguarded_entry, m)}
        self.assertEqual(list(fs), ["getOrder"])
        f = fs["getOrder"]
        self.assertEqual((f["strength"], f["evidence"]["auth"]), ("strong", "authz"))
        self.assertEqual(f["evidence"]["guards"], ["internal/auth/policy.go:Allow"])
        self.assertIn("authorization check", f["title"])
        # the authn guard on the same request does not count for authz
        m["sequences"]["orders"]["messages"][3:4] = [msg("web", "auth", "Allow(user, order)", 2)]
        self.assertEqual(run(security_unguarded_entry, m), [])
        # a hook that runs as its own step before the entry point belongs to its request
        m["sequences"]["orders"]["messages"] = [msg("browser", "web", "Hook.onMount(conn)", 0),
                                                msg("web", "auth", "Allow(user, order)", 1),
                                                msg("browser", "web", "(*App).getOrder(w, r)", 0)]
        self.assertEqual(run(security_unguarded_entry, m), [])

    def test_unchecked_entry(self):
        m = model()
        m["declarations"] = [
            decl("function", "lib/policy.ex:can_access?", "guard", "authz", file="lib/policy.ex"),
            decl("function", "shop/auth.py:check_owner", "guard", "authz", file="shop/auth.py"),
            decl("function", "shop/auth.py:login_required", "guard", "authn", file="shop/auth.py"),
            *[decl("entrypoint", f"lib/export_controller.ex:{n}", "auth", "authz", file="lib/export_controller.ex")
              for n in ("export", "submissions", "show")],
            *[decl("entrypoint", f"shop/views.py:{n}", "auth", "authz", file="shop/views.py")
              for n in ("report", "invoice")],
            decl("entrypoint", "lib/export_controller.ex:forbidden", "auth", "required",
                 file="lib/export_controller.ex")]
        files = {"lib/export_controller.ex": CONTROLLER, "shop/views.py": VIEWS}
        fs = {f["evidence"]["entry"]: f for f in run(security_unchecked_entry, m, files)}
        # submissions calls the guard through a helper; show through a plug; invoice directly.
        # login_required only authenticates.
        self.assertEqual(sorted(fs), ["lib/export_controller.ex:export", "shop/views.py:report"])
        self.assertEqual(fs["lib/export_controller.ex:export"]["strength"], "medium")
        self.assertEqual(fs["lib/export_controller.ex:export"]["evidence"]["read"], ["export"])  # the plug is for show
        fs = run(security_unchecked_entry, m, files, hooks=["Orders"])  # a hook checks what export names
        self.assertEqual([f["evidence"]["entry"] for f in fs], ["shop/views.py:report"])
        # a trace of a request that passes the guard clears it
        m["sequences"] = {"export": {"entry": "Shop.ExportController.export/2", "messages": [
            msg("browser", "web", "Shop.ExportController.export/2", 0),
            msg("web", "auth", "Policy.can_access?/2", 1)]}}
        m["components"]["web"]["path"] = "lib"
        m["components"]["auth"]["path"] = "lib/policy.ex"
        fs = run(security_unchecked_entry, m, files)
        self.assertEqual([f["evidence"]["entry"] for f in fs], ["shop/views.py:report"])

    def test_unchecked_entry_follows_the_package(self):
        go = {
            "internal/dataentry/actions.go": GO_ACTIONS, "internal/dataentry/actions_ops.go": GO_OPS,
            "internal/dataentry/perms.go": GO_PERMS, "internal/dataentry/export_ops.go": GO_EXPORT,
            "internal/jobs/run.go": "package jobs\n\nfunc (r *Runner) run(ctx context.Context) {\n"
                                    "\tholdsActionPermission(ctx, \"x\")\n}\n"}
        m = model()
        m["components"]["dataentry"] = {"name": "Dataentry", "path": "internal/dataentry", "kind": "internal"}
        m["components"]["jobs"] = {"name": "Jobs", "path": "internal/jobs", "kind": "internal"}
        m["signatures"] = GO_SIGS
        m["declarations"] = [
            decl("function", "internal/dataentry.holdsActionPermission", "guard", "authz",
                 file="internal/dataentry/perms.go"),
            *[decl("entrypoint", f"internal/dataentry.(*Handler).{n}", "auth", "authz",
                   file="internal/dataentry/actions.go", name=n)
              for n in ("handleV1Action", "handleV1Bulk", "handleV1Export", "handleV1Jobs")]]
        fs = {f["evidence"]["entry"]: f for f in run(security_unchecked_entry, m, go)}
        # handleV1Action → actionOps.run (actions_ops.go) → actionOps.authorize (perms.go) → the guard.
        # handleV1Bulk gets its actionOps from newActionOps. handleV1Export calls run on a field of unknown
        # type: two types have a run method, so it reads neither. handleV1Jobs calls run on a jobs.Runner.
        self.assertEqual(sorted(fs), ["internal/dataentry.(*Handler).handleV1Export",
                                      "internal/dataentry.(*Handler).handleV1Jobs"])
        self.assertEqual(fs["internal/dataentry.(*Handler).handleV1Export"]["evidence"]["read"],
                         ["handleV1Export"])
        self.assertEqual(fs["internal/dataentry.(*Handler).handleV1Jobs"]["evidence"]["read"], ["handleV1Jobs"])
        # The cap counts the functions of all files.
        fs = {f["evidence"]["entry"]: f for f in run(security_unchecked_entry, m, go, max_functions=2)}
        self.assertIn("internal/dataentry.(*Handler).handleV1Action", fs)
        self.assertEqual(fs["internal/dataentry.(*Handler).handleV1Action"]["evidence"]["read"],
                         ["handleV1Action", "actionOps.run"])
        self.assertEqual(fs["internal/dataentry.(*Handler).handleV1Action"]["evidence"]["files"],
                         ["internal/dataentry/actions_ops.go"])
        # Without signatures it reads only the entry point's file.
        del m["signatures"]
        self.assertEqual(len(run(security_unchecked_entry, m, go)), 4)

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

    def test_untested_denial(self):
        m = model()
        m["declarations"].append(
            decl("function", "internal/orders/store.go:Find", "sink", "read", file="internal/orders/store.go"))
        # getOrder passes the guard; listOrders passes none (unguarded_entry's); export is undeclared.
        [f] = run(security_untested_denial, m)
        self.assertEqual(f["evidence"]["entry"], "internal/web/orders.go:getOrder")
        self.assertEqual(f["evidence"]["sequences"], ["get-order"])
        self.assertEqual(f["evidence"]["guards"], ["internal/auth/verifier.go:VerifySubject"])
        self.assertEqual(f["evidence"]["sinks"], ["internal/orders/store.go:Find"])
        self.assertIn("expect Find not to be called", f["evidence"]["moves"][0])
        self.assertEqual(f["strength"], "weak")  # no outcomes: test titles only
        self.assertIn("only test titles", f["detail"])
        seqs = m["sequences"]
        # A failed test does not count; a passing one titled as a refusal does.
        seqs["get-order-denied"] = {"title": "rejects another user (failed)",
                                    "entry": "internal/web.(*App).getOrder", "messages": [
                                        msg("Caller", "web", "(*App).getOrder(w, r)", 0)]}
        self.assertEqual(len(run(security_untested_denial, m)), 1)
        seqs["get-order-denied"]["title"] = "rejects another user"
        self.assertEqual(run(security_untested_denial, m), [])
        # With outcomes, a guard that refused clears it, and findings are medium.
        del seqs["get-order-denied"]
        seqs["list-orders"]["messages"][0]["outcome"] = "status:404"
        [f] = run(security_untested_denial, m)
        self.assertEqual(f["strength"], "medium")
        seqs["get-order-anon"] = {"title": "get order", "entry": "internal/web.(*App).getOrder", "messages": [
            msg("Caller", "web", "(*App).getOrder(w, r)", 0),
            {**msg("web", "internal/auth.Verifier", "(*Verifier).VerifySubject(ctx, raw)", 1), "outcome": "error"}]}
        self.assertEqual(run(security_untested_denial, m), [])
        # A refusal by another call does not count; one by the entry point does.
        seqs["get-order-anon"]["messages"][1] = {**msg("web", "orders", "Find(id)", 1), "outcome": "raise"}
        self.assertEqual(len(run(security_untested_denial, m)), 1)
        seqs["get-order-anon"]["messages"][0]["outcome"] = "status:403"
        self.assertEqual(run(security_untested_denial, m), [])

    def test_guards_off(self):
        m = model()
        m["sequences"]["list-orders"]["guards"] = "off"  # a test server without an access policy
        fs = [f["evidence"]["entry"] for f in run(security_unguarded_entry, m)]
        self.assertEqual(fs, ["internal/web.(*App).export"])
        es = {e["entry"]: e for e in blast.entries(Ctx(m))}
        lst = es["internal/web/orders.go:listOrders"]
        self.assertEqual((lst["gaps"], lst["sequences"], lst["components"]), ([], ["list-orders"], ["web", "orders"]))
        # a run with guards on still decides
        m["sequences"]["list-orders-acl"] = {**m["sequences"]["list-orders"], "guards": None}
        self.assertIn("internal/web.(*App).listOrders",
                      [f["evidence"]["entry"] for f in run(security_unguarded_entry, m)])

    def test_refused_before_a_sink(self):
        m = model()
        m["sequences"]["list-orders"]["messages"][0]["outcome"] = "status:405"  # a method check
        fs = [f["evidence"]["entry"] for f in run(security_unguarded_entry, m)]
        self.assertEqual(fs, ["internal/web.(*App).export"])
        self.assertEqual({e["entry"]: e for e in blast.entries(Ctx(m))}["internal/web/orders.go:listOrders"]["gaps"],
                         [])
        # refused after it reached a sink: still judged
        m["declarations"].append(
            decl("function", "internal/orders/store.go:List", "sink", "read", file="internal/orders/store.go"))
        self.assertIn("internal/web.(*App).listOrders",
                      [f["evidence"]["entry"] for f in run(security_unguarded_entry, m)])

    def test_blast_radius(self):
        m = model()
        m["declarations"] += [
            decl("function", "internal/orders/store.go:Find", "sink", "read", file="internal/orders/store.go"),
            decl("function", "internal/orders/store.go:All", "sink", "write", file="internal/orders/store.go")]
        ctx = Ctx(m)
        es = {e["entry"]: e for e in blast.entries(ctx)}
        get, lst = es["internal/web/orders.go:getOrder"], es["internal/web/orders.go:listOrders"]
        self.assertEqual(get["components"], ["web", "auth", "orders"])
        self.assertEqual(get["guards"], ["internal/auth/verifier.go:VerifySubject"])
        self.assertEqual(get["sinks"], [{"id": "internal/orders/store.go:Find", "kind": "read"}])
        self.assertEqual(get["zones"], ["internet", "internal"])
        self.assertEqual(get["gaps"], ["no refusal"])
        self.assertEqual(lst["gaps"], ["unguarded"])
        self.assertEqual(es["internal/web/health.go:health"]["gaps"], [])  # auth=none
        # export writes with no guard and no auth declaration: the riskiest.
        self.assertEqual(blast.entries(ctx)[0]["entry"], "internal/web.(*App).export")
        self.assertEqual(blast.entries(ctx)[0]["risk"], 30 + 10 + 5 + 30)  # write, undeclared, two zones, unguarded
        p = {x["entry"]: x for x in blast.plan(blast.entries(ctx))}
        self.assertEqual(sorted(p), ["internal/web.(*App).export", "internal/web/orders.go:getOrder",
                                     "internal/web/orders.go:listOrders"])
        self.assertIn("expect internal/orders/store.go:Find not to be called",
                      p["internal/web/orders.go:getOrder"]["tests"][0])
        self.assertEqual(p["internal/web/orders.go:getOrder"]["start_from"], "get-order")
        # A change to the guard's file touches only the request that passes it; one in orders touches all three.
        t = blast.touched(ctx, list(es.values()), ["internal/auth/verifier.go"])
        self.assertEqual([e["entry"] for e in t], ["internal/web/orders.go:getOrder"])
        self.assertEqual(t[0]["because"], ["internal/auth/verifier.go is on its path"])
        t = blast.touched(ctx, list(es.values()), ["internal/orders/other.go"])
        self.assertEqual(len(t), 3)
        rep = blast.report(ctx, ["internal/auth/verifier.go"])
        self.assertEqual([x["entry"] for x in rep["plan"]], ["internal/web/orders.go:getOrder"])
        self.assertIn("1 changed files touch 1 of 4", blast.text(rep))
        # A traced refusal closes the gap.
        m["sequences"]["get-order-anon"] = {"entry": "internal/web.(*App).getOrder", "messages": [
            msg("Caller", "web", "(*App).getOrder(w, r)", 0),
            {**msg("web", "internal/auth.Verifier", "(*Verifier).VerifySubject(ctx, raw)", 1), "outcome": "error"}]}
        self.assertEqual({e["entry"]: e for e in blast.entries(ctx)}["internal/web/orders.go:getOrder"]["gaps"], [])

    def test_blast_untraced_hint_follows_language(self):
        def hint(source):
            return blast.text(blast.report(Ctx(dict(model(), sequences={}, source=source)), []))
        self.assertIn("archplan trace-elixir", hint({"languages": ["elixir"]}))
        self.assertIn("seqtrace", hint({"languages": ["go"]}))
        self.assertNotIn("trace-elixir", hint({"languages": ["go"]}))
        self.assertIn("archplan trace-python", hint({"languages": ["python"]}))
        for source in ({"languages": ["typescript"]}, {}, None):
            h = hint(source)
            self.assertIn("exporter-plugins.md", h)
            self.assertNotIn("elixir", h)

    def test_risk_hotspot(self):
        m = model()
        m["declarations"].append(
            decl("function", "internal/orders/store.go:All", "sink", "write", file="internal/orders/store.go"))
        ctx = Ctx(m)
        ctx.churn = {"web": 40, "orders": 30, "auth": 4, "vault": 2}
        # Only the top quarter by commits: web, not orders.
        fs = run_ctx(security_risk_hotspot, ctx)
        self.assertEqual([f["components"] for f in fs], [["web"]])
        self.assertEqual(fs[0]["evidence"]["writes"], ["internal/orders/store.go:All"])
        self.assertIn("1 traced entry point passes it to reach 1 write sink; it lacks a guard", fs[0]["detail"])
        ctx.churn["orders"] = 40
        self.assertEqual(sorted(f["components"][0] for f in run_ctx(security_risk_hotspot, ctx)), ["orders", "web"])
        self.assertEqual(run_ctx(security_risk_hotspot, ctx, min_commits=41), [])
        ctx.churn = {}
        self.assertEqual(run_ctx(security_risk_hotspot, ctx), [])

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

    def paths_model(self):
        m = model()
        for c in ("export", "audit", "memvault"):
            m["components"][c] = {"name": c.title(), "path": f"internal/{c}", "kind": "internal"}
        m["dependencies"] += [{"from": "web", "to": "export"}, {"from": "export", "to": "vault"},
                              {"from": "web", "to": "audit"}, {"from": "audit", "to": "auth"},
                              {"from": "audit", "to": "vault"}, {"from": "memvault", "to": "vault"}]
        m["types"].update({"internal/vault.Store": {"name": "Store", "component": "vault", "kind": "interface"},
                           "internal/memvault.Mem": {"name": "Mem", "component": "memvault",
                                                     "implements": ["internal/vault.Store"]}})
        return m

    def test_gateway_bypass(self):
        m = self.paths_model()
        self.assertEqual(run(security_gateway, m), [])  # no gateways declared
        m["declarations"].append(decl("component", "vault", "gateway", ["audit", "nowhere"]))
        m["sequences"]["seal"] = {"entry": "internal/web.(*App).seal", "messages": [
            msg("Caller", "web", "(*App).seal(w, r)", 0), msg("web", "vault", "Seal()", 1)]}
        fs = {(f["evidence"]["from"], f["evidence"]["to"]): f for f in run(security_gateway, m)}
        # audit is the gateway; memvault implements vault's interface.
        self.assertEqual(sorted(fs), [("export", "vault"), ("web", "vault")])
        self.assertEqual(fs[("export", "vault")]["strength"], "weak")
        self.assertEqual(fs[("web", "vault")]["strength"], "strong")  # a trace shows the call
        self.assertEqual(fs[("web", "vault")]["evidence"]["sequences"], ["seal"])
        self.assertEqual(fs[("web", "vault")]["evidence"]["gateways"], ["audit"])
        self.assertEqual(fs[("export", "vault")]["title"], "Export depends on Vault directly, not through Audit.")

    def test_unguarded_path(self):
        m = self.paths_model()
        fs = run(security_unguarded_path, m)
        # web → vault is the entry component's own call; audit depends on the guard's component.
        self.assertEqual([(f["evidence"]["from"], f["evidence"]["to"]) for f in fs], [("export", "vault")])
        self.assertEqual(fs[0]["evidence"]["path"], ["export", "vault"])
        self.assertEqual(fs[0]["evidence"]["entries"], ["web"])
        self.assertEqual(fs[0]["strength"], "weak")
        m["dependencies"] = [d for d in m["dependencies"] if d != {"from": "export", "to": "vault"}]
        m["dependencies"] += [{"from": "export", "to": "orders"}, {"from": "orders", "to": "vault"}]
        # web also calls orders, so orders → vault is the finding and export's path repeats it.
        self.assertEqual([f["evidence"]["path"] for f in run(security_unguarded_path, m)], [["orders", "vault"]])
        m["dependencies"] = [d for d in m["dependencies"] if d not in ({"from": "web", "to": "orders"},
                                                                     {"from": "orders", "to": "web"})]
        self.assertEqual([f["evidence"]["path"] for f in run(security_unguarded_path, m)],
                         [["export", "orders", "vault"]])
        self.assertEqual(run(security_unguarded_path, m, max_hops=1), [])
        m["declarations"].append(decl("component", "vault", "gateway", ["orders"]))
        self.assertEqual(run(security_unguarded_path, m), [])  # the path passes a gateway
        m["declarations"] = [d for d in m["declarations"] if d["key"] not in ("zone", "gateway")]
        self.assertEqual(run(security_unguarded_path, m), [])  # no restricted zone: no targets
        m["declarations"].append(decl("function", "internal/vault/seal.go:Seal", "sink", "write",
                                      file="internal/vault/seal.go"))
        self.assertEqual(run(security_unguarded_path, m), [])
        self.assertEqual(len(run(security_unguarded_path, m, sink_targets=True)), 1)

    def test_unguarded_path_gated_by(self):
        m = self.paths_model()
        m["components"]["visibility"] = {"name": "Visibility", "path": "internal/visibility", "kind": "internal"}
        m["dependencies"].append({"from": "visibility", "to": "export"})  # it wraps export at wiring
        m["declarations"].append(decl("component", "export", "gated_by", ["visibility", "nowhere"]))
        # web does not depend on visibility, so nothing says it gets export wrapped.
        fs = run(security_unguarded_path, m)
        self.assertEqual([(f["evidence"]["from"], f["evidence"]["to"]) for f in fs], [("export", "vault")])
        m["dependencies"] += [{"from": "orders", "to": "visibility"}]
        self.assertEqual(run(security_unguarded_path, m), [])  # web → orders → visibility
        m["dependencies"].remove({"from": "orders", "to": "visibility"})
        m["declarations"] = [d for d in m["declarations"] if d["key"] != "gated_by"]
        m["declarations"].append(decl("component", "vault", "gated_by", ["visibility"]))
        self.assertEqual(len(run(security_unguarded_path, m)), 1)
        m["dependencies"].append({"from": "web", "to": "visibility"})
        self.assertEqual(run(security_unguarded_path, m), [])  # the target itself is gated
        # gated_by is not a gateway: it does not change security.gateway_bypass.
        self.assertEqual(run(security_gateway, m), [])
        m["declarations"].append(decl("component", "vault", "gateway", ["audit"]))
        self.assertEqual(sorted((f["evidence"]["from"], f["evidence"]["to"]) for f in run(security_gateway, m)),
                         [("export", "vault"), ("web", "vault")])

    def input_model(self):
        m = model()
        entry = "internal/web.(*App).search"

        def seq(*calls, entry=entry):
            return {"entry": entry, "messages": [msg("Caller", "web", "(*App).search(w, r)", 0)] + [
                msg(*c) if len(c) == 4 else msg("web", *c) for c in calls]}
        m["sequences"] = {
            "unchecked": seq(("orders", "Load(id)", 1), ("orders", "Query(q)", 1)),
            "checked": seq(("auth", "Clean(form)", 1), ("orders", "Query(q)", 1)),
            "own-calls": seq(("auth", "Clean(form)", 1), ("auth", "orders", "Query(q)", 2)),
            "wrong-kind": seq(("auth", "Escape(s)", 1), ("orders", "Query(q)", 1), ("vault", "Render(s)", 1)),
            "trusting": seq(("orders", "Apply(o)", 1)),
            "second-step": {"entry": entry, "messages": [
                msg("Caller", "web", "(*App).search(w, r)", 0), msg("web", "auth", "Clean(form)", 1),
                msg("Caller", "web", "(*App).search(w, r)", 0), msg("web", "orders", "Query(q)", 1)]},
            "hook": {"messages": [msg("Caller", "web", "Tick()", 0), msg("web", "orders", "Query(q)", 1),
                                  msg("web", "auth", "ReadHook(r)", 1), msg("web", "vault", "Exec(cmd)", 1)]},
        }
        m["declarations"] += [
            decl("function", "internal/auth/forms.go:Clean", "validates", True, file="internal/auth/forms.go"),
            decl("function", "internal/auth/forms.go:Escape", "validates", "html", file="internal/auth/forms.go"),
            decl("function", "internal/auth/hooks.go:ReadHook", "source", True, file="internal/auth/hooks.go"),
            decl("function", "internal/orders/store.go:Query", "interprets", "sql", file="internal/orders/store.go"),
            decl("function", "internal/orders/store.go:Apply", "trusts", True, file="internal/orders/store.go"),
            decl("function", "internal/vault/run.go:Exec", "interprets", "shell", file="internal/vault/run.go"),
            decl("function", "internal/vault/view.go:Render", "interprets", "html", file="internal/vault/view.go")]
        return m

    def test_unchecked_input(self):
        fs = run(security_unchecked_input, self.input_model())
        got = {(f["evidence"]["entry"], f["evidence"]["kind"]): f["evidence"]["sequences"] for f in fs}
        # checked and own-calls pass a validator first; Escape covers Render, not Query; the hook
        # sequence has no entry, so only the source's input counts.
        self.assertEqual(got, {
            ("internal/web.(*App).search", "sql"): ["second-step", "unchecked", "wrong-kind"],
            ("internal/web.(*App).search", "trusts"): ["trusting"],
            ("internal/auth/hooks.go:ReadHook", "shell"): ["hook"]})
        f = {x["evidence"]["kind"]: x for x in fs}
        self.assertEqual(f["sql"]["title"], "(*App).search passes input to Query, which reads it as sql, before any check.")
        self.assertEqual([r["function"] for r in f["sql"]["evidence"]["review"]], ["web: search"])
        self.assertEqual(f["shell"]["strength"], "strong")
        self.assertEqual(f["trusts"]["components"], ["web", "orders"])
        self.assertEqual(len(run(security_unchecked_input, self.input_model(), report_trusts=False)), 2)
        self.assertEqual(run(security_unchecked_input), [])  # nothing declared

    def test_unchecked_input_chain(self):
        m = self.input_model()
        m["sequences"] = {"nested": {"entry": "internal/web.(*App).search", "messages": [
            msg("Caller", "web", "(*App).search(w, r)", 0), msg("web", "auth", "Lookup(id)", 1),
            msg("web", "orders", "Load(id)", 1), msg("orders", "orders", "build(q)", 2),
            msg("orders", "orders", "Query(q)", 3)]}}
        f = run(security_unchecked_input, m)[0]
        # Lookup ran before, but does not pass the input on.
        self.assertEqual([r["function"] for r in f["evidence"]["review"]],
                         ["web: search", "orders: Load", "orders: build"])
        self.assertIn("through 3 functions: web: search, orders: Load, orders: build.", f["detail"])
        self.assertEqual(f["evidence"]["entry"], "internal/web.(*App).search")
        # A declared entry point names the request.
        m["declarations"].append(
            decl("entrypoint", "internal/web/search.go:search", "auth", "none", "public", file="internal/web/search.go"))
        self.assertEqual(run(security_unchecked_input, m)[0]["evidence"]["entry"], "internal/web/search.go:search")

    def test_blast_unchecked_input(self):
        m = self.input_model()
        m["signatures"] = [{"id": "internal/web.(*App).search", "file": "internal/web/search.go", "component": "web",
                            "name": "search"}]
        m["declarations"].append(
            decl("entrypoint", "internal/web/search.go:search", "auth", "none", "public", file="internal/web/search.go"))
        ctx = Ctx(m)
        es = {e["entry"]: e for e in blast.entries(ctx)}
        e = es["internal/web/search.go:search"]
        self.assertIn("unchecked input", e["gaps"])
        self.assertEqual(sorted(u["kind"] for u in e["unchecked"]), ["sql", "trusts"])
        self.assertEqual(e["review"][0], {"function": "web: search", "file": "internal/web/search.go"})
        p = {x["entry"]: x for x in blast.plan(blast.entries(ctx))}["internal/web/search.go:search"]
        self.assertIn("holds a quote", p["tests"][-1])
        self.assertIn("Review first, as they pass its unchecked input on: web: search",
                      blast.text(blast.report(ctx, ["internal/web/search.go"])))
        t = blast.touched(ctx, [e], ["internal/web/search.go"])
        self.assertEqual(t[0]["because"], ["internal/web/search.go passes its unchecked input on"])


if __name__ == "__main__":
    unittest.main()
