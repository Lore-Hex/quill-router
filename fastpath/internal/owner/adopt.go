package owner

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"strings"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// FromLog is the settle log's publisher as the owner's Publisher.
func FromLog(l *settlelog.Log) Publisher { return leaseLog{l} }

type leaseLog struct{ l *settlelog.Log }

func (l leaseLog) Publish(lease string, data []byte) Waiter { return l.l.Publish(lease, data, nil) }
func (l leaseLog) Resume(lease string)                      { l.l.Resume(lease) }

// RecordLog publishes the record topic (settlelog.Records): a reap's full
// record goes there before its record does (§4.9).
type RecordLog interface {
	Publish(authorization, kind string, data []byte) Waiter
}

// FromRecords is the record topic's publisher as a RecordLog.
func FromRecords(r *settlelog.Records) RecordLog { return recordLog{r} }

type recordLog struct{ r *settlelog.Records }

func (r recordLog) Publish(authorization, kind string, data []byte) Waiter {
	return r.r.Publish(authorization, kind, data)
}

// adoptDrain is a lease's adoption, at a renewal round (§4.5): the
// drain-log rows committed since the last read are adopted, and a lease a
// renewal found past its cutoff admits and decides again. What Spanner
// fails is tried at the next round.
func (o *Owner) adoptDrain(ctx context.Context, l *Lease) {
	if !l.adoptable() {
		return
	}
	rows, read, err := o.cfg.Spanner.ReadDrainSince(ctx, l.ref(), l.adopted)
	if err != nil {
		return
	}
	for _, row := range rows {
		if l.adopt(row) != nil {
			return
		}
	}
	l.adopted = read
	l.mu.Lock()
	l.unadopted = false
	l.mu.Unlock()
}

// adoptable: the lease is held and within its cutoff.
func (l *Lease) adoptable() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return !l.let && l.withinCutoff(l.o.cfg.Clock())
}

// Reap is one pass of the owner's reaper (§4.5, §4.9), apart from the
// renewal rounds, so a record topic that is slow holds up no renewal: each
// open hold whose last heartbeat's deadline plus the grace has passed is
// reaped, unless the drain log has a terminal for it, which is adopted
// instead. A lease past its cutoff, or whose drain log a renewal left to
// adopt, is not reaped. Passes run one at a time, and each ends with ctx or
// the owner, which waits for it to end when it stops. What Spanner or the
// record topic fails is tried at the next pass.
func (o *Owner) Reap(ctx context.Context) error {
	if o.cfg.Spanner == nil || o.cfg.Records == nil {
		return errors.New("owner: no store or record topic to reap with")
	}
	o.mu.Lock()
	if o.stopped {
		o.mu.Unlock()
		return errors.New("owner: stopped")
	}
	o.rounds.Add(1)
	o.mu.Unlock()
	defer o.rounds.Done()
	o.reaping.Lock()
	defer o.reaping.Unlock()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer context.AfterFunc(o.ctx, cancel)()
	o.mu.Lock()
	leases := make([]*Lease, 0, len(o.leases))
	for _, l := range o.leases {
		leases = append(leases, l)
	}
	o.mu.Unlock()
	slices.SortFunc(leases, func(a, b *Lease) int { return strings.Compare(a.id, b.id) })
	now := o.cfg.Clock()
	for _, l := range leases {
		if !l.reapable() {
			continue
		}
		for _, d := range l.due(now, o.cfg.Grace) {
			o.reapOne(ctx, l, d)
		}
		l.releaseDue(now, o.cfg.FirstHeartbeat, o.cfg.Grace)
	}
	return nil
}

