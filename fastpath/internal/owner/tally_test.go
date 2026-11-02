package owner

import (
	"context"
	"errors"
	"testing"
	"time"
)

// tallyFixture is newFixture with an answer wait no test reaches, so a
// held acknowledgement is answered whatever the wall clock does, and the
// tally timing by the fixture's clock, read without moving it.
func tallyFixture(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t, 1000, nil, func(c *Config) { c.AnswerWait = time.Minute })
	f.owner.tally.now = func() time.Time {
		f.clock.mu.Lock()
		defer f.clock.mu.Unlock()
		return f.clock.now
	}
	return f
}

// TestTheTallySaysWhereATerminalWaited: a settle and a refund whose records
// both await their acknowledgement a second and a half count two slow
// acknowledgements, two slow terminals and two records awaited at once;
// taking the tally starts the next.
func TestTheTallySaysWhereATerminalWaited(t *testing.T) {
	f := tallyFixture(t)
	ctx := context.Background()
	settled, refunded := f.admit(t, 400, false), f.admit(t, 300, false)
	_ = f.owner.TakeTally()
	f.log.hold()
	done := make(chan error, 2)
	go func() {
		_, err := f.lease.Settle(ctx, settled, 250, sum("d1"))
		done <- err
	}()
	waitFor(t, "the settle's record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	go func() {
		_, err := f.lease.Refund(ctx, refunded)
		done <- err
	}()
	waitFor(t, "the refund's record", func() bool { return len(f.log.records(t, "lease-1")) == 2 })
	f.clock.advance(1500 * time.Millisecond)
	f.log.letGo()
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	got := f.owner.TakeTally()
	want := Tally{Acks: 2, AcksSlow: 2, AckTotal: 3 * time.Second, AckMax: 1500 * time.Millisecond, DepthMax: 2,
		Terminals: 2, TerminalsSlow: 2, TerminalMax: 1500 * time.Millisecond}
	if got != want {
		t.Fatalf("the tally: %+v, want %+v", got, want)
	}
	if next := f.owner.TakeTally(); next != (Tally{}) {
		t.Fatalf("the next tally: %+v", next)
	}
}

// TestTheTallySaysWhereAHeartbeatWaited: a heartbeat whose record awaits its
// acknowledgement past a second is a slow heartbeat, and the next, answered
// at once, is not.
func TestTheTallySaysWhereAHeartbeatWaited(t *testing.T) {
	f := tallyFixture(t)
	ctx := context.Background()
	a := f.admit(t, 500, true)
	_ = f.owner.TakeTally()
	f.log.hold()
	type answer struct {
		deadline time.Time
		err      error
	}
	first := make(chan answer, 1)
	go func() {
		d, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
			Basis: []byte("terms")})
		first <- answer{d, err}
	}()
	waitFor(t, "the heartbeat's record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	f.clock.advance(1200 * time.Millisecond)
	f.log.letGo()
	got := <-first
	if got.err != nil {
		t.Fatal(got.err)
	}
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21,
		Echoed: got.deadline}); err != nil {
		t.Fatal(err)
	}
	tl := f.owner.TakeTally()
	if tl.Heartbeats != 2 || tl.HeartbeatsSlow != 1 || tl.HeartbeatMax != 1200*time.Millisecond || tl.Terminals != 0 {
		t.Fatalf("the tally: %+v", tl)
	}
}

// TestAFailedPublishIsCountedOnce: a publish refused past the lease's
// cutoff is one failure, however often the flusher waits on it again
// before a renewal lets it republish, and the republish is acknowledged.
func TestAFailedPublishIsCountedOnce(t *testing.T) {
	f := tallyFixture(t)
	ctx := context.Background()
	a := f.admit(t, 10, false)
	_ = f.owner.TakeTally()
	f.log.hold()
	refunded := make(chan struct{})
	go func() {
		defer close(refunded)
		_, _ = f.lease.Refund(ctx, a)
	}()
	waitFor(t, "the refund's record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	f.clock.advance(58 * time.Second) // past the cutoff: no republish until a renewal
	f.log.letGoFailing(errors.New("refused"))
	waitFor(t, "the failure", func() bool {
		f.lease.mu.Lock()
		defer f.lease.mu.Unlock()
		return f.lease.failed
	})
	for range 3 {
		// The flusher waits on the failed publish again at each kick.
		f.lease.kick <- struct{}{}
	}
	f.lease.Renewed(start.Add(5 * time.Minute))
	waitFor(t, "the republish's acknowledgement", func() bool {
		f.lease.mu.Lock()
		defer f.lease.mu.Unlock()
		return len(f.lease.inflight) == 0
	})
	<-refunded
	if tl := f.owner.TakeTally(); tl.PublishFailures != 1 || tl.Acks != 1 {
		t.Fatalf("the tally: %+v", tl)
	}
}

// TestEachRefusedPublishIsCounted: a record refused twice within the
// cutoff, republished after each, is two failures, then one
// acknowledgement.
func TestEachRefusedPublishIsCounted(t *testing.T) {
	f := tallyFixture(t)
	ctx := context.Background()
	a := f.admit(t, 10, false)
	_ = f.owner.TakeTally()
	f.log.fail("lease-1", 2)
	if _, err := f.lease.Refund(ctx, a); err != nil {
		t.Fatal(err)
	}
	if tl := f.owner.TakeTally(); tl.PublishFailures != 2 || tl.Acks != 1 {
		t.Fatalf("the tally: %+v", tl)
	}
}
