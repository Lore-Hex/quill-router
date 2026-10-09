package store

import (
	"context"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// TestANodeMayStopOnlyOnceLeavingWithNoOpenLease: a node with no row is not
// done; serving and owning an open lease, it is not, for both; leaving with
// the lease still open, it is not; once the lease drains, it is. Another
// node's open lease is never counted.
func TestANodeMayStopOnlyOnceLeavingWithNoOpenLease(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address, other := storetest.UniqueID("node"), storetest.UniqueID("node")
	status := func(step string, wantDone bool, wantOpen int64, reasons int) NodeStatus {
		t.Helper()
		st, err := s.NodeStatus(ctx, address)
		if err != nil {
			t.Fatalf("%s: %v", step, err)
		}
		done, why := st.Done()
		if done != wantDone || st.OpenLeases != wantOpen || len(why) != reasons || st.ReadTS.IsZero() {
			t.Fatalf("%s: %+v, done %v %v", step, st, done, why)
		}
		return st
	}
	if st := status("no row", false, 0, 1); st.Found {
		t.Fatalf("a node with no row found: %+v", st)
	}
	epoch, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	ws := seedWorkspace(t, 100)
	mine := grantOf(ws, 10)
	mine.Owner = Owner{Node: address, Epoch: epoch}
	theirs := grantOf(ws, 10)
	theirs.Owner = Owner{Node: other, Epoch: 1}
	for _, req := range []GrantRequest{mine, theirs} {
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
	}
	if st := status("serving, a lease open", false, 1, 2); !st.Found || st.State != Serving || !st.Live ||
		st.Epoch != epoch {
		t.Fatalf("the serving node: %+v", st)
	}
	if ok, _, err := s.Heartbeat(ctx, address, epoch, Leaving); err != nil || !ok {
		t.Fatalf("marking the node leaving: %v %v", ok, err)
	}
	status("leaving, the lease still open", false, 1, 1)
	if ok, _, err := s.OwnerMarkDraining(ctx, mine.Owner, LeaseRef{ws, mine.LeaseID}); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	status("leaving, its lease draining", true, 0, 0)
}

// TestDoneNamesEachThingKeepingANode: each thing that keeps a node from
// stopping keeps it alone, and is the one reason given.
func TestDoneNamesEachThingKeepingANode(t *testing.T) {
	if done, why := (NodeStatus{Found: true, State: Leaving}).Done(); !done || len(why) != 0 {
		t.Fatalf("a node leaving and owning nothing open: %v %v", done, why)
	}
	for _, st := range []NodeStatus{{State: Leaving}, {Found: true, State: Serving}, {Found: true, State: Withdrawn},
		{Found: true, State: Leaving, OpenLeases: 1}} {
		if done, why := st.Done(); done || len(why) != 1 {
			t.Errorf("%+v: done %v %v; want one reason", st, done, why)
		}
	}
}
