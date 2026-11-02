package owner

import (
	"context"
	"testing"
	"time"
)

// TestTheTallySaysWhereAnAnswerWaited: a settle whose record waits a second
// and a half for its acknowledgement counts a slow acknowledgement and a
// slow terminal, by the tally's clock; a refund acknowledged at once counts
// neither; the most records the lease awaited is kept; and taking the
// tally starts the next.
func TestTheTallySaysWhereAnAnswerWaited(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	settled, refunded := f.admit(t, 400, false), f.admit(t, 300, false)
	f.lease.o.tally.now = func() time.Time {
		f.clock.mu.Lock()
		defer f.clock.mu.Unlock()
		return f.clock.now
	}
	_ = f.lease.o.TakeTally()
	f.log.hold()
	done := make(chan error, 1)
	go func() {
		_, err := f.lease.Settle(ctx, settled, 250, sum("d1"))
		done <- err
	}()
	waitFor(t, "the settle's record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	f.clock.advance(1500 * time.Millisecond)
	f.log.letGo()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Refund(ctx, refunded); err != nil {
		t.Fatal(err)
	}
	got := f.lease.o.TakeTally()
	want := Tally{Acks: 2, AcksSlow: 1, AckTotal: 1500 * time.Millisecond, AckMax: 1500 * time.Millisecond,
		DepthMax: 1, Terminals: 2, TerminalsSlow: 1, TerminalMax: 1500 * time.Millisecond}
	if got != want {
		t.Fatalf("the tally: %+v, want %+v", got, want)
	}
	if next := f.lease.o.TakeTally(); next != (Tally{}) {
		t.Fatalf("the next tally: %+v", next)
	}
}
