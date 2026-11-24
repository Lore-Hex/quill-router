package gwstate

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"slices"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// lookupWithin runs LookupWithin on its own goroutine, so a test can act
// while it waits; the answer comes on the channel.
func lookupWithin(cache *Cache, hash string, now time.Time, wait time.Duration) <-chan Read {
	out := make(chan Read, 1)
	go func() { out <- cache.LookupWithin(hash, now, wait) }()
	return out
}

func answer(t *testing.T, got <-chan Read) Read {
	t.Helper()
	select {
	case r := <-got:
		return r
	case <-time.After(5 * time.Second):
		t.Fatal("LookupWithin did not return")
	}
	return Read{}
}

// TestLookupWithinDecidesAMissOnTheReadItWaitsFor: a key never seen is
// read, with its workspace's credit rows after it, while the lookup waits;
// what it returns is usable, one read of each made, and the miss is named
// for the key's first sighting.
func TestLookupWithinDecidesAMissOnTheReadItWaitsFor(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	gate, started := make(chan struct{}), make(chan string, 4)
	r.gate, r.started = gate, started
	cache := newTestCache(t, r, c)
	got := lookupWithin(cache, "lh", c.Now(), 5*time.Second)
	receive(t, started)
	close(gate)
	read := answer(t, got)
	if !read.Usable() || read.KeyMiss != MissNew || read.Wait != WaitFilled || read.Waited <= 0 ||
		read.Facts.Key.WorkspaceID != "ws" || read.CreditState != funded {
		t.Fatalf("the read-through: %+v", read)
	}
	if r.reads("lh") != 1 || r.creditReads("ws") != 1 {
		t.Fatalf("reads: %d of the key, %d of its credit rows", r.reads("lh"), r.creditReads("ws"))
	}
	if got := cache.Counters(); got.KeyCold != 1 || got.CreditCold != 0 || got.KeyHits != 0 {
		t.Fatalf("the counters count the first look alone: %+v", got)
	}
	// No wait is Lookup.
	if got := cache.LookupWithin("other", c.Now(), 0); got.Key != Cold || got.Wait != "" || got.Waited != 0 {
		t.Fatalf("no wait: %+v", got)
	}
	cache.settle()
	// A fresh entry waits for nothing, and is Lookup's answer.
	if got := cache.LookupWithin("lh", c.Now(), time.Hour); !got.Usable() || got.Wait != "" || got.KeyMiss != "" ||
		got.Waited != 0 {
		t.Fatalf("a fresh hit: %+v", got)
	}
}

// TestLookupWithinPassesWhenItsWaitEnds: a read slower than the wait leaves
// the request cold (it passes, cache.key), the wait no longer than asked;
// the read goes on and fills the entry for the next request.
func TestLookupWithinPassesWhenItsWaitEnds(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	gate := make(chan struct{})
	r.gate = gate
	cache := newTestCache(t, r, c)
	const wait = 30 * time.Millisecond
	began := time.Now()
	read := cache.LookupWithin("lh", c.Now(), wait)
	took := time.Since(began)
	if read.Key != Cold || read.Usable() || read.Wait != WaitTimedOut || read.KeyMiss != MissNew ||
		read.Waited < wait || took > wait+2*time.Second {
		t.Fatalf("a wait that ended first, after %v: %+v", took, read)
	}
	// A stale entry whose two reads, the key's and the credit entry's, are
	// slow: the stale facts are returned, never usable, once the wait ends,
	// not once the second read gives up at ReadTimeout (2 s).
	close(gate)
	cache.settle()
	gate2 := make(chan struct{})
	r.set(func(r *fakeReader) { r.gate = gate2 })
	began = time.Now()
	read = cache.LookupWithin("lh", c.advance(2*time.Minute), wait)
	took = time.Since(began)
	if read.Key != Stale || read.Credit != Stale || read.Usable() || read.Wait != WaitTimedOut ||
		read.KeyMiss != MissIdle || took >= time.Second {
		t.Fatalf("a stale entry's two slow reads, after %v: %+v", took, read)
	}
	close(gate2)
	cache.settle()
	if got := cache.Lookup("lh", c.Now()); !got.Usable() {
		t.Fatalf("filled behind the wait: %+v", got)
	}
}

