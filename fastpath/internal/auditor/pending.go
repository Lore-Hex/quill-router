package auditor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// PendingStore is what the pending work reads and writes in Spanner;
// *store.Store has it.
type PendingStore interface {
	PendingPacks(ctx context.Context, after store.PendingPack, limit int) ([]store.PendingPack, error)
	LoadWinners(ctx context.Context, ref store.LeaseRef) ([]store.Pack, time.Time, error)
	ReadStaged(ctx context.Context, authorization string, digest []byte) (store.StagedRecord, bool, error)
	WriteRecords(ctx context.Context, records []store.LeaseRecord) error
	MarkPackDone(ctx context.Context, ref store.LeaseRef, version int64) (bool, error)
	DropStaged(ctx context.Context, keys ...store.StagedKey) error
	RetiredStaged(ctx context.Context, limit int) ([]store.StagedKey, error)
}

// PendingConfig is the pending work's.
type PendingConfig struct {
	Store   PendingStore
	Records RecordLog
	// Every is how often the sweep reads the packs whose work is not done,
	// Limit packs a read; Wait bounds an outcome's publish.
	Every time.Duration
	Limit int
	Wait  time.Duration
	// Alert tells a person of a winner whose work cannot be done as it is,
	// and of a pack whose work has been pending longer than Overdue
	// (AlertPendingWorkOverdue), once per pack, counted from when the sweep
	// first saw it, since a pack carries no time of its own; 0 tells no
	// one. Clock is the sweep's; nil is the wall clock.
	Alert   func(authorization, what string)
	Overdue time.Duration
	Clock   func() time.Time
}

// Pending does the winners' pending work (§4.8, §4.9): for each pack whose
// work is not done, every winner's outcome is published to the record topic,
// and then its records are written: a settle's or a reap's generation and
// activity records from its staged full record, a refund's or a release's
// disposition record. Then the pack is marked done, and its winners' staged
// records are dropped. A pack whose work cannot be done yet, a staged record
// not there or a write that failed, waits for the next sweep, and holds up
// none after it. A staged record no pack drops, such as one a crash left
// between a mark and its drop, goes once its lease retires. Every write is idempotent on its authorization, and an outcome
// published twice is the same message, so members that sweep at once, or a
// sweep after a crash, do no harm.
type Pending struct {
	cfg PendingConfig
	// firstSaw is when each pack whose work is not done was first seen,
	// and told whether its being overdue was told; packs done are
	// forgotten at the next sweep.
	firstSaw map[string]time.Time
	told     map[string]bool
}

// NewPending is the pending work with its configuration.
func NewPending(cfg PendingConfig) (*Pending, error) {
	if cfg.Store == nil || cfg.Records == nil || cfg.Every <= 0 || cfg.Limit < 1 || cfg.Wait <= 0 || cfg.Alert == nil ||
		cfg.Overdue < 0 {
		return nil, errors.New("auditor: pending work needs a store, the record topic, an interval, a page size, " +
			"a publish wait, someone to tell and a non-negative overdue allowance")
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	return &Pending{cfg: cfg, firstSaw: map[string]time.Time{}, told: map[string]bool{}}, nil
}

// Run sweeps every Every until ctx ends.
func (p *Pending) Run(ctx context.Context) error {
	t := time.NewTicker(p.cfg.Every)
	defer t.Stop()
	for {
		p.sweep(ctx)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}
	}
}

// sweep does the work of every pack not done, then drops the staged records
// of leases that have retired.
func (p *Pending) sweep(ctx context.Context) {
	p.sweepPacks(ctx)
	p.dropRetired(ctx)
}

