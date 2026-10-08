package store

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

// wonLease grants a lease with a lease ID an authorization can name, and
// commits the given winners, each charging 4, in one pack.
func wonLease(t *testing.T, s *Store, winners ...Winner) LeaseRef {
	t.Helper()
	req := grantOf(seedWorkspace(t, 100), 30)
	req.LeaseID = NewLeaseID()
	if got, err := s.Grant(context.Background(), req); err != nil || got.Refused != "" {
		t.Fatalf("grant: %+v %v", got, err)
	}
	ref := LeaseRef{req.Workspace, req.LeaseID}
	var money []MoneyOp
	for _, w := range winners {
		money = append(money, Book(w.Charge, 0))
	}
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: money, Winners: winners})
	return ref
}

func TestStagedRecordsJoinByAuthorizationAndDigest(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	lease := NewLeaseID()
	a, _ := NewAuthorizationID(lease)
	stage := func(ref LeaseRef, digest, body string) {
		t.Helper()
		if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: a, Digest: []byte(digest), Ref: ref, Body: []byte(body),
			MessageID: "m-" + digest, PublishTime: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	ref := LeaseRef{"ws", lease}
	stage(ref, "original", "full")
	stage(ref, "original", "full")
	stage(ref, "compacted", "retry")
	got, ok, err := s.ReadStaged(ctx, a, []byte("compacted"))
	if err != nil || !ok || string(got.Body) != "retry" {
		t.Fatalf("the compacted copy: %+v %v %v", got, ok, err)
	}
}

// TestStagedRecordsStayUntilNothingNeedsThem: a staged record stays until
// its authorization's winner is in a pack marked done, whatever records are
// written before, or, with no winner, until its lease has closed (§4.9).
func TestStagedRecordsStayUntilNothingNeedsThem(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	lease := NewLeaseID()
	ids := map[string]string{}
	for _, name := range []string{"settled", "refunded", "unseen"} {
		var err error
		if ids[name], err = NewAuthorizationID(lease); err != nil {
			t.Fatal(err)
		}
	}
	stage := func(ref LeaseRef) {
		t.Helper()
		for _, a := range ids {
			for _, d := range []string{"original", "compacted"} {
				if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: a, Digest: []byte(d), Ref: ref,
					Body: []byte("full"), MessageID: "m-" + d, PublishTime: time.Now()}); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	drop := func(name string) bool {
		t.Helper()
		dropped, err := s.DropStaged(ctx, ids[name], []byte("original"), []byte("compacted"))
		if err != nil {
			t.Fatal(err)
		}
		for _, d := range []string{"original", "compacted"} {
			if _, kept, err := s.ReadStaged(ctx, ids[name], []byte(d)); err != nil || kept == dropped {
				t.Fatalf("%s's %s: dropped %v, and kept %v, %v", name, d, dropped, kept, err)
			}
		}
		return dropped
	}
	req := grantOf(seedWorkspace(t, 100), 30)
	req.LeaseID = lease
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("grant: %+v %v", got, err)
	}
	ref := LeaseRef{req.Workspace, req.LeaseID}
	stage(ref)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(4, 0)},
		Winners: []Winner{{AuthorizationID: ids["settled"], Kind: "settle", Charge: 4, RecordID: "o1"},
			{AuthorizationID: ids["refunded"], Kind: "refund", RecordID: "o2"}}})
	// A crash between the winner's records: the generation record alone is
	// not its work done.
	if err := s.WriteRecords(ctx, []LeaseRecord{{AuthorizationID: ids["settled"], Kind: "generation", Ref: ref,
		Outcome: "settled", Cost: spanner.NullInt64{Int64: 4, Valid: true}, Body: []byte("{}")}}); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"settled", "refunded", "unseen"} {
		if drop(name) {
			t.Fatalf("%s's staged records go while the lease is open and its pack's work is pending", name)
		}
	}
	execLease(t, ref, closeIt)
	if drop("settled") || drop("refunded") {
		t.Fatal("a winner's staged records go before its pack's work is done")
	}
	if !drop("unseen") {
		t.Fatal("a closed lease keeps the staged records of an authorization with no winner")
	}
	if ok, err := s.MarkPackDone(ctx, ref, 1); err != nil || !ok {
		t.Fatalf("marking the pack done: %v %v", ok, err)
	}
	if !drop("settled") || !drop("refunded") {
		t.Fatal("a winner's staged records stay after its pack's work is done")
	}
	// A record staged again late, once retention has deleted the lease's row
	// and its packs, can still go.
	stage(ref)
	execLease(t, ref, `DELETE FROM tr_lease WHERE workspace_id = @w AND lease_id = @l`)
	for _, name := range []string{"settled", "refunded", "unseen"} {
		if !drop(name) {
			t.Fatalf("%s's staged records stay after the lease's row is gone", name)
		}
	}
	if dropped, err := s.DropStaged(ctx, "gwa-not-one-of-ours", []byte("d")); err == nil || dropped {
		t.Fatalf("a staged record of an authorization that names no lease: %v %v", dropped, err)
	}
}

