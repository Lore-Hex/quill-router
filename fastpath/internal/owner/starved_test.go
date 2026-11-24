package owner

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// lines collects an owner's log lines.
type lines struct {
	mu  sync.Mutex
	got []string
}

func (l *lines) logf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.got = append(l.got, fmt.Sprintf(format, args...))
}

func (l *lines) all() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.got...)
}

// starvedFixture is a shard with one lease of 10 holding 8, so 2 is free,
// whose next ask the store refuses for refusal, reading a headroom of 3: the
// request of 5 that
// follows finds no room and asks, and the store's refusal is in before the
// function returns. The shard asks only for requests no lease takes.
func starvedFixture(t *testing.T, mode Starved, refusal store.Refusal, tweaks ...func(*fakeSpanner)) (*fixture, *fakeSpanner, *Lease, Admitted, *lines) {
	t.Helper()
	logged := &lines{}
	f, sp := shardFixture(t, func(c *Config) { c.StarvedLeases, c.Logf = mode, logged.logf })
	f.owner.cfg.TopUps.LowWater = 0
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0]
	held := admitTo(t, f, 8)
	f.clock.advance(11 * time.Second) // the first ask's cooldown passes
	sp.mu.Lock()
	// Under the request's 5, and with the lease's 2 free enough for it.
	sp.refuseGrants, sp.grantRefusal, sp.grantHeadroom = true, refusal, 3
	for _, tweak := range tweaks {
		tweak(sp)
	}
	sp.mu.Unlock()
	if _, err := f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request the lease has no room for: %v", err)
	}
	waitFor(t, "the refused ask", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return !f.owner.shards[key].asking && len(sp.granted()) == 2
	})
	if got := logged.all(); len(got) != 0 {
		t.Fatalf("logged before any refusal was in: %q", got)
	}
	return f, sp, a, held, logged
}

func starvedAgain(t *testing.T, f *fixture) {
	t.Helper()
	if _, err := f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request the lease has no room for: %v", err)
	}
}

// TestAStarvedShardClosesItsLease: under close, a request no lease has room
// for, just after the store refused the shard's ask for the floor, closes
// the lease too small for it. It admits nothing more, its next checkpoint
// returns exactly its free room and is not final while its hold is open,
// the hold settles, and the lease then finishes and leaves the shard.
func TestAStarvedShardClosesItsLease(t *testing.T) {
	f, sp, a, held, logged := starvedFixture(t, StarvedClose, store.RefusedFloor)
	f.clock.advance(time.Second)
	starvedAgain(t, f)
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under the starved lease: %v", err)
	}
	got := logged.all()
	if len(got) != 1 || !strings.Contains(got[0], "fastpath: owner starved shard workspace=ws-1 region=us-central1 shard=2 "+
		"mode=close leases=1 free=2 need=5 headroom=3 asked=13 refused=\""+string(store.RefusedFloor)+"\" admissions=1") {
		t.Fatalf("the starved line: %q", got)
	}
	ctx := context.Background()
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if r := f.lastRecordOf(t, a.id); r.Checkpoint == nil || r.Checkpoint.Return != 2 || r.Checkpoint.Final ||
		r.Checkpoint.Open != 1 || r.Checkpoint.OpenSum != 8 {
		t.Fatalf("the starved lease's checkpoint: %+v", r)
	}
	if _, err := a.Settle(ctx, held.Auth, 8, sum("held")); err != nil {
		t.Fatalf("settling the hold of a closed lease: %v", err)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the starved lease let go", func() bool {
		_, kept := f.owner.Lease(a.id)
		return !kept
	})
	// A closed lease answers ErrClosing, not ErrNoRoom, so the shard is not
	// starved again, though a fresh refusal stands: nothing counted, and
	// nothing logged past the minute.
	f.clock.advance(starvedLogEvery)
	n := len(sp.granted())
	starvedAgain(t, f) // its ask is refused again
	waitFor(t, "the ask refused again", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return !f.owner.shards[key].asking && len(sp.granted()) == n+1
	})
	starvedAgain(t, f)
	f.owner.mu.Lock()
	counted := f.owner.shards[key].starvedN
	f.owner.mu.Unlock()
	if got := logged.all(); len(got) != 1 || counted != 0 {
		t.Fatalf("starved again: %d counted, logged %q", counted, got)
	}
}

// TestARefusalOfALargerAskIsNotStarvation: an ask is sized for the shard's
// recent spend, so the store may refuse it for the floor though the
// headroom outside leases still covers the request. The synchronous path
// takes that request; nothing is closed or logged. A headroom one short of
// the request's hold is starvation, while the lease's free room of 2 makes
// up the rest; under that the workspace is spent (Python's 402), and
// closing would return too little to help.
func TestARefusalOfALargerAskIsNotStarvation(t *testing.T) {
	for _, c := range []struct {
		headroom int64
		starved  bool
	}{{5, false}, {1 << 40, false}, {4, true}, {3, true}, {2, false}, {-3, false}} {
		f, sp, a, _, logged := starvedFixture(t, StarvedClose, store.RefusedFloor, func(sp *fakeSpanner) {
			sp.grantHeadroom = c.headroom
		})
		_ = sp
		starvedAgain(t, f)
		_, err := a.Admit(Admission{Estimate: 1, Boot: boot})
		if closed := errors.Is(err, ErrClosing); closed != c.starved || (len(logged.all()) == 1) != c.starved {
			t.Fatalf("headroom %d: closed %v (%v), logged %q; want starved %v", c.headroom, closed, err,
				logged.all(), c.starved)
		}
	}
}

