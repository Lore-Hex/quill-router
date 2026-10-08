package auditor

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"reflect"
	"slices"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

var (
	ref      = store.LeaseRef{Workspace: "ws-1", LeaseID: "lease-1"}
	start    = time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)
	deadline = start.Add(time.Minute)
	fence    = start.Add(10 * time.Minute)
	boot     = []byte("boot-1")
)

func sum(s string) []byte {
	h := sha256.Sum256([]byte(s))
	return h[:]
}

// loaded is a lease as store.Load reads it: open, or draining with its fence.
func loaded(state string) store.Loaded {
	l := store.Lease{Ref: ref, State: state, Granted: 1000, Allocation: 1000, CommitVersion: 3}
	if state == "draining" {
		l.FenceTime = spanner.NullTime{Time: fence, Valid: true}
	}
	return store.Loaded{Lease: l}
}

func load(t *testing.T, state string) *Lease {
	t.Helper()
	l, err := Load(ref, loaded(state), 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return l
}

func owner(seq int64, kind record.Kind) record.Record {
	return record.Record{Version: record.Version, Lease: "lease-1", Epoch: 1, Seq: seq, Kind: kind}
}

func hb(seq int64, auth string, gseq, running int64) record.Record {
	r := owner(seq, record.Heartbeat)
	r.Auth, r.Estimate = auth, 100
	r.Snapshot = &record.Snapshot{GatewaySeq: gseq, Hash: sum(auth), Usage: []byte(`{"tokens":1}`), Running: running,
		Deadline: deadline}
	if gseq == 1 {
		r.First, r.Basis = true, []byte("terms")
	}
	return r
}

func settle(seq int64, auth string, charge, shortfall int64) record.Record {
	r := owner(seq, record.Settle)
	r.Auth, r.Estimate, r.Charge, r.Shortfall, r.Digest = auth, 100, charge, shortfall, sum("full "+auth)
	return r
}

func refund(seq int64, auth string) record.Record {
	r := owner(seq, record.Refund)
	r.Auth, r.Estimate, r.Boot = auth, 100, boot
	return r
}

func ckpt(seq int64, c record.CheckpointOf) record.Record {
	r := owner(seq, record.Checkpoint)
	r.Checkpoint = &c
	return r
}

func tick(n int64, at time.Time) record.Record {
	return record.Record{Version: record.Version, Lease: "lease-1", Kind: record.Tick, TickNumber: n, TickAt: at}
}

func apply(t *testing.T, l *Lease, want Outcome, rs ...record.Record) {
	t.Helper()
	for _, r := range rs {
		if err := r.Validate(); err != nil {
			t.Fatalf("the test's record %+v: %v", r, err)
		}
		got, err := l.Apply(r, start)
		if err != nil || got != want {
			t.Fatalf("applying %s %d: %v %v, want %v", r.Kind, r.Seq, got, err, want)
		}
	}
}

// TestTheMemberAppliesALeasesRecords: heartbeats keep the open holds'
// latest snapshots, the first terminal for each authorization wins and is
// booked with its record's shortfall total, and the commit carries it all
// with the progress and the audit's sum.
func TestTheMemberAppliesALeasesRecords(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, hb(1, "a", 1, 10), hb(2, "b", 1, 5), hb(3, "a", 2, 30), settle(4, "a", 150, 50),
		ckpt(5, record.CheckpointOf{Consumed: 150, Open: 1, OpenSum: 100, LatestEnd: deadline, KeyStatus: 7}))
	req := l.Request()
	b := hb(2, "b", 1, 5)
	wantHold := store.HoldRow{AuthorizationID: "b", Estimate: 100, Deadline: deadline,
		SnapshotSeq: spanner.NullInt64{Int64: 1, Valid: true}, SnapshotHash: b.Snapshot.Hash,
		SnapshotUsage: b.Snapshot.Usage, RunningCharge: spanner.NullInt64{Int64: 5, Valid: true},
		SnapshotOwnerSeq: spanner.NullInt64{Int64: 2, Valid: true}, ReapBasis: []byte("terms")}
	switch {
	case req.Ref != ref || req.ReadVersion != 3 || req.AppliedSeq != 5 || req.AuditOsum != 150 || req.AuditFault != nil:
		t.Fatalf("the commit: %+v", req)
	case !reflect.DeepEqual(req.Money, []store.MoneyOp{store.Book(150, 50)}):
		t.Fatalf("the money: %+v", req.Money)
	case !reflect.DeepEqual(req.Winners, []store.Winner{{AuthorizationID: "a", Kind: "settle", Charge: 150, RecordID: "o4"}}):
		t.Fatalf("the winners: %+v", req.Winners)
	case len(req.PutHolds) != 1 || !reflect.DeepEqual(req.PutHolds[0], wantHold):
		t.Fatalf("the holds: %+v, want %+v", req.PutHolds, wantHold)
	}
	if err := l.Committed(store.CommitResult{Ref: ref, NewVersion: 4, State: "open"}); err != nil {
		t.Fatal(err)
	}
	if l.Dirty() {
		t.Fatal("dirty after its commit")
	}
	apply(t, l, Applied, refund(6, "b"))
	req = l.Request()
	if req.ReadVersion != 4 || req.AppliedSeq != 6 || len(req.PutHolds) != 0 ||
		!reflect.DeepEqual(req.Winners, []store.Winner{{AuthorizationID: "b", Kind: "refund", RecordID: "o6"}}) ||
		!reflect.DeepEqual(req.Money, []store.MoneyOp{store.Book(0, 0)}) {
		t.Fatalf("the next commit: %+v", req)
	}
}

