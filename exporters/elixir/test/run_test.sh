#!/usr/bin/env bash
# Runs the Elixir exporter and tracer on a small Mix project and checks the
# output. Needs elixir, python3 and network access to hex.pm the first time
# (the fixture depends on ecto).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
exporter="$repo/src/archplan/elixir"
schema="$repo/src/archplan/format/archmodel.schema.json"
work="${TMPDIR:-/tmp}/archplan-elixir-test"

rm -rf "$work"
mkdir -p "$work"
cp -R "$here/fixture" "$work/repo"
cd "$work/repo"
git init -q . && git add -A && git -c user.email=t@t -c user.name=t commit -qm fixture

mix deps.get >/dev/null
MIX_ENV=test mix compile >/dev/null

# Trace: once without the test process, once with it.
ARCHPLAN_TRACE_DIR="$work/seq1" MIX_ENV=test elixir -r "$exporter/archplan_trace.ex" -S \
  mix test --formatter ArchplanTrace --formatter ExUnit.CLIFormatter >/dev/null
ARCHPLAN_TRACE_DIR="$work/seq2" ARCHPLAN_TRACE_TEST=1 MIX_ENV=test elixir -r "$exporter/archplan_trace.ex" -S \
  mix test --formatter ArchplanTrace --formatter ExUnit.CLIFormatter >/dev/null

# And with the canary.
ARCHPLAN_TRACE_DIR="$work/seq3" ARCHPLAN_TRACE_TEST=1 ARCHPLAN_CANARY=1 MIX_ENV=test \
  elixir -r "$exporter/archplan_trace.ex" -S mix test --formatter ArchplanTrace --formatter ExUnit.CLIFormatter >/dev/null

MIX_ENV=test sh "$exporter/archplan-elixir" export -o "$work/model1.json" --sequences "$work/seq1"
MIX_ENV=test sh "$exporter/archplan-elixir" export --sequences "$work/seq2" > "$work/model2.json"
MIX_ENV=test sh "$exporter/archplan-elixir" export --sequences "$work/seq3" > "$work/model3.json"

python3 - "$work" "$schema" <<'EOF'
import json, sys
work, schema = sys.argv[1], sys.argv[2]
m1 = json.load(open(f"{work}/model1.json"))
m2 = json.load(open(f"{work}/model2.json"))
m3 = json.load(open(f"{work}/model3.json"))

def check(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)

check(m1["format"] == "archmodel/1", "format")
check(set(m1["components"]) == {"Shop.Accounts", "Shop.Orders"}, f"components {sorted(m1['components'])}")
check(m1["components"]["Shop.Orders"]["path"] == "lib/shop/orders", f"path {m1['components']['Shop.Orders']}")
deps = {(d["from"], d["to"]): d for d in m1["dependencies"]}
check(("Shop.Orders", "Shop.Accounts") in deps, f"orders -> accounts dependency: {deps.keys()}")
check(("Shop.Accounts", "Shop.Orders") in deps, "accounts -> orders dependency (behaviour, schema assoc)")
check(any(e.startswith("lib/shop/orders.ex:") for e in deps[("Shop.Orders", "Shop.Accounts")]["evidence"]),
      "evidence with line")

t = m1["types"]
user = t["Shop.Accounts.User"]
check(user["kind"] == "struct" and user["component"] == "Shop.Accounts", "user type")
orders = [f for f in user["fields"] if f["name"] == "orders"][0]
check(orders == {"name": "orders", "ref": "Shop.Orders.Order", "many": True}, f"has_many field {orders}")
order_user = [f for f in t["Shop.Orders.Order"]["fields"] if f["name"] == "user"][0]
check(order_user == {"name": "user", "ref": "Shop.Accounts.User"}, f"belongs_to field {order_user}")
check(t["Shop.Orders.Notifier"]["kind"] == "interface", "behaviour is an interface")
check(t["Shop.Orders.Notifier"]["methods"][0]["name"].startswith("notify/"), "callback as method")
check("Shop.Orders.Notifier" in t["Shop.Accounts.EmailNotifier"]["implements"], "behaviour implementer")
check(t["Shop.Orders.Priced"]["kind"] == "interface", "protocol is an interface")
check("Shop.Orders.Priced" in t["Shop.Orders.Order"]["implements"], "protocol implementation")
check(all("/" in m["name"] and not m.get("sig", "(").startswith(m["name"].split("/")[0])
          for ty in t.values() for m in ty.get("methods", [])), "method names carry the arity; sigs omit the name")
check(t["Shop.Orders.Order"]["name"] == "Order", f"short type name {t['Shop.Orders.Order']['name']}")
cart = {f["name"]: f for f in t["Shop.Orders.Cart"]["fields"]}
check(cart["owner"].get("ref") == "Shop.Accounts.User", f"struct field ref from @type t: {cart}")
check(cart["items"].get("many") is True, "list field is many")
check(t["Shop.Accounts.User"]["file"] == "lib/shop/accounts/user.ex" and t["Shop.Accounts.User"]["line"] == 1, "file/line")

s1 = m1["sequences"]
check(list(s1) == ["orders-place-an-order-in-a-task"], f"sequences without test process: {list(s1)}")
seq = s1["orders-place-an-order-in-a-task"]
first = seq["messages"][0]
check(first["from"] == "process" and first["to"] == "Shop.Orders" and first.get("async") is True, f"root {first}")
labels = [(m["from"], m["to"], m["label"], m["depth"]) for m in seq["messages"]]
check(("Shop.Orders", "Shop.Accounts", "Shop.Accounts.get_user/1", 1) in labels, f"cross-component call {labels}")
check(all(m["from"] != m["to"] for m in seq["messages"]), "no same-component messages")
s2 = m2["sequences"]
check("orders-place-an-order" in s2 and s2["orders-place-an-order"]["messages"][0]["from"] == "test",
      f"test-process sequence {list(s2)}")
check(s2["orders-place-an-order"]["messages"][0]["id"] == "test>Shop.Orders:Shop.Orders.place/2#1", "message id")
check(not any("taint" in m or "interprets" in m for s in s2.values() for m in s["messages"]), "no canary notes without it")
r = {m["label"]: m for m in m3["sequences"]["orders-resume-a-session"]["messages"]}
check(r["Shop.Orders.resume/1"].get("taint") == ["1"], f"tainted argument {r['Shop.Orders.resume/1']}")
check(r["Shop.Accounts.load_session/1"].get("interprets") ==
      [{"kind": "deserialize", "call": "erlang.binary_to_term", "canary": "raw"}], f"raw sink {r}")
check(r["Shop.Accounts.greeting/1"].get("interprets") ==
      [{"kind": "code", "call": "Code.eval_string", "canary": "escaped"}], f"escaped sink {r}")

print("checks: ok")
EOF

# Validate against the schema with the first python that has jsonschema.
for py in python3 python3.13 python3.12 python3.11; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import jsonschema' 2>/dev/null; then
    "$py" - "$work" "$schema" <<'EOF'
import json, sys, jsonschema
work, schema = sys.argv[1], json.load(open(sys.argv[2]))
for f in ("model1.json", "model2.json", "model3.json"):
    jsonschema.validate(json.load(open(f"{work}/{f}")), schema)
print("schema: valid")
EOF
    echo ok
    exit 0
  fi
done
echo "schema: no python with jsonschema, skipped"
echo ok
