package store

import (
	"context"
	"slices"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

// ceiledHold is snapped's hold with a stream's ceiling
// (docs/design/fast-admission-streams.md section 12).
func ceiledHold(a string, estimate, ceiling, charge, seq int64) HoldRow {
	h := snapped(a, estimate, charge, seq)
	h.Ceiling = spanner.NullInt64{Int64: ceiling, Valid: true}
	return h
}

// reapAt is reapOf with a ceiling.
func reapAt(a string, charge, ceiling int64) ReapRow {
	r := reapOf(a, charge)
	r.Ceiling = ceiling
	return r
}

// TestAReapAboveItsHoldRaisesTheLease answers CreditDebt's mutant
// reap-without-its-raise (section 12.2 item 3): a reap of a stream whose
// running charge passed its hold, within its ceiling, raises the lease's
// allocation and door_raised, its first donor's allocation and that shard's
// reservation by what the charge passes the hold, in the reap's own
// transaction, whatever the shard's headroom, and its row keeps the raise;
// a reap at or below the hold raises nothing. A running charge past the
// ceiling is reaped at the ceiling. The lease then books the reaps and
// closes, every row's identity holding throughout.
func TestAReapAboveItsHoldRaisesTheLease(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	// A lease of 50 on two shards: 30 from shard 0, the first donor, which
	// has no headroom left, and 20 from shard 1; drained, with S stored and
	// five holds of 10 open.
	ref := grantLease(t, s, 50, 30, 100)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 2, Boundary: &Boundary{S: 2, T: time.Now().UTC()},
		PutHolds: []HoldRow{snapped("a1", 10, 3, 2), ceiledHold("c1", 10, 25, 18, 2), ceiledHold("c2", 10, 25, 7, 2),
			ceiledHold("c3", 10, 25, 10, 2), ceiledHold("c4", 10, 25, 40, 2)}})
	if got.Refused != "" {
		t.Fatalf("storing S and the holds: %+v", got)
	}
	loaded, err := s.Load(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range loaded.Holds {
		if want := h.AuthorizationID != "a1"; h.Ceiling.Valid != want || (want && h.Ceiling.Int64 != 25) {
			t.Fatalf("hold %s loads with the ceiling %v", h.AuthorizationID, h.Ceiling)
		}
	}
	version := got.NewVersion

	// Not the hold's: another ceiling, none, or a charge that is not the
	// running charge capped at the ceiling.
	for name, r := range map[string]ReapRow{
		"no ceiling":               reapOf("c1", 10),
		"another ceiling":          reapAt("c1", 18, 30),
		"the ceiling, not the run": reapAt("c1", 25, 25),
		"a ceiling on a1":          reapAt("a1", 3, 25),
		"a ceiling at the hold":    reapAt("c1", 10, 10),
		"a charge past it":         reapAt("c1", 26, 25),
	} {
		if r, _, err := s.Reap(ctx, ref, version, due, r); err == nil {
			t.Fatalf("a reap at %s is taken: %q", name, r)
		}
	}
	if l := readLease(t, s, ref); l.Allocation != 50 || l.DoorRaised != 0 {
		t.Fatalf("refused reaps raised the lease: %+v", l)
	}

	raised := int64(0)
	for _, c := range []struct {
		r     ReapRow
		raise int64
	}{
		{reapOf("a1", 3), 0},
		{reapAt("c2", 7, 25), 0},
		{reapAt("c3", 10, 25), 0},
		{reapAt("c1", 18, 25), 8},
		{reapAt("c4", 25, 25), 15},
	} {
		if r, _, err := s.Reap(ctx, ref, version, due, c.r); err != nil || r != "" {
			t.Fatalf("the reap of %s at %d: %q %v", c.r.AuthorizationID, c.r.Charge, r, err)
		}
		raised += c.raise
		rows, _, err := s.ReadHoldDrainRows(ctx, ref, c.r.AuthorizationID)
		if err != nil || len(rows) != 1 || rows[0].Kind != "reap" || rows[0].Charge != c.r.Charge ||
			rows[0].DoorRaise != c.raise {
			t.Fatalf("%s's row: %+v %v, want a raise of %d", c.r.AuthorizationID, rows, err, c.raise)
		}
		l := readLease(t, s, ref)
		if l.Allocation != 50+raised || l.DoorRaised != raised {
			t.Fatalf("after %s's reap the lease's allocation is %d, raised %d; want %d, %d",
				c.r.AuthorizationID, l.Allocation, l.DoorRaised, 50+raised, raised)
		}
		if d := donorsOf(t, ref); len(d) != 2 || d[0].Allocation != 30+raised || d[1].Allocation != 20 {
			t.Fatalf("after %s's reap the donors are %+v", c.r.AuthorizationID, d)
		}
		if r := reserved(readRows(t, ref.Workspace)); !slices.Equal(r, []int64{30 + raised, 20}) {
			t.Fatalf("after %s's reap the shards reserve %v", c.r.AuthorizationID, r)
		}
		identityHolds(t, s, ref.Workspace)
	}

	// The commit that applies the rows books them, and the close returns
	// what is left.
	rows, read, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil || len(rows) != 5 {
		t.Fatalf("the drain log: %+v %v", rows, err)
	}
	listed := int64(2)
	req := CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed}
	booked := int64(0)
	for _, row := range rows {
		req.Money = append(req.Money, Book(row.Charge, 0))
		req.Winners = append(req.Winners, Winner{AuthorizationID: row.AuthorizationID, Kind: "reap", Charge: row.Charge,
			FromDrain: true, RecordID: row.RecordID})
		booked += row.Charge
	}
	won := commitOne(t, s, req)
	if l := readLease(t, s, ref); won.Refused != "" || l.Consumed != booked || l.FaultUsage != 0 {
		t.Fatalf("booking the reaps: %+v, the lease %+v", won, l)
	}
	identityHolds(t, s, ref.Workspace)
	closed, err := s.CloseLease(ctx, ref, won.NewVersion, read, time.Now())
	if err != nil || closed.Refused != "" || closed.Released != 50+raised-booked {
		t.Fatalf("the close: %+v %v, want %d released", closed, err, 50+raised-booked)
	}
	identityHolds(t, s, ref.Workspace)
}
