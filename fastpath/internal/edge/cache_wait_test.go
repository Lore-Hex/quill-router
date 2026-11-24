package edge

import (
	"crypto/ed25519"
	"crypto/rand"
	"maps"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
)

// movingClock is a cache's clock a test moves.
type movingClock struct {
	mu  sync.Mutex
	now time.Time
}

func (m *movingClock) Now() time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.now
}

func (m *movingClock) set(t time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.now = t
}

// waitingDecide is the plain answered case's decide edge over a cache of
// the world's, waiting wait for a cold or stale entry's read.
func waitingDecide(t *testing.T, world func(authorizeCase) *fakeWorld, wait time.Duration) (*decideEdge,
	authorizeCase, *gwstate.Cache, *movingClock) {
	t.Helper()
	h, c := baseDecide(t)
	clock := &movingClock{now: c.at()}
	cache, err := gwstate.NewCache(gwstate.CacheConfig{Reader: world(c), Clock: clock.Now})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cache.Close)
	h.edge.decider.cfg.Cache, h.edge.decider.cfg.CacheWait = cache, wait
	return h, c, cache, clock
}

func plainWorld(c authorizeCase) *fakeWorld { return &fakeWorld{c: c} }

// TestTheReadThroughAnswersAColdKey: a key the edge has never seen passes
// cache.key with no wait; with one, the edge waits for the read the miss
// starts, and answers the request itself on what it read. Either way the
// decide line names the miss.
func TestTheReadThroughAnswersAColdKey(t *testing.T) {
	for _, tc := range []struct {
		wait     time.Duration
		status   int
		passed   map[string]int64
		waited   map[string]int64
		answered int64
	}{
		{0, http.StatusTeapot, map[string]int64{PassCacheKey: 1}, map[string]int64{}, 0},
		{MaxCacheWait, http.StatusOK, map[string]int64{}, map[string]int64{gwstate.WaitFilled: 1}, 1},
	} {
		h, c, _, _ := waitingDecide(t, plainWorld, tc.wait)
		rec := h.send(t, c)
		got := h.edge.DecideCounts()
		if rec.Code != tc.status || got.Answered != tc.answered || (h.python.hits.Load() == 1) != (tc.answered == 0) ||
			!maps.Equal(got.Passed, tc.passed) || !maps.Equal(got.CacheWait, tc.waited) ||
			!maps.Equal(got.CacheMiss, map[string]int64{gwstate.MissNew: 1}) {
			t.Fatalf("wait %v: %d, Python reached %d, %+v", tc.wait, rec.Code, h.python.hits.Load(), got)
		}
	}
}

// TestTheReadThroughPassesWhenTheReadIsSlow: a read slower than the wait
// passes the request (cache.key) once the wait ends, never later, and the
// decide line counts the wait timed out.
func TestTheReadThroughPassesWhenTheReadIsSlow(t *testing.T) {
	const wait = 30 * time.Millisecond
	slow := func(c authorizeCase) *fakeWorld { return &fakeWorld{c: c, slow: 5 * time.Second} }
	h, c, _, _ := waitingDecide(t, slow, wait)
	began := time.Now()
	rec := h.send(t, c)
	took := time.Since(began)
	got := h.edge.DecideCounts()
	if rec.Code != http.StatusTeapot || h.python.hits.Load() != 1 || took > 2*time.Second ||
		!maps.Equal(got.Passed, map[string]int64{PassCacheKey: 1}) ||
		!maps.Equal(got.CacheWait, map[string]int64{gwstate.WaitTimedOut: 1}) {
		t.Fatalf("a slow read, after %v: %d %+v", took, rec.Code, got)
	}
	h.edge.decider.flush()
	if line := h.logs.all(); !strings.Contains(line, `cache_miss={"cold.new":1} cache_wait={"timed_out":1} `+
		`cache_wait_ms_sum=`) {
		t.Fatalf("the decide line: %s", line)
	}
}

