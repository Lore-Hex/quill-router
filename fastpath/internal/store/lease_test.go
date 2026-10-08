package store

import (
	"context"
	"math"
	"slices"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

var owner = Owner{Node: "node-a", Epoch: 1}

// grantLease grants a lease of amount from a workspace with the given
// credit, and returns its ref.
func grantLease(t *testing.T, s *Store, amount int64, credits ...int64) LeaseRef {
	t.Helper()
	req := grantOf(seedWorkspace(t, credits...), amount)
	got, err := s.Grant(context.Background(), req)
	if err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	return LeaseRef{req.Workspace, req.LeaseID}
}

func readLease(t *testing.T, s *Store, ref LeaseRef) Lease {
	t.Helper()
	l, _, err := s.ReadLease(context.Background(), ref)
	if err != nil {
		t.Fatal(err)
	}
	return l
}

// execLease runs a statement on a lease's row, for a test's setup.
func execLease(t *testing.T, ref LeaseRef, sql string) {
	t.Helper()
	_, err := shared.ReadWriteTransaction(context.Background(), func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{SQL: sql, Params: ref.params()})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

const (
	drainIt = `UPDATE tr_lease SET state = 'draining', drained_by = 'owner', fence_time = TIMESTAMP_ADD(expiry, INTERVAL 1 SECOND)
	            WHERE workspace_id = @w AND lease_id = @l`
	closeIt = `UPDATE tr_lease SET state = 'closed', closed_at = CURRENT_TIMESTAMP(), close_kind = 'auditor',
	                  drained_by = COALESCE(drained_by, 'owner'), fence_time = COALESCE(fence_time, TIMESTAMP_ADD(expiry, INTERVAL 1 SECOND))
	            WHERE workspace_id = @w AND lease_id = @l`
)

func renewOne(t *testing.T, s *Store, o Owner, ref LeaseRef) RenewResult {
	t.Helper()
	got, _, err := s.Renew(context.Background(), o, []LeaseRef{ref})
	if err != nil || len(got) != 1 {
		t.Fatalf("renew: %+v %v", got, err)
	}
	return got[0]
}

func TestRenewExtendsBySpannersTime(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 10, 100)
	before := readLease(t, s, ref)
	got, at, err := s.Renew(context.Background(), owner, []LeaseRef{ref})
	if err != nil || len(got) != 1 || !got[0].Renewed {
		t.Fatalf("renew: %+v %v", got, err)
	}
	window := testConfig().Window
	if got[0].Expiry.After(at.Add(window)) || got[0].Expiry.Before(at.Add(window-5*time.Second)) {
		t.Fatalf("expiry %v for a commit at %v", got[0].Expiry, at)
	}
	after := readLease(t, s, ref)
	if !after.Expiry.Equal(got[0].Expiry) {
		t.Fatalf("the row's expiry %v, the answer's %v", after.Expiry, got[0].Expiry)
	}
	after.Expiry = before.Expiry
	if after != before {
		t.Fatalf("a renewal changed more than the expiry:\n%+v\n%+v", before, after)
	}
}

// TestRenewRefusesWhatTheDesignRefuses answers LeaseLifecycle's mutants
// renew-a-revoked-lease, renew-a-draining-lease and
// replay-a-renewal-on-a-draining-lease: the guards are the statement's.
func TestRenewRefusesWhatTheDesignRefuses(t *testing.T) {
	s := spikeStore(t)
	cases := map[string]struct {
		setup string
		who   Owner
	}{
		"revoked":         {`UPDATE tr_lease SET revoked = TRUE WHERE workspace_id = @w AND lease_id = @l`, owner},
		"draining":        {drainIt, owner},
		"closed":          {closeIt, owner},
		"another epoch":   {"", Owner{Node: "node-a", Epoch: 2}},
		"another process": {"", Owner{Node: "node-b", Epoch: 1}},
	}
	for name, c := range cases {
		ref := grantLease(t, s, 10, 100)
		if c.setup != "" {
			execLease(t, ref, c.setup)
		}
		before := readLease(t, s, ref)
		if got := renewOne(t, s, c.who, ref); got.Renewed {
			t.Errorf("%s: renewed to %v", name, got.Expiry)
		}
		if after := readLease(t, s, ref); after != before {
			t.Errorf("%s: the refused renewal changed the row", name)
		}
	}
}

func TestRenewGivesEachLeaseOfABatchItsOwnResult(t *testing.T) {
	s := spikeStore(t)
	mine, revoked, other := grantLease(t, s, 10, 100), grantLease(t, s, 10, 100), grantLease(t, s, 10, 100)
	execLease(t, revoked, `UPDATE tr_lease SET revoked = TRUE WHERE workspace_id = @w AND lease_id = @l`)
	execLease(t, other, `UPDATE tr_lease SET owner_node = 'node-b' WHERE workspace_id = @w AND lease_id = @l`)
	got, _, err := s.Renew(context.Background(), owner, []LeaseRef{mine, revoked, other})
	if err != nil {
		t.Fatal(err)
	}
	renewed := []bool{got[0].Renewed, got[1].Renewed, got[2].Renewed}
	if !slices.Equal(renewed, []bool{true, false, false}) || got[0].Ref != mine || got[1].Ref != revoked {
		t.Fatalf("the batch's results: %+v", got)
	}
}

func TestARenewalNeverMovesTheExpiryBack(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 10, 100)
	execLease(t, ref, `UPDATE tr_lease SET expiry = TIMESTAMP_ADD(expiry, INTERVAL 1 HOUR) WHERE workspace_id = @w AND lease_id = @l`)
	later := readLease(t, s, ref).Expiry
	got := renewOne(t, s, owner, ref)
	if !got.Renewed || !got.Expiry.Equal(later) || !readLease(t, s, ref).Expiry.Equal(later) {
		t.Fatalf("a renewal below the stored expiry %v: %+v", later, got)
	}
}

