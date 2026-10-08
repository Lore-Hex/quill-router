package ring

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Watcher keeps a front door's view of the members, read every interval in
// one strong read. A read that fails keeps the last view; View returns it
// with the local time the read that made it began, so a front door can
// refuse a view too old to route by, its read's own latency counted.
type Watcher struct {
	m        Membership
	interval time.Duration

	// ctx ends when Stop begins, and with it a read under way.
	ctx    context.Context
	cancel context.CancelFunc

	mu     sync.Mutex
	view   View
	readAt time.Time

	done chan struct{}
}

// Watch reads the members once, and then every interval until stopped.
func Watch(ctx context.Context, m Membership, interval time.Duration) (*Watcher, error) {
	if interval <= 0 {
		return nil, fmt.Errorf("ring: a watch interval of %v", interval)
	}
	w := &Watcher{m: m, interval: interval, done: make(chan struct{})}
	if err := w.read(ctx); err != nil {
		return nil, err
	}
	w.ctx, w.cancel = context.WithCancel(context.Background())
	go w.run()
	return w, nil
}

// View is the latest view, a copy the caller may keep or change, and the
// local time the read that made it began.
func (w *Watcher) View() (View, time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.view.clone(), w.readAt
}

// Stop ends any read under way, starts no other, and waits for the reads
// to end.
func (w *Watcher) Stop() {
	w.cancel()
	<-w.done
}

func (w *Watcher) run() {
	defer close(w.done)
	ticker := time.NewTicker(w.interval)
	defer ticker.Stop()
	for {
		select {
		case <-w.ctx.Done():
			return
		case <-ticker.C:
		}
		// A tick and the stop can be ready together: the stop wins.
		if w.ctx.Err() != nil {
			return
		}
		ctx, cancel := context.WithTimeout(w.ctx, w.interval)
		_ = w.read(ctx)
		cancel()
	}
}

func (w *Watcher) read(ctx context.Context) error {
	began := time.Now()
	members, at, err := w.m.Members(ctx)
	if err != nil {
		return err
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.view, w.readAt = View{Members: members, ReadAt: at}, began
	return nil
}
