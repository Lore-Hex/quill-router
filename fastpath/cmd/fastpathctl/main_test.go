package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "fastpathctl tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "fastpathctl tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "fastpathctl tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// onShared is the command's store, on the test's database.
func onShared(t *testing.T) func(context.Context, string) (*store.Store, func(), error) {
	t.Helper()
	if shared == nil {
		t.Skip(skipped)
	}
	return func(context.Context, string) (*store.Store, func(), error) {
		s, err := store.New(shared, service.Defaults().Store)
		return s, func() {}, err
	}
}

// TestTheCommandRefusesWhatItCannotDo: no database, no workspace, another
// command, each exit 2, and none reaches the store.
func TestTheCommandRefusesWhatItCannotDo(t *testing.T) {
	never := func(context.Context, string) (*store.Store, func(), error) {
		t.Fatal("the store was opened")
		return nil, nil, nil
	}
	for _, args := range [][]string{{"status", "ws"}, {"-database", "d", "status"}, {"-database", "d", "drop", "ws"},
		{"-database", "d", "status", ""}, {"-database", "d"}, {"-database", "d", "disable-all", "ws"},
		{"-database", "d", "enable", "ws", "more"}, {"-database", "d", "node"}, {"-database", "d", "node", ""},
		{"-database", "d", "node", "a", "b"}, {"-database", "d", "closed"}, {"-database", "d", "closed", "a", "b"},
		{"-database", "d", "leave"}, {"-database", "d", "leave", "a", "b"}, {"-database", "d", "audit"},
		{"-database", "d", "audit", "a", "b"}} {
		if code, err := run(context.Background(), args, &bytes.Buffer{}, never); code != 2 || err == nil {
			t.Errorf("%q: exit %d, %v", args, code, err)
		}
	}
	if _, err := run(context.Background(), []string{"-database", "d"}, &bytes.Buffer{}, never); err == nil ||
		!strings.HasPrefix(err.Error(), "usage:") {
		t.Errorf("no command: %v, want the usage", err)
	}
}

// TestStatusSaysWhenAWorkspaceIsDone: a workspace enabled is not done, exit
// 3, and once disabled, with nothing of the fast path left of it, it is,
// exit 0; enable and disable answer what they did.
func TestStatusSaysWhenAWorkspaceIsDone(t *testing.T) {
	open := onShared(t)
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	do := func(command string) (int, map[string]any) {
		t.Helper()
		var out bytes.Buffer
		code, err := run(ctx, []string{"-database", "d", command, ws}, &out, open)
		if err != nil {
			t.Fatalf("%s: %v", command, err)
		}
		var got map[string]any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Fatalf("%s wrote %q: %v", command, out.String(), err)
		}
		return code, got
	}
	if code, got := do("enable"); code != 0 || got["enabled"] != true {
		t.Fatalf("enable: exit %d, %v", code, got)
	}
	code, got := do("status")
	if why, _ := json.Marshal(got["why_not"]); code != 3 || got["done"] != false ||
		!strings.Contains(string(why), "enabled") {
		t.Fatalf("an enabled workspace's status: exit %d, %v", code, got)
	}
	if code, got := do("disable"); code != 0 || got["enabled"] != false || got["revoked_leases"] != float64(0) {
		t.Fatalf("disable: exit %d, %v", code, got)
	}
	if code, got := do("status"); code != 0 || got["done"] != true {
		t.Fatalf("a disabled workspace with nothing left: exit %d, %v", code, got)
	}
}