// TestTheReadThroughReadsARevokedKey: a key the cache held eligible, then
// disabled in the store while its entry aged, is read again by the wait and
// passed as disabled (Python answers its 401); never decided on the facts
// held before.
func TestTheReadThroughReadsARevokedKey(t *testing.T) {
	var world *fakeWorld
	keep := func(c authorizeCase) *fakeWorld {
		world = &fakeWorld{c: c}
		return world
	}
	h, c, cache, clock := waitingDecide(t, keep, MaxCacheWait)
	// Filled, eligible, two minutes before the request.
	clock.set(c.at().Add(-2 * time.Minute))
	if got := cache.LookupWithin(c.Facts.Key.LookupHash, clock.Now(), time.Second); !got.Usable() {
		t.Fatalf("the fill: %+v", got)
	}
	revoked := c
	body := strings.TrimSuffix(*c.Key.KeyBody, "}") + `,"disabled":true}`
	revoked.Key.KeyBody = &body
	world.c = revoked
	clock.set(c.at())
	rec := h.send(t, c)
	got := h.edge.DecideCounts()
	if rec.Code != http.StatusTeapot || h.python.hits.Load() != 1 || got.Answered != 0 ||
		!maps.Equal(got.Passed, map[string]int64{PassKeyDisabled: 1}) ||
		!maps.Equal(got.CacheMiss, map[string]int64{gwstate.MissIdle: 1}) ||
		!maps.Equal(got.CacheWait, map[string]int64{gwstate.WaitFilled: 1}) {
		t.Fatalf("a revoked key: %d %+v", rec.Code, got)
	}
}

// TestTheReadThroughReadsALookupHashWithNoKey: a lookup hash naming no key
// is read by the wait and passed as missing (Python's 401), not as a cold
// cache.
func TestTheReadThroughReadsALookupHashWithNoKey(t *testing.T) {
	nobody := func(c authorizeCase) *fakeWorld {
		c.Key.KeyBody = nil
		return &fakeWorld{c: c}
	}
	h, c, _, _ := waitingDecide(t, nobody, MaxCacheWait)
	rec := h.send(t, c)
	got := h.edge.DecideCounts()
	if rec.Code != http.StatusTeapot || !maps.Equal(got.Passed, map[string]int64{PassKeyMissing: 1}) ||
		!maps.Equal(got.CacheWait, map[string]int64{gwstate.WaitFilled: 1}) {
		t.Fatalf("no key: %d %+v", rec.Code, got)
	}
}

// TestTheCacheWaitIsBounded: New takes a wait of 0 to MaxCacheWait, and a
// wait only with a cache that can wait for a read.
func TestTheCacheWaitIsBounded(t *testing.T) {
	if MaxCacheWait > DecideBudget/6 || MaxCacheWait >= SlowAtEdge {
		t.Fatalf("the wait %v eats into the decide budget %v or the slow pass bound %v", MaxCacheWait, DecideBudget,
			SlowAtEdge)
	}
	upstream, _ := url.Parse("http://127.0.0.1:1")
	snapshotURL, _ := url.Parse("http://127.0.0.1:1/internal/fastpath/routing-snapshot")
	policyURL, _ := url.Parse("http://127.0.0.1:1/internal/fastpath/edge-policy")
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys := []SnapshotKey{{Kid: "routing-snapshot-1", PublicKey: public}}
	cache, err := gwstate.NewCache(gwstate.CacheConfig{Reader: &fakeWorld{}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cache.Close)
	for _, tc := range []struct {
		cache KeyCache
		wait  time.Duration
		ok    bool
	}{
		{cache, 0, true},
		{cache, time.Millisecond, true},
		{cache, MaxCacheWait, true},
		{cache, MaxCacheWait + time.Nanosecond, false},
		{cache, -time.Nanosecond, false},
		{&recordingCache{KeyCache: cache}, 0, true},
		{&recordingCache{KeyCache: cache}, time.Millisecond, false},
	} {
		_, err := New(Config{Token: "t", Upstream: upstream, Boots: &boots{},
			Snapshot: SnapshotConfig{URL: snapshotURL, Keys: keys, Plane: "gcp/us-central1"},
			Policy:   PolicyConfig{URL: policyURL, Keys: keys, Plane: "gcp/us-central1"},
			Decide:   &DecideConfig{Cache: tc.cache, Decides: &fakeDecides{}, Rows: &fakeInserter{}, CacheWait: tc.wait}})
		if (err == nil) != tc.ok {
			t.Errorf("%T, wait %v: %v", tc.cache, tc.wait, err)
		}
	}
}
