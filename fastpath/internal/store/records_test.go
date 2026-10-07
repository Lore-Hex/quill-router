package store

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

func TestStagedRecordsJoinByAuthorizationAndDigest(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := LeaseRef{"ws", NewLeaseID()}
	a, _ := NewAuthorizationID(ref.LeaseID)
	stage := func(digest, body string) {
		t.Helper()
		if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: a, Digest: []byte(digest), Ref: ref, Body: []byte(body),
			MessageID: "m-" + digest, PublishTime: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	stage("original", "full")
	stage("original", "full")
	stage("compacted", "retry")
	got, ok, err := s.ReadStaged(ctx, a, []byte("compacted"))
	if err != nil || !ok || string(got.Body) != "retry" {
		t.Fatalf("the compacted copy: %+v %v %v", got, ok, err)
	}
	if err := s.WriteRecords(ctx, []LeaseRecord{{AuthorizationID: a, Kind: "generation", Ref: ref, Outcome: "settled",
		Body: []byte("{}")}}); err != nil {
		t.Fatal(err)
	}
	if dropped, err := s.DropStaged(ctx, a, []byte("original"), []byte("compacted")); err != nil || !dropped {
		t.Fatalf("dropping records written: %v %v", dropped, err)
	}
	if _, ok, err := s.ReadStaged(ctx, a, []byte("original")); err != nil || ok {
		t.Fatalf("a dropped record is read: %v %v", ok, err)
	}
}

// TestStagedRecordsStayUntilNothingNeedsThem: a staged record stays until a
// record for its authorization is written, or its lease has closed with no
// charged winner for it (§4.9).
func TestStagedRecordsStayUntilNothingNeedsThem(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	req := grantOf(seedWorkspace(t, 100), 30)
	req.LeaseID = NewLeaseID()
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("grant: %+v %v", got, err)
	}
	ref := LeaseRef{req.Workspace, req.LeaseID}
	ids := map[string]string{}
	for _, name := range []string{"settled", "refunded", "lost", "unseen"} {
		var err error
		if ids[name], err = NewAuthorizationID(ref.LeaseID); err != nil {
			t.Fatal(err)
		}
		if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: ids[name], Digest: []byte("d"), Ref: ref,
			Body: []byte("full"), MessageID: "m-" + name, PublishTime: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	drop := func(name string) bool {
		t.Helper()
		dropped, err := s.DropStaged(ctx, ids[name], []byte("d"))
		if err != nil {
			t.Fatal(err)
		}
		_, kept, err := s.ReadStaged(ctx, ids[name], []byte("d"))
		if err != nil || kept == dropped {
			t.Fatalf("%s: dropped %v, and kept %v, %v", name, dropped, kept, err)
		}
		return dropped
	}
	commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(4, 0)},
		Winners: []Winner{{AuthorizationID: ids["settled"], Kind: "settle", Charge: 4, RecordID: "o1"},
			{AuthorizationID: ids["refunded"], Kind: "refund", RecordID: "o2"}}})
	for _, name := range []string{"settled", "refunded", "unseen"} {
		if drop(name) {
			t.Fatalf("%s's staged record goes while its lease is open and no record is written", name)
		}
	}
	execLease(t, ref, closeIt)
	if drop("settled") {
		t.Fatal("a charged winner's staged record goes before its records are written")
	}
	if !drop("refunded") || !drop("unseen") {
		t.Fatal("a closed lease keeps a staged record no winner needs")
	}
	if err := s.WriteRecords(ctx, []LeaseRecord{{AuthorizationID: ids["settled"], Kind: "generation", Ref: ref,
		Outcome: "settled", Cost: spanner.NullInt64{Int64: 4, Valid: true}, Body: []byte("{}")}}); err != nil {
		t.Fatal(err)
	}
	if !drop("settled") {
		t.Fatal("a staged record stays after its winner's records are written")
	}
	if dropped, err := s.DropStaged(ctx, "gwa-not-one-of-ours", []byte("d")); err == nil || dropped {
		t.Fatalf("a staged record of an authorization that names no lease: %v %v", dropped, err)
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
