package owner

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

var key = ShardKey{Workspace: "ws-1", Region: "us-central1", Shard: 2}

// shardFixture is an owner with top-ups: a shard below 50 asks, at most
// every 10 s, for what it charged over a minute plus its holds' needs,
// between 10 and 1000; a lease idle 5 minutes, or an hour old, closes.
func shardFixture(t *testing.T) (*fixture, *fakeSpanner) {
	t.Helper()
	sp := newFakeSpanner()
	sp.expiry = start.Add(2 * time.Hour)
	f := newFixture(t, 1000, nil, func(c *Config) {
		c.Spanner, c.Node, c.RenewEvery, c.Window, c.KeyStatus = sp, "owner-1", time.Hour, 30*time.Second, 7
		c.TopUps = TopUps{LowWater: 50, Cooldown: 10 * time.Second, Horizon: time.Minute, Min: 10, Max: 1000,
			IdleAfter: 5 * time.Minute, MaxLife: time.Hour}
	})
	return f, sp
}

// leases are the shard's leases, oldest first.
func (f *fixture) leases(key ShardKey) []*Lease {
	f.owner.mu.Lock()
	defer f.owner.mu.Unlock()
	if sh := f.owner.shards[key]; sh != nil {
		return append([]*Lease(nil), sh.leases...)
	}
	return nil
}

func (f *fixture) waitLeases(t *testing.T, n int) []*Lease {
	t.Helper()
	waitFor(t, "the shard's leases", func() bool { return len(f.leases(key)) == n })
	return f.leases(key)
}

func admitTo(t *testing.T, f *fixture, e int64) Admitted {
	t.Helper()
	got, err := f.owner.Admit(key, Admission{Estimate: e, Boot: boot})
	if err != nil {
		t.Fatalf("admitting %d: %v", e, err)
	}
	return got
}

// TestAShardWithNoLeaseAsksForOne: its first request finds no room, and the
// owner asks Spanner for a lease of the shard, of the least size, with
// nothing charged yet; the next request is admitted under it.
func TestAShardWithNoLeaseAsksForOne(t *testing.T) {
	f, sp := shardFixture(t)
	if _, err := f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request for a shard with no lease: %v", err)
	}
	ls := f.waitLeases(t, 1)
	grants := sp.granted()
	if len(grants) != 1 || grants[0].Workspace != "ws-1" || grants[0].Region != "us-central1" ||
		grants[0].WorkspaceShard != 2 || grants[0].Amount != 10 || grants[0].Owner != (store.Owner{Node: "owner-1", Epoch: 3}) ||
		grants[0].LeaseID != ls[0].id || grants[0].KeyStatusVersion != 7 {
		t.Fatalf("the grants: %+v", grants)
	}
	if got := admitTo(t, f, 5); got.Lease != ls[0].id {
		t.Fatalf("admitted under %s, and the shard's lease is %s", got.Lease, ls[0].id)
	}
}

// TestAShardAdmitsUnderItsOldestLeaseWithRoom, and asks for another while
// its room is under the low-water mark: one ask at a time, none within the
// cooldown of the last.
func TestAShardAdmitsUnderItsOldestLeaseWithRoom(t *testing.T) {
	f, sp := shardFixture(t)
	sp.mu.Lock()
	sp.grantGate = make(chan struct{})
	sp.mu.Unlock()
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	waitFor(t, "the ask", func() bool { return len(sp.granted()) == 1 })
	f.clock.advance(time.Minute) // the cooldown passes, and the ask is still outstanding
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	time.Sleep(20 * time.Millisecond)
	if n := len(sp.granted()); n != 1 {
		t.Fatalf("%d asks while one is outstanding", n)
	}
	sp.mu.Lock()
	close(sp.grantGate)
	sp.grantGate = nil
	sp.mu.Unlock()
	first := f.waitLeases(t, 1)[0] // 10
	admitTo(t, f, 4)
	if n := len(sp.granted()); n != 1 {
		t.Fatalf("%d asks within the cooldown", n)
	}
	f.clock.advance(10 * time.Second)
	admitTo(t, f, 4) // room 2, and the cooldown has passed
	second := f.waitLeases(t, 2)[1]
	if got := sp.granted()[1].Amount; got != 10 {
		t.Fatalf("the second lease's size: %d", got)
	}
	if got := admitTo(t, f, 2); got.Lease != first.id {
		t.Fatalf("admitted under %s, and the oldest with room is %s", got.Lease, first.id)
	}
	if got := admitTo(t, f, 5); got.Lease != second.id {
		t.Fatalf("admitted under %s, and the next with room is %s", got.Lease, second.id)
	}
}

