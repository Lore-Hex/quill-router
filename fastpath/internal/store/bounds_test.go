package store

import (
	"context"
	"errors"
	"math"
	"slices"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The tests below put amounts at int64's bounds into rows no legitimate
// write makes, so that the store's arithmetic on them is exact (Codex,
// review round 1 of S4b).

func insertLease(t *testing.T, ws, lease string, granted int64) {
	t.Helper()
	row := leaseRow(ws, lease, time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC))
	row["granted"], row["allocation"] = granted, granted
	if _, err := shared.Apply(context.Background(), []*spanner.Mutation{spanner.InsertMap("tr_lease", row)}); err != nil {
		t.Fatal(err)
	}
}

func TestAnExposureAtTheTopRefusesAGrant(t *testing.T) {
	s := spikeStore(t, func(c *Config) { c.Allowance = math.MaxInt64 })
	ws := seedWorkspace(t, 20)
	insertLease(t, ws, storetest.UniqueID("l"), math.MaxInt64-1)
	got, err := s.Grant(context.Background(), grantOf(ws, 10))
	if err != nil || got.Refused != RefusedAllowance {
		t.Fatalf("a grant past an exposure of MaxInt64-1: %+v %v", got, err)
	}
}

func TestAReleaseRepairsANegativeRowUnmarked(t *testing.T) {
	spikeStore(t)
	// Production has such rows from before the debt rules: shard 0 is -30
	// with 10 of it reserved, and nothing is marked.
	ws := seedWorkspace(t, 0, 20)
	setRow(t, ws, 0, map[string]any{"total_usage": int64(20), "reserved": int64(10)})
	err := inTransaction(t, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		return release(ctx, txn, ws, 0, 10, "test")
	})
	if err != nil {
		t.Fatal(err)
	}
	if rows := readRows(t, ws); !slices.Equal(headrooms(rows), []int64{0, 0}) || slices.Contains(marks(rows), true) {
		t.Fatalf("after releasing 10 on [-30 20]: %v %v; production's take_inflow gives [0 0]", headrooms(rows), marks(rows))
	}
}

func TestCheckIdentityOfAWorkspaceWithoutRows(t *testing.T) {
	s := spikeStore(t)
	req := grantOf(seedWorkspace(t, 100), 10)
	if got, err := s.Grant(context.Background(), req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	_, err := shared.ReadWriteTransaction(context.Background(), func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{SQL: `DELETE FROM tr_credit_balance WHERE workspace_id = @w`,
			Params: map[string]any{"w": req.Workspace}})
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.CheckIdentity(context.Background(), req.Workspace); !errors.Is(err, ErrCreditRowsIncomplete) {
		t.Fatalf("a workspace with no credit rows: %v", err)
	}
}

func TestCheckIdentitySumsExactly(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	// A lease of 1 whose donors hold MaxInt64, MaxInt64 and 3, which wrap to 1.
	ws := seedWorkspace(t, math.MaxInt64, math.MaxInt64, 3)
	lease := storetest.UniqueID("l")
	insertLease(t, ws, lease, 1)
	var mutations []*spanner.Mutation
	for shard, a := range []int64{math.MaxInt64, math.MaxInt64, 3} {
		mutations = append(mutations, spanner.InsertMap("tr_lease_donor", map[string]any{"workspace_id": ws,
			"lease_id": lease, "credit_shard": int64(shard), "allocation": a}))
	}
	if _, err := shared.Apply(ctx, mutations); err != nil {
		t.Fatal(err)
	}
	for shard, r := range []int64{math.MaxInt64, math.MaxInt64, 3} {
		setRow(t, ws, int64(shard), map[string]any{"reserved": r})
	}
	problems, err := s.CheckIdentity(ctx, ws)
	if err != nil || !strings.Contains(strings.Join(problems, "\n"), "has allocation 1 and consumption 0, its donors 18446744073709551617") {
		t.Fatalf("a lease of 1 with donors summing past int64: %q %v", problems, err)
	}
	// Marked rows whose sum wraps negative are still solvent.
	ws = seedWorkspace(t, math.MaxInt64, math.MaxInt64)
	setRows(t, ws, map[string]any{"in_debt": true})
	problems, err = s.CheckIdentity(ctx, ws)
	if err != nil || !strings.Contains(strings.Join(problems, "\n"), "the rows are marked and their signed sum is 18446744073709551614") {
		t.Fatalf("marked rows summing past int64: %q %v", problems, err)
	}
}
