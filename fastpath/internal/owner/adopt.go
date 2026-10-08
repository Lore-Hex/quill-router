package owner

import (
	"context"
	"crypto/sha256"
	"encoding/json"
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

// adoptAndReap is a lease's adoption and reaper, at a renewal round (§4.5):
// the drain-log rows committed since the last read are adopted, then each
// open hold whose last heartbeat's deadline plus the grace has passed is
// reaped, unless the drain log has a terminal for it, which is adopted
// instead. What Spanner or the record topic fails is tried at the next
// round.
func (o *Owner) adoptAndReap(ctx context.Context, l *Lease, now time.Time) {
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
	if o.cfg.Records == nil {
		return
	}
	for _, d := range l.due(now, o.cfg.Grace) {
		rows, _, err := o.cfg.Spanner.ReadHoldDrainRows(ctx, l.ref(), d.Auth)
		switch {
		case err != nil:
			continue
		case len(rows) > 0:
			_ = l.adopt(rows[0])
			continue
		}
		full, err := json.Marshal(d)
		if err != nil {
			continue
		}
		wctx, cancel := context.WithTimeout(ctx, o.cfg.AnswerWait)
		_, err = o.cfg.Records.Publish(d.Auth, settlelog.FullRecord, full).Wait(wctx)
		cancel()
		if err != nil {
			continue
		}
		digest := sha256.Sum256(full)
		_ = l.reap(d, digest[:])
	}
}

// adoptable: the lease is held, within its cutoff, and deciding.
func (l *Lease) adoptable() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return !l.let && l.withinCutoff(l.o.cfg.Clock())
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
	allocation := l.allocation
	l.allocation += row.DoorRaise
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
