// Package order holds the order type.
package order

// Order is one order.
type Order struct {
	ID    string
	State string
}

// Paid reports whether the order is paid.
func (o Order) Paid() bool { return o.State == "paid" }