// scriptedClock is LookupWithin's monotonic clock in a test: it reads base
// until its jump'th reading, and base plus two hours from then on, so a
// deadline passes at a reading the test chooses whatever the scheduler does.
type scriptedClock struct {
	base  time.Time
	jump  int64
	reads atomic.Int64
}

func (s *scriptedClock) now() time.Time {
	if s.reads.Add(1) >= s.jump {
		return s.base.Add(2 * time.Hour)
	}
	return s.base
}

// TestLookupWithinTakesNothingPastItsDeadline: a read that answers once the
// wait's deadline has passed is not taken, though the wait saw it done (a
// select with the read done and the timer fired may take either): the
// request gets the first look, timed out, and passes. The deadline is
// checked once the reads are done and again after the last look; a read
// done before it is taken. The wait itself is an hour, so its timer never
// decides.
func TestLookupWithinTakesNothingPastItsDeadline(t *testing.T) {
	for name, tc := range map[string]struct {
		jump int64
		want string
	}{
		// Readings: 1 the wait's start, 2 once the reads are done, 3 after
		// the last look.
		"past it once the reads are done": {2, WaitTimedOut},
		"past it after the last look":     {3, WaitTimedOut},
		"before it":                       {100, WaitFilled},
	} {
		r, c := newFakeReader(), start()
		r.keys["lh"] = keyContext("lh", "ws", "")
		r.credits["ws"] = funded
		cache := newTestCache(t, r, c)
		clock := &scriptedClock{base: time.Now(), jump: tc.jump}
		cache.monotonic = clock.now
		got := cache.LookupWithin("lh", c.Now(), time.Hour)
		if got.Wait != tc.want || got.Usable() != (tc.want == WaitFilled) || got.KeyMiss != MissNew {
			t.Fatalf("%s: %+v", name, got)
		}
		if tc.want == WaitTimedOut && (got.Key != Cold || got.Waited != 2*time.Hour) {
			t.Fatalf("%s: not the first look: %+v", name, got)
		}
		cache.settle()
		// The read it did not take filled the entry for the next request.
		if got := cache.Lookup("lh", c.Now()); !got.Usable() {
			t.Fatalf("%s: not filled behind the wait: %+v", name, got)
		}
	}
}

// TestLookupWithinJudgesFreshnessAtTheRequestsInstant: what the wait
// returns is judged at the request's instant, as Lookup judges it, never at
// the cache's clock. A request whose instant is past the maximum age of
// every read the cache can make (its instant ahead of the cache's clock)
// gets a stale entry from LookupWithin exactly as from Lookup, though at
// the clock's instant the entry would be fresh: for a key held and for one
// never seen.
func TestLookupWithinJudgesFreshnessAtTheRequestsInstant(t *testing.T) {
	setup := func() (*fakeReader, *clock, *Cache) {
		r, c := newFakeReader(), start()
		r.keys["held"], r.keys["new"] = keyContext("held", "ws", ""), keyContext("new", "ws2", "")
		r.credits["ws"], r.credits["ws2"] = funded, funded
		cache := newTestCache(t, r, c)
		cache.Lookup("held", c.Now())
		cache.settle()
		return r, c, cache
	}
	_, c, plain := setup()
	_, _, within := setup()
	ahead := c.Now().Add(DefaultMaxAge + time.Second)
	for _, hash := range []string{"held", "new"} {
		// Lookup, its read landed: what a request at ahead is given.
		plain.Lookup(hash, ahead)
		plain.settle()
		want := plain.Lookup(hash, ahead)
		got := within.LookupWithin(hash, ahead, 5*time.Second)
		if want.Key != Stale || want.Usable() || got.Key != want.Key || got.Credit != want.Credit || got.Usable() ||
			got.Wait != WaitUnfilled {
			t.Fatalf("%s at an instant past the reads' maximum age: LookupWithin %+v, Lookup %+v", hash, got, want)
		}
		within.settle()
		// At the cache's own instant both are fresh.
		if at := within.LookupWithin(hash, c.Now(), 5*time.Second); !at.Usable() {
			t.Fatalf("%s at the clock's instant: %+v", hash, at)
		}
	}
}

