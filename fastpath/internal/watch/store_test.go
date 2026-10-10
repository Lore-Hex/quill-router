package watch

import (
	"context"
	"fmt"
	"os"
	"slices"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

var (
	emulator *storetest.Emulator
	skipped  string
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "watch tests:", err)
		os.Exit(1)
	}
	code := m.Run()
	if emulator != nil {
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "watch tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// TestTheStoreReadsPendingWorkAndBookings: on a database of its own, since
// the pending work is every workspace's, the store names each pack whose
// work is not done and none that is, refuses to read more than its limit,
// and sums the workspace's bookings, not another's.
func TestTheStoreReadsPendingWorkAndBookings(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	db, err := emulator.Database(ctx, storetest.UniqueID("watch"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	s, err := store.New(db, store.Config{LiveFor: time.Hour, Window: 30 * time.Second, Skew: 2 * time.Second,
		PublishDeadline: 5 * time.Second, MaxLife: 5 * time.Minute, Grace: time.Minute, Allowance: 1_000_000,
		RequiredTier: 3})
	if err != nil {
		t.Fatal(err)
	}
	ws, other := storetest.UniqueID("ws"), storetest.UniqueID("ws")
	for _, w := range []struct {
		id    string
		usage []int64
	}{{ws, []int64{40, 2}}, {other, []int64{1000}}} {
		var mutations []*spanner.Mutation
		mutations = append(mutations, storetest.Enabled(w.id), storetest.Member("node-a", 1))
		for shard, usage := range w.usage {
			mutations = append(mutations, spanner.InsertMap("tr_credit_balance", map[string]any{"workspace_id": w.id,
				"shard": int64(shard), "total_credits": int64(10_000), "total_usage": usage, "trust_tier": int64(3)}))
		}
		if _, err := db.Apply(ctx, mutations); err != nil {
			t.Fatal(err)
		}
	}
	var want []string
	for i := range 3 {
		req := store.GrantRequest{Workspace: ws, LeaseID: store.NewLeaseID(), Region: "us-central1",
			Owner: store.Owner{Node: "node-a", Epoch: 1}, Amount: 100}
		if got, err := s.Grant(ctx, req); err != nil || got.Refused != "" {
			t.Fatalf("the grant: %+v %v", got, err)
		}
		ref := store.LeaseRef{Workspace: ws, LeaseID: req.LeaseID}
		got, _, err := s.Commit(ctx, []store.CommitRequest{{Ref: ref, AppliedSeq: 1}})
		if err != nil || len(got) != 1 || got[0].Refused != "" {
			t.Fatalf("the commit: %+v %v", got, err)
		}
		name := fmt.Sprintf("%s/%s/%d", ws, req.LeaseID, got[0].NewVersion)
		if i == 0 {
			// Its work done: not pending.
			if ok, err := s.MarkPackDone(ctx, ref, got[0].NewVersion); err != nil || !ok {
				t.Fatalf("the pack's work: %v %v", ok, err)
			}
			continue
		}
		want = append(want, name)
	}
	slices.Sort(want)
	src := Store{Store: s, Workspace: ws, Limit: 2}
	pending, err := src.Pending(ctx)
	slices.Sort(pending)
	if err != nil || !slices.Equal(pending, want) {
		t.Fatalf("the pending packs: %v %v, want %v", pending, err, want)
	}
	src.Limit = 1
	if _, err := src.Pending(ctx); err == nil {
		t.Fatal("more pending packs than the limit were read")
	}
	// A page at a time: every pack across the pages, and the limit across
	// them too.
	src = Store{Store: s, Workspace: ws, Limit: 10, Page: 1}
	if pending, err = src.Pending(ctx); err != nil || len(pending) != len(want) {
		t.Fatalf("the pending packs a page of one at a time: %v %v", pending, err)
	}
	slices.Sort(pending)
	if !slices.Equal(pending, want) {
		t.Fatalf("the pending packs a page of one at a time: %v, want %v", pending, want)
	}
	src.Limit = 1
	if _, err := src.Pending(ctx); err == nil {
		t.Fatal("more pending packs than the limit were read, a page at a time")
	}
	if booked, err := src.Booked(ctx); err != nil || booked != 42 {
		t.Fatalf("the workspace's bookings: %d %v", booked, err)
	}
}
