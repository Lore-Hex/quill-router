package store

import (
	"context"
	"math"
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

// snapped is an open hold of estimate whose latest snapshot, from owner
// record seq, has the running charge given.
func snapped(a string, estimate, charge, seq int64) HoldRow {
	h := hold(a, estimate)
	h.SnapshotSeq, h.RunningCharge = spanner.NullInt64{Int64: 1, Valid: true}, spanner.NullInt64{Int64: charge, Valid: true}
	h.SnapshotOwnerSeq = spanner.NullInt64{Int64: seq, Valid: true}
	return h
}

// drained grants a lease, drains it, and stores S with a commit, with a1's
// hold open at a snapshot of 3 of 10 from owner record 2; it returns the
// lease and the version the commit left.
func drained(t *testing.T, s *Store, credits ...int64) (LeaseRef, int64) {
	t.Helper()
	ctx := context.Background()
	ref := grantLease(t, s, 30, credits...)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 2, Boundary: &Boundary{S: 2, T: time.Now().UTC()},
		PutHolds: []HoldRow{snapped("a1", 10, 3, 2)}})
	if got.Refused != "" {
		t.Fatalf("storing S: %+v", got)
	}
	return ref, got.NewVersion
}

// due is a tick past the deadline of every hold the tests store, plus the
// grace.
var due = time.Date(2026, 10, 9, 0, 0, 0, 0, time.UTC)

// reapOf is a reap of a at a charge, of a hold of 10 from owner record 2,
// as drained's a1 is at a charge of 3.
func reapOf(a string, charge int64) ReapRow {
	return ReapRow{AuthorizationID: a, RecordID: "reap-" + a, Charge: charge, Estimate: 10, SnapshotOwnerSeq: 2,
		Digest: []byte("d"), Money: []byte(`{"cost":0}`)}
}

// TestAReapNeedsItsOpenHold: a reap is of a stored open hold, at the hold's
// snapshot. An authorization whose winner is stored, its hold gone with it,
// or one the lease never held has none to reap; a reap at another charge,
// estimate or snapshot is not the hold's.
func TestAReapNeedsItsOpenHold(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, Money: []MoneyOp{Book(4, 0)},
		Winners:  []Winner{{AuthorizationID: "a1", Kind: "settle", Charge: 4, RecordID: "o2"}},
		PutHolds: []HoldRow{hold("a2", 10), snapped("a3", 10, 15, 2)}})
	for _, a := range []string{"a1", "a9"} {
		if r, _, err := s.Reap(ctx, ref, got.NewVersion, due, reapOf(a, 3)); err != nil || r != RefusedNoHold {
			t.Fatalf("a reap of %s, which has no open hold: %q %v", a, r, err)
		}
	}
	noDigest := reapOf("a3", 10)
	noDigest.Digest = nil
	for name, r := range map[string]ReapRow{
		"another charge": reapOf("a3", 9),
		"another estimate": {AuthorizationID: "a3", RecordID: "x", Charge: 10, Estimate: 11, SnapshotOwnerSeq: 2,
			Digest: []byte("d"), Money: []byte("{}")},
		"another snapshot": {AuthorizationID: "a3", RecordID: "x", Charge: 10, Estimate: 10, SnapshotOwnerSeq: 1,
			Digest: []byte("d"), Money: []byte("{}")},
		"a charge on no snapshot":      reapOf("a2", 1),
		"no digest of its full record": noDigest,
	} {
		if _, _, err := s.Reap(ctx, ref, got.NewVersion, due, r); err == nil {
			t.Fatalf("a reap at %s is taken", name)
		}
	}
	// A snapshot past the estimate is capped at it; a hold with none charges nothing.
	if r, _, err := s.Reap(ctx, ref, got.NewVersion, due, reapOf("a3", 10)); err != nil || r != "" {
		t.Fatalf("a reap at the estimate: %q %v", r, err)
	}
	if r, _, err := s.Reap(ctx, ref, got.NewVersion, due, ReapRow{AuthorizationID: "a2", RecordID: "reap-a2", Estimate: 10,
		Digest: []byte("d"), Money: []byte("{}")}); err != nil || r != "" {
		t.Fatalf("a reap of a hold with no snapshot: %q %v", r, err)
	}
}

