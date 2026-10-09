package store

import (
	"context"
	"testing"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// unswitched is a funded workspace with no switch row: what every workspace
// is until it is enabled.
func unswitched(t *testing.T, credits int64) string {
	t.Helper()
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(context.Background(), []*spanner.Mutation{spanner.InsertMap("tr_credit_balance",
		map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": credits, "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	return ws
}

func leasesOf(t *testing.T, ws string) int64 {
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

// TestAGrantNeedsItsWorkspaceEnabled: a workspace with no switch row, or one
// turned off, is granted nothing and nothing of it is written; once turned
// on, the same grant is taken.
func TestAGrantNeedsItsWorkspaceEnabled(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := unswitched(t, 100)
	before := readRows(t, ws)
	for _, step := range []string{"no row", "off"} {
		if step == "off" {
			if _, _, err := s.SetWorkspace(ctx, ws, false); err != nil {
				t.Fatal(err)
			}
		}
		got, err := s.Grant(ctx, grantOf(ws, 10))
		if err != nil || got.Refused != RefusedNotEnabled {
			t.Fatalf("%s: a grant for the workspace: %+v %v", step, got, err)
		}
		if after := readRows(t, ws); len(after) != len(before) || after[0] != before[0] || leasesOf(t, ws) != 0 {
			t.Fatalf("%s: a refused grant wrote: rows %+v, then %+v, %d leases", step, before, after, leasesOf(t, ws))
		}
	}
	if _, _, err := s.SetWorkspace(ctx, ws, true); err != nil {
		t.Fatal(err)
	}
	if got, err := s.Grant(ctx, grantOf(ws, 10)); err != nil || got.Refused != "" {
		t.Fatalf("a grant for the workspace turned on: %+v %v", got, err)
	}
}

// TestTurningAWorkspaceOffRevokesItsOpenLeases: its open leases take no
// renewal and its grants are refused, its draining lease is left as it was,
// and turning it on again grants new leases but revives none.
func TestTurningAWorkspaceOffRevokesItsOpenLeases(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 100)
	grant := func() LeaseRef {
		t.Helper()
		req := grantOf(ws, 10)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		return LeaseRef{ws, req.LeaseID}
	}
	open, draining := grant(), grant()
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, draining); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	other := grant()
	revoked, _, err := s.SetWorkspace(ctx, ws, false)
	if err != nil || revoked != 2 {
		t.Fatalf("turning the workspace off revoked %d leases, %v; want its 2 open ones", revoked, err)
	}
	for _, ref := range []LeaseRef{open, other} {
		if l := readLease(t, s, ref); !l.Revoked {
			t.Errorf("open lease %s is not revoked", ref.LeaseID)
		}
		if got := renewOne(t, s, owner, ref); got.Renewed {
			t.Errorf("revoked lease %s took a renewal", ref.LeaseID)
		}
	}
	if l := readLease(t, s, draining); l.Revoked || l.State != "draining" {
		t.Errorf("the draining lease is %+v", l)
	}
	if got, err := s.Grant(ctx, grantOf(ws, 10)); err != nil || got.Refused != RefusedNotEnabled {
		t.Fatalf("a grant for the workspace turned off: %+v %v", got, err)
	}
	if revoked, _, err := s.SetWorkspace(ctx, ws, true); err != nil || revoked != 0 {
		t.Fatalf("turning the workspace on: revoked %d, %v", revoked, err)
	}
	if l := readLease(t, s, open); !l.Revoked {
		t.Error("turning the workspace on revived a revoked lease")
	}
	grant()
}

// TestEnabledWorkspacesReadsTheSwitch: the workspaces turned on, and none
// turned off or never switched.
func TestEnabledWorkspacesReadsTheSwitch(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	on, off, never := unswitched(t, 1), unswitched(t, 1), unswitched(t, 1)
	for _, w := range []struct {
		ws      string
		enabled bool
	}{{on, true}, {off, true}, {off, false}} {
		if _, _, err := s.SetWorkspace(ctx, w.ws, w.enabled); err != nil {
			t.Fatal(err)
		}
	}
	got, read, err := s.EnabledWorkspaces(ctx)
	if err != nil || read.IsZero() {
		t.Fatalf("the read: %v at %v", err, read)
	}
	if !got[on] || got[off] || got[never] {
		t.Fatalf("enabled %v; want %s and neither %s nor %s", got, on, off, never)
	}
}