func TestRevokeStopsRenewals(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 10, 100)
	if ok, _, err := s.Revoke(ctx, ref); err != nil || !ok {
		t.Fatalf("revoke: %v %v", ok, err)
	}
	if ok, _, err := s.Revoke(ctx, ref); err != nil || ok {
		t.Fatalf("a second revoke: %v %v", ok, err)
	}
	if got := renewOne(t, s, owner, ref); got.Renewed {
		t.Fatal("a revoked lease renewed")
	}
	closed := grantLease(t, s, 10, 100)
	execLease(t, closed, closeIt)
	if ok, _, err := s.Revoke(ctx, closed); err != nil || ok {
		t.Fatalf("revoking a closed lease: %v %v", ok, err)
	}
}

// TestRenewalsRaceARevocation: whatever order Spanner serializes them in,
// no renewal commits after the revocation, and the expiry is the last
// renewal's.
func TestRenewalsRaceARevocation(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 10, 100)
	type renewal struct {
		at     time.Time
		expiry time.Time
	}
	var mu sync.Mutex
	var renewals []renewal
	var wg sync.WaitGroup
	for range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for range 10 {
				got, at, err := s.Renew(ctx, owner, []LeaseRef{ref})
				if err != nil {
					t.Error(err)
					return
				}
				if got[0].Renewed {
					mu.Lock()
					renewals = append(renewals, renewal{at, got[0].Expiry})
					mu.Unlock()
				}
			}
		}()
	}
	time.Sleep(20 * time.Millisecond)
	ok, revokedAt, err := s.Revoke(ctx, ref)
	wg.Wait()
	if err != nil || !ok {
		t.Fatalf("revoke: %v %v", ok, err)
	}
	var last time.Time
	for _, r := range renewals {
		if !r.at.Before(revokedAt) {
			t.Fatalf("a renewal committed at %v, the revocation at %v", r.at, revokedAt)
		}
		if r.expiry.After(last) {
			last = r.expiry
		}
	}
	if len(renewals) > 0 && !readLease(t, s, ref).Expiry.Equal(last) {
		t.Fatalf("the expiry is %v, the last renewal's %v", readLease(t, s, ref).Expiry, last)
	}
	if renewOne(t, s, owner, ref).Renewed {
		t.Fatal("a renewal after the revocation")
	}
}