// TestACreditReadTimeoutIsCounted: a credit read failing at its deadline is
// a read timeout, as a key read's is.
func TestACreditReadTimeoutIsCounted(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"], r.credits["ws"] = keyContext("lh", "ws", ""), funded
	r.creditErr = status.Error(codes.DeadlineExceeded, "deadline")
	cache := newTestCache(t, r, c)
	cache.Lookup("lh", c.Now())
	cache.settle()
	if got := cache.Counters(); got.ReadFailures != 1 || got.ReadTimeouts != 1 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestLookupWithinReadsARevokedKeyAsRevoked: a key disabled, or its
// workspace paused, in the store while its entry aged is read as it is now:
// the wait returns the change, never the facts held before it.
func TestLookupWithinReadsARevokedKeyAsRevoked(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.keys["lh2"] = keyContext("lh2", "ws2", "")
	r.credits["ws"], r.credits["ws2"] = funded, funded
	cache := newTestCache(t, r, c)
	for _, hash := range []string{"lh", "lh2"} {
		if got := cache.LookupWithin(hash, c.Now(), 5*time.Second); !got.Usable() {
			t.Fatalf("%s: %+v", hash, got)
		}
	}
	r.set(func(r *fakeReader) {
		r.keys["lh"] = keyContext("lh", "ws", `,"disabled":true`)
		r.credits["ws2"] = store.CreditState{Shards: 1, Paused: true, Tier: 3, Refused: store.RefusedPaused}
	})
	c.advance(2 * time.Minute)
	got := cache.LookupWithin("lh", c.Now(), 5*time.Second)
	if got.Key != Fresh || got.Facts.Key.Eligible || got.Facts.Key.Reason != ReasonKeyDisabled || got.Usable() ||
		got.Credit != Unneeded || got.Wait != WaitFilled || got.KeyMiss != MissIdle {
		t.Fatalf("a disabled key: %+v", got)
	}
	got = cache.LookupWithin("lh2", c.Now(), 5*time.Second)
	if got.Key != Fresh || got.Credit != Fresh || !got.CreditState.Paused || got.Wait != WaitFilled {
		t.Fatalf("a paused workspace: %+v", got)
	}
}

// TestLookupWithinReadsALookupHashWithNoKey: no key is a fresh negative
// entry once read (Python's 401, key.missing), and read again, with the
// miss named, once it lapses.
func TestLookupWithinReadsALookupHashWithNoKey(t *testing.T) {
	r, c := newFakeReader(), start()
	cache := newTestCache(t, r, c)
	got := cache.LookupWithin("nobody", c.Now(), 5*time.Second)
	if got.Key != Fresh || got.Found || got.Credit != Unneeded || got.Usable() || got.Wait != WaitFilled ||
		got.KeyMiss != MissNew {
		t.Fatalf("no key: %+v", got)
	}
	if got := cache.LookupWithin("nobody", c.Now().Add(DefaultNegativeFor), 5*time.Second); got.Key != Fresh ||
		got.Found || got.Wait != "" || r.reads("nobody") != 1 {
		t.Fatalf("no key, within its time: %+v", got)
	}
	got = cache.LookupWithin("nobody", c.advance(DefaultNegativeFor+time.Nanosecond), 5*time.Second)
	if got.Key != Fresh || got.Found || got.KeyMiss != MissNegative || got.Wait != WaitFilled || r.reads("nobody") != 2 {
		t.Fatalf("no key, past its time: %+v", got)
	}
}

// TestLookupWithinAnEvictionWhileItWaitsLeavesItCold: Python's refusal of
// the key while the request waits discards the read, which may have read
// before the change Python saw: the request is not decided.
func TestLookupWithinAnEvictionWhileItWaitsLeavesItCold(t *testing.T) {
	for name, evict := range map[string]func(*Cache){
		"key":       func(c *Cache) { c.Evict("lh") },
		"workspace": func(c *Cache) { c.EvictWorkspace("ws") },
	} {
		r, c := newFakeReader(), start()
		r.keys["lh"] = keyContext("lh", "ws", "")
		r.credits["ws"] = funded
		gate, started := make(chan struct{}), make(chan string, 4)
		r.gate, r.started = gate, started
		cache := newTestCache(t, r, c)
		got := lookupWithin(cache, "lh", c.Now(), 5*time.Second)
		receive(t, started)
		evict(cache)
		close(gate)
		if read := answer(t, got); read.Key != Cold || read.Usable() || read.Wait != WaitUnfilled {
			t.Fatalf("%s evicted during the wait: %+v", name, read)
		}
		cache.settle()
	}
}

// TestLookupWithinAFailedReadLeavesItUnfilled: a read that fails within the
// wait leaves the entry as it was: cold, or stale and its facts never
// usable.
func TestLookupWithinAFailedReadLeavesItUnfilled(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c)
	cache.LookupWithin("lh", c.Now(), 5*time.Second)
	r.set(func(r *fakeReader) { r.keyErr = errors.New("spanner unavailable") })
	got := cache.LookupWithin("lh", c.advance(DefaultMaxAge+time.Second), 5*time.Second)
	if got.Key != Stale || got.Usable() || got.Wait != WaitUnfilled || got.KeyMiss != MissUnrefreshed {
		t.Fatalf("a failed read of a stale entry: %+v", got)
	}
	if got := cache.LookupWithin("other", c.Now(), 5*time.Second); got.Key != Cold || got.Wait != WaitUnfilled {
		t.Fatalf("a failed read of a new key: %+v", got)
	}
	// The key read again, its credit read failing: the key is fresh, the
	// credit entry still stale, and the wait unfilled.
	r.set(func(r *fakeReader) { r.keyErr, r.creditErr = nil, errors.New("spanner unavailable") })
	got = cache.LookupWithin("lh", c.advance(DefaultMaxAge+time.Second), 5*time.Second)
	if got.Key != Fresh || got.Credit != Stale || got.Usable() || got.Wait != WaitUnfilled {
		t.Fatalf("a failed credit read: %+v", got)
	}
}

