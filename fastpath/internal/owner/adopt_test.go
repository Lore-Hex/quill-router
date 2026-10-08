package owner

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fakeRecords is the record topic: it keeps what is published to it, and
// fails that many publishes.
type fakeRecords struct {
	mu      sync.Mutex
	failing int
	auths   []string
	data    [][]byte
	// hold, when set, holds each acknowledgement until it closes.
	hold chan struct{}
}

type recordAnswer struct {
	id   string
	err  error
	hold chan struct{}
}

func (a recordAnswer) Wait(ctx context.Context) (string, error) {
	if a.hold != nil {
		select {
		case <-a.hold:
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	return a.id, a.err
}

func (r *fakeRecords) Publish(authorization, kind string, data []byte) Waiter {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.failing > 0 {
		r.failing--
		return recordAnswer{err: fmt.Errorf("the record topic failed")}
	}
	r.auths = append(r.auths, authorization+"/"+kind)
	r.data = append(r.data, slices.Clone(data))
	return recordAnswer{id: fmt.Sprintf("m%d", len(r.auths)), hold: r.hold}
}

func (r *fakeRecords) published() ([]string, [][]byte) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return slices.Clone(r.auths), slices.Clone(r.data)
}

// adoptFixture is an owner with a store and the record topic, and a grace
// of a minute.
func adoptFixture(t *testing.T) (*fixture, *fakeSpanner, *fakeRecords) {
	t.Helper()
	sp := newFakeSpanner()
	sp.expiry = start.Add(10 * time.Minute)
	rec := &fakeRecords{}
	f := newFixture(t, 1000, nil, func(c *Config) {
		c.Spanner, c.Node, c.RenewEvery, c.Window, c.KeyStatus = sp, "owner-1", time.Hour, 30*time.Second, 7
		c.Grace, c.Records = time.Minute, rec
	})
	return f, sp, rec
}

// terminals are the lease's terminal records, in order: a renewal round's
// checkpoints aside.
func terminals(t *testing.T, f *fixture) []record.Record {
	t.Helper()
	var out []record.Record
	for _, r := range f.log.records(t, "lease-1") {
		if r.Kind.Terminal() {
			out = append(out, r)
		}
	}
	return out
}

func lastTerminal(t *testing.T, f *fixture) record.Record {
	t.Helper()
	ts := terminals(t, f)
	if len(ts) == 0 {
		return record.Record{}
	}
	return ts[len(ts)-1]
}

func renew(t *testing.T, f *fixture) {
	t.Helper()
	if err := f.owner.Renew(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// renewAndReap is a renewal round, then a pass of the reaper.
func renewAndReap(t *testing.T, f *fixture) {
	t.Helper()
	renew(t, f)
	if err := f.owner.Reap(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// TestARenewalAdoptsTheFirstRow: a front door's terminal for an undecided
// hold is published as the owner's own record, carrying the row's record ID,
// its raise counted as allocation, so the overrun it covered needs no
// shortfall; a later row of the hold, a later read, and a later settle
// change nothing.
func TestARenewalAdoptsTheFirstRow(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 10, false)
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d1", Kind: "settle", Charge: 15, Estimate: 10,
		DoorRaise: 5, Digest: sum("d1")}, start.Add(time.Second))
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d2", Kind: "refund", Estimate: 10,
		Digest: sum("d2")}, start.Add(2*time.Second))
	before := f.lease.Books()
	renew(t, f)
	r := lastTerminal(t, f)
	if r.Kind != record.Settle || r.Auth != a || r.Drain != "d1" || r.Charge != 15 || string(r.Digest) != string(sum("d1")) ||
		r.Shortfall != 0 {
		t.Fatalf("the adopted record: %+v", r)
	}
	if b := f.lease.Books(); b.Allocation != before.Allocation+5 || b.Consumed != 15 || b.Held != before.Held-10 ||
		b.Shortfall != 0 {
		t.Fatalf("the books after the adoption: %+v, before %+v", b, before)
	}
	n := len(terminals(t, f))
	renew(t, f)
	if got := len(terminals(t, f)); got != n {
		t.Fatalf("%d records after another renewal, %d before", got, n)
	}
	sp.mu.Lock()
	cursors := slices.Clone(sp.cursors)
	sp.mu.Unlock()
	if len(cursors) != 2 || !cursors[0].IsZero() || !cursors[1].Equal(start.Add(2*time.Second)) {
		t.Fatalf("the drain log's reads were from %v; the second from the first's read", cursors)
	}
	if out, err := f.lease.Settle(ctx, a, 12, sum("late")); err != nil || out.Kind != record.Settle || out.Charge != 15 {
		t.Fatalf("a settle after the adoption: %+v %v", out, err)
	}
}

