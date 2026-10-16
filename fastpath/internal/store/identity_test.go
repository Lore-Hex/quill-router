package store

import (
	"context"
	"fmt"
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
	}, "shard 0 reserves 31; its live leases' donors hold 30 and its synchronous holds 0")
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

// TestCheckIdentityCountsSynchronousHolds: a workspace with requests of its
// own carries their unsettled holds in a shard's reserved credit beside its
// leases'; the identity counts them, and only them: a settled reservation,
// another workspace's hold and a hold on another shard are not this shard's.
func TestCheckIdentityCountsSynchronousHolds(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t)
	ws := seedWorkspace(t, 100, 100)
	req := grantOf(ws, 30)
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	identityHolds(t, s, ws)
	hold := func(id, workspace string, shard, amount int64, settled bool) {
		t.Helper()
		_, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			_, err := txn.Update(ctx, spanner.Statement{
				SQL: `INSERT INTO tr_reservation (reservation_id, workspace_id, ws_shard, credit_shard,
				        credit_reserved_micro, settled, expires_at)
				      VALUES (@id, @w, @shard, @shard, @amount, @settled, CURRENT_TIMESTAMP())`,
				Params: map[string]any{"id": id, "w": workspace, "shard": shard, "amount": amount, "settled": settled},
			})
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	reserved := func(shard int64) int64 {
		t.Helper()
		var r int64
		row, err := shared.Single().ReadRow(ctx, "tr_credit_balance", spanner.Key{ws, shard}, []string{"reserved"})
		if err != nil {
			t.Fatal(err)
		}
		if err := row.Columns(&r); err != nil {
			t.Fatal(err)
		}
		return r
	}
	problems := func() string {
		t.Helper()
		got, err := s.CheckIdentity(ctx, ws)
		if err != nil {
			t.Fatal(err)
		}
		return strings.Join(got, "\n")
	}
	// A request in flight on each shard, its hold in reserved as today's path
	// writes them both in one transaction: the identity holds.
	shard0, shard1 := reserved(0), reserved(1)
	hold(ws+"-a", ws, 0, 7, false)
	setRow(t, ws, 0, map[string]any{"reserved": shard0 + 7})
	hold(ws+"-b", ws, 1, 5, false)
	setRow(t, ws, 1, map[string]any{"reserved": shard1 + 5})
	if got := problems(); got != "" {
		t.Fatalf("holds counted, problems %q", got)
	}
	// Settled reservations and another workspace's hold count for nothing.
	hold(ws+"-c", ws, 0, 11, true)
	hold(ws+"-d", ws+"-other", 0, 13, false)
	if got := problems(); got != "" {
		t.Fatalf("a settled hold or another workspace's counted, problems %q", got)
	}
	// Reserved past its holds is still a problem, named with both sums.
	setRow(t, ws, 0, map[string]any{"reserved": shard0 + 8})
	want := fmt.Sprintf("shard 0 reserves %d; its live leases' donors hold %d and its synchronous holds 7", shard0+8, shard0)
	if got := problems(); !strings.Contains(got, want) {
		t.Fatalf("problems %q, want one saying %q", got, want)
	}
	// And a hold whose reserved credit was given back without settling it is
	// one too: the shard now reserves less than its holds.
	setRow(t, ws, 0, map[string]any{"reserved": shard0})
	want = fmt.Sprintf("shard 0 reserves %d; its live leases' donors hold %d and its synchronous holds 7", shard0, shard0)
	if got := problems(); !strings.Contains(got, want) {
		t.Fatalf("problems %q, want one saying %q", got, want)
	}
}
