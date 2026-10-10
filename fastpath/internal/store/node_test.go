package store

import (
	"context"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

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
	if _, err := shared.Apply(ctx, []*spanner.Mutation{storetest.Member(other, 1)}); err != nil {
		t.Fatal(err)
	}
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

// TestANodeStartedAgainStillOwnsItsEarlierLeases: a lease granted at the
// node's first epoch is counted once the node has started again and is
// leaving at its second, so a node whose process started again with a
// lease of the old process still open is not done.
func TestANodeStartedAgainStillOwnsItsEarlierLeases(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	first, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	ws := seedWorkspace(t, 100)
	req := grantOf(ws, 10)
	req.Owner = Owner{Node: address, Epoch: first}
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	second, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil || second != first+1 {
		t.Fatalf("the second start: epoch %d, %v", second, err)
	}
	if ok, _, err := s.Heartbeat(ctx, address, second, Leaving); err != nil || !ok {
		t.Fatalf("marking the node leaving: %v %v", ok, err)
	}
	st, err := s.NodeStatus(ctx, address)
	if err != nil {
		t.Fatal(err)
	}
	if done, why := st.Done(); done || st.OpenLeases != 1 || st.Epoch != second || len(st.Roles) != 1 ||
		st.Roles[0] != "owner" {
		t.Fatalf("a node started again, with its first epoch's lease open: %+v, done %v %v", st, done, why)
	}
}

// TestAGrantNeedsAMemberServingAtItsEpoch: a grant is refused for an owner
// with no member row, one leaving, and one at another epoch than the
// row's, and taken for a member serving at its epoch; a grant retried after
// its member left still finds its lease, so a retry after an unknown
// outcome is answered as the first was.
func TestAGrantNeedsAMemberServingAtItsEpoch(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	ws := seedWorkspace(t, 100)
	address := storetest.UniqueID("node")
	req := grantOf(ws, 10)
	req.Owner = Owner{Node: address, Epoch: 1}
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != RefusedNotServing {
		t.Fatalf("a grant to a node with no row: %+v %v", got, err)
	}
	epoch, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	req.Owner.Epoch = epoch + 1
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != RefusedNotServing {
		t.Fatalf("a grant at an epoch the row does not have: %+v %v", got, err)
	}
	req.Owner.Epoch = epoch
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("a grant to a member serving: %+v %v", got, err)
	}
	if ok, _, err := s.Heartbeat(ctx, address, epoch, Leaving); err != nil || !ok {
		t.Fatalf("marking the node leaving: %v %v", ok, err)
	}
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" || got.Expiry.IsZero() {
		t.Fatalf("the retry of a grant whose member has left: %+v %v", got, err)
	}
	another := grantOf(ws, 10)
	another.Owner = req.Owner
	if got, err := s.Grant(ctx, another); err != nil || got.Refused != RefusedNotServing {
		t.Fatalf("a grant to a member leaving: %+v %v", got, err)
	}
	if n := leaseCount(t, ws); n != 1 {
		t.Fatalf("%d leases after the refusals", n)
	}
	identityHolds(t, s, ws)
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

// TestClosedJudgesANodeByItsLeases: a node is closed once it owns no lease
// open or draining, whatever its row says: not while a lease is open, nor
// while one is draining, stopped at a gap the auditor does not close, which
// is listed as such though Done no longer counts it; closed once every
// lease is, though the node was never marked leaving. The leases listed
// are bounded, and more are said to be. A node with no row is not closed.
func TestClosedJudgesANodeByItsLeases(t *testing.T) {
	s := spikeStore(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	status := func(step string) NodeStatus {
		t.Helper()
		st, err := s.NodeStatus(ctx, address)
		if err != nil {
			t.Fatalf("%s: %v", step, err)
		}
		return st
	}
	if closed, why := status("no row").Closed(); closed || len(why) != 1 {
		t.Fatalf("a node with no row: closed %v %v", closed, why)
	}
	epoch, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	ws := seedWorkspace(t, 100)
	var refs []LeaseRef
	for range 2 {
		req := grantOf(ws, 10)
		req.Owner = Owner{Node: address, Epoch: epoch}
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		refs = append(refs, LeaseRef{ws, req.LeaseID})
	}
	listed := unclosedListed
	unclosedListed = 1
	st := status("two leases open, one listed")
	unclosedListed = listed
	if closed, why := st.Closed(); closed || len(st.Unclosed) != 1 || !st.MoreUnclosed || len(why) != 2 {
		t.Fatalf("two open leases, one listed: %+v, closed %v %v", st, closed, why)
	}
	if st = status("two leases open"); len(st.Unclosed) != 2 || st.MoreUnclosed || st.OpenLeases != 2 {
		t.Fatalf("two open leases: %+v", st)
	}
	// The first lease drains and stops at a gap; the node is leaving.
	if ok, _, err := s.Heartbeat(ctx, address, epoch, Leaving); err != nil || !ok {
		t.Fatalf("marking the node leaving: %v %v", ok, err)
	}
	for _, ref := range refs {
		if ok, _, err := s.OwnerMarkDraining(ctx, Owner{Node: address, Epoch: epoch}, ref); err != nil || !ok {
			t.Fatalf("the draining write: %v %v", ok, err)
		}
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.UpdateMap("tr_lease", map[string]any{
		"workspace_id": refs[0].Workspace, "lease_id": refs[0].LeaseID, "gap_seq": int64(5)})}); err != nil {
		t.Fatal(err)
	}
	st = status("both draining, one at a gap")
	done, _ := st.Done()
	closed, why := st.Closed()
	if !done || closed || len(why) != 2 || len(st.Unclosed) != 2 || st.Unclosed[0].State != "draining" ||
		st.Unclosed[0].Gap == st.Unclosed[1].Gap {
		t.Fatalf("both draining, one at a gap: %+v, done %v, closed %v %v", st, done, closed, why)
	}
	for _, l := range st.Unclosed {
		if l.Gap != (l.Ref == refs[0]) {
			t.Fatalf("the gap is %s's: %+v", refs[0].LeaseID, st.Unclosed)
		}
	}
	if !strings.Contains(strings.Join(why, "; "), "stopped at a gap") {
		t.Fatalf("the gap is not said: %v", why)
	}
	// Both close, as the auditor or an operator closes them.
	var closes []*spanner.Mutation
	for _, ref := range refs {
		closes = append(closes, spanner.UpdateMap("tr_lease", map[string]any{"workspace_id": ref.Workspace,
			"lease_id": ref.LeaseID, "state": "closed", "closed_at": time.Now(), "close_kind": "operator"}))
	}
	if _, err := shared.Apply(ctx, closes); err != nil {
		t.Fatal(err)
	}
	if st = status("both closed"); len(st.Unclosed) != 0 || st.OpenLeases != 0 {
		t.Fatalf("both closed: %+v", st)
	}
	if closed, why := st.Closed(); !closed || len(why) != 0 {
		t.Fatalf("both closed: closed %v %v", closed, why)
	}
	// A node never marked leaving, its leases closed, is closed, not done.
	if _, _, err := s.Join(ctx, address, []string{"owner"}); err != nil {
		t.Fatal(err)
	}
	st = status("serving again, nothing left")
	done, _ = st.Done()
	if closed, _ := st.Closed(); done || !closed {
		t.Fatalf("serving with nothing left: done %v, closed %v: %+v", done, closed, st)
	}
}