// TestARowOfADecidedHoldIsNotAdopted: the owner's terminal came first in
// the lease's order; the row changes nothing, its raise included.
func TestARowOfADecidedHoldIsNotAdopted(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 10, false)
	if _, err := f.lease.Settle(ctx, a, 4, sum("a")); err != nil {
		t.Fatal(err)
	}
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d1", Kind: "settle", Charge: 15, Estimate: 10,
		DoorRaise: 5, Digest: sum("d1")}, start.Add(time.Second))
	n, before := len(terminals(t, f)), f.lease.Books()
	renew(t, f)
	if got, b := len(terminals(t, f)), f.lease.Books(); got != n || b.Allocation != before.Allocation {
		t.Fatalf("a row of a decided hold: %d records, %d before; books %+v, before %+v", got, n, b, before)
	}
}

// TestAnAdoptedRefundCarriesItsBoot: a refund's record names the hold's boot
// binding, and no digest.
func TestAnAdoptedRefundCarriesItsBoot(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	a := f.admit(t, 10, false)
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d1", Kind: "refund", Estimate: 10,
		Digest: sum("d1")}, start.Add(time.Second))
	renew(t, f)
	if r := lastTerminal(t, f); r.Kind != record.Refund || r.Drain != "d1" || string(r.Boot) != string(boot) || len(r.Digest) != 0 {
		t.Fatalf("the adopted refund: %+v", r)
	}
}

// TestTheReaperReapsAtTheLastSnapshot: a stream's hold whose last
// heartbeat's deadline plus the grace has passed is reaped at that
// heartbeat's snapshot and running charge, with the basis its first brought,
// its full record on the record topic first and named by its digest; not a
// microsecond sooner.
func TestTheReaperReapsAtTheLastSnapshot(t *testing.T) {
	f, _, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h0"), Usage: 4, Running: 8,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	f.clock.advance(10 * time.Second)
	granted, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h1"), Usage: 10, Running: 20,
		Echoed: first})
	if err != nil {
		t.Fatal(err)
	}
	if !granted.After(first) {
		t.Fatalf("the second heartbeat's deadline %v, the first's %v", granted, first)
	}
	hb := f.log.records(t, "lease-1")[1]
	f.clock.advance(granted.Add(time.Minute - time.Microsecond).Sub(start.Add(10 * time.Second)))
	renewAndReap(t, f)
	if got, _ := rec.published(); len(got) != 0 || len(terminals(t, f)) != 0 {
		t.Fatalf("a reap before its hold was due: %v", got)
	}
	f.clock.advance(time.Microsecond)
	renewAndReap(t, f)
	got, data := rec.published()
	if !slices.Equal(got, []string{a + "/record"}) {
		t.Fatalf("the record topic: %v", got)
	}
	digest := sha256.Sum256(data[0])
	if r := lastTerminal(t, f); r.Kind != record.Reap || r.Auth != a || r.Charge != 20 || r.SnapshotSeq != hb.Seq ||
		string(r.Digest) != string(digest[:]) {
		t.Fatalf("the reap: %+v", r)
	}
	var full dueReap
	if err := json.Unmarshal(data[0], &full); err != nil {
		t.Fatal(err)
	}
	want := dueReap{Lease: "lease-1", Auth: a, Estimate: 100, Charge: 20, Deadline: granted.UTC(), GatewaySeq: 2,
		Hash: sum("h1"), Usage: 10, OwnerSeq: hb.Seq, Basis: []byte("terms"), Boot: boot}
	if !reflect.DeepEqual(full, want) {
		t.Fatalf("the reap's full record %+v, want %+v", full, want)
	}
	if b := f.lease.Books(); b.Consumed != 20 || b.Open != 0 {
		t.Fatalf("the books after the reap: %+v", b)
	}
}