// TestLookupWithinWithEveryReadBusyWaitsForNothing: a miss that finds no
// read free passes at once, as Lookup's does.
func TestLookupWithinWithEveryReadBusyWaitsForNothing(t *testing.T) {
	r, c := newFakeReader(), start()
	gate, started := make(chan struct{}), make(chan string, 4)
	r.gate, r.started = gate, started
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Reads = 1 })
	cache.Lookup("a", c.Now())
	receive(t, started)
	began := time.Now()
	got := cache.LookupWithin("b", c.Now(), 5*time.Second)
	if got.Key != Cold || got.Wait != "" || got.Waited != 0 || time.Since(began) > time.Second {
		t.Fatalf("every read busy: %+v", got)
	}
	close(gate)
	cache.settle()
	if r.reads("b") != 0 || cache.Counters().Dropped != 1 {
		t.Fatalf("b read %d times, %+v", r.reads("b"), cache.Counters())
	}
}

// TestLookupWithinWaitsForBothReads: a key and a credit entry both stale
// are read at once, and the lookup waits for both before it is usable; one
// lookup waiting holds up neither another key's lookup nor its own key's.
func TestLookupWithinWaitsForBothReads(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.keys["hot"] = keyContext("hot", "ws-hot", "")
	r.credits["ws"], r.credits["ws-hot"] = funded, funded
	cache := newTestCache(t, r, c)
	cache.LookupWithin("lh", c.Now(), 5*time.Second)
	c.advance(DefaultMaxAge + time.Second)
	cache.LookupWithin("hot", c.Now(), 5*time.Second)
	gate, started := make(chan struct{}), make(chan string, 4)
	r.set(func(r *fakeReader) { r.gate, r.started = gate, started })
	got := lookupWithin(cache, "lh", c.Now(), 5*time.Second)
	receive(t, started)
	receive(t, started)
	// While it waits: another key's fresh lookup, and its own key's plain
	// lookup, return at once.
	if other := cache.Lookup("hot", c.Now()); !other.Usable() {
		t.Fatalf("another key while one waits: %+v", other)
	}
	if same := cache.Lookup("lh", c.Now()); same.Key != Stale || same.Credit != Stale {
		t.Fatalf("its own key while it waits: %+v", same)
	}
	close(gate)
	if read := answer(t, got); !read.Usable() || read.Wait != WaitFilled || read.KeyMiss != MissUnrefreshed {
		t.Fatalf("both stale: %+v", read)
	}
	if r.reads("lh") != 2 || r.creditReads("ws") != 2 {
		t.Fatalf("reads: %d %d", r.reads("lh"), r.creditReads("ws"))
	}
}

