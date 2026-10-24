// Package gwauth is the edge's gateway authorization: the rows an
// authorization the edge decides writes, exactly as Python's leased
// authorize writes them (docs/design/fast-admission-e2-decide.md, §5 and
// §8.3). This file holds only the row types, so the compose side, which
// fills them, and the store, which inserts them, build against one
// definition.
package gwauth

import "time"

// AuthorizationRow is an authorization's row in tr_gateway_authorization.
// Of the table's typed columns only these three are written; the rest, and
// terminal_at, are NULL, and settled is false.
type AuthorizationRow struct {
	AuthorizationID        string
	WorkspaceID            string
	KeyHash                string
	ReservationID          string
	ModelID                string
	Provider               string
	UsageType              string // "Credits"
	EstimatedMicrodollars  int64
	CreatedAt              time.Time
	Payload                string
	IdempotencyFingerprint string
	StageDBootKid          string
	InvocationNonce        *string // nil writes NULL
}

// ReservationRow is the authorization's row in tr_reservation. The shards,
// the reserved amounts and settled take their §5 constants (0, 0 and
// false); actual_micro, settled_usage_type and terminal_at are NULL.
// created_at is the commit's timestamp, so it is no field here: the column
// is allow_commit_timestamp, and Spanner refuses a value in it later than
// the commit's own timestamp, which the edge's clock reading is whenever
// the clock runs ahead of TrueTime by more than the time to the commit.
type ReservationRow struct {
	ReservationID          string
	WorkspaceID            string
	KeyHash                string
	HoldUsageType          string // "Credits"
	AuthorizationID        string
	IdempotencyScope       string
	IdempotencyFingerprint string
	ExpiresAt              time.Time
}