// TestALaterTerminalChargesNothing, though the audit's sum counts it, and
// its shortfall total, if higher, still raises the stored one.
func TestALaterTerminalChargesNothing(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, settle(1, "a", 80, 0), settle(2, "a", 300, 40))
	req := l.Request()
	if req.AuditOsum != 380 || len(req.Winners) != 1 ||
		!reflect.DeepEqual(req.Money, []store.MoneyOp{store.Book(80, 0), store.Book(0, 40)}) {
		t.Fatalf("the commit: %+v", req)
	}
}

// TestRedeliveriesAndGaps: an owner record at or below the progress is a
// redelivery; one past the next is a gap, applied not at all.
func TestRedeliveriesAndGaps(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, hb(1, "a", 1, 10), hb(2, "a", 2, 20))
	apply(t, l, Skipped, hb(1, "a", 1, 10), hb(2, "a", 2, 20))
	before := l.Request()
	apply(t, l, Gap, settle(4, "a", 30, 0))
	if after := l.Request(); !reflect.DeepEqual(after, before) {
		t.Fatalf("a gap changed what the member applied: %+v, then %+v", before, after)
	}
}

// TestTheCheckpointAudit: a checkpoint's consumed must be what the owner's
// terminals with lower numbers charged; the first difference is the fault
// the commit stores. A return leaves the allocation; a final checkpoint
// lists the open holds.
func TestTheCheckpointAudit(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, settle(1, "a", 80, 0), ckpt(2, record.CheckpointOf{Consumed: 70, KeyStatus: 7}),
		ckpt(3, record.CheckpointOf{Consumed: 60, KeyStatus: 7, Return: 25}),
		ckpt(4, record.CheckpointOf{Consumed: 80, KeyStatus: 7, Final: true}))
	req := l.Request()
	if req.AuditFault == nil || *req.AuditFault != 2 || req.HoldsListedSeq == nil || *req.HoldsListedSeq != 4 ||
		!reflect.DeepEqual(req.Money, []store.MoneyOp{store.Book(80, 0), store.Return(25)}) {
		t.Fatalf("the commit: %+v", req)
	}
}