// TestTheReaperAdoptsTheHoldsRowFirst: a due hold with a terminal in the
// drain log, which the renewal's read passed by, has it adopted, not
// reaped.
func TestTheReaperAdoptsTheHoldsRowFirst(t *testing.T) {
	f, sp, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d1", Kind: "settle", Charge: 30, Estimate: 100,
		Digest: sum("d1")}, start.Add(time.Second))
	f.lease.adopted = start.Add(time.Hour) // the renewal's read is past the row
	f.clock.advance(5 * time.Minute)
	renewAndReap(t, f)
	if got, _ := rec.published(); len(got) != 0 {
		t.Fatalf("a full record for a hold the drain log decided: %v", got)
	}
	if r := lastTerminal(t, f); r.Kind != record.Settle || r.Drain != "d1" || r.Charge != 30 {
		t.Fatalf("the hold's record: %+v", r)
	}
}

// TestAReapWaitsForItsFullRecord: a reap whose full record the record topic
// failed is not decided; the next round reaps.
func TestAReapWaitsForItsFullRecord(t *testing.T) {
	f, _, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	rec.failing = 1
	f.clock.advance(5 * time.Minute)
	renewAndReap(t, f)
	if ts := terminals(t, f); len(ts) != 0 {
		t.Fatalf("a reap whose full record failed: %+v", ts)
	}
	renewAndReap(t, f)
	if r := lastTerminal(t, f); r.Kind != record.Reap || r.Auth != a {
		t.Fatalf("the reap at the next round: %+v", r)
	}
}

// TestAHoldWithNoHeartbeatIsNotReaped: it has no snapshot to reap at; a
// stream that never heartbeated, and a request that never streams, wait.
func TestAHoldWithNoHeartbeatIsNotReaped(t *testing.T) {
	f, _, rec := adoptFixture(t)
	f.admit(t, 10, true)
	f.admit(t, 10, false)
	f.clock.advance(5 * time.Minute)
	renewAndReap(t, f)
	if got, _ := rec.published(); len(got) != 0 || len(terminals(t, f)) != 0 {
		t.Fatalf("a reap of a hold with no heartbeat: %v", got)
	}
}

// TestAReapAtAMovedSnapshotWaits: a heartbeat after the reaper read the hold
// moves its snapshot; the reap at the old one is not decided.
func TestAReapAtAMovedSnapshotWaits(t *testing.T) {
	f, _, _ := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	due := f.lease.due(start.Add(5*time.Minute), time.Minute)
	if len(due) != 1 {
		t.Fatalf("the due holds: %+v", due)
	}
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21,
		Echoed: first}); err != nil {
		t.Fatal(err)
	}
	n := len(terminals(t, f))
	if err := f.lease.reap(due[0], sum("full")); err != nil || len(terminals(t, f)) != n {
		t.Fatalf("a reap at a moved snapshot: %v", err)
	}
}

