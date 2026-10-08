package auditor

import (
	"context"
	"crypto/sha256"
	"errors"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Store is what the runtime reads and writes in Spanner; *store.Store has
// it.
type Store interface {
	FindLease(ctx context.Context, leaseID string) (store.LeaseRef, error)
	Load(ctx context.Context, ref store.LeaseRef) (store.Loaded, error)
	ReadLease(ctx context.Context, ref store.LeaseRef) (store.Lease, time.Time, error)
	LoadWinners(ctx context.Context, ref store.LeaseRef) ([]store.Pack, time.Time, error)
	Commit(ctx context.Context, reqs []store.CommitRequest) ([]store.CommitResult, time.Time, error)
	StopForGap(ctx context.Context, ref store.LeaseRef, readVersion, seq int64) (bool, time.Time, error)
	ReadDrainSince(ctx context.Context, ref store.LeaseRef, cursor time.Time) ([]store.DrainRow, time.Time, error)
	Reap(ctx context.Context, ref store.LeaseRef, readVersion int64, tick time.Time, r store.ReapRow) (store.Refusal, time.Time, error)
	CloseLease(ctx context.Context, ref store.LeaseRef, readVersion int64, drainRead, now time.Time) (store.CloseResult, error)
}

// RecordLog publishes the record topic (settlelog.Records): a reap's full
// record goes there before its drain-log row.
type RecordLog interface {
	Publish(authorization, kind string, data []byte) Waiter
}

// FromRecords is the record topic's publisher as a RecordLog.
func FromRecords(r *settlelog.Records) RecordLog { return recordLog{r} }

type recordLog struct{ r *settlelog.Records }

func (r recordLog) Publish(authorization, kind string, data []byte) Waiter {
	return r.r.Publish(authorization, kind, data)
}

// Delivery is one record as the settle log delivered it: the lease whose
// ordering key it came on, its bytes, its publish time, and its
// acknowledgement.
type Delivery interface {
	Lease() string
	Data() []byte
	Published() time.Time
	Ack()
}

// Source delivers a region's settle log to the member: each lease's records
// one at a time, in the order the log stored them, the next once handle has
// returned for the one before; leases concurrently. A record not
// acknowledged when Receive returns is delivered again (assumption A1).
type Source interface {
	Receive(ctx context.Context, handle func(context.Context, Delivery)) error
}

// FromSubscription is the settle log's subscription as a Source.
func FromSubscription(s *settlelog.Subscription) Source { return subscription{s} }

type subscription struct{ s *settlelog.Subscription }

func (s subscription) Receive(ctx context.Context, handle func(context.Context, Delivery)) error {
	return s.s.Receive(ctx, func(ctx context.Context, d *settlelog.Delivery) { handle(ctx, delivered{d}) })
}

type delivered struct{ d *settlelog.Delivery }

func (d delivered) Lease() string        { return d.d.Lease }
func (d delivered) Data() []byte         { return d.d.Data }
func (d delivered) Published() time.Time { return d.d.PublishTime }
func (d delivered) Ack()                 { d.d.Ack() }

// Config is a runtime's.
type Config struct {
	Store Store
	// Skew is the skew allowance: a tick at or past a lease's fence F plus
	// it is the fence tick (§4.8).
	Skew time.Duration
	// CommitEvery is how often Run commits what its leases applied, about
	// how long a record waits for its acknowledgement.
	CommitEvery time.Duration
	// MaxBatch bounds the leases one commit carries.
	MaxBatch int
	// Retry is how long the runtime waits to read or write again after
	// Spanner fails.
	Retry time.Duration
	// Alert tells a person what needs one: a lease stopped at a gap, an
	// audit fault, a charge past the allocation, a record no member can
	// read. It gets the lease and what happened, never a record's contents.
	// A member that reads a lease tells again what its row already holds,
	// since the member that stored it may have stopped before it told:
	// the receiver keeps one per lease and kind.
	Alert func(lease, what string)
	// ForgetAfter is how long a lease that is done stays known, so its
	// later records are acknowledged without reading it again.
	ForgetAfter time.Duration
	// Grace is the reaper's grace (the store's Config.Grace): a hold is
	// reaped at a tick past its deadline plus it (§4.8). MaxLife is a
	// hold's longest life (the store's Config.MaxLife): holds no list named
	// end by the lease's expiry plus it plus the grace.
	Grace   time.Duration
	MaxLife time.Duration
	// Records takes the full records of the reaps the runtime makes, and
	// Wait bounds how long one's publish is waited for.
	Records RecordLog
	Wait    time.Duration
	// Clock is the member's clock, which says when it saw a drain's end
	// condition hold.
	Clock func() time.Time
}

// Runtime is a member's work on its share of a region's settle log (§4.8):
// each lease's records applied in order to the lease's member, the leases'
// commits batched, and a record acknowledged only once a commit has made
// what it did durable (assumption A1).
type Runtime struct {
	cfg     Config
	mu      sync.Mutex
	leases  map[string]*held
	workers sync.WaitGroup // leases read again after a refused or lost commit
	// The handlers under way, which Run waits for, and whether Run is
	// stopping, when it takes no more.
	hmu      sync.Mutex
	handlers int
	stopping bool
	idle     *sync.Cond
}

// held is a lease at the runtime: its member, and the records handled since
// its last commit, in order. They are acknowledged once a commit lands.
// After a refused or failed write the member is read again and they are
// applied to it again, as Pub/Sub would deliver them again from the first
// not acknowledged (AuditorCommit's Reread), so the runtime never asks for
// a record again itself. A lease that is done (closed, stopped at a gap, or
// none) has its records acknowledged as they come: none is booked.
type held struct {
	mu         sync.Mutex
	id         string
	ref        store.LeaseRef
	lease      *Lease
	pending    []*handled
	done       bool
	doneAt     time.Time
	recovering bool
	// A draining lease's drain log is read past cursor, the timestamp of
	// the read before; endSince is when the member saw the drain's end
	// condition hold, from when a tick may close the lease.
	cursor, endSince time.Time
}

// handled is a record handled, with its delivery. A bad one no member can
// read or apply: it is never applied, so if it was an owner record, the
// one after it is a gap.
type handled struct {
	d   Delivery
	r   record.Record
	bad bool
}

// New is a runtime with its configuration.
func New(cfg Config) (*Runtime, error) {
	if cfg.Store == nil || cfg.Skew < 0 || cfg.CommitEvery <= 0 || cfg.MaxBatch < 1 || cfg.Retry <= 0 ||
		cfg.ForgetAfter < 0 || cfg.Grace < 0 || cfg.MaxLife <= 0 || cfg.Records == nil || cfg.Wait <= 0 {
		return nil, errors.New("auditor: a runtime needs a store, a skew allowance, a commit interval, a batch size, " +
			"a retry wait, a grace, a hold's maximum life, the record topic and a publish wait")
	}
	if cfg.Alert == nil {
		cfg.Alert = func(string, string) {}
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	rt := &Runtime{cfg: cfg, leases: map[string]*held{}}
	rt.idle = sync.NewCond(&rt.hmu)
	return rt, nil
}

// Run receives the settle log until ctx ends or src fails, committing what
// it applied every CommitEvery. Its handlers work under ctx, and it returns
// only once each has ended, though src returned first: what it has not
// acknowledged then, src delivers again.
func (rt *Runtime) Run(ctx context.Context, src Source) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	committed := make(chan struct{})
	go func() {
		defer close(committed)
		t := time.NewTicker(rt.cfg.CommitEvery)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				rt.commitAll(ctx)
			}
		}
	}()
	err := src.Receive(ctx, func(_ context.Context, d Delivery) {
		if !rt.enter() {
			return
		}
		defer rt.exit()
		rt.handle(ctx, d)
	})
	cancel()
	rt.hmu.Lock()
	rt.stopping = true
	for rt.handlers > 0 {
		rt.idle.Wait()
	}
	rt.hmu.Unlock()
	<-committed
	rt.workers.Wait()
	return err
}

