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

// TestRetiredStagedReadsWhatNothingNeeds: the staged records of a lease that
// has retired, closed with every pack's work done, or whose row is gone are
// read, and DropStaged drops them; those of a lease open, or closed with a
// pack's work pending, are not read (§4.9).
func TestRetiredStagedReadsWhatNothingNeeds(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	stage := func(ref LeaseRef) StagedKey {
		t.Helper()
		a, err := NewAuthorizationID(ref.LeaseID)
		if err != nil {
			t.Fatal(err)
		}
		k := StagedKey{AuthorizationID: a, Digest: []byte("d")}
		if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: a, Digest: k.Digest, Ref: ref, Body: []byte("full"),
			MessageID: "m", PublishTime: time.Now()}); err != nil {
			t.Fatal(err)
		}
		return k
	}
	open := wonLease(t, s)
	pending := wonLease(t, s)
	execLease(t, pending, closeIt)
	retired := wonLease(t, s)
	execLease(t, retired, closeIt)
	if ok, err := s.MarkPackDone(ctx, retired, 1); err != nil || !ok {
		t.Fatalf("marking the retired lease's pack done: %v %v", ok, err)
	}
	gone := wonLease(t, s)
	kept := []StagedKey{stage(open), stage(pending)}
	dropped := []StagedKey{stage(retired), stage(gone)}
	execLease(t, gone, `DELETE FROM tr_lease WHERE workspace_id = @w AND lease_id = @l`)
	// The database is the package's: other tests' records may be read too.
	seen := map[string]bool{}
	for {
		keys, err := s.RetiredStaged(ctx, MaxDropStaged)
		if err != nil {
			t.Fatal(err)
		}
		for _, k := range keys {
			seen[k.AuthorizationID] = true
		}
		if err := s.DropStaged(ctx, keys...); err != nil {
			t.Fatal(err)
		}
		if len(keys) < MaxDropStaged {
			break
		}
	}
	for _, k := range kept {
		if _, ok, err := s.ReadStaged(ctx, k.AuthorizationID, k.Digest); seen[k.AuthorizationID] || !ok || err != nil {
			t.Fatalf("a record a pending pack may need: read %v, kept %v %v", seen[k.AuthorizationID], ok, err)
		}
	}
	for _, k := range dropped {
		if _, ok, err := s.ReadStaged(ctx, k.AuthorizationID, k.Digest); !seen[k.AuthorizationID] || ok || err != nil {
			t.Fatalf("a record nothing needs: read %v, kept %v %v", seen[k.AuthorizationID], ok, err)
		}
	}
	if err := s.DropStaged(ctx, make([]StagedKey, MaxDropStaged+1)...); err == nil {
		t.Fatal("more staged records dropped at once than a commit holds")
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
