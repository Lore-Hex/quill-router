package service

import (
	"context"
	"log"
	"sync"
	"time"
)

// workspaceSwitch is the node's copy of the fast path's switch,
// tr_fastpath_workspace (docs/design/fast-admission-production-rollout.md,
// W1): the workspaces enabled for the fast path, read every interval. The
// front door and the owner's own route admit only for a workspace it has.
// A copy older than three intervals, by the node's clock from the start of
// the read that made it, enables none, so a node that cannot read the
// switch admits for no workspace. The store refuses a grant, and revokes the
// leases, of a workspace turned off, whatever a node's copy says.
type workspaceSwitch struct {
	read  func(context.Context) (map[string]bool, time.Time, error)
	every time.Duration
	clock func() time.Time

	mu      sync.Mutex
	enabled map[string]bool
	at      time.Time
}

// Enabled says whether the workspace is enabled, by a copy no older than
// three intervals.
func (w *workspaceSwitch) Enabled(workspace string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.enabled[workspace] && !w.at.IsZero() && w.clock().Sub(w.at) < 3*w.every
}

// refresh reads the switch, and takes what it read as of the read's start.
func (w *workspaceSwitch) refresh(ctx context.Context) error {
	started := w.clock()
	got, _, err := w.read(ctx)
	if err != nil {
		return err
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.enabled, w.at = got, started
	return nil
}

// run refreshes the copy every interval until ctx ends; a read that fails
// leaves the copy to age, and is logged.
func (w *workspaceSwitch) run(ctx context.Context) {
	t := time.NewTicker(w.every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			rctx, cancel := context.WithTimeout(ctx, w.every)
			if err := w.refresh(rctx); err != nil && ctx.Err() == nil {
				log.Printf("service: reading the fast path's switch: %v", err)
			}
			cancel()
		}
	}
}