// TestAReapWaitsForItsDeadlinePlusGrace: a reap is taken only at a tick past
// its hold's deadline plus the grace, so a settle the hold may still have
// cannot lose to it.
func TestAReapWaitsForItsDeadlinePlusGrace(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	at := hold("a1", 10).Deadline.Add(testConfig().Grace)
	if r, _, err := s.Reap(ctx, ref, version, at.Add(-time.Microsecond), reapOf("a1", 3)); err != nil || r != RefusedNotDue {
		t.Fatalf("a reap before the deadline plus the grace: %q %v", r, err)
	}
	if r, _, err := s.Reap(ctx, ref, version, at, reapOf("a1", 3)); err != nil || r != "" {
		t.Fatalf("a reap at the deadline plus the grace: %q %v", r, err)
	}
}

// TestMaxLifeAndGraceAreBounded: a close adds MaxLife and Grace to the
// expiry, and a reap Grace to a deadline, so New bounds both as it does the
// other durations, and no sum of them overflows.
func TestMaxLifeAndGraceAreBounded(t *testing.T) {
	spikeStore(t)
	for name, change := range map[string]func(*Config){
		"MaxLife past a week": func(c *Config) { c.MaxLife = MaxSetting + time.Microsecond },
		"Grace past a week":   func(c *Config) { c.Grace = MaxSetting + time.Microsecond },
		"a sum that overflows": func(c *Config) {
			c.MaxLife, c.Grace = time.Duration(math.MaxInt64/2+1), time.Duration(math.MaxInt64/2+1)
		},
	} {
		cfg := testConfig()
		change(&cfg)
		if _, err := New(shared, cfg); err == nil {
			t.Errorf("%s is taken", name)
		}
	}
	cfg := testConfig()
	cfg.MaxLife, cfg.Grace = MaxSetting, MaxSetting
	if _, err := New(shared, cfg); err != nil {
		t.Fatalf("a week each is refused: %v", err)
	}
}

// TestTheTimingsKeepTheirRatios: the publish deadline is less than the
// grace less twice the skew (§4.5), so the grace is more than twice the
// skew, and an auditor's clock that runs the skew fast reaps no hold before
// its deadline.
func TestTheTimingsKeepTheirRatios(t *testing.T) {
	spikeStore(t)
	for name, change := range map[string]func(*Config){
		"a grace less than the skew": func(c *Config) { c.Skew, c.Grace, c.PublishDeadline = 2*time.Second, time.Second, time.Microsecond },
		"a deadline at the grace less twice the skew": func(c *Config) {
			c.Skew, c.Grace, c.PublishDeadline = 2*time.Second, time.Minute, time.Minute-4*time.Second
		},
	} {
		cfg := testConfig()
		change(&cfg)
		if _, err := New(shared, cfg); err == nil {
			t.Errorf("%s is taken", name)
		}
	}
	cfg := testConfig()
	cfg.Skew, cfg.Grace, cfg.PublishDeadline = 2*time.Second, time.Minute, time.Minute-4*time.Second-time.Microsecond
	if _, err := New(shared, cfg); err != nil {
		t.Fatalf("a deadline just under the grace less twice the skew is refused: %v", err)
	}
}

// TestNoCloseWithAnUndecidedDrainRow: a front door's terminal the member
// has read, but whose winner it has not committed, holds the close back,
// though no row is past the member's read; once its winner is stored the
// lease closes.
func TestNoCloseWithAnUndecidedDrainRow(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	listed := int64(2)
	got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed,
		Winners: []Winner{{AuthorizationID: "a1", Kind: "refund", RecordID: "o2"}}})
	appendOK(t, s, terminal(ref, "a2", "r2", 4, 4))
	_, read, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if c, err := s.CloseLease(ctx, ref, got.NewVersion, read, time.Now()); err != nil || c.Refused != RefusedUndecided {
		t.Fatalf("a close with a read row's winner not stored: %+v %v", c, err)
	}
	won := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: got.NewVersion, AppliedSeq: 2, Money: []MoneyOp{Book(4, 0)},
		Winners: []Winner{{AuthorizationID: "a2", Kind: "settle", Charge: 4, FromDrain: true, RecordID: "r2"}}})
	if c, err := s.CloseLease(ctx, ref, won.NewVersion, read, time.Now()); err != nil || c.Refused != "" || c.Released != 26 {
		t.Fatalf("the close once the row's winner is stored: %+v %v", c, err)
	}
}

