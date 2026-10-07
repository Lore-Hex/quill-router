package store

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

func commitOne(t *testing.T, s *Store, r CommitRequest) CommitResult {
	t.Helper()
	got, _, err := s.Commit(context.Background(), []CommitRequest{r})
	if err != nil || len(got) != 1 {
		t.Fatalf("commit: %+v %v", got, err)
	}
	return got[0]
}

func hold(a string, estimate int64) HoldRow {
	return HoldRow{AuthorizationID: a, Estimate: estimate, Deadline: time.Date(2026, 10, 8, 0, 0, 0, 0, time.UTC)}
}

func donorsOf(t *testing.T, ref LeaseRef) []donorMoney {
	t.Helper()
	var out []donorMoney
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT credit_shard, allocation, consumed FROM tr_lease_donor WHERE workspace_id = @w AND lease_id = @l
		       ORDER BY credit_shard`,
		Params: ref.params(),
	}).Do(func(row *spanner.Row) error {
		var d donorMoney
		if err := row.Columns(&d.Shard, &d.Allocation, &d.Consumed); err != nil {
			return err
		}
		out = append(out, d)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// TestACommitBooksAndAdvancesTheVersion answers AuditorCommit's mutants
// audit-sum-not-stored, refunds-not-stored and holds-not-loaded: the audited
// sum, a refund's zero-charge winner and the open holds are all stored.
func TestACommitBooksAndAdvancesTheVersion(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 70, 50, 40)
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 3, LastTick: 1, AuditOsum: 30,
		Money: []MoneyOp{Book(30, 0)}, PutHolds: []HoldRow{hold("a1", 10)},
		Winners: []Winner{{AuthorizationID: "a0", Kind: "settle", Charge: 30, RecordID: "o3"},
			{AuthorizationID: "ar", Kind: "refund", RecordID: "o2"}}})
	if got.Refused != "" || got.NewVersion != 1 || got.State != "open" || len(got.Faults) != 0 {
		t.Fatalf("the commit: %+v", got)
	}
	l := readLease(t, s, ref)
	if l.CommitVersion != 1 || l.AppliedSeq != 3 || l.LastTick != 1 || l.AuditOsum != 30 || l.Consumed != 30 || l.Allocation != 70 {
		t.Fatalf("the lease after: %+v", l)
	}
	if d := donorsOf(t, ref); !slices.Equal(d, []donorMoney{{0, 50, 30}, {1, 20, 0}}) {
		t.Fatalf("donors %v", d)
	}
	if rows := readRows(t, ref.Workspace); rows[0].usage != 30 || rows[0].reserved != 20 || rows[1].reserved != 20 {
		t.Fatalf("credit rows %+v", rows)
	}
	identityHolds(t, s, ref.Workspace)
	loaded, err := s.Load(context.Background(), ref)
	if err != nil || len(loaded.Holds) != 1 || loaded.Holds[0].AuthorizationID != "a1" || len(loaded.Packs) != 0 {
		t.Fatalf("an open lease loads holds and no winners: %+v %v", loaded, err)
	}
	packs, _, err := s.LoadWinners(context.Background(), ref)
	if err != nil || len(packs) != 1 || packs[0].CommitVersion != 1 || len(packs[0].Winners) != 2 ||
		packs[0].Winners[0].RecordID != "o3" || packs[0].Winners[1].Kind != "refund" {
		t.Fatalf("the winners: %+v %v", packs, err)
	}
}

// TestAStaleMemberIsRefused answers AuditorCommit's mutant
// commit-without-the-version: a replayed commit is refused, and writes nothing.
func TestAStaleMemberIsRefused(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(5, 0)}})
	before := readLease(t, s, ref)
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 2, Money: []MoneyOp{Book(5, 0)},
		AuditFault: ptr(int64(2))})
	if got.Refused != RefusedVersion || readLease(t, s, ref) != before {
		t.Fatalf("a stale commit: %+v", got)
	}
	identityHolds(t, s, ref.Workspace)
}

func ptr[T any](v T) *T { return &v }

func TestOneRefusedLeaseLeavesTheOthersToCommit(t *testing.T) {
	s := spikeStore(t)
	stale, fresh := grantLease(t, s, 30, 100), grantLease(t, s, 30, 100)
	commitOne(t, s, CommitRequest{Ref: stale, AppliedSeq: 1})
	got, _, err := s.Commit(context.Background(), []CommitRequest{
		{Ref: stale, AppliedSeq: 2, Money: []MoneyOp{Book(5, 0)}},
		{Ref: fresh, AppliedSeq: 1, Money: []MoneyOp{Book(5, 0)}},
	})
	if err != nil || got[0].Refused != RefusedVersion || got[1].Refused != "" || got[1].NewVersion != 1 {
		t.Fatalf("a batch with one stale lease: %+v %v", got, err)
	}
	if readLease(t, s, stale).Consumed != 0 || readLease(t, s, fresh).Consumed != 5 {
		t.Fatal("the batch booked the stale lease, or not the fresh one")
	}
}

// TestARaiseBetweenLoadAndCommitIsKept answers AuditorCommit's mutant
// commit-loses-a-raise: the commit books against the row as its transaction
// reads it, so an owner's raise after the member loaded the lease stays.
func TestARaiseBetweenLoadAndCommitIsKept(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	loaded, err := s.Load(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := s.ShortfallWrite(ctx, owner, ref, 5); err != nil || got.Rise != 5 {
		t.Fatalf("the owner's write: %+v %v", got, err)
	}
	got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: loaded.Lease.CommitVersion, AppliedSeq: 1,
		Money: []MoneyOp{Book(33, 8)}})
	l := readLease(t, s, ref)
	if got.Refused != "" || l.ShortfallTotal != 8 || l.Allocation != 38 || l.Consumed != 33 {
		t.Fatalf("the commit after a raise: %+v, lease %+v", got, l)
	}
	identityHolds(t, s, ref.Workspace)
}

func TestAFaultIsBookedAsUsageAndCovered(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 10, 10, 100)
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(15, 0)}})
	if !slices.Equal(got.Faults, []int64{5}) || readLease(t, s, ref).FaultUsage != 5 {
		t.Fatalf("a booking 5 past the allocation: %+v", got)
	}
	if rows := readRows(t, ref.Workspace); !slices.Equal(headrooms(rows), []int64{0, 95}) || slices.Contains(marks(rows), true) {
		t.Fatalf("after the fault: %v %v", headrooms(rows), marks(rows))
	}
	identityHolds(t, s, ref.Workspace)
}

func TestReturnsComeFromTheLastDonorFirst(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 70, 50, 40)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(45, 0), Return(22)}})
	l := readLease(t, s, ref)
	if d := donorsOf(t, ref); !slices.Equal(d, []donorMoney{{0, 48, 45}, {1, 0, 0}}) || l.Allocation != 48 || l.Returned != 22 {
		t.Fatalf("after booking 45 and returning 22: donors %v, lease %+v", d, l)
	}
	if r := reserved(readRows(t, ref.Workspace)); !slices.Equal(r, []int64{3, 0}) {
		t.Fatalf("reserved %v", r)
	}
	identityHolds(t, s, ref.Workspace)
}

// TestSIsStoredOnceWithItsProgress answers AuditorCommit's mutants
// boundary-moved and a-row-decided-before-s-is-stored.
func TestSIsStoredOnceWithItsProgress(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	at := time.Date(2026, 10, 7, 21, 0, 0, 0, time.UTC)
	if _, _, err := s.Commit(ctx, []CommitRequest{{Ref: ref, AppliedSeq: 4, Boundary: &Boundary{S: 4, T: at}}}); err == nil {
		t.Fatal("S is stored on an open lease")
	}
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	if _, _, err := s.Commit(ctx, []CommitRequest{{Ref: ref, AppliedSeq: 5, Boundary: &Boundary{S: 4, T: at}}}); err == nil {
		t.Fatal("S is stored with other progress than its own")
	}
	drained := Winner{AuthorizationID: "a9", Kind: "settle", Charge: 1, FromDrain: true, RecordID: "d1"}
	if _, _, err := s.Commit(ctx, []CommitRequest{{Ref: ref, AppliedSeq: 4, Winners: []Winner{drained}}}); err == nil {
		t.Fatal("a winner from the drain log is stored before S")
	}
	if got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 4, Boundary: &Boundary{S: 4, T: at},
		Winners: []Winner{drained}, Money: []MoneyOp{Book(1, 0)}}); got.Refused != "" || got.State != "draining" {
		t.Fatalf("storing S: %+v", got)
	}
	l := readLease(t, s, ref)
	if !l.BoundarySeq.Valid || l.BoundarySeq.Int64 != 4 || !l.BoundaryPublishTime.Time.Equal(at) {
		t.Fatalf("S and T: %v %v", l.BoundarySeq, l.BoundaryPublishTime)
	}
	if got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 1, AppliedSeq: 5, Boundary: &Boundary{S: 5, T: at}}); got.Refused != RefusedBoundary {
		t.Fatalf("another S: %+v", got)
	}
	if got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 1, AppliedSeq: 4, Money: []MoneyOp{Book(1, 0)}}); got.Refused != "" {
		t.Fatalf("a commit after S: %+v", got)
	}
	// Past S no owner record is applied, whether or not the request names S.
	late := Winner{AuthorizationID: "a8", Kind: "settle", Charge: 7, RecordID: "o5"}
	before := readLease(t, s, ref)
	if got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 2, AppliedSeq: 5, Winners: []Winner{late},
		Money: []MoneyOp{Book(7, 0)}}); got.Refused != RefusedBoundary || readLease(t, s, ref) != before {
		t.Fatalf("a record past S: %+v", got)
	}
}

// TestHoldsGoWithTheirWinners: the commit that stores a winner deletes its
// hold's row, so a member that takes the lease over loads no hold that is
// decided already.
func TestHoldsGoWithTheirWinners(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, PutHolds: []HoldRow{hold("a1", 10), hold("a2", 5)}})
	snap := hold("a2", 5)
	snap.SnapshotSeq, snap.RunningCharge = spanner.NullInt64{Int64: 3, Valid: true}, spanner.NullInt64{Int64: 0, Valid: true}
	commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 1, AppliedSeq: 2, Money: []MoneyOp{Book(7, 0)},
		Winners: []Winner{{AuthorizationID: "a1", Kind: "settle", Charge: 7, RecordID: "o2"}}, PutHolds: []HoldRow{snap}})
	loaded, err := s.Load(ctx, ref)
	if err != nil || len(loaded.Holds) != 1 || loaded.Holds[0].AuthorizationID != "a2" ||
		loaded.Holds[0].RunningCharge != (spanner.NullInt64{Int64: 0, Valid: true}) {
		t.Fatalf("the holds after a1's winner: %+v %v", loaded.Holds, err)
	}
	refund := Winner{AuthorizationID: "a2", Kind: "refund", RecordID: "o3"}
	if _, _, err := s.Commit(ctx, []CommitRequest{{Ref: ref, ReadVersion: 2, AppliedSeq: 3, PutHolds: []HoldRow{hold("a2", 5)},
		Winners: []Winner{refund}}}); err == nil {
		t.Fatal("a hold is stored beside its own winner")
	}
}

func TestAnAuditFaultRevokesWithItsCommit(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 7, AuditFault: ptr(int64(7))})
	l := readLease(t, s, ref)
	if got.Refused != "" || !l.Revoked || l.AuditFaultSeq.Int64 != 7 {
		t.Fatalf("an audit fault: %+v, revoked %v, fault %v", got, l.Revoked, l.AuditFaultSeq)
	}
}

// TestAGapStopsTheLease answers AuditorCommit's mutant
// a-gap-from-stale-progress.
func TestAGapStopsTheLease(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1})
	if ok, _, err := s.StopForGap(ctx, ref, 0, 3); err != nil || ok {
		t.Fatalf("a gap from a stale member: %v %v", ok, err)
	}
	if ok, _, err := s.StopForGap(ctx, ref, 1, 2); err != nil || ok {
		t.Fatalf("the next record taken for a gap: %v %v", ok, err)
	}
	if _, _, err := s.StopForGap(ctx, ref, 1, 0); err == nil {
		t.Fatal("a gap at no sequence number")
	}
	if ok, _, err := s.StopForGap(ctx, ref, 1, 3); err != nil || !ok {
		t.Fatalf("the gap: %v %v", ok, err)
	}
	if got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 1, AppliedSeq: 2}); got.Refused != RefusedGap {
		t.Fatalf("a commit after the gap: %+v", got)
	}
	if l := readLease(t, s, ref); l.CommitVersion != 1 || l.GapSeq.Int64 != 3 {
		t.Fatalf("the stopped lease: version %d, gap %v", l.CommitVersion, l.GapSeq)
	}
	if ok, _, err := s.StopForGap(ctx, ref, 1, 4); err != nil || ok {
		t.Fatalf("a second gap: %v %v", ok, err)
	}
}

// TestNoGapPastS: once S is stored, owner records past it are not applied,
// so none of them stops the lease.
func TestNoGapPastS(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 4, Boundary: &Boundary{S: 4, T: time.Now().UTC()}})
	if ok, _, err := s.StopForGap(ctx, ref, 1, 6); err != nil || ok {
		t.Fatalf("a record past S stops the lease: %v %v", ok, err)
	}
	if got := commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 1, AppliedSeq: 4}); got.Refused != "" {
		t.Fatalf("a commit after a record past S: %+v", got)
	}
}

// TestACommitsMoneyNeverWraps: money whose sum passes int64's range, here
// two leases' usage on one shard, is an error, and nothing is written, so no
// usage that wrapped reads as credit.
func TestACommitsMoneyNeverWraps(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 100)
	var refs []LeaseRef
	for range 2 {
		req := grantOf(ws, 30)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		refs = append(refs, LeaseRef{ws, req.LeaseID})
	}
	rows := readRows(t, ws)
	_, _, err := s.Commit(ctx, []CommitRequest{
		{Ref: refs[0], AppliedSeq: 1, Money: []MoneyOp{Book(math.MaxInt64, 0)}},
		{Ref: refs[1], AppliedSeq: 1, Money: []MoneyOp{Book(200, 0)}},
	})
	if !errors.Is(err, errMoneyRange) {
		t.Fatalf("a commit whose usage passes int64's range: %v", err)
	}
	if after := readRows(t, ws); !slices.Equal(after, rows) {
		t.Fatalf("the credit rows changed: %+v, then %+v", rows, after)
	}
	for _, ref := range refs {
		if l := readLease(t, s, ref); l.CommitVersion != 0 || l.Consumed != 0 || l.FaultUsage != 0 {
			t.Fatalf("lease %v changed: %+v", ref, l)
		}
	}
	identityHolds(t, s, ws)
}

func TestADrainingLeaseLoadsItsWinners(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(2, 0)},
		Winners: []Winner{{AuthorizationID: "a1", Kind: "settle", Charge: 2, RecordID: "o1"}}})
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("drain: %v %v", ok, err)
	}
	loaded, err := s.Load(ctx, ref)
	if err != nil || len(loaded.Packs) != 1 || loaded.Packs[0].Winners[0].AuthorizationID != "a1" || loaded.Lease.State != "draining" {
		t.Fatalf("a draining lease's load: %+v %v", loaded, err)
	}
	if !strings.Contains(string(mustJSON(t, loaded.Packs[0].Winners[0])), `"a":"a1"`) {
		t.Fatal("a winner's stored form")
	}
}

func mustJSON(t *testing.T, w Winner) []byte {
	t.Helper()
	b, err := json.Marshal(w)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// TestTheOwnersWritesRaceTheAuditorsCommits: whatever order Spanner
// serializes them in, the stored total is the largest any wrote, nothing a
// commit booked is lost, and the accounting holds.
func TestTheOwnersWritesRaceTheAuditorsCommits(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 50, 200)
	done := make(chan error, 2)
	go func() {
		for total := int64(1); total <= 12; total++ {
			if _, err := s.ShortfallWrite(ctx, owner, ref, total); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()
	go func() {
		for seq := int64(1); seq <= 8; seq++ {
			for {
				l, _, err := s.ReadLease(ctx, ref)
				if err != nil {
					done <- err
					return
				}
				got, _, err := s.Commit(ctx, []CommitRequest{{Ref: ref, ReadVersion: l.CommitVersion, AppliedSeq: seq,
					Money: []MoneyOp{Book(3, seq)}}})
				if err != nil {
					done <- err
					return
				}
				if got[0].Refused == "" {
					break
				}
			}
		}
		done <- nil
	}()
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	l := readLease(t, s, ref)
	if l.ShortfallTotal != 12 || l.Consumed != 24 || l.Allocation != 62 || l.CommitVersion != 8 {
		t.Fatalf("after the race: total %d, consumed %d, allocation %d, version %d", l.ShortfallTotal, l.Consumed,
			l.Allocation, l.CommitVersion)
	}
	identityHolds(t, s, ref.Workspace)
}
