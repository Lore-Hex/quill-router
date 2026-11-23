package frontdoor

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// production is newPeers with the waits the nodes run (service.Defaults):
// an answer budget of 1.2 s keeping 150 ms for a terminal's append, an
// owner's wait of 500 ms, a peer's of 250 ms, and a publish's share of
// 250 ms, which it may run past to 700 ms.
func production(t *testing.T) *peersFixture {
	t.Helper()
	f := newPeers(t)
	cfg := &f.door.cfg
	cfg.OwnerWait, cfg.PeerWait, cfg.PublishWait = 500*time.Millisecond, 250*time.Millisecond, 250*time.Millisecond
	cfg.PublishMax = 700 * time.Millisecond
	cfg.AnswerWithin, cfg.AppendWithin = 1200*time.Millisecond, 150*time.Millisecond
	if _, err := New(*cfg); err != nil {
		t.Fatal(err)
	}
	f.owners.holding = map[string]bool{}
	return f
}

// TestASettleWhosePublishIsSlowIsStillDecided: a settle whose full record
// takes 300 to 600 ms to be acknowledged, past the publish's 250 ms share of
// the answer budget, is still sent to its owner once the record is
// acknowledged, and answered Won, within the budget (2026-11-23: the record
// topic's p99 publish latency rose past 250 ms for minutes at a time, and
// each settle whose publish took that long was answered Failed, though the
// publish succeeded and the owner answered terminals in milliseconds).
func TestASettleWhosePublishIsSlowIsStillDecided(t *testing.T) {
	ctx := context.Background()
	won := TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
	for _, slow := range []time.Duration{300 * time.Millisecond, 450 * time.Millisecond, 600 * time.Millisecond} {
		f := production(t)
		f.owners.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
		f.records.slow = slow
		began := time.Now()
		if got := f.door.Settle(ctx, settle(sealedAt(t, "node-b", "lease-1", "gwa-1"))); got != won {
			t.Fatalf("a settle whose publish took %v: %+v", slow, got)
		}
		withinBudget(t, "a settle whose publish is slow", began, f)
		// The full record was acknowledged before the owner had the settle
		// (§4.9).
		if got := f.ev.all(); !slices.Equal(got, []string{"publish gwa-1 record", "owner node-b settle"}) {
			t.Fatalf("a settle whose publish took %v: %q", slow, got)
		}
	}
}

// unreachedOf is the withdrawal's evidence: the owners whose calls failed
// here with their full wait while a peer reached them.
func (f *peersFixture) unreachedOf() []string {
	f.door.mu.Lock()
	defer f.door.mu.Unlock()
	var owners []string
	for o := range f.door.unreached {
		owners = append(owners, o)
	}
	return owners
}

// peered reports whether any request went through a peer.
func (f *peersFixture) peered() bool {
	return slices.ContainsFunc(f.ev.all(), func(e string) bool { return strings.HasPrefix(e, "peer ") })
}

