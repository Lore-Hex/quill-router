package owner

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// fakeLog is the settle log as Pub/Sub keeps it: each lease's records stored
// in the order handed over; a failed publish pauses the lease's key, and
// every publish to it fails until Resume. A test can fail publishes, hold
// acknowledgements back until it releases them, and see every publish
// attempted, at the time of the test's clock, now. onRepublish runs at
// each publish of a record published before.
type fakeLog struct {
	mu          sync.Mutex
	stored      map[string][][]byte
	paused      map[string]bool
	failNext    map[string]int
	holding     bool
	release     chan struct{}
	now         func() time.Time
	attempts    []attempt
	published   map[string]int
	onRepublish func()
}

type attempt struct {
	at   time.Time
	data string
}

func newFakeLog() *fakeLog {
	return &fakeLog{stored: map[string][][]byte{}, paused: map[string]bool{}, failNext: map[string]int{},
		release: make(chan struct{}), published: map[string]int{}}
}

// attemptsFrom are the publishes attempted at or after t.
func (f *fakeLog) attemptsFrom(t time.Time) []attempt {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []attempt
	for _, a := range f.attempts {
		if !a.at.Before(t) {
			out = append(out, a)
		}
	}
	return out
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
	if f.now != nil {
		f.attempts = append(f.attempts, attempt{at: f.now(), data: string(data)})
	}
	if f.published[string(data)]++; f.published[string(data)] > 1 && f.onRepublish != nil {
		f.onRepublish()
	}
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
	mu   sync.Mutex
	now  time.Time
	step time.Duration
}

// Now is the clock's time, which moves on by step at each reading.
func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now
	c.now = c.now.Add(c.step)
	return now
}

func (c *clock) stepping(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.step = d
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

func newFixture(t *testing.T, allocation int64, overrun func(int64) int64, changes ...func(*Config)) *fixture {
	t.Helper()
	f := &fixture{log: newFakeLog(), clock: &clock{now: start}}
	f.log.now = f.clock.Now
	n := 0
	var mu sync.Mutex
	cfg := Config{Epoch: 3, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Overrun: overrun, Clock: f.clock.Now,
		NewAuthorization: func(lease string) (string, error) {
			mu.Lock()
			defer mu.Unlock()
			n++
			return fmt.Sprintf("gwa-%s-%d", lease, n), nil
		}}
	for _, change := range changes {
		change(&cfg)
	}
	o, err := New(cfg, f.log)
	if err != nil {
		t.Fatal(err)
	}
	f.owner = o
	if f.lease, err = o.Take("lease-1", "ws-1", allocation, start.Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(o.Stop)
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
	deadline := time.Now().Add(5 * time.Second)
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
		"regressed usage":          {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 9, Running: 20, Echoed: first}, ErrRejected},
		"a regressed charge":       {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 19, Echoed: first}, ErrRejected},
		"a charge over its cap":    {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 501, Echoed: first}, ErrRejected},
		"no hash":                  {HeartbeatOf{GatewaySeq: 2, Usage: 11, Running: 21, Echoed: first}, ErrRejected},
		"a hash not SHA-256's":     {HeartbeatOf{GatewaySeq: 2, Hash: []byte("h2"), Usage: 11, Running: 21, Echoed: first}, ErrRejected},
		"a later one with no echo": {HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 11, Running: 21}, ErrRejected},
	} {
		if _, err := f.lease.Heartbeat(ctx, a, c.hb); !errors.Is(err, c.want) {
			t.Errorf("%s: %v, want %v", name, err, c.want)
		}
	}
	plain := f.admit(t, 100, false)
	if _, err := f.lease.Heartbeat(ctx, plain, HeartbeatOf{GatewaySeq: 1, Hash: sum("p1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); !errors.Is(err, ErrRejected) {
		t.Errorf("a heartbeat for a hold that does not stream: %v", err)
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
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 2, Running: 2,
		Echoed: first}); !errors.Is(err, ErrDeadlinePassed) {
		t.Fatalf("its replay: %v", err)
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
	// The same records: each stored as its first publish carried it, and
	// their charges the books'.
	first := map[int64]string{}
	for _, a := range f.log.attemptsFrom(time.Time{}) {
		r, err := record.Decode([]byte(a.data))
		if err != nil {
			t.Fatal(err)
		}
		if _, ok := first[r.Seq]; !ok {
			first[r.Seq] = a.data
		}
	}
	var charged int64
	f.log.mu.Lock()
	for i, b := range f.log.stored["lease-1"] {
		if string(b) != first[int64(i+1)] {
			t.Errorf("record %d is stored as %s, and was first published as %s", i+1, b, first[int64(i+1)])
		}
	}
	f.log.mu.Unlock()
	for _, r := range recs {
		charged += r.Charge
	}
	if b := f.lease.Books(); b.Consumed != charged {
		t.Fatalf("the records charge %d, and the books consumed %d", charged, b.Consumed)
	}
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); err != nil {
		t.Fatalf("an admission once republished: %v", err)
	}
}

