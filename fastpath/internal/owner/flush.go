package owner

import (
	"context"
	"time"
)

// The flusher's wait between a failure and the republish, doubling to its
// longest while publishes keep failing.
const (
	firstBackoff = 50 * time.Millisecond
	lastBackoff  = 2 * time.Second
)

// flush is a lease's one flusher. It waits for the lease's records'
// acknowledgements in their order: each acknowledged one frees what it
// freed, and is answered. A failed publish pauses the lease's key, so every
// record after it fails too, and nothing new is admitted (§4.5). The flusher
// then resumes the key and republishes the same records, with their
// numbers, from the first not acknowledged, before anything new. It never
// republishes past the lease's cutoff, so every publish of the owner's is
// received by the cutoff plus the publish deadline (§4.8): records still not
// acknowledged then are given up, and their terminals are answered retry and
// go to the drain log.
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
			l.failed = false
			close(s.done)
			l.mu.Unlock()
			backoff = firstBackoff
			continue
		}
		l.failed = true
		if !l.withinCutoff(l.o.cfg.Clock()) {
			for _, x := range l.inflight {
				close(x.done)
			}
			l.inflight = nil
			l.mu.Unlock()
			continue
		}
		l.mu.Unlock()
		select {
		case <-time.After(backoff):
		case <-l.stop:
			return
		}
		backoff = min(2*backoff, lastBackoff)
		l.mu.Lock()
		if l.withinCutoff(l.o.cfg.Clock()) {
			l.o.pub.Resume(l.id)
			for _, x := range l.inflight {
				x.waiter = l.o.pub.Publish(l.id, x.data)
			}
		}
		l.mu.Unlock()
	}
}