// enter admits a handler, unless Run is stopping.
func (rt *Runtime) enter() bool {
	rt.hmu.Lock()
	defer rt.hmu.Unlock()
	if rt.stopping {
		return false
	}
	rt.handlers++
	return true
}

func (rt *Runtime) exit() {
	rt.hmu.Lock()
	defer rt.hmu.Unlock()
	if rt.handlers--; rt.handlers == 0 {
		rt.idle.Broadcast()
	}
}

// handle takes a lease's next record: it is applied to the lease's member,
// read first if the runtime has none, and kept until a commit lands.
func (rt *Runtime) handle(ctx context.Context, d Delivery) {
	h := rt.held(d.Lease())
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.done {
		d.Ack()
		return
	}
	r, err := record.Decode(d.Data())
	x := &handled{d: d, r: r, bad: err != nil}
	if x.bad {
		rt.cfg.Alert(h.id, "a record the auditor cannot read")
	}
	h.pending = append(h.pending, x)
	rt.replay(ctx, h, len(h.pending)-1)
	if !x.bad && x.r.Kind == record.Tick && h.lease != nil && !h.done {
		rt.drain(ctx, h, x.r.TickAt)
	}
}

func (rt *Runtime) held(id string) *held {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	h := rt.leases[id]
	if h == nil {
		h = &held{id: id}
		rt.leases[id] = h
	}
	return h
}

