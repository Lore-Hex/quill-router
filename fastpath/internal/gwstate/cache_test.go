package gwstate

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// clock is a test's time, moved by hand.
type clock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) advance(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
	return c.now
}

// fakeReader is the store as the cache reads it: keys by lookup hash and
// credit states by workspace, changed by the test, each read counted. A
// read waits on gate while it is set.
type fakeReader struct {
	mu       sync.Mutex
	keys     map[string]store.KeyContext
	credits  map[string]store.CreditState
	keyErr   error
	gate     chan struct{}
	started  chan string
	keyReads map[string]int
	creditN  map[string]int
	// delay lengthens every read, widening the windows reads run across.
	delay time.Duration
}

func newFakeReader() *fakeReader {
	return &fakeReader{keys: map[string]store.KeyContext{}, credits: map[string]store.CreditState{},
		keyReads: map[string]int{}, creditN: map[string]int{}}
}

func (r *fakeReader) wait(ctx context.Context, what string) error {
	r.mu.Lock()
	gate, started, delay := r.gate, r.started, r.delay
	r.mu.Unlock()
	if delay > 0 {
		time.Sleep(time.Duration(rand.Int64N(int64(delay))))
	}
	if started != nil {
		started <- what
	}
	if gate != nil {
		select {
		case <-gate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (r *fakeReader) ReadKeyContext(ctx context.Context, lookupHash string) (store.KeyContext, bool, error) {
	if err := r.wait(ctx, "key "+lookupHash); err != nil {
		return store.KeyContext{}, false, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.keyReads[lookupHash]++
	if r.keyErr != nil {
		return store.KeyContext{}, false, r.keyErr
	}
	kc, ok := r.keys[lookupHash]
	return kc, ok, nil
}

func (r *fakeReader) ReadCreditState(ctx context.Context, workspace string) (store.CreditState, error) {
	if err := r.wait(ctx, "credit "+workspace); err != nil {
		return store.CreditState{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.creditN[workspace]++
	state, ok := r.credits[workspace]
	if !ok {
		return store.CreditState{}, store.ErrCreditRowsIncomplete
	}
	return state, nil
}

func (r *fakeReader) set(f func(r *fakeReader)) {
	r.mu.Lock()
	defer r.mu.Unlock()
	f(r)
}

func (r *fakeReader) reads(lookupHash string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.keyReads[lookupHash]
}

func (r *fakeReader) creditReads(workspace string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.creditN[workspace]
}

// keyContext is an eligible key of a workspace, and its workspace, as the
// store returns them; extra fields are added to the key's body.
func keyContext(lookupHash, workspace, extra string) store.KeyContext {
	return store.KeyContext{
		KeyBody: `{"hash":"kh-` + lookupHash + `","salt":"s","secret_hash":"x","lookup_hash":"` + lookupHash +
			`","name":"n","label":"l","workspace_id":"` + workspace + `","creator_user_id":null` + extra + `}`,
		RangeWorkspaceID: workspace,
		WorkspaceBody:    `{"id":"` + workspace + `","name":"w","owner_user_id":"u"}`,
		WorkspaceFound:   true,
	}
}

var funded = store.CreditState{Shards: 1, Tier: 3}

func newTestCache(t *testing.T, r *fakeReader, c *clock, change ...func(*CacheConfig)) *Cache {
	t.Helper()
	cfg := CacheConfig{Reader: r, Clock: c.Now}
	for _, f := range change {
		f(&cfg)
	}
	cache, err := NewCache(cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cache.Close)
	return cache
}

// receive waits for a read to begin, failing the test rather than hanging
// it when none does.
func receive(t *testing.T, started <-chan string) {
	t.Helper()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("no read began")
	}
}

// settle waits for the cache's background reads to answer.
func (c *Cache) settle() { c.wg.Wait() }

func start() *clock { return &clock{now: time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)} }

// TestAMissPassesColdAndOneReadFillsIt: a lookup never waits for a read; a
// cold key is filled in the background by one read however many lookups
// miss it meanwhile, with its workspace's credit rows after it, and the
// next lookup is a fresh hit on both.
func TestAMissPassesColdAndOneReadFillsIt(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	gate := make(chan struct{})
	r.gate = gate
	cache := newTestCache(t, r, c)
	var wg sync.WaitGroup
	for range 64 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if got := cache.Lookup("lh", c.Now()); got.Key != Cold || got.Credit != Cold {
				t.Errorf("a miss: %+v", got)
			}
		}()
	}
	wg.Wait()
	close(gate)
	cache.settle()
	if n := r.reads("lh"); n != 1 {
		t.Fatalf("%d reads of one key", n)
	}
	if n := r.creditReads("ws"); n != 1 {
		t.Fatalf("%d reads of its credit rows", n)
	}
	got := cache.Lookup("lh", c.Now())
	if got.Key != Fresh || !got.Found || !got.Facts.Key.Eligible || got.Facts.Key.WorkspaceID != "ws" ||
		got.Credit != Fresh || !got.CreditRows || got.CreditState != funded {
		t.Fatalf("the hit: %+v", got)
	}
	if got := cache.Counters(); got.KeyCold != 64 || got.KeyHits != 1 || got.CreditHits != 1 || got.Reads != 2 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestCreditRowsAreReadOnlyForAKeyInTheCohort: a key the facts put outside
// the cohort, or no key, never costs a credit read.
func TestCreditRowsAreReadOnlyForAKeyInTheCohort(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["capped"] = keyContext("capped", "ws", `,"limit_microdollars":5`)
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c)
	for _, hash := range []string{"capped", "nobody"} {
		cache.Lookup(hash, c.Now())
		cache.settle()
	}
	if got := cache.Lookup("capped", c.Now()); got.Key != Fresh || got.Facts.Key.Reason != ReasonKeyLimit ||
		got.Credit != Unneeded {
		t.Fatalf("a capped key: %+v", got)
	}
	if got := cache.Lookup("nobody", c.Now()); got.Key != Fresh || got.Found || got.Credit != Unneeded {
		t.Fatalf("no key: %+v", got)
	}
	if n := r.creditReads("ws"); n != 0 {
		t.Fatalf("%d credit reads", n)
	}
	if got := cache.Counters(); got.KeyMissing != 1 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestAnEntryIsStalePastItsMaximumAge: fresh up to 15 s after its read
// began, stale after, its facts still returned; a stale lookup reads again
// and the read makes it fresh.
func TestAnEntryIsStalePastItsMaximumAge(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c)
	cache.Lookup("lh", c.Now())
	cache.settle()
	read := c.Now()
	if got := cache.Lookup("lh", read.Add(DefaultMaxAge)); got.Key != Fresh || got.Credit != Fresh {
		t.Fatalf("at the maximum age: %+v", got)
	}
	cache.settle()
	if n := r.reads("lh"); n != 1 {
		t.Fatalf("a fresh hit read again: %d", n)
	}
	got := cache.Lookup("lh", c.advance(DefaultMaxAge+time.Nanosecond))
	if got.Key != Stale || !got.Found || got.KeyReadAt != read || got.Credit != Stale ||
		got.CreditState != funded {
		t.Fatalf("past the maximum age: %+v", got)
	}
	cache.settle()
	if n := r.reads("lh"); n != 2 {
		t.Fatalf("a stale hit read %d times", n)
	}
	if got := cache.Lookup("lh", c.Now()); got.Key != Fresh || got.Credit != Fresh {
		t.Fatalf("refilled: %+v", got)
	}
	if got := cache.Counters(); got.KeyStale != 1 || got.CreditStale != 1 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestRefreshReadsEntriesInUseAndHonoursAChange: a refresh reads again
// every entry used in the last minute, so a key disabled or a workspace
// paused in the store is seen at the next refresh; an entry not used for a
// minute is left to age.
func TestRefreshReadsEntriesInUseAndHonoursAChange(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["used"] = keyContext("used", "ws", "")
	r.keys["idle"] = keyContext("idle", "ws-idle", "")
	r.credits["ws"], r.credits["ws-idle"] = funded, funded
	cache := newTestCache(t, r, c)
	for _, hash := range []string{"used", "idle"} {
		cache.Lookup(hash, c.Now())
	}
	cache.settle()
	for range 13 {
		c.advance(DefaultRefreshEvery)
		cache.Lookup("used", c.Now())
		cache.Refresh(context.Background())
	}
	if n := r.reads("used"); n != 14 {
		t.Fatalf("an entry in use read %d times", n)
	}
	if n := r.creditReads("ws"); n != 14 {
		t.Fatalf("a credit entry in use read %d times", n)
	}
	// Used once at the start: refreshed for its first minute, then not.
	if n := r.reads("idle"); n != 13 {
		t.Fatalf("an idle entry read %d times", n)
	}
	// Its last read a minute after its use: stale past 15 s more.
	if got := cache.Lookup("idle", c.advance(DefaultMaxAge-DefaultRefreshEvery+time.Second)); got.Key != Stale {
		t.Fatalf("an idle entry: %+v", got)
	}
	cache.settle()
	r.set(func(r *fakeReader) {
		r.keys["used"] = keyContext("used", "ws", `,"disabled":true`)
		r.credits["ws"] = store.CreditState{Shards: 1, Paused: true, Tier: 3, Refused: store.RefusedPaused}
	})
	c.advance(DefaultRefreshEvery)
	cache.Refresh(context.Background())
	if got := cache.Lookup("used", c.Now()); got.Key != Fresh || got.Facts.Key.Reason != ReasonKeyDisabled {
		t.Fatalf("a disabled key: %+v", got)
	}
	// The key is now outside the cohort; its workspace's credit entry was
	// refreshed with it.
	r.set(func(r *fakeReader) { r.keys["used"] = keyContext("used", "ws", "") })
	c.advance(DefaultRefreshEvery)
	cache.Refresh(context.Background())
	if got := cache.Lookup("used", c.Now()); got.Credit != Fresh || !got.CreditState.Paused {
		t.Fatalf("a paused workspace: %+v", got)
	}
}

// TestAFailedRefreshKeepsTheEntryUntilItIsStale: the reads failing, an
// entry keeps what it held, fresh up to its maximum age and stale past it.
func TestAFailedRefreshKeepsTheEntryUntilItIsStale(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c)
	cache.Lookup("lh", c.Now())
	cache.settle()
	r.set(func(r *fakeReader) { r.keyErr = errors.New("spanner unavailable") })
	for range 3 {
		c.advance(DefaultRefreshEvery)
		cache.Refresh(context.Background())
		if got := cache.Lookup("lh", c.Now()); got.Key != Fresh || !got.Facts.Key.Eligible {
			t.Fatalf("within the maximum age: %+v", got)
		}
	}
	c.advance(time.Nanosecond)
	if got := cache.Lookup("lh", c.Now()); got.Key != Stale {
		t.Fatalf("past the maximum age: %+v", got)
	}
	cache.settle()
	if got := cache.Counters(); got.ReadFailures != 4 {
		t.Fatalf("the counters: %+v", got)
	}
	r.set(func(r *fakeReader) { r.keyErr = nil })
	cache.Refresh(context.Background())
	if got := cache.Lookup("lh", c.Now()); got.Key != Fresh {
		t.Fatalf("the reads back: %+v", got)
	}
}

// TestNegativeEntriesLapse: a lookup hash with no key, and a workspace with
// no complete credit rows, are remembered for 5 s, never refreshed, and
// read again on the first lookup after.
func TestNegativeEntriesLapse(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	cache := newTestCache(t, r, c)
	cache.Lookup("nobody", c.Now())
	cache.Lookup("lh", c.Now())
	cache.settle()
	got := cache.Lookup("lh", c.Now())
	if got.Key != Fresh || got.Credit != Fresh || got.CreditRows {
		t.Fatalf("no credit rows: %+v", got)
	}
	if got := cache.Lookup("nobody", c.Now().Add(DefaultNegativeFor)); got.Key != Fresh || got.Found {
		t.Fatalf("no key, within its time: %+v", got)
	}
	c.advance(DefaultNegativeFor)
	cache.Refresh(context.Background())
	if r.reads("nobody") != 1 || r.creditReads("ws") != 1 {
		t.Fatalf("a negative entry refreshed: %d %d", r.reads("nobody"), r.creditReads("ws"))
	}
	r.set(func(r *fakeReader) {
		r.keys["nobody"] = keyContext("nobody", "ws2", "")
		r.credits["ws"] = funded
	})
	c.advance(time.Nanosecond)
	if got := cache.Lookup("nobody", c.Now()); got.Key != Cold {
		t.Fatalf("no key, past its time: %+v", got)
	}
	if got := cache.Lookup("lh", c.Now()); got.Credit != Cold {
		t.Fatalf("no credit rows, past their time: %+v", got)
	}
	cache.settle()
	if got := cache.Lookup("nobody", c.Now()); got.Key != Fresh || !got.Found {
		t.Fatalf("the key made: %+v", got)
	}
	if got := cache.Lookup("lh", c.Now()); got.Credit != Fresh || !got.CreditRows {
		t.Fatalf("the credit rows made: %+v", got)
	}
}

// TestEvictDropsTheKeyAndItsWorkspace: Python's refusal of a key evicts
// it, its workspace's credit entry and every other key of the workspace;
// another workspace's are kept.
func TestEvictDropsTheKeyAndItsWorkspace(t *testing.T) {
	r, c := newFakeReader(), start()
	for hash, ws := range map[string]string{"a": "ws", "b": "ws", "c": "other"} {
		r.keys[hash] = keyContext(hash, ws, "")
		r.credits[ws] = funded
	}
	r.keys["capped"] = keyContext("capped", "ws", `,"limit_microdollars":5`)
	cache := newTestCache(t, r, c)
	fill := func() {
		for _, hash := range []string{"a", "b", "c", "capped"} {
			cache.Lookup(hash, c.Now())
		}
		cache.settle()
	}
	fill()
	cache.Evict("a")
	for hash, want := range map[string]Status{"a": Cold, "b": Cold, "capped": Cold, "c": Fresh} {
		if got := cache.Lookup(hash, c.Now()); got.Key != want {
			t.Fatalf("%s after evicting a: %v, want %v", hash, got.Key, want)
		}
	}
	cache.settle()
	cache.EvictWorkspace("other")
	if got := cache.Lookup("c", c.Now()); got.Key != Cold {
		t.Fatalf("c after evicting its workspace: %+v", got)
	}
	cache.settle()
	if keys, credits := cache.Len(); keys != 4 || credits != 2 {
		t.Fatalf("refilled: %d keys, %d credit entries", keys, credits)
	}
	cache.EvictWorkspace("ws")
	if keys, credits := cache.Len(); keys != 1 || credits != 1 {
		t.Fatalf("after evicting ws: %d keys, %d credit entries", keys, credits)
	}
	if got := cache.Counters(); got.Evictions != 3 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestAnEvictionDiscardsAReadRunning: a read that began before an eviction
// may have read before the change Python saw, so its answer is discarded
// and the entry stays cold; a key read running when another workspace is
// evicted is kept, its answer naming a workspace not evicted.
func TestAnEvictionDiscardsAReadRunning(t *testing.T) {
	for _, evict := range []func(*Cache){
		func(c *Cache) { c.Evict("lh") },
		func(c *Cache) { c.EvictWorkspace("ws") },
	} {
		r, c := newFakeReader(), start()
		r.keys["lh"] = keyContext("lh", "ws", "")
		r.credits["ws"] = funded
		gate, started := make(chan struct{}), make(chan string, 4)
		r.gate, r.started = gate, started
		cache := newTestCache(t, r, c)
		cache.Lookup("lh", c.Now())
		receive(t, started)
		evict(cache)
		close(gate)
		cache.settle()
		if got := cache.Lookup("lh", c.Now()); got.Key != Cold {
			t.Fatalf("a read the eviction discarded was held: %+v", got)
		}
		cache.settle()
		if got := cache.Counters(); got.Voided != 1 || got.Reads != 3 {
			t.Fatalf("the counters: %+v", got)
		}
	}
	{
		r, c := newFakeReader(), start()
		r.keys["lh"] = keyContext("lh", "ws", "")
		r.credits["ws"] = funded
		gate, started := make(chan struct{}), make(chan string, 4)
		r.gate, r.started = gate, started
		cache := newTestCache(t, r, c)
		cache.Lookup("lh", c.Now())
		receive(t, started)
		cache.EvictWorkspace("elsewhere")
		close(gate)
		cache.settle()
		if got := cache.Lookup("lh", c.Now()); !got.Usable() {
			t.Fatalf("a read another workspace's eviction ran beside: %+v", got)
		}
		if got := cache.Counters(); got.Voided != 0 || got.Reads != 2 {
			t.Fatalf("the counters: %+v", got)
		}
	}
	// A workspace's credit read running when it is evicted.
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c)
	cache.Lookup("lh", c.Now())
	cache.settle()
	c.advance(DefaultMaxAge + time.Second)
	gate, started := make(chan struct{}), make(chan string, 4)
	r.set(func(r *fakeReader) { r.gate, r.started = gate, started })
	cache.Lookup("lh", c.Now())
	receive(t, started)
	receive(t, started)
	cache.EvictWorkspace("ws")
	close(gate)
	cache.settle()
	if keys, credits := cache.Len(); keys != 0 || credits != 0 {
		t.Fatalf("held after the eviction: %d keys, %d credit entries", keys, credits)
	}
	if got := cache.Counters(); got.Voided != 2 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestTheSizeBoundDropsTheLeastRecentlyUsed: past its size the cache drops
// the key used longest ago, with its workspace's credit entry when no key
// held names the workspace.
func TestTheSizeBoundDropsTheLeastRecentlyUsed(t *testing.T) {
	r, c := newFakeReader(), start()
	for i := range 4 {
		hash, ws := fmt.Sprint("k", i), fmt.Sprint("ws", i)
		r.keys[hash], r.credits[ws] = keyContext(hash, ws, ""), funded
	}
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Size = 3 })
	for _, hash := range []string{"k0", "k1", "k2", "k0", "k3"} {
		cache.Lookup(hash, c.Now())
		cache.settle()
	}
	if keys, credits := cache.Len(); keys != 3 || credits != 3 {
		t.Fatalf("%d keys, %d credit entries", keys, credits)
	}
	for _, want := range []struct {
		hash   string
		status Status
	}{{"k0", Fresh}, {"k2", Fresh}, {"k3", Fresh}, {"k1", Cold}} {
		if got := cache.Lookup(want.hash, c.Now()); got.Key != want.status {
			t.Fatalf("%s: %v, want %v", want.hash, got.Key, want.status)
		}
		cache.settle()
	}
	if got := cache.Counters(); got.Displaced != 2 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestAMissWithEveryReadBusyIsNotFilled: reads are bounded; a miss that
// finds none free passes without one, and is counted.
func TestAMissWithEveryReadBusyIsNotFilled(t *testing.T) {
	r, c := newFakeReader(), start()
	gate, started := make(chan struct{}), make(chan string, 4)
	r.gate, r.started = gate, started
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Reads = 1 })
	cache.Lookup("a", c.Now())
	receive(t, started)
	cache.Lookup("b", c.Now())
	close(gate)
	cache.settle()
	if r.reads("b") != 0 || cache.Counters().Dropped != 1 {
		t.Fatalf("b read %d times, %+v", r.reads("b"), cache.Counters())
	}
}