// TestTheMissIsNamed: each way a key's entry is cold or stale at a lookup
// has its name (Read.KeyMiss), so the decide line can count the requests
// passed as cache.key by cause.
func TestTheMissIsNamed(t *testing.T) {
	look := func(t *testing.T, cache *Cache, hash string, now time.Time, want string) {
		t.Helper()
		if got := cache.Lookup(hash, now); got.KeyMiss != want {
			t.Fatalf("%s: %q, want %q (%+v)", hash, got.KeyMiss, want, got)
		}
		cache.settle()
	}
	t.Run("cold", func(t *testing.T) {
		r, c := newFakeReader(), start()
		for hash, ws := range map[string]string{"a": "ws", "b": "ws", "c": "other", "d": "d", "e": "e"} {
			r.keys[hash], r.credits[ws] = keyContext(hash, ws, ""), funded
		}
		cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Size = 3 })
		for _, hash := range []string{"a", "b", "c"} {
			look(t, cache, hash, c.Now(), MissNew)
		}
		look(t, cache, "a", c.Now(), "")
		cache.Evict("a")
		look(t, cache, "a", c.Now(), MissEvicted)
		look(t, cache, "b", c.Now(), MissEvicted) // its workspace's
		// The size bound (3): c, used longest ago, goes.
		look(t, cache, "d", c.Now(), MissNew)
		look(t, cache, "c", c.Now(), MissDisplaced)
		// A key Python refused that the cache never held.
		cache.Evict("e")
		look(t, cache, "e", c.Now(), MissEvicted)
		look(t, cache, "nobody", c.Now(), MissNew)
		look(t, cache, "nobody", c.Now(), "")
		look(t, cache, "nobody", c.advance(DefaultNegativeFor+time.Second), MissNegative)
	})
	t.Run("cold after a failed read", func(t *testing.T) {
		r, c := newFakeReader(), start()
		r.keyErr = errors.New("spanner unavailable")
		cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.ReadTimeout = 20 * time.Millisecond })
		look(t, cache, "lh", c.Now(), MissNew)
		look(t, cache, "lh", c.Now(), MissReadFailed)
		r.set(func(r *fakeReader) { r.keyErr, r.gate = nil, make(chan struct{}) })
		look(t, cache, "lh", c.Now(), MissReadFailed)
		look(t, cache, "lh", c.Now(), MissReadTimeout)
		if got := cache.Counters(); got.ReadFailures != 4 || got.ReadTimeouts != 2 {
			t.Fatalf("the counters: %+v", got)
		}
	})
	t.Run("stale", func(t *testing.T) {
		r, c := newFakeReader(), start()
		r.keys["lh"], r.credits["ws"] = keyContext("lh", "ws", ""), funded
		cache := newTestCache(t, r, c)
		look(t, cache, "lh", c.Now(), MissNew)
		// Used every 10 s, no refresh run: stale while in use.
		look(t, cache, "lh", c.advance(10*time.Second), "")
		look(t, cache, "lh", c.advance(10*time.Second), MissUnrefreshed)
		// Refreshed while in use, then unused for two minutes: idle.
		for range 3 {
			c.advance(DefaultRefreshEvery)
			cache.Refresh(context.Background())
		}
		for range 24 {
			c.advance(DefaultRefreshEvery)
			cache.Refresh(context.Background())
		}
		look(t, cache, "lh", c.Now(), MissIdle)
		// In use, its refreshes failing: named for the failure, until a read
		// answers.
		r.set(func(r *fakeReader) { r.keyErr = errors.New("spanner unavailable") })
		for range 4 {
			c.advance(DefaultRefreshEvery)
			cache.Lookup("lh", c.Now())
			cache.Refresh(context.Background())
		}
		look(t, cache, "lh", c.Now(), MissRefreshFailed)
		r.set(func(r *fakeReader) { r.keyErr = status.Error(codes.DeadlineExceeded, "deadline") })
		look(t, cache, "lh", c.advance(time.Second), MissRefreshFailed)
		look(t, cache, "lh", c.advance(time.Second), MissRefreshTimeout)
		r.set(func(r *fakeReader) { r.keyErr = nil })
		look(t, cache, "lh", c.advance(time.Second), MissRefreshTimeout)
		look(t, cache, "lh", c.advance(time.Second), "")
	})
}