// replay applies the lease's records from pending[from] on to its member, in
// order, reading the member first if the runtime has none, and then from
// the first. At a gap it stops the lease, once what the member applied
// before it is committed (AuditorCommit's Gap); after another member's
// write it reads the lease again and applies its records again. It returns
// once every record is applied, the lease is done, or ctx has ended, when
// the member is dropped so that no commit covers a record not applied.
func (rt *Runtime) replay(ctx context.Context, h *held, from int) {
	for !h.done {
		if h.lease == nil {
			if !rt.load(ctx, h) {
				return
			}
			from = 0
		}
		gap := -1
		for i := from; i < len(h.pending) && gap < 0; i++ {
			x := h.pending[i]
			if x.bad {
				continue
			}
			out, ok := rt.apply(ctx, h, x)
			if !ok {
				if !h.done {
					h.lease = nil
				}
				return
			}
			if out == Gap {
				gap = i
			}
		}
		if gap < 0 || rt.stopAtGap(ctx, h, gap) {
			return
		}
		h.lease = nil
		if !rt.wait(ctx) {
			return
		}
	}
}

// apply applies one record, reading first what the member lacks (Behind).
// It reports false if the lease turned out done, or ctx ended, first.
func (rt *Runtime) apply(ctx context.Context, h *held, x *handled) (Outcome, bool) {
	for {
		out, err := h.lease.Apply(x.r, x.d.Published())
		switch {
		case err != nil:
			x.bad = true
			rt.cfg.Alert(h.id, "a record the auditor cannot apply")
			return Skipped, true
		case out != Behind:
			return out, true
		}
		lease, _, err := rt.cfg.Store.ReadLease(ctx, h.ref)
		if err == nil {
			// The row may hold what another member stored since this one
			// read the lease: it is told, and a lease closed or stopped at
			// a gap is done, as a load that reads it so finds.
			rt.told(h, lease)
			if lease.State == "closed" || lease.GapSeq.Valid {
				rt.finish(h)
				return 0, false
			}
		}
		switch {
		case errors.Is(err, store.ErrNoLease):
			rt.cfg.Alert(h.id, "a record of a lease the store does not have")
			rt.finish(h)
			return 0, false
		case err != nil:
		case x.r.Kind == record.Tick && !lease.FenceTime.Valid:
			// A tick for a lease still open, which none is sent: it
			// changes nothing.
			return Skipped, true
		case x.r.Kind == record.Tick:
			err = h.lease.Drained(lease)
		default:
			var packs []store.Pack
			if packs, _, err = rt.cfg.Store.LoadWinners(ctx, h.ref); err == nil {
				err = h.lease.LoadWinners(lease, packs)
			}
		}
		if err != nil && !rt.wait(ctx) {
			return 0, false
		}
	}
}

// load reads the lease's member: the row's key from the lease's ID, then the
// lease as a member keeps it. A lease closed, stopped at a gap, or none is
// done. It reports false if the lease is done or ctx ended first.
func (rt *Runtime) load(ctx context.Context, h *held) bool {
	for {
		loaded, err := rt.read(ctx, h)
		switch {
		case errors.Is(err, store.ErrNoLease):
			rt.cfg.Alert(h.id, "a record of a lease the store does not have")
			rt.finish(h)
			return false
		case err == nil && (loaded.Lease.State == "closed" || loaded.Lease.GapSeq.Valid):
			rt.told(h, loaded.Lease)
			rt.finish(h)
			return false
		case err == nil:
			if h.lease, err = Load(h.ref, loaded, rt.cfg.Skew); err == nil {
				// A member read again reads the drain log from the first
				// row, and sees the end afresh: what the one dropped
				// applied of it was never committed.
				h.cursor, h.endSince = time.Time{}, time.Time{}
				rt.told(h, loaded.Lease)
				return true
			}
		}
		if !rt.wait(ctx) {
			return false
		}
	}
}

