package owner

// Streams on the fast-path lease, the owner's part
// (docs/design/fast-admission-streams.md section 4.6): the echo the owner
// takes for a gateway that keeps no deadline, a running charge that may
// fall, and a stream hold's reap floor.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// streamFixture is an owner whose holds live an hour, with a grace of a
// minute and the reap floor given, and a lease of its that lasts three
// hours, so a stream heartbeats long after its admission.
func streamFixture(t *testing.T, floor StreamReap, recordFloor bool) (*fixture, *Lease) {
	t.Helper()
	f := newFixture(t, 1000, nil, func(c *Config) {
		c.Grace, c.StreamReap, c.RecordReapFloor = time.Minute, floor, recordFloor
	})
	l, err := f.owner.Take("lease-2", "ws-1", 1000, start.Add(3*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	return f, l
}

func admitOn(t *testing.T, l *Lease, e int64, stream bool) string {
	t.Helper()
	a, err := l.Admit(Admission{Estimate: e, Stream: stream, Boot: boot})
	if err != nil {
		t.Fatalf("admitting %d: %v", e, err)
	}
	return a.Auth
}

func TestARunningChargeMayFallButNeverPassesTheHold(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 500, true)
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	// A cache-read count reported after the prompt: more usage, a lower
	// charge. Accepted, and the record carries the lower charge.
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 19,
		Echoed: first}); err != nil {
		t.Fatalf("a falling running charge: %v", err)
	}
	recs := f.log.records(t, "lease-1")
	if got := recs[len(recs)-1].Snapshot.Running; got != 19 {
		t.Fatalf("the record's running charge %d, want 19", got)
	}
	for name, hb := range map[string]HeartbeatOf{
		"over the hold":   {GatewaySeq: 3, Hash: sum("h3"), Usage: 12, Running: 501, Echoed: first},
		"negative":        {GatewaySeq: 3, Hash: sum("h3"), Usage: 12, Running: -1, Echoed: first},
		"usage regressed": {GatewaySeq: 3, Hash: sum("h3"), Usage: 10, Running: 19, Echoed: first},
	} {
		if _, err := f.lease.Heartbeat(ctx, a, hb); !errors.Is(err, ErrRejected) {
			t.Errorf("%s: %v, want %v", name, err, ErrRejected)
		}
	}
	// At the hold exactly is taken.
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 3, Hash: sum("h3"), Usage: 12, Running: 500,
		Echoed: first}); err != nil {
		t.Fatalf("a running charge at the hold: %v", err)
	}
}

func TestEchoGrantedTakesTheDeadlineLastGranted(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 500, true)
	// The first heartbeat has no deadline to echo; EchoGranted there asks
	// for nothing.
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms"), EchoGranted: true})
	if err != nil {
		t.Fatal(err)
	}
	// Neither an echo nor EchoGranted: refused, as before.
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21}); !errors.Is(err, ErrRejected) {
		t.Fatalf("a later heartbeat with no echo: %v", err)
	}
	// Both: refused, there is one echo.
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21,
		Echoed: first, EchoGranted: true}); !errors.Is(err, ErrRejected) {
		t.Fatalf("an echo and EchoGranted: %v", err)
	}
	f.clock.advance(10 * time.Second)
	second, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21,
		EchoGranted: true})
	if err != nil || !second.Equal(start.Add(40*time.Second)) {
		t.Fatalf("EchoGranted within the deadline: %v %v", second, err)
	}
	// A replay is answered as the heartbeat it repeats.
	if again, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21,
		EchoGranted: true}); err != nil || !again.Equal(second) {
		t.Fatalf("a replay of EchoGranted: %v %v", again, err)
	}
	// Acknowledged past the deadline the owner last granted: the stream's
	// gateway learned it too late, deadline_passed, as an echo of that
	// deadline would be.
	f.clock.advance(31 * time.Second)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 3, Hash: sum("h3"), Usage: 12, Running: 22,
		EchoGranted: true}); !errors.Is(err, ErrDeadlinePassed) {
		t.Fatalf("EchoGranted acknowledged past the last deadline: %v, want %v", err, ErrDeadlinePassed)
	}
}