// TestNothingIsAdmittedWhilePublishesFailOrRepublishedPastTheCutoff: while
// publishes fail nothing is admitted, and no publish is attempted past the
// cutoff; a settle whose record waits there goes to the drain log.
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
	if err := <-got; !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle whose record could not be published: %v", err)
	}
	f.log.fail("lease-1", 0)
	f.log.Resume("lease-1")
	time.Sleep(3 * lastBackoff)
	if n := len(f.log.records(t, "lease-1")); n != 0 {
		t.Fatalf("%d records published past the cutoff", n)
	}
	if late := f.log.attemptsFrom(start.Add(58 * time.Second)); len(late) != 0 {
		t.Fatalf("%d publishes attempted past the cutoff", len(late))
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
		streams := map[string]bool{}
		var consumed int64
		// The shortfall rule (§4.2), apart from the owner's: after each
		// terminal, what is booked and held past the allocation raises the
		// shortfall total and the allocation by as much.
		allocation, shortfall := int64(2000), int64(0)
		var wantShort []int64
		decided := func() {
			var held int64
			for _, e := range open {
				held += e
			}
			if over := consumed + held - allocation; over > 0 {
				shortfall, allocation = shortfall+over, allocation+over
			}
			wantShort = append(wantShort, shortfall)
		}
		gseq := map[string]int64{}
		granted := map[string]time.Time{}
		for step := 0; step < 80; step++ {
			switch op := rng.Intn(4); {
			case op == 0 || len(open) == 0:
				e := int64(1 + rng.Intn(300))
				stream := rng.Intn(2) == 0
				a, err := f.lease.Admit(Admission{Estimate: e, Stream: stream, Boot: boot})
				if err == nil {
					open[a.Auth], streams[a.Auth] = e, stream
				} else if !errors.Is(err, ErrNoRoom) {
					t.Fatal(err)
				}
			default:
				var a string
				for a = range open {
					break
				}
				e := open[a]
				if op == 1 && !streams[a] {
					op = 3
				}
				switch op {
				case 1:
					gseq[a]++
					deadline, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: gseq[a],
						Hash: sum(fmt.Sprint(a, gseq[a])), Usage: gseq[a], Running: min(e, gseq[a]), Echoed: granted[a],
						Basis: []byte("terms")})
					if err != nil {
						t.Fatal(err)
					}
					granted[a] = deadline
				case 2:
					charge := int64(rng.Intn(int(2*e + 1)))
					if _, err := f.lease.Settle(ctx, a, charge, sum("d")); err != nil {
						t.Fatal(err)
					}
					consumed += charge
					delete(open, a)
					decided()
				case 3:
					if _, err := f.lease.Refund(ctx, a); err != nil {
						t.Fatal(err)
					}
					delete(open, a)
					decided()
				}
			}
			b := f.lease.Books()
			var held int64
			for _, e := range open {
				held += e
			}
			if b.Held != held || b.Consumed != consumed || b.Allocation != allocation || b.Shortfall != shortfall ||
				b.Remaining() < 0 || b.Pending != 0 || b.Open != len(open) {
				t.Fatalf("seed %d step %d: books %+v, held %d, consumed %d", seed, step, b, held, consumed)
			}
		}
		var charged int64
		terminals := 0
		for i, r := range f.log.records(t, "lease-1") {
			if r.Seq != int64(i+1) || r.Epoch != 3 {
				t.Fatalf("seed %d: record %d is %+v", seed, i, r)
			}
			if r.Kind.Terminal() {
				charged += r.Charge
				if terminals >= len(wantShort) || r.Shortfall != wantShort[terminals] {
					t.Fatalf("seed %d: terminal %d carries a shortfall total of %d, and the rule gives %v", seed,
						terminals, r.Shortfall, wantShort)
				}
				terminals++
			}
		}
		if charged != consumed || terminals != len(wantShort) {
			t.Fatalf("seed %d: the log's %d terminals charge %d; %d decided, consumed %d", seed, terminals, charged,
				len(wantShort), consumed)
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

// TestARecordPastTheCutoffWaitsForARenewal: a record that cannot be
// published by the cutoff is kept, not dropped; its settle and every retry
// of it go to the drain log meanwhile, and nothing is attempted past the
// cutoff. A renewal that moves the cutoff republishes it first, with its
// number, before anything new, so the lease's order has no gap.
func TestARecordPastTheCutoffWaitsForARenewal(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a, b := f.admit(t, 100, false), f.admit(t, 100, false)
	f.log.fail("lease-1", 1000)
	got := make(chan error, 1)
	go func() {
		_, err := f.lease.Settle(ctx, a, 50, sum("a"))
		got <- err
	}()
	waitFor(t, "the failure", func() bool {
		_, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot})
		return errors.Is(err, ErrPublishing)
	})
	f.clock.advance(58 * time.Second) // the cutoff
	if err := <-got; !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle whose record waits past the cutoff: %v", err)
	}
	if _, err := f.lease.Settle(ctx, a, 50, sum("a")); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("its retry: %v", err)
	}
	if _, err := f.lease.Settle(ctx, b, 60, sum("b")); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle past the cutoff: %v", err)
	}
	f.log.fail("lease-1", 0)
	time.Sleep(3 * lastBackoff)
	if late := f.log.attemptsFrom(start.Add(58 * time.Second)); len(late) != 0 {
		t.Fatalf("%d publishes attempted past the cutoff", len(late))
	}
	f.lease.Renewed(start.Add(5 * time.Minute))
	waitFor(t, "the republish", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	if _, err := f.lease.Settle(ctx, b, 60, sum("b")); err != nil {
		t.Fatalf("a settle after the renewal: %v", err)
	}
	recs := f.log.records(t, "lease-1")
	if len(recs) != 2 || recs[0].Seq != 1 || recs[0].Auth != a || recs[1].Seq != 2 || recs[1].Auth != b {
		t.Fatalf("the log after the renewal: %+v", recs)
	}
	if out, err := f.lease.Settle(ctx, a, 50, sum("a")); err != nil || out != (Outcome{Kind: record.Settle, Charge: 50}) {
		t.Fatalf("a retry of the first settle: %+v %v", out, err)
	}
}