// TestATopUpIsSizedByTheShardsCharges: what the shard charged over the
// horizon, plus its open holds' needs; charges older than the horizon do
// not count.
func TestATopUpIsSizedByTheShardsCharges(t *testing.T) {
	f, sp := shardFixture(t)
	ctx := context.Background()
	renew := func() {
		t.Helper()
		if err := f.owner.Renew(ctx); err != nil {
			t.Fatal(err)
		}
	}
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 10: nothing charged, nothing held
	renew()                    // the base: nothing charged
	got := admitTo(t, f, 6)
	if _, err := a.Settle(ctx, got.Auth, 6, sum("a")); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(10 * time.Second)
	renew()
	admitTo(t, f, 3) // room 1: an ask, for 6 charged and 3 held
	waitFor(t, "the second ask", func() bool { return len(sp.granted()) == 2 })
	if got := sp.granted()[1].Amount; got != 10 {
		t.Fatalf("a top-up for 6 charged and 3 held, at least 10: %d", got)
	}
	b := f.waitLeases(t, 2)[1]
	// A minute on, the 6 is past the horizon; 40 charged since counts.
	f.clock.advance(70 * time.Second)
	renew()
	big, err := b.Admit(Admission{Estimate: 9, Boot: boot}) // under the lease: no ask
	if err != nil {
		t.Fatal(err)
	}
	if _, err := b.Settle(ctx, big.Auth, 40, sum("b")); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(time.Second)
	renew()
	admitTo(t, f, 1)
	waitFor(t, "the third ask", func() bool { return len(sp.granted()) == 3 })
	if got := sp.granted()[2].Amount; got != 40+3+1 {
		t.Fatalf("a top-up for 40 charged and 4 held: %d", got)
	}
}

// TestAShardWithRoomAsksForNothing: above the low-water mark no top-up is
// asked for, the cooldown past or not.
func TestAShardWithRoomAsksForNothing(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 500
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	f.waitLeases(t, 1)
	f.clock.advance(time.Minute)
	admitTo(t, f, 400) // room 100
	time.Sleep(20 * time.Millisecond)
	if n := len(sp.granted()); n != 1 {
		t.Fatalf("%d asks with room above the low-water mark", n)
	}
	admitTo(t, f, 60) // room 40
	waitFor(t, "the ask below the low-water mark", func() bool { return len(sp.granted()) == 2 })
}

// TestALeaseIdleOrOldCloses: one that has admitted nothing for IdleAfter,
// or is MaxLife old, admits nothing more; its next checkpoint returns its
// free room, and once none of its holds is open it finishes and leaves the
// shard.
func TestALeaseIdleOrOldCloses(t *testing.T) {
	f, sp := shardFixture(t)
	ctx := context.Background()
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0]
	f.clock.advance(4 * time.Minute)
	open := admitTo(t, f, 2)
	f.clock.advance(2 * time.Minute) // 6 minutes old, idle 2
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	zero, err := a.Admit(Admission{Estimate: 0, Boot: boot})
	if err != nil {
		t.Fatalf("an admission under a lease busy 2 minutes ago: %v", err)
	}
	f.clock.advance(5 * time.Minute)
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under an idle lease: %v", err)
	}
	if r := f.lastRecordOf(t, a.id); r.Checkpoint == nil || r.Checkpoint.Return != 8 || r.Checkpoint.Final {
		t.Fatalf("the idle lease's checkpoint: %+v", r)
	}
	if _, err := a.Settle(ctx, open.Auth, 2, sum("open")); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Refund(ctx, zero.Auth); err != nil {
		t.Fatal(err)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the idle lease let go", func() bool {
		_, held := f.owner.Lease(a.id)
		return !held
	})
	if ls := f.leases(key); len(ls) != 0 {
		t.Fatalf("the shard keeps a lease let go: %v", ls)
	}
	// An old lease, though busy.
	f.clock.advance(10 * time.Second)
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	b := f.waitLeases(t, 1)[0]
	for range 13 {
		f.clock.advance(5 * time.Minute)
		admitTo(t, f, 0)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under an hour-old lease: %v", err)
	}
	_ = sp
}

func (f *fixture) lastRecordOf(t *testing.T, lease string) record.Record {
	t.Helper()
	recs := f.log.records(t, lease)
	if len(recs) == 0 {
		t.Fatalf("no record of %s", lease)
	}
	return recs[len(recs)-1]
}

// TestAClosingLeaseIsNoRoom: a lease that admits nothing more adds nothing
// to its shard's room, though it has not returned its free room yet.
func TestAClosingLeaseIsNoRoom(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 500
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0]
	f.clock.advance(time.Minute)
	a.Close()
	if _, err := f.owner.Admit(key, Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request for a shard whose only lease is closing: %v", err)
	}
	waitFor(t, "the ask", func() bool { return len(sp.granted()) == 2 })
}
