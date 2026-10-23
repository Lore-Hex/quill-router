package gwstate

import (
	"container/list"
	"context"
	"errors"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The cache's numbers (docs/design/fast-admission-e2-decide.md, decisions 2
// and 17): an entry used in the last minute is read again strongly every 5 s,
// and one read more than 15 s ago is stale, so a disabled, expired or
// rescoped key, a paused or latched workspace, or a debt mark is honoured
// within 15 s, two failed refreshes allowed. A lookup hash with no key is
// remembered for 5 s.
const (
	DefaultCacheSize    = 100_000
	DefaultRefreshEvery = 5 * time.Second
	DefaultUsedWithin   = time.Minute
	DefaultMaxAge       = 15 * time.Second
	DefaultNegativeFor  = 5 * time.Second
	DefaultReadTimeout  = 2 * time.Second
	DefaultReads        = 32
)

// Reader is the store's reads the cache makes (store.ReadKeyContext and
// store.ReadCreditState); *store.Store is one.
type Reader interface {
	ReadKeyContext(ctx context.Context, lookupHash string) (store.KeyContext, bool, error)
	ReadCreditState(ctx context.Context, workspace string) (store.CreditState, error)
}

// CacheConfig is a cache's reader and numbers; a zero number is its default.
type CacheConfig struct {
	Reader Reader
	Clock  func() time.Time
	// Size bounds the keys held, the least recently used dropped first; a
	// workspace's credit entry is held while a key held names it.
	Size int
	// RefreshEvery is how often an entry used within UsedWithin is read
	// again; MaxAge how long after its read an entry is fresh; NegativeFor
	// how long a lookup hash with no key, or a workspace with no complete
	// credit rows, is remembered. MaxAge must be past RefreshEvery, and
	// NegativeFor no more than MaxAge.
	RefreshEvery, UsedWithin, MaxAge, NegativeFor time.Duration
	// ReadTimeout bounds one read; Reads how many run at once. A miss
	// that finds every read busy is not filled (Counters.Dropped). The
	// refresh takes at most three quarters of the reads (one at least),
	// waiting for one when they are busy, so a quarter is always left to
	// the misses.
	//
	// What the defaults hold fresh: a refresh pass makes about 24 reads at
	// once, so with a strong read's 5 to 10 ms it reads some 2,400 to
	// 4,800 entries a second, 12,000 to 24,000 in a pass of 5 s, and keeps
	// some 36,000 to 72,000 keys and credit entries in use within 15 s.
	// Run ends each pass at RefreshEvery, and every pass reads the entries
	// read longest ago first; past that capacity the entries it does not
	// reach go stale and pass, and a lookup of one reads it with the
	// misses' reads (Counters.Unrefreshed counts the reads a pass gave up).
	ReadTimeout time.Duration
	Reads       int
}

// Status is what an entry is worth to a request.
type Status uint8

const (
	// Cold: nothing is held, or what was is past use. The request passes
	// (cache.key, cache.workspace) and a read fills the entry behind it.
	Cold Status = iota
	// Fresh: read within MaxAge (a negative entry within NegativeFor).
	Fresh
	// Stale: read more than MaxAge ago, its refreshes failing or not yet
	// made. What it holds is returned, for the shadow to see, but the
	// request passes as for Cold, and a read refreshes it.
	Stale
	// Unneeded: the workspace's credit rows are not read for a key the
	// facts already put outside the cohort, or that has no key.
	Unneeded
)

func (s Status) String() string {
	switch s {
	case Cold:
		return "cold"
	case Fresh:
		return "fresh"
	case Stale:
		return "stale"
	case Unneeded:
		return "unneeded"
	}
	return "unknown"
}

// Read is what the cache holds for a lookup hash at a request's instant.
// The facts' slices are shared with the cache and with other requests:
// they are read, never changed.
type Read struct {
	// Key is the key entry's status; unless Cold, Found is whether the
	// lookup hash names a key (false: Python's 401, key.missing), Facts
	// what one read of it found, and KeyReadAt when that read began.
	Key       Status
	Found     bool
	Facts     Facts
	KeyReadAt time.Time
	// Credit is the status of the key's workspace's credit entry; unless
	// Cold or Unneeded, CreditRows is whether the workspace has complete
	// credit rows (false: store.ErrCreditRowsIncomplete, which a grant
	// refuses), CreditState what they say, and CreditReadAt when the read
	// began.
	Credit       Status
	CreditRows   bool
	CreditState  store.CreditState
	CreditReadAt time.Time
}

// Usable is whether the cache lets the request be decided: the key fresh
// and found, its facts and its workspace's eligible, and its workspace's
// credit entry fresh with complete rows. It is necessary, not sufficient:
// the key's expiry at the request's instant (KeyFacts.At) and what the
// credit rows say (CreditState's pause, latch, debt, tier and refusal) are
// the gate's to read.
func (r Read) Usable() bool {
	return r.Key == Fresh && r.Found && r.Facts.Key.Eligible && r.Facts.Workspace.Eligible &&
		r.Credit == Fresh && r.CreditRows
}

// Counters are a cache's counts since it was made.
type Counters struct {
	// Lookups of keys and of credit entries, by what they found. KeyMissing
	// counts the fresh negative entries among KeyHits.
	KeyHits, KeyCold, KeyStale, KeyMissing int64
	CreditHits, CreditCold, CreditStale    int64
	// Reads made, ReadFailures those that failed (an entry refreshed or
	// filled by a failed read keeps what it held, and ages), Dropped the
	// misses not filled for every read being busy, Voided the reads whose
	// answer an eviction made while they ran discarded.
	Reads, ReadFailures, Dropped, Voided int64
	// Evictions by Evict and EvictWorkspace; Displaced the keys the size
	// bound dropped; Unrefreshed the reads a refresh pass gave up when its
	// context ended.
	Evictions, Displaced, Unrefreshed int64
}

// Cache holds the facts of the keys the edge has seen, by lookup hash, and
// the credit state of their workspaces, for the decide path to gate on
// without a read of its own (E2, docs/design/fast-admission-e2-decide.md
// §1 step 3). A lookup never waits for a read: what it misses passes to
// Python and is filled in the background, one read per lookup hash or
// workspace at a time, and Run reads again every entry in use, strongly, so
// what a request sees is at most MaxAge old or it passes.
//
// Python's 401, 402 or 403 for a key, or its pause 503, says the facts held
// may be behind the store's: the edge calls Evict, or EvictWorkspace, and a
// read already running when it does is discarded, as it may have read
// before the change Python saw.
type Cache struct {
	cfg CacheConfig
	// reads are every read's turn; refreshes the refresh's share of them.
	reads, refreshes chan struct{}
	ctx              context.Context
	stop             context.CancelFunc
	wg               sync.WaitGroup

	mu sync.Mutex
	// keys are held by lookup hash; order their uses, the most recent at
	// the front.
	keys  map[string]*keyEntry
	order *list.List
	// credits are held by workspace; members names the held keys whose
	// facts name each workspace, so evicting it evicts them (each holds
	// the workspace's entity facts), and the last of them to go takes its
	// credit entry.
	credits map[string]*creditEntry
	members map[string]map[string]struct{}
	// keyReads and creditReads are the reads running, one per lookup hash
	// or workspace; a refresh registers a read when it begins it.
	keyReads    map[string]*flight
	creditReads map[string]*flight

	keyHits, keyCold, keyStale, keyMissing atomic.Int64
	creditHits, creditCold, creditStale    atomic.Int64
	readCount, readFailures, dropped       atomic.Int64
	voided, evictions, displaced           atomic.Int64
	unrefreshed                            atomic.Int64
}

type keyEntry struct {
	lookupHash     string
	found          bool
	facts          Facts
	readAt, usedAt time.Time
	elem           *list.Element
}

// workspace is the workspace the key's facts name, "" for none.
func (e *keyEntry) workspace() string {
	if !e.found {
		return ""
	}
	return e.facts.Key.WorkspaceID
}

// credit is the workspace whose credit entry the key's facts need, "" for
// none: no key, or one the facts put outside the cohort whatever its credit
// rows say.
func (e *keyEntry) credit() string {
	if !e.facts.Key.Eligible || !e.facts.Workspace.Eligible {
		return ""
	}
	return e.workspace()
}

type creditEntry struct {
	rows           bool
	state          store.CreditState
	readAt, usedAt time.Time
}

// flight is a read running. Void is set when an eviction discards its
// answer; refresh is a read of an entry held, whose answer is dropped if the
// entry has gone meanwhile. Evicted are the workspaces evicted while a key's
// read ran: its answer is discarded if it names one of them, as it may have
// read before the change Python saw.
type flight struct {
	void, refresh bool
	evicted       []string
}

// NewCache makes a cache; Run refreshes it, and Close stops its reads.
func NewCache(cfg CacheConfig) (*Cache, error) {
	if cfg.Reader == nil {
		return nil, errors.New("gwstate: a cache needs a reader")
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	for _, d := range []struct {
		into *time.Duration
		def  time.Duration
	}{{&cfg.RefreshEvery, DefaultRefreshEvery}, {&cfg.UsedWithin, DefaultUsedWithin}, {&cfg.MaxAge, DefaultMaxAge},
		{&cfg.NegativeFor, DefaultNegativeFor}, {&cfg.ReadTimeout, DefaultReadTimeout}} {
		if *d.into == 0 {
			*d.into = d.def
		}
		if *d.into < 0 {
			return nil, errors.New("gwstate: a cache's durations are positive")
		}
	}
	if cfg.Size == 0 {
		cfg.Size = DefaultCacheSize
	}
	if cfg.Reads == 0 {
		cfg.Reads = DefaultReads
	}
	if cfg.Size < 0 || cfg.Reads < 0 {
		return nil, errors.New("gwstate: a cache's size and reads are positive")
	}
	if cfg.MaxAge <= cfg.RefreshEvery || cfg.NegativeFor > cfg.MaxAge {
		return nil, errors.New("gwstate: a cache's maximum age must be past its refresh interval and its negative time")
	}
	ctx, stop := context.WithCancel(context.Background())
	return &Cache{cfg: cfg, reads: make(chan struct{}, cfg.Reads),
		refreshes: make(chan struct{}, max(1, cfg.Reads-cfg.Reads/4)), ctx: ctx, stop: stop,
		keys: map[string]*keyEntry{}, order: list.New(), credits: map[string]*creditEntry{},
		members: map[string]map[string]struct{}{}, keyReads: map[string]*flight{},
		creditReads: map[string]*flight{}}, nil
}

// Close stops the cache's reads and waits for those running to end; it is
// called after the last Lookup and Refresh.
func (c *Cache) Close() {
	c.stop()
	c.wg.Wait()
}

// Lookup is what the cache holds for a lookup hash at now, the request's
// one clock reading (decision 15). It never waits: a cold or stale entry is
// returned as such, and a read of it starts unless one is running.
func (c *Cache) Lookup(lookupHash string, now time.Time) Read {
	c.mu.Lock()
	defer c.mu.Unlock()
	e := c.keys[lookupHash]
	if e != nil && !e.found && now.Sub(e.readAt) > c.cfg.NegativeFor {
		c.remove(e)
		e = nil
	}
	if e == nil {
		c.keyCold.Add(1)
		c.startKey(lookupHash)
		return Read{}
	}
	if now.After(e.usedAt) {
		e.usedAt = now
	}
	c.order.MoveToFront(e.elem)
	r := Read{Key: Fresh, Found: e.found, Facts: e.facts, KeyReadAt: e.readAt, Credit: Unneeded}
	switch {
	case now.Sub(e.readAt) > c.cfg.MaxAge:
		r.Key = Stale
		c.keyStale.Add(1)
		c.startKey(lookupHash)
	case !e.found:
		c.keyMissing.Add(1)
		c.keyHits.Add(1)
	default:
		c.keyHits.Add(1)
	}
	ws := e.credit()
	if ws == "" {
		return r
	}
	ce := c.credits[ws]
	if ce != nil && !ce.rows && now.Sub(ce.readAt) > c.cfg.NegativeFor {
		delete(c.credits, ws)
		ce = nil
	}
	if ce == nil {
		r.Credit = Cold
		c.creditCold.Add(1)
		c.startCredit(ws)
		return r
	}
	// Whether the entry is fresh is judged here, at now, and only here:
	// startCredit reads it unless a read is running, whatever the cache's
	// own clock says.
	if now.After(ce.usedAt) {
		ce.usedAt = now
	}
	r.Credit, r.CreditRows, r.CreditState, r.CreditReadAt = Fresh, ce.rows, ce.state, ce.readAt
	if now.Sub(ce.readAt) > c.cfg.MaxAge {
		r.Credit = Stale
		c.creditStale.Add(1)
		c.startCredit(ws)
	} else {
		c.creditHits.Add(1)
	}
	return r
}

// Evict drops what the cache holds for a lookup hash, with its workspace
// (EvictWorkspace) when the facts held name one: Python answered the key a
// 401, 402 or 403, or its workspace a pause 503. A read of the key running
// is discarded.
func (c *Cache) Evict(lookupHash string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.evictions.Add(1)
	c.voidKey(lookupHash)
	e := c.keys[lookupHash]
	if e == nil {
		return
	}
	ws := e.facts.Key.WorkspaceID
	c.remove(e)
	if ws != "" {
		c.evictWorkspace(ws)
	}
}

// EvictWorkspace drops a workspace's credit entry and every key held whose
// facts name it. The workspace's credit read running is discarded, and so
// is any read of a key running now whose answer names the workspace (its
// workspace is not known until it answers).
func (c *Cache) EvictWorkspace(workspace string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.evictions.Add(1)
	c.evictWorkspace(workspace)
}

func (c *Cache) evictWorkspace(workspace string) {
	delete(c.credits, workspace)
	if f := c.creditReads[workspace]; f != nil {
		f.void = true
		delete(c.creditReads, workspace)
	}
	for hash := range c.members[workspace] {
		c.remove(c.keys[hash])
	}
	for _, f := range c.keyReads {
		f.evicted = append(f.evicted, workspace)
	}
}

func (c *Cache) voidKey(lookupHash string) {
	if f := c.keyReads[lookupHash]; f != nil {
		f.void = true
		delete(c.keyReads, lookupHash)
	}
}

// remove drops a key entry, and its workspace's credit entry when no other
// key held needs it.
func (c *Cache) remove(e *keyEntry) {
	delete(c.keys, e.lookupHash)
	c.order.Remove(e.elem)
	c.unmember(e.lookupHash, e.workspace())
}

func (c *Cache) unmember(lookupHash, workspace string) {
	if workspace == "" {
		return
	}
	m := c.members[workspace]
	delete(m, lookupHash)
	if len(m) == 0 {
		delete(c.members, workspace)
		delete(c.credits, workspace)
	}
}

func (c *Cache) member(lookupHash, workspace string) {
	if workspace == "" {
		return
	}
	m := c.members[workspace]
	if m == nil {
		m = map[string]struct{}{}
		c.members[workspace] = m
	}
	m[lookupHash] = struct{}{}
}

// startKey starts a read of a key unless one is running or every read is
// busy; c.mu is held. The key's first read reads its workspace's credit
// rows after it, with the same turn of the reads.
func (c *Cache) startKey(lookupHash string) {
	if c.keyReads[lookupHash] != nil || !c.takeRead() {
		return
	}
	f := &flight{}
	c.keyReads[lookupHash] = f
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		defer c.releaseRead()
		if ws, at := c.readKey(lookupHash, f); ws != "" {
			c.mu.Lock()
			cf := c.beginCredit(ws, at)
			c.mu.Unlock()
			if cf != nil {
				c.readCredit(ws, cf)
			}
		}
	}()
}

// startCredit starts a read of a workspace's credit rows, which a lookup
// found cold or stale, unless one is running or every read is busy; c.mu is
// held.
func (c *Cache) startCredit(workspace string) {
	if c.creditReads[workspace] != nil || !c.takeRead() {
		return
	}
	f := &flight{}
	c.creditReads[workspace] = f
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		defer c.releaseRead()
		c.readCredit(workspace, f)
	}()
}

