package auditor

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// ceiled is a heartbeat record of a stream whose hold of 100 has a ceiling
// of 300 (docs/design/fast-admission-streams.md section 12).
func ceiled(seq int64, auth string, gseq, running int64) record.Record {
	r := hb(seq, auth, gseq, running)
	r.Ceiling = 300
	return r
}

// TestAHoldsRowKeepsAStreamsCeiling (section 12.4, PR 2): a heartbeat record
// carrying a stream's ceiling stores it as the row's, the bound a reap of
// the hold charges up to; a later heartbeat stores the one its record states,
// none for a record without one. A hand-off's hold carries its ceiling to
// the row, and keeps it under a newer heartbeat that states none.
func TestAHoldsRowKeepsAStreamsCeiling(t *testing.T) {
	put := func(t *testing.T, l *Lease) map[string]store.HoldRow {
		t.Helper()
		out := map[string]store.HoldRow{}
		for _, h := range l.Request().PutHolds {
			out[h.AuthorizationID] = h
		}
		return out
	}

	l := load(t, "open")
	apply(t, l, Applied, hb(1, "p", 1, 5), ceiled(2, "c", 1, 250))
	rows := put(t, l)
	if got := rows["p"].Ceiling; got.Valid {
		t.Fatalf("a row with no ceiling has %v", got)
	}
	if got := rows["c"]; !got.Ceiling.Valid || got.Ceiling.Int64 != 300 || got.RunningCharge.Int64 != 250 {
		t.Fatalf("a row with a ceiling: %+v", got)
	}
	apply(t, l, Applied, hb(3, "c", 2, 60))
	if got := put(t, l)["c"]; got.Ceiling.Valid || got.RunningCharge.Int64 != 60 {
		t.Fatalf("a later heartbeat with no ceiling: %+v", got)
	}

	snap := &record.Snapshot{GatewaySeq: 2, Hash: sum("h"), Usage: []byte(`{"tokens":2}`), Running: 250,
		Deadline: deadline}
	held := []record.HeldHold{{Auth: "h", Estimate: 100, Ceiling: 300, Deadline: deadline, Boot: boot, Snapshot: snap,
		SnapshotSeq: 1, Basis: []byte("terms")}, {Auth: "n", Estimate: 50, Deadline: deadline, Boot: boot}}
	m := load(t, "open")
	if err := m.LoadWinners(loaded("open").Lease, nil); err != nil {
		t.Fatal(err)
	}
	// h's heartbeat after its chunk was cut is newer than the chunk's
	// snapshot, and states no ceiling.
	apply(t, m, Applied, hb(1, "h", 1, 1), chunkRec(2, held...), hb(3, "h", 3, 40),
		manifestRec(4, digestOf(t, held...), 2))
	rows = put(t, m)
	if got := rows["h"]; !got.Ceiling.Valid || got.Ceiling.Int64 != 300 || got.RunningCharge.Int64 != 40 {
		t.Fatalf("a handed-off stream's row: %+v", got)
	}
	if got := rows["n"].Ceiling; got.Valid {
		t.Fatalf("a handed-off hold with no ceiling has %v", got)
	}

	// The ceiling is in the commit, and a member that loads the row has it.
	again, err := Load(ref, store.Loaded{Lease: loaded("open").Lease, Holds: []store.HoldRow{rows["h"]}}, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if got := again.openHolds(); len(got) != 1 || got[0].Ceiling != rows["h"].Ceiling {
		t.Fatalf("the loaded rows: %+v", got)
	}
}

// TestADrainsReapChargesUpToTheCeilingAndRaisesTheLease (section 12.2 item
// 3; CreditDebt's mutant reap-without-its-raise): the drain reaps a stream
// whose last snapshot passed its hold, within its ceiling, at that running
// charge, its full record and money fields stating the ceiling, and its
// row raises the lease by what the charge passes the hold; a stream with a
// ceiling reaped within its hold raises nothing. The lease closes with both
// booked.
func TestADrainsReapChargesUpToTheCeilingAndRaisesTheLease(t *testing.T) {
	f := newRuntimeFixture(t)
	ctx := context.Background()
	var clock time.Time
	rt := f.runtime(func(c *Config) { c.Clock = func() time.Time { return clock } })
	handleAll(rt, on(t, f.ref, ceiled(1, "a", 1, 250)), on(t, f.ref, ceiled(2, "b", 1, 60)))
	round(rt)
	if l := f.loaded(); len(l.Holds) != 2 || l.Holds[0].Ceiling.Int64 != 300 || l.Holds[1].Ceiling.Int64 != 300 {
		t.Fatalf("the stored holds: %+v", l.Holds)
	}
	if ok, _, err := f.s.OwnerMarkDraining(ctx, grantee, f.ref); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	row, _, err := f.s.ReadLease(ctx, f.ref)
	if err != nil {
		t.Fatal(err)
	}
	n := int64(0)
	tickAt := func(at time.Time) {
		n++
		handleAll(rt, on(t, f.ref, tick(n, at)))
	}
	tickAt(row.FenceTime.Time.Add(2 * time.Second)) // the fence tick: S
	round(rt)
	late := row.Expiry
	if deadline.After(late) {
		late = deadline
	}
	late = late.Add(time.Hour)
	clock = late
	tickAt(late) // a and b reaped
	for auth, want := range map[string][2]int64{"a": {250, 150}, "b": {60, 0}} {
		rows, _, err := f.s.ReadHoldDrainRows(ctx, f.ref, auth)
		if err != nil || len(rows) != 1 || rows[0].Kind != "reap" || rows[0].Charge != want[0] ||
			rows[0].DoorRaise != want[1] || rows[0].Estimate != 100 {
			t.Fatalf("%s's reap row: %+v %v, want a charge of %d raising %d", auth, rows, err, want[0], want[1])
		}
		var money reapMoney
		if err := json.Unmarshal(rows[0].Money, &money); err != nil || money != (reapMoney{Charge: want[0],
			Estimate: 100, Ceiling: 300}) {
			t.Fatalf("%s's reap money: %+v %v", auth, money, err)
		}
	}
	f.records.mu.Lock()
	bodies := append([][]byte(nil), f.records.data...)
	f.records.mu.Unlock()
	for _, body := range bodies {
		var full reapRecord
		if err := json.Unmarshal(body, &full); err != nil || full.Estimate != 100 || full.Ceiling != 300 {
			t.Fatalf("a reap's full record: %+v %v", full, err)
		}
	}
	if l, _, err := f.s.ReadLease(ctx, f.ref); err != nil || l.Allocation != 1150 || l.DoorRaised != 150 {
		t.Fatalf("the lease after the reaps: %+v %v", l, err)
	}
	round(rt)
	tickAt(late.Add(time.Second)) // the reaps' rows applied
	round(rt)
	tickAt(late.Add(3 * time.Second)) // the end seen
	tickAt(late.Add(5 * time.Second)) // the close
	if l := f.loaded(); l.Lease.State != "closed" || l.Lease.Consumed != 310 || l.Lease.DoorRaised != 150 {
		t.Fatalf("the closed lease: %+v", l.Lease)
	}
}
