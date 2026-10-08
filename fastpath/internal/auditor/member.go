// Package auditor is the lease auditor (design §4.8; spike plan §2): a member
// of the consumer group on a region's settle log, and the only writer of
// lease consumption. This part is a member's lease in memory, as
// AuditorCommit's member keeps it (proofs/AuditorCommit.tla,
// internal/auditorcommit): what it loaded from the store, the records it
// applies in the lease's order, and the commit that makes them durable. The
// subscription, ticks, draining, reaps and the close come in later parts of
// S5.
package auditor

import (
	"bytes"
	"errors"
	"fmt"
	"slices"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Outcome is what applying a record did.
type Outcome int

const (
	// Applied: the record is applied, and the next commit carries it.
	Applied Outcome = iota
	// Skipped: a redelivery or duplicate (an owner record at or below the
	// progress, a tick at or below the last), or an owner record once S is
	// known, which is ignored whenever it arrives (§4.8).
	Skipped
	// Gap: an owner record beyond the next sequence number. The member
	// commits what it applied, then stops the lease (store.StopForGap), and
	// books nothing after it.
	Gap
	// Behind: the member knows too little of the lease to apply the
	// record. A tick needs the lease's fence F: the member reads the lease
	// (Drained) and applies it again. An owner record of a lease the member
	// knows has drained needs its stored winners (LoadWinners) first, as
	// AuditorCommit's ApplyRecord does. Until it learns the lease drained,
	// at a load, a tick or a commit, a member applies owner records without
	// them: the spec's guard sweep finds that guard breaks no claim, since
	// no stored winner can decide an owner record above the stored
	// progress (proofs/AuditorCommit.guards.toml).
	Behind
)

// Lease is a member's lease: the row as it loaded or last committed it, and
// what it has applied since.
type Lease struct {
	ref  store.LeaseRef
	skew time.Duration

	version  int64
	draining bool
	fence    time.Time // F, known once the lease drains and the member loaded it so
	applied  int64     // the highest owner sequence number applied
	lastTick int64
	osum     int64 // what the owner's terminals applied charge, the checkpoint audit's sum
	s        int64 // the boundary S, once stored or applied
	sKnown   bool
	listed   bool
	holds    map[string]*hold
	winners  map[string]bool // decided: stored, or applied since
	// winnersLoaded: the member knows every stored winner, as it does
	// for a lease that was draining when it loaded it, or after LoadWinners.
	winnersLoaded bool
	shortfall     int64 // the highest shortfall total a terminal carried

	// Applied and not yet committed.
	dirty     bool
	money     []store.MoneyOp
	won       []store.Winner
	put       map[string]bool
	boundary  *store.Boundary
	listedSeq *int64
	fault     *int64
	chunks    map[int64][]record.HeldHold
}

// hold is an open hold the log has shown, as its row stores it.
type hold struct {
	row store.HoldRow
}

// Load is a member's lease as store.Load read it. skew is the skew
// allowance: a tick at or past the fence F plus it is the fence tick.
func Load(ref store.LeaseRef, l store.Loaded, skew time.Duration) (*Lease, error) {
	if l.Lease.State == "closed" {
		return nil, errors.New("auditor: the lease is closed")
	}
	out := &Lease{ref: ref, skew: skew, version: l.Lease.CommitVersion, draining: l.Lease.State == "draining",
		applied: l.Lease.AppliedSeq, lastTick: l.Lease.LastTick, osum: l.Lease.AuditOsum,
		listed: l.Lease.HoldsListedSeq.Valid, holds: map[string]*hold{}, winners: map[string]bool{},
		shortfall: l.Lease.ShortfallTotal, put: map[string]bool{}, chunks: map[int64][]record.HeldHold{}}
	if l.Lease.FenceTime.Valid {
		out.fence = l.Lease.FenceTime.Time
	}
	if l.Lease.BoundarySeq.Valid {
		out.s, out.sKnown = l.Lease.BoundarySeq.Int64, true
	}
	for _, h := range l.Holds {
		out.holds[h.AuthorizationID] = &hold{row: h}
	}
	if out.draining {
		out.loadWinners(l.Packs)
	}
	return out, nil
}

// Drained takes the fence F of a lease that drained since the member loaded
// it open, from the lease's row as read: the draining write stores it.
func (l *Lease) Drained(lease store.Lease) error {
	if !lease.FenceTime.Valid {
		return errors.New("auditor: the lease has no fence: it is still open")
	}
	l.draining, l.fence = true, lease.FenceTime.Time
	return nil
}

// LoadWinners takes the winners and fence of a lease that drained since the
// member loaded it open (AuditorCommit's LoadWinners): the winners beside
// the ones it decided itself.
func (l *Lease) LoadWinners(lease store.Lease, packs []store.Pack) error {
	if err := l.Drained(lease); err != nil {
		return err
	}
	l.loadWinners(packs)
	return nil
}

func (l *Lease) loadWinners(packs []store.Pack) {
	for _, p := range packs {
		for _, w := range p.Winners {
			l.winners[w.AuthorizationID] = true
		}
	}
	l.winnersLoaded = true
}

// Apply applies a record of the lease, delivered in the lease's order, the
// tick's publish time with it.
func (l *Lease) Apply(r record.Record, published time.Time) (Outcome, error) {
	if r.Lease != l.ref.LeaseID {
		return 0, fmt.Errorf("auditor: a record of lease %s applied to %s", r.Lease, l.ref.LeaseID)
	}
	if r.Kind == record.Tick {
		return l.applyTick(r, published)
	}
	switch {
	case l.sKnown || r.Seq <= l.applied:
		return Skipped, nil
	case r.Seq > l.applied+1:
		return Gap, nil
	case l.draining && !l.winnersLoaded:
		return Behind, nil
	}
	switch r.Kind {
	case record.Heartbeat:
		l.heartbeat(r)
	case record.Settle, record.Refund, record.Reap, record.Release:
		l.terminal(r)
	case record.Checkpoint:
		l.checkpoint(r)
	case record.Handoff:
		l.handoff(r)
	case record.Manifest:
		l.manifest(r)
	default:
		return 0, fmt.Errorf("auditor: a %s record of the owner", r.Kind)
	}
	l.applied, l.dirty = r.Seq, true
	return Applied, nil
}

// heartbeat keeps an open hold's latest valid snapshot (the owner validated
// it): a hold decided already keeps none.
func (l *Lease) heartbeat(r record.Record) {
	if l.winners[r.Auth] {
		return
	}
	h := l.holds[r.Auth]
	if h == nil {
		h = &hold{row: store.HoldRow{AuthorizationID: r.Auth, Estimate: r.Estimate}}
		l.holds[r.Auth] = h
	}
	s := r.Snapshot
	h.row.Deadline = s.Deadline
	h.row.SnapshotSeq = spanner.NullInt64{Int64: s.GatewaySeq, Valid: true}
	h.row.SnapshotHash, h.row.SnapshotUsage = s.Hash, s.Usage
	h.row.RunningCharge = spanner.NullInt64{Int64: s.Running, Valid: true}
	h.row.SnapshotOwnerSeq = spanner.NullInt64{Int64: r.Seq, Valid: true}
	if r.First {
		h.row.ReapBasis = r.Basis
	}
	l.put[r.Auth] = true
}

// terminal applies an owner terminal: the first for its authorization wins
// and is booked, its record's shortfall total first (§4.2, §4.5); a later
// one charges nothing. The checkpoint audit's sum counts every one.
func (l *Lease) terminal(r record.Record) {
	if !l.winners[r.Auth] {
		id := fmt.Sprintf("o%d", r.Seq)
		if r.Drain != "" {
			// An adopted row: its drain-log copy is recognized by its ID.
			id = r.Drain
		}
		l.winners[r.Auth] = true
		l.won = append(l.won, store.Winner{AuthorizationID: r.Auth, Kind: string(r.Kind), Charge: r.Charge,
			RecordID: id})
		l.money = append(l.money, store.Book(r.Charge, r.Shortfall))
		delete(l.holds, r.Auth)
		delete(l.put, r.Auth)
	} else if r.Shortfall > l.shortfall {
		l.money = append(l.money, store.Book(0, r.Shortfall))
	}
	l.shortfall = max(l.shortfall, r.Shortfall)
	l.osum += r.Charge
}

// checkpoint audits the owner (§4.8): its consumed must be what its
// terminals with lower numbers charged. A difference is a fault, which the
// commit stores and which revokes the lease. A return leaves the
// allocation; a final checkpoint lists the open holds: none.
func (l *Lease) checkpoint(r record.Record) {
	c := r.Checkpoint
	if c.Consumed != l.osum && l.fault == nil {
		seq := r.Seq
		l.fault = &seq
	}
	if c.Return > 0 {
		l.money = append(l.money, store.Return(c.Return))
	}
	if c.Final {
		l.list(r.Seq)
	}
}

// handoff keeps a forced exit's chunk of open holds, until its manifest.
func (l *Lease) handoff(r record.Record) {
	l.chunks[r.Seq] = slices.Clone(r.Holds)
}

// manifest lists the open holds once every chunk it names is applied and
// their holds have its digest; a partial hand-off counts as none (§4.8).
func (l *Lease) manifest(r record.Record) {
	m := r.Manifest
	var holds []record.HeldHold
	for _, seq := range m.Seqs {
		chunk, ok := l.chunks[seq]
		if !ok {
			return
		}
		holds = append(holds, chunk...)
	}
	digest, err := record.HoldsDigest(holds)
	if err != nil || !bytes.Equal(digest, m.HoldsDigest) {
		return
	}
	for _, held := range holds {
		if l.winners[held.Auth] {
			continue
		}
		row := store.HoldRow{AuthorizationID: held.Auth, Estimate: held.Estimate, Deadline: held.Deadline, Listed: true}
		if s := held.Snapshot; s != nil {
			row.SnapshotSeq = spanner.NullInt64{Int64: s.GatewaySeq, Valid: true}
			row.SnapshotHash, row.SnapshotUsage = s.Hash, s.Usage
			row.RunningCharge = spanner.NullInt64{Int64: s.Running, Valid: true}
			row.SnapshotOwnerSeq = spanner.NullInt64{Int64: held.SnapshotSeq, Valid: true}
			row.ReapBasis = held.Basis
		}
		l.holds[held.Auth] = &hold{row: row}
		l.put[held.Auth] = true
	}
	l.list(r.Seq)
}

func (l *Lease) list(seq int64) {
	if !l.listed {
		l.listed, l.listedSeq = true, &seq
	}
}

// applyTick applies one of the auditor's ticks: one at or below the last is
// a redelivery. The first past the fence F plus the skew allowance, on a
// draining lease, is the fence tick: S becomes the highest owner sequence
// number applied, with T its publish time, unless the member knows a stored
// S (§4.8). Another tick changes nothing a commit must store: the next
// commit carries it as the last, and one redelivered after a crash before
// then changes nothing again.
func (l *Lease) applyTick(r record.Record, published time.Time) (Outcome, error) {
	if r.TickNumber <= l.lastTick {
		return Skipped, nil
	}
	if l.fence.IsZero() {
		return Behind, nil
	}
	l.lastTick = r.TickNumber
	if !l.sKnown && !r.TickAt.Before(l.fence.Add(l.skew)) {
		l.s, l.sKnown, l.dirty = l.applied, true, true
		l.boundary = &store.Boundary{S: l.applied, T: published}
		// The holds' rows are stored with S: none is listed by this.
	}
	return Applied, nil
}

// ApplyRow applies a draining lease's next drain-log row, once S is known
// and every owner record up to it applied (§4.8): the first terminal for its
// authorization wins, after the owner's records. Its booking raises no
// shortfall total: the row's raise is the append's, already on the row, and
// every total the member knows a booking before it in the commit carries.
func (l *Lease) ApplyRow(row store.DrainRow) error {
	if !l.draining || !l.sKnown || !l.winnersLoaded {
		return errors.New("auditor: a drain-log row before S is known")
	}
	if l.winners[row.AuthorizationID] {
		return nil
	}
	l.winners[row.AuthorizationID] = true
	l.won = append(l.won, store.Winner{AuthorizationID: row.AuthorizationID, Kind: row.Kind, Charge: row.Charge,
		FromDrain: true, RecordID: row.RecordID})
	l.money = append(l.money, store.Book(row.Charge, 0))
	delete(l.holds, row.AuthorizationID)
	delete(l.put, row.AuthorizationID)
	l.dirty = true
	return nil
}

// Dirty reports whether the member applied anything since its last commit.
func (l *Lease) Dirty() bool { return l.dirty }

// Request is the commit of what the member applied (§4.8): its version and
// progress, the last tick, the audit's sum, the money in log order, the
// holds it changed, the winners, and S, the holds' listing and a fault once
// known.
func (l *Lease) Request() store.CommitRequest {
	req := store.CommitRequest{Ref: l.ref, ReadVersion: l.version, AppliedSeq: l.applied, LastTick: l.lastTick,
		AuditOsum: l.osum, Money: slices.Clone(l.money), Winners: slices.Clone(l.won), Boundary: l.boundary,
		HoldsListedSeq: l.listedSeq, AuditFault: l.fault}
	for auth := range l.put {
		req.PutHolds = append(req.PutHolds, l.holds[auth].row)
	}
	slices.SortFunc(req.PutHolds, func(a, b store.HoldRow) int {
		switch {
		case a.AuthorizationID < b.AuthorizationID:
			return -1
		case a.AuthorizationID > b.AuthorizationID:
			return 1
		}
		return 0
	})
	return req
}

// ErrReread: the store refused the member's write; the member re-reads the
// lease (Load again) and applies again from what Pub/Sub redelivers.
var ErrReread = errors.New("auditor: another member has the lease's newer state; re-read it")

// Committed takes the commit's result: the version it advanced to, the
// lease's state, and nothing pending; or ErrReread.
func (l *Lease) Committed(got store.CommitResult) error {
	if got.Refused != "" {
		return fmt.Errorf("%w: %s", ErrReread, got.Refused)
	}
	l.version = got.NewVersion
	l.draining = l.draining || got.State != "open"
	l.dirty, l.money, l.won, l.put, l.boundary, l.listedSeq, l.fault = false, nil, nil, map[string]bool{}, nil, nil, nil
	return nil
}
