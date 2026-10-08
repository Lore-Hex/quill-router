// Package owner is a lease's owner in memory (design §4.2, §4.3, §4.5; spike
// plan §2): the books of each lease the process was granted, its open holds,
// and the one order of its records. An admission, a heartbeat's record and a
// terminal's decision each take the lease's lock: the decision, the record's
// owner sequence number and the books move together, and the record is
// handed to the lease's ordering key under the lock, so number n + 1 is never
// handed over before n. Nothing waits under the lock: an answer waits for its
// record's acknowledgement after it is released.
//
// Its Spanner side (spanner.go) renews the leases, checkpoints them, stores
// their shortfall totals, and ends a lease that stopped admitting. Grants and
// top-ups, adoption, the owner's reaper, the release and the forced exit come
// in later parts of S5.
package owner

import (
	"context"
	"errors"
	"fmt"
	"math"
	"slices"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// Publisher hands a lease's records to its ordering key, in the order of the
// calls, as settlelog.Log does; Resume lets a key paused by a failed publish
// take records again.
type Publisher interface {
	Publish(lease string, data []byte) Waiter
	Resume(lease string)
}

// Waiter is one publish: its acknowledgement, or its failure.
type Waiter interface {
	Wait(ctx context.Context) (string, error)
}

// Config is an owner's.
type Config struct {
	// Epoch is the process's, which its leases carry.
	Epoch int64
	// Skew is the skew allowance: the owner admits, decides and publishes
	// under a lease only while its clock is before the expiry less it.
	Skew time.Duration
	// AnswerWait bounds how long an answer waits for its record's
	// acknowledgement before it answers retry.
	AnswerWait time.Duration
	// HoldLife is a hold's longest life from its admission, its end of
	// life; HeartbeatEvery the deadline each heartbeat's answer grants.
	HoldLife       time.Duration
	HeartbeatEvery time.Duration
	// Overrun is what a hold of an estimate is expected to overrun by, its
	// part of the lease's buffer (§4.2). Nil is no buffer.
	Overrun func(estimate int64) int64
	// NewAuthorization mints an authorization ID that names its lease
	// (§4.9).
	NewAuthorization func(lease string) (string, error)
	// Clock is the owner's clock.
	Clock func() time.Time

	// Spanner is the store an owner renews its leases in and writes their
	// shortfalls and draining to, nil for one that writes nothing there.
	// Node is the owner's address, which with Epoch names it in the store's
	// conditions; it renews every RenewEvery; Window is the leases' expiry
	// window, after which an owner whose publishes for a lease keep failing
	// stops renewing it (§4.2); KeyStatus is the key-status version it
	// applies (§4.6), fixed in the spike.
	Spanner    Spanner
	Node       string
	RenewEvery time.Duration
	Window     time.Duration
	KeyStatus  int64
	// TopUps is how the owner keeps its shards' leases (shard.go); zero, it
	// asks for none, and admits only under the leases it was given.
	TopUps TopUps
}

func (c Config) validate() error {
	if c.Epoch < 1 || c.Skew <= 0 || c.AnswerWait <= 0 || c.HoldLife <= 0 || c.HeartbeatEvery <= 0 ||
		c.NewAuthorization == nil || c.Clock == nil || c.KeyStatus < 0 {
		return errors.New("owner: an epoch, positive durations, an authorization minter and a clock")
	}
	if c.Spanner != nil && (c.Node == "" || c.RenewEvery <= 0 || c.Window <= 0) {
		return errors.New("owner: a store needs the owner's address, a renewal interval and the expiry window")
	}
	if c.TopUps != (TopUps{}) {
		if c.Spanner == nil {
			return errors.New("owner: top-ups need a store")
		}
		return c.TopUps.validate()
	}
	return nil
}

func (c Config) overrun(estimate int64) int64 {
	if c.Overrun == nil {
		return 0
	}
	return max(0, c.Overrun(estimate))
}

// add sums amounts of money, each at least 0, and reports whether an int64
// holds the sum.
func add(xs ...int64) (int64, bool) {
	var t int64
	for _, x := range xs {
		if x < 0 || x > math.MaxInt64-t {
			return 0, false
		}
		t += x
	}
	return t, true
}

// Owner holds the leases its process was granted. A process uses only those
// (§4.3): it never takes up a lease it was not granted, and answers a request
// that names one as an owner past its cutoff does.
type Owner struct {
	cfg Config
	pub Publisher

	// ctx ends when the owner stops; writers are its shortfall writers,
	// which outlive the leases they write for.
	ctx     context.Context
	cancel  context.CancelFunc
	writers sync.WaitGroup

	mu      sync.Mutex
	stopped bool
	leases  map[string]*Lease
	// retired are the leases the owner let go, each with its workers' end:
	// none is taken again, since a lease's records are numbered once.
	retired map[string]<-chan struct{}
	// shards are the shards the owner admits for, with their leases.
	shards map[ShardKey]*shard
}

// New starts an owner with no leases.
func New(cfg Config, pub Publisher) (*Owner, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	if pub == nil {
		return nil, errors.New("owner: no publisher")
	}
	o := &Owner{cfg: cfg, pub: pub, leases: map[string]*Lease{}, retired: map[string]<-chan struct{}{},
		shards: map[ShardKey]*shard{}}
	o.ctx, o.cancel = context.WithCancel(context.Background())
	return o, nil
}

// Stop ends the owner: it lets every lease go, and its shortfall writers end,
// leaving any total they did not store to the auditor's commits.
func (o *Owner) Stop() {
	o.cancel()
	o.mu.Lock()
	o.stopped = true
	ids := make([]string, 0, len(o.leases))
	for id := range o.leases {
		ids = append(ids, id)
	}
	o.mu.Unlock()
	for _, id := range ids {
		o.Let(id)
	}
	o.writers.Wait()
}

// Take puts a lease granted to this process under its care, with the
// allocation the grant reserved and the expiry Spanner set.
func (o *Owner) Take(lease, workspace string, allocation int64, expiry time.Time) (*Lease, error) {
	return o.take(lease, workspace, allocation, expiry, nil)
}

// take is Take, the lease joining sh's leases when sh is set.
func (o *Owner) take(lease, workspace string, allocation int64, expiry time.Time, sh *shard) (*Lease, error) {
	if !record.ValidLease(lease) || workspace == "" || allocation < 0 {
		return nil, fmt.Errorf("owner: a lease %q of %q, allocation %d", lease, workspace, allocation)
	}
	now := o.cfg.Clock()
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.stopped {
		return nil, errors.New("owner: stopped")
	}
	if _, ok := o.leases[lease]; ok {
		return nil, fmt.Errorf("owner: lease %s is held already", lease)
	}
	if o.retired[lease] != nil {
		return nil, fmt.Errorf("owner: lease %s was let go", lease)
	}
	l := &Lease{o: o, id: lease, workspace: workspace, allocation: allocation, expiry: expiry, nextSeq: 1,
		holds: map[string]*hold{}, decided: map[string]*decision{}, kick: make(chan struct{}, 1),
		shortKick: make(chan struct{}, 1), stop: make(chan struct{}), stopped: make(chan struct{}),
		shard: sh, takenAt: now, lastAdmit: now}
	o.leases[lease] = l
	if sh != nil {
		sh.leases = append(sh.leases, l)
	}
	l.workers.Add(1)
	go func() {
		defer l.workers.Done()
		l.flush()
	}()
	go func() {
		l.workers.Wait()
		close(l.stopped)
	}()
	if o.cfg.Spanner != nil {
		// The shortfall writer outlives the lease: it stops once it has
		// stored all the owner decided, or Spanner refuses, or the owner
		// stops (§4.2).
		o.writers.Add(1)
		go func() {
			defer o.writers.Done()
			l.writeShortfalls()
		}()
	}
	return l, nil
}

// Lease returns a lease this process holds.
func (o *Owner) Lease(id string) (*Lease, bool) {
	o.mu.Lock()
	defer o.mu.Unlock()
	l, ok := o.leases[id]
	return l, ok
}

// Let lets a lease go: the owner holds it no more and never takes it again,
// its flusher and the finish of its draining stop, and a request that
// reaches it through a handle kept from before is answered as one past its
// cutoff (§4.3). Every caller returns once they have stopped. Its shortfall
// writer goes on until it has stored what the owner decided.
func (o *Owner) Let(id string) {
	if stopped := o.release(id); stopped != nil {
		<-stopped
	}
}

// release begins letting a lease go, and returns its workers' end without
// waiting for it.
func (o *Owner) release(id string) <-chan struct{} {
	o.mu.Lock()
	l, ok := o.leases[id]
	if ok {
		delete(o.leases, id)
		o.retired[id] = l.stopped
		if l.shard != nil {
			l.shard.leases = slices.DeleteFunc(l.shard.leases, func(x *Lease) bool { return x == l })
		}
	}
	stopped := o.retired[id]
	o.mu.Unlock()
	if ok {
		l.mu.Lock()
		l.let = true
		l.mu.Unlock()
		close(l.stop)
	}
	return stopped
}

// hold is an open hold, with the boot binding its envelope carries, which a
// refund's record names (§4.9).
type hold struct {
	auth      string
	estimate  int64
	overrun   int64
	endOfLife time.Time
	stream    bool
	boot      []byte
	// The latest valid heartbeat: the gateway's sequence and hash, the
	// usage, the running charge, the deadline granted and the one it
	// echoed, and the owner sequence number of its record; heartbeat is
	// whether one was issued.
	heartbeat  bool
	echoed     time.Time
	gatewaySeq int64
	hash       []byte
	usage      int64
	running    int64
	deadline   time.Time
	snapSeq    int64
	sent       *sent
}

// decision is an authorization's terminal: its kind and charge, and the
// record that carries it.
type decision struct {
	kind   record.Kind
	charge int64
	sent   *sent
}

// sent is a record handed over: its bytes, so a failure can republish the
// same record, what it frees once acknowledged, the waiter of its latest
// publish, and done, closed once it is acknowledged (at ackedAt, before the
// cutoff or not).
type sent struct {
	seq          int64
	data         []byte
	freed        int64
	waiter       Waiter
	done         chan struct{}
	acked        bool
	ackedAt      time.Time
	beforeCutoff bool
}

// Lease is one lease under this process's care.
type Lease struct {
	o         *Owner
	id        string
	workspace string

	mu sync.Mutex
	// The books (§4.2): the allocation (L, less returns, plus the
	// shortfall), what open holds hold, what was booked, what decided
	// terminals freed whose publishes are not acknowledged, the buffer of
	// the open holds, and the shortfall total.
	allocation int64
	held       int64
	consumed   int64
	pending    int64
	buffer     int64
	shortfall  int64
	expiry     time.Time
	nextSeq    int64
	holds      map[string]*hold
	decided    map[string]*decision
	// inflight are the records handed over and not acknowledged, in order,
	// the first live of them published since the last failure, and the rest
	// awaiting the flusher's republish; failed is set from a failed publish
	// until a publish is acknowledged again. let is set once the owner let
	// the lease go.
	inflight []*sent
	live     int
	failed   bool
	let      bool
	// failedAt is when the lease's publishes began failing, zero while they
	// succeed. closing is set once the lease admits nothing more, and final
	// is its final checkpoint's record once handed over. stored is the
	// shortfall total Spanner has from the owner's writes.
	failedAt time.Time
	closing  bool
	final    *sent
	stored   int64
	// shard is the shard whose leases it is among, if any; takenAt and
	// lastAdmit are when it was taken and last admitted a hold.
	shard     *shard
	takenAt   time.Time
	lastAdmit time.Time

	// workers are the lease's flusher and the finish of its draining, which
	// Let waits for: stopped is closed once they end.
	workers   sync.WaitGroup
	kick      chan struct{}
	shortKick chan struct{}
	stop      chan struct{}
	stopped   chan struct{}
}

// ID is the lease's.
func (l *Lease) ID() string { return l.id }

// Books is a lease's books at one moment.
type Books struct {
	Allocation, Held, Consumed, Pending, Buffer, Shortfall int64
	NextSeq                                                int64
	Open                                                   int
}

// Remaining is the allocation less what is booked and held; Free, Remaining
// less pending and the buffer, is what admission reads, and none if those
// four pass an int64.
func (b Books) Remaining() int64 { return b.Allocation - b.Consumed - b.Held }
func (b Books) Free() int64 {
	used, ok := add(b.Consumed, b.Held, b.Pending, b.Buffer)
	if !ok {
		return math.MinInt64
	}
	return b.Allocation - used
}

// Books reads the lease's books under its lock.
func (l *Lease) Books() Books {
	l.mu.Lock()
	defer l.mu.Unlock()
	return Books{Allocation: l.allocation, Held: l.held, Consumed: l.consumed, Pending: l.pending, Buffer: l.buffer,
		Shortfall: l.shortfall, NextSeq: l.nextSeq, Open: len(l.holds)}
}

// Renewed takes a renewal's answer: the expiry Spanner holds now. A renewal
// that moves a cutoff that has passed lets the flusher republish what waits,
// before anything new; the owner adopts the lease's drain log before it
// calls Renewed so (§4.2), in a later part of S5. It never brings back a
// lease the owner let go.
func (l *Lease) Renewed(expiry time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.let || !expiry.After(l.expiry) {
		return
	}
	l.expiry = expiry
	select {
	case l.kick <- struct{}{}:
	default:
	}
}

// withinCutoff: the owner holds the lease and its clock is before the
// lease's expiry less the skew (LeaseLifecycle's WithinCutoff, with the
// owner's one reading).
func (l *Lease) withinCutoff(now time.Time) bool {
	return !l.let && now.Add(l.o.cfg.Skew).Before(l.expiry)
}
