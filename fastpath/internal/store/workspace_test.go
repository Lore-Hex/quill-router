package store

import (
	"context"
	"testing"
	"time"

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
// another workspace's lease too; turning it on again grants new leases but
// revives none, and a retry of the grant of a lease revoked, draining or
// closed is refused, whether the workspace is off or on again, so no owner
// takes it up, while a retry of an open one's is answered.
func TestTurningAWorkspaceOffRevokesItsOpenLeases(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws, elsewhere := seedWorkspace(t, 100), seedWorkspace(t, 100)
	reqs := map[LeaseRef]GrantRequest{}
	grantIn := func(w string) LeaseRef {
		t.Helper()
		req := grantOf(w, 10)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		ref := LeaseRef{w, req.LeaseID}
		reqs[ref] = req
		return ref
	}
	grant := func() LeaseRef { return grantIn(ws) }
	open, draining, closed, theirs := grant(), grant(), grant(), grantIn(elsewhere)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, draining); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	execLease(t, closed, drainIt)
	execLease(t, closed, closeIt)
	// None of them is revoked: each retry below is refused for its state.
	retried := func(when string) {
		t.Helper()
		for name, ref := range map[string]LeaseRef{"draining": draining, "closed": closed} {
			if got, err := s.Grant(ctx, reqs[ref]); err != nil || got.Refused != RefusedRevoked {
				t.Errorf("%s, a retry of a %s lease's grant: %+v %v", when, name, got, err)
			}
		}
	}
	retried("with the workspace on")
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
	if l := readLease(t, s, closed); l.Revoked || l.State != "closed" {
		t.Errorf("the closed lease is %+v", l)
	}
	retried("with the workspace off")
	if l := readLease(t, s, theirs); l.Revoked {
		t.Error("another workspace's lease is revoked")
	}
	if got := renewOne(t, s, owner, theirs); !got.Renewed {
		t.Error("another workspace's lease took no renewal")
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
	if got, err := s.Grant(ctx, reqs[open]); err != nil || got.Refused != RefusedRevoked {
		t.Errorf("a retry of a revoked lease's grant: %+v %v", got, err)
	}
	retried("with the workspace on again")
	if got, err := s.Grant(ctx, reqs[theirs]); err != nil || got.Refused != "" || got.Expiry.IsZero() {
		t.Errorf("a retry of an open lease's grant: %+v %v", got, err)
	}
	grant()
}

// TestDisableAllTurnsEveryWorkspaceOff, on a database of its own, since it
// reaches every workspace: every enabled workspace turns off and every open
// lease is revoked, in one transaction, and no grant is taken after it.
func TestDisableAllTurnsEveryWorkspaceOff(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("all"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := New(db, testConfig())
	if err != nil {
		t.Fatal(err)
	}
	var leases []LeaseRef
	for _, ws := range []string{storetest.UniqueID("ws"), storetest.UniqueID("ws")} {
		if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), spanner.InsertMap("tr_credit_balance",
			map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": int64(100), "trust_tier": int64(3)})}); err != nil {
			t.Fatal(err)
		}
		req := grantOf(ws, 10)
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		leases = append(leases, LeaseRef{ws, req.LeaseID})
	}
	off, revoked, _, err := s.DisableAll(ctx)
	if err != nil || off != 2 || revoked != 2 {
		t.Fatalf("turning everything off: %d workspaces, %d leases, %v; want 2 and 2", off, revoked, err)
	}
	enabled, _, err := s.EnabledWorkspaces(ctx)
	if err != nil || len(enabled) != 0 {
		t.Fatalf("enabled after: %v %v", enabled, err)
	}
	for _, ref := range leases {
		got, _, err := s.Renew(ctx, owner, []LeaseRef{ref})
		if err != nil || len(got) != 1 || got[0].Renewed {
			t.Errorf("lease %s after everything was turned off: %+v %v", ref.LeaseID, got, err)
		}
		if g, err := s.Grant(ctx, grantOf(ref.Workspace, 10)); err != nil || g.Refused != RefusedNotEnabled {
			t.Errorf("a grant for %s after: %+v %v", ref.Workspace, g, err)
		}
	}
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

// TestAWorkspaceTurnedOffIsDoneOnlyOnceNothingIsLeft walks one workspace
// through turning off, the status read after each step: done only once it is
// off, its lease closed, what its donors held returned, and its pack's work
// done.
func TestAWorkspaceTurnedOffIsDoneOnlyOnceNothingIsLeft(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 100)
	status := func(step string, want WorkspaceStatus, done bool) {
		t.Helper()
		got, err := s.WorkspaceStatus(ctx, ws)
		if err != nil {
			t.Fatalf("%s: %v", step, err)
		}
		if got.ReadTS.IsZero() {
			t.Fatalf("%s: no read timestamp", step)
		}
		want.Workspace, want.ReadTS = ws, got.ReadTS
		if got != want {
			t.Fatalf("%s: %+v, want %+v", step, got, want)
		}
		if ok, why := got.Done(); ok != done || ok != (len(why) == 0) {
			t.Fatalf("%s: done %v %v, want %v", step, ok, why, done)
		}
	}
	status("enabled", WorkspaceStatus{Enabled: true}, false)
	req := grantOf(ws, 10)
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	ref := LeaseRef{ws, req.LeaseID}
	status("granted", WorkspaceStatus{Enabled: true, Open: 1, LeaseReserved: 10, CreditReserved: 10}, false)
	if _, _, err := s.SetWorkspace(ctx, ws, false); err != nil {
		t.Fatal(err)
	}
	status("off", WorkspaceStatus{Open: 1, Revoked: 1, LeaseReserved: 10, CreditReserved: 10}, false)
	if ok, _, err := s.OwnerMarkDraining(ctx, owner, ref); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	status("draining", WorkspaceStatus{Draining: 1, LeaseReserved: 10, CreditReserved: 10}, false)
	zero := int64(0)
	got, _, err := s.Commit(ctx, []CommitRequest{{Ref: ref, Boundary: &Boundary{S: 0, T: time.Now()},
		HoldsListedSeq: &zero}})
	if err != nil || len(got) != 1 || got[0].Refused != "" {
		t.Fatalf("S's commit: %+v %v", got, err)
	}
	status("S stored", WorkspaceStatus{Draining: 1, LeaseReserved: 10, CreditReserved: 10, PendingPacks: 1}, false)
	_, read, err := s.ReadDrainSince(ctx, ref, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	closed, err := s.CloseLease(ctx, ref, got[0].NewVersion, read, readLease(t, s, ref).Expiry)
	if err != nil || closed.Refused != "" || closed.Released != 10 {
		t.Fatalf("the close: %+v %v", closed, err)
	}
	status("closed", WorkspaceStatus{Closed: 1, PendingPacks: 1}, false)
	if ok, err := s.MarkPackDone(ctx, ref, got[0].NewVersion); err != nil || !ok {
		t.Fatalf("the pack's work: %v %v", ok, err)
	}
	status("its work done", WorkspaceStatus{Closed: 1}, true)
}

// TestDoneNamesEachThingLeft: each thing left of a workspace keeps it from
// done alone, and is the one reason given; with nothing left it is done.
func TestDoneNamesEachThingLeft(t *testing.T) {
	if done, why := (WorkspaceStatus{Closed: 3, CreditReserved: 7}).Done(); !done || len(why) != 0 {
		t.Fatalf("nothing of the fast path left: done %v, %v", done, why)
	}
	for _, left := range []WorkspaceStatus{{Enabled: true}, {Open: 1}, {Open: 1, Revoked: 1}, {Draining: 1},
		{LeaseReserved: 1}, {LeaseReserved: -1}, {PendingPacks: 1}} {
		if done, why := left.Done(); done || len(why) != 1 {
			t.Errorf("%+v: done %v, %v; want one reason", left, done, why)
		}
	}
}
