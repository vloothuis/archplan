// Package api serves the shop over HTTP.
package api

import (
	"net/http"

	"example.com/shop/store"
)

const v1 = "/api/v1"

// App holds the handlers.
type App struct {
	db *store.DB
}

// Routes registers the handlers.
func (a *App) Routes(mux *http.ServeMux) {
	mux.HandleFunc("GET "+v1+"/orders/{id}", a.getOrder)
	mux.Handle("POST /api/v1/orders", http.HandlerFunc(a.createOrder))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {})
}