// TestARenewalPastTheCutoffAdoptsBeforeDecidingAgain: a renewal that finds
// its lease past the cutoff leaves it admitting and deciding nothing until
// its drain log is adopted, which a failed read puts off to the next round
// (§4.2); a lease renewed within its cutoff admits on whatever the read.
func TestARenewalPastTheCutoffAdoptsBeforeDecidingAgain(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 10, false)
	s := f.admit(t, 10, true)
	r := f.admit(t, 10, true)
	f.clock.advance(55 * time.Second) // the lease's expiry is a minute away, its cutoff two seconds before
	first := HeartbeatOf{GatewaySeq: 1, Hash: sum("r1"), Usage: 1, Running: 1, Basis: []byte("terms")}
	granted, err := f.lease.Heartbeat(ctx, r, first)
	if err != nil {
		t.Fatal(err)
	}
	f.clock.advance(4 * time.Second)
	if _, err := f.lease.Settle(ctx, a, 15, sum("late")); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle past the cutoff: %v", err)
	}
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d1", Kind: "settle", Charge: 15, Estimate: 10,
		DoorRaise: 5, Digest: sum("late")}, start.Add(59*time.Second))
	sp.failReads = 1
	renew(t, f)
	before, n := f.lease.Books(), len(terminals(t, f))
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("an admission before the adoption: %v", err)
	}
	if _, err := f.lease.Refund(ctx, a); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a refund before the adoption: %v", err)
	}
	if _, err := f.lease.Heartbeat(ctx, s, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat before the adoption: %v", err)
	}
	if _, err := f.lease.Heartbeat(ctx, r, first); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat's replay before the adoption: %v", err)
	}
	if b := f.lease.Books(); b != before || len(terminals(t, f)) != n {
		t.Fatalf("the books before the adoption: %+v, then %+v", before, b)
	}
	renew(t, f)
	if r := lastTerminal(t, f); r.Kind != record.Settle || r.Auth != a || r.Drain != "d1" || r.Charge != 15 {
		t.Fatalf("the adopted settle: %+v", r)
	}
	if out, err := f.lease.Refund(ctx, a); err != nil || out.Kind != record.Settle || out.Charge != 15 {
		t.Fatalf("a refund after the adoption: %+v %v", out, err)
	}
	if got, err := f.lease.Heartbeat(ctx, r, first); err != nil || !got.Equal(granted) {
		t.Fatalf("a heartbeat's replay after the adoption: %v %v, granted %v", got, err, granted)
	}
	f.admit(t, 1, false)

	f, sp, _ = adoptFixture(t)
	sp.failReads = 1
	renew(t, f)
	f.admit(t, 1, false)
}

// TestRenewalRoundsRunOneAtATime: a round waits for the one under way, so
// each reads the drain log from the last one's read.
func TestRenewalRoundsRunOneAtATime(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: "gone", RecordID: "d1", Kind: "refund"}, start.Add(time.Second))
	sp.reading, sp.readGate = make(chan struct{}, 2), make(chan struct{})
	done := make(chan error, 2)
	go func() { done <- f.owner.Renew(context.Background()) }()
	<-sp.reading
	go func() { done <- f.owner.Renew(context.Background()) }()
	time.Sleep(100 * time.Millisecond)
	sp.mu.Lock()
	rounds := len(sp.rounds)
	sp.mu.Unlock()
	if rounds != 1 {
		t.Fatalf("%d rounds renewed while the first read the drain log", rounds)
	}
	close(sp.readGate)
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	sp.mu.Lock()
	cursors := slices.Clone(sp.cursors)
	sp.mu.Unlock()
	if len(cursors) != 2 || !cursors[0].IsZero() || !cursors[1].Equal(start.Add(time.Second)) {
		t.Fatalf("the reads' cursors: %v", cursors)
	}
}

// TestStoppingEndsARenewalRound: Stop ends a round's read and returns once
// the round has, though the read takes a while to end; Run ends with the
// owner.
func TestStoppingEndsARenewalRound(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	sp.reading, sp.readsWait, sp.afterCancel = make(chan struct{}, 1), true, 200*time.Millisecond
	renewed := make(chan error, 1)
	go func() { renewed <- f.owner.Renew(context.Background()) }()
	<-sp.reading
	ran := make(chan struct{})
	go func() {
		f.owner.Run(context.Background())
		close(ran)
	}()
	stopped := make(chan struct{})
	go func() {
		f.owner.Stop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(10 * time.Second):
		t.Fatal("Stop did not return")
	}
	select {
	case <-renewed:
	default:
		t.Fatal("Stop returned while a round ran")
	}
	select {
	case <-ran:
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not end with the owner")
	}
	if err := f.owner.Renew(context.Background()); err == nil {
		t.Fatal("a round after Stop")
	}
}