// beginCredit registers a read of a workspace's credit rows, after its
// key's read that began at, unless one is running or the entry is fresh
// at; c.mu is held.
func (c *Cache) beginCredit(workspace string, at time.Time) *flight {
	if c.creditReads[workspace] != nil {
		return nil
	}
	if ce := c.credits[workspace]; ce != nil {
		age := at.Sub(ce.readAt)
		if age <= c.cfg.MaxAge && (ce.rows || age <= c.cfg.NegativeFor) {
			return nil
		}
	}
	f := &flight{}
	c.creditReads[workspace] = f
	return f
}

func (c *Cache) takeRead() bool {
	select {
	case c.reads <- struct{}{}:
		return true
	default:
		c.dropped.Add(1)
		return false
	}
}

func (c *Cache) releaseRead() { <-c.reads }

// readKey reads a key and holds what it finds, unless the read was voided,
// failed, named a workspace evicted while it ran, or refreshed an entry
// gone meanwhile. It returns the workspace whose credit entry the key's
// facts need, if it held them, and when the read began.
func (c *Cache) readKey(lookupHash string, f *flight) (string, time.Time) {
	at := c.cfg.Clock()
	ctx, cancel := context.WithTimeout(c.ctx, c.cfg.ReadTimeout)
	kc, found, err := c.cfg.Reader.ReadKeyContext(ctx, lookupHash)
	cancel()
	c.readCount.Add(1)
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.keyReads[lookupHash] == f {
		delete(c.keyReads, lookupHash)
	}
	var facts Facts
	if found {
		facts = FromContext(kc)
	}
	switch {
	case f.void || found && (slices.Contains(f.evicted, facts.Key.WorkspaceID) ||
		slices.Contains(f.evicted, kc.RangeWorkspaceID)):
		c.voided.Add(1)
		return "", at
	case err != nil:
		c.readFailures.Add(1)
		return "", at
	}
	e := c.keys[lookupHash]
	if e == nil {
		if f.refresh {
			return "", at
		}
		if len(c.keys) >= c.cfg.Size {
			c.remove(c.order.Back().Value.(*keyEntry))
			c.displaced.Add(1)
		}
		e = &keyEntry{lookupHash: lookupHash, usedAt: at}
		e.elem = c.order.PushFront(e)
		c.keys[lookupHash] = e
	}
	was := e.workspace()
	e.found, e.facts, e.readAt = found, facts, at
	if ws := e.workspace(); ws != was {
		c.unmember(lookupHash, was)
		c.member(lookupHash, ws)
	}
	return e.credit(), at
}

