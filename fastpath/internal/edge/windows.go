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

// KeyWindowReader reads every tr_key_limit row of a key, in shard order
// (store.Store.ReadKeyWindows): not the shards the cached count names, so a
// key whose shards changed since is refused (key_window.rows).
type KeyWindowReader interface {
	ReadKeyWindows(ctx context.Context, keyHash string) ([]keywindow.Row, error)
}

// KeyWindowReaderAt reads the same rows as they stood at exactly at
// (store.Store.ReadKeyWindowsAt): Python's window check's read timestamp,
// for the shadow's confirmation of a window race (confirmRace).
type KeyWindowReaderAt interface {
	ReadKeyWindowsAt(ctx context.Context, keyHash string, at time.Time) ([]keywindow.Row, error)
}

// ShadowWindowReader is the decide shadow's reader: its window check's
// strong read, and the read at Python's timestamp that settles a race.
type ShadowWindowReader interface {
	KeyWindowReader
	KeyWindowReaderAt
}

// windowRead is one window check: its verdict, whether the read was made
// (no reader makes none), how long it took, and the rows it read and the
// instant it evaluated them at (for the shadow's reading of Python's
// refusal, compareWindowed).
type windowRead struct {
	verdict keywindow.Verdict
	made    bool
	took    time.Duration
	rows    []keywindow.Row
	at      time.Time
}

// readWindows checks an enforcing windowed key's decision: one read of its
// rows, ending at deadline or WindowReadBudget from now, whichever comes
// first, evaluated at at, the request's one clock reading. A read that fails
// or runs past its deadline, or no reader, is key_window.unread.
func readWindows(ctx context.Context, reader KeyWindowReader, d *Decision, at, deadline time.Time) windowRead {
	if reader == nil {
		return windowRead{verdict: keywindow.Verdict{Pass: keywindow.PassUnread}}
	}
	return readWindowsBy(ctx, reader.ReadKeyWindows, d, at, deadline)
}

// readWindowsAt is readWindows over the rows as they stood at asOf
// (KeyWindowReaderAt), still evaluated at at.
func readWindowsAt(ctx context.Context, reader KeyWindowReaderAt, d *Decision, at, asOf,
	deadline time.Time) windowRead {
	if reader == nil {
		return windowRead{verdict: keywindow.Verdict{Pass: keywindow.PassUnread}}
	}
	return readWindowsBy(ctx, func(ctx context.Context, keyHash string) ([]keywindow.Row, error) {
		return reader.ReadKeyWindowsAt(ctx, keyHash, asOf)
	}, d, at, deadline)
}

// readWindowsBy is one window check whose rows read reads.
func readWindowsBy(ctx context.Context, read func(context.Context, string) ([]keywindow.Row, error), d *Decision,
	at, deadline time.Time) windowRead {
	if late := time.Now().Add(WindowReadBudget); late.Before(deadline) {
		deadline = late
	}
	readCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	start := time.Now()
	rows, err := read(readCtx, d.KeyHash)
	w := windowRead{made: true, took: time.Since(start), rows: rows, at: at}
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
