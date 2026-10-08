package owner

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// fakeLog is the settle log as Pub/Sub keeps it: each lease's records stored
// in the order handed over; a failed publish pauses the lease's key, and
// every publish to it fails until Resume. A test can fail publishes, and
// hold acknowledgements back until it releases them.
type fakeLog struct {
	mu       sync.Mutex
	stored   map[string][][]byte
	paused   map[string]bool
	failNext map[string]int
	holding  bool
	release  chan struct{}
}

func newFakeLog() *fakeLog {
	return &fakeLog{stored: map[string][][]byte{}, paused: map[string]bool{}, failNext: map[string]int{},
		release: make(chan struct{})}
}

type waiter struct {
	err  error
	hold chan struct{}
}

func (w waiter) Wait(ctx context.Context) (string, error) {
	if w.hold != nil {
		select {
		case <-w.hold:
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	return "", w.err
}

var errPaused = errors.New("the key is paused")

func (f *fakeLog) Publish(lease string, data []byte) Waiter {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.paused[lease] {
		return waiter{err: errPaused}
	}
	if f.failNext[lease] > 0 {
		f.failNext[lease]--
		f.paused[lease] = true
		return waiter{err: errors.New("refused")}
	}
	f.stored[lease] = append(f.stored[lease], append([]byte(nil), data...))
	if f.holding {
		return waiter{hold: f.release}
	}
	return waiter{}
}

func (f *fakeLog) Resume(lease string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.paused[lease] = false
}

func (f *fakeLog) fail(lease string, n int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failNext[lease] = n
}

// hold makes the next publishes wait for their acknowledgement until
// letGo.
func (f *fakeLog) hold() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.holding, f.release = true, make(chan struct{})
}

func (f *fakeLog) letGo() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.holding = false
	close(f.release)
}

func (f *fakeLog) records(t *testing.T, lease string) []record.Record {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []record.Record
	for _, b := range f.stored[lease] {
		r, err := record.Decode(b)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, r)
	}
	return out
}

type clock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

var start = time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)

// boot is the tests' boot binding.
var boot = []byte("boot-1")

// sum is the SHA-256 hash or digest the tests name by s.
func sum(s string) []byte {
	h := sha256.Sum256([]byte(s))
	return h[:]
}

type fixture struct {
	log   *fakeLog
	clock *clock
	owner *Owner
	lease *Lease
}

func newFixture(t *testing.T, allocation int64, overrun func(int64) int64) *fixture {
	t.Helper()
	f := &fixture{log: newFakeLog(), clock: &clock{now: start}}
	n := 0
	var mu sync.Mutex
	o, err := New(Config{Epoch: 3, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Overrun: overrun, Clock: f.clock.Now,
		NewAuthorization: func(lease string) (string, error) {
			mu.Lock()
			defer mu.Unlock()
			n++
			return fmt.Sprintf("gwa-%s-%d", lease, n), nil
		}}, f.log)
	if err != nil {
		t.Fatal(err)
	}
	f.owner = o
	if f.lease, err = o.Take("lease-1", "ws-1", allocation, start.Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { o.Let("lease-1") })
	return f
}

func (f *fixture) admit(t *testing.T, e int64, stream bool) string {
	t.Helper()
	a, err := f.lease.Admit(Admission{Estimate: e, Stream: stream, Boot: boot})
	if err != nil {
		t.Fatalf("admitting %d: %v", e, err)
	}
	return a.Auth
}

func TestAdmissionNeedsRoomAndTheCutoff(t *testing.T) {
	f := newFixture(t, 1000, func(e int64) int64 { return e / 10 })
	f.admit(t, 400, false) // holds 400 with a buffer of 40
	f.admit(t, 400, true)  // a stream: its buffer waits for its first heartbeat
	if b := f.lease.Books(); b.Held != 800 || b.Buffer != 40 || b.Free() != 160 {
		t.Fatalf("books after two holds: %+v", b)
	}
	if _, err := f.lease.Admit(Admission{Estimate: 150, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("a hold of 150 and its buffer of 15 against 160 free: %v", err)
	}
	f.admit(t, 140, false)
	before := f.lease.Books()
	f.clock.advance(58 * time.Second) // within two seconds of the expiry: past the cutoff
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("an admission past the cutoff: %v", err)
	}
	if after := f.lease.Books(); after != before {
		t.Fatalf("a refused admission moved the books: %+v, then %+v", before, after)
	}
	f.lease.Renewed(start.Add(-time.Hour))
	f.lease.Renewed(start.Add(2 * time.Minute))
	f.admit(t, 1, false)
}