// TestARefreshEndsWithItsContext: a refresh waiting for a read gives up the
// reads it has not begun, so a lookup can start them.
func TestARefreshEndsWithItsContext(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["a"], r.keys["b"] = keyContext("a", "ws", ""), keyContext("b", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Reads = 1 })
	for _, hash := range []string{"a", "b"} {
		cache.Lookup(hash, c.Now())
		cache.settle()
	}
	c.advance(DefaultRefreshEvery)
	gate, started := make(chan struct{}), make(chan string, 4)
	r.set(func(r *fakeReader) { r.gate, r.started = gate, started })
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { cache.Refresh(ctx); close(done) }()
	receive(t, started)
	cancel()
	close(gate)
	<-done
	cache.mu.Lock()
	running := len(cache.keyReads) + len(cache.creditReads)
	cache.mu.Unlock()
	if running != 0 {
		t.Fatalf("%d reads left registered", running)
	}
	// Three reads due (a, b and ws's credit rows): one begun, two given up.
	if got := cache.Counters(); got.Unrefreshed != 2 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestACancelledRefreshBeginsNoRead: with read slots free, a refresh whose
// context has ended gives up every due read. (A select with both cases ready
// picks at random, so each round would otherwise begin a read a quarter of
// the time.)
func TestACancelledRefreshBeginsNoRead(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["a"], r.keys["b"] = keyContext("a", "ws", ""), keyContext("b", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Reads = 4 })
	for _, hash := range []string{"a", "b"} {
		cache.Lookup(hash, c.Now())
		cache.settle()
	}
	c.advance(DefaultRefreshEvery)
	reads := cache.Counters().Reads
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for range 64 {
		cache.Refresh(ctx)
	}
	if got := cache.Counters(); got.Reads != reads || got.Unrefreshed != 64*3 {
		t.Fatalf("the counters: %+v (reads before %d)", got, reads)
	}
}