// sweepPacks does the work of every pack not done, a page at a time,
// reading each lease's packs once a page.
func (p *Pending) sweepPacks(ctx context.Context) {
	var after store.PendingPack
	seen := map[string]time.Time{}
	complete := false
	defer func() {
		// Only a sweep that read every page says which packs are gone:
		// one that ended early, a read failed or ctx ended, keeps what it
		// knew of the packs it did not reach.
		if !complete {
			for k, t := range p.firstSaw {
				if _, ok := seen[k]; !ok {
					seen[k] = t
				}
			}
		}
		p.firstSaw = seen
		for k := range p.told {
			if _, ok := seen[k]; !ok {
				delete(p.told, k)
			}
		}
	}()
	for ctx.Err() == nil {
		page, err := p.cfg.Store.PendingPacks(ctx, after, p.cfg.Limit)
		if err != nil {
			return
		}
		// A pack is first seen when its page is read, not when the sweep
		// began: a sweep's earlier pages can take long.
		now := p.cfg.Clock()
		var ref store.LeaseRef
		var packs []store.Pack
		read := false
		for _, pp := range page {
			p.overdue(pp, now, seen)
			if !read || pp.Ref != ref {
				ref, read = pp.Ref, true
				if packs, _, err = p.cfg.Store.LoadWinners(ctx, ref); err != nil {
					packs = nil
				}
			}
			for _, pack := range packs {
				if pack.CommitVersion == pp.CommitVersion {
					_, _ = p.Do(ctx, ref, pack)
				}
			}
		}
		if len(page) < p.cfg.Limit {
			complete = ctx.Err() == nil
			return
		}
		after = page[len(page)-1]
	}
}

// overdue tells of a pack pending longer than Overdue, once, from when the
// sweep first saw it.
func (p *Pending) overdue(pp store.PendingPack, now time.Time, seen map[string]time.Time) {
	key := fmt.Sprintf("%s/%s@%d", pp.Ref.Workspace, pp.Ref.LeaseID, pp.CommitVersion)
	first, ok := p.firstSaw[key]
	if !ok {
		first = now
	}
	seen[key] = first
	if p.cfg.Overdue > 0 && !p.told[key] && now.Sub(first) > p.cfg.Overdue {
		p.told[key] = true
		p.cfg.Alert(key, AlertPendingWorkOverdue)
	}
}

// dropRetired drops the staged records of leases that have retired or gone
// (store.RetiredStaged), a commit's worth at a time.
func (p *Pending) dropRetired(ctx context.Context) {
	for ctx.Err() == nil {
		keys, err := p.cfg.Store.RetiredStaged(ctx, stagedBatch)
		if err != nil || p.cfg.Store.DropStaged(ctx, keys...) != nil || len(keys) < stagedBatch {
			return
		}
	}
}

// outcomes are the outcomes of the winners' kinds, as answers name them.
var outcomes = map[string]string{"settle": "settled", "refund": "refunded", "reap": "reaped_snapshot",
	"release": "released"}

// OutcomeRecord is a winner's outcome as the record topic carries it
// (§4.9): the authorization, the outcome, the cost, the boot binding, and
// the digest of the winning terminal's full record, which picks it among
// the archived ones.
type OutcomeRecord struct {
	V       int    `json:"v"`
	Auth    string `json:"a"`
	Outcome string `json:"outcome"`
	Cost    int64  `json:"cost"`
	Boot    []byte `json:"boot,omitempty"`
	Digest  []byte `json:"digest,omitempty"`
}

// DispositionRecord is a refund's or a release's record (§4.9): the
// authorization, its outcome and its boot binding, which a boot-signed
// lookup checks.
type DispositionRecord struct {
	V       int    `json:"v"`
	Auth    string `json:"a"`
	Outcome string `json:"outcome"`
	Boot    []byte `json:"boot,omitempty"`
}

// winnerWork is a winner with its work read, its staged full record's body
// and its boot binding.
type winnerWork struct {
	store.Winner
	work    Work
	outcome string
	body    []byte
	boot    []byte
}

// staged: a settle's and a reap's records come from a staged full record.
func (w winnerWork) staged() bool { return w.Kind == "settle" || w.Kind == "reap" }

// recordBatch bounds one write of a pack's records, its bodies' bytes and
// its rows, well within a Spanner commit's limits.
var recordBatch = struct {
	bytes int
	rows  int
}{16 << 20, 1000}

// stagedBatch bounds the staged records one drop removes.
var stagedBatch = store.MaxDropStaged

// bootOf is the boot binding a full record states (§4.9): a gateway's, an
// owner's reap's and an auditor's reap's each carry it, as "boot".
func bootOf(body []byte) []byte {
	var full struct {
		Boot []byte `json:"boot"`
	}
	if json.Unmarshal(body, &full) != nil {
		return nil
	}
	return full.Boot
}