// TestTheFenceTickStoresS: on a draining lease, a tick before the fence F
// plus the skew is only a tick; the first at or after it makes S the
// highest owner sequence number applied, with T its publish time. Owner
// records after it are ignored, and a redelivered tick skipped.
func TestTheFenceTickStoresS(t *testing.T) {
	quiet := load(t, "draining")
	if got, _ := quiet.Apply(tick(1, fence.Add(time.Second)), start); got != Applied || quiet.Dirty() {
		t.Fatalf("an early tick on a lease with nothing applied: %v, dirty %v", got, quiet.Dirty())
	}
	l := load(t, "draining")
	apply(t, l, Applied, hb(1, "a", 1, 10), settle(2, "b", 40, 0))
	if got, err := l.Apply(tick(1, fence.Add(time.Second)), start); err != nil || got != Applied {
		t.Fatalf("an early tick: %v %v", got, err)
	}
	if req := l.Request(); req.Boundary != nil || req.LastTick != 1 {
		t.Fatalf("an early tick stored S: %+v", req)
	}
	published := fence.Add(3 * time.Second)
	if got, err := l.Apply(tick(2, fence.Add(2*time.Second)), published); err != nil || got != Applied {
		t.Fatalf("the fence tick: %v %v", got, err)
	}
	if got, _ := l.Apply(tick(2, fence.Add(2*time.Second)), published); got != Skipped {
		t.Fatalf("a redelivered tick: %v", got)
	}
	apply(t, l, Skipped, settle(3, "a", 50, 0))
	req := l.Request()
	if req.Boundary == nil || *req.Boundary != (store.Boundary{S: 2, T: published}) || req.AppliedSeq != 2 ||
		req.LastTick != 2 {
		t.Fatalf("the commit with S: %+v", req)
	}
}

// TestAMemberBehindReadsTheLease: a tick for a lease the member loaded open
// waits for its fence; an owner record of a lease it knows has drained, for
// its winners. Then each applies.
func TestAMemberBehindReadsTheLease(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, settle(1, "a", 40, 0))
	if got, _ := l.Apply(tick(1, fence.Add(time.Hour)), start); got != Behind {
		t.Fatalf("a tick for a lease loaded open: %v", got)
	}
	lease := loaded("draining").Lease
	if err := l.Drained(lease); err != nil {
		t.Fatal(err)
	}
	if got, _ := l.Apply(settle(2, "b", 7, 0), start); got != Behind {
		t.Fatalf("an owner record of a lease known drained, its winners not loaded: %v", got)
	}
	packs := []store.Pack{{CommitVersion: 2, Winners: []store.Winner{{AuthorizationID: "c", Kind: "refund", RecordID: "o9"}}}}
	if err := l.LoadWinners(lease, packs); err != nil {
		t.Fatal(err)
	}
	apply(t, l, Applied, settle(2, "b", 7, 0))
	if got, err := l.Apply(tick(1, fence.Add(time.Hour)), start); err != nil || got != Applied {
		t.Fatalf("the tick after: %v %v", got, err)
	}
	if err := l.ApplyRow(store.DrainRow{AuthorizationID: "c", RecordID: "d1", Kind: "settle", Charge: 9}); err != nil {
		t.Fatal(err)
	}
	if req := l.Request(); len(req.Winners) != 2 || req.Winners[0].AuthorizationID != "a" ||
		req.Winners[1].AuthorizationID != "b" {
		t.Fatalf("a row for a stored winner was decided: %+v", req.Winners)
	}
}

// TestDrainRowsAfterS: once S is known the drain log's rows are booked in
// order after the owner's records, the first for an authorization winning;
// before S none is.
func TestDrainRowsAfterS(t *testing.T) {
	l := load(t, "draining")
	apply(t, l, Applied, settle(1, "a", 40, 0))
	row := func(auth, id string, charge int64) store.DrainRow {
		return store.DrainRow{AuthorizationID: auth, RecordID: id, Kind: "settle", Charge: charge}
	}
	if err := l.ApplyRow(row("b", "d1", 5)); err == nil {
		t.Fatal("a drain-log row before S")
	}
	if _, err := l.Apply(tick(1, fence.Add(time.Hour)), start); err != nil {
		t.Fatal(err)
	}
	for _, r := range []store.DrainRow{row("a", "d0", 99), row("b", "d1", 5), row("b", "d2", 7)} {
		if err := l.ApplyRow(r); err != nil {
			t.Fatal(err)
		}
	}
	req := l.Request()
	want := []store.Winner{{AuthorizationID: "a", Kind: "settle", Charge: 40, RecordID: "o1"},
		{AuthorizationID: "b", Kind: "settle", Charge: 5, FromDrain: true, RecordID: "d1"}}
	if !reflect.DeepEqual(req.Winners, want) ||
		!reflect.DeepEqual(req.Money, []store.MoneyOp{store.Book(40, 0), store.Book(5, 0)}) {
		t.Fatalf("the commit: %+v", req)
	}
}