func (rt *Runtime) read(ctx context.Context, h *held) (store.Loaded, error) {
	if h.ref.Workspace == "" {
		ref, err := rt.cfg.Store.FindLease(ctx, h.id)
		if err != nil {
			return store.Loaded{}, err
		}
		h.ref = ref
	}
	return rt.cfg.Store.Load(ctx, h.ref)
}

// drain does a draining lease's work at a tick published at at, once S is
// stored (§4.8): it applies the drain log's rows committed since its last
// read, in order, after every owner record up to S; reaps each open hold
// whose deadline plus the grace the tick has passed; and closes the lease
// at a tick published once the drain's end condition held, the skew
// allowance after the member saw it hold. Each step a Spanner failure stops
// is tried at the next tick; a write another member's refused drops the
// member, which the next record reads again.
func (rt *Runtime) drain(ctx context.Context, h *held, at time.Time) {
	l := h.lease
	if !l.draining || !l.sStored {
		return
	}
	if !l.winnersLoaded {
		lease, _, err := rt.cfg.Store.ReadLease(ctx, h.ref)
		if err != nil {
			return
		}
		packs, _, err := rt.cfg.Store.LoadWinners(ctx, h.ref)
		if err != nil || l.LoadWinners(lease, packs) != nil {
			return
		}
	}
	rows, read, err := rt.cfg.Store.ReadDrainSince(ctx, h.ref, h.cursor)
	if err != nil {
		return
	}
	for _, row := range rows {
		if err := l.ApplyRow(row); err != nil {
			return
		}
	}
	h.cursor = read
	for _, hold := range l.openHolds() {
		if at.Before(hold.Deadline.Add(rt.cfg.Grace)) {
			continue
		}
		if !rt.reap(ctx, h, hold, at) {
			return
		}
	}
	rt.closeAt(ctx, h, at)
}

// reapRecord is the full record of a reap the auditor makes (§4.9), version
// 1: the hold's latest snapshot and the basis its first heartbeat brought,
// from which the request records are written if the reap wins.
type reapRecord struct {
	V          int       `json:"v"`
	Lease      string    `json:"lease"`
	Auth       string    `json:"a"`
	Estimate   int64     `json:"est"`
	Charge     int64     `json:"charge"`
	At         time.Time `json:"at"`
	GatewaySeq int64     `json:"gseq,omitempty"`
	Hash       []byte    `json:"hash,omitempty"`
	Usage      []byte    `json:"usage,omitempty"`
	OwnerSeq   int64     `json:"snap,omitempty"`
	Basis      []byte    `json:"basis,omitempty"`
	Boot       []byte    `json:"boot,omitempty"`
}

// reapMoney is a reap row's money fields.
type reapMoney struct {
	Charge   int64 `json:"charge"`
	Estimate int64 `json:"est"`
}

// reap reaps an open hold at a tick published at at (§4.8): at its latest
// snapshot's running charge, capped at its estimate. Its full record goes to
// the record topic first, then its row to the drain log, which the member
// applies as the hold's winner at a later tick unless a terminal was there
// first. It reports false if the member was dropped to be read again.
func (rt *Runtime) reap(ctx context.Context, h *held, hold store.HoldRow, at time.Time) bool {
	var charge int64
	if hold.RunningCharge.Valid {
		charge = min(hold.RunningCharge.Int64, hold.Estimate)
	}
	full := encoded(reapRecord{V: 1, Lease: h.ref.LeaseID, Auth: hold.AuthorizationID, Estimate: hold.Estimate,
		Charge: charge, At: at, GatewaySeq: hold.SnapshotSeq.Int64, Hash: hold.SnapshotHash, Usage: hold.SnapshotUsage,
		OwnerSeq: hold.SnapshotOwnerSeq.Int64, Basis: hold.ReapBasis, Boot: hold.Boot})
	wctx, cancel := context.WithTimeout(ctx, rt.cfg.Wait)
	_, err := rt.cfg.Records.Publish(hold.AuthorizationID, settlelog.FullRecord, full).Wait(wctx)
	cancel()
	if err != nil {
		return true
	}
	digest := sha256.Sum256(full)
	refused, _, err := rt.cfg.Store.Reap(ctx, h.ref, h.lease.version, at, store.ReapRow{
		AuthorizationID: hold.AuthorizationID, RecordID: "reap-" + hold.AuthorizationID, Charge: charge,
		Estimate: hold.Estimate, SnapshotOwnerSeq: hold.SnapshotOwnerSeq.Int64, Digest: digest[:],
		Money: encoded(reapMoney{Charge: charge, Estimate: hold.Estimate})})
	if err == nil && (refused == store.RefusedVersion || refused == store.RefusedGap) {
		h.lease = nil
		return false
	}
	return true
}