// releaseDue releases each stream's hold whose boot declares the heartbeat
// at stream open and for which no heartbeat record was issued by its
// admission plus the first-heartbeat allowance plus the grace (§4.5,
// TerminalOrder's OwnerRelease): uncharged, its record naming its boot
// binding. The test is that none was issued, not acknowledged: one issued
// and not yet acknowledged may still be stored, so the hold is reaped at
// its snapshot instead. With no allowance it releases none, and with an
// allowance and a grace whose sum is past the longest duration, none
// before their sum.
func (l *Lease) releaseDue(now time.Time, allowance, grace time.Duration) {
	if allowance == 0 {
		return
	}
	after := allowance + grace
	if after < allowance {
		after = math.MaxInt64
	}
	due := gather(l, func(h *hold) (string, bool) { return h.auth, released(h, now, after) })
	slices.Sort(due)
	for len(due) > 0 {
		n := min(len(due), releaseBatch)
		if !l.releaseSome(due[:n], now, after) {
			return
		}
		due = due[n:]
	}
}

// releaseBatch is how many releases a pass decides under one hold of the
// lease's lock: between batches a hand-off can take it.
const releaseBatch = 256

// scanBatch is how many holds a pass over a lease's holds visits under one
// hold of its lock.
const scanBatch = 1024

// scan calls f with each of the lease's holds under its lock, which it lets
// go between batches of scanBatch, so a pass over many holds keeps a
// hand-off waiting no longer than a batch takes; it stops once the lease is
// let go or handed off. Between batches, and at the end, it calls flush
// with the lock let go: f keeps what it finds in a buffer of scanBatch,
// which never grows under the lock, and flush moves it out, so nothing a
// pass gathers is copied under the lock as it grows with the holds. A map
// may change while it is ranged over, and here each change comes under the
// lock, between batches: a hold removed then is not visited after, and one
// added may be visited or not, so a pass rechecks each hold as it decides
// it.
func (l *Lease) scan(f func(h *hold), flush func()) {
	l.mu.Lock()
	n := 0
	for _, h := range l.holds {
		if l.let || l.handedOff {
			break
		}
		f(h)
		if n++; n%scanBatch == 0 {
			l.mu.Unlock()
			flush()
			l.mu.Lock()
		}
	}
	l.mu.Unlock()
	flush()
}

// released: a hold due for release at now, after the first-heartbeat
// allowance and the grace.
func released(h *hold, now time.Time, after time.Duration) bool {
	return h.openHeartbeat && !h.heartbeat && !now.Before(h.admitted.Add(after))
}

// releaseSome releases each of auths still open and due, under the lease's
// lock, and reports whether the pass goes on: not after a decision fails, as
// every one does once the lease is let go or handed off.
func (l *Lease) releaseSome(auths []string, now time.Time, after time.Duration) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, auth := range auths {
		if h := l.holds[auth]; h == nil || !released(h, now, after) {
			continue
		}
		if _, err := l.decide(auth, terminalOf{kind: record.Release}); err != nil {
			return false
		}
	}
	return true
}

// reapable: the lease is held, within its cutoff, and its drain log
// adopted since a renewal found it past its cutoff.
func (l *Lease) reapable() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return !l.let && !l.unadopted && l.withinCutoff(l.o.cfg.Clock())
}

// reapOne reaps a due hold: unless the drain log has a terminal for it,
// whose first row is adopted instead, its full record goes to the record
// topic, and once that is acknowledged the reap is decided.
func (o *Owner) reapOne(ctx context.Context, l *Lease, d dueReap) {
	rows, _, err := o.cfg.Spanner.ReadHoldDrainRows(ctx, l.ref(), d.Auth)
	switch {
	case err != nil:
		return
	case len(rows) > 0:
		_ = l.adopt(rows[0])
		return
	}
	full, err := json.Marshal(d)
	if err != nil {
		return
	}
	wctx, cancel := context.WithTimeout(ctx, o.cfg.AnswerWait)
	_, err = o.cfg.Records.Publish(d.Auth, settlelog.FullRecord, full).Wait(wctx)
	cancel()
	if err != nil {
		return
	}
	digest := sha256.Sum256(full)
	_ = l.reap(d, digest[:])
}