// TestAHandOffListsTheHoldsWhole: a manifest lists the open holds only with
// every chunk it names applied and their holds' digest its own; a hold
// decided already is not put again.
func TestAHandOffListsTheHoldsWhole(t *testing.T) {
	held := []record.HeldHold{{Auth: "a", Estimate: 100, Deadline: deadline, Boot: boot},
		{Auth: "b", Estimate: 50, Deadline: deadline, Boot: boot}}
	digest, err := record.HoldsDigest(held)
	if err != nil {
		t.Fatal(err)
	}
	first, err := record.HoldsDigest(held[:1])
	if err != nil {
		t.Fatal(err)
	}
	chunk := func(seq int64, holds ...record.HeldHold) record.Record {
		r := owner(seq, record.Handoff)
		r.Holds = holds
		return r
	}
	manifest := func(seq int64, d []byte, seqs ...int64) record.Record {
		r := owner(seq, record.Manifest)
		r.Manifest = &record.ManifestOf{Chunks: len(seqs), HoldsDigest: d, Seqs: seqs}
		return r
	}
	for name, c := range map[string]struct {
		rs     []record.Record
		listed int64
		put    []string
	}{
		"whole":          {[]record.Record{chunk(1, held[0]), chunk(2, held[1]), manifest(3, digest, 1, 2)}, 3, []string{"a", "b"}},
		"a chunk short":  {[]record.Record{chunk(1, held[0]), manifest(2, digest, 1, 3)}, 0, nil},
		"another digest": {[]record.Record{chunk(1, held[0]), chunk(2, held[1]), manifest(3, sum("x"), 1, 2)}, 0, nil},
		// A record it names that is no chunk counts as one missing, though
		// the digest is the chunks' found.
		"a heartbeat named": {[]record.Record{chunk(1, held[0]), hb(2, "c", 1, 1), manifest(3, first, 1, 2)}, 0, []string{"c"}},
		"a hold decided":    {[]record.Record{settle(1, "a", 9, 0), chunk(2, held...), manifest(3, digest, 2)}, 3, []string{"b"}},
	} {
		l := load(t, "open")
		for _, r := range c.rs {
			if _, err := l.Apply(r, start); err != nil {
				t.Fatalf("%s: %v", name, err)
			}
		}
		req := l.Request()
		var put []string
		for _, h := range req.PutHolds {
			put = append(put, h.AuthorizationID)
			if h.Listed != (c.listed != 0) {
				t.Fatalf("%s: hold %+v", name, h)
			}
		}
		if (req.HoldsListedSeq != nil) != (c.listed != 0) || (c.listed != 0 && *req.HoldsListedSeq != c.listed) ||
			!slices.Equal(put, c.put) {
			t.Fatalf("%s: listed at %v, holds %+v", name, req.HoldsListedSeq, req.PutHolds)
		}
	}
}

