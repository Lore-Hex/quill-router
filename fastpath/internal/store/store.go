// Package store reads and writes the fast-admission spike's database
// (fastpath/schema/spike.sql). Each operation's guards are conditions in the
// transaction that writes (docs/design/fast-admission-spike.md, §2), and a
// guard that refuses is a result, not an error. Every transaction carries a
// tag naming its operation, so Spanner's statistics tell the operations
// apart. The store reads no clock of this machine: the times it writes are
// Spanner's, and a time the design puts on a node's clock is a parameter.
package store

import (
	"errors"
	"time"

	"cloud.google.com/go/spanner"
)

// Config holds what the store's statements take from the deployment.
type Config struct {
	// LiveFor is how recent a member's heartbeat must be for the member to
	// be live: three seconds in the spike (spike plan, §4).
	LiveFor time.Duration
}

// Store reads and writes the spike's database through client, which its
// caller opens and closes.
type Store struct {
	client *spanner.Client
	cfg    Config
}

// New returns a store of the database client reads and writes.
func New(client *spanner.Client, cfg Config) (*Store, error) {
	if client == nil {
		return nil, errors.New("store: no client")
	}
	if cfg.LiveFor <= 0 {
		return nil, errors.New("store: LiveFor must be positive")
	}
	return &Store{client: client, cfg: cfg}, nil
}

// tag is the transaction tag of an operation, and the request tag of each of
// its statements.
func tag(operation string) string {
	return "fastpath-" + operation
}
