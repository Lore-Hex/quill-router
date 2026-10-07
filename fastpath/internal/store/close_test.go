package store

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

func TestAnAuthorizationNamesItsLease(t *testing.T) {
	lease := NewLeaseID()
	a, err := NewAuthorizationID(lease)
	if err != nil || len(lease) != 22 || len(a) > 64 || !strings.HasPrefix(a, "gwa-") {
		t.Fatalf("lease %q, authorization %q, %v", lease, a, err)
	}
	if got, err := LeaseOfAuthorization(a); err != nil || got != lease {
		t.Fatalf("the lease of %q: %q %v", a, got, err)
	}
	for _, bad := range []string{"", "gwa-", "gwa-" + lease, "xyz-" + a[4:], a + "x", "gwa-" + strings.Repeat("!", 44)} {
		if _, err := LeaseOfAuthorization(bad); err == nil {
			t.Errorf("%q is read as an authorization", bad)
		}
	}
	if _, err := NewAuthorizationID("not-a-lease"); err == nil {
		t.Error("an authorization is minted under no lease")
	}
}

// drained grants a lease, drains it, and stores S with a commit; it returns
// the lease and the version the commit left.
func drained(t *testing.T, s *Store, credits ...int64) (LeaseRef, int64) {
	t.Helper()
	ctx := context.Background()
	ref := grantLease(t, s, 30, credits...)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 2, Boundary: &Boundary{S: 2, T: time.Now().UTC()},
		PutHolds: []HoldRow{hold("a1", 10)}})
	if got.Refused != "" {
		t.Fatalf("storing S: %+v", got)
	}
	return ref, got.NewVersion
}

func reapOf(a string, charge int64) ReapRow {
	return ReapRow{AuthorizationID: a, RecordID: "reap-" + a, Charge: charge, Estimate: 10, SnapshotOwnerSeq: 2,
		Digest: []byte("d"), Money: []byte(`{"cost":0}`)}
}

// TestAReapIsGuardedAsEveryWriteIs answers AuditorCommit's mutants
// reap-without-the-version and a-reap-before-s-is-stored.
func TestAReapIsGuardedAsEveryWriteIs(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	open := grantLease(t, s, 30, 100)
	if r, _, err := s.Reap(ctx, open, 0, reapOf("a1", 3)); err != nil || r != RefusedNotDraining {
		t.Fatalf("a reap of an open lease: %q %v", r, err)
	}
	noS := grantLease(t, s, 30, 100)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, noS); err != nil || !ok {
		t.Fatal(err)
	}
	if r, _, err := s.Reap(ctx, noS, 0, reapOf("a1", 3)); err != nil || r != RefusedNoBoundary {
		t.Fatalf("a reap before S is stored: %q %v", r, err)
	}
	ref, version := drained(t, s, 100)
	if r, _, err := s.Reap(ctx, ref, version-1, reapOf("a1", 3)); err != nil || r != RefusedVersion {
		t.Fatalf("a reap from a stale member: %q %v", r, err)
	}
	if _, _, err := s.Reap(ctx, ref, version, reapOf("a1", 11)); err == nil {
		t.Fatal("a reap above the hold's estimate is taken")
	}
	r, at, err := s.Reap(ctx, ref, version, reapOf("a1", 3))
	if err != nil || r != "" {
		t.Fatalf("the reap: %q %v", r, err)
	}
	rows, _, err := s.ReadHoldDrainRows(ctx, ref, "a1")
	if err != nil || len(rows) != 1 || rows[0].Kind != "reap" || rows[0].Charge != 3 || !rows[0].CommitTS.Equal(at) ||
		rows[0].SnapshotOwnerSeq.Int64 != 2 {
		t.Fatalf("the reap's row: %+v %v", rows, err)
	}
	if r, _, err := s.Reap(ctx, ref, version, reapOf("a1", 3)); err != nil || r != RefusedTerminal {
		t.Fatalf("a second reap: %q %v", r, err)
	}
	if ok, _, err := s.StopForGap(ctx, ref, version, 9); err != nil || !ok {
		t.Fatal(err)
	}
	if r, _, err := s.Reap(ctx, ref, version, reapOf("a2", 1)); err != nil || r != RefusedGap {
		t.Fatalf("a reap of a stopped lease: %q %v", r, err)
	}
}

func TestAReapLosesToAFrontDoorsTerminal(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	appendOK(t, s, terminal(ref, "a1", "r1", 4, 10))
	if r, _, err := s.Reap(ctx, ref, version, reapOf("a1", 3)); err != nil || r != RefusedTerminal {
		t.Fatalf("a reap after a settle: %q %v", r, err)
	}
}

