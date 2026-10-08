package owner

import (
	"context"
	"errors"
	"time"
)

// The flusher's wait between a failure and the republish, doubling to its
// longest while publishes keep failing.
const (
	firstBackoff = 50 * time.Millisecond
	lastBackoff  = 2 * time.Second
)

// errNotSent is the publish of a record handed over while records before it
// awaited their republish: the flusher's republish sends it, in its order.
var errNotSent = errors.New("owner: not sent yet")

type notSent struct{}

func (notSent) Wait(context.Context) (string, error) { return "", errNotSent }

// flush is a lease's one flusher. It waits for the lease's records'
// acknowledgements in their order: each acknowledged one frees what it
// freed, and is answered. A failed publish pauses the lease's key, so every
// record after it fails too, and nothing new is admitted (§4.5). The flusher
// then resumes the key and republishes the same records, with their
// numbers, from the first not acknowledged, before anything new, each one
// only before the lease's cutoff, so every publish of the owner's is
// received by the cutoff plus the publish deadline (§4.8). Past the cutoff
// it keeps the records and waits: a renewal that moves the cutoff lets it
// republish them, still before anything new, and Let ends it.
func (l *Lease) flush() {
	defer close(l.stopped)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		<-l.stop
		cancel()
	}()
	backoff := firstBackoff
	for {
		l.mu.Lock()
		var s *sent
		if len(l.inflight) > 0 {
			s = l.inflight[0]
		}
		l.mu.Unlock()
		if s == nil {
			select {
			case <-l.kick:
				continue
			case <-l.stop:
				return
			}
		}
		_, err := s.waiter.Wait(ctx)
		if ctx.Err() != nil {
			return
		}
		l.mu.Lock()
		if err == nil {
			now := l.o.cfg.Clock()
			s.acked, s.ackedAt, s.beforeCutoff = true, now, l.withinCutoff(now)
			l.pending -= s.freed
			l.inflight = l.inflight[1:]
			l.live--
			l.failed = false
			close(s.done)
			l.mu.Unlock()
			backoff = firstBackoff
			continue
		}
		// Every record not acknowledged awaits its republish now.
		l.failed, l.live = true, 0
		within := l.withinCutoff(l.o.cfg.Clock())
		l.mu.Unlock()
		if !within {
			// Past the cutoff: wait for a renewal, or the end.
			backoff = firstBackoff
			select {
			case <-l.kick:
				continue
			case <-l.stop:
				return
			}
		}
		select {
		case <-time.After(backoff):
		case <-l.stop:
			return
		}
		backoff = min(2*backoff, lastBackoff)
		l.republish()
	}
}

// republish resumes the lease's key and hands it every record not
// acknowledged again, in order, each only before the cutoff. A record the
// cutoff stops waits, and keeps every record after it waiting too.
func (l *Lease) republish() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if len(l.inflight) == 0 || !l.withinCutoff(l.o.cfg.Clock()) {
		return
	}
	l.o.pub.Resume(l.id)
	for _, x := range l.inflight {
		if !l.withinCutoff(l.o.cfg.Clock()) {
			return
		}
		x.waiter = l.o.pub.Publish(l.id, x.data)
		l.live++
	}
}