func TestASettleMovesTheBooks(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	under, over, refunded, past := f.admit(t, 400, false), f.admit(t, 300, false), f.admit(t, 200, false), f.admit(t, 100, false)
	f.log.hold()
	done := make(chan Outcome, 1)
	go func() {
		out, err := f.lease.Settle(ctx, under, 250, sum("d1"))
		if err != nil {
			t.Error(err)
		}
		done <- out
	}()
	waitFor(t, "the settle's record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	if b := f.lease.Books(); b.Held != 600 || b.Consumed != 250 || b.Pending != 150 || b.Free() != 0 {
		t.Fatalf("books with a settle's record not acknowledged: %+v", b)
	}
	f.log.letGo()
	if out := <-done; out != (Outcome{Kind: record.Settle, Charge: 250}) {
		t.Fatalf("the settle: %+v", out)
	}
	if b := f.lease.Books(); b.Pending != 0 || b.Free() != 150 {
		t.Fatalf("books once acknowledged: %+v", b)
	}
	// An overrun beyond the lease's room: its charge exceeds what the lease
	// has, so the shortfall total and the allocation rise by the excess.
	if _, err := f.lease.Settle(ctx, over, 600, sum("d2")); err != nil {
		t.Fatal(err)
	}
	if b := f.lease.Books(); b.Shortfall != 150 || b.Allocation != 1150 || b.Remaining() != 0 {
		t.Fatalf("books after an overrun: %+v", b)
	}
	if _, err := f.lease.Refund(ctx, refunded); err != nil {
		t.Fatal(err)
	}
	if b := f.lease.Books(); b.Held != 100 || b.Consumed != 850 || b.Free() != 200 {
		t.Fatalf("books after a refund: %+v", b)
	}
	recs := f.log.records(t, "lease-1")
	if len(recs) != 3 || recs[1].Shortfall != 150 || recs[2].Shortfall != 150 || recs[0].Shortfall != 0 ||
		recs[2].Kind != record.Refund || recs[2].Charge != 0 || !bytes.Equal(recs[2].Boot, boot) ||
		!bytes.Equal(recs[0].Digest, sum("d1")) || recs[0].Boot != nil {
		t.Fatalf("the records: %+v", recs)
	}
	_ = past
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("%s did not happen", what)
		}
		time.Sleep(time.Millisecond)
	}
}

// TestTheFirstTerminalWins: a later terminal is answered with the winner's
// outcome, and nothing is published for it.
func TestTheFirstTerminalWins(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 400, false)
	if _, err := f.lease.Settle(ctx, a, 300, sum("d")); err != nil {
		t.Fatal(err)
	}
	out, err := f.lease.Refund(ctx, a)
	if err != nil || out != (Outcome{Kind: record.Settle, Charge: 300}) {
		t.Fatalf("a refund after the settle: %+v %v", out, err)
	}
	if again, err := f.lease.Settle(ctx, a, 900, sum("other")); err != nil || again != out {
		t.Fatalf("a second settle: %+v %v", again, err)
	}
	if n := len(f.log.records(t, "lease-1")); n != 1 {
		t.Fatalf("%d records for one authorization", n)
	}
	if _, err := f.lease.Settle(ctx, "gwa-other", 1, sum("d")); !errors.Is(err, ErrUnknownHold) {
		t.Fatalf("a settle of an authorization the lease never held: %v", err)
	}
	// A settle the record format cannot carry decides nothing, though its
	// charge overruns what the lease has: the hold stays open, and a settle
	// that names its record wins.
	b := f.admit(t, 100, false)
	before := f.lease.Books()
	for name, c := range map[string]struct {
		charge int64
		digest []byte
	}{"a short digest": {5000, []byte("d")}, "a negative charge": {-1, sum("d")}} {
		if _, err := f.lease.Settle(ctx, b, c.charge, c.digest); err == nil {
			t.Fatalf("a settle with %s", name)
		}
		if after := f.lease.Books(); after != before {
			t.Fatalf("a settle with %s moved the books: %+v, then %+v", name, before, after)
		}
	}
	if n := len(f.log.records(t, "lease-1")); n != 1 {
		t.Fatalf("%d records after the refused settles", n)
	}
	if out, err := f.lease.Settle(ctx, b, 20, sum("d")); err != nil || out.Charge != 20 {
		t.Fatalf("the settle after it: %+v %v", out, err)
	}
}

