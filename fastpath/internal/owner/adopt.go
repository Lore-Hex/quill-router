package owner

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
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
	}
	return nil
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
// grace has passed, in order of their authorizations. A hold that never
// heartbeated has no snapshot to reap at.
func (l *Lease) due(now time.Time, grace time.Duration) []dueReap {
	l.mu.Lock()
	defer l.mu.Unlock()
	var out []dueReap
	for _, h := range l.holds {
		if !h.heartbeat || now.Before(h.deadline.Add(grace)) {
			continue
		}
		out = append(out, dueReap{Lease: l.id, Auth: h.auth, Estimate: h.estimate, Charge: h.running,
			Deadline: h.deadline.UTC(), GatewaySeq: h.gatewaySeq, Hash: h.hash, Usage: h.usage, OwnerSeq: h.snapSeq,
			Basis: h.basis, Boot: h.boot})
	}
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