// adopt adopts a drain-log row (§4.5): a front door's terminal for a hold
// the owner has not decided, published as the owner's own record carrying
// the row's record ID, so the auditor knows its drain-log copy by identity.
// The row's raise, which its append made for a charge above the hold, is
// counted as allocation first, so the owner does not reserve for the row
// again. A row of a hold not open changes nothing: one decided already, its
// terminal first in the lease's order, or one the lease never held. Nor
// does a reap, which only the auditor appends, once the lease is draining.
func (l *Lease) adopt(row store.DrainRow) error {
	kind := record.Kind(row.Kind)
	if kind != record.Settle && kind != record.Refund {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.holds[row.AuthorizationID] == nil {
		return nil
	}
	t := terminalOf{kind: kind, drain: row.RecordID}
	if kind == record.Settle {
		t.charge, t.digest = row.Charge, row.Digest
	}
	raised, ok := add(l.allocation, row.DoorRaise)
	if !ok {
		return fmt.Errorf("owner: a raise of %d the lease's allocation cannot hold", row.DoorRaise)
	}
	allocation := l.allocation
	l.allocation = raised
	if _, err := l.decide(row.AuthorizationID, t); err != nil {
		l.allocation = allocation
		return err
	}
	return nil
}

// gather is what visit takes from each of the lease's holds, in a scan: a
// batch of at most scanBatch, made under the lock, joins the result only with
// the lock let go, so nothing a pass gathers grows, and is copied, under the
// lock with the lease's holds.
func gather[T any](l *Lease, visit func(h *hold) (T, bool)) []T {
	var out []T
	batch := make([]T, 0, scanBatch)
	l.scan(func(h *hold) {
		if x, ok := visit(h); ok {
			batch = append(batch, x)
		}
	}, func() {
		out, batch = append(out, batch...), batch[:0]
	})
	return out
}

// dueReap is a hold the reaper reaps, as its full record states it (§4.9):
// at its last heartbeat's snapshot, the running charge, the basis the first
// brought, and the boot binding.
type dueReap struct {
	Lease      string    `json:"lease"`
	Auth       string    `json:"a"`
	Estimate   int64     `json:"est"`
	Charge     int64     `json:"charge"`
	Deadline   time.Time `json:"deadline"`
	GatewaySeq int64     `json:"gseq"`
	Hash       []byte    `json:"hash"`
	Usage      int64     `json:"usage"`
	OwnerSeq   int64     `json:"snap"`
	Basis      []byte    `json:"basis"`
	Boot       []byte    `json:"boot"`
}

// due are the lease's open holds whose last heartbeat's deadline plus the
// grace has passed, in order of their authorizations, found by a scan and
// sorted once its lock is let go. A hold that never heartbeated has no
// snapshot to reap at.
func (l *Lease) due(now time.Time, grace time.Duration) []dueReap {
	out := gather(l, func(h *hold) (dueReap, bool) {
		if !h.heartbeat || now.Before(h.deadline.Add(grace)) {
			return dueReap{}, false
		}
		return dueReap{Lease: l.id, Auth: h.auth, Estimate: h.estimate, Charge: h.running,
			Deadline: h.deadline.UTC(), GatewaySeq: h.gatewaySeq, Hash: h.hash, Usage: h.usage, OwnerSeq: h.snapSeq,
			Basis: h.basis, Boot: h.boot}, true
	})
	slices.SortFunc(out, func(a, b dueReap) int { return strings.Compare(a.Auth, b.Auth) })
	return out
}

// reap decides a reap of a due hold at the snapshot its full record states:
// unless it was decided meanwhile, so no longer open, or a heartbeat since
// moved its snapshot, when the next round looks again.
func (l *Lease) reap(d dueReap, digest []byte) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	h := l.holds[d.Auth]
	if h == nil || h.snapSeq != d.OwnerSeq {
		return nil
	}
	_, err := l.decide(d.Auth, terminalOf{kind: record.Reap, charge: d.Charge, digest: digest, snapSeq: d.OwnerSeq})
	return err
}