func TestHeartbeatValidity(t *testing.T) {
	f := newFixture(t, 1000, func(e int64) int64 { return e / 10 })
	ctx := context.Background()
	a := f.admit(t, 500, true)
	if b := f.lease.Books(); b.Buffer != 0 {
		t.Fatalf("a stream's buffer before its first heartbeat: %+v", b)
	}
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20,
		Basis: []byte("terms")})
	if err != nil || !first.Equal(start.Add(30*time.Second)) {
		t.Fatalf("the first heartbeat: %v %v", first, err)
	}
	if b := f.lease.Books(); b.Buffer != 50 {
		t.Fatalf("a stream's buffer after its first heartbeat: %+v", b)
	}
	for name, c := range map[string]struct {
		hb   HeartbeatOf
		want error
	}{
		"a lower sequence":         {HeartbeatOf{GatewaySeq: 0, Hash: sum("h0"), Usage: 10, Running: 20}, ErrStale},
		"the same with a new hash": {HeartbeatOf{GatewaySeq: 1, Hash: sum("x"), Usage: 10, Running: 20}, ErrStale},
		"regressed usage":          {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 9, Running: 20}, ErrRejected},
		"a regressed charge":       {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 19}, ErrRejected},
		"a charge over its cap":    {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 501}, ErrRejected},
		"no hash":                  {HeartbeatOf{GatewaySeq: 2, Usage: 11, Running: 21}, ErrRejected},
		"a hash not SHA-256's":     {HeartbeatOf{GatewaySeq: 2, Hash: []byte("h2"), Usage: 11, Running: 21}, ErrRejected},
	} {
		if _, err := f.lease.Heartbeat(ctx, a, c.hb); !errors.Is(err, c.want) {
			t.Errorf("%s: %v, want %v", name, err, c.want)
		}
	}
	other := f.admit(t, 100, true)
	for name, hb := range map[string]HeartbeatOf{
		"a first without the reap's basis": {GatewaySeq: 1, Hash: sum("o1"), Usage: 1, Running: 1},
		"a first numbered 0":               {GatewaySeq: 0, Hash: sum("o0"), Usage: 1, Running: 1, Basis: []byte("terms")},
	} {
		if _, err := f.lease.Heartbeat(ctx, other, hb); !errors.Is(err, ErrRejected) {
			t.Errorf("%s: %v, want %v", name, err, ErrRejected)
		}
	}
	f.clock.advance(time.Second)
	replay, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 10, Running: 20})
	if err != nil || !replay.Equal(first) {
		t.Fatalf("a replay: %v %v", replay, err)
	}
	if n := len(f.log.records(t, "lease-1")); n != 1 {
		t.Fatalf("a replay published: %d records", n)
	}
	next, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 30, Running: 60, Echoed: first})
	if err != nil || !next.Equal(start.Add(31*time.Second)) {
		t.Fatalf("the second heartbeat: %v %v", next, err)
	}
	recs := f.log.records(t, "lease-1")
	if len(recs) != 2 || string(recs[0].Basis) != "terms" || recs[1].Snapshot.Running != 60 || recs[1].Seq != 2 {
		t.Fatalf("the heartbeat records: %+v", recs)
	}
	if _, err := f.lease.Settle(ctx, a, 60, sum("d")); err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 3, Hash: sum("h3"), Usage: 40, Running: 70}); !errors.Is(err, ErrDecided) {
		t.Fatalf("a heartbeat after the settle: %v", err)
	}
}

