package owner

import (
	"errors"
	"math"
	"slices"
	"sync/atomic"
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
	return nil
}

// shard is a shard's leases at the owner, oldest first; whether an ask for a
// top-up is outstanding and when the last began; and what its leases have
// charged, ever, with samples of it over the horizon for its rate.
type shard struct {
	key     ShardKey
	leases  []*Lease
	asking  bool
	askedAt time.Time
	charged atomic.Int64
	samples []sample
}

type sample struct {
	at      time.Time
	charged int64
}

// shardLocked is the owner's shard of key, made if it has none, with o.mu
// held.
func (o *Owner) shardLocked(key ShardKey) *shard {
	sh := o.shards[key]
	if sh == nil {
		sh = &shard{key: key}
		o.shards[key] = sh
	}
	return sh
}

// Admit admits a request for a shard under its oldest lease with room
// (§4.4), and asks for a top-up when the shard's room is low. With no lease
// that takes it, it answers ErrNoRoom: the front door tries another shard's
// owner, or answers that the request should wait.
func (o *Owner) Admit(key ShardKey, a Admission) (Admitted, error) {
	o.mu.Lock()
	leases := slices.Clone(o.shardLocked(key).leases)
	o.mu.Unlock()
	defer o.topUp(key)
	for _, l := range leases {
		got, err := l.Admit(a)
		switch {
		case err == nil:
			return got, nil
		case errors.Is(err, ErrNoRoom), errors.Is(err, ErrClosing), errors.Is(err, ErrPastCutoff),
			errors.Is(err, ErrPublishing):
		default:
			return Admitted{}, err
		}
	}
	return Admitted{}, ErrNoRoom
}

// topUp asks for another lease for the shard once its room is below the
// low-water mark, unless an ask is outstanding or the last began within the
// cooldown. The grant is a Spanner transaction off the request's path.
func (o *Owner) topUp(key ShardKey) {
	t := o.cfg.TopUps
	if t == (TopUps{}) {
		return
	}
	now := o.cfg.Clock()
	o.mu.Lock()
	sh := o.shardLocked(key)
	cooling := sh.asking || (!sh.askedAt.IsZero() && now.Sub(sh.askedAt) < t.Cooldown)
	leases := slices.Clone(sh.leases)
	o.mu.Unlock()
	if cooling {
		return
	}
	var room, needs int64
	for _, l := range leases {
		r, n := l.roomAndNeeds(now)
		room, needs = sat(room, r), sat(needs, n)
	}
	if room >= t.LowWater {
		return
	}
	o.mu.Lock()
	if sh.asking || (!sh.askedAt.IsZero() && now.Sub(sh.askedAt) < t.Cooldown) {
		o.mu.Unlock()
		return
	}
	sh.asking, sh.askedAt = true, now
	amount := min(max(sat(sh.chargedOver(), needs), t.Min), t.Max)
	o.mu.Unlock()
	go o.grant(sh, amount)
}

// grant asks Spanner for a lease of amount for the shard, and takes it.
func (o *Owner) grant(sh *shard, amount int64) {
	id := store.NewLeaseID()
	got, err := o.cfg.Spanner.Grant(o.ctx, store.GrantRequest{Workspace: sh.key.Workspace, LeaseID: id,
		Region: sh.key.Region, WorkspaceShard: sh.key.Shard, Owner: o.who(), Amount: amount,
		KeyStatusVersion: o.cfg.KeyStatus})
	if err == nil && got.Refused == "" {
		_, _ = o.take(id, sh.key.Workspace, amount, got.Expiry, sh)
	}
	o.mu.Lock()
	sh.asking = false
	o.mu.Unlock()
}

// roomAndNeeds is what a lease adds to its shard's room, its free room with
// pending put back while it admits, and to a top-up's size, its open holds
// and their buffer.
func (l *Lease) roomAndNeeds(now time.Time) (room, needs int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	b := l.booksLocked()
	if !l.closing && l.withinCutoff(now) {
		if free := b.Free(); free != math.MinInt64 {
			room = max(sat(free, b.Pending), 0)
		}
	}
	return room, sat(b.Held, b.Buffer)
}

// closeIfDone stops admitting under a lease that has admitted nothing for
// IdleAfter or is MaxLife old.
func (l *Lease) closeIfDone(now time.Time, t TopUps) {
	if t == (TopUps{}) {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if now.Sub(l.lastAdmit) >= t.IdleAfter || now.Sub(l.takenAt) >= t.MaxLife {
		l.closing = true
	}
}

// sampleShards records each shard's charges for its rate, keeping one sample
// at or before the horizon's start as the base.
func (o *Owner) sampleShards(now time.Time) {
	t := o.cfg.TopUps
	if t == (TopUps{}) {
		return
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	for _, sh := range o.shards {
		sh.samples = append(sh.samples, sample{at: now, charged: sh.charged.Load()})
		for len(sh.samples) > 1 && !sh.samples[1].at.After(now.Add(-t.Horizon)) {
			sh.samples = sh.samples[1:]
		}
	}
}

// chargedOver is what the shard charged since its base sample, with o.mu
// held.
func (sh *shard) chargedOver() int64 {
	if len(sh.samples) == 0 {
		return 0
	}
	return max(sh.charged.Load()-sh.samples[0].charged, 0)
}

// sat adds two amounts, each at least 0, saturating at the largest.
func sat(a, b int64) int64 {
	if s, ok := add(a, b); ok {
		return s
	}
	return math.MaxInt64
}
