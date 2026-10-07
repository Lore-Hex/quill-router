package store

import (
	"context"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"
)

// TestCheckIdentityFindsWhatBreaksIt: each way a workspace can break the
// accounting is reported, where the same workspace unbroken is not.
func TestCheckIdentityFindsWhatBreaksIt(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t)
	broken := func(name string, credits []int64, breakIt func(ws, lease string), want string) {
		t.Helper()
		ws := seedWorkspace(t, credits...)
		req := grantOf(ws, 30)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("%s: the grant: %+v %v", name, got, err)
		}
		identityHolds(t, s, ws)
		breakIt(ws, req.LeaseID)
		problems, err := s.CheckIdentity(ctx, ws)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if !strings.Contains(strings.Join(problems, "\n"), want) {
			t.Errorf("%s: problems %q, want one saying %q", name, problems, want)
		}
	}
	exec := func(sql string, params map[string]any) {
		t.Helper()
		_, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			_, err := txn.Update(ctx, spanner.Statement{SQL: sql, Params: params})
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	broken("a reservation no lease holds", []int64{100}, func(ws, _ string) {
		setRow(t, ws, 0, map[string]any{"reserved": int64(31)})
	}, "shard 0 reserves 31, and its live leases' donors hold 30")
	broken("marks that differ", []int64{100, 100}, func(ws, _ string) {
		setRow(t, ws, 1, map[string]any{"in_debt": true})
	}, "the rows' marks differ")
	broken("a negative row unmarked", []int64{100, 100}, func(ws, _ string) {
		setRow(t, ws, 1, map[string]any{"total_usage": int64(150)})
	}, "a row is negative and the rows are not marked")
	broken("marked while solvent", []int64{100}, func(ws, _ string) {
		setRows(t, ws, map[string]any{"in_debt": true})
	}, "the rows are marked and their signed sum is 70")
	broken("a lease its donors do not sum to", []int64{100}, func(ws, lease string) {
		exec(`UPDATE tr_lease SET allocation = 35, granted = 35 WHERE workspace_id = @w AND lease_id = @l`,
			map[string]any{"w": ws, "l": lease})
	}, "has allocation 35 and consumption 0, its donors 30 and 0")
}