// onOwn is the command's store, on a database of the test's own, for a
// command that touches every workspace.
func onOwn(t *testing.T) func(context.Context, string) (*store.Store, func(), error) {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	db, err := emulator.Database(context.Background(), storetest.UniqueID("own"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return func(context.Context, string) (*store.Store, func(), error) {
		s, err := store.New(db, service.Defaults().Store)
		return s, func() {}, err
	}
}

// TestDisableAllTurnsEverythingOff: every workspace enabled is turned off,
// and the status of each then says so. It has its own database, so it turns
// off only its own workspaces.
func TestDisableAllTurnsEverythingOff(t *testing.T) {
	open := onOwn(t)
	ctx := context.Background()
	a, b := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	for _, ws := range []string{a, b} {
		if code, err := run(ctx, []string{"-database", "d", "enable", ws}, &bytes.Buffer{}, open); code != 0 || err != nil {
			t.Fatalf("enable %s: %d %v", ws, code, err)
		}
	}
	var out bytes.Buffer
	if code, err := run(ctx, []string{"-database", "d", "disable-all"}, &out, open); code != 0 || err != nil {
		t.Fatalf("disable-all: %d %v", code, err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.Bytes(), &got); err != nil || got["workspaces_turned_off"] != float64(2) ||
		got["revoked_leases"] != float64(0) {
		t.Fatalf("disable-all wrote %q: %v", out.String(), err)
	}
	for _, ws := range []string{a, b} {
		if code, err := run(ctx, []string{"-database", "d", "status", ws}, &bytes.Buffer{}, open); code != 0 || err != nil {
			t.Errorf("%s after disable-all: exit %d, %v", ws, code, err)
		}
	}
}

// full is an output that takes nothing.
type full struct{}

func (full) Write([]byte) (int, error) { return 0, errors.New("the output is full") }

// TestAnAnswerNotWrittenIsAFailure: each command whose answer cannot be
// written exits 1, though what it did is done.
func TestAnAnswerNotWrittenIsAFailure(t *testing.T) {
	open := onOwn(t)
	ws := storetest.UniqueID("ws")
	for _, args := range [][]string{{"enable", ws}, {"status", ws}, {"disable", ws}, {"disable-all"}} {
		code, err := run(context.Background(), append([]string{"-database", "d"}, args...), full{}, open)
		if code != 1 || err == nil {
			t.Errorf("%q with its answer not written: exit %d, %v", args, code, err)
		}
	}
}

// TestNodeSaysWhenANodeMayStop: a node with no row may not stop, exit 3;
// joined and marked leaving, owning no lease, it may, exit 0.
func TestNodeSaysWhenANodeMayStop(t *testing.T) {
	open := onShared(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	node := func() (int, map[string]any) {
		t.Helper()
		var out bytes.Buffer
		code, err := run(ctx, []string{"-database", "d", "node", address}, &out, open)
		if err != nil {
			t.Fatal(err)
		}
		var got map[string]any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Fatalf("node wrote %q: %v", out.String(), err)
		}
		return code, got
	}
	if code, got := node(); code != 3 || got["done"] != false {
		t.Fatalf("a node with no row: exit %d, %v", code, got)
	}
	s, err := store.New(shared, service.Defaults().Store)
	if err != nil {
		t.Fatal(err)
	}
	epoch, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	if ok, _, err := s.Heartbeat(ctx, address, epoch, store.Leaving); err != nil || !ok {
		t.Fatalf("marking the node leaving: %v %v", ok, err)
	}
	if code, got := node(); code != 0 || got["done"] != true {
		t.Fatalf("a node leaving with no lease: exit %d, %v", code, got)
	}
}

// TestClosedSaysWhenNothingOfANodeIsLeft: closed exits 3 for a node with
// no row, and for one owning a lease, open or draining, which it lists,
// and 0 once every lease of the node's is closed, leaving or not.
func TestClosedSaysWhenNothingOfANodeIsLeft(t *testing.T) {
	open := onShared(t)
	ctx := context.Background()
	address := storetest.UniqueID("node")
	closed := func() (int, map[string]any) {
		t.Helper()
		var out bytes.Buffer
		code, err := run(ctx, []string{"-database", "d", "closed", address}, &out, open)
		if err != nil {
			t.Fatal(err)
		}
		var got map[string]any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Fatalf("closed wrote %q: %v", out.String(), err)
		}
		return code, got
	}
	if code, got := closed(); code != 3 || got["done"] != false {
		t.Fatalf("a node with no row: exit %d, %v", code, got)
	}
	s, err := store.New(shared, service.Defaults().Store)
	if err != nil {
		t.Fatal(err)
	}
	epoch, _, err := s.Join(ctx, address, []string{"owner"})
	if err != nil {
		t.Fatal(err)
	}
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), spanner.InsertMap("tr_credit_balance",
		map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": int64(100), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	req := store.GrantRequest{Workspace: ws, LeaseID: store.NewLeaseID(), Region: "us-central1",
		Owner: store.Owner{Node: address, Epoch: epoch}, Amount: 10}
	if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
		t.Fatalf("the grant: %+v %v", got, err)
	}
	code, got := closed()
	why, _ := got["why_not"].([]any)
	if code != 3 || got["done"] != false || len(why) != 2 || !strings.Contains(fmt.Sprint(why[1]), req.LeaseID) {
		t.Fatalf("a node owning an open lease: exit %d, %v", code, got)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: req.LeaseID}
	if ok, _, err := s.OwnerMarkDraining(ctx, req.Owner, ref); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	if code, got := closed(); code != 3 || got["done"] != false {
		t.Fatalf("a node owning a draining lease: exit %d, %v", code, got)
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.UpdateMap("tr_lease", map[string]any{"workspace_id": ws,
		"lease_id": req.LeaseID, "state": "closed", "closed_at": time.Now(), "close_kind": "operator"})}); err != nil {
		t.Fatal(err)
	}
	if code, got := closed(); code != 3 || got["done"] != false {
		t.Fatalf("a node whose every lease is closed, its row still serving: exit %d, %v", code, got)
	}
	leave := func(addr string) (int, map[string]any) {
		t.Helper()
		var out bytes.Buffer
		code, err := run(ctx, []string{"-database", "d", "leave", addr}, &out, open)
		if err != nil {
			t.Fatal(err)
		}
		var got map[string]any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Fatalf("leave wrote %q: %v", out.String(), err)
		}
		return code, got
	}
	if code, got := leave(storetest.UniqueID("nobody")); code != 3 || got["found"] != false {
		t.Fatalf("leaving a node with no row: exit %d, %v", code, got)
	}
	if code, got := leave(address); code != 0 || got["found"] != true {
		t.Fatalf("leaving the node: exit %d, %v", code, got)
	}
	if code, got := closed(); code != 0 || got["done"] != true {
		t.Fatalf("a node fenced, its every lease closed: exit %d, %v", code, got)
	}
}

