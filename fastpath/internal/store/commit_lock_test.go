package store

import (
	"slices"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"
)

func TestCommitReadDoesNotLockRenewalColumns(t *testing.T) {
	want := []string{"state", "allocation", "consumed", "shortfall_total", "returned", "fault_usage",
		"commit_version", "audit_fault_seq", "gap_seq", "boundary_seq"}
	if !slices.Equal(commitLeaseColumns, want) {
		t.Fatalf("commit projection %v, want only money and commit guards %v", commitLeaseColumns, want)
	}
	row, err := spanner.NewRow(want, []any{"open", int64(100), int64(10), int64(2), int64(3), int64(4),
		int64(5), spanner.NullInt64{Int64: 6, Valid: true}, spanner.NullInt64{}, spanner.NullInt64{Int64: 7, Valid: true}})
	if err != nil {
		t.Fatal(err)
	}
	var got Lease
	if err := got.scanCommit(row); err != nil {
		t.Fatal(err)
	}
	wantLease := Lease{State: "open", Allocation: 100, Consumed: 10, ShortfallTotal: 2, Returned: 3,
		FaultUsage: 4, CommitVersion: 5, AuditFaultSeq: spanner.NullInt64{Int64: 6, Valid: true},
		BoundarySeq: spanner.NullInt64{Int64: 7, Valid: true}}
	if got != wantLease {
		t.Fatalf("commit projection decoded %+v, want %+v", got, wantLease)
	}
}

func TestCommitOnlyTouchesRevocationForAnAuditFault(t *testing.T) {
	r := CommitRequest{Ref: LeaseRef{Workspace: "w", LeaseID: "l"}, ReadVersion: 3, AppliedSeq: 4}
	normal := commitLeaseStatement(r, leaseMoney{})
	if strings.Contains(normal.SQL, "revoked") || strings.Contains(normal.SQL, "expiry") {
		t.Fatalf("ordinary commit locks renewal fields: %s", normal.SQL)
	}
	r.AuditFault = ptr(int64(4))
	fault := commitLeaseStatement(r, leaseMoney{})
	if !strings.Contains(fault.SQL, "revoked = TRUE") || strings.Contains(fault.SQL, "expiry") {
		t.Fatalf("audit fault must revoke atomically without touching expiry: %s", fault.SQL)
	}
	for _, statement := range []spanner.Statement{normal, fault} {
		for _, guard := range []string{"commit_version = @read", "state != 'closed'", "gap_seq IS NULL", "boundary_seq = @applied"} {
			if !strings.Contains(statement.SQL, guard) {
				t.Errorf("commit lost guard %q", guard)
			}
		}
	}
}

func TestCommitPreservesAnExistingRevocationAndExpiry(t *testing.T) {
	s := spikeStore(t)
	ref := grantLease(t, s, 30, 100)
	if _, _, err := s.Revoke(t.Context(), ref); err != nil {
		t.Fatal(err)
	}
	before := readLease(t, s, ref)
	got := commitOne(t, s, CommitRequest{Ref: ref, AppliedSeq: 1, Money: []MoneyOp{Book(5, 0)}})
	after := readLease(t, s, ref)
	if got.Refused != "" || !after.Revoked || !after.Expiry.Equal(before.Expiry) || after.Consumed != 5 {
		t.Fatalf("commit must preserve revocation/expiry while booking: %+v before %+v after %+v", got, before, after)
	}
	identityHolds(t, s, ref.Workspace)
}