// TestAnOwnerCallASlowPublishCutsIsNoEvidence: a settle whose publish runs
// past its share far enough to leave its owner less than the owner's wait
// still calls the owner, with what is left; a call so cut that does not
// answer, holding the settle to the budget's end or refused at once, is no
// evidence about the owner: it is not failed here, no lease is failing, and
// it goes through no peer, whose reach would be evidence against this
// front door. The settle is Recorded in the drain log with the cause
// budget, within the answer budget. A call a slow publish leaves its full
// wait is evidence as before: the owner failed here, and a peer that then
// reaches it is evidence against this front door.
func TestAnOwnerCallASlowPublishCutsIsNoEvidence(t *testing.T) {
	ctx := context.Background()
	sealed := sealedAt(t, "node-b", "lease-1", "gwa-1")
	for _, refused := range []bool{false, true} {
		f := production(t)
		if refused {
			f.owners.unreachable["node-b"] = true
		} else {
			f.owners.holding["node-b"] = true
		}
		// A peer would reach the owner at once, were it asked.
		f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
		// 600 ms of the hops' 1050 leaves the owner 450 ms of its 500.
		f.records.slow = 600 * time.Millisecond
		began := time.Now()
		if got := f.door.Settle(ctx, settle(sealed)); got.Status != Recorded || len(f.store.appended) != 1 ||
			f.store.appended[0].Cause != "budget" {
			t.Fatalf("a settle whose owner call the publish cut (refused %v): %+v, %+v", refused, got,
				f.store.appended)
		}
		withinBudget(t, "a settle whose owner call the publish cut", began, f)
		if here, failing := f.evidence(); len(here) != 0 || failing != 0 {
			t.Fatalf("an owner call the publish cut (refused %v) kept as evidence: here %v, %d leases failing",
				refused, here, failing)
		}
		if f.peered() {
			t.Fatalf("an owner call the publish cut (refused %v) sent to a peer: %q", refused, f.ev.all())
		}
		if tally := f.door.TakeTally(); tally.Short != 1 || tally.Published != 1 || tally.PublishedLate != 1 {
			t.Fatalf("the tally (refused %v): %+v", refused, tally)
		}
	}

	// 300 ms of the hops' 1050 leaves the owner its full 500 ms: its
	// failure is evidence, and the peer that reaches it, with a little less
	// than its own 250 ms left, is evidence against this front door.
	f := production(t)
	f.owners.holding["node-b"] = true
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
	f.records.slow = 300 * time.Millisecond
	began := time.Now()
	if got := f.door.Settle(ctx, settle(sealed)); got != (TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}) {
		t.Fatalf("a settle whose owner had its full wait, through a peer: %+v", got)
	}
	withinBudget(t, "a settle through a peer after a slow publish", began, f)
	if here, _ := f.evidence(); !slices.Equal(here, []string{"node-b"}) ||
		!slices.Equal(f.unreachedOf(), []string{"node-b"}) {
		t.Fatalf("an owner call with its full wait: here %v, unreached %v", here, f.unreachedOf())
	}
	if tally := f.door.TakeTally(); tally.Short != 0 {
		t.Fatalf("the tally: %+v", tally)
	}
}

// TestNewTakesAPublishsLongestWaitOnlyWhereItFits: New takes a publish's
// longest wait only at least its share, and, with an answer budget, only
// where an owner called after it keeps more than half its wait and the
// append its time; zero is the share, as before it. Its share, an owner's
// and a peer's full waits and the append's must still fit as well.
func TestNewTakesAPublishsLongestWaitOnlyWhereItFits(t *testing.T) {
	ok := production(t).door.cfg
	if _, err := New(ok); err != nil {
		t.Fatalf("the nodes' waits: %v", err)
	}
	for _, c := range []struct {
		what       string
		share, max time.Duration
	}{
		{"a longest wait below its share", 250 * time.Millisecond, 200 * time.Millisecond},
		{"a negative longest wait", 250 * time.Millisecond, -time.Millisecond},
		// 800 + 250 (half the owner's 500) + 150 is the whole 1200.
		{"a longest wait leaving the owner half its wait", 250 * time.Millisecond, 800 * time.Millisecond},
		// 300 + 500 + 250 + 150 is the whole 1200, whatever the longest.
		{"a share leaving the owner and the peer less than their waits", 300 * time.Millisecond, 0},
		{"a share leaving them less, with a longest wait", 300 * time.Millisecond, 700 * time.Millisecond},
	} {
		cfg := ok
		cfg.PublishWait, cfg.PublishMax = c.share, c.max
		if _, err := New(cfg); err == nil {
			t.Fatalf("%s: a share of %v and a longest wait of %v taken", c.what, c.share, c.max)
		}
	}
	for _, longest := range []time.Duration{0, 250 * time.Millisecond, 799 * time.Millisecond} {
		cfg := ok
		cfg.PublishMax = longest
		if _, err := New(cfg); err != nil {
			t.Fatalf("a longest wait of %v: %v", longest, err)
		}
	}
	// With no answer budget, any longest wait at least the share.
	cfg := ok
	cfg.AnswerWithin, cfg.AppendWithin, cfg.PublishMax = 0, 0, 5*time.Second
	if _, err := New(cfg); err != nil {
		t.Fatalf("no budget: %v", err)
	}
}