// TestAReaperPassHoldsUpNoRenewal: a reap waiting on the record topic holds
// up no renewal round, which runs apart; once acknowledged, it is decided.
func TestAReaperPassHoldsUpNoRenewal(t *testing.T) {
	f, sp, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(5 * time.Minute)
	renew(t, f) // renewed past its cutoff: the round adopts, and the lease decides again
	rec.mu.Lock()
	rec.hold = make(chan struct{})
	rec.mu.Unlock()
	reaped := make(chan error, 1)
	go func() { reaped <- f.owner.Reap(ctx) }()
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		if got, _ := rec.published(); len(got) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the reaper published no full record")
		}
	}
	began := time.Now()
	renew(t, f)
	renew(t, f)
	if took := time.Since(began); took > 500*time.Millisecond {
		t.Fatalf("two renewal rounds took %v beside a reap held on the record topic", took)
	}
	sp.mu.Lock()
	rounds := len(sp.rounds)
	sp.mu.Unlock()
	if rounds != 3 {
		t.Fatalf("%d renewal rounds", rounds)
	}
	close(rec.hold)
	if err := <-reaped; err != nil {
		t.Fatal(err)
	}
	if r := lastTerminal(t, f); r.Kind != record.Reap || r.Auth != a {
		t.Fatalf("the reap once acknowledged: %+v", r)
	}
}

// TestARaiseTheAllocationCannotHoldIsNotAdopted: a drain-log row whose
// raise would take the lease's allocation past what an int64 holds is not
// adopted: the hold stays open, the allocation and the cursor as they were.
func TestARaiseTheAllocationCannotHoldIsNotAdopted(t *testing.T) {
	f, sp, _ := adoptFixture(t)
	ctx := context.Background()
	a, b, c := f.admit(t, 1, false), f.admit(t, 998, false), f.admit(t, 1, false)
	if _, err := f.lease.Settle(ctx, a, math.MaxInt64-999, sum("a")); err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Refund(ctx, b); err != nil {
		t.Fatal(err)
	}
	before := f.lease.Books()
	if before.Allocation != math.MaxInt64 {
		t.Fatalf("the allocation after the settle: %+v", before)
	}
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: c, RecordID: "d1", Kind: "settle", Charge: 2, Estimate: 1,
		DoorRaise: 1, Digest: sum("d1")}, start.Add(time.Second))
	n := len(terminals(t, f))
	renew(t, f)
	renew(t, f)
	got := f.lease.Books()
	got.NextSeq = before.NextSeq // the rounds' checkpoints
	if got != before || len(terminals(t, f)) != n {
		t.Fatalf("a raise past an int64: books %+v, before %+v; %d records, %d before", got, before,
			len(terminals(t, f)), n)
	}
	sp.mu.Lock()
	cursors := slices.Clone(sp.cursors)
	sp.mu.Unlock()
	if len(cursors) != 2 || !cursors[1].IsZero() {
		t.Fatalf("the reads' cursors: %v", cursors)
	}
}

// TestTheReaperSkipsALeaseThatDecidesNothing: a lease past its cutoff, or
// one a renewal found past its cutoff whose drain log is not yet adopted,
// is not reaped: its due hold's full record is not even published.
func TestTheReaperSkipsALeaseThatDecidesNothing(t *testing.T) {
	ctx := context.Background()
	dueHold := func(f *fixture) {
		t.Helper()
		a := f.admit(t, 100, true)
		if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
			Basis: []byte("terms")}); err != nil {
			t.Fatal(err)
		}
	}
	f, _, rec := adoptFixture(t)
	dueHold(f)
	f.clock.advance(5 * time.Minute) // past the deadline and grace, and the lease's cutoff
	if err := f.owner.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	if got, _ := rec.published(); len(got) != 0 || len(terminals(t, f)) != 0 {
		t.Fatalf("a lease past its cutoff reaped: %v", got)
	}

	f, sp, rec := adoptFixture(t)
	dueHold(f)
	f.clock.advance(5 * time.Minute)
	sp.failReads = 1
	renew(t, f) // renewed past its cutoff; its drain log's read fails
	if err := f.owner.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	if got, _ := rec.published(); len(got) != 0 || len(terminals(t, f)) != 0 {
		t.Fatalf("a lease whose drain log is not adopted reaped: %v", got)
	}
	renewAndReap(t, f)
	if r := lastTerminal(t, f); r.Kind != record.Reap {
		t.Fatalf("the reap once the drain log is adopted: %+v", r)
	}
}