// TestEachRepublishIsBeforeTheCutoff: the cutoff is read before each record
// is handed over again, not once for all of them, so a republish that takes
// time stops at the cutoff; the records it did not reach wait, and their
// settles go to the drain log. After a renewal, a new record follows them:
// though the key was resumed, it is not published before them.
func TestEachRepublishIsBeforeTheCutoff(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	var auths []string
	for range 3 {
		auths = append(auths, f.admit(t, 100, false))
	}
	later := f.admit(t, 100, false)
	f.clock.advance(57*time.Second + 500*time.Millisecond) // the cutoff is at 58 s
	f.log.mu.Lock()
	f.log.failNext["lease-1"] = 1
	f.log.onRepublish = func() { f.clock.advance(time.Second) } // each republish takes a second
	f.log.mu.Unlock()
	answers := make([]chan error, len(auths))
	outcomes := make([]Outcome, len(auths))
	for i, a := range auths {
		answers[i] = make(chan error, 1)
		go func() {
			out, err := f.lease.Settle(ctx, a, 10, sum(a))
			outcomes[i] = out
			answers[i] <- err
		}()
		waitFor(t, "the settle's hand-over", func() bool { return f.lease.Books().NextSeq > int64(i+1) })
	}
	for i := range auths {
		err := <-answers[i]
		switch {
		case i == 0 && (err != nil || outcomes[0] != (Outcome{Kind: record.Settle, Charge: 10, Recorded: true})):
			t.Errorf("the settle republished at 57.5 s and acknowledged at 58.5 s: %+v %v", outcomes[0], err)
		case i > 0 && !errors.Is(err, ErrPastCutoff):
			t.Errorf("settle %d, not republished by the cutoff: %v", i, err)
		}
	}
	if late := f.log.attemptsFrom(start.Add(58 * time.Second)); len(late) != 0 {
		t.Fatalf("%d publishes attempted past the cutoff", len(late))
	}
	if recs := f.log.records(t, "lease-1"); len(recs) != 1 || recs[0].Seq != 1 {
		t.Fatalf("the log: %+v", recs)
	}
	f.log.mu.Lock()
	f.log.onRepublish = nil
	f.log.mu.Unlock()
	f.lease.Renewed(start.Add(5 * time.Minute))
	if _, err := f.lease.Settle(ctx, later, 10, sum(later)); err != nil {
		t.Fatalf("a settle after the renewal: %v", err)
	}
	recs := f.log.records(t, "lease-1")
	if len(recs) != 4 {
		t.Fatalf("the log after the renewal: %+v", recs)
	}
	for i, r := range recs {
		if r.Seq != int64(i+1) {
			t.Fatalf("record %d of the log is number %d", i, r.Seq)
		}
	}
}

