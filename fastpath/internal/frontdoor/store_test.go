package frontdoor

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The front door's tests against the store run on the Spanner emulator, as
// the store's own do (fastpath/README.md); without it they skip, and the
// rest run on fakes.
var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "frontdoor tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "frontdoor tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "frontdoor tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// TestAFrontDoorAgainstTheStore: an authorize is held under a lease the
// owner's top-up was granted; the request's settle, its owner not reached,
// is appended to the lease's drain log with its envelope's estimate, its
// charge above it raising the lease's allocation in the same write, and a
// retry of it finds its row; a refund the owner answers past its cutoff is
// appended too.
func TestAFrontDoorAgainstTheStore(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	s, err := store.New(shared, store.Config{LiveFor: time.Hour, Window: 30 * time.Second, Skew: 2 * time.Second,
		PublishDeadline: 5 * time.Second, MaxLife: 5 * time.Minute, Grace: time.Minute, Allowance: 1_000_000,
		RequiredTier: 3})
	if err != nil {
		t.Fatal(err)
	}
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), spanner.InsertMap("tr_credit_balance", map[string]any{
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	c := &clock{now: time.Now()}
	o, err := owner.New(ownerConfig(c, s), &fakeLog{records: map[string][][]byte{}})
	if err != nil {
		t.Fatal(err)
	}
	defer o.Stop()
	local, err := NewLocal(o, "node-a", "us-central1", key, everyWorkspace)
	if err != nil {
		t.Fatal(err)
	}
	ev := &events{}
	records := &fakeRecords{ev: ev}
	door := func(to Owners) *FrontDoor {
		d, err := New(Config{Enabled: everyWorkspace, Owners: to, Store: s, Records: records, Members: fakeMembers{owners("node-a")},
			Key: key, Shards: func(string) int64 { return 1 }, OwnerWait: time.Second, PublishWait: time.Second})
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	up, down := door(Direct{"node-a": local}), door(Direct{})

	var got Authorized
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if got = up.Authorize(ctx, AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40, Boot: []byte("boot")}); got.Status != Busy {
			break
		}
	}
	if got.Status != Admitted {
		t.Fatalf("the authorize: %+v", got)
	}
	e, err := Open(key, got.Envelope)
	if err != nil {
		t.Fatal(err)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: e.Lease}
	before, _, err := s.ReadLease(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	full := []byte(`{"full":"record of r1"}`)
	settled := SettleOf{Envelope: got.Envelope, Charge: 55, Full: full, Money: []byte(`{"cost":55}`)}
	for i := 0; i < 2; i++ {
		if ans := down.Settle(ctx, settled); ans.Status != Recorded {
			t.Fatalf("settle %d with its owner not reached: %+v", i, ans)
		}
	}
	rows, _, err := s.ReadHoldDrainRows(ctx, ref, e.Auth)
	if err != nil {
		t.Fatal(err)
	}
	digest := digestOf(full)
	if len(rows) != 1 {
		t.Fatalf("the drain log's rows for the settle: %+v", rows)
	}
	r := rows[0]
	if r.RecordID != rowID(OwnerTerminal{Kind: "settle", Charge: 55, Digest: digest}, settled.Money) || r.Kind != "settle" ||
		r.Charge != 55 || r.Estimate != 40 ||
		r.DoorRaise != 15 || string(r.Digest) != string(digest) || string(r.Money) != `{"cost":55}` || r.Cause != "unreachable" {
		t.Fatalf("the settle's row: %+v", r)
	}
	after, _, err := s.ReadLease(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	if after.Allocation != before.Allocation+15 || after.DoorRaised != before.DoorRaised+15 {
		t.Fatalf("the lease's allocation %d and raises %d, before %d and %d", after.Allocation, after.DoorRaised,
			before.Allocation, before.DoorRaised)
	}
	// Settles that state other money fields or another charge, with the
	// same full record, are other terminals: each has its row.
	for _, other := range []SettleOf{{Envelope: got.Envelope, Charge: 55, Full: full, Money: []byte(`{"cost":55,"x":1}`)},
		{Envelope: got.Envelope, Charge: 56, Full: full, Money: settled.Money}} {
		if ans := down.Settle(ctx, other); ans.Status != Recorded {
			t.Fatalf("another settle: %+v", ans)
		}
	}
	if rows, _, err = s.ReadHoldDrainRows(ctx, ref, e.Auth); err != nil || len(rows) != 3 {
		t.Fatalf("the hold's rows after two other settles: %d %v", len(rows), err)
	}

	got = up.Authorize(ctx, AuthorizeOf{Workspace: ws, Request: "r2", Estimate: 40, Boot: []byte("boot")})
	if got.Status != Admitted {
		t.Fatalf("the second authorize: %+v", got)
	}
	second, err := Open(key, got.Envelope)
	if err != nil {
		t.Fatal(err)
	}
	c.advance(time.Hour) // past every lease's cutoff
	if ans := up.Refund(ctx, RefundOf{Envelope: got.Envelope, Money: []byte(`{"cost":0}`)}); ans.Status != Recorded {
		t.Fatalf("a refund its owner answers past its cutoff: %+v", ans)
	}
	rows, _, err = s.ReadHoldDrainRows(ctx, store.LeaseRef{Workspace: ws, LeaseID: second.Lease}, second.Auth)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].RecordID != rowID(OwnerTerminal{Kind: "refund"}, []byte(`{"cost":0}`)) ||
		rows[0].Kind != "refund" || rows[0].Charge != 0 ||
		rows[0].Estimate != 40 || rows[0].Cause != "past_cutoff" {
		t.Fatalf("the refund's rows: %+v", rows)
	}
}