// TestReaperPassesRunOneAtATime: a pass waits for the one under way, so a
// hold due once is published once.
func TestReaperPassesRunOneAtATime(t *testing.T) {
	f, _, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(5 * time.Minute)
	renew(t, f)
	rec.mu.Lock()
	rec.hold = make(chan struct{})
	rec.mu.Unlock()
	done := make(chan error, 2)
	go func() { done <- f.owner.Reap(ctx) }()
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		if got, _ := rec.published(); len(got) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the first pass published nothing")
		}
	}
	go func() { done <- f.owner.Reap(ctx) }()
	time.Sleep(100 * time.Millisecond)
	if got, _ := rec.published(); len(got) != 1 {
		t.Fatalf("two passes at once published %v", got)
	}
	close(rec.hold)
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	if got, _ := rec.published(); len(got) != 1 {
		t.Fatalf("a hold due once published %v", got)
	}
}

// TestStoppingEndsAReaperPass: Stop ends a pass waiting on the record topic
// at once, though its wait is long, and returns once the pass has.
func TestStoppingEndsAReaperPass(t *testing.T) {
	f, _, rec := adoptFixture(t)
	f.owner.cfg.AnswerWait = time.Hour
	ctx := context.Background()
	a := f.admit(t, 100, true)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(5 * time.Minute)
	renew(t, f)
	rec.mu.Lock()
	rec.hold = make(chan struct{})
	rec.mu.Unlock()
	defer close(rec.hold)
	reaped := make(chan error, 1)
	go func() { reaped <- f.owner.Reap(ctx) }()
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		if got, _ := rec.published(); len(got) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the pass published nothing")
		}
	}
	stopped := make(chan struct{})
	go func() {
		f.owner.Stop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("Stop waited on a pass held on the record topic")
	}
	select {
	case <-reaped:
	default:
		t.Fatal("Stop returned while a pass ran")
	}
}

// TestAReapUnderWayWhenAdoptionIsLeftDecidesNothing: a reap waiting on the
// record topic when a renewal finds its lease past the cutoff and fails to
// read its drain log decides nothing once acknowledged; the next round
// adopts the hold's row from the drain log.
func TestAReapUnderWayWhenAdoptionIsLeftDecidesNothing(t *testing.T) {
	f, sp, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")}); err != nil {
		t.Fatal(err)
	}
	renew(t, f) // the lease expires at start+10m
	f.clock.advance(2 * time.Minute)
	rec.mu.Lock()
	rec.hold = make(chan struct{})
	rec.mu.Unlock()
	reaped := make(chan error, 1)
	go func() { reaped <- f.owner.Reap(ctx) }()
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		if got, _ := rec.published(); len(got) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the reap published no full record")
		}
	}
	f.clock.advance(9 * time.Minute) // past the cutoff
	sp.appendRow("lease-1", store.DrainRow{AuthorizationID: a, RecordID: "d1", Kind: "settle", Charge: 90, Estimate: 100,
		Digest: sum("d1")}, start.Add(11*time.Minute))
	sp.mu.Lock()
	sp.expiry, sp.failReads = start.Add(30*time.Minute), 1
	sp.mu.Unlock()
	renew(t, f) // renewed past its cutoff; its drain log's read fails
	close(rec.hold)
	if err := <-reaped; err != nil {
		t.Fatal(err)
	}
	if ts := terminals(t, f); len(ts) != 0 {
		t.Fatalf("a reap decided with the drain log left to adopt: %+v", ts)
	}
	renew(t, f)
	if r := lastTerminal(t, f); r.Kind != record.Settle || r.Drain != "d1" || r.Charge != 90 {
		t.Fatalf("the hold's row adopted: %+v", r)
	}
}