// TestNoCommitAfterTheClose: the close advances the version, and a commit at
// the new version is refused and writes nothing, so nothing is booked on a
// lease whose remainder went back, or left pending in a pack of a lease
// whose row may be going.
func TestNoCommitAfterTheClose(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	listed := int64(2)
	got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed,
		Winners: []Winner{{AuthorizationID: "a1", Kind: "refund", RecordID: "o2"}}})
	_, read, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	c, err := s.CloseLease(ctx, ref, got.NewVersion, read, time.Now())
	if err != nil || c.Refused != "" || c.Released != 30 {
		t.Fatalf("the close: %+v %v", c, err)
	}
	before := readLease(t, s, ref)
	packs, _, err := s.LoadWinners(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	late := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: c.NewVersion, AppliedSeq: 2, Money: []MoneyOp{Book(4, 10)}})
	after, _, err := s.LoadWinners(ctx, ref)
	if err != nil || late.Refused != RefusedClosed || readLease(t, s, ref) != before || len(after) != len(packs) {
		t.Fatalf("a commit after the close: %+v, %d packs then %d, %v", late, len(packs), len(after), err)
	}
	identityHolds(t, s, ref.Workspace)
}

// TestAReapIsGuardedAsEveryWriteIs answers AuditorCommit's mutants
// reap-without-the-version and a-reap-before-s-is-stored.
func TestAReapIsGuardedAsEveryWriteIs(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	open := grantLease(t, s, 30, 100)
	if r, _, err := s.Reap(ctx, open, 0, due, reapOf("a1", 3)); err != nil || r != RefusedNotDraining {
		t.Fatalf("a reap of an open lease: %q %v", r, err)
	}
	noS := grantLease(t, s, 30, 100)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, noS); err != nil || !ok {
		t.Fatal(err)
	}
	if r, _, err := s.Reap(ctx, noS, 0, due, reapOf("a1", 3)); err != nil || r != RefusedNoBoundary {
		t.Fatalf("a reap before S is stored: %q %v", r, err)
	}
	ref, version := drained(t, s, 100)
	if r, _, err := s.Reap(ctx, ref, version-1, due, reapOf("a1", 3)); err != nil || r != RefusedVersion {
		t.Fatalf("a reap from a stale member: %q %v", r, err)
	}
	if _, _, err := s.Reap(ctx, ref, version, due, reapOf("a1", 11)); err == nil {
		t.Fatal("a reap above the hold's estimate is taken")
	}
	r, at, err := s.Reap(ctx, ref, version, due, reapOf("a1", 3))
	if err != nil || r != "" {
		t.Fatalf("the reap: %q %v", r, err)
	}
	rows, _, err := s.ReadHoldDrainRows(ctx, ref, "a1")
	if err != nil || len(rows) != 1 || rows[0].Kind != "reap" || rows[0].Charge != 3 || !rows[0].CommitTS.Equal(at) ||
		rows[0].SnapshotOwnerSeq.Int64 != 2 {
		t.Fatalf("the reap's row: %+v %v", rows, err)
	}
	if r, _, err := s.Reap(ctx, ref, version, due, reapOf("a1", 3)); err != nil || r != RefusedTerminal {
		t.Fatalf("a second reap: %q %v", r, err)
	}
	// A gap is stored only before S, and stops the lease's later writes.
	if ok, _, err := s.StopForGap(ctx, noS, 0, 9); err != nil || !ok {
		t.Fatalf("the gap: %v %v", ok, err)
	}
	if r, _, err := s.Reap(ctx, noS, 0, due, reapOf("a2", 1)); err != nil || r != RefusedGap {
		t.Fatalf("a reap of a stopped lease: %q %v", r, err)
	}
}

func TestAReapLosesToAFrontDoorsTerminal(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref, version := drained(t, s, 100)
	appendOK(t, s, terminal(ref, "a1", "r1", 4, 10))
	if r, _, err := s.Reap(ctx, ref, version, due, reapOf("a1", 3)); err != nil || r != RefusedTerminal {
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
		Winners: []Winner{{AuthorizationID: "a1", Kind: "settle", Charge: 4, RecordID: "o3"}}})
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
	})
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