// readCredit reads a workspace's credit rows and holds what they say,
// unless the read was voided, failed, or no key held needs them now.
func (c *Cache) readCredit(workspace string, f *flight) {
	at := c.cfg.Clock()
	ctx, cancel := context.WithTimeout(c.ctx, c.cfg.ReadTimeout)
	state, err := c.cfg.Reader.ReadCreditState(ctx, workspace)
	cancel()
	c.readCount.Add(1)
	rows := true
	if errors.Is(err, store.ErrCreditRowsIncomplete) {
		state, rows, err = store.CreditState{}, false, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.creditReads[workspace] == f {
		delete(c.creditReads, workspace)
	}
	switch {
	case f.void:
		c.voided.Add(1)
		return
	case err != nil:
		c.readFailures.Add(1)
		return
	case len(c.members[workspace]) == 0:
		return
	}
	ce := c.credits[workspace]
	if ce == nil {
		if f.refresh {
			return
		}
		ce = &creditEntry{usedAt: at}
		c.credits[workspace] = ce
	}
	ce.rows, ce.state, ce.readAt = rows, state, at
}

// Run refreshes the cache every RefreshEvery until ctx ends, each pass
// given up at RefreshEvery so the next begins with the entries read longest
// ago.
func (c *Cache) Run(ctx context.Context) {
	tick := time.NewTicker(c.cfg.RefreshEvery)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			pass, cancel := context.WithTimeout(ctx, c.cfg.RefreshEvery)
			c.Refresh(pass)
			cancel()
		}
	}
}