// TestCloseRefusesWhatTheDesignRefuses answers the mutants
// close-without-reading-drain, close-before-the-boundary,
// a-partial-hand-off-counts and close-while-a-listed-hold-is-open.
func TestCloseRefusesWhatTheDesignRefuses(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	future := time.Now().Add(24 * time.Hour)
	ref, version := drained(t, s, 100)
	close := func(v int64, read, now time.Time) Refusal {
		t.Helper()
		got, err := s.CloseLease(ctx, ref, v, read, now)
		if err != nil {
			t.Fatal(err)
		}
		return got.Refused
	}
	if r := close(version, future, future); r != RefusedHoldsOpen {
		t.Fatalf("a close with a hold open: %q", r)
	}
	commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, Money: []MoneyOp{Book(4, 0)},
		Winners: []Winner{{AuthorizationID: "a1", Kind: "settle", Charge: 4, RecordID: "o3"}}, DropHolds: []string{"a1"}})
	version++
	if r := close(version-1, future, future); r != RefusedVersion {
		t.Fatalf("a close from a stale member: %q", r)
	}
	if r := close(version, future, time.Now()); r != RefusedTooSoon {
		t.Fatalf("a close before the holds can have ended: %q", r)
	}
	_, read, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	appendOK(t, s, terminal(ref, "a2", "r2", 1, 10))
	if r := close(version, read, future); r != RefusedRowsBeyond {
		t.Fatalf("a close with a row past its read: %q", r)
	}
	open := grantLease(t, s, 30, 100)
	if got, err := s.CloseLease(ctx, open, 0, future, future); err != nil || got.Refused != RefusedNotDraining {
		t.Fatalf("a close of an open lease: %+v %v", got, err)
	}
}

// TestCloseReleasesWhatTheLeaseHolds answers the mutant close-keeps-the-remainder.
func TestCloseReleasesWhatTheLeaseHolds(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	listed := int64(2)
	got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed,
		Money: []MoneyOp{Book(4, 0)}, Winners: []Winner{{AuthorizationID: "a1", Kind: "settle", Charge: 4, RecordID: "o3"}},
		DropHolds: []string{"a1"}})
	_, read, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	closed, err := s.CloseLease(ctx, ref, got.NewVersion, read, time.Now())
	if err != nil || closed.Refused != "" || closed.Released != 26 || closed.NewVersion != got.NewVersion+1 {
		t.Fatalf("the close: %+v %v", closed, err)
	}
	l := readLease(t, s, ref)
	if l.State != "closed" || l.CloseKind.StringVal != "auditor" || l.Allocation != 4 || l.Returned != 26 {
		t.Fatalf("the closed lease: %+v", l)
	}
	if r := reserved(readRows(t, ref.Workspace)); !slices.Equal(r, []int64{0}) {
		t.Fatalf("reserved after the close: %v", r)
	}
	identityHolds(t, s, ref.Workspace)
	if a, err := s.Append(ctx, terminal(ref, "a5", "r5", 1, 10)); err != nil || a.Refused != RefusedClosed {
		t.Fatalf("an append after the close: %+v %v", a, err)
	}
	if w, err := s.ShortfallWrite(ctx, owner, ref, 99); err != nil || w.Refused != RefusedClosed {
		t.Fatalf("a shortfall write after the close: %+v %v", w, err)
	}
}

func TestAPackIsDeletableOnceItsLeaseClosedAndItsWorkIsDone(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	retention := func(ref LeaseRef) (bool, []bool) {
		t.Helper()
		var retire spanner.NullTime
		row, err := shared.Single().ReadRow(ctx, "tr_lease", ref.key(), []string{"retire_at"})
		if err == nil {
			err = row.Column(0, &retire)
		}
		if err != nil {
			t.Fatal(err)
		}
		var deletable []bool
		err = shared.Single().Query(ctx, spanner.Statement{
			SQL:    `SELECT deletable_at IS NOT NULL FROM tr_lease_winners WHERE workspace_id = @w AND lease_id = @l ORDER BY commit_version`,
			Params: ref.params(),
		}).Do(func(r *spanner.Row) error {
			var d bool
			deletable = append(deletable, false)
			if err := r.Column(0, &d); err != nil {
				return err
			}
			deletable[len(deletable)-1] = d
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		return retire.Valid, deletable
	}
	listed := int64(2)
	for _, doneFirst := range []bool{true, false} {
		ref, version := drained(t, s, 100)
		got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed,
			Winners: []Winner{{AuthorizationID: "a1", Kind: "refund", RecordID: "o3"}}, DropHolds: []string{"a1"}})
		if doneFirst {
			for _, v := range []int64{1, 2} {
				if ok, err := s.MarkPackDone(ctx, ref, v); err != nil || !ok {
					t.Fatalf("mark %d: %v %v", v, ok, err)
				}
			}
			if retire, deletable := retention(ref); retire || slices.Contains(deletable, true) {
				t.Fatal("an open lease's packs are deletable")
			}
		}
		_, read, _ := s.ReadDrainSince(ctx, ref, time.Time{})
		if c, err := s.CloseLease(ctx, ref, got.NewVersion, read, time.Now()); err != nil || c.Refused != "" {
			t.Fatalf("close: %+v %v", c, err)
		}
		if !doneFirst {
			if retire, deletable := retention(ref); retire || slices.Contains(deletable, true) {
				t.Fatalf("packs with work pending: retire %v, deletable %v", retire, deletable)
			}
			for _, v := range []int64{1, 2} {
				if ok, err := s.MarkPackDone(ctx, ref, v); err != nil || !ok {
					t.Fatalf("mark %d: %v %v", v, ok, err)
				}
			}
		}
		if retire, deletable := retention(ref); !retire || !slices.Equal(deletable, []bool{true, true}) {
			t.Fatalf("done first %v: retire %v, deletable %v", doneFirst, retire, deletable)
		}
		if ok, err := s.MarkPackDone(ctx, ref, 1); err != nil || ok {
			t.Fatalf("a pack marked twice: %v %v", ok, err)
		}
	}
}