// Do does a pack's work, and reports whether the pack is done. A settle's or
// a reap's staged full record is read first, for its records and its boot
// binding, which a refund's or a release's work carries itself. Then every
// outcome is published, so a record written can be rebuilt from the topic's
// export once the pack is gone; once every one is acknowledged the records
// are written, in batches; once every one is written the pack is marked
// done; and then its winners' staged records are dropped, in batches too.
// A drop that fails leaves the pack done, and its records to go once the
// lease retires.
func (p *Pending) Do(ctx context.Context, ref store.LeaseRef, pack store.Pack) (bool, error) {
	if pack.WorkDoneAt.Valid {
		return true, nil
	}
	winners := make([]winnerWork, 0, len(pack.Winners))
	for _, w := range pack.Winners {
		ww := winnerWork{Winner: w, outcome: outcomes[w.Kind]}
		if err := json.Unmarshal(w.Work, &ww.work); err != nil || ww.work.V != 1 || ww.outcome == "" ||
			(ww.staged() && len(ww.work.Digest) == 0) {
			p.cfg.Alert(w.AuthorizationID, AlertUnreadableWork)
			return false, errors.New("auditor: a winner's pending work cannot be read")
		}
		winners = append(winners, ww)
	}
	for i := range winners {
		w := &winners[i]
		w.boot = w.work.Boot
		if !w.staged() {
			continue
		}
		r, ok, err := p.cfg.Store.ReadStaged(ctx, w.AuthorizationID, w.work.Digest)
		if err != nil || !ok {
			// Not staged yet: the record topic's consumer is behind.
			return false, err
		}
		w.body = r.Body
		if len(w.boot) == 0 {
			w.boot = bootOf(r.Body)
		}
	}
	for _, w := range winners {
		if len(w.boot) == 0 {
			p.cfg.Alert(w.AuthorizationID, AlertNoBootBinding)
			return false, errors.New("auditor: a winner with no boot binding")
		}
	}
	waits := make([]Waiter, len(winners))
	for i, w := range winners {
		waits[i] = p.cfg.Records.Publish(w.AuthorizationID, settlelog.Outcome, encoded(OutcomeRecord{V: 1,
			Auth: w.AuthorizationID, Outcome: w.outcome, Cost: w.Charge, Boot: w.boot, Digest: w.work.Digest}))
	}
	for _, wait := range waits {
		wctx, cancel := context.WithTimeout(ctx, p.cfg.Wait)
		_, err := wait.Wait(wctx)
		cancel()
		if err != nil {
			return false, err
		}
	}
	var records []store.LeaseRecord
	var drop []store.StagedKey
	for _, w := range winners {
		cost := spanner.NullInt64{Int64: w.Charge, Valid: true}
		if !w.staged() {
			records = append(records, store.LeaseRecord{AuthorizationID: w.AuthorizationID, Kind: "disposition",
				Ref: ref, Outcome: w.outcome, Cost: cost, BootBinding: w.boot,
				Body: encoded(DispositionRecord{V: 1, Auth: w.AuthorizationID, Outcome: w.outcome, Boot: w.boot})})
			continue
		}
		for _, kind := range []string{"generation", "activity"} {
			records = append(records, store.LeaseRecord{AuthorizationID: w.AuthorizationID, Kind: kind, Ref: ref,
				Outcome: w.outcome, Cost: cost, WinnerDigest: w.work.Digest, BootBinding: w.boot, Body: w.body})
		}
		drop = append(drop, store.StagedKey{AuthorizationID: w.AuthorizationID, Digest: w.work.Digest})
	}
	for len(records) > 0 {
		n, size := 0, 0
		for n < len(records) && n < recordBatch.rows && (n == 0 || size+len(records[n].Body) <= recordBatch.bytes) {
			size += len(records[n].Body)
			n++
		}
		if err := p.cfg.Store.WriteRecords(ctx, records[:n]); err != nil {
			return false, err
		}
		records = records[n:]
	}
	if _, err := p.cfg.Store.MarkPackDone(ctx, ref, pack.CommitVersion); err != nil {
		return false, err
	}
	for len(drop) > 0 {
		n := min(len(drop), stagedBatch)
		if p.cfg.Store.DropStaged(ctx, drop[:n]...) != nil {
			break
		}
		drop = drop[n:]
	}
	return true, nil
}