// TestAHeartbeatAckedAfterItsDeadline: the record is acknowledged after the
// deadline the heartbeat echoed, so it is answered deadline_passed.
func TestAHeartbeatAckedAfterItsDeadline(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 500, true)
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	f.log.hold()
	got := make(chan error, 1)
	go func() {
		_, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 2, Running: 2, Echoed: first})
		got <- err
	}()
	waitFor(t, "the second record", func() bool { return len(f.log.records(t, "lease-1")) == 2 })
	f.clock.advance(31 * time.Second)
	f.log.letGo()
	if err := <-got; !errors.Is(err, ErrDeadlinePassed) {
		t.Fatalf("a heartbeat acknowledged past its deadline: %v", err)
	}
}

// TestAFailedPublishIsRepublishedInOrder: a failure pauses the lease's key;
// the flusher resumes it and republishes the same records, numbers and all,
// before anything new, and admits nothing meanwhile.
func TestAFailedPublishIsRepublishedInOrder(t *testing.T) {
	f := newFixture(t, 10000, nil)
	ctx := context.Background()
	var auths []string
	for range 4 {
		auths = append(auths, f.admit(t, 100, false))
	}
	if _, err := f.lease.Settle(ctx, auths[0], 50, sum("d0")); err != nil {
		t.Fatal(err)
	}
	f.log.fail("lease-1", 1)
	var wg sync.WaitGroup
	for _, a := range auths[1:] {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := f.lease.Settle(ctx, a, 50, sum("d")); err != nil {
				t.Errorf("a settle while publishes fail: %v", err)
			}
		}()
		waitFor(t, "the settle's hand-over", func() bool { return f.lease.Books().NextSeq > 2 })
	}
	wg.Wait()
	recs := f.log.records(t, "lease-1")
	if len(recs) != 4 {
		t.Fatalf("the log holds %d records", len(recs))
	}
	for i, r := range recs {
		if r.Seq != int64(i+1) {
			t.Fatalf("record %d has sequence number %d: %+v", i, r.Seq, recs)
		}
	}
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); err != nil {
		t.Fatalf("an admission once republished: %v", err)
	}
}

// TestNothingIsAdmittedWhilePublishesFail and nothing is republished past
// the cutoff.
func TestNothingIsAdmittedWhilePublishesFailOrRepublishedPastTheCutoff(t *testing.T) {
	f := newFixture(t, 10000, nil)
	ctx := context.Background()
	a := f.admit(t, 100, false)
	f.log.fail("lease-1", 1000)
	got := make(chan error, 1)
	go func() {
		_, err := f.lease.Settle(ctx, a, 50, sum("d"))
		got <- err
	}()
	waitFor(t, "the failure", func() bool {
		_, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot})
		return errors.Is(err, ErrPublishing)
	})
	f.clock.advance(time.Hour)
	if err := <-got; !errors.Is(err, ErrRetry) {
		t.Fatalf("a settle whose record could not be published: %v", err)
	}
	f.log.fail("lease-1", 0)
	f.log.Resume("lease-1")
	time.Sleep(20 * time.Millisecond)
	if n := len(f.log.records(t, "lease-1")); n != 0 {
		t.Fatalf("%d records published past the cutoff", n)
	}
}

// TestAnAnswerAfterTheCutoffIsRecorded: a settle handed over before the
// cutoff and acknowledged after it is answered recorded (§4.5).
func TestAnAnswerAfterTheCutoffIsRecorded(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 100, false)
	f.log.hold()
	got := make(chan Outcome, 1)
	go func() {
		out, err := f.lease.Settle(ctx, a, 80, sum("d"))
		if err != nil {
			t.Error(err)
		}
		got <- out
	}()
	waitFor(t, "the record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	f.clock.advance(59 * time.Second)
	f.log.letGo()
	if out := <-got; out != (Outcome{Kind: record.Settle, Charge: 80, Recorded: true}) {
		t.Fatalf("a settle acknowledged past the cutoff: %+v", out)
	}
}

