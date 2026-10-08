package store

import (
	"context"
	"slices"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

func terminal(ref LeaseRef, a, record string, charge, estimate int64) DrainTerminal {
	return DrainTerminal{Ref: ref, AuthorizationID: a, RecordID: record, Kind: "settle", Charge: charge, Estimate: estimate,
		Digest: []byte("digest-" + record), Money: []byte(`{"cost":1}`), Cause: "test"}
}

func appendOK(t *testing.T, s *Store, d DrainTerminal) AppendResult {
	t.Helper()
	got, err := s.Append(context.Background(), d)
	if err != nil || got.Refused != "" {
		t.Fatalf("append %s: %+v %v", d.RecordID, got, err)
	}
	return got
}

func ids(rows []DrainRow) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.RecordID
	}
	return out
}

func TestAnAppendWithinItsEstimateRaisesNothing(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	got := appendOK(t, s, terminal(ref, "a1", "r1", 4, 10))
	if got.Raise != 0 || readLease(t, s, ref).Allocation != 30 {
		t.Fatalf("an append within its estimate: %+v", got)
	}
	rows, _, err := s.ReadDrainSince(context.Background(), ref, time.Time{})
	if err != nil || len(rows) != 1 || !rows[0].CommitTS.Equal(got.CommitTS) || rows[0].Charge != 4 {
		t.Fatalf("the log: %+v %v", rows, err)
	}
	identityHolds(t, s, ref.Workspace)
}

// TestAnAppendAboveItsEstimateRaisesTheLease answers CreditDebt's mutant
// front-door-appends-without-the-raise.
func TestAnAppendAboveItsEstimateRaisesTheLease(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	got := appendOK(t, s, terminal(ref, "a1", "r1", 15, 10))
	l := readLease(t, s, ref)
	if got.Raise != 5 || l.Allocation != 35 || l.DoorRaised != 5 || reserved(readRows(t, ref.Workspace))[0] != 35 {
		t.Fatalf("an append 5 above its estimate: %+v, allocation %d, raised %d", got, l.Allocation, l.DoorRaised)
	}
	identityHolds(t, s, ref.Workspace)
}

// TestAnAppendToAClosedLeaseIsRefused answers TerminalOrder's mutant
// append-after-close.
func TestAnAppendToAClosedLeaseIsRefused(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	execLease(t, ref, closeIt)
	before := readLease(t, s, ref)
	got, err := s.Append(context.Background(), terminal(ref, "a1", "r1", 15, 10))
	if err != nil || got.Refused != RefusedClosed {
		t.Fatalf("an append to a closed lease: %+v %v", got, err)
	}
	rows, _, _ := s.ReadDrainSince(context.Background(), ref, time.Time{})
	if len(rows) != 0 || readLease(t, s, ref) != before || reserved(readRows(t, ref.Workspace))[0] != 30 {
		t.Fatalf("the refused append wrote rows %v or a raise", rows)
	}
}

func TestAnAppendRetriedFindsItsRow(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	first := appendOK(t, s, terminal(ref, "a1", "r1", 15, 10))
	again := appendOK(t, s, terminal(ref, "a1", "r1", 15, 10))
	if again != first || readLease(t, s, ref).DoorRaised != 5 {
		t.Fatalf("the retry: %+v, first %+v", again, first)
	}
	other := terminal(ref, "a1", "r1", 20, 10)
	if _, err := s.Append(context.Background(), other); err == nil {
		t.Fatal("another terminal with the record ID is taken")
	}
	// Another record for the authorization is a second row, after the first.
	appendOK(t, s, terminal(ref, "a1", "r0", 3, 10))
	rows, _, err := s.ReadHoldDrainRows(context.Background(), ref, "a1")
	if err != nil || !slices.Equal(ids(rows), []string{"r1", "r0"}) {
		t.Fatalf("a1's rows: %v %v", ids(rows), err)
	}
	identityHolds(t, s, ref.Workspace)
}

func TestADrainingLeaseTakesAppends(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	execLease(t, ref, drainIt)
	appendOK(t, s, terminal(ref, "a1", "r1", 12, 10))
	identityHolds(t, s, ref.Workspace)
}

// TestTheDrainCursorSkipsNothing: read in the log's order, each read's
// timestamp is the next read's cursor, and two rows that share a commit
// timestamp come together, in record-ID order.
func TestTheDrainCursorSkipsNothing(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ref := grantLease(t, s, 30, 100)
	appendOK(t, s, terminal(ref, "a1", "r1", 1, 10))
	rows, cursor, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil || !slices.Equal(ids(rows), []string{"r1"}) {
		t.Fatalf("the first read: %v %v", ids(rows), err)
	}
	if rows, cursor, err = s.ReadDrainSince(ctx, ref, cursor); err != nil || len(rows) != 0 {
		t.Fatalf("a read with nothing new: %v %v", ids(rows), err)
	}
	// Two rows committed together, inserted in the reverse of their order.
	_, err = shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: `INSERT INTO tr_lease_drain (workspace_id, lease_id, authorization_id, record_id, kind, charge, estimate,
			        money, cause, commit_ts)
			      VALUES (@w, @l, 'a3', 'r3', 'refund', 0, 10, b'{}', 'test', PENDING_COMMIT_TIMESTAMP()),
			             (@w, @l, 'a2', 'r2', 'refund', 0, 10, b'{}', 'test', PENDING_COMMIT_TIMESTAMP())`,
			Params: ref.params(),
		})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	appendOK(t, s, terminal(ref, "a4", "r4", 1, 10))
	rows, _, err = s.ReadDrainSince(ctx, ref, cursor)
	if err != nil || !slices.Equal(ids(rows), []string{"r2", "r3", "r4"}) || !rows[0].CommitTS.Equal(rows[1].CommitTS) {
		t.Fatalf("the read after: %v %v", ids(rows), err)
	}
}
