package owner

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
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
}

type recordAnswer struct {
	id  string
	err error
}

func (a recordAnswer) Wait(context.Context) (string, error) { return a.id, a.err }

func (r *fakeRecords) Publish(authorization, kind string, data []byte) Waiter {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.failing > 0 {
		r.failing--
		return recordAnswer{err: fmt.Errorf("the record topic failed")}
	}
	r.auths = append(r.auths, authorization+"/"+kind)
	r.data = append(r.data, slices.Clone(data))
	return recordAnswer{id: fmt.Sprintf("m%d", len(r.auths))}
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
	renew(t, f)
	if got, _ := rec.published(); len(got) != 0 || len(terminals(t, f)) != 0 {
		t.Fatalf("a reap before its hold was due: %v", got)
	}
	f.clock.advance(time.Microsecond)
	renew(t, f)
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
	renew(t, f)
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
	renew(t, f)
	if ts := terminals(t, f); len(ts) != 0 {
		t.Fatalf("a reap whose full record failed: %+v", ts)
	}
	renew(t, f)
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
	renew(t, f)
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
	f.clock.advance(59 * time.Second) // the lease's expiry is a minute away, its cutoff two seconds before
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