// closeAt closes a draining lease at a tick published at at (§4.8): once
// every record and drain-log row it read is applied and committed, and no
// hold the member knows is open, at a tick published the skew allowance
// after it first saw that hold, so everything published before the tick,
// which it has applied, came after the end. The close itself refuses rows
// past the member's read, and holds the lease never listed until their
// time has run out.
func (rt *Runtime) closeAt(ctx context.Context, h *held, at time.Time) {
	l := h.lease
	now := rt.cfg.Clock()
	if len(l.holds) > 0 || l.Dirty() || (!l.listed && now.Before(l.expiry.Add(rt.cfg.MaxLife+rt.cfg.Grace))) {
		// Not the end: a hold the member knows is open, or holds no list
		// named, which end only by the expiry plus their maximum life
		// plus the grace (§4.8).
		h.endSince = time.Time{}
		return
	}
	if h.endSince.IsZero() {
		h.endSince = now
		return
	}
	if at.Before(h.endSince.Add(rt.cfg.Skew)) {
		return
	}
	got, err := rt.cfg.Store.CloseLease(ctx, h.ref, l.version, h.cursor, at)
	switch {
	case err == nil && got.Refused == "":
		rt.finish(h)
	case err != nil, got.Refused == store.RefusedVersion, got.Refused == store.RefusedGap, got.Refused == store.RefusedClosed:
		// Its outcome unknown, or another member wrote the lease or
		// closed it: the member is read again, by the next round of
		// commits, and a lease found closed is done.
		h.lease = nil
	}
}

// told tells what a lease's row holds that a person needs: a write that
// stored it may have lost its answer, or its member stopped, before telling.
func (rt *Runtime) told(h *held, l store.Lease) {
	if l.GapSeq.Valid {
		rt.cfg.Alert(h.id, "a gap in the lease's records stopped it")
	}
	if l.AuditFaultSeq.Valid {
		rt.cfg.Alert(h.id, "an audit fault: the owner's checkpoint disagrees with its records")
	}
	if l.FaultUsage > 0 {
		rt.cfg.Alert(h.id, "a charge past the lease's allocation")
	}
}

// stopAtGap stops the lease at the gap before pending[i] (AuditorCommit's
// Gap): what the member applied before the gap is committed first, and the
// lease is stopped at the version it read, so nothing after the gap is
// booked. It reports false if the lease must be read again: another member
// wrote it meanwhile, or a write's outcome is unknown.
func (rt *Runtime) stopAtGap(ctx context.Context, h *held, i int) bool {
	if h.lease.Dirty() {
		req := h.lease.Request()
		results, _, err := rt.cfg.Store.Commit(ctx, []store.CommitRequest{req})
		if err != nil || len(results) != 1 || results[0].Ref != req.Ref || h.lease.Committed(results[0]) != nil {
			return false
		}
		rt.landed(h, results[0], i)
		i = 0
	}
	stopped, _, err := rt.cfg.Store.StopForGap(ctx, h.ref, h.lease.version, h.pending[i].r.Seq)
	if err != nil || !stopped {
		return false
	}
	rt.cfg.Alert(h.id, "a gap in the lease's records stopped it")
	rt.finish(h)
	return true
}

// finish marks a lease done: none of its records is booked, so each is
// acknowledged, those kept and those to come.
func (rt *Runtime) finish(h *held) {
	h.done, h.lease, h.doneAt = true, nil, time.Now()
	rt.ack(h, len(h.pending))
}

// ack acknowledges the first n records kept.
func (rt *Runtime) ack(h *held, n int) {
	for _, x := range h.pending[:n] {
		x.d.Ack()
	}
	h.pending = slices.Delete(h.pending, 0, n)
}

