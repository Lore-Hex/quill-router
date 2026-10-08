package owner

import (
	"context"
	"errors"
	"runtime"
	"strings"
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
// its room is under the low-water mark: one ask at a time, though the
// cooldown has passed, and none within the cooldown of the last, though it
// was answered.
func TestAShardAdmitsUnderItsOldestLeaseWithRoom(t *testing.T) {
	f, sp := shardFixture(t)
	askedAt := func() time.Time {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return f.owner.shards[key].askedAt
	}
	sp.mu.Lock()
	sp.grantGate = make(chan struct{})
	sp.mu.Unlock()
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	first := askedAt()
	f.clock.advance(time.Minute) // the cooldown passes, and the ask is still outstanding
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	if got := askedAt(); !got.Equal(first) {
		t.Fatalf("an ask at %v while the one at %v is outstanding", got, first)
	}
	sp.mu.Lock()
	close(sp.grantGate)
	sp.grantGate = nil
	sp.mu.Unlock()
	a := f.waitLeases(t, 1)[0] // 10
	admitTo(t, f, 4)           // room 6: an ask, the cooldown past
	b := f.waitLeases(t, 2)[1] // 10, its ask answered
	second := askedAt()
	f.clock.advance(5 * time.Second)
	admitTo(t, f, 4) // under a: room 2 and 10, within the cooldown
	if got := askedAt(); !got.Equal(second) {
		t.Fatalf("an ask at %v within the cooldown of the one at %v", got, second)
	}
	if got := admitTo(t, f, 2); got.Lease != a.id {
		t.Fatalf("admitted under %s, and the oldest with room is %s", got.Lease, a.id)
	}
	if got := admitTo(t, f, 5); got.Lease != b.id {
		t.Fatalf("admitted under %s, and the next with room is %s", got.Lease, b.id)
	}
	f.clock.advance(5 * time.Second)
	admitTo(t, f, 1) // the cooldown past
	waitFor(t, "the third ask", func() bool { return len(sp.granted()) == 3 })
}

// TestATopUpIsSizedByTheShardsCharges: what the shard charged within the
// horizon, by when each charge was decided, before any renewal round or
// not, plus its open holds' needs.
func TestATopUpIsSizedByTheShardsCharges(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 1
	ctx := context.Background()
	_, _ = f.owner.Admit(key, Admission{Estimate: 50, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 50: for the request no lease took
	got, err := a.Admit(Admission{Estimate: 50, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Settle(ctx, got.Auth, 6, sum("a")); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(10 * time.Second)
	admitTo(t, f, 41) // room 3: an ask, for 6 charged and 41 held
	waitFor(t, "the second ask", func() bool { return len(sp.granted()) == 2 })
	if got := sp.granted()[1].Amount; got != 6+41 {
		t.Fatalf("a top-up for 6 charged and 41 held: %d", got)
	}
	b := f.waitLeases(t, 2)[1]
	// Seventy seconds on, the 6 is past the horizon; 40 charged since counts.
	f.clock.advance(70 * time.Second)
	big, err := b.Admit(Admission{Estimate: 45, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := b.Settle(ctx, big.Auth, 40, sum("b")); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(time.Second)
	admitTo(t, f, 1)
	waitFor(t, "the third ask", func() bool { return len(sp.granted()) == 3 })
	if got := sp.granted()[2].Amount; got != 40+41+1 {
		t.Fatalf("a top-up for 40 charged and 42 held: %d", got)
	}
}

// TestARequestNoLeaseTakesAsksForOneThatWill: the shard's room is above
// the low-water mark, but no one lease has room for the request; the owner
// asks for a lease that has, within the cooldown's rule.
func TestARequestNoLeaseTakesAsksForOneThatWill(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 100
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	f.waitLeases(t, 1) // 100
	f.clock.advance(10 * time.Second)
	if _, err := f.owner.Admit(key, Admission{Estimate: 101, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request of 101 under a lease of 100: %v", err)
	}
	b := f.waitLeases(t, 2)[1]
	if got := sp.granted()[1].Amount; got != 101 {
		t.Fatalf("the lease asked for the request of 101: %d", got)
	}
	if got := admitTo(t, f, 101); got.Lease != b.id {
		t.Fatalf("the request of 101 under %s, and the lease of 101 is %s", got.Lease, b.id)
	}
}

// TestALostGrantIsAskedForAgainTheSame: an ask whose answer is lost may have
// been granted; the next is the same, lease ID and amount, which the store
// answers with the lease it granted.
func TestALostGrantIsAskedForAgainTheSame(t *testing.T) {
	f, sp := shardFixture(t)
	sp.mu.Lock()
	sp.lostGrants = 1
	sp.mu.Unlock()
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	waitFor(t, "the lost answer", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return len(sp.granted()) == 1 && !f.owner.shards[key].asking
	})
	f.clock.advance(10 * time.Second)
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	l := f.waitLeases(t, 1)[0]
	grants := sp.granted()
	if len(grants) != 2 || grants[1] != grants[0] || l.id != grants[0].LeaseID {
		t.Fatalf("the asks: %+v, the lease taken %s", grants, l.id)
	}
}

// TestStopWaitsForAnAsk: Stop returns only once an ask under way has ended;
// a lease granted after Stop began is not taken.
func TestStopWaitsForAnAsk(t *testing.T) {
	f, sp := shardFixture(t)
	sp.mu.Lock()
	sp.grantGate, sp.deaf = make(chan struct{}), true
	sp.mu.Unlock()
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	waitFor(t, "the ask", func() bool { return len(sp.granted()) == 1 })
	stopped := make(chan struct{})
	go func() {
		f.owner.Stop()
		close(stopped)
	}()
	time.Sleep(20 * time.Millisecond)
	select {
	case <-stopped:
		t.Fatal("Stop returned with an ask under way")
	default:
	}
	sp.mu.Lock()
	close(sp.grantGate)
	sp.grantGate = nil
	sp.mu.Unlock()
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("Stop did not return")
	}
	f.owner.mu.Lock()
	defer f.owner.mu.Unlock()
	if n := len(f.owner.leases); n != 0 || len(f.owner.shards[key].leases) != 0 {
		t.Fatalf("a stopped owner holds %d leases", n)
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
	// Asks only for requests no lease takes, so that the shard's leases are
	// the test's.
	f.owner.cfg.TopUps.LowWater = 0
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
	// An old lease, though busy, refuses at admission, before any renewal
	// round looks.
	f.clock.advance(10 * time.Second)
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	b := f.waitLeases(t, 1)[0]
	for range 14 {
		f.clock.advance(4 * time.Minute)
		if _, err := b.Admit(Admission{Estimate: 0, Boot: boot}); err != nil {
			t.Fatalf("an admission under a busy lease: %v", err)
		}
	}
	f.clock.advance(4 * time.Minute)
	if _, err := b.Admit(Admission{Estimate: 0, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under an hour-old lease: %v", err)
	}
	_ = sp
}

// TestAnIdleLeaseRefusesAtAdmission: an admission after IdleAfter with none
// between finds the lease idle, though no renewal round has looked, and does
// not make it busy again.
func TestAnIdleLeaseRefusesAtAdmission(t *testing.T) {
	f, _ := shardFixture(t)
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0]
	f.clock.advance(5 * time.Minute)
	for range 2 {
		if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
			t.Fatalf("an admission under a lease idle 5 minutes: %v", err)
		}
	}
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
// to its shard's room, though it has not returned its free room yet: a
// shard whose other lease runs low asks for a top-up.
func TestAClosingLeaseIsNoRoom(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 500
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 500
	f.clock.advance(time.Minute)
	a.Close()
	if _, err := f.owner.Admit(key, Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request for a shard whose only lease is closing: %v", err)
	}
	b := f.waitLeases(t, 2)[1] // 500
	f.clock.advance(time.Minute)
	if got := admitTo(t, f, 460); got.Lease != b.id { // b's room 40, a's 500 closing
		t.Fatalf("admitted under %s, and the open lease is %s", got.Lease, b.id)
	}
	waitFor(t, "the ask for the shard's room", func() bool { return len(sp.granted()) == 3 })
}

// TestARetiringOwnerTakesNothingNew: once the owner retires, its leases
// admit nothing more and a request it cannot take is ErrNoRoom, with no
// lease asked for, the cooldown past or not; and a grant asked before it
// retired and answered after is not taken.
func TestARetiringOwnerTakesNothingNew(t *testing.T) {
	f, sp := shardFixture(t)
	askedAt := func() time.Time {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return f.owner.shards[key].askedAt
	}
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 10
	admitTo(t, f, 5)
	f.clock.advance(time.Minute) // the cooldown passes
	sp.mu.Lock()
	sp.grantGate = make(chan struct{})
	sp.mu.Unlock()
	_, _ = f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}) // room 0: an ask, held at the store
	waitFor(t, "the held ask", func() bool { return len(sp.granted()) == 2 })
	f.owner.Retire()
	asked := askedAt()
	f.clock.advance(time.Minute)
	if _, err := f.owner.Admit(key, Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request to a retiring owner: %v", err)
	}
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("a request to a retiring owner's lease: %v", err)
	}
	if got := askedAt(); !got.Equal(asked) {
		t.Fatalf("an ask at %v by a retiring owner, its last at %v", got, asked)
	}
	sp.mu.Lock()
	close(sp.grantGate)
	sp.grantGate = nil
	sp.mu.Unlock()
	waitFor(t, "the held ask's end", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return !f.owner.shards[key].asking
	})
	if ls := f.leases(key); len(ls) != 1 || ls[0] != a {
		t.Fatalf("a retiring owner's leases: %d", len(ls))
	}
}

// lostAsk makes the shard's next ask lose its answer, the grant made, and
// waits for it.
func lostAsk(t *testing.T, f *fixture, sp *fakeSpanner, asks int, admit func()) {
	t.Helper()
	sp.mu.Lock()
	sp.lostGrants = 1
	sp.mu.Unlock()
	admit()
	waitFor(t, "the lost answer", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return len(sp.granted()) == asks && !f.owner.shards[key].asking
	})
}

// TestALostGrantIsAskedForAgainWithRoom: an ask lost below the low-water
// mark is asked again though the shard's room has come back, at its next
// admission, or at a renewal round with none.
func TestALostGrantIsAskedForAgainWithRoom(t *testing.T) {
	for _, round := range []bool{false, true} {
		f, sp := shardFixture(t)
		f.owner.cfg.TopUps.Min = 100
		_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
		a := f.waitLeases(t, 1)[0] // 100
		f.clock.advance(10 * time.Second)
		big, err := a.Admit(Admission{Estimate: 60, Boot: boot})
		if err != nil {
			t.Fatal(err)
		}
		lostAsk(t, f, sp, 2, func() { admitTo(t, f, 1) }) // room 39
		if _, err := a.Refund(context.Background(), big.Auth); err != nil {
			t.Fatal(err)
		}
		f.clock.advance(10 * time.Second)
		if round {
			if err := f.owner.Renew(context.Background()); err != nil {
				t.Fatal(err)
			}
		} else {
			admitTo(t, f, 1) // room 98
		}
		b := f.waitLeases(t, 2)[1]
		if grants := sp.granted(); len(grants) != 3 || grants[2] != grants[1] || b.id != grants[1].LeaseID {
			t.Fatalf("the asks: %+v, the lease taken %s (at a renewal round %v)", grants, b.id, round)
		}
	}
}

// TestALeaseWhoseBuffersTookItsRoomHasNone: an overrun the lease had no
// room for leaves its buffers past its allocation; it adds no room to its
// shard, and the shard's other lease running low asks for a top-up.
func TestALeaseWhoseBuffersTookItsRoomHasNone(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.Overrun = func(e int64) int64 { return e / 4 }
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 10
	first, err := a.Admit(Admission{Estimate: 4, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Admit(Admission{Estimate: 4, Boot: boot}); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Settle(context.Background(), first.Auth, 7, sum("first")); err != nil {
		t.Fatal(err)
	}
	if free := a.Books().Free(); free != -1 {
		t.Fatalf("the lease's free room after the overrun: %d", free)
	}
	f.clock.advance(10 * time.Second)
	if _, err := f.owner.Admit(key, Admission{Estimate: 4, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request under a lease with no room: %v", err)
	}
	b := f.waitLeases(t, 2)[1] // 7 charged, 5 held and buffered, 5 for the request: 17
	f.clock.advance(10 * time.Second)
	if got := admitTo(t, f, 4); got.Lease != b.id { // b's room 12, a's none
		t.Fatalf("admitted under %s, and the lease with room is %s", got.Lease, b.id)
	}
	waitFor(t, "the ask below the low-water mark", func() bool { return len(sp.granted()) == 3 })
}

// TestAnAdmissionChecksIdleAndAgeWhenItHolds: a lease that goes idle, or
// old, while an admission mints its authorization refuses it.
func TestAnAdmissionChecksIdleAndAgeWhenItHolds(t *testing.T) {
	for _, old := range []bool{false, true} {
		f, _ := shardFixture(t)
		_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
		a := f.waitLeases(t, 1)[0]
		if old {
			for range 14 {
				f.clock.advance(4 * time.Minute)
				if _, err := a.Admit(Admission{Estimate: 0, Boot: boot}); err != nil {
					t.Fatalf("an admission under a busy lease: %v", err)
				}
			}
			f.clock.advance(4*time.Minute - time.Millisecond) // an hour less a millisecond old
		} else {
			f.clock.advance(5*time.Minute - time.Millisecond) // idle 5 minutes less a millisecond
		}
		mint := f.owner.cfg.NewAuthorization
		f.owner.cfg.NewAuthorization = func(lease string) (string, error) {
			f.clock.advance(2 * time.Millisecond)
			return mint(lease)
		}
		before := a.Books()
		if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
			t.Fatalf("an admission that ends past the lease's idle or age limit (old %v): %v", old, err)
		}
		if b := a.Books(); b != before {
			t.Fatalf("the refused admission's hold stays: %+v, before %+v", b, before)
		}
	}
}

// TestARequestOfNothingWithNoLeaseAsksForOne, a low-water mark of zero
// and no buffer or not.
func TestARequestOfNothingWithNoLeaseAsksForOne(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.LowWater = 0
	if _, err := f.owner.Admit(key, Admission{Estimate: 0, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request for a shard with no lease: %v", err)
	}
	l := f.waitLeases(t, 1)[0]
	if got := sp.granted()[0].Amount; got != 10 {
		t.Fatalf("the lease asked for a request of nothing: %d", got)
	}
	if got := admitTo(t, f, 0); got.Lease != l.id {
		t.Fatalf("admitted under %s, and the shard's lease is %s", got.Lease, l.id)
	}
}

// TestAnAskIsTimedWhenItIsMade: a top-up that waited on a lease's lock past
// the cooldown is timed when it asks, so the next waits the cooldown after
// it.
func TestAnAskIsTimedWhenItIsMade(t *testing.T) {
	f, sp := shardFixture(t)
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 10, below the low-water mark
	f.clock.advance(10 * time.Second)
	a.mu.Lock()
	read := f.clock.readings()
	asked := make(chan struct{})
	go func() {
		f.owner.topUp(key, false, 0)
		close(asked)
	}()
	// The top-up has read the clock once, before it reads a's room, which
	// waits on a's lock.
	waitFor(t, "the top-up's first reading", func() bool { return f.clock.readings() > read })
	f.clock.advance(20 * time.Second)
	a.mu.Unlock()
	<-asked
	waitFor(t, "the second lease", func() bool { return len(f.leases(key)) == 2 })
	f.owner.mu.Lock()
	askedAt := f.owner.shards[key].askedAt
	f.owner.mu.Unlock()
	if want := start.Add(30 * time.Second); !askedAt.Equal(want) {
		t.Fatalf("the ask is timed at %v, and it was made at %v", askedAt, want)
	}
	f.clock.advance(5 * time.Second)
	admitTo(t, f, 1)
	time.Sleep(20 * time.Millisecond)
	if n := len(sp.granted()); n != 2 {
		t.Fatalf("%d asks, one within the cooldown of the last", n)
	}
}

// TestALateChargeLeavesANewerBucket: a charge decided a horizon before the
// one its bucket holds, which reached the bucket after it, is past the
// horizon: the newer charge stays.
func TestALateChargeLeavesANewerBucket(t *testing.T) {
	var c charges
	c.width = time.Second
	c.add(start.Add(time.Minute), 100)
	c.add(start, 5) // the same bucket, a horizon older
	if got := c.over(start.Add(time.Minute)); got != 100 {
		t.Fatalf("charged within the horizon: %d", got)
	}
}

// TestAnIdleLeaseAddsNoRoom: a lease gone idle since the last renewal round
// admits nothing more, so its room is no shard's, and the shard's busy
// lease running low asks for a top-up.
func TestAnIdleLeaseAddsNoRoom(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 100
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 100
	f.clock.advance(10 * time.Second)
	if _, err := f.owner.Admit(key, Admission{Estimate: 101, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request of 101 under a lease of 100: %v", err)
	}
	f.waitLeases(t, 2) // 101, which admits nothing from here on
	f.clock.advance(4 * time.Minute)
	if got := admitTo(t, f, 1); got.Lease != a.id { // a's room 99, the other's 101
		t.Fatalf("admitted under %s, and the oldest with room is %s", got.Lease, a.id)
	}
	f.clock.advance(time.Minute + time.Second) // the other idle, a busy a minute ago
	if got := admitTo(t, f, 60); got.Lease != a.id {
		t.Fatalf("admitted under %s, and the oldest with room is %s", got.Lease, a.id)
	}
	waitFor(t, "the ask with only a's room of 39", func() bool { return len(sp.granted()) == 3 })
}

// blockedIn reports whether a goroutine is blocked for reason, as the
// runtime names the wait (such as "sync.Mutex.Lock"), in a function of this
// package whose name begins with fn, the first of this package on its stack.
func blockedIn(fn, reason string) bool {
	const frame = "github.com/Lore-Hex/quill-router/fastpath/internal/owner."
	buf := make([]byte, 1<<16)
	for {
		n := runtime.Stack(buf, true)
		if n < len(buf) {
			buf = buf[:n]
			break
		}
		buf = make([]byte, 2*len(buf))
	}
	for _, g := range strings.Split(string(buf), "\n\n") {
		header, frames, _ := strings.Cut(g, "\n")
		if !strings.Contains(header, " ["+reason+"]") && !strings.Contains(header, " ["+reason+",") {
			continue
		}
		for _, line := range strings.Split(frames, "\n") {
			if strings.HasPrefix(line, frame) {
				if strings.HasPrefix(line, frame+fn) {
					return true
				}
				break
			}
		}
	}
	return false
}

// TestAnAskIsTimedOnceTheChargesAreRead: a top-up that waited for the
// shard's charges is timed after the wait, and sized by the charges within
// the horizon then.
func TestAnAskIsTimedOnceTheChargesAreRead(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 1
	_, _ = f.owner.Admit(key, Admission{Estimate: 5, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 5
	got, err := a.Admit(Admission{Estimate: 5, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Settle(context.Background(), got.Auth, 5, sum("a")); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(10 * time.Second)
	f.owner.mu.Lock()
	sh := f.owner.shards[key]
	f.owner.mu.Unlock()
	sh.charges.mu.Lock()
	asked := make(chan struct{})
	go func() {
		f.owner.topUp(key, true, 1)
		close(asked)
	}()
	// Seen waiting for the charges' lock, the top-up has read every room.
	waitFor(t, "the top-up at the charges' lock", func() bool { return blockedIn("(*charges).", "sync.Mutex.Lock") })
	f.clock.advance(70 * time.Second) // the charge of 5 is past the horizon
	sh.charges.mu.Unlock()
	<-asked
	f.owner.mu.Lock()
	askedAt := sh.askedAt
	f.owner.mu.Unlock()
	if want := start.Add(80 * time.Second); !askedAt.Equal(want) {
		t.Fatalf("the ask is timed at %v, and it was made at %v", askedAt, want)
	}
	waitFor(t, "the second ask", func() bool { return len(sp.granted()) == 2 })
	if got := sp.granted()[1].Amount; got != 1 {
		t.Fatalf("an ask for the request of 1, with nothing charged within the horizon: %d", got)
	}
}

// TestALeaseWhosePublishesFailAddsNoRoom: it admits nothing until a publish
// succeeds again, so the shard's other lease running low asks for a
// top-up; its holds still size it.
func TestALeaseWhosePublishesFailAddsNoRoom(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 100
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0] // 100
	f.clock.advance(10 * time.Second)
	if _, err := f.owner.Admit(key, Admission{Estimate: 101, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request of 101 under a lease of 100: %v", err)
	}
	b := f.waitLeases(t, 2)[1] // 101
	f.owner.cfg.TopUps.Min = 1
	f.owner.cfg.Overrun = func(e int64) int64 { return e / 4 }
	if _, err := a.Admit(Admission{Estimate: 8, Boot: boot}); err != nil { // held through: 8 and a buffer of 2
		t.Fatal(err)
	}
	settled, err := a.Admit(Admission{Estimate: 7, Boot: boot})
	if err != nil {
		t.Fatal(err)
	}
	f.log.fail(a.id, 1_000_000)
	go func() { _, _ = a.Settle(context.Background(), settled.Auth, 1, sum("a")) }()
	waitFor(t, "a's publishes failing", func() bool {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.failed
	})
	f.clock.advance(10 * time.Second)
	if got := admitTo(t, f, 60); got.Lease != b.id { // b's room 26, a's none
		t.Fatalf("admitted under %s, and the lease whose publishes work is %s", got.Lease, b.id)
	}
	waitFor(t, "the ask with only b's room of 26", func() bool { return len(sp.granted()) == 3 })
	// 1 charged; a's hold of 8 and its buffer of 2; b's 60 and 15.
	if got := sp.granted()[2].Amount; got != 1+10+75 {
		t.Fatalf("a top-up for 1 charged and 85 held and buffered, a's 10 among them: %d", got)
	}
}

// TestAHorizonIsWholeBuckets: top-ups whose horizon the sixty buckets
// cannot cover exactly are refused.
func TestAHorizonIsWholeBuckets(t *testing.T) {
	ok := TopUps{LowWater: 50, Cooldown: time.Second, Horizon: time.Minute, Min: 1, Max: 10, IdleAfter: time.Minute,
		MaxLife: time.Hour}
	if err := ok.validate(); err != nil {
		t.Fatal(err)
	}
	for _, h := range []time.Duration{30, 61, time.Minute + 1} {
		bad := ok
		bad.Horizon = h
		if err := bad.validate(); err == nil {
			t.Errorf("a horizon of %v", h)
		}
	}
}

// TestATopUpCountsTheHoldsBuffers: a top-up's size has room for the open
// holds' buffers too.
func TestATopUpCountsTheHoldsBuffers(t *testing.T) {
	f, sp := shardFixture(t)
	f.owner.cfg.TopUps.Min = 1
	f.owner.cfg.Overrun = func(e int64) int64 { return e / 4 }
	_, _ = f.owner.Admit(key, Admission{Estimate: 40, Boot: boot})
	f.waitLeases(t, 1) // 50: the request's 40 and its buffer of 10
	f.clock.advance(10 * time.Second)
	admitTo(t, f, 40) // room 0: an ask for 40 held and a buffer of 10
	waitFor(t, "the second ask", func() bool { return len(sp.granted()) == 2 })
	if got := sp.granted()[1].Amount; got != 50 {
		t.Fatalf("a top-up for 40 held and a buffer of 10: %d", got)
	}
}
