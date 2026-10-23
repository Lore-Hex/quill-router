package edge

import (
	"context"
	"sync/atomic"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
)

// Admitter takes an authorize to its shard's owner and a refund of the hold
// it made: the node's own front door (frontdoor.FrontDoor), in the edge's
// process. The edge calls neither yet; it will admit the requests it
// decides (E2), as Python's pilot does through the front door's HTTP
// surface.
type Admitter interface {
	Authorize(ctx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized
	Refund(ctx context.Context, r frontdoor.RefundOf) frontdoor.TerminalAnswer
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
// nothing.
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
