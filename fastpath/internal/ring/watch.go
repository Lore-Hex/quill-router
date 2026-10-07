package ring

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Watcher keeps a front door's view of the members, read every interval in
// one strong read. A read that fails keeps the last view; View returns it
// with the local time it was read, so a front door can refuse one too old
// to route by.
type Watcher struct {
	m        Membership
	interval time.Duration

	mu     sync.Mutex
	view   View
	readAt time.Time

	stop     chan struct{}
	done     chan struct{}
	stopOnce sync.Once
}

// Watch reads the members once, and then every interval until stopped.
func Watch(ctx context.Context, m Membership, interval time.Duration) (*Watcher, error) {
	if interval <= 0 {
		return nil, fmt.Errorf("ring: a watch interval of %v", interval)
	}
	w := &Watcher{m: m, interval: interval, stop: make(chan struct{}), done: make(chan struct{})}
	if err := w.read(ctx); err != nil {
		return nil, err
	}
	go w.run()
	return w, nil
}

// View is the latest view, and the local time of the read that made it.
func (w *Watcher) View() (View, time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.view, w.readAt
}

// Stop stops the reads and waits for the last to end.
func (w *Watcher) Stop() {
	w.stopOnce.Do(func() { close(w.stop) })
	<-w.done
}

func (w *Watcher) run() {
	defer close(w.done)
	ticker := time.NewTicker(w.interval)
	defer ticker.Stop()
	for {
		select {
		case <-w.stop:
			return
		case <-ticker.C:
		}
		ctx, cancel := context.WithTimeout(context.Background(), w.interval)
		_ = w.read(ctx)
		cancel()
	}
}

func (w *Watcher) read(ctx context.Context) error {
	members, at, err := w.m.Members(ctx)
	if err != nil {
		return err
	}
	local := time.Now()
	w.mu.Lock()
	defer w.mu.Unlock()
	w.view, w.readAt = View{Members: members, ReadAt: at}, local
	return nil
}