// TestARefreshLeavesTheMissesAQuarterOfTheReads: a refresh with more due
// than it can read at once takes three quarters of the reads, and a miss
// meanwhile still finds one.
func TestARefreshLeavesTheMissesAQuarterOfTheReads(t *testing.T) {
	r, c := newFakeReader(), start()
	for i := range 10 {
		hash := fmt.Sprint("k", i)
		r.keys[hash] = keyContext(hash, "ws", "")
	}
	r.keys["new"] = keyContext("new", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Reads = 4 })
	for i := range 10 {
		cache.Lookup(fmt.Sprint("k", i), c.Now())
		cache.settle()
	}
	c.advance(DefaultRefreshEvery)
	gate, started := make(chan struct{}), make(chan string, 32)
	r.set(func(r *fakeReader) { r.gate, r.started = gate, started })
	done := make(chan struct{})
	go func() { cache.Refresh(context.Background()); close(done) }()
	for range 3 {
		receive(t, started)
	}
	cache.Lookup("new", c.Now())
	if what := <-started; what != "key new" {
		t.Fatalf("the read begun: %q", what)
	}
	close(gate)
	<-done
	cache.settle()
	if got := cache.Counters(); got.Dropped != 0 {
		t.Fatalf("the counters: %+v", got)
	}
}