// TestTheDrainingWritesStoreTheFence answers LeaseLifecycle's mutants
// the-owner-marks-a-lease-that-has-closed,
// the-auditor-marks-a-lease-that-has-closed and drain-on-a-stale-read.
func TestTheDrainingWritesStoreTheFence(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	cfg := testConfig()
	fence := cfg.Skew + cfg.PublishDeadline

	ref := grantLease(t, s, 10, 100)
	if ok, _, err := s.OwnerMarkDraining(ctx, Owner{Node: "node-a", Epoch: 2}, ref); err != nil || ok {
		t.Fatalf("another epoch's draining write: %v %v", ok, err)
	}
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("the owner's draining write: %v %v", ok, err)
	}
	l := readLease(t, s, ref)
	if l.State != "draining" || l.DrainedBy.StringVal != "owner" || !l.FenceTime.Time.Equal(l.Expiry.Add(fence)) {
		t.Fatalf("after the owner's draining write: %s %v %v, expiry %v", l.State, l.DrainedBy, l.FenceTime, l.Expiry)
	}
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || ok {
		t.Fatalf("a second draining write: %v %v", ok, err)
	}
	closedByOwner := grantLease(t, s, 10, 100)
	execLease(t, closedByOwner, closeIt)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, closedByOwner); err != nil || ok {
		t.Fatalf("the owner's draining write on a closed lease: %v %v", ok, err)
	}

	// The auditor's, from a scan with its clock past the expiry plus the skew.
	ref = grantLease(t, s, 10, 100)
	expiry := readLease(t, s, ref).Expiry
	if found := scanFor(t, s, ref, expiry.Add(cfg.Skew-time.Millisecond)); found != nil {
		t.Fatalf("the scan found a lease within its skew: %+v", found)
	}
	found := scanFor(t, s, ref, expiry.Add(cfg.Skew))
	if found == nil || !found.Expiry.Equal(expiry) {
		t.Fatalf("the scan past the skew: %+v", found)
	}
	if ok, _, err := s.AuditorMarkDraining(ctx, ref, expiry.Add(-time.Second)); err != nil || ok {
		t.Fatalf("an auditor's write on an expiry since renewed: %v %v", ok, err)
	}
	if ok, _, err := s.AuditorMarkDraining(ctx, ref, found.Expiry); err != nil || !ok {
		t.Fatalf("the auditor's write: %v %v", ok, err)
	}
	l = readLease(t, s, ref)
	if l.State != "draining" || l.DrainedBy.StringVal != "auditor" || !l.FenceTime.Time.Equal(expiry.Add(fence)) {
		t.Fatalf("after the auditor's write: %s %v %v", l.State, l.DrainedBy, l.FenceTime)
	}
	if found := scanFor(t, s, ref, expiry.Add(time.Hour)); found != nil {
		t.Fatal("the scan finds a draining lease")
	}
	closed := grantLease(t, s, 10, 100)
	execLease(t, closed, closeIt)
	if ok, _, err := s.AuditorMarkDraining(ctx, closed, readLease(t, s, closed).Expiry); err != nil || ok {
		t.Fatalf("an auditor's write on a closed lease: %v %v", ok, err)
	}
}

// scanFor runs ScanExpired at now and returns what it found of ref.
func scanFor(t *testing.T, s *Store, ref LeaseRef, now time.Time) *Expired {
	t.Helper()
	found, _, err := s.ScanExpired(context.Background(), now, 100000)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range found {
		if e.Ref == ref {
			return &e
		}
	}
	return nil
}