// TestALetLeaseAnswersAsPastItsCutoff: once the owner lets a lease go, a
// handle kept from before admits nothing, decides nothing and publishes
// nothing, a renewal does not bring it back, and a decision acknowledged
// before still stands (§4.3).
func TestALetLeaseAnswersAsPastItsCutoff(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 100, true)
	settled := f.admit(t, 100, false)
	if _, err := f.lease.Settle(ctx, settled, 10, sum("s")); err != nil {
		t.Fatal(err)
	}
	f.owner.Let("lease-1")
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("an admission: %v", err)
	}
	if _, err := f.lease.Refund(ctx, a); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a refund: %v", err)
	}
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat: %v", err)
	}
	f.lease.Renewed(start.Add(time.Hour))
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("an admission after a renewal: %v", err)
	}
	if out, err := f.lease.Settle(ctx, settled, 10, sum("s")); err != nil || out != (Outcome{Kind: record.Settle, Charge: 10}) {
		t.Fatalf("the settle decided before: %+v %v", out, err)
	}
	if n := len(f.log.records(t, "lease-1")); n != 1 {
		t.Fatalf("%d records", n)
	}
}

// TestAHeartbeatAckedAfterTheCutoffIsRetried: a heartbeat's answer depends
// on the owner's decision, so it is given only for a record acknowledged
// before the cutoff; after it, the heartbeat and its replay get retry.
func TestAHeartbeatAckedAfterTheCutoffIsRetried(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 500, true)
	f.clock.advance(57 * time.Second)
	f.log.hold()
	hb := HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1, Basis: []byte("terms")}
	got := make(chan error, 1)
	go func() {
		_, err := f.lease.Heartbeat(ctx, a, hb)
		got <- err
	}()
	waitFor(t, "the record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	f.clock.advance(2 * time.Second)
	f.log.letGo()
	if err := <-got; !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat acknowledged after the cutoff: %v", err)
	}
	if _, err := f.lease.Heartbeat(ctx, a, hb); !errors.Is(err, ErrRetry) {
		t.Fatalf("its replay: %v", err)
	}
}