// TestTheRequestsInstantJudgesFreshness: a lookup judges an entry at its
// request's instant, which need not be the cache's clock's, ahead of it or
// behind it; a read it starts never trips on the two disagreeing.
func TestTheRequestsInstantJudgesFreshness(t *testing.T) {
	r, c := newFakeReader(), start()
	r.keys["lh"] = keyContext("lh", "ws", "")
	r.credits["ws"] = funded
	cache := newTestCache(t, r, c)
	cache.Lookup("lh", c.Now())
	cache.settle()
	// Ahead: stale at the request's instant, fresh at the clock's.
	ahead := c.Now().Add(DefaultMaxAge + time.Second)
	if got := cache.Lookup("lh", ahead); got.Key != Stale || got.Credit != Stale || got.Usable() {
		t.Fatalf("ahead of the clock: %+v", got)
	}
	cache.settle()
	if r.reads("lh") != 2 || r.creditReads("ws") != 2 {
		t.Fatalf("ahead of the clock, reads: %d %d", r.reads("lh"), r.creditReads("ws"))
	}
	// Behind: fresh at the request's instant though the clock has moved
	// past the maximum age.
	behind := c.Now()
	c.advance(DefaultMaxAge + time.Second)
	if got := cache.Lookup("lh", behind); got.Key != Fresh || !got.Usable() {
		t.Fatalf("behind the clock: %+v", got)
	}
	// The negative credit entry too: lapsed at the request's instant only.
	r.set(func(r *fakeReader) { delete(r.credits, "ws") })
	cache.Lookup("lh", c.Now())
	cache.settle()
	if got := cache.Lookup("lh", c.Now()); got.Credit != Fresh || got.CreditRows {
		t.Fatalf("no credit rows: %+v", got)
	}
	if got := cache.Lookup("lh", c.Now().Add(DefaultNegativeFor+time.Second)); got.Credit != Cold {
		t.Fatalf("no credit rows, ahead of the clock: %+v", got)
	}
	cache.settle()
}

