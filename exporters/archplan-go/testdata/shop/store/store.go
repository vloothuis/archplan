// Package store keeps orders.
package store

import "example.com/shop/order"

// DB is the order store.
type DB struct{ rows map[string]order.Order }

// Get returns an order.
// archplan: sink=read
func (d *DB) Get(id string, forUpdate bool) (order.Order, error) { return d.rows[id], nil }

// Put saves an order.
// archplan: sink=write
func (d *DB) Put(o order.Order, upsert, notify bool) error {
	switch o.State {
	case "draft", "paid":
	case "shipped":
		o.State = "closed"
	}
	return nil
}