// TestCloseDoesNotWaitForTheLine: the line is written at most once a
// minute, but under close every starved request closes its leases: a
// second lease starved within the minute of the first line is closed with
// no second line.
func TestCloseDoesNotWaitForTheLine(t *testing.T) {
	f, sp, a, _, logged := starvedFixture(t, StarvedClose, store.RefusedFloor)
	starvedAgain(t, f) // closes a, the shard's first line
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("the first starved lease: %v", err)
	}
	answered := func(n int) {
		waitFor(t, "the ask answered", func() bool {
			f.owner.mu.Lock()
			defer f.owner.mu.Unlock()
			return !f.owner.shards[key].asking && len(sp.granted()) == n
		})
	}
	sp.mu.Lock()
	sp.refuseGrants = false
	sp.mu.Unlock()
	f.clock.advance(11 * time.Second)
	n := len(sp.granted())
	starvedAgain(t, f) // a is closing: nothing short, and the ask is granted
	answered(n + 1)
	b := f.waitLeases(t, 2)[1]
	admitTo(t, f, 5)
	admitTo(t, f, 5) // b: 13, 3 free
	sp.mu.Lock()
	sp.refuseGrants = true
	sp.mu.Unlock()
	f.clock.advance(11 * time.Second)
	starvedAgain(t, f) // refusal forgotten by the grant: not starved, asks, refused
	answered(n + 2)
	if _, err := b.Admit(Admission{Estimate: 0, Boot: boot}); err != nil {
		t.Fatalf("b before its shard is starved again: %v", err)
	}
	starvedAgain(t, f) // starved within the minute of the line
	if _, err := b.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("the second starved lease, closed with no line: %v", err)
	}
	if got := logged.all(); len(got) != 1 {
		t.Fatalf("lines: %q", got)
	}
}

// TestTheHeadroomIsWeighedAgainstTheHoldNotTheBuffer: Python's
// synchronous hold is the request's estimate, without the owner's buffer,
// so a headroom that covers the estimate is not starvation though it is
// short of the estimate and buffer.
func TestTheHeadroomIsWeighedAgainstTheHoldNotTheBuffer(t *testing.T) {
	for _, c := range []struct {
		headroom int64
		starved  bool
	}{{5, false}, {4, true}} {
		f, _, a, _, _ := starvedFixture(t, StarvedClose, store.RefusedFloor, func(sp *fakeSpanner) {
			sp.grantHeadroom = c.headroom
		})
		f.owner.cfg.Overrun = func(e int64) int64 {
			if e == 5 {
				return 1 // need 6 for the request of 5
			}
			return 0
		}
		starvedAgain(t, f)
		_, err := a.Admit(Admission{Estimate: 1, Boot: boot})
		if closed := errors.Is(err, ErrClosing); closed != c.starved {
			t.Fatalf("headroom %d against a hold of 5 and a need of 6: closed %v (%v)", c.headroom, closed, err)
		}
	}
}

// TestTheAllowanceRefusalStarvesToo: a lease holding the allowance strands
// its room as the floor does.
func TestTheAllowanceRefusalStarvesToo(t *testing.T) {
	f, _, a, _, _ := starvedFixture(t, StarvedClose, store.RefusedAllowance)
	starvedAgain(t, f)
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under the starved lease: %v", err)
	}
}

// TestAStarvedShardIsLoggedOnlyInLogMode: under log the lease keeps
// admitting, the shard is logged once a minute at most, and each line
// counts the starved admissions since the last.
func TestAStarvedShardIsLoggedOnlyInLogMode(t *testing.T) {
	// A headroom of 4 stays starved once the lease's free room is 1.
	f, sp, a, _, logged := starvedFixture(t, StarvedLog, store.RefusedFloor, func(sp *fakeSpanner) {
		sp.grantHeadroom = 4
	})
	starvedAgain(t, f)
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); err != nil {
		t.Fatalf("an admission under a lease log mode keeps: %v", err)
	}
	starvedAgain(t, f) // free 1 now
	got := logged.all()
	if len(got) != 1 || !strings.Contains(got[0], "mode=log leases=1 free=2 need=5") ||
		!strings.HasSuffix(got[0], "admissions=1") {
		t.Fatalf("the first starved line: %q", got)
	}
	// Within the minute, refused again by the asks the requests make, the
	// shard is counted but not logged; after it, logged with the count.
	for range 3 {
		f.clock.advance(11 * time.Second)
		n := len(sp.granted())
		starvedAgain(t, f)
		waitFor(t, "the ask refused", func() bool {
			f.owner.mu.Lock()
			defer f.owner.mu.Unlock()
			return !f.owner.shards[key].asking && len(sp.granted()) == n+1
		})
	}
	f.clock.advance(30 * time.Second)
	starvedAgain(t, f)
	got = logged.all()
	if len(got) != 2 || !strings.Contains(got[1], "free=1 need=5") || !strings.HasSuffix(got[1], "admissions=5") {
		t.Fatalf("the second starved line: %q", got)
	}
}