// TestUsable: the cache lets a request be decided only with the key fresh,
// found and eligible and its workspace's credit entry fresh with complete
// rows.
func TestUsable(t *testing.T) {
	usable := Read{Key: Fresh, Found: true, Facts: Facts{Key: KeyFacts{Eligible: true},
		Workspace: WorkspaceFacts{Eligible: true}}, Credit: Fresh, CreditRows: true}
	if !usable.Usable() {
		t.Fatal("a usable read")
	}
	for name, change := range map[string]func(*Read){
		"key stale":            func(r *Read) { r.Key = Stale },
		"key cold":             func(r *Read) { r.Key = Cold },
		"no key":               func(r *Read) { r.Found = false },
		"key ineligible":       func(r *Read) { r.Facts.Key.Eligible = false },
		"workspace ineligible": func(r *Read) { r.Facts.Workspace.Eligible = false },
		"credit stale":         func(r *Read) { r.Credit = Stale },
		"credit cold":          func(r *Read) { r.Credit = Cold },
		"credit unneeded":      func(r *Read) { r.Credit = Unneeded },
		"no credit rows":       func(r *Read) { r.CreditRows = false },
	} {
		r := usable
		change(&r)
		if r.Usable() {
			t.Errorf("%s: usable", name)
		}
	}
}