// TestAuditHoldsTheRowsToTheIdentity: a workspace with no credit rows cannot
// be audited, exit 1; one whose rows keep the identity answers no problem,
// exit 0; and a reservation no lease holds is a problem named, exit 3, with
// the rows left as they were.
func TestAuditHoldsTheRowsToTheIdentity(t *testing.T) {
	open := onShared(t)
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	audit := func(flags ...string) (int, map[string]any, error) {
		t.Helper()
		var out bytes.Buffer
		code, err := run(ctx, append(append([]string{"-database", "d"}, flags...), "audit", ws), &out, open)
		var got map[string]any
		if out.Len() > 0 {
			if err := json.Unmarshal(out.Bytes(), &got); err != nil {
				t.Fatalf("audit wrote %q: %v", out.String(), err)
			}
		}
		return code, got, err
	}
	if code, _, err := audit(); code != 1 || !errors.Is(err, store.ErrCreditRowsIncomplete) {
		t.Fatalf("a workspace with no credit rows: exit %d, %v", code, err)
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), storetest.Member("node-a", 1),
		spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": ws, "shard": int64(0),
			"total_credits": int64(100), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	code, got, err := audit("-funded", "100")
	if problems, _ := got["problems"].([]any); code != 0 || err != nil || len(problems) != 0 ||
		got["status"].(map[string]any)["Enabled"] != true || got["sums"].(map[string]any)["Credits"] != float64(100) {
		t.Fatalf("a workspace whose rows keep the identity: exit %d, %v %v", code, got, err)
	}
	// What no row says: the funding.
	if code, got, err := audit("-funded", "90"); code != 3 || err != nil ||
		!strings.Contains(fmt.Sprint(got["problems"]), "hold 100 credits, and the workspace was funded 90") {
		t.Fatalf("funded 90: exit %d, %v %v", code, got, err)
	}
	// A reservation held by no lease, written as the emulator takes a change
	// to one column (DML).
	if _, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: "UPDATE tr_credit_balance SET reserved = 5 WHERE workspace_id = @w", Params: map[string]any{"w": ws}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	code, got, err = audit()
	problems, _ := got["problems"].([]any)
	if code != 3 || err != nil || len(problems) != 1 || !strings.Contains(problems[0].(string), "shard 0 reserves 5") {
		t.Fatalf("a reservation no lease holds: exit %d, %v %v", code, got, err)
	}
	// A lease's booking with its winner, once: kept; the same winner stored
	// again by a later commit books it twice, which the audit names.
	if _, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.Update(ctx, spanner.Statement{
			SQL: "UPDATE tr_credit_balance SET reserved = 0 WHERE workspace_id = @w", Params: map[string]any{"w": ws}})
		return err
	}); err != nil {
		t.Fatal(err)
	}
	s, _, err := open(ctx, "d")
	if err != nil {
		t.Fatal(err)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: storetest.UniqueID("l")}
	granted, err := s.Grant(ctx, store.GrantRequest{Workspace: ws, LeaseID: ref.LeaseID, Region: "us-central1",
		Owner: store.Owner{Node: "node-a", Epoch: 1}, Amount: 30})
	if err != nil || granted.Refused != "" {
		t.Fatalf("the grant: %+v %v", granted, err)
	}
	book := func(readVersion, seq int64) int64 {
		t.Helper()
		results, _, err := s.Commit(ctx, []store.CommitRequest{{Ref: ref, ReadVersion: readVersion, AppliedSeq: seq,
			Money:   []store.MoneyOp{store.Book(10, 0)},
			Winners: []store.Winner{{AuthorizationID: "auth-1", Kind: "settle", Charge: 10, RecordID: "rec-1"}}}})
		if err != nil || len(results) != 1 || results[0].Refused != "" {
			t.Fatalf("the commit at version %d: %+v %v", readVersion, results, err)
		}
		return results[0].NewVersion
	}
	version := book(0, 1)
	if code, got, err := audit("-funded", "100"); code != 0 || err != nil ||
		got["sums"].(map[string]any)["Consumed"] != float64(10) {
		t.Fatalf("one booking with its winner: exit %d, %v %v", code, got, err)
	}
	// Usage below what the retained leases booked.
	setUsage := func(usage int64) {
		t.Helper()
		if _, err := shared.ReadWriteTransaction(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			_, err := txn.Update(ctx, spanner.Statement{SQL: "UPDATE tr_credit_balance SET total_usage = @u WHERE workspace_id = @w",
				Params: map[string]any{"w": ws, "u": usage}})
			return err
		}); err != nil {
			t.Fatal(err)
		}
	}
	setUsage(0)
	if code, got, err := audit("-funded", "100"); code != 3 || err != nil ||
		!strings.Contains(fmt.Sprint(got["problems"]), "book 0 of usage, below the 10 the retained leases booked") {
		t.Fatalf("usage below the leases' bookings: exit %d, %v %v", code, got, err)
	}
	setUsage(10)
	// The same winner stored again by a later commit.
	version = book(version, 2)
	code, got, err = audit("-funded", "100")
	problems, _ = got["problems"].([]any)
	if code != 3 || err != nil || len(problems) != 1 ||
		!strings.Contains(problems[0].(string), "decides authorization auth-1 twice: settle in pack 1 and settle in pack 2") {
		t.Fatalf("the winner stored twice: exit %d, %v %v", code, got, err)
	}
	// One authorization decided as a release and then, under another
	// record, as a settle; and one pack naming a winner twice, written as
	// no commit would (the request refuses it), straight to the table.
	commit := func(readVersion, seq int64, money []store.MoneyOp, winners ...store.Winner) int64 {
		t.Helper()
		results, _, err := s.Commit(ctx, []store.CommitRequest{{Ref: ref, ReadVersion: readVersion, AppliedSeq: seq,
			Money: money, Winners: winners}})
		if err != nil || len(results) != 1 || results[0].Refused != "" {
			t.Fatalf("the commit at version %d: %+v %v", readVersion, results, err)
		}
		return results[0].NewVersion
	}
	version = commit(version, 3, nil, store.Winner{AuthorizationID: "auth-2", Kind: "release", Charge: 0, RecordID: "rec-2"})
	commit(version, 4, []store.MoneyOp{store.Book(10, 0)},
		store.Winner{AuthorizationID: "auth-2", Kind: "settle", Charge: 10, RecordID: "rec-3"})
	twice := store.Winner{AuthorizationID: "auth-3", Kind: "release", Charge: 0, RecordID: "rec-5"}
	packBody, err := json.Marshal(map[string]any{"v": 1, "winners": []store.Winner{twice, twice}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.InsertMap("tr_lease_winners", map[string]any{
		"workspace_id": ws, "lease_id": ref.LeaseID, "commit_version": int64(99), "pack": packBody, "winner_count": int64(2)})}); err != nil {
		t.Fatal(err)
	}
	code, got, err = audit("-funded", "100")
	problems, _ = got["problems"].([]any)
	if code != 3 || err != nil || len(problems) != 3 ||
		!strings.Contains(problems[0].(string), "auth-1 twice: settle in pack 1 and settle in pack 2") ||
		!strings.Contains(problems[1].(string), "auth-2 twice: release in pack 3 and settle in pack 4") ||
		!strings.Contains(problems[2].(string), "auth-3 twice: release in pack 99 and release in pack 99") {
		t.Fatalf("the winners stored twice: exit %d, %v %v", code, got, err)
	}
	// A booking apart from its winner's charge: the lease books 20 more,
	// past its donors' allocation, and its new winner charges 10, so the
	// retained packs charge 40 and the rows book 50.
	commit(version+1, 5, []store.MoneyOp{store.Book(20, 0)},
		store.Winner{AuthorizationID: "auth-4", Kind: "settle", Charge: 10, RecordID: "rec-6"})
	code, got, err = audit("-funded", "100")
	problems, _ = got["problems"].([]any)
	if code != 3 || err != nil || len(problems) != 4 ||
		!strings.Contains(problems[3].(string), "retained packs charge 40, and its rows book 50 (30 on the donors and 20 past them)") {
		t.Fatalf("a booking apart from its winner's charge: exit %d, %v %v", code, got, err)
	}
}
