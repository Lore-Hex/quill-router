package edge

import (
	"context"
	"sync/atomic"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
)

// Admitter takes an authorize to its shard's owner and a refund of the hold
// it made: the node's own front door (frontdoor.FrontDoor), in the edge's
// process, which the decide path admits the requests it decides through,
// as Python's pilot does through the front door's HTTP surface. Enabled is
// the front door's copy of the switch, which the decide pipeline reads as
// a decision gate (PassWorkspaceNotEnabled) before it would admit.
type Admitter interface {
	Authorize(ctx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized
	Refund(ctx context.Context, r frontdoor.RefundOf) frontdoor.TerminalAnswer
	Enabled(workspace string) bool
}

// enabledOf is the switch copy of an admitter for DecideInputs.Enabled: nil
// for none bound.
func enabledOf(a Admitter) func(string) bool {
	if a == nil {
		return nil
	}
	return a.Enabled
}

// LateAdmitter holds the edge's admitter, bound once it exists: a process
// makes its edge before its admission node, whose front door is the
// admitter. Unbound, or a nil holder, it holds none, and the edge decides
// nothing. Its zero value is unbound and ready for use.
type LateAdmitter struct {
	bound atomic.Pointer[admitterOf]
}

type admitterOf struct{ a Admitter }

// Bind binds a, or unbinds the holder for a nil a; a nil holder binds
// nothing. A typed nil (a nil pointer in a non-nil interface) is bound as
// given, so callers bind only a value they have made.
func (l *LateAdmitter) Bind(a Admitter) {
	if l == nil {
		return
	}
	if a == nil {
		l.bound.Store(nil)
		return
	}
	l.bound.Store(&admitterOf{a})
}

// Load is the admitter bound now, or nil for none.
func (l *LateAdmitter) Load() Admitter {
	if l == nil {
		return nil
	}
	if b := l.bound.Load(); b != nil {
		return b.a
	}
	return nil
}

// Admitter is the admitter bound to the edge's holder now (Config.Admitter),
// or nil for none.
func (e *Edge) Admitter() Admitter {
	return e.cfg.Admitter.Load()
}