// TestNoEvictionIsUndone: a key's version is moved in the store, then the
// key or its workspace evicted; from then on no fresh lookup returns an
// older version, whatever reads ran across the eviction, with lookups,
// evictions and refreshes at once.
func TestNoEvictionIsUndone(t *testing.T) {
	r, c := newFakeReader(), start()
	r.delay = 200 * time.Microsecond
	const keys, workspaces = 24, 4
	versions := make([]int, keys)
	floors := make([]atomic.Int64, keys)
	body := func(i int) store.KeyContext {
		return keyContext(fmt.Sprint("k", i), fmt.Sprint("ws", i%workspaces), fmt.Sprintf(`,"tags":{"v":"%d"}`, versions[i]))
	}
	for i := range keys {
		r.keys[fmt.Sprint("k", i)] = body(i)
		r.credits[fmt.Sprint("ws", i%workspaces)] = funded
	}
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Reads = 6 })
	version := func(got Read) int64 {
		for _, tag := range got.Facts.Key.Tags {
			if tag.Key == "v" {
				var v int64
				fmt.Sscan(tag.Value, &v)
				return v
			}
		}
		t.Errorf("no version: %+v", got)
		return -1
	}
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for ctx.Err() == nil {
			c.advance(time.Second)
			cache.Refresh(ctx)
		}
	}()
	// One evictor, so a key's floor only rises.
	wg.Add(1)
	go func() {
		defer wg.Done()
		rng := rand.New(rand.NewPCG(1, 2))
		for ctx.Err() == nil {
			var moved []int
			if rng.IntN(2) == 0 {
				moved = []int{rng.IntN(keys)}
			} else {
				ws := rng.IntN(workspaces)
				for i := ws; i < keys; i += workspaces {
					moved = append(moved, i)
				}
			}
			r.set(func(r *fakeReader) {
				for _, i := range moved {
					versions[i]++
					r.keys[fmt.Sprint("k", i)] = body(i)
				}
			})
			if len(moved) == 1 {
				cache.Evict(fmt.Sprint("k", moved[0]))
			} else {
				cache.EvictWorkspace(fmt.Sprint("ws", moved[0]%workspaces))
			}
			r.set(func(*fakeReader) {
				for _, i := range moved {
					floors[i].Store(int64(versions[i]))
				}
			})
			time.Sleep(100 * time.Microsecond)
		}
	}()
	var fresh atomic.Int64
	for g := range 6 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rng := rand.New(rand.NewPCG(uint64(g), 9))
			for ctx.Err() == nil {
				i := rng.IntN(keys)
				floor := floors[i].Load()
				got := cache.Lookup(fmt.Sprint("k", i), c.Now())
				if got.Key == Fresh && got.Found {
					fresh.Add(1)
					if v := version(got); v < floor {
						t.Errorf("k%d fresh at version %d after its eviction at %d", i, v, floor)
					}
				}
			}
		}()
	}
	time.Sleep(300 * time.Millisecond)
	cancel()
	wg.Wait()
	cache.settle()
	cache.checkIndexes(t)
	if fresh.Load() == 0 || cache.Counters().Evictions == 0 {
		t.Fatalf("nothing tried: %d fresh, %+v", fresh.Load(), cache.Counters())
	}
}

