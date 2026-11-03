package owner

import (
	"sync"
	"time"
)

// slowAnswer is the wait past which a request counts as slow: the front
// door gives an owner this long (frontdoor.Config.OwnerWait) before it
// takes the owner for one not reached.
const slowAnswer = 500 * time.Millisecond

// Tally is what the owner's leases did over a period, for its log line: the
// records acknowledged and how long each waited from its hand-over to its
// acknowledgement, a republished one from its first hand-over; the most
// records a lease awaited at once; the publishes that failed; and how long
// heartbeats and terminals took to answer, from the call to its answer.
// It says where an owner's answer spends its time (2026-11-02: an owner
// node's answers passed OwnerWait while the edge answered streams, with the
// store's transactions in milliseconds).
type Tally struct {
	Acks, AcksSlow   int64
	AckTotal, AckMax time.Duration
	DepthMax         int
	PublishFailures  int64

	Heartbeats, HeartbeatsSlow int64
	HeartbeatMax               time.Duration
	Terminals, TerminalsSlow   int64
	TerminalMax                time.Duration
}

// tally is the owner's period tally, under its own lock: it is kept from
// under a lease's lock, and read by the log's ticker. It times by now, the
// process's clock unless a test sets one, never the owner's: a reading of
// the owner's clock is part of what a decision does, and a test may hold
// or count those readings.
type tally struct {
	mu  sync.Mutex
	t   Tally
	now func() time.Time
}

func (t *tally) clock() time.Time {
	if t.now != nil {
		return t.now()
	}
	return time.Now()
}

func (t *tally) ack(waited time.Duration) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.Acks++
	t.t.AckTotal += waited
	t.t.AckMax = max(t.t.AckMax, waited)
	if waited > slowAnswer {
		t.t.AcksSlow++
	}
}

func (t *tally) depth(n int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.DepthMax = max(t.t.DepthMax, n)
}

func (t *tally) publishFailed() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.PublishFailures++
}

func (t *tally) heartbeat(took time.Duration) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.Heartbeats++
	t.t.HeartbeatMax = max(t.t.HeartbeatMax, took)
	if took > slowAnswer {
		t.t.HeartbeatsSlow++
	}
}

func (t *tally) terminal(took time.Duration) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.Terminals++
	t.t.TerminalMax = max(t.t.TerminalMax, took)
	if took > slowAnswer {
		t.t.TerminalsSlow++
	}
}

// TakeTally returns the tally since the last one taken, and starts the
// next.
func (o *Owner) TakeTally() Tally {
	o.tally.mu.Lock()
	defer o.tally.mu.Unlock()
	t := o.tally.t
	o.tally.t = Tally{}
	return t
}
