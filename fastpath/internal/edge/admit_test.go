package edge

import (
	"context"
	"net/url"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
)

type stubAdmitter struct{ status frontdoor.Status }

func (s stubAdmitter) Authorize(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
	return frontdoor.Authorized{Status: s.status}
}

func (s stubAdmitter) Refund(context.Context, frontdoor.RefundOf) frontdoor.TerminalAnswer {
	return frontdoor.TerminalAnswer{Status: s.status}
}

// The node's front door is an admitter.
var _ Admitter = (*frontdoor.FrontDoor)(nil)

// TestTheEdgesAdmitterIsBoundLate: an edge made with no holder, or with one
// not yet bound, has no admitter, and a nil holder neither panics nor binds;
// once bound, the edge made before the binding sees the admitter, and an
// unbinding takes it away again.
func TestTheEdgesAdmitterIsBoundLate(t *testing.T) {
	var none *LateAdmitter
	none.Bind(stubAdmitter{status: frontdoor.Admitted})
	if a := none.Load(); a != nil {
		t.Fatalf("a nil holder holds %T", a)
	}
	upstream, _ := url.Parse("http://python.internal")
	bare, err := New(Config{Token: "t", Upstream: upstream, Boots: &boots{}})
	if err != nil {
		t.Fatal(err)
	}
	if a := bare.Admitter(); a != nil {
		t.Fatalf("an edge with no holder has %T", a)
	}

	holder := &LateAdmitter{}
	e, err := New(Config{Token: "t", Upstream: upstream, Boots: &boots{}, Admitter: holder})
	if err != nil {
		t.Fatal(err)
	}
	if a := e.Admitter(); a != nil {
		t.Fatalf("an unbound holder holds %T", a)
	}
	holder.Bind(stubAdmitter{status: frontdoor.Busy})
	a := e.Admitter()
	if a == nil {
		t.Fatal("the edge does not see the admitter bound after it was made")
	}
	if got := a.Authorize(context.Background(), frontdoor.AuthorizeOf{}); got.Status != frontdoor.Busy {
		t.Fatalf("the bound admitter answered %+v", got)
	}
	holder.Bind(nil)
	if a := e.Admitter(); a != nil {
		t.Fatalf("an unbound holder still holds %T", a)
	}
}
