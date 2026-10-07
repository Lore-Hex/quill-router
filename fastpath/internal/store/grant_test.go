package store

import (
	"context"
	"slices"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

func grantOf(ws string, amount int64) GrantRequest {
	return GrantRequest{Workspace: ws, LeaseID: storetest.UniqueID("l"), Region: "us-central1",
		Owner: Owner{Node: "node-a", Epoch: 1}, Amount: amount}
}

func reserved(rows []row) []int64 {
	out := make([]int64, len(rows))
	for i, r := range rows {
		out[i] = r.reserved
	}
	return out
}

// leaseCount is how many leases a workspace has, in any state.
func leaseCount(t *testing.T, ws string) int64 {
	t.Helper()
	var n int64
	err := shared.Single().Query(context.Background(), spanner.Statement{
		SQL: `SELECT COUNT(*) FROM tr_lease WHERE workspace_id = @w`, Params: map[string]any{"w": ws},
	}).Do(func(r *spanner.Row) error { return r.Column(0, &n) })
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func identityHolds(t *testing.T, s *Store, ws string) {
	t.Helper()
	problems, err := s.CheckIdentity(context.Background(), ws)
	if err != nil || len(problems) > 0 {
		t.Fatalf("the identity of %s: %v %v", ws, problems, err)
	}
}

func TestGrantReservesOnDonorsInShardOrder(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 50, 30, 100)
	req := grantOf(ws, 70)
	got, err := s.Grant(ctx, req)
	if err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	if want := []Donor{{0, 50}, {1, 20}}; !slices.Equal(got.Donors, want) {
		t.Fatalf("donors %v, want %v", got.Donors, want)
	}
	if r := reserved(readRows(t, ws)); !slices.Equal(r, []int64{50, 20, 0}) {
		t.Fatalf("reserved %v", r)
	}
	// The expiry is Spanner's time plus the window, read in the grant.
	if got.Expiry.After(got.CommitTS.Add(testConfig().Window)) || got.Expiry.Before(got.CommitTS.Add(testConfig().Window-5*time.Second)) {
		t.Fatalf("expiry %v for a commit at %v", got.Expiry, got.CommitTS)
	}
	row, err := shared.Single().ReadRow(ctx, "tr_lease", spanner.Key{ws, req.LeaseID},
		[]string{"state", "granted", "allocation", "consumed", "owner_node", "owner_epoch", "expiry", "revoked"})
	if err != nil {
		t.Fatal(err)
	}
	var state, node string
	var granted, allocation, consumed, epoch int64
	var expiry time.Time
	var revoked bool
	if err := row.Columns(&state, &granted, &allocation, &consumed, &node, &epoch, &expiry, &revoked); err != nil {
		t.Fatal(err)
	}
	if state != "open" || granted != 70 || allocation != 70 || consumed != 0 || node != "node-a" || epoch != 1 ||
		!expiry.Equal(got.Expiry) || revoked {
		t.Fatalf("the lease row: %s %d %d %d %s %d %v %v", state, granted, allocation, consumed, node, epoch, expiry, revoked)
	}
	identityHolds(t, s, ws)
}

func TestGrantRefusesWhatTheDesignRefuses(t *testing.T) {
	ctx := context.Background()
	latched := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	cases := []struct {
		name    string
		credits []int64
		set     map[string]any
		change  func(*Config)
		amount  int64
		refused Refusal
	}{
		{"in debt", []int64{100}, map[string]any{"in_debt": true}, nil, 10, RefusedInDebt},
		{"paused", []int64{100}, map[string]any{"billing_pause_causes": []string{"dispute"}}, nil, 10, RefusedPaused},
		{"below the tier", []int64{100}, map[string]any{"trust_tier": int64(2)}, nil, 10, RefusedTier},
		{"latched", []int64{100}, map[string]any{"trust_latched_at": latched}, nil, 10, RefusedTier},
		{"past the allowance", []int64{1000}, nil, func(c *Config) { c.Allowance = 100 }, 101, RefusedAllowance},
		{"below the floor", []int64{100}, nil, func(c *Config) { c.Floor = 30 }, 80, RefusedFloor},
		{"more than the headroom", []int64{40, 10}, nil, nil, 60, RefusedFloor},
	}
	for _, c := range cases {
		var changes []func(*Config)
		if c.change != nil {
			changes = append(changes, c.change)
		}
		s := spikeStore(t, changes...)
		ws := seedWorkspace(t, c.credits...)
		if c.set != nil {
			setRows(t, ws, c.set)
		}
		before := readRows(t, ws)
		got, err := s.Grant(ctx, grantOf(ws, c.amount))
		if err != nil || got.Refused != c.refused {
			t.Errorf("%s: %+v %v, want refused %q", c.name, got, err, c.refused)
			continue
		}
		if after := readRows(t, ws); !slices.Equal(after, before) || leaseCount(t, ws) != 0 {
			t.Errorf("%s: the refused grant wrote rows %+v, leases %d", c.name, after, leaseCount(t, ws))
		}
	}
	// An empty list of pause causes is no pause, as production reads it.
	s := spikeStore(t)
	ws := seedWorkspace(t, 100)
	setRows(t, ws, map[string]any{"billing_pause_causes": []string{}})
	if got, err := s.Grant(ctx, grantOf(ws, 10)); err != nil || got.Refused != "" {
		t.Fatalf("an empty pause list: %+v %v", got, err)
	}
}

// TestExposureCountsWhatTheDesignCounts: an open or draining lease counts
// its remaining allocation; a draining lease whose open holds are listed
// counts their estimates; a closed lease counts nothing (§4.2).
func TestExposureCountsWhatTheDesignCounts(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t, func(c *Config) { c.Allowance = 100 })
	ws := seedWorkspace(t, 1000)
	first := grantOf(ws, 60)
	if got, err := s.Grant(ctx, first); err != nil || got.Refused != "" {
		t.Fatalf("the first grant: %+v %v", got, err)
	}
	exec := func(sql string) {
		t.Helper()
		_, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			_, err := txn.Update(ctx, spanner.Statement{SQL: sql, Params: map[string]any{"w": ws, "l": first.LeaseID}})
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	try := func(amount int64, want Refusal) {
		t.Helper()
		got, err := s.Grant(ctx, grantOf(ws, amount))
		if err != nil || got.Refused != want {
			t.Fatalf("a grant of %d: %+v %v, want refused %q", amount, got, err, want)
		}
		if want == "" {
			// Leave the exposure as it was for the next step.
			exec(`UPDATE tr_lease SET state = 'closed', fence_time = TIMESTAMP_ADD(expiry, INTERVAL 1 SECOND),
			       drained_by = 'owner', closed_at = CURRENT_TIMESTAMP(), close_kind = 'auditor'
			       WHERE workspace_id = @w AND lease_id != @l AND state = 'open'`)
		}
	}
	try(41, RefusedAllowance) // 60 + 41
	exec(`UPDATE tr_lease SET consumed = 20 WHERE workspace_id = @w AND lease_id = @l`)
	try(61, RefusedAllowance) // 40 + 61
	try(60, "")
	exec(`UPDATE tr_lease SET state = 'draining', fence_time = TIMESTAMP_ADD(expiry, INTERVAL 1 SECOND),
	       drained_by = 'owner' WHERE workspace_id = @w AND lease_id = @l`)
	try(61, RefusedAllowance) // a draining lease still counts its 40
	exec(`UPDATE tr_lease SET holds_listed_seq = 5 WHERE workspace_id = @w AND lease_id = @l`)
	_, err := shared.Apply(ctx, []*spanner.Mutation{
		spanner.InsertMap("tr_lease_hold", map[string]any{"workspace_id": ws, "lease_id": first.LeaseID,
			"authorization_id": "a1", "estimate": int64(10), "deadline": time.Now().Add(time.Hour)}),
		spanner.InsertMap("tr_lease_hold", map[string]any{"workspace_id": ws, "lease_id": first.LeaseID,
			"authorization_id": "a2", "estimate": int64(15), "deadline": time.Now().Add(time.Hour)}),
	})
	if err != nil {
		t.Fatal(err)
	}
	try(76, RefusedAllowance) // listed: 25, not 40
	try(75, "")
	exec(`UPDATE tr_lease SET state = 'closed', closed_at = CURRENT_TIMESTAMP(), close_kind = 'auditor'
	       WHERE workspace_id = @w AND lease_id = @l`)
	try(100, "") // a closed lease counts nothing
}

func TestGrantRetryFindsItsLease(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t)
	ws := seedWorkspace(t, 100)
	req := grantOf(ws, 30)
	first, err := s.Grant(ctx, req)
	if err != nil || first.Refused != "" {
		t.Fatalf("the grant: %+v %v", first, err)
	}
	again, err := s.Grant(ctx, req)
	if err != nil || again.Refused != "" || !again.Expiry.Equal(first.Expiry) || !slices.Equal(again.Donors, first.Donors) {
		t.Fatalf("the retry: %+v %v, first %+v", again, err, first)
	}
	if r := reserved(readRows(t, ws)); !slices.Equal(r, []int64{30}) || leaseCount(t, ws) != 1 {
		t.Fatalf("after the retry: reserved %v, leases %d", r, leaseCount(t, ws))
	}
	other := req
	other.Owner = Owner{Node: "node-b", Epoch: 1}
	if got, err := s.Grant(ctx, other); err != nil || got.Refused != RefusedLeaseID {
		t.Fatalf("another owner's grant with the ID: %+v %v", got, err)
	}
	elsewhere := grantOf(seedWorkspace(t, 100), 30)
	elsewhere.LeaseID = req.LeaseID
	got, err := s.Grant(ctx, elsewhere)
	if err != nil || got.Refused != RefusedLeaseID {
		t.Fatalf("another workspace's grant with the ID: %+v %v", got, err)
	}
	if r := reserved(readRows(t, elsewhere.Workspace)); !slices.Equal(r, []int64{0}) {
		t.Fatalf("the refused grant in another workspace reserved %v", r)
	}
	identityHolds(t, s, ws)
	identityHolds(t, s, elsewhere.Workspace)
}

// TestGrantsRaceForOneWorkspace: grants that together ask for more than the
// headroom, all at once; whichever order Spanner serializes them in, the
// headroom grants two and refuses the rest, and nothing is reserved twice.
func TestGrantsRaceForOneWorkspace(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t)
	ws := seedWorkspace(t, 60, 40)
	var wg sync.WaitGroup
	results := make([]GrantResult, 6)
	errs := make([]error, 6)
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = s.Grant(ctx, grantOf(ws, 40))
		}(i)
	}
	wg.Wait()
	granted := 0
	for i, r := range results {
		if errs[i] != nil {
			t.Fatalf("grant %d: %v", i, errs[i])
		}
		switch r.Refused {
		case "":
			granted++
		case RefusedFloor:
		default:
			t.Fatalf("grant %d refused: %s", i, r.Refused)
		}
	}
	if granted != 2 || leaseCount(t, ws) != 2 {
		t.Fatalf("%d grants of 40 from 100 succeeded, %d leases", granted, leaseCount(t, ws))
	}
	if r := reserved(readRows(t, ws)); r[0]+r[1] != 80 {
		t.Fatalf("reserved %v", r)
	}
	identityHolds(t, s, ws)
}