// TestTheBooksRefuseWhatTheyCannotHold: an admission or a terminal whose
// amounts an int64 cannot sum is refused, and moves nothing.
func TestTheBooksRefuseWhatTheyCannotHold(t *testing.T) {
	f := newFixture(t, 100, func(int64) int64 { return 1 })
	ctx := context.Background()
	if _, err := f.lease.Admit(Admission{Estimate: math.MaxInt64, Boot: boot}); err == nil {
		t.Fatal("a hold whose estimate and buffer overflow")
	}
	a := f.admit(t, 49, false)
	f.admit(t, 49, false)
	before := f.lease.Books()
	for _, charge := range []int64{math.MaxInt64, math.MaxInt64 - 48, -1} {
		if _, err := f.lease.Settle(ctx, a, charge, sum("a")); err == nil {
			t.Fatalf("a charge of %d", charge)
		}
		if after := f.lease.Books(); after != before {
			t.Fatalf("a charge of %d moved the books: %+v, then %+v", charge, before, after)
		}
	}
	if n := len(f.log.records(t, "lease-1")); n != 0 {
		t.Fatalf("%d records", n)
	}
}

// TestAHandOverPastTheCutoffIsRefused: the cutoff is read again once the
// record is encoded, which takes time; a decision or a heartbeat whose
// record would be handed over past it is refused, and moves nothing.
func TestAHandOverPastTheCutoffIsRefused(t *testing.T) {
	f := newFixture(t, 1000, func(e int64) int64 { return e / 10 })
	ctx := context.Background()
	a, b := f.admit(t, 100, false), f.admit(t, 100, true)
	f.clock.advance(57*time.Second + 500*time.Millisecond) // the cutoff is at 58 s
	before := f.lease.Books()
	f.clock.stepping(time.Second) // each reading a second on, as if the encoding took that long
	if _, err := f.lease.Settle(ctx, a, 10, sum("a")); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle whose record is encoded past the cutoff: %v", err)
	}
	f.clock.stepping(0)
	f.clock.mu.Lock()
	f.clock.now = start.Add(57*time.Second + 500*time.Millisecond)
	f.clock.mu.Unlock()
	f.clock.stepping(time.Second)
	if _, err := f.lease.Heartbeat(ctx, b, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat whose record is encoded past the cutoff: %v", err)
	}
	f.clock.stepping(0)
	if after := f.lease.Books(); after != before {
		t.Fatalf("the refusals moved the books: %+v, then %+v", before, after)
	}
	if n := len(f.log.attemptsFrom(time.Time{})); n != 0 {
		t.Fatalf("%d publishes", n)
	}
}

// TestALeaseLetGoIsNotTakenAgain: its records are numbered once, so the
// owner refuses its grant again, though a grant's retry can return it.
func TestALeaseLetGoIsNotTakenAgain(t *testing.T) {
	f := newFixture(t, 1000, nil)
	if _, err := f.lease.Settle(context.Background(), f.admit(t, 100, false), 10, sum("a")); err != nil {
		t.Fatal(err)
	}
	f.owner.Let("lease-1")
	if _, err := f.owner.Take("lease-1", "ws-1", 1000, start.Add(time.Minute)); err == nil {
		t.Fatal("a lease let go is taken again")
	}
}

// TestTheBufferNeverWraps: streams' buffers join the lease's at their first
// heartbeats; one that would take it past an int64 is rejected, and the
// books' free room saturates rather than wraps.
func TestTheBufferNeverWraps(t *testing.T) {
	f := newFixture(t, math.MaxInt64, func(int64) int64 { return math.MaxInt64 / 2 })
	ctx := context.Background()
	var streams []string
	for range 3 {
		streams = append(streams, f.admit(t, 1, true))
	}
	for i, a := range streams {
		_, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum(a), Usage: 1, Running: 1,
			Basis: []byte("terms")})
		if i < 2 && err != nil {
			t.Fatalf("stream %d's first heartbeat: %v", i, err)
		}
		if i == 2 && !errors.Is(err, ErrRejected) {
			t.Fatalf("a first heartbeat whose buffer passes an int64: %v", err)
		}
		if b := f.lease.Books(); b.Buffer < 0 || b.Free() > b.Allocation-b.Held {
			t.Fatalf("after stream %d's heartbeat: %+v, free %d", i, b, b.Free())
		}
	}
	if _, err := f.lease.Admit(Admission{Estimate: math.MaxInt64 / 4, Boot: boot}); !errors.Is(err, ErrNoRoom) {
		t.Fatalf("an admission into a full buffer: %v", err)
	}
	if free := (Books{Allocation: 100, Pending: math.MaxInt64, Buffer: math.MaxInt64}).Free(); free >= 0 {
		t.Fatalf("free room of %d with more pending and buffered than an int64 holds", free)
	}
}

