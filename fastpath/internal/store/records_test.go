package store

import (
	"context"
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
	if err := s.DropStaged(ctx, a, []byte("original"), []byte("compacted")); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := s.ReadStaged(ctx, a, []byte("original")); err != nil || ok {
		t.Fatalf("a dropped record is read: %v %v", ok, err)
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
	pending, err := s.PendingPacks(ctx, 100000)
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