// TestNoStarvationWithoutAFreshStarvingRefusal: off does nothing; a refusal
// for another reason, or one older than starvedFresh,
// starves nothing (a later grant: TestAGrantForgetsTheRefusal).
func TestNoStarvationWithoutAFreshStarvingRefusal(t *testing.T) {
	t.Run("off", func(t *testing.T) {
		f, _, a, _, logged := starvedFixture(t, StarvedOff, store.RefusedFloor)
		starvedAgain(t, f)
		if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); err != nil || len(logged.all()) != 0 {
			t.Fatalf("off: %v, %q", err, logged.all())
		}
	})
	t.Run("paused", func(t *testing.T) {
		f, _, a, _, logged := starvedFixture(t, StarvedClose, store.RefusedPaused)
		starvedAgain(t, f)
		if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); err != nil || len(logged.all()) != 0 {
			t.Fatalf("a pause's refusal: %v, %q", err, logged.all())
		}
	})
	t.Run("stale", func(t *testing.T) {
		f, _, a, _, logged := starvedFixture(t, StarvedClose, store.RefusedFloor)
		f.clock.advance(starvedFresh + time.Second)
		starvedAgain(t, f)
		if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); err != nil || len(logged.all()) != 0 {
			t.Fatalf("a stale refusal: %v, %q", err, logged.all())
		}
	})
}

// TestAGrantForgetsTheRefusal: an ask whose answer is lost keeps the
// shard's refusal, so the next request is still starved; a granted ask
// forgets it, so the request after is admitted under the new lease and
// nothing is closed.
func TestAGrantForgetsTheRefusal(t *testing.T) {
	f, sp, a, _, logged := starvedFixture(t, StarvedClose, store.RefusedFloor)
	refusal := func() (time.Time, store.Refusal, int64) {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		sh := f.owner.shards[key]
		return sh.refusedAt, sh.refused, sh.refusedAsk
	}
	at, why, asked := refusal()
	if at.IsZero() || why != store.RefusedFloor || asked != 13 {
		t.Fatalf("the refusal kept: %v %q %d", at, why, asked)
	}
	settled := func(n int) {
		waitFor(t, "the ask answered", func() bool {
			f.owner.mu.Lock()
			defer f.owner.mu.Unlock()
			return !f.owner.shards[key].asking && len(sp.granted()) == n
		})
	}
	// A lost answer: the refusal stands.
	sp.mu.Lock()
	sp.refuseGrants, sp.lostGrants = false, 1
	sp.mu.Unlock()
	f.clock.advance(11 * time.Second)
	n := len(sp.granted())
	_, _ = f.owner.Admit(key, Admission{Estimate: 5, Boot: boot}) // starved: closes a, asks, the answer lost
	settled(n + 1)
	if got, _, _ := refusal(); !got.Equal(at) {
		t.Fatalf("a lost answer moved the refusal from %v to %v", at, got)
	}
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("the starved lease after a lost answer: %v", err)
	}
	// The same ask again, granted: the refusal is forgotten.
	f.clock.advance(11 * time.Second)
	_, _ = f.owner.Admit(key, Admission{Estimate: 5, Boot: boot})
	settled(n + 2)
	if got, why, _ := refusal(); !got.IsZero() || why != "" {
		t.Fatalf("a granted ask kept the refusal: %v %q", got, why)
	}
	b := f.waitLeases(t, 2)[1]
	lines := len(logged.all())
	if got := admitTo(t, f, 5); got.Lease != b.id || len(logged.all()) != lines {
		t.Fatalf("admitted under %s, the new lease %s; logged %q", got.Lease, b.id, logged.all())
	}
}

func TestStarvedLeasesFlag(t *testing.T) {
	var s Starved
	if s.String() != "off" {
		t.Fatalf("the zero value: %q", s.String())
	}
	for _, v := range []string{"off", "log", "close"} {
		if err := s.Set(v); err != nil || s.String() != v {
			t.Fatalf("%q: %v, %q", v, err, s.String())
		}
	}
	if err := s.Set("on"); err == nil || s != StarvedClose {
		t.Fatalf("an unknown value: %v, kept %q", err, s)
	}
	f := newFixture(t, 10, nil)
	cfg := f.owner.cfg
	cfg.StarvedLeases = "on"
	if err := cfg.validate(); err == nil {
		t.Fatal("a config with an unknown starved mode validates")
	}
}
