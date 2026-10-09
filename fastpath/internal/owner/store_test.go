package owner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The owner's tests against the store run on the Spanner emulator, as the
// store's own do (fastpath/README.md); without it they skip, and the rest
// run on fakes.
var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "owner tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "owner tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "owner tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

// TestTheOwnerAgainstTheStore: an owner of a lease the store granted renews
// it at Spanner's expiry, stores its shortfall total in the lease's row as
// soon as a settle raises it, and, once the lease closed and its last hold
// ended, publishes the final checkpoint and marks the lease draining itself.
func TestTheOwnerAgainstTheStore(t *testing.T) {
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
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(1000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	who := store.Owner{Node: "owner-1", Epoch: 3}
	id := store.NewLeaseID()
	granted, err := s.Grant(ctx, store.GrantRequest{Workspace: ws, LeaseID: id, Region: "us-central1", Owner: who,
		Amount: 100, KeyStatusVersion: 7})
	if err != nil || granted.Refused != "" {
		t.Fatalf("the grant: %+v %v", granted, err)
	}
	log := newFakeLog()
	o, err := New(Config{Epoch: who.Epoch, Node: who.Node, Spanner: s, RenewEvery: time.Hour, Window: 30 * time.Second, Enabled: everyWorkspace,
		KeyStatus: 7, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Clock: time.Now, NewAuthorization: store.NewAuthorizationID}, log)
	if err != nil {
		t.Fatal(err)
	}
	l, err := o.Take(id, ws, 100, granted.Expiry)
	if err != nil {
		t.Fatal(err)
	}
	defer o.Stop()
	ref := store.LeaseRef{Workspace: ws, LeaseID: id}
	a, err := l.Admit(Admission{Estimate: 60, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	b, err := l.Admit(Admission{Estimate: 40, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := l.Settle(ctx, a.Auth, 90, sum("a")); err != nil { // 90 and 40 held: 30 past the allocation
		t.Fatal(err)
	}
	waitFor(t, "the shortfall total in the lease's row", func() bool {
		row, _, err := s.ReadLease(ctx, ref)
		return err == nil && row.ShortfallTotal == 30 && row.Allocation == 130
	})
	if err := o.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	row, _, err := s.ReadLease(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	l.mu.Lock()
	expiry := l.expiry
	l.mu.Unlock()
	if !expiry.Equal(row.Expiry) || !row.Expiry.After(granted.Expiry) {
		t.Fatalf("the owner's expiry %v, the row's %v, granted %v", expiry, row.Expiry, granted.Expiry)
	}
	l.Close()
	if _, err := l.Settle(ctx, b.Auth, 40, sum("b")); err != nil {
		t.Fatal(err)
	}
	if err := o.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the owner's draining write", func() bool {
		row, _, err := s.ReadLease(ctx, ref)
		return err == nil && row.State == "draining" && row.DrainedBy.StringVal == "owner"
	})
	waitFor(t, "the lease let go", func() bool {
		_, held := o.Lease(id)
		return !held
	})
	recs := log.records(t, id)
	last := recs[len(recs)-1]
	if last.Kind != record.Checkpoint || !last.Checkpoint.Final || last.Checkpoint.Consumed != 130 {
		t.Fatalf("the lease's last record: %+v %+v", last, last.Checkpoint)
	}
}

// TestARevokedLeaseIsDropped: once its renewal is revoked in Spanner, the
// owner's next round finds it refused and stops using it: a heartbeat under
// it gets retry and a settle past_cutoff, for the front door's drain log.
func TestARevokedLeaseIsDropped(t *testing.T) {
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
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(1000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	who := store.Owner{Node: "owner-1", Epoch: 5}
	id := store.NewLeaseID()
	granted, err := s.Grant(ctx, store.GrantRequest{Workspace: ws, LeaseID: id, Region: "us-central1", Owner: who,
		Amount: 100, KeyStatusVersion: 7})
	if err != nil || granted.Refused != "" {
		t.Fatalf("the grant: %+v %v", granted, err)
	}
	log := newFakeLog()
	o, err := New(Config{Epoch: who.Epoch, Node: who.Node, Spanner: s, RenewEvery: time.Hour, Window: 30 * time.Second, Enabled: everyWorkspace,
		KeyStatus: 7, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Clock: time.Now, NewAuthorization: store.NewAuthorizationID}, log)
	if err != nil {
		t.Fatal(err)
	}
	defer o.Stop()
	l, err := o.Take(id, ws, 100, granted.Expiry)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := l.Admit(Admission{Estimate: 60, Stream: true, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	if took, _, err := s.Revoke(ctx, store.LeaseRef{Workspace: ws, LeaseID: id}); err != nil || !took {
		t.Fatalf("the revocation: %v %v", took, err)
	}
	if err := o.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if _, held := o.Lease(id); held {
		t.Fatal("a revoked lease is still held")
	}
	if _, err := l.Heartbeat(ctx, stream.Auth, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat under a revoked lease: %v", err)
	}
	if _, err := l.Settle(ctx, stream.Auth, 10, sum("s")); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle under a revoked lease: %v", err)
	}
	if n := len(log.records(t, id)); n != 0 {
		t.Fatalf("%d records under a revoked lease", n)
	}
}

// TestAShardIsGrantedItsLeaseByTheStore: a shard's first request finds no
// room, and the owner's ask is a grant in Spanner, which the next request is
// admitted under.
func TestAShardIsGrantedItsLeaseByTheStore(t *testing.T) {
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
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(1000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	o, err := New(Config{Epoch: 4, Node: "owner-1", Spanner: s, RenewEvery: time.Hour, Window: 30 * time.Second, Enabled: everyWorkspace,
		KeyStatus: 7, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Clock: time.Now, NewAuthorization: store.NewAuthorizationID,
		TopUps: TopUps{LowWater: 50, Cooldown: time.Second, Horizon: time.Minute, Min: 100, Max: 500,
			IdleAfter: time.Hour, MaxLife: time.Hour}}, newFakeLog())
	if err != nil {
		t.Fatal(err)
	}
	defer o.Stop()
	shard := ShardKey{Workspace: ws, Region: "us-central1", Shard: 0}
	if _, err := o.Admit(shard, Admission{Estimate: 50, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("the shard's first request: %v", err)
	}
	var id string
	waitFor(t, "the grant", func() bool {
		o.mu.Lock()
		defer o.mu.Unlock()
		if sh := o.shards[shard]; sh != nil && len(sh.leases) == 1 {
			id = sh.leases[0].id
			return true
		}
		return false
	})
	got, err := o.Admit(shard, Admission{Estimate: 50, Boot: boot})
	if err != nil || got.Lease != id {
		t.Fatalf("the next request: %+v %v", got, err)
	}
	row, _, err := s.ReadLease(ctx, store.LeaseRef{Workspace: ws, LeaseID: id})
	if err != nil || row.Granted != 100 || row.Owner != (store.Owner{Node: "owner-1", Epoch: 4}) || row.State != "open" {
		t.Fatalf("the granted lease's row: %+v %v", row, err)
	}
}

// TestTheOwnerAdoptsAgainstTheStore: a front door's append for a hold the
// owner holds, its charge above the hold, raises the lease in Spanner; the
// owner's next renewal adopts the row as its own record and counts the
// raise as allocation, so its books and the lease's agree.
func TestTheOwnerAdoptsAgainstTheStore(t *testing.T) {
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
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(1000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	who := store.Owner{Node: "owner-1", Epoch: 3}
	id := store.NewLeaseID()
	granted, err := s.Grant(ctx, store.GrantRequest{Workspace: ws, LeaseID: id, Region: "us-central1", Owner: who,
		Amount: 100, KeyStatusVersion: 7})
	if err != nil || granted.Refused != "" {
		t.Fatalf("the grant: %+v %v", granted, err)
	}
	log := newFakeLog()
	o, err := New(Config{Epoch: who.Epoch, Node: who.Node, Spanner: s, RenewEvery: time.Hour, Window: 30 * time.Second, Enabled: everyWorkspace,
		KeyStatus: 7, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Clock: time.Now, NewAuthorization: store.NewAuthorizationID,
		Grace: time.Minute, Records: &fakeRecords{}}, log)
	if err != nil {
		t.Fatal(err)
	}
	l, err := o.Take(id, ws, 100, granted.Expiry)
	if err != nil {
		t.Fatal(err)
	}
	defer o.Stop()
	ref := store.LeaseRef{Workspace: ws, LeaseID: id}
	a, err := l.Admit(Admission{Estimate: 10, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	got, err := s.Append(ctx, store.DrainTerminal{Ref: ref, AuthorizationID: a.Auth, RecordID: "d1", Kind: "settle",
		Charge: 15, Estimate: 10, Digest: sum("d1"), Money: []byte(`{}`), Cause: "owner unreachable"})
	if err != nil || got.Refused != "" || got.Raise != 5 {
		t.Fatalf("the append: %+v %v", got, err)
	}
	if err := o.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	var adopted record.Record
	for _, r := range log.records(t, id) {
		if r.Kind == record.Settle {
			adopted = r
		}
	}
	if adopted.Drain != "d1" || adopted.Auth != a.Auth || adopted.Charge != 15 || adopted.Shortfall != 0 {
		t.Fatalf("the adopted record: %+v", adopted)
	}
	row, _, err := s.ReadLease(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	if b := l.Books(); row.Allocation != 105 || b.Allocation != row.Allocation || b.Consumed != 15 {
		t.Fatalf("the lease's allocation %d, the owner's books %+v", row.Allocation, b)
	}
}