// landed acknowledges what a commit made durable, the first n records
// kept, and alerts on what it stored that needs a person: the audit fault
// it stored, the member's or a checkpoint's, and a charge past the
// allocation.
func (rt *Runtime) landed(h *held, got store.CommitResult, n int) {
	if got.AuditFault != nil {
		rt.cfg.Alert(h.id, "an audit fault: the owner's checkpoint disagrees with its records")
	}
	if len(got.Faults) > 0 {
		rt.cfg.Alert(h.id, "a charge past the lease's allocation")
	}
	rt.ack(h, n)
}

// commitAll commits what the leases applied, MaxBatch leases a commit, and
// acknowledges the records of a lease with nothing to commit. A lease whose
// handler holds it, reading Spanner, waits for the next round.
func (rt *Runtime) commitAll(ctx context.Context) {
	rt.mu.Lock()
	hs := make([]*held, 0, len(rt.leases))
	for _, h := range rt.leases {
		hs = append(hs, h)
	}
	rt.mu.Unlock()
	slices.SortFunc(hs, func(a, b *held) int { return strings.Compare(a.id, b.id) })
	var batch []*held
	flush := func() {
		rt.commit(ctx, batch)
		var again []*held
		for _, h := range batch {
			// A lease to read again is claimed for its worker while the
			// round holds it: the round never waits for it again.
			if h.lease == nil && !h.recovering {
				h.recovering = true
				again = append(again, h)
			}
			h.mu.Unlock()
		}
		for _, h := range again {
			rt.recover(ctx, h)
		}
		batch = batch[:0]
	}
	for _, h := range hs {
		if !h.mu.TryLock() {
			continue
		}
		switch {
		case h.lease != nil && h.lease.Dirty():
			if batch = append(batch, h); len(batch) == rt.cfg.MaxBatch {
				flush()
			}
			continue
		case h.lease != nil:
			rt.ack(h, len(h.pending))
		case !h.done && len(h.pending) > 0 && !h.recovering:
			// A member dropped outside a commit, as by a close another
			// member's write refused: no record may come that reads it
			// again, so the round does.
			h.recovering = true
			h.mu.Unlock()
			rt.recover(ctx, h)
			continue
		case h.done && len(h.pending) == 0 && time.Since(h.doneAt) >= rt.cfg.ForgetAfter:
			rt.forget(h)
		}
		h.mu.Unlock()
	}
	if len(batch) > 0 {
		flush()
	}
}

// commit commits a batch of leases in one transaction. A lease the commit
// refused, or all of them if its outcome is unknown, loses its member, to
// be read again with its records applied again (recover): what a commit
// that landed made durable, they skip.
func (rt *Runtime) commit(ctx context.Context, batch []*held) {
	reqs := make([]store.CommitRequest, len(batch))
	for i, h := range batch {
		reqs[i] = h.lease.Request()
	}
	results, _, err := rt.cfg.Store.Commit(ctx, reqs)
	for i, h := range batch {
		if err == nil && len(results) == len(reqs) && results[i].Ref == reqs[i].Ref &&
			h.lease.Committed(results[i]) == nil {
			rt.landed(h, results[i], len(h.pending))
			continue
		}
		h.lease = nil
	}
}

// recover reads a lease that lost its member again, claimed for it
// (recovering), and applies its kept records again, in a goroutine of its
// own: a lease whose reads keep failing, or whose handler holds it, holds up
// only its own records, and never the round that started it.
func (rt *Runtime) recover(ctx context.Context, h *held) {
	rt.workers.Add(1)
	go func() {
		defer rt.workers.Done()
		h.mu.Lock()
		defer h.mu.Unlock()
		h.recovering = false
		if h.lease == nil && !h.done && len(h.pending) > 0 {
			rt.replay(ctx, h, 0)
		}
	}()
}

// forget drops a lease that is done and has nothing kept: a record of it
// that comes later reads it again, and finds it done.
func (rt *Runtime) forget(h *held) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	if rt.leases[h.id] == h {
		delete(rt.leases, h.id)
	}
}

// wait waits before Spanner is tried again, and reports false if ctx ended.
func (rt *Runtime) wait(ctx context.Context) bool {
	t := time.NewTimer(rt.cfg.Retry)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}
