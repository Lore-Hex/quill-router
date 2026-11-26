package owner

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// timedSpanner is a store that notes, for each ask, how long its context
// gave it and how it ended.
type timedSpanner struct {
	*fakeSpanner
	mu    sync.Mutex
	given []time.Duration
	ended []error
}

func (s *timedSpanner) Grant(ctx context.Context, req store.GrantRequest) (store.GrantResult, error) {
	given := time.Duration(-1)
	if by, ok := ctx.Deadline(); ok {
		given = time.Until(by)
	}
	s.mu.Lock()
	s.given = append(s.given, given)
	s.mu.Unlock()
	got, err := s.fakeSpanner.Grant(ctx, req)
	s.mu.Lock()
	s.ended = append(s.ended, err)
	s.mu.Unlock()
	return got, err
}

func (s *timedSpanner) asks() ([]time.Duration, []error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]time.Duration(nil), s.given...), append([]error(nil), s.ended...)
}

// timedFixture is shardFixture with a store that notes its asks' deadlines.
func timedFixture(t *testing.T, changes ...func(*Config)) (*fixture, *fakeSpanner, *timedSpanner) {
	t.Helper()
	var timed *timedSpanner
	f, sp := shardFixture(t, append([]func(*Config){func(c *Config) {
		timed = &timedSpanner{fakeSpanner: c.Spanner.(*fakeSpanner)}
		c.Spanner = timed
	}}, changes...)...)
	return f, sp, timed
}

// TestAnAskHasGrantDeadline: the owner gives each ask GrantDeadline, the
// store's bound on the edge's insert, and no longer: a grant holds shared
// locks on every credit row of its workspace until it ends.
func TestAnAskHasGrantDeadline(t *testing.T) {
	if want := 1500 * time.Millisecond; GrantDeadline != want {
		t.Fatalf("an ask's deadline is %v, want %v, the store's insert's (%v)", GrantDeadline, want, store.InsertDeadline)
	}
	f, _, timed := timedFixture(t)
	_, _ = f.owner.Admit(key, Admission{Estimate: 5, Boot: boot})
	f.waitLeases(t, 1)
	given, ended := timed.asks()
	if len(given) != 1 || len(ended) != 1 || ended[0] != nil {
		t.Fatalf("the asks: given %v, ended %v", given, ended)
	}
	if given[0] <= GrantDeadline-time.Second || given[0] > GrantDeadline {
		t.Fatalf("an ask was given %v, want GrantDeadline, %v", given[0], GrantDeadline)
	}
}

// TestAnAskGivenUpAtItsDeadlineIsAskedAgainTheSame: an ask the store does
// not answer ends at its deadline, though the owner goes on; the owner
// takes no lease for it, and asks again as it does for a lost answer, the
// same request with the same lease ID, which the store answers with the
// lease if the first had been granted after all.
func TestAnAskGivenUpAtItsDeadlineIsAskedAgainTheSame(t *testing.T) {
	const deadline = 40 * time.Millisecond
	f, sp, timed := timedFixture(t, func(c *Config) { c.grantDeadline = deadline })
	sp.mu.Lock()
	sp.grantGate = make(chan struct{})
	sp.mu.Unlock()
	began := time.Now()
	if _, err := f.owner.Admit(key, Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request for a shard with no lease: %v", err)
	}
	waitFor(t, "the ask's end", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return len(sp.granted()) == 1 && !f.owner.shards[key].asking
	})
	if took := time.Since(began); took < deadline || took > 2*time.Second {
		t.Fatalf("the ask ended after %v, and its deadline is %v", took, deadline)
	}
	given, ended := timed.asks()
	if len(ended) != 1 || !errors.Is(ended[0], context.DeadlineExceeded) || given[0] <= 0 || given[0] > deadline {
		t.Fatalf("the ask: given %v, ended %v", given, ended)
	}
	f.owner.mu.Lock()
	retry, held := f.owner.shards[key].retry, len(f.owner.shards[key].leases)
	f.owner.mu.Unlock()
	if retry == nil || *retry != sp.granted()[0] || held != 0 {
		t.Fatalf("after an ask given up: to ask again %+v, of %+v; %d leases", retry, sp.granted(), held)
	}
	// Still no room, and the request goes elsewhere, as before any lease.
	if _, err := f.owner.Admit(key, Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a request after an ask given up: %v", err)
	}
	sp.mu.Lock()
	close(sp.grantGate)
	sp.grantGate = nil
	sp.mu.Unlock()
	waitFor(t, "the asks' end", func() bool {
		f.owner.mu.Lock()
		defer f.owner.mu.Unlock()
		return !f.owner.shards[key].asking
	})
	f.clock.advance(10 * time.Second)
	_, _ = f.owner.Admit(key, Admission{Estimate: 1, Boot: boot})
	l := f.waitLeases(t, 1)[0]
	grants := sp.granted()
	for _, g := range grants {
		if g != grants[0] {
			t.Fatalf("the asks differ: %+v", grants)
		}
	}
	if len(grants) < 2 || l.id != grants[0].LeaseID {
		t.Fatalf("the asks: %+v, the lease taken %s", grants, l.id)
	}
}
