package main

import (
	"context"
	"path/filepath"
	"slices"
	"testing"
)

// exportShop exports testdata/shop: an HTTP layer that registers routes on
// a ServeMux, a store with boolean flags, and an order type with a string
// state.
func exportShop(t *testing.T) *Model {
	t.Helper()
	if testing.Short() {
		t.Skip("runs go list -export")
	}
	root, err := filepath.Abs(filepath.Join("testdata", "shop"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := Export(context.Background(), Options{Root: root, Patterns: []string{"./..."}, NoVocab: true})
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func TestCodeSections(t *testing.T) {
	m := exportShop(t)

	t.Run("capabilities", func(t *testing.T) {
		for _, c := range []string{"components", "deps", "files", "types", "signatures", "literals"} {
			if !slices.Contains(m.Source.Capabilities, c) {
				t.Errorf("capabilities %v lack %s", m.Source.Capabilities, c)
			}
		}
		if slices.Contains(m.Source.Capabilities, "calls") {
			t.Errorf("calls without sequences: %v", m.Source.Capabilities)
		}
	})

	t.Run("files", func(t *testing.T) {
		orders, ok := m.Files["api/orders.go"]
		if !ok || orders.Component != "api" {
			t.Fatalf("api/orders.go = %+v (present %v)", orders, ok)
		}
		want := []Ref{{To: "api/routes.go", Line: 10}, {To: "order/order.go", Line: 12}, {To: "store/store.go", Line: 11}}
		if !slices.Equal(orders.Refs, want) {
			t.Errorf("api/orders.go refs = %+v, want %+v", orders.Refs, want)
		}
		if f, ok := m.Files["order/order.go"]; !ok || len(f.Refs) != 0 || f.Refs == nil {
			t.Errorf("order/order.go = %+v, want an entry with an empty refs list", f)
		}
		if _, ok := m.Files["order/order_test.go"]; ok {
			t.Error("test files have no entry")
		}
	})

	t.Run("signatures", func(t *testing.T) {
		by := map[string]Signature{}
		for _, s := range m.Signatures {
			by[s.ID] = s
		}
		put, ok := by["store.(*DB).Put"]
		if !ok || !put.Public || put.File != "store/store.go" || put.Line != 15 || put.Component != "store" {
			t.Fatalf("Put = %+v (present %v)", put, ok)
		}
		if len(put.Params) != 3 || put.Params[1] != (Param{"upsert", "bool"}) || put.Returns == nil || *put.Returns != "error" {
			t.Errorf("Put params %+v returns %v", put.Params, put.Returns)
		}
		if get := by["api.(*App).getOrder"]; get.Public || get.Returns != nil {
			t.Errorf("getOrder = %+v, want private without returns", get)
		}
		if s, ok := by["order.Order.Paid"]; !ok || !s.Public {
			t.Errorf("value receiver: %+v (present %v)", s, ok)
		}
		if r := by["store.(*DB).Get"].Returns; r == nil || *r != "(order.Order, error)" {
			t.Errorf("Get returns %v", r)
		}
	})

	t.Run("literals", func(t *testing.T) {
		type key struct{ value, context, field, file string }
		got := map[key]bool{}
		for _, l := range m.Literals {
			got[key{l.Value, l.Context, l.Field, l.File}] = true
			if l.File == "order/order_test.go" {
				t.Errorf("literal from a test file: %+v", l)
			}
		}
		for _, k := range []key{
			{"paid", "compare", "State", "order/order.go"},
			{"shipped", "case", "State", "store/store.go"},
			{"closed", "assign", "State", "store/store.go"},
			{"draft", "assign", "State", "api/orders.go"},
		} {
			if !got[k] {
				t.Errorf("missing literal %+v in %+v", k, m.Literals)
			}
		}
		if got[key{"not found", "compare", "", "api/orders.go"}] {
			t.Error("values with whitespace are left out")
		}
	})

	t.Run("routes", func(t *testing.T) {
		want := []Route{
			{Method: "GET", Pattern: "/api/v1/orders/{id}", Handler: "api.(*App).getOrder", Component: "api", File: "api/orders.go", Line: 10},
			{Method: "POST", Pattern: "/api/v1/orders", Handler: "api.(*App).createOrder", Component: "api", File: "api/orders.go", Line: 18},
			{Method: "*", Pattern: "/healthz", Component: "api", File: "api/routes.go", Line: 21},
		}
		if !slices.Equal(m.Routes, want) {
			t.Errorf("routes = %+v\nwant %+v", m.Routes, want)
		}
	})
}

func TestSplitPattern(t *testing.T) {
	for in, want := range map[string][2]string{
		"GET /a/{id}":               {"GET", "/a/{id}"},
		"/a/":                       {"*", "/a/"},
		"post example.com/a":        {"POST", "/a"},
		"example.com/static/{p...}": {"*", "/static/{p...}"},
	} {
		if m, p := splitPattern(in); m != want[0] || p != want[1] {
			t.Errorf("splitPattern(%q) = %q %q, want %v", in, m, p, want)
		}
	}
}