// TestTheLeaseGoesOnceClosedWithNoWorkPending: the lease's row may go only
// once it is closed and no pack's work is pending, in either order, and its
// packs only with it, so a pack whose work is done stays while another's is
// pending.
func TestTheLeaseGoesOnceClosedWithNoWorkPending(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	retiring := func(ref LeaseRef) bool {
		t.Helper()
		var retire spanner.NullTime
		row, err := shared.Single().ReadRow(ctx, "tr_lease", ref.key(), []string{"retire_at"})
		if err == nil {
			err = row.Column(0, &retire)
		}
		if err != nil {
			t.Fatal(err)
		}
		return retire.Valid
	}
	mark := func(ref LeaseRef, v int64) {
		t.Helper()
		if ok, err := s.MarkPackDone(ctx, ref, v); err != nil || !ok {
			t.Fatalf("mark %d: %v %v", v, ok, err)
		}
	}
	listed := int64(2)
	for _, doneFirst := range []bool{true, false} {
		ref, version := drained(t, s, 100)
		got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: version, AppliedSeq: 2, HoldsListedSeq: &listed,
			Winners: []Winner{{AuthorizationID: "a1", Kind: "refund", RecordID: "o3"}}})
		if doneFirst {
			mark(ref, 1)
			mark(ref, 2)
			if retiring(ref) {
				t.Fatal("an open lease's row may go")
			}
		}
		_, read, _ := s.ReadDrainSince(ctx, ref, time.Time{})
		if c, err := s.CloseLease(ctx, ref, got.NewVersion, read, time.Now()); err != nil || c.Refused != "" {
			t.Fatalf("close: %+v %v", c, err)
		}
		if !doneFirst {
			mark(ref, 1)
			if retiring(ref) {
				t.Fatal("the row may go while a pack's work is pending")
			}
			mark(ref, 2)
		}
		if !retiring(ref) {
			t.Fatalf("done first %v: a closed lease with no work pending is kept", doneFirst)
		}
		if ok, err := s.MarkPackDone(ctx, ref, 1); err != nil || ok {
			t.Fatalf("a pack marked twice: %v %v", ok, err)
		}
	}
}

// indebted grants 20 from shards holding 10, 10 and 15, so the lease's
// donors are shards 0 and 1, then puts shard 2 at -15 with every row marked,
// drains the lease and stores S, with the holds listed.
func indebted(t *testing.T, s *Store) (LeaseRef, int64) {
	t.Helper()
	ctx := context.Background()
	ref := grantLease(t, s, 20, 10, 10, 15)
	setRow(t, ref.Workspace, 2, map[string]any{"total_usage": int64(30)})
	setRows(t, ref.Workspace, map[string]any{"in_debt": true})
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	listed := int64(2)
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 2, Boundary: &Boundary{S: 2, T: time.Now().UTC()},
		HoldsListedSeq: &listed})
	if got.Refused != "" {
		t.Fatalf("storing S: %+v", got)
	}
	if h := headrooms(readRows(t, ref.Workspace)); !slices.Equal(h, []int64{0, 0, -15}) {
		t.Fatalf("headroom before: %v", h)
	}
	identityHolds(t, s, ref.Workspace)
	return ref, got.NewVersion
}

// TestCloseUsesTheSameDonorOrderAsReturn: a close returns the remainder as a
// commit's return does, from the last donor first (§4.2), so a workspace in
// debt is repaid the same way by either.
func TestCloseUsesTheSameDonorOrderAsReturn(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	closed, version := indebted(t, s)
	_, read, err := s.ReadDrainSince(ctx, closed, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if c, err := s.CloseLease(ctx, closed, version, read, time.Now()); err != nil || c.Refused != "" || c.Released != 20 {
		t.Fatalf("the close: %+v %v", c, err)
	}
	returned, version := indebted(t, s)
	if got := commitOne(t, s, CommitRequest{Ref: returned, ReadVersion: version, AppliedSeq: 2,
		Money: []MoneyOp{Return(20)}}); got.Refused != "" {
		t.Fatalf("the return: %+v", got)
	}
	for _, ref := range []LeaseRef{closed, returned} {
		rows := readRows(t, ref.Workspace)
		if h := headrooms(rows); !slices.Equal(h, []int64{5, 0, 0}) || rows[0].marked {
			t.Fatalf("after %v: headroom %v, rows %+v", ref, h, rows)
		}
		identityHolds(t, s, ref.Workspace)
	}
}
