package auditor

import (
	"context"
	"errors"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// TickStore is what the ticker reads and writes in Spanner; *store.Store has
// it.
type TickStore interface {
	ScanExpired(ctx context.Context, now time.Time, limit int) ([]store.Expired, time.Time, error)
	AuditorMarkDraining(ctx context.Context, ref store.LeaseRef, readExpiry time.Time) (bool, time.Time, error)
	ScanDraining(ctx context.Context, region string, after store.LeaseRef, limit int) ([]store.LeaseRef, error)
}

// Waiter is a publish: its acknowledgement's message ID, or its failure.
type Waiter interface {
	Wait(ctx context.Context) (string, error)
}

// LeaseLog publishes records under a lease's ordering key, and lets a key
// that a failed publish paused take records again (settlelog.Log).
type LeaseLog interface {
	Publish(lease string, data []byte) Waiter
	Resume(lease string)
}

// FromLog is the settle log's publisher as a LeaseLog.
func FromLog(l *settlelog.Log) LeaseLog { return leaseLog{l} }

type leaseLog struct{ l *settlelog.Log }

func (l leaseLog) Publish(lease string, data []byte) Waiter { return l.l.Publish(lease, data, nil) }
func (l leaseLog) Resume(lease string)                      { l.l.Resume(lease) }

// TickerConfig is a ticker's.
type TickerConfig struct {
	Store TickStore
	Log   LeaseLog
	// Region is the settle log's region: its draining leases are ticked.
	Region string
	// Every is how often the ticker drains expired leases and ticks.
	Every time.Duration
	// Limit bounds the leases one read finds.
	Limit int
	// Wait bounds how long a tick's publish is waited for.
	Wait  time.Duration
	Clock func() time.Time
}

// Ticker drains a region's expired leases and ticks its draining ones
// (§4.8). Every Every it marks draining each open lease whose expiry plus
// the skew allowance has passed, conditional on the expiry it read, and
// publishes a tick under each draining lease of its region, carrying its
// clock's time. A member that applies a tick past a lease's fence F plus
// the skew allowance stores S; later ticks drive the drain log, reaps and
// the close (Runtime). Ticks are numbered by their time, so a later one
// sorts after an earlier one and a redelivery is skipped; two tickers may
// tick one lease, and a tick passed over changes nothing the next does not.
type Ticker struct {
	cfg TickerConfig
}

// NewTicker is a ticker with its configuration.
func NewTicker(cfg TickerConfig) (*Ticker, error) {
	if cfg.Store == nil || cfg.Log == nil || cfg.Region == "" || cfg.Every <= 0 || cfg.Limit < 1 || cfg.Wait <= 0 {
		return nil, errors.New("auditor: a ticker needs a store, a log, a region, an interval, a page size and a publish wait")
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	return &Ticker{cfg: cfg}, nil
}

// Run ticks every Every until ctx ends.
func (t *Ticker) Run(ctx context.Context) error {
	tk := time.NewTicker(t.cfg.Every)
	defer tk.Stop()
	for {
		t.round(ctx)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-tk.C:
		}
	}
}

// round drains the expired leases one read finds, then ticks every draining
// lease of the region, a page at a time. What fails is tried at the next
// round.
func (t *Ticker) round(ctx context.Context) {
	if expired, _, err := t.cfg.Store.ScanExpired(ctx, t.cfg.Clock(), t.cfg.Limit); err == nil {
		for _, e := range expired {
			_, _, _ = t.cfg.Store.AuditorMarkDraining(ctx, e.Ref, e.Expiry)
		}
	}
	var after store.LeaseRef
	for ctx.Err() == nil {
		refs, err := t.cfg.Store.ScanDraining(ctx, t.cfg.Region, after, t.cfg.Limit)
		if err != nil {
			return
		}
		t.tick(ctx, refs)
		if len(refs) < t.cfg.Limit {
			return
		}
		after = refs[len(refs)-1]
	}
}

// tick publishes a tick under each lease, and waits for them; a lease whose
// tick failed has its key resumed, for the next.
func (t *Ticker) tick(ctx context.Context, refs []store.LeaseRef) {
	waits := make([]Waiter, len(refs))
	for i, ref := range refs {
		at := t.cfg.Clock().UTC().Truncate(time.Microsecond)
		data, err := record.Encode(record.Record{Version: record.Version, Lease: ref.LeaseID, Kind: record.Tick,
			TickNumber: at.UnixMicro(), TickAt: at})
		if err != nil {
			continue
		}
		waits[i] = t.cfg.Log.Publish(ref.LeaseID, data)
	}
	for i, w := range waits {
		if w == nil {
			continue
		}
		wctx, cancel := context.WithTimeout(ctx, t.cfg.Wait)
		if _, err := w.Wait(wctx); err != nil {
			t.cfg.Log.Resume(refs[i].LeaseID)
		}
		cancel()
	}
}