// TestTheBooksKeepTheirIdentities: on random admissions, heartbeats and
// terminals, held is the open holds' estimates, consumed the settles'
// charges, the allocation rises only by the shortfall, remaining is never
// negative, and the log holds every record in sequence, its terminals'
// charges summing to consumed and each one's shortfall total the owner's at
// that point.
func TestTheBooksKeepTheirIdentities(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		rng := rand.New(rand.NewSource(seed))
		f := newFixture(t, 2000, func(e int64) int64 { return e / 20 })
		ctx := context.Background()
		open := map[string]int64{}
		var consumed int64
		gseq := map[string]int64{}
		for step := 0; step < 80; step++ {
			switch op := rng.Intn(4); {
			case op == 0 || len(open) == 0:
				e := int64(1 + rng.Intn(300))
				a, err := f.lease.Admit(Admission{Estimate: e, Stream: rng.Intn(2) == 0, Boot: boot})
				if err == nil {
					open[a.Auth] = e
				} else if !errors.Is(err, ErrNoRoom) {
					t.Fatal(err)
				}
			default:
				var a string
				for a = range open {
					break
				}
				e := open[a]
				switch op {
				case 1:
					gseq[a]++
					if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: gseq[a], Hash: sum(fmt.Sprint(a, gseq[a])),
						Usage: gseq[a], Running: min(e, gseq[a]), Basis: []byte("terms")}); err != nil {
						t.Fatal(err)
					}
				case 2:
					charge := int64(rng.Intn(int(2*e + 1)))
					if _, err := f.lease.Settle(ctx, a, charge, sum("d")); err != nil {
						t.Fatal(err)
					}
					consumed += charge
					delete(open, a)
				case 3:
					if _, err := f.lease.Refund(ctx, a); err != nil {
						t.Fatal(err)
					}
					delete(open, a)
				}
			}
			b := f.lease.Books()
			var held int64
			for _, e := range open {
				held += e
			}
			if b.Held != held || b.Consumed != consumed || b.Allocation != 2000+b.Shortfall || b.Remaining() < 0 ||
				b.Pending != 0 || b.Open != len(open) {
				t.Fatalf("seed %d step %d: books %+v, held %d, consumed %d", seed, step, b, held, consumed)
			}
		}
		var sum, shortfall int64
		for i, r := range f.log.records(t, "lease-1") {
			if r.Seq != int64(i+1) || r.Epoch != 3 {
				t.Fatalf("seed %d: record %d is %+v", seed, i, r)
			}
			if r.Kind.Terminal() {
				sum += r.Charge
				if r.Shortfall < shortfall {
					t.Fatalf("seed %d: a shortfall total fell: %+v", seed, r)
				}
				shortfall = r.Shortfall
			}
		}
		if sum != consumed || shortfall != f.lease.Books().Shortfall {
			t.Fatalf("seed %d: the log's terminals charge %d, shortfall %d; the books %+v", seed, sum, shortfall,
				f.lease.Books())
		}
		f.owner.Let("lease-1")
	}
}

func TestAnOwnerHoldsOnlyItsGrantedLeases(t *testing.T) {
	f := newFixture(t, 100, nil)
	if _, ok := f.owner.Lease("lease-2"); ok {
		t.Fatal("an owner holds a lease it was never granted")
	}
	if _, err := f.owner.Take("lease-1", "ws-1", 100, start); err == nil {
		t.Fatal("a lease taken twice")
	}
	if l, ok := f.owner.Lease("lease-1"); !ok || l.ID() != "lease-1" {
		t.Fatal("the granted lease")
	}
	if _, err := New(Config{}, f.log); err == nil {
		t.Fatal("an owner with no configuration")
	}
}