// Refresh reads again, strongly, every key and credit entry used within
// UsedWithin and not read within half of RefreshEvery (a miss just filled),
// those read longest ago first, and returns when the reads have answered or
// ctx ends; the reads not begun then are given up. A negative entry is not
// refreshed: it lapses after NegativeFor, and the next lookup reads again.
//
// The entries due are found under the lock by walking the uses from the
// most recent, to the first unused for UsedWithin and RefreshEvery more (a
// request's instant may trail another's by the decide budget, never by
// seconds); each read is registered when it begins, so a lookup is never
// kept waiting for a read the pass has yet to make.
func (c *Cache) Refresh(ctx context.Context) {
	now := c.cfg.Clock()
	due := func(readAt, usedAt time.Time) bool {
		return now.Sub(usedAt) <= c.cfg.UsedWithin && now.Sub(readAt) >= c.cfg.RefreshEvery/2
	}
	type job struct {
		key, workspace string
		readAt         time.Time
	}
	var jobs []job
	workspaces := map[string]bool{}
	c.mu.Lock()
	for el := c.order.Front(); el != nil; el = el.Next() {
		e := el.Value.(*keyEntry)
		if now.Sub(e.usedAt) > c.cfg.UsedWithin+c.cfg.RefreshEvery {
			break
		}
		if e.found && due(e.readAt, e.usedAt) {
			jobs = append(jobs, job{key: e.lookupHash, readAt: e.readAt})
		}
		if ws := e.credit(); ws != "" && !workspaces[ws] {
			workspaces[ws] = true
			if ce := c.credits[ws]; ce != nil && ce.rows && due(ce.readAt, ce.usedAt) {
				jobs = append(jobs, job{workspace: ws, readAt: ce.readAt})
			}
		}
	}
	c.mu.Unlock()
	slices.SortStableFunc(jobs, func(a, b job) int { return a.readAt.Compare(b.readAt) })
	var pass sync.WaitGroup
	defer pass.Wait()
	for i, j := range jobs {
		if !c.takeRefresh(ctx) {
			c.unrefreshed.Add(int64(len(jobs) - i))
			return
		}
		c.mu.Lock()
		f := &flight{refresh: true}
		switch {
		case j.key != "" && c.keyReads[j.key] == nil && c.keys[j.key] != nil:
			c.keyReads[j.key] = f
		case j.workspace != "" && c.creditReads[j.workspace] == nil && c.credits[j.workspace] != nil:
			c.creditReads[j.workspace] = f
		default:
			// Read meanwhile by a lookup, or gone.
			f = nil
		}
		c.mu.Unlock()
		if f == nil {
			c.releaseRefresh()
			continue
		}
		pass.Add(1)
		c.wg.Add(1)
		go func() {
			defer c.wg.Done()
			defer pass.Done()
			defer c.releaseRefresh()
			if j.key != "" {
				c.readKey(j.key, f)
			} else {
				c.readCredit(j.workspace, f)
			}
		}()
	}
}