// TestTheShortfallWriteIsTheLargerOf answers CreditDebt's mutants
// a-stale-write-lowers-the-total and owner-raise-lands-after-close.
func TestTheShortfallWriteIsTheLargerOf(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	write := func(o Owner, total int64) ShortfallResult {
		t.Helper()
		got, err := s.ShortfallWrite(ctx, o, ref, total)
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := write(owner, 5); got.Rise != 5 || got.Refused != "" {
		t.Fatalf("the first write: %+v", got)
	}
	l := readLease(t, s, ref)
	if l.ShortfallTotal != 5 || l.Allocation != 35 || l.CommitVersion != 0 || reserved(readRows(t, ref.Workspace))[0] != 35 {
		t.Fatalf("after a write of 5: total %d, allocation %d, version %d", l.ShortfallTotal, l.Allocation, l.CommitVersion)
	}
	if got := write(owner, 5); got.Rise != 0 {
		t.Fatalf("a repeat: %+v", got)
	}
	if got := write(owner, 3); got.Rise != 0 || readLease(t, s, ref).ShortfallTotal != 5 {
		t.Fatalf("a lower total: %+v", got)
	}
	// A return the auditor applies between two writes of one total is not
	// undone: the write moves the total, never the allocation as it read it.
	execLease(t, ref, `UPDATE tr_lease SET allocation = allocation - 4, returned = returned + 4
	                    WHERE workspace_id = @w AND lease_id = @l`)
	if got := write(owner, 5); got.Rise != 0 || readLease(t, s, ref).Allocation != 31 {
		t.Fatalf("a repeat after a return: %+v, allocation %d", got, readLease(t, s, ref).Allocation)
	}
	execLease(t, ref, `UPDATE tr_lease SET allocation = allocation + 4, returned = returned - 4
	                    WHERE workspace_id = @w AND lease_id = @l`)
	identityHolds(t, s, ref.Workspace)
	if got := write(Owner{Node: "node-a", Epoch: 2}, 9); got.Refused != RefusedOwner {
		t.Fatalf("another epoch's write: %+v", got)
	}
	execLease(t, ref, drainIt)
	if got := write(owner, 8); got.Rise != 3 {
		t.Fatalf("a draining lease's write: %+v", got)
	}
	execLease(t, ref, closeIt)
	if got := write(owner, 9); got.Refused != RefusedClosed {
		t.Fatalf("a closed lease's write: %+v", got)
	}
}

func TestAShortfallWriteCoversOrMarks(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	// The lease's donor is shard 0, which it fills; shard 1 has room.
	ref := grantLease(t, s, 50, 50, 40)
	if got, err := s.ShortfallWrite(ctx, owner, ref, 30); err != nil || got.Rise != 30 {
		t.Fatalf("the write: %+v %v", got, err)
	}
	if rows := readRows(t, ref.Workspace); !slices.Equal(headrooms(rows), []int64{0, 10}) || slices.Contains(marks(rows), true) {
		t.Fatalf("after a raise past shard 0: %v %v; want [0 10] unmarked", headrooms(rows), marks(rows))
	}
	identityHolds(t, s, ref.Workspace)
	if got, err := s.ShortfallWrite(ctx, owner, ref, 50); err != nil || got.Rise != 20 {
		t.Fatalf("the second write: %+v %v", got, err)
	}
	if rows := readRows(t, ref.Workspace); !slices.Equal(headrooms(rows), []int64{-20, 10}) || !slices.Equal(marks(rows), []bool{true, true}) {
		t.Fatalf("after a raise past the workspace: %v %v; want [-20 10], every row marked", headrooms(rows), marks(rows))
	}
}

// TestTheFenceIsKeptToTheMicrosecond: F is the stored expiry plus the skew
// plus the publish deadline exactly, a part of a millisecond included, for
// both draining writes (Codex, review round 1 of S4c: in milliseconds, a
// skew of 100 µs and a deadline of 400 µs stored F at the expiry itself).
func TestTheFenceIsKeptToTheMicrosecond(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t, func(c *Config) { c.Skew, c.PublishDeadline = 100*time.Microsecond, 400*time.Microsecond })
	ownerDrained := grantLease(t, s, 10, 100)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ownerDrained); err != nil || !ok {
		t.Fatalf("the owner's draining write: %v %v", ok, err)
	}
	auditorDrained := grantLease(t, s, 10, 100)
	if ok, _, err := s.AuditorMarkDraining(ctx, auditorDrained, readLease(t, s, auditorDrained).Expiry); err != nil || !ok {
		t.Fatalf("the auditor's draining write: %v %v", ok, err)
	}
	for _, ref := range []LeaseRef{ownerDrained, auditorDrained} {
		if l := readLease(t, s, ref); l.FenceTime.Time.Sub(l.Expiry) != 500*time.Microsecond {
			t.Errorf("F - expiry = %v, want 500µs", l.FenceTime.Time.Sub(l.Expiry))
		}
	}
	cfg := testConfig()
	cfg.Skew = time.Millisecond + 1
	if _, err := New(shared, cfg); err == nil {
		t.Fatal("a skew finer than a microsecond is taken")
	}
	// Two settings whose sum overflows a duration (Codex, round 2): F
	// would have come out before the expiry.
	cfg = testConfig()
	cfg.Skew, cfg.PublishDeadline = time.Duration(math.MaxInt64/2+1), time.Duration(math.MaxInt64/2+1)
	if _, err := New(shared, cfg); err == nil {
		t.Fatal("a skew and a deadline that overflow together are taken")
	}
	// The largest settings §4.5's ratios allow under a week's grace.
	cfg = testConfig()
	cfg.Grace, cfg.Skew, cfg.PublishDeadline = MaxSetting, MaxSetting/8, MaxSetting/2
	if _, err := New(shared, cfg); err != nil {
		t.Fatalf("settings near a week are refused: %v", err)
	}
}