// TestADoneWinnersRecordsGoOnAnOpenLease: the pack's work done is enough,
// whether or not the lease has closed.
func TestADoneWinnersRecordsGoOnAnOpenLease(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := wonLease(t, s)
	b, _ := NewAuthorizationID(ref.LeaseID)
	commitOne(t, s, CommitRequest{Ref: ref, ReadVersion: 1, AppliedSeq: 2, Money: []MoneyOp{Book(4, 0)},
		Winners: []Winner{{AuthorizationID: b, Kind: "settle", Charge: 4, RecordID: "o2"}}})
	if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: b, Digest: []byte("d"), Ref: ref, Body: []byte("full"),
		MessageID: "m", PublishTime: time.Now()}); err != nil {
		t.Fatal(err)
	}
	if dropped, err := s.DropStaged(ctx, b, []byte("d")); err != nil || dropped {
		t.Fatalf("before the pack is done: %v %v", dropped, err)
	}
	if ok, err := s.MarkPackDone(ctx, ref, 2); err != nil || !ok {
		t.Fatalf("marking the pack done: %v %v", ok, err)
	}
	if dropped, err := s.DropStaged(ctx, b, []byte("d")); err != nil || !dropped {
		t.Fatalf("after the pack is done: %v %v", dropped, err)
	}
}

// TestDispositionAnswersAsTheDesignSays: from the winner, then the record,
// then released for a lease the auditor closed, else pending.
func TestDispositionAnswersAsTheDesignSays(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	req := grantOf(seedWorkspace(t, 100), 30)
	req.LeaseID = NewLeaseID()
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("grant: %+v %v", got, err)
	}
	ref := LeaseRef{req.Workspace, req.LeaseID}
	won, _ := NewAuthorizationID(ref.LeaseID)
	recorded, _ := NewAuthorizationID(ref.LeaseID)
	unseen, _ := NewAuthorizationID(ref.LeaseID)
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(4, 0)},
		Winners: []Winner{{AuthorizationID: won, Kind: "settle", Charge: 4, RecordID: "o1"}}})
	if err := s.WriteRecords(ctx, []LeaseRecord{{AuthorizationID: recorded, Kind: "disposition", Ref: ref, Outcome: "refunded",
		Cost: spanner.NullInt64{Int64: 0, Valid: true}, Body: []byte("{}")}}); err != nil {
		t.Fatal(err)
	}
	answer := func(a string) Disposition {
		t.Helper()
		d, err := s.Disposition(ctx, a)
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	if d := answer(won); d.Outcome != "settled" || d.Cost.Int64 != 4 || d.From != "winner" {
		t.Fatalf("a winner's answer: %+v", d)
	}
	if d := answer(recorded); d.Outcome != "refunded" || d.From != "record" {
		t.Fatalf("a record's answer: %+v", d)
	}
	if d := answer(unseen); d.Outcome != "pending" {
		t.Fatalf("an open lease's unseen authorization: %+v", d)
	}
	if d := answer("gwa-not-one-of-ours"); d.Outcome != "pending" {
		t.Fatalf("an authorization the store cannot place: %+v", d)
	}
	execLease(t, ref, closeIt)
	if d := answer(unseen); d.Outcome != "released" || d.From != "closed" {
		t.Fatalf("an authorization of a lease the auditor closed: %+v", d)
	}
	execLease(t, ref, `UPDATE tr_lease SET close_kind = 'operator' WHERE workspace_id = @w AND lease_id = @l`)
	if d := answer(unseen); d.Outcome != "pending" {
		t.Fatalf("an authorization of a lease an operator closed: %+v", d)
	}
	if err := s.WriteRecords(ctx, []LeaseRecord{{AuthorizationID: recorded, Kind: "disposition", Ref: ref, Outcome: "refunded",
		Cost: spanner.NullInt64{Int64: 0, Valid: true}, Body: []byte("{}")}}); err != nil {
		t.Fatal(err)
	}
	var n int64
	err := shared.Single().Query(ctx, spanner.Statement{SQL: `SELECT COUNT(*) FROM tr_lease_record WHERE authorization_id = @a`,
		Params: map[string]any{"a": recorded}}).Do(func(r *spanner.Row) error { return r.Column(0, &n) })
	if err != nil || n != 1 {
		t.Fatalf("a record written twice: %d rows, %v", n, err)
	}
	pending, err := s.PendingPacks(ctx, PendingPack{}, 100000)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, p := range pending {
		found = found || p.Ref == ref
	}
	if !found {
		t.Fatal("the lease's pack with its work pending is not listed")
	}
}

// TestPendingPacksPageInKeyOrder: the sweep pages through the packs with
// work pending in key order, from the last it read, so one whose work stays
// pending holds up none after it.
func TestPendingPacksPageInKeyOrder(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 1000)
	var refs []LeaseRef
	for range 3 {
		req := grantOf(ws, 10)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		ref := LeaseRef{ws, req.LeaseID}
		commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1})
		refs = append(refs, ref)
	}
	slices.SortFunc(refs, func(a, b LeaseRef) int { return strings.Compare(a.LeaseID, b.LeaseID) })
	cursor := PendingPack{Ref: LeaseRef{Workspace: ws}}
	for _, want := range refs {
		page, err := s.PendingPacks(ctx, cursor, 1)
		if err != nil || len(page) != 1 || page[0] != (PendingPack{Ref: want, CommitVersion: 1}) {
			t.Fatalf("the page after %+v: %+v %v, want %v", cursor, page, err, want)
		}
		cursor = page[0]
	}
}