// TestAListingIsStoredOnce: a lease whose holds are listed already lists
// them at no later record.
func TestAListingIsStoredOnce(t *testing.T) {
	loaded := loaded("open")
	loaded.Lease.HoldsListedSeq = spanner.NullInt64{Int64: 2, Valid: true}
	loaded.Lease.AppliedSeq = 2
	l, err := Load(ref, loaded, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	apply(t, l, Applied, ckpt(3, record.CheckpointOf{Final: true}))
	if req := l.Request(); req.HoldsListedSeq != nil {
		t.Fatalf("listed again at %d", *req.HoldsListedSeq)
	}
	l = load(t, "open")
	apply(t, l, Applied, ckpt(1, record.CheckpointOf{Final: true}), ckpt(2, record.CheckpointOf{Final: true}))
	if req := l.Request(); req.HoldsListedSeq == nil || *req.HoldsListedSeq != 1 {
		t.Fatalf("listed at %v, and the first listing was at 1", req.HoldsListedSeq)
	}
}

// TestAHoldsRowKeepsItsFirstHeartbeatsBasis, and a commit puts the holds
// in order of their authorizations; a heartbeat after its hold's terminal
// keeps no hold.
func TestAHoldsRowKeepsItsFirstHeartbeatsBasis(t *testing.T) {
	l := load(t, "open")
	auths := []string{"e", "c", "a", "d", "b"}
	for i, auth := range auths {
		apply(t, l, Applied, hb(int64(i+1), auth, 1, 1))
	}
	apply(t, l, Applied, hb(6, "c", 2, 3), settle(7, "f", 1, 0), hb(8, "f", 1, 1))
	req := l.Request()
	var put []string
	for _, h := range req.PutHolds {
		put = append(put, h.AuthorizationID)
		if !bytes.Equal(h.ReapBasis, []byte("terms")) {
			t.Fatalf("hold %s's basis %q", h.AuthorizationID, h.ReapBasis)
		}
	}
	if !slices.Equal(put, []string{"a", "b", "c", "d", "e"}) || req.PutHolds[2].RunningCharge.Int64 != 3 {
		t.Fatalf("the holds put: %+v", req.PutHolds)
	}
}

// TestAnAdoptedTerminalWinsByItsRow: a terminal that adopts a drain-log
// row is stored under the row's record ID, which is how its copy in the
// drain log is known.
func TestAnAdoptedTerminalWinsByItsRow(t *testing.T) {
	l := load(t, "open")
	r := settle(1, "a", 40, 0)
	r.Drain = "d-7"
	apply(t, l, Applied, r)
	if req := l.Request(); len(req.Winners) != 1 || req.Winners[0].RecordID != "d-7" {
		t.Fatalf("the adopted winner: %+v", req.Winners)
	}
}

// TestAClosedLeaseIsNotLoaded, and a lease with no fence has not drained.
func TestAClosedLeaseIsNotLoaded(t *testing.T) {
	if _, err := Load(ref, loaded("closed"), 2*time.Second); err == nil {
		t.Fatal("a closed lease loaded")
	}
	l := load(t, "open")
	if err := l.Drained(loaded("open").Lease); err == nil {
		t.Fatal("an open lease drained")
	}
}

// TestARefusedCommitRereads: the member re-reads after its commit is
// refused, and keeps what it applied after one taken.
func TestARefusedCommitRereads(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, settle(1, "a", 40, 0))
	if err := l.Committed(store.CommitResult{Ref: ref, Refused: store.RefusedVersion}); !errors.Is(err, ErrReread) {
		t.Fatalf("a refused commit: %v", err)
	}
	if !l.Dirty() {
		t.Fatal("a refused commit dropped what the member applied")
	}
}

// TestACommitTellsTheMemberTheLeaseDrained: a commit that finds the lease
// draining leaves the member needing its winners for owner records, and its
// fence for ticks.
func TestACommitTellsTheMemberTheLeaseDrained(t *testing.T) {
	l := load(t, "open")
	apply(t, l, Applied, settle(1, "a", 40, 0))
	if err := l.Committed(store.CommitResult{Ref: ref, NewVersion: 4, State: "draining"}); err != nil {
		t.Fatal(err)
	}
	if got, _ := l.Apply(settle(2, "b", 7, 0), start); got != Behind {
		t.Fatalf("an owner record after a commit found the lease draining: %v", got)
	}
	if got, _ := l.Apply(tick(1, fence.Add(time.Hour)), start); got != Behind {
		t.Fatalf("a tick before the fence is read: %v", got)
	}
	if err := l.LoadWinners(loaded("draining").Lease, nil); err != nil {
		t.Fatal(err)
	}
	apply(t, l, Applied, settle(2, "b", 7, 0))
}
