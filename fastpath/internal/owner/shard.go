package owner

import (
	"errors"
	"fmt"
	"math"
	"slices"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// ShardKey names a workspace's shard in a region: the leases an owner holds
// for it, which admit its requests, and the unit its top-ups are asked for
// (§4.2, §4.4).
type ShardKey struct {
	Workspace string
	Region    string
	Shard     int64
}

// TopUps is how an owner keeps a shard's leases (§4.2). When a shard's free
// room, with pending put back, falls below LowWater, the owner asks Spanner
// for another lease: one ask outstanding per shard, and none sooner than
// Cooldown after the last. Its size is what the shard charged over the last
// Horizon, plus what its leases' open holds hold and buffer, between Min and
// Max. A lease that has admitted nothing for IdleAfter, or is MaxLife old,
// admits nothing more (Close).
type TopUps struct {
	LowWater  int64
	Cooldown  time.Duration
	Horizon   time.Duration
	Min, Max  int64
	IdleAfter time.Duration
	MaxLife   time.Duration
}

func (t TopUps) validate() error {
	if t.LowWater < 0 || t.Cooldown <= 0 || t.Horizon <= 0 || t.Min <= 0 || t.Max < t.Min || t.IdleAfter <= 0 ||
		t.MaxLife <= 0 {
		return errors.New("owner: top-ups need a low-water mark, positive durations and sizes, and Min at most Max")
	}
	if t.Horizon%time.Duration(len(charges{}.buckets)) != 0 {
		// The horizon is kept in sixty buckets: a whole number of
		// nanoseconds each, so the buckets cover it exactly.
		return fmt.Errorf("owner: a horizon of %v is not %d buckets of whole nanoseconds", t.Horizon,
			len(charges{}.buckets))
	}
	return nil
}

// shard is a shard's leases at the owner, oldest first; whether an ask for a
// top-up is outstanding, when the last began, and an ask to make again,
// whose answer was lost; and what its leases charged, by when.
type shard struct {
	key     ShardKey
	leases  []*Lease
	asking  bool
	askedAt time.Time
	retry   *store.GrantRequest
	charges charges
}

// charges are a shard's charges over the horizon, in sixty buckets of a
// sixtieth of it each, by when they were decided: a top-up is sized by the
// charges within the horizon, to within a bucket.
type charges struct {
	mu      sync.Mutex
	width   time.Duration
	buckets [60]struct {
		start   time.Time
		charged int64
	}
}

func (c *charges) add(now time.Time, charged int64) {
	if c.width <= 0 || charged <= 0 {
		return
	}
	start := now.Truncate(c.width)
	i := int((start.UnixNano() / int64(c.width)) % int64(len(c.buckets)))
	if i < 0 {
		i += len(c.buckets)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	b := &c.buckets[i]
	switch {
	case b.start.After(start):
		// A charge decided a horizon or more before the bucket's: one
		// that waited for the lock this long is past the horizon already.
		return
	case b.start.Before(start):
		b.start, b.charged = start, 0
	}
	b.charged = sat(b.charged, charged)
}

// over is what was charged within the horizon before now.
func (c *charges) over(now time.Time) int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.overLocked(now)
}

// overNow is what was charged within the horizon, and when: the clock read
// once the charges' lock is held, so no wait for the lock ages the sum.
func (c *charges) overNow(clock func() time.Time) (int64, time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := clock()
	return c.overLocked(now), now
}

func (c *charges) overLocked(now time.Time) int64 {
	from := now.Add(-c.width * time.Duration(len(c.buckets)))
	var t int64
	for _, b := range c.buckets {
		if b.start.After(from) && !b.start.After(now) {
			t = sat(t, b.charged)
		}
	}
	return t
}

// shardLocked is the owner's shard of key, made if it has none, with o.mu
// held.
func (o *Owner) shardLocked(key ShardKey) *shard {
	sh := o.shards[key]
	if sh == nil {
		sh = &shard{key: key}
		sh.charges.width = o.cfg.TopUps.Horizon / time.Duration(len(sh.charges.buckets))
		o.shards[key] = sh
	}
	return sh
}

// Admit admits a request for a shard under its oldest lease with room
// (§4.4), and asks for a top-up when the shard's room is low. With no lease
// that takes it, it answers ErrNoRoom, and the top-up it asks for is sized
// to take it too: the front door tries another shard's owner, or answers
// that the request should wait. A workspace the switch does not enable is
// ErrOff, with no lease asked for; each lease asks the switch again as it
// makes the hold.
func (o *Owner) Admit(key ShardKey, a Admission) (Admitted, error) {
	if err := a.valid(); err != nil {
		return Admitted{}, err
	}
	if !o.cfg.Enabled(key.Workspace) {
		return Admitted{}, ErrOff
	}
	o.mu.Lock()
	leases := slices.Clone(o.shardLocked(key).leases)
	o.mu.Unlock()
	for _, l := range leases {
		got, err := l.Admit(a)
		switch {
		case err == nil:
			o.topUp(key, false, 0)
			return got, nil
		case errors.Is(err, ErrNoRoom), errors.Is(err, ErrClosing), errors.Is(err, ErrPastCutoff),
			errors.Is(err, ErrPublishing):
		default:
			return Admitted{}, err
		}
	}
	o.topUp(key, true, sat(a.Estimate, o.cfg.overrun(a.Estimate)))
	return Admitted{}, ErrNoRoom
}

// Retire stops the owner taking anything new, as a node marked leaving
// does (spike plan §4, K3): it asks for no lease and takes none, a grant
// answered after it included, and each lease it holds admits nothing more
// (Lease.Close), serving its holds, its next checkpoint returning its free
// room and its final one, once no hold is open, ending it. A request it can
// no longer take is ErrNoRoom, for the front door to take elsewhere.
func (o *Owner) Retire() {
	o.mu.Lock()
	o.retiring = true
	leases := make([]*Lease, 0, len(o.leases))
	for _, l := range o.leases {
		leases = append(leases, l)
	}
	o.mu.Unlock()
	for _, l := range leases {
		l.Close()
	}
}

// topUp asks for another lease for the shard once its room is below the
// low-water mark, a request found no lease to take it (unmet, with need, the
// request's estimate and buffer, which the lease is sized for too), or an
// ask's answer was lost, room or not; one ask at a time, none within the
// cooldown of the last, none once the owner stops, and none for a workspace
// the switch does not enable, asked as the ask is made: a lost ask waits
// for it to be on again. The grant is a Spanner transaction off the
// request's path.
func (o *Owner) topUp(key ShardKey, unmet bool, need int64) {
	t := o.cfg.TopUps
	if t == (TopUps{}) {
		return
	}
	cooling := func(sh *shard, now time.Time) bool {
		return o.stopped || o.handoff != nil || o.retiring || sh.asking ||
			(!sh.askedAt.IsZero() && now.Sub(sh.askedAt) < t.Cooldown)
	}
	o.mu.Lock()
	sh := o.shardLocked(key)
	idle, lost := cooling(sh, o.cfg.Clock()), sh.retry != nil
	leases := slices.Clone(sh.leases)
	o.mu.Unlock()
	if idle {
		return
	}
	var room, needs int64
	for _, l := range leases {
		r, n := l.roomAndNeeds()
		room, needs = sat(room, r), sat(needs, n)
	}
	if room >= t.LowWater && !unmet && !lost {
		return
	}
	// The ask is timed by the clock read with every lock it needs held, so
	// however long the room, the owner's lock or the charges took, its
	// cooldown and its horizon start when it is made.
	o.mu.Lock()
	defer o.mu.Unlock()
	req, charged, now := sh.retry, int64(0), time.Time{}
	if req == nil {
		charged, now = sh.charges.overNow(o.cfg.Clock)
	} else {
		now = o.cfg.Clock()
	}
	if cooling(sh, now) || !o.cfg.Enabled(key.Workspace) {
		return
	}
	if req == nil {
		amount := min(max(sat(sat(charged, needs), need), t.Min), t.Max)
		req = &store.GrantRequest{Workspace: key.Workspace, LeaseID: store.NewLeaseID(), Region: key.Region,
			WorkspaceShard: key.Shard, Owner: o.who(), Amount: amount, KeyStatusVersion: o.cfg.KeyStatus}
	}
	sh.asking, sh.askedAt = true, now
	o.grants.Add(1)
	go o.grant(sh, *req)
}

// grant asks Spanner for a lease for the shard, and takes it. An ask whose
// answer is lost may have been granted: the shard's next ask is the same,
// lease ID and all, at its next admission or renewal round, which the store
// answers with the lease it granted, so none is granted and left unheld.
func (o *Owner) grant(sh *shard, req store.GrantRequest) {
	defer o.grants.Done()
	got, err := o.cfg.Spanner.Grant(o.ctx, req)
	if err == nil && got.Refused == "" {
		_, _ = o.take(req.LeaseID, req.Workspace, req.Amount, got.Expiry, sh)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	sh.asking, sh.retry = false, nil
	if err != nil {
		sh.retry = &req
	}
}

// roomAndNeeds is what a lease adds to its shard's room, while it admits
// (not closing, its publishes not failing, within its cutoff, neither idle
// nor old, by the clock read under its lock), its allocation beyond what it
// consumed, holds and buffers (its free room with pending put back), none
// if its buffers took it all; and to a top-up's size, its open holds and
// their buffer, whether it admits or not.
func (l *Lease) roomAndNeeds() (room, needs int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.o.cfg.Clock()
	b := l.booksLocked()
	if !l.closing && !l.failed && !l.unadopted && l.withinCutoff(now) &&
		!l.o.cfg.TopUps.over(now, l.lastAdmit, l.takenAt) {
		if used, ok := add(b.Consumed, b.Held, b.Buffer); ok {
			room = max(b.Allocation-used, 0)
		}
	}
	return room, sat(b.Held, b.Buffer)
}

// retryLost asks again, at a renewal round, for each shard's lease whose
// answer was lost, so a lease the store granted is held though no request
// comes.
func (o *Owner) retryLost() {
	o.mu.Lock()
	var lost []ShardKey
	for key, sh := range o.shards {
		if sh.retry != nil {
			lost = append(lost, key)
		}
	}
	o.mu.Unlock()
	for _, key := range lost {
		o.topUp(key, false, 0)
	}
}

// over: a lease that has admitted nothing for IdleAfter, or is MaxLife old,
// admits nothing more.
func (t TopUps) over(now, lastAdmit, takenAt time.Time) bool {
	return t != (TopUps{}) && (now.Sub(lastAdmit) >= t.IdleAfter || now.Sub(takenAt) >= t.MaxLife)
}

// closeIfDone stops admitting under a lease gone idle or old (TopUps.over),
// at each renewal round; an admission checks too.
func (l *Lease) closeIfDone(now time.Time, t TopUps) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if t.over(now, l.lastAdmit, l.takenAt) {
		l.closing = true
	}
}

// sat adds two amounts, each at least 0, saturating at the largest.
func sat(a, b int64) int64 {
	if s, ok := add(a, b); ok {
		return s
	}
	return math.MaxInt64
}
