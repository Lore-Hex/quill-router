package owner

import (
	"context"
	"crypto/sha256"
	"encoding/json"
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
// heartbeat's running charge, its full record on the record topic first and
// named by its digest; not a microsecond sooner.
func TestTheReaperReapsAtTheLastSnapshot(t *testing.T) {
	f, _, rec := adoptFixture(t)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	granted, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	hb := f.log.records(t, "lease-1")[0]
	f.clock.advance(granted.Add(time.Minute - time.Microsecond).Sub(start))
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
	want := dueReap{Lease: "lease-1", Auth: a, Estimate: 100, Charge: 20, Deadline: granted.UTC(), GatewaySeq: 1,
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
