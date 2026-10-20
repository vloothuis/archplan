package api

import (
	"net/http"

	"example.com/shop/order"
)

// archplan: auth=authz
func (a *App) getOrder(w http.ResponseWriter, r *http.Request) {
	o, _ := a.db.Get(r.PathValue("id"), false)
	if o.State == "draft" {
		http.Error(w, "not found", http.StatusNotFound)
	}
}

// archplan: auth=required
func (a *App) createOrder(w http.ResponseWriter, r *http.Request) {
	o := order.Order{State: "draft"}
	_ = a.db.Put(o, true, false)
}