func TestACacheNeedsSaneNumbers(t *testing.T) {
	r := newFakeReader()
	for name, cfg := range map[string]CacheConfig{
		"no reader":         {},
		"max age too short": {Reader: r, MaxAge: DefaultRefreshEvery},
		"negative too long": {Reader: r, NegativeFor: DefaultMaxAge + time.Second},
		"negative size":     {Reader: r, Size: -1},
		"negative duration": {Reader: r, ReadTimeout: -time.Second},
	} {
		if _, err := NewCache(cfg); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}

// TestTheCacheUnderConcurrentUse: lookups, evictions and refreshes at once,
// for the race detector; whatever the interleaving, the cache's indexes
// stay consistent and no read is left registered.
func TestTheCacheUnderConcurrentUse(t *testing.T) {
	r, c := newFakeReader(), start()
	const keys, workspaces = 40, 6
	for i := range keys {
		hash, ws := fmt.Sprint("k", i), fmt.Sprint("ws", i%workspaces)
		r.keys[hash], r.credits[ws] = keyContext(hash, ws, ""), funded
	}
	cache := newTestCache(t, r, c, func(cfg *CacheConfig) { cfg.Size = 25; cfg.Reads = 4 })
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	var lookups atomic.Int64
	wg.Add(1)
	go func() {
		defer wg.Done()
		for ctx.Err() == nil {
			c.advance(time.Second)
			cache.Refresh(ctx)
		}
	}()
	for g := range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			rng := rand.New(rand.NewPCG(uint64(g), 7))
			for i := 0; i < 400; i++ {
				hash := fmt.Sprint("k", rng.IntN(keys))
				switch rng.IntN(10) {
				case 0:
					cache.Evict(hash)
				case 1:
					cache.EvictWorkspace(fmt.Sprint("ws", rng.IntN(workspaces)))
				default:
					cache.Lookup(hash, c.Now())
					lookups.Add(1)
				}
			}
		}()
	}
	time.Sleep(50 * time.Millisecond)
	cancel()
	wg.Wait()
	cache.settle()
	cache.checkIndexes(t)
	if lookups.Load() == 0 {
		t.Fatal("no lookups")
	}
}

