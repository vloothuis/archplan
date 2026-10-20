package api

import "net/http"

// archplan: guard=authz
func mayRead(r *http.Request) bool { return r.Header.Get("X-User") != "" }
