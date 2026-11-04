package store

import (
	"context"
	"errors"
	"reflect"
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
	ref := wonLease(t, s)
	a, _ := NewAuthorizationID(ref.LeaseID)
	stage := func(ref LeaseRef, digest, body string) {
		t.Helper()
		if err := s.StageRecord(ctx, StagedRecord{AuthorizationID: a, Digest: []byte(digest), Ref: ref, Body: []byte(body),
			MessageID: "m-" + digest, PublishTime: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	stage(ref, "original", "full")
	stage(ref, "original", "full")
	stage(ref, "compacted", "retry")
	got, ok, err := s.ReadStaged(ctx, a, []byte("compacted"))
	if err != nil || !ok || string(got.Body) != "retry" {
		t.Fatalf("the compacted copy: %+v %v %v", got, ok, err)
	}
}

// TestStagedRecordsAreReadManyAtOnce: ReadStagedBatch reads, in one call,
// the staged record of each authorization and digest there is one for, as
// ReadStaged reads each: a compacted copy is not the original, and an
// authorization staged under another digest, or not staged at all, has no
// answer. A key given twice is answered once, none reads nothing, and more
// than MaxReadStaged are refused.
func TestStagedRecordsAreReadManyAtOnce(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := wonLease(t, s)
	var keys []StagedKey
	want := map[string]StagedRecord{}
	for i := range 3 {
		a, _ := NewAuthorizationID(ref.LeaseID)
		r := StagedRecord{AuthorizationID: a, Digest: []byte("original"), Ref: ref, Body: []byte("full " + a),
			MessageID: "m-" + a, PublishTime: time.Now().UTC().Truncate(time.Microsecond)}
		if err := s.StageRecord(ctx, r); err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			compacted := r
			compacted.Digest, compacted.Body = []byte("compacted"), []byte("retry")
			if err := s.StageRecord(ctx, compacted); err != nil {
				t.Fatal(err)
			}
		}
		keys = append(keys, StagedKey{a, r.Digest})
		want[a] = r
	}
	other, _ := NewAuthorizationID(ref.LeaseID)
	asked := append(slices.Clone(keys), keys[1], StagedKey{keys[2].AuthorizationID, []byte("another")},
		StagedKey{other, []byte("original")})
	got, err := s.ReadStagedBatch(ctx, asked...)
	if err != nil || len(got) != len(keys) {
		t.Fatalf("read %d records, %v; want %d", len(got), err, len(keys))
	}
	for _, r := range got {
		w, ok := want[r.AuthorizationID]
		if !ok || string(r.Digest) != "original" || string(r.Body) != string(w.Body) || r.Ref != ref ||
			r.MessageID != w.MessageID || !r.PublishTime.Equal(w.PublishTime) {
			t.Fatalf("read %+v, want %+v", r, w)
		}
		one, ok, err := s.ReadStaged(ctx, r.AuthorizationID, r.Digest)
		if !ok || err != nil || !reflect.DeepEqual(one, r) {
			t.Fatalf("ReadStaged reads %+v %v %v, ReadStagedBatch %+v", one, ok, err, r)
		}
		delete(want, r.AuthorizationID)
	}
	if got, err := s.ReadStagedBatch(ctx); got != nil || err != nil {
		t.Fatalf("no keys: %v %v", got, err)
	}
	if _, err := s.ReadStagedBatch(ctx, make([]StagedKey, MaxReadStaged+1)...); err == nil {
		t.Fatal("more than MaxReadStaged read at once")
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
	// Each is staged before its lease retires or goes, since nothing is
	// staged after.
	open, pending, retired, gone := wonLease(t, s), wonLease(t, s), wonLease(t, s), wonLease(t, s)
	kept := []StagedKey{stage(open), stage(pending)}
	dropped := []StagedKey{stage(retired), stage(gone)}
	execLease(t, pending, closeIt)
	execLease(t, retired, closeIt)
	if ok, err := s.MarkPackDone(ctx, retired, 1); err != nil || !ok {
		t.Fatalf("marking the retired lease's pack done: %v %v", ok, err)
	}
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

// TestNothingIsStagedForALeaseRetiredOrGone: a lease closed with a pack's
// work pending takes a staged record; once the pack's work is done and the
// lease retires, a redelivery of it, or another record, is refused, and so
// is one for a lease the store does not have; the records already staged
// are kept for the sweep.
func TestNothingIsStagedForALeaseRetiredOrGone(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := wonLease(t, s)
	a, err := NewAuthorizationID(ref.LeaseID)
	if err != nil {
		t.Fatal(err)
	}
	stage := func(ref LeaseRef, digest string) error {
		return s.StageRecord(ctx, StagedRecord{AuthorizationID: a, Digest: []byte(digest), Ref: ref, Body: []byte("full"),
			MessageID: "m-" + digest, PublishTime: time.Now()})
	}
	execLease(t, ref, closeIt)
	if err := stage(ref, "before"); err != nil {
		t.Fatalf("a record for a lease closed with its work pending: %v", err)
	}
	if ok, err := s.MarkPackDone(ctx, ref, 1); err != nil || !ok {
		t.Fatalf("the pack's work: %v %v", ok, err)
	}
	for _, digest := range []string{"before", "after"} {
		if err := stage(ref, digest); !errors.Is(err, ErrRetired) {
			t.Fatalf("%s, staged for a lease retired: %v", digest, err)
		}
	}
	if _, ok, err := s.ReadStaged(ctx, a, []byte("after")); ok || err != nil {
		t.Fatalf("a record refused was staged: %v %v", ok, err)
	}
	if _, ok, err := s.ReadStaged(ctx, a, []byte("before")); !ok || err != nil {
		t.Fatalf("the record staged before the lease retired: %v %v", ok, err)
	}
	if err := stage(LeaseRef{ref.Workspace, NewLeaseID()}, "nowhere"); !errors.Is(err, ErrNoLease) {
		t.Fatalf("a record for a lease the store does not have: %v", err)
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

// TestStageRecordsAnswersEachInOneTransaction: StageRecords stages, in one
// call, the records of leases that have not retired, and answers each as
// StageRecord would: a retired lease's ErrRetired, a lease with no row's
// ErrNoLease, an incomplete record's refusal. A record given twice leaves
// one row, and those it refuses are not staged.
func TestStageRecordsAnswersEachInOneTransaction(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	live, retired := wonLease(t, s), wonLease(t, s)
	if _, err := s.client.Apply(ctx, []*spanner.Mutation{spanner.Update("tr_lease",
		[]string{"workspace_id", "lease_id", "retire_at"},
		[]any{retired.Workspace, retired.LeaseID, time.Now()})}); err != nil {
		t.Fatal(err)
	}
	missing := LeaseRef{live.Workspace, NewLeaseID()}
	record := func(ref LeaseRef, digest string) StagedRecord {
		a, err := NewAuthorizationID(ref.LeaseID)
		if err != nil {
			t.Fatal(err)
		}
		return StagedRecord{AuthorizationID: a, Digest: []byte(digest), Ref: ref, Body: []byte("full " + digest),
			MessageID: "m-" + digest, PublishTime: time.Now()}
	}
	first, second := record(live, "one"), record(live, "two")
	incomplete := record(live, "three")
	incomplete.MessageID = ""
	rs := []StagedRecord{first, record(retired, "four"), second, record(missing, "five"), incomplete, first}
	errs, err := s.StageRecords(ctx, rs)
	if err != nil || len(errs) != len(rs) {
		t.Fatalf("the batch: %v %v", errs, err)
	}
	if errs[0] != nil || errs[2] != nil || errs[5] != nil {
		t.Fatalf("the live lease's records: %v", errs)
	}
	if !errors.Is(errs[1], ErrRetired) || !errors.Is(errs[3], ErrNoLease) || errs[4] == nil ||
		errors.Is(errs[4], ErrRetired) || errors.Is(errs[4], ErrNoLease) {
		t.Fatalf("the refused records: %v", errs)
	}
	for _, r := range []StagedRecord{first, second} {
		got, ok, err := s.ReadStaged(ctx, r.AuthorizationID, r.Digest)
		if err != nil || !ok || string(got.Body) != string(r.Body) {
			t.Fatalf("%s: %+v %v %v", r.Digest, got, ok, err)
		}
	}
	for _, r := range []StagedRecord{rs[1], rs[3], incomplete} {
		if _, ok, err := s.ReadStaged(ctx, r.AuthorizationID, r.Digest); err != nil || ok {
			t.Fatalf("%s was staged: %v %v", r.Digest, ok, err)
		}
	}
	if errs, err := s.StageRecords(ctx, nil); errs != nil || err != nil {
		t.Fatalf("none: %v %v", errs, err)
	}
	if errs, err := s.StageRecords(ctx, []StagedRecord{incomplete}); err != nil || len(errs) != 1 || errs[0] == nil {
		t.Fatalf("only an incomplete record: %v %v", errs, err)
	}
	if _, err := s.StageRecords(ctx, make([]StagedRecord, MaxStageRecords+1)); err == nil {
		t.Fatal("more than MaxStageRecords were taken")
	}
	big := record(live, "big")
	big.Body = make([]byte, MaxStageBytes/2+1)
	if _, err := s.StageRecords(ctx, []StagedRecord{big, big}); err == nil {
		t.Fatal("more than MaxStageBytes were taken")
	}
	alone := record(live, "alone")
	alone.Body = make([]byte, MaxStageBytes+1)
	if errs, err := s.StageRecords(ctx, []StagedRecord{alone}); err != nil || errs[0] != nil {
		t.Fatalf("one record past MaxStageBytes alone: %v %v", errs, err)
	}
}
