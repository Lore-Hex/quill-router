package edge

import (
	"context"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
)

// The window check of an enforcing windowed key (docs/design/
// fast-admission-e2-key-windows.md sections 4.2 to 4.4): Decide composes the
// key's decision with its cached limits and shard count (Decision.Windows);
// the decide path, and the shadow off the request path, read the key's
// tr_key_limit rows once and evaluate Python's decide_key_window_limits over
// them at the request's instant. Only an allowed verdict admits; every other
// one passes to Python, which answers the 429, replays, or admits.

// KeyWindowReader reads a key's tr_key_limit rows of shards 0 to shards-1
// (store.Store.ReadKeyWindows).
type KeyWindowReader interface {
	ReadKeyWindows(ctx context.Context, keyHash string, shards int) ([]keywindow.Row, error)
}

// windowRead is one window check: its verdict, whether the read was made
// (no reader makes none), and how long it took.
type windowRead struct {
	verdict keywindow.Verdict
	made    bool
	took    time.Duration
}

// readWindows checks an enforcing windowed key's decision: one read of its
// rows, ending at deadline or WindowReadBudget from now, whichever comes
// first, evaluated at at, the request's one clock reading. A read that fails
// or runs past its deadline, or no reader, is key_window.unread.
func readWindows(ctx context.Context, reader KeyWindowReader, d *Decision, at, deadline time.Time) windowRead {
	if reader == nil {
		return windowRead{verdict: keywindow.Verdict{Pass: keywindow.PassUnread}}
	}
	if late := time.Now().Add(WindowReadBudget); late.Before(deadline) {
		deadline = late
	}
	readCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	start := time.Now()
	rows, err := reader.ReadKeyWindows(readCtx, d.KeyHash, d.Windows.Shards)
	w := windowRead{made: true, took: time.Since(start)}
	if err != nil {
		w.verdict = keywindow.Verdict{Pass: keywindow.PassUnread}
		return w
	}
	w.verdict = keywindow.Evaluate(d.Windows.Limits, d.Windows.Shards, rows, d.Estimate, at)
	return w
}

// windowLatency is a window read's duration as the counts bucket it: the
// rollout gate asks its p99 under 50 ms (section 8.3).
func windowLatency(took time.Duration) string {
	switch {
	case took <= 10*time.Millisecond:
		return "latency.le_10ms"
	case took <= 50*time.Millisecond:
		return "latency.le_50ms"
	case took <= 100*time.Millisecond:
		return "latency.le_100ms"
	case took <= WindowReadBudget:
		return "latency.le_300ms"
	}
	return "latency.gt_300ms"
}

// windowOutcome is a window check's verdict as the counts name it: admit, or
// its key_window.* reason.
func windowOutcome(v keywindow.Verdict) string {
	if v.Admit() {
		return "verdict.admit"
	}
	return "verdict." + v.Pass
}