// TestABurstAfterIdleIsNamedIdle: the requests of a burst that find an
// idle key's entry stale are all named idle, though the first one's use
// makes the entry look in use to the next; a read's answer ends the name.
func TestABurstAfterIdleIsNamedIdle(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"], r.credits["ws"] = keyContext("lh", "ws", ""), funded
	cache := newTestCache(t, r, c)
	cache.Lookup("lh", c.Now())
	cache.settle()
	gate := make(chan struct{})
	r.set(func(r *fakeReader) { r.gate = gate })
	c.advance(5 * time.Minute)
	for i := range 3 {
		if got := cache.Lookup("lh", c.advance(time.Millisecond)); got.Key != Stale || got.KeyMiss != MissIdle {
			t.Fatalf("lookup %d of the burst: %+v", i, got)
		}
	}
	close(gate)
	cache.settle()
	if got := cache.Lookup("lh", c.Now()); got.Key != Fresh || got.KeyMiss != "" {
		t.Fatalf("after the read: %+v", got)
	}
	// Stale again while in use (no refresh run): named for that.
	if got := cache.Lookup("lh", c.advance(DefaultMaxAge+time.Second)); got.KeyMiss != MissUnrefreshed {
		t.Fatalf("stale in use: %+v", got)
	}
	cache.settle()
}

// TestWhatIsRememberedIsBounded: the lookup hashes the cache dropped are
// remembered up to Size, the oldest forgotten (its next miss is new again),
// and a hash filled again is forgotten.
func TestWhatIsRememberedIsBounded(t *testing.T) {
	r, c := newFakeReader(), start()
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Size = 2 })
	for i := range 5 {
		cache.Evict(fmt.Sprint("k", i))
	}
	cache.mu.Lock()
	held := len(cache.gone)
	cache.mu.Unlock()
	if held != 2 {
		t.Fatalf("%d remembered, size 2", held)
	}
	if got := cache.Lookup("k0", c.Now()); got.KeyMiss != MissNew {
		t.Fatalf("the oldest: %+v", got)
	}
	if got := cache.Lookup("k4", c.Now()); got.KeyMiss != MissEvicted {
		t.Fatalf("the newest: %+v", got)
	}
	cache.settle()
	cache.mu.Lock()
	_, kept := cache.gone["k4"]
	cache.mu.Unlock()
	if kept {
		t.Fatal("a filled hash still remembered")
	}
}

