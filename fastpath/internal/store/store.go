// Package store reads and writes the fast-admission spike's database
// (fastpath/schema/spike.sql). Each operation's guards are conditions in the
// transaction that writes (docs/design/fast-admission-spike.md, §2), and a
// guard that refuses is a result, not an error. Every transaction carries a
// tag naming its operation, so Spanner's statistics tell the operations
// apart. The store reads no clock of this machine: the times it writes are
// Spanner's, and a time the design puts on a node's clock is a parameter.
//
// The store changes rows that exist only with DML. The emulator the tests run
// on resets the columns an update mutation does not name to their defaults,
// which Spanner does not; inserts name what they set and take the defaults
// for the rest, on both.
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
	// Window is how far past Spanner's time a grant or a renewal sets a
	// lease's expiry (§4.2).
	Window time.Duration
	// Skew is the allowance for nodes' clocks, and PublishDeadline the
	// owner's deadline for a publish (§4.5); a draining write stores the
	// fence F as the expiry plus both (§4.8).
	Skew            time.Duration
	PublishDeadline time.Duration
	// MaxLife is a hold's longest life, and Grace the time past it before a
	// lease whose holds were never listed may close (§4.8): 2 h 20 min and
	// the grace in production, scaled down for the spike's correctness runs.
	MaxLife time.Duration
	Grace   time.Duration
	// Allowance caps a workspace's exposure, across its regions and shards;
	// Floor is the headroom a grant leaves outside leases; RequiredTier is
	// the trust tier that allows leases (§4.2, §4.7, §4.11). The spike has
	// one tier, so one allowance.
	Allowance    int64
	Floor        int64
	RequiredTier int64
}

// MaxSetting bounds every duration in Config.
const MaxSetting = 7 * 24 * time.Hour

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
	if cfg.LiveFor <= 0 || cfg.Window <= 0 || cfg.Skew <= 0 || cfg.PublishDeadline <= 0 || cfg.MaxLife <= 0 || cfg.Grace <= 0 {
		return nil, errors.New("store: LiveFor, Window, Skew, PublishDeadline, MaxLife and Grace must be positive")
	}
	// Spanner's intervals here are whole microseconds; a finer duration
	// would be cut short, and the window or the fence with it. No lease's
	// setting is near a week, and bounding them there keeps every sum of
	// them, and every time they are added to, in range.
	for _, d := range []time.Duration{cfg.Window, cfg.Skew, cfg.PublishDeadline} {
		if d%time.Microsecond != 0 || d > MaxSetting {
			return nil, errors.New("store: Window, Skew and PublishDeadline must be whole microseconds, at most MaxSetting")
		}
	}
	if cfg.Allowance <= 0 || cfg.Floor < 0 || cfg.RequiredTier < 0 || cfg.RequiredTier > 3 {
		return nil, errors.New("store: the allowance must be positive, the floor not negative, and the tier 0 to 3")
	}
	return &Store{client: client, cfg: cfg}, nil
}

// readTimestamp is a read-only transaction's timestamp in UTC, as every
// time the store returns is, those it reads from rows included.
func readTimestamp(ro *spanner.ReadOnlyTransaction) (time.Time, error) {
	t, err := ro.Timestamp()
	return t.UTC(), err
}

// tag is the transaction tag of an operation, and the request tag of each of
// its statements.
func tag(operation string) string {
	return "fastpath-" + operation
}