// TestRecordsCarryWhatTheirKindsNeed: a hold's first heartbeat record, and
// only it, carries the reap's basis; a refund's carries its hold's boot
// binding, a settle's its digest; and the records' times are UTC whatever
// the zone of the owner's clock, as the format requires.
func TestRecordsCarryWhatTheirKindsNeed(t *testing.T) {
	f := newFixture(t, 1000, nil)
	f.clock.mu.Lock()
	f.clock.now = start.In(time.FixedZone("PDT", -7*3600))
	f.clock.mu.Unlock()
	ctx := context.Background()
	a := f.admit(t, 500, true)
	first, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 2, Running: 2,
		Echoed: first, Basis: []byte("other terms")}); err != nil {
		t.Fatal(err)
	}
	refunded, err := f.lease.Admit(Admission{Estimate: 100, Boot: []byte("boot-2")})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Refund(ctx, refunded.Auth); err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Settle(ctx, a, 2, sum("full")); err != nil {
		t.Fatal(err)
	}
	recs := f.log.records(t, "lease-1")
	if len(recs) != 4 {
		t.Fatalf("the records: %+v", recs)
	}
	if hb := recs[0]; !hb.First || string(hb.Basis) != "terms" || hb.Snapshot.Deadline.Location() != time.UTC {
		t.Fatalf("the first heartbeat's record: %+v", hb)
	}
	if hb := recs[1]; hb.First || hb.Basis != nil || hb.Snapshot.GatewaySeq != 2 {
		t.Fatalf("the second heartbeat's record: %+v", hb)
	}
	if r := recs[2]; r.Kind != record.Refund || r.Auth != refunded.Auth || string(r.Boot) != "boot-2" || r.Digest != nil {
		t.Fatalf("the refund's record: %+v", r)
	}
	if r := recs[3]; r.Kind != record.Settle || !bytes.Equal(r.Digest, sum("full")) || r.Boot != nil {
		t.Fatalf("the settle's record: %+v", r)
	}
}

// TestAnOwnerMintsOnlyWhatItCanRecord: the owner takes no lease and admits
// no hold whose ID the record format cannot carry, nor an authorization its
// minter gave before, open or decided.
func TestAnOwnerMintsOnlyWhatItCanRecord(t *testing.T) {
	ctx := context.Background()
	id := "gwa-0"
	o, err := New(Config{Epoch: 1, Skew: time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: time.Minute, Clock: func() time.Time { return start },
		NewAuthorization: func(string) (string, error) { return id, nil }}, newFakeLog())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.Take(strings.Repeat("l", 33), "ws-1", 100, start.Add(time.Hour)); err == nil {
		t.Fatal("a lease whose ID the format cannot carry")
	}
	l, err := o.Take("lease-1", "ws-1", 100, start.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	defer o.Let("lease-1")
	if _, err := l.Admit(Admission{Estimate: 1}); err == nil {
		t.Fatal("an admission with no boot binding")
	}
	id = strings.Repeat("a", 65)
	if _, err := l.Admit(Admission{Estimate: 1, Boot: boot}); err == nil {
		t.Fatal("a hold whose authorization the format cannot carry")
	}
	if b := l.Books(); b.Held != 0 || b.Open != 0 {
		t.Fatalf("the books after the refused admissions: %+v", b)
	}
	id = "gwa-1"
	if _, err := l.Admit(Admission{Estimate: 1, Boot: boot}); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Admit(Admission{Estimate: 2, Boot: boot}); err == nil {
		t.Fatal("an open authorization minted again")
	}
	if b := l.Books(); b.Held != 1 || b.Open != 1 {
		t.Fatalf("the books after the refusals: %+v", b)
	}
	if _, err := l.Refund(ctx, "gwa-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Admit(Admission{Estimate: 3, Boot: boot}); err == nil {
		t.Fatal("a decided authorization minted again")
	}
	if b := l.Books(); b.Held != 0 || b.Open != 0 {
		t.Fatalf("the books at the end: %+v", b)
	}
}