// takeRefresh waits for a turn of the refresh's share of the reads, and of
// the reads, until ctx ends.
func (c *Cache) takeRefresh(ctx context.Context) bool {
	select {
	case c.refreshes <- struct{}{}:
	case <-ctx.Done():
		return false
	}
	select {
	case c.reads <- struct{}{}:
		return true
	case <-ctx.Done():
		<-c.refreshes
		return false
	}
}

func (c *Cache) releaseRefresh() {
	<-c.reads
	<-c.refreshes
}

// Counters are the cache's counts now.
func (c *Cache) Counters() Counters {
	return Counters{
		KeyHits: c.keyHits.Load(), KeyCold: c.keyCold.Load(), KeyStale: c.keyStale.Load(),
		KeyMissing: c.keyMissing.Load(), CreditHits: c.creditHits.Load(), CreditCold: c.creditCold.Load(),
		CreditStale: c.creditStale.Load(), Reads: c.readCount.Load(), ReadFailures: c.readFailures.Load(),
		Dropped: c.dropped.Load(), Voided: c.voided.Load(), Evictions: c.evictions.Load(),
		Displaced: c.displaced.Load(), Unrefreshed: c.unrefreshed.Load(),
	}
}

// Len is how many keys and credit entries the cache holds.
func (c *Cache) Len() (keys, credits int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.keys), len(c.credits)
}