// TestATerminalAnsweredFailedSaysWhy: each terminal the front door answers
// Failed is counted by why, so the node's line names the cause: a publish
// not acknowledged within its longest wait, a publish that failed, the
// gateway's request ending first, the owner's own Failed, an append that
// failed, and a refused append's disposition read that failed; and taking
// the tally starts the next.
func TestATerminalAnsweredFailedSaysWhy(t *testing.T) {
	ctx := context.Background()
	sealed := sealedAt(t, "node-b", "lease-1", "gwa-1")
	f := production(t)
	failed := func(what string, do func() TerminalAnswer) {
		t.Helper()
		if got := do(); got.Status != Failed {
			t.Fatalf("%s: %+v", what, got)
		}
	}

	f.records.holding = true
	began := time.Now()
	failed("a publish that holds", func() TerminalAnswer { return f.door.Settle(ctx, settle(sealed)) })
	if took := time.Since(began); took < 700*time.Millisecond {
		t.Fatalf("a publish that holds answered after %v, before its longest wait", took)
	}
	withinBudget(t, "a publish that holds", began, f)
	f.records.holding = false

	f.records.fail = true
	failed("a publish that fails", func() TerminalAnswer { return f.door.Settle(ctx, settle(sealed)) })
	f.records.fail = false

	f.records.holding = true
	failed("a request that ends during its publish", func() TerminalAnswer {
		ctx, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
		defer cancel()
		return f.door.Settle(ctx, settle(sealed))
	})
	f.records.holding = false
	failed("a request that ended before its owner", func() TerminalAnswer {
		ctx, cancel := context.WithCancel(ctx)
		cancel()
		return f.door.Refund(ctx, RefundOf{Envelope: sealed, Money: []byte("{}")})
	})

	f.owners.terminal = OwnerTerminalAnswer{Status: Failed}
	failed("an owner's Failed", func() TerminalAnswer { return f.door.Settle(ctx, settle(sealed)) })
	failed("an owner's Failed to a refund", func() TerminalAnswer {
		return f.door.Refund(ctx, RefundOf{Envelope: sealed, Money: []byte("{}")})
	})

	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	f.store.failAppend = true
	failed("an append that fails", func() TerminalAnswer { return f.door.Settle(ctx, settle(sealed)) })
	f.store.failAppend, f.store.refuse, f.store.failDisp = false, true, true
	failed("a refused append's disposition read that fails", func() TerminalAnswer {
		return f.door.Settle(ctx, settle(sealed))
	})

	got := f.door.TakeTally()
	want := Failures{PublishTimeout: 1, PublishError: 1, Gone: 2, Owner: 2, Append: 1, Disposition: 1}
	if got.Failed != want || got.Published != 3 || got.PublishedLate != 0 || got.Short != 0 {
		t.Fatalf("the tally: %+v, want %+v failed and 3 published", got, want)
	}
	if got := f.door.TakeTally(); got != (Tally{}) {
		t.Fatalf("the next tally: %+v", got)
	}

	// A publish past its share is counted, with its wait.
	f = production(t)
	f.owners.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
	f.records.slow = 300 * time.Millisecond
	if got := f.door.Settle(ctx, settle(sealed)); got.Status != Won {
		t.Fatalf("a settle whose publish took 300 ms: %+v", got)
	}
	if got := f.door.TakeTally(); got.Published != 1 || got.PublishedLate != 1 ||
		got.PublishMax < 300*time.Millisecond || got.Failed != (Failures{}) {
		t.Fatalf("the tally after a slow publish: %+v", got)
	}
}