// TestTheReadThroughDecidesTheSparseKeys replays the traffic shape that
// passes cache.key: keys whose requests reach the node minutes apart, beside
// one used every few seconds, over two hours of the cache's clock with a
// refresh every 5 s. With Lookup, every sparse request finds its entry stale
// (unused for over UsedWithin, so not refreshed) or cold, and would pass;
// with LookupWithin, each is decided on the read it waits for, and the
// requests that pass fall to none. The reads made are the same: a miss reads
// its key either way.
func TestTheReadThroughDecidesTheSparseKeys(t *testing.T) {
	type request struct {
		at   time.Duration
		hash string
	}
	rng := rand.New(rand.NewPCG(1, 2))
	var requests []request
	for k := range 20 {
		hash := fmt.Sprint("sparse", k)
		for at := time.Duration(rng.Int64N(int64(5 * time.Minute))); at < 2*time.Hour; {
			requests = append(requests, request{at, hash})
			// Two to five minutes apart: past the minute and 15 s an entry
			// stays fresh after its last use.
			at += 2*time.Minute + time.Duration(rng.Int64N(int64(3*time.Minute)))
		}
	}
	for at := time.Duration(0); at < 2*time.Hour; at += 3 * time.Second {
		requests = append(requests, request{at, "dense"})
	}
	replay := func(wait time.Duration) (passed, sparse, dense int, reads int64) {
		r, c := newFakeReader(), start()
		for i := range 20 {
			hash := fmt.Sprint("sparse", i)
			r.keys[hash] = keyContext(hash, fmt.Sprint("ws", i), "")
			r.credits[fmt.Sprint("ws", i)] = funded
		}
		r.keys["dense"], r.credits["ws-dense"] = keyContext("dense", "ws-dense", ""), funded
		cache := newTestCache(t, r, c)
		origin := c.Now()
		// In time order, a refresh every 5 s between them.
		sorted := slices.Clone(requests)
		slices.SortStableFunc(sorted, func(a, b request) int { return cmp.Compare(a.at, b.at) })
		next := DefaultRefreshEvery
		for _, q := range sorted {
			for next <= q.at {
				c.advance(origin.Add(next).Sub(c.Now()))
				cache.Refresh(context.Background())
				next += DefaultRefreshEvery
			}
			c.advance(origin.Add(q.at).Sub(c.Now()))
			var got Read
			if wait > 0 {
				got = cache.LookupWithin(q.hash, c.Now(), wait)
			} else {
				got = cache.Lookup(q.hash, c.Now())
			}
			// A request's read lands before the next request, as one strong
			// read does long before a sparse key's next request.
			cache.settle()
			if !got.Usable() {
				passed++
				if q.hash == "dense" {
					dense++
				} else {
					sparse++
				}
			}
		}
		return passed, sparse, dense, cache.Counters().Reads
	}
	sparseRequests := len(requests) - int((2*time.Hour)/(3*time.Second))
	passedLookup, sparseLookup, denseLookup, readsLookup := replay(0)
	passedWithin, _, _, readsWithin := replay(100 * time.Millisecond)
	t.Logf("%d requests (%d sparse): Lookup passed %d (sparse %d, dense %d), %d reads; "+
		"LookupWithin passed %d, %d reads", len(requests), sparseRequests, passedLookup, sparseLookup, denseLookup,
		readsLookup, passedWithin, readsWithin)
	// Every sparse request is a miss under Lookup; the dense key's first
	// alone.
	if sparseLookup != sparseRequests || denseLookup != 1 {
		t.Fatalf("Lookup: %d of %d sparse requests passed, %d of the dense key's", sparseLookup, sparseRequests,
			denseLookup)
	}
	if passedWithin != 0 || readsWithin != readsLookup {
		t.Fatalf("LookupWithin: %d passed, %d reads against Lookup's %d", passedWithin, readsWithin, readsLookup)
	}
}