// TestAReplayAnswersAsTheHeartbeatItRepeats: by the deadline the original
// echoed, whatever the replay echoes.
func TestAReplayAnswersAsTheHeartbeatItRepeats(t *testing.T) {
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
	second := HeartbeatOf{GatewaySeq: 2, Hash: sum("h2"), Usage: 2, Running: 2, Echoed: first}
	go func() {
		_, err := f.lease.Heartbeat(ctx, a, second)
		got <- err
	}()
	waitFor(t, "the second record", func() bool { return len(f.log.records(t, "lease-1")) == 2 })
	f.clock.advance(31 * time.Second)
	f.log.letGo()
	if err := <-got; !errors.Is(err, ErrDeadlinePassed) {
		t.Fatalf("the second heartbeat: %v", err)
	}
	second.Echoed = time.Time{}
	if _, err := f.lease.Heartbeat(ctx, a, second); !errors.Is(err, ErrDeadlinePassed) {
		t.Fatalf("its replay echoing nothing: %v", err)
	}
	second.Echoed = start.Add(time.Hour)
	if _, err := f.lease.Heartbeat(ctx, a, second); !errors.Is(err, ErrDeadlinePassed) {
		t.Fatalf("its replay echoing a later deadline: %v", err)
	}
}

// TestEveryLetWaitsForTheLeaseToStop: a second Let while the first is under
// way returns only once the flusher has stopped, as the first does.
func TestEveryLetWaitsForTheLeaseToStop(t *testing.T) {
	f := newFixture(t, 1000, nil)
	f.lease.mu.Lock() // the first Let waits for the lease's lock
	firstDone, secondDone := make(chan struct{}), make(chan struct{})
	go func() {
		f.owner.Let("lease-1")
		close(firstDone)
	}()
	waitFor(t, "the first Let to begin", func() bool {
		_, held := f.owner.Lease("lease-1")
		return !held
	})
	go func() {
		f.owner.Let("lease-1")
		close(secondDone)
	}()
	select {
	case <-secondDone:
		t.Fatal("a second Let returned while the first is under way")
	case <-time.After(50 * time.Millisecond):
	}
	f.lease.mu.Unlock()
	for _, done := range []chan struct{}{firstDone, secondDone} {
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Fatal("a Let did not return")
		}
	}
	select {
	case <-f.lease.stopped:
	default:
		t.Fatal("Let returned before the flusher stopped")
	}
}

// TestARecordTooLargeIsNotNumbered: a record the settle log could not carry
// is refused before it takes a number, so it never holds back the lease's
// records after it; and an admission's boot binding is bounded, so its
// refund always fits.
func TestARecordTooLargeIsNotNumbered(t *testing.T) {
	f := newFixture(t, 1000, nil)
	ctx := context.Background()
	a, b := f.admit(t, 100, true), f.admit(t, 100, false)
	if _, err := f.lease.Heartbeat(ctx, a, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: bytes.Repeat([]byte("x"), 2<<20)}); !errors.Is(err, ErrRejected) {
		t.Fatalf("a heartbeat whose record is past the settle log's size: %v", err)
	}
	if b := f.lease.Books(); b.NextSeq != 1 {
		t.Fatalf("the refused record took a number: %+v", b)
	}
	if _, err := f.lease.Refund(ctx, b); err != nil {
		t.Fatalf("a refund after it: %v", err)
	}
	if recs := f.log.records(t, "lease-1"); len(recs) != 1 || recs[0].Seq != 1 || recs[0].Kind != record.Refund {
		t.Fatalf("the records: %+v", recs)
	}
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: bytes.Repeat([]byte("b"), 2<<10)}); err == nil {
		t.Fatal("an admission whose boot binding its refund could not carry")
	}
	big, err := f.lease.Admit(Admission{Estimate: 1, Boot: bytes.Repeat([]byte("b"), 1<<10)})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.lease.Refund(ctx, big.Auth); err != nil {
		t.Fatalf("the refund of the longest boot binding: %v", err)
	}
}