// checkIndexes holds the cache's maps and list to one another.
func (c *Cache) checkIndexes(t *testing.T) {
	t.Helper()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.order.Len() != len(c.keys) || len(c.keys) > c.cfg.Size {
		t.Fatalf("%d in order, %d keys, size %d", c.order.Len(), len(c.keys), c.cfg.Size)
	}
	for el := c.order.Front(); el != nil; el = el.Next() {
		e := el.Value.(*keyEntry)
		if c.keys[e.lookupHash] != e {
			t.Fatalf("%s in order and not held", e.lookupHash)
		}
		if ws := e.workspace(); ws != "" {
			if _, ok := c.members[ws][e.lookupHash]; !ok {
				t.Fatalf("%s not a member of %s", e.lookupHash, ws)
			}
		}
	}
	for ws, m := range c.members {
		if len(m) == 0 {
			t.Fatalf("%s has no members", ws)
		}
		for hash := range m {
			if e := c.keys[hash]; e == nil || e.workspace() != ws {
				t.Fatalf("%s a member of %s and not held for it", hash, ws)
			}
		}
	}
	for ws := range c.credits {
		if len(c.members[ws]) == 0 {
			t.Fatalf("a credit entry for %s with no key", ws)
		}
	}
	if len(c.reads) != 0 || len(c.keyReads) != 0 || len(c.creditReads) != 0 {
		t.Fatalf("reads left: %d %d %d", len(c.reads), len(c.keyReads), len(c.creditReads))
	}
}
