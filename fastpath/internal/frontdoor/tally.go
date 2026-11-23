package frontdoor

import (
	"sync"
	"time"
)

// Tally is what a front door's terminals did over a period, for its log
// line: the settles whose full record was acknowledged, the longest of
// those waits, and the ones past the publish's share (PublishWait), which
// took their time from the owner's and the peer's waits; the calls to
// owners and peers the budget left less than their wait that did not
// answer, which are no evidence about whom they called (hop); and the
// terminals answered Failed, by why. A Failed terminal is retried by the
// control plane, from its drain if not at once, so the line is what names
// its cause (2026-11-23: settles answered Failed at a publish's 250 ms
// wait, with nothing on the node to say so).
type Tally struct {
	Published, PublishedLate int64
	PublishMax               time.Duration
	Short                    int64
	Failed                   Failures
}

// Failures are terminals answered Failed, by why: a settle's full record
// not acknowledged within PublishMax or the budget (PublishTimeout), or
// failed (PublishError); the gateway's request ended first (Gone); the
// owner answered Failed (Owner); the drain log's append failed (Append); or
// a refused append's disposition read failed (Disposition).
type Failures struct {
	PublishTimeout, PublishError, Gone, Owner, Append, Disposition int64
}

// why is a Failures field's name.
type why int

const (
	failedPublishTimeout why = iota
	failedPublishError
	failedGone
	failedOwner
	failedAppend
	failedDisposition
)

// tally is the front door's period tally, under its own lock, apart from
// the evidence's: it is kept on every terminal and read by the node's
// ticker.
type tally struct {
	mu sync.Mutex
	t  Tally
}

func (t *tally) published(took time.Duration, late bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.Published++
	t.t.PublishMax = max(t.t.PublishMax, took)
	if late {
		t.t.PublishedLate++
	}
}

func (t *tally) short() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.t.Short++
}

func (t *tally) failed(w why) {
	t.mu.Lock()
	defer t.mu.Unlock()
	f := &t.t.Failed
	switch w {
	case failedPublishTimeout:
		f.PublishTimeout++
	case failedPublishError:
		f.PublishError++
	case failedGone:
		f.Gone++
	case failedOwner:
		f.Owner++
	case failedAppend:
		f.Append++
	case failedDisposition:
		f.Disposition++
	}
}

// TakeTally returns the tally since the last one taken, and starts the
// next.
func (f *FrontDoor) TakeTally() Tally {
	f.tally.mu.Lock()
	defer f.tally.mu.Unlock()
	t := f.tally.t
	f.tally.t = Tally{}
	return t
}