// TestAStreamHoldIsReapedNoSoonerThanItsFloor: the floor's two terms and
// its cap, each a nanosecond before and at its instant, and a hold that is
// not a stream never has one.
func TestAStreamHoldIsReapedNoSoonerThanItsFloor(t *testing.T) {
	ctx := context.Background()
	floor := StreamReap{AfterAdmission: 10 * time.Minute, AfterHeartbeat: 5 * time.Minute}
	dueAt := func(t *testing.T, l *Lease, auth string, at time.Time) {
		t.Helper()
		for _, d := range l.due(at.Add(-time.Nanosecond), time.Minute) {
			if d.Auth == auth {
				t.Fatalf("due a nanosecond before %v", at)
			}
		}
		found := false
		for _, d := range l.due(at, time.Minute) {
			found = found || d.Auth == auth
		}
		if !found {
			t.Fatalf("not due at %v", at)
		}
	}

	// Admitted at start, a heartbeat at start+1m: the admission's term,
	// start+10m, is the later.
	f, l := streamFixture(t, floor, false)
	a := admitOn(t, l, 100, true)
	f.clock.advance(time.Minute)
	if _, err := l.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("a1"), Usage: 1, Running: 1,
		Basis: []byte("t")}); err != nil {
		t.Fatal(err)
	}
	dueAt(t, l, a, start.Add(10*time.Minute))

	// Heartbeats every 30 s to start+8m: the last one's term, start+13m,
	// is the later.
	f2, l2 := streamFixture(t, floor, false)
	b := admitOn(t, l2, 100, true)
	f2.clock.advance(time.Minute)
	gb, err := l2.Heartbeat(ctx, b, HeartbeatOf{GatewaySeq: 1, Hash: sum("b1"), Usage: 1, Running: 1, Basis: []byte("t")})
	if err != nil {
		t.Fatal(err)
	}
	for i := int64(2); i <= 15; i++ {
		f2.clock.advance(30 * time.Second)
		if gb, err = l2.Heartbeat(ctx, b, HeartbeatOf{GatewaySeq: i, Hash: sum(fmt.Sprintf("b%d", i)), Usage: i,
			Running: i, Echoed: gb}); err != nil {
			t.Fatalf("heartbeat %d: %v", i, err)
		}
	}
	// The last at start+8m: start+13m, after start+10m.
	if now := f2.clock.Now(); !now.Equal(start.Add(8 * time.Minute)) {
		t.Fatalf("the last heartbeat at %v", now)
	}
	dueAt(t, l2, b, start.Add(13*time.Minute))

	// A heartbeat near the end of life (an hour): the floor is capped at
	// the end of life plus the grace, start+61m, before start+58m+5m.
	f3, l3 := streamFixture(t, floor, false)
	c := admitOn(t, l3, 100, true)
	f3.clock.advance(58 * time.Minute)
	if _, err := l3.Heartbeat(ctx, c, HeartbeatOf{GatewaySeq: 1, Hash: sum("c1"), Usage: 1, Running: 1, Basis: []byte("t")}); err != nil {
		t.Fatal(err)
	}
	dueAt(t, l3, c, start.Add(61*time.Minute))

	// No floor: a stream's hold is due at its deadline plus the grace, as
	// before.
	f4, l4 := streamFixture(t, StreamReap{}, false)
	d := admitOn(t, l4, 100, true)
	f4.clock.advance(time.Minute)
	gd, err := l4.Heartbeat(ctx, d, HeartbeatOf{GatewaySeq: 1, Hash: sum("d1"), Usage: 1, Running: 1, Basis: []byte("t")})
	if err != nil {
		t.Fatal(err)
	}
	dueAt(t, l4, d, gd.Add(time.Minute))
}

func TestOnlyAStreamsHoldHasAReapFloor(t *testing.T) {
	floor := StreamReap{AfterAdmission: 10 * time.Minute, AfterHeartbeat: 5 * time.Minute}
	beat := start.Add(time.Minute)
	deadline := beat.Add(30 * time.Second)
	plain := &hold{stream: false, admitted: start, endOfLife: start.Add(time.Hour)}
	if got := floor.reapDeadline(plain, beat, deadline, time.Minute); !got.IsZero() {
		t.Fatalf("a hold that is not a stream has a reap deadline %v", got)
	}
	stream := &hold{stream: true, admitted: start, endOfLife: start.Add(time.Hour)}
	if got := floor.reapDeadline(stream, beat, deadline, time.Minute); !got.Equal(start.Add(9 * time.Minute)) {
		t.Fatalf("a stream's reap deadline %v, want the floor less the grace, %v", got, start.Add(9*time.Minute))
	}
	// A floor before the deadline plus the grace changes nothing.
	short := StreamReap{AfterAdmission: time.Second, AfterHeartbeat: time.Second}
	if got := short.reapDeadline(stream, beat, deadline, time.Minute); !got.IsZero() {
		t.Fatalf("a floor before the deadline gave %v", got)
	}
}

// TestTheReapFloorIsRecordedOnlyWhenAsked: an owner that applies the floor
// to its own reaps writes records an auditor from before the field reads,
// byte for byte as before, until RecordReapFloor; then each stream
// heartbeat's record and its hold in a hand-off carry it.
func TestTheReapFloorIsRecordedOnlyWhenAsked(t *testing.T) {
	ctx := context.Background()
	floor := StreamReap{AfterAdmission: 10 * time.Minute, AfterHeartbeat: 5 * time.Minute}
	for _, recordFloor := range []bool{false, true} {
		f, l := streamFixture(t, floor, recordFloor)
		a := admitOn(t, l, 100, true)
		if _, err := l.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("r1"), Usage: 1, Running: 1,
			Basis: []byte("t")}); err != nil {
			t.Fatal(err)
		}
		f.log.mu.Lock()
		raw := f.log.stored["lease-2"][0]
		f.log.mu.Unlock()
		has := bytes.Contains(raw, []byte(`"reap_deadline"`))
		if has != recordFloor {
			t.Fatalf("RecordReapFloor %v: the record %s", recordFloor, raw)
		}
		r, err := record.Decode(raw)
		if err != nil {
			t.Fatal(err)
		}
		want := time.Time{}
		if recordFloor {
			want = start.Add(9 * time.Minute)
		}
		if !r.Snapshot.ReapDeadline.Equal(want) {
			t.Fatalf("RecordReapFloor %v: the reap deadline %v, want %v", recordFloor, r.Snapshot.ReapDeadline, want)
		}
		// The owner's own reap waits for the floor either way.
		if d := l.due(start.Add(10*time.Minute-time.Nanosecond), time.Minute); len(d) != 0 {
			t.Fatalf("RecordReapFloor %v: due before the floor", recordFloor)
		}
		if _, err := l.handoff(ctx); err != nil {
			t.Fatal(err)
		}
		var holds []record.HeldHold
		for _, r := range f.log.records(t, "lease-2") {
			if r.Kind == record.Handoff {
				holds = append(holds, r.Holds...)
			}
		}
		if len(holds) != 1 || holds[0].Snapshot == nil || !holds[0].Snapshot.ReapDeadline.Equal(want) {
			t.Fatalf("RecordReapFloor %v: the handed-off hold %+v", recordFloor, holds)
		}
	}
}
