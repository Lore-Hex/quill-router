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
// whose next ask the store refuses for refusal: the request of 5 that
// follows finds no room and asks, and the store's refusal is in before the
// function returns. The shard asks only for requests no lease takes.
func starvedFixture(t *testing.T, mode Starved, refusal store.Refusal) (*fixture, *fakeSpanner, *Lease, Admitted, *lines) {
	t.Helper()
	logged := &lines{}
	f, sp := shardFixture(t, func(c *Config) { c.StarvedLeases, c.Logf = mode, logged.logf })
	f.owner.cfg.TopUps.LowWater = 0
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	a := f.waitLeases(t, 1)[0]
	held := admitTo(t, f, 8)
	f.clock.advance(11 * time.Second) // the first ask's cooldown passes
	sp.mu.Lock()
	sp.refuseGrants, sp.grantRefusal = true, refusal
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
	f, _, a, held, logged := starvedFixture(t, StarvedClose, store.RefusedFloor)
	f.clock.advance(time.Second)
	starvedAgain(t, f)
	if _, err := a.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under the starved lease: %v", err)
	}
	got := logged.all()
	if len(got) != 1 || !strings.Contains(got[0], "fastpath: owner starved shard workspace=ws-1 region=us-central1 shard=2 "+
		"mode=close leases=1 free=2 need=5 refused=\""+string(store.RefusedFloor)+"\" admissions=1") {
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
	// A closed lease answers ErrClosing, not ErrNoRoom, so it is not
	// starved again, and nothing more is logged within the minute.
	starvedAgain(t, f)
	if got := logged.all(); len(got) != 1 {
		t.Fatalf("logged again: %q", got)
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
	f, sp, a, _, logged := starvedFixture(t, StarvedLog, store.RefusedFloor)
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

// TestAGrantForgetsTheRefusal: a granted ask clears the shard's refusal, so
// a request after it is not starved by the refusal before.
func TestAGrantForgetsTheRefusal(t *testing.T) {
	f, sp, _, _, logged := starvedFixture(t, StarvedClose, store.RefusedFloor)
	f.owner.mu.Lock()
	sh := f.owner.shards[key]
	f.owner.noteGrant(sh, "", nil)
	cleared := sh.refusedAt.IsZero() && sh.refused == ""
	f.owner.noteGrant(sh, store.RefusedFloor, nil)
	f.owner.noteGrant(sh, "", errors.New("the answer was lost"))
	kept := !sh.refusedAt.IsZero()
	f.owner.mu.Unlock()
	if !cleared || !kept {
		t.Fatalf("a grant cleared the refusal: %v; a lost answer kept it: %v", cleared, kept)
	}
	// End to end: the store grants the next ask, a lease of 10 is taken,
	// and the request after it is admitted, nothing closed or logged.
	sp.mu.Lock()
	sp.refuseGrants = false
	sp.mu.Unlock()
	f.owner.mu.Lock()
	f.owner.noteGrant(sh, "", nil)
	f.owner.mu.Unlock()
	f.clock.advance(11 * time.Second)
	starvedAgain(t, f)
	b := f.waitLeases(t, 2)[1]
	if got := admitTo(t, f, 5); got.Lease != b.id || len(logged.all()) != 0 {
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
