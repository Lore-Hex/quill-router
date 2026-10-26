package auditor

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// lines keeps what a part logs.
type lines struct {
	mu  sync.Mutex
	got []string
}

func (l *lines) logf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.got = append(l.got, fmt.Sprintf(format, args...))
}

func (l *lines) with(s string) []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	var out []string
	for _, line := range l.got {
		if strings.Contains(line, s) {
			out = append(out, line)
		}
	}
	return out
}

// within runs f, and fails the test if it has not returned by d.
func within(t *testing.T, d time.Duration, what string, f func()) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		f()
	}()
	select {
	case <-done:
	case <-time.After(d):
		t.Fatalf("%s did not return within %v", what, d)
	}
}

// blocking is every store the auditor's parts call, each call held until
// its context ends, as a Spanner call held on the client's side was on
// 2026-10-26; calls counts them by name.
type blocking struct {
	mu    sync.Mutex
	calls map[string]int
}

func (b *blocking) block(ctx context.Context, name string) error {
	b.mu.Lock()
	if b.calls == nil {
		b.calls = map[string]int{}
	}
	b.calls[name]++
	b.mu.Unlock()
	<-ctx.Done()
	return ctx.Err()
}

func (b *blocking) count(name string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.calls[name]
}

func (b *blocking) FindLease(ctx context.Context, _ string) (store.LeaseRef, error) {
	return store.LeaseRef{}, b.block(ctx, "FindLease")
}
func (b *blocking) Load(ctx context.Context, _ store.LeaseRef) (store.Loaded, error) {
	return store.Loaded{}, b.block(ctx, "Load")
}
func (b *blocking) ReadLease(ctx context.Context, _ store.LeaseRef) (store.Lease, time.Time, error) {
	return store.Lease{}, time.Time{}, b.block(ctx, "ReadLease")
}
func (b *blocking) LoadWinners(ctx context.Context, _ store.LeaseRef) ([]store.Pack, time.Time, error) {
	return nil, time.Time{}, b.block(ctx, "LoadWinners")
}
func (b *blocking) Commit(ctx context.Context, _ []store.CommitRequest) ([]store.CommitResult, time.Time, error) {
	return nil, time.Time{}, b.block(ctx, "Commit")
}
func (b *blocking) StopForGap(ctx context.Context, _ store.LeaseRef, _, _ int64) (bool, time.Time, error) {
	return false, time.Time{}, b.block(ctx, "StopForGap")
}
func (b *blocking) ReadDrainSince(ctx context.Context, _ store.LeaseRef, _ time.Time) ([]store.DrainRow, time.Time, error) {
	return nil, time.Time{}, b.block(ctx, "ReadDrainSince")
}
func (b *blocking) Reap(ctx context.Context, _ store.LeaseRef, _ int64, _ time.Time, _ store.ReapRow) (store.Refusal, time.Time, error) {
	return "", time.Time{}, b.block(ctx, "Reap")
}
func (b *blocking) CloseLease(ctx context.Context, _ store.LeaseRef, _ int64, _, _ time.Time) (store.CloseResult, error) {
	return store.CloseResult{}, b.block(ctx, "CloseLease")
}
func (b *blocking) ScanExpired(ctx context.Context, _ time.Time, _ int) ([]store.Expired, time.Time, error) {
	return nil, time.Time{}, b.block(ctx, "ScanExpired")
}
func (b *blocking) AuditorMarkDraining(ctx context.Context, _ store.LeaseRef, _ time.Time) (bool, time.Time, error) {
	return false, time.Time{}, b.block(ctx, "AuditorMarkDraining")
}
func (b *blocking) ScanDraining(ctx context.Context, _ string, _ store.LeaseRef, _ int) ([]store.LeaseRef, error) {
	return nil, b.block(ctx, "ScanDraining")
}
func (b *blocking) PendingPacks(ctx context.Context, _ store.PendingPack, _ int) ([]store.PendingPack, error) {
	return nil, b.block(ctx, "PendingPacks")
}
func (b *blocking) ReadStaged(ctx context.Context, _ string, _ []byte) (store.StagedRecord, bool, error) {
	return store.StagedRecord{}, false, b.block(ctx, "ReadStaged")
}
func (b *blocking) WriteRecords(ctx context.Context, _ []store.LeaseRecord) error {
	return b.block(ctx, "WriteRecords")
}
func (b *blocking) MarkPackDone(ctx context.Context, _ store.LeaseRef, _ int64) (bool, error) {
	return false, b.block(ctx, "MarkPackDone")
}
func (b *blocking) DropStaged(ctx context.Context, _ ...store.StagedKey) error {
	return b.block(ctx, "DropStaged")
}
func (b *blocking) RetiredStaged(ctx context.Context, _ int) ([]store.StagedKey, error) {
	return nil, b.block(ctx, "RetiredStaged")
}
func (b *blocking) StageRecord(ctx context.Context, _ store.StagedRecord) error {
	return b.block(ctx, "StageRecord")
}

// TestEveryStoreCallHasADeadline: each Spanner call of the runtime, the
// ticker, the pending work and the stager returns once its deadline has
// run out, though the call itself would wait for ever, with the deadline's
// error, and the failure is logged by its class alone.
func TestEveryStoreCallHasADeadline(t *testing.T) {
	const d = 20 * time.Millisecond
	b, l := &blocking{}, &lines{}
	bd := bound{d: d, j: newJournal(l.logf)}
	rs, ts := boundStore{s: b, bound: bd}, boundTickStore{s: b, bound: bd}
	ps, ss := boundPendingStore{s: b, bound: bd}, boundStagingStore{s: b, bound: bd}
	ref := store.LeaseRef{Workspace: "ws-secret", LeaseID: "lease-1"}
	ctx := context.Background()
	calls := map[string]func() error{
		"runtime FindLease":      func() error { _, err := rs.FindLease(ctx, "lease-1"); return err },
		"runtime Load":           func() error { _, err := rs.Load(ctx, ref); return err },
		"runtime ReadLease":      func() error { _, _, err := rs.ReadLease(ctx, ref); return err },
		"runtime LoadWinners":    func() error { _, _, err := rs.LoadWinners(ctx, ref); return err },
		"runtime Commit":         func() error { _, _, err := rs.Commit(ctx, nil); return err },
		"runtime StopForGap":     func() error { _, _, err := rs.StopForGap(ctx, ref, 1, 2); return err },
		"runtime ReadDrainSince": func() error { _, _, err := rs.ReadDrainSince(ctx, ref, time.Time{}); return err },
		"runtime Reap": func() error {
			_, _, err := rs.Reap(ctx, ref, 1, time.Time{}, store.ReapRow{})
			return err
		},
		"runtime CloseLease": func() error {
			_, err := rs.CloseLease(ctx, ref, 1, time.Time{}, time.Time{})
			return err
		},
		"ticker ScanExpired":         func() error { _, _, err := ts.ScanExpired(ctx, time.Time{}, 1); return err },
		"ticker AuditorMarkDraining": func() error { _, _, err := ts.AuditorMarkDraining(ctx, ref, time.Time{}); return err },
		"ticker ScanDraining":        func() error { _, err := ts.ScanDraining(ctx, "r", ref, 1); return err },
		"pending PendingPacks":       func() error { _, err := ps.PendingPacks(ctx, store.PendingPack{}, 1); return err },
		"pending LoadWinners":        func() error { _, _, err := ps.LoadWinners(ctx, ref); return err },
		"pending ReadStaged":         func() error { _, _, err := ps.ReadStaged(ctx, "a", nil); return err },
		"pending WriteRecords":       func() error { return ps.WriteRecords(ctx, nil) },
		"pending MarkPackDone":       func() error { _, err := ps.MarkPackDone(ctx, ref, 1); return err },
		"pending DropStaged":         func() error { return ps.DropStaged(ctx) },
		"pending RetiredStaged":      func() error { _, err := ps.RetiredStaged(ctx, 1); return err },
		"stager FindLease":           func() error { _, err := ss.FindLease(ctx, "lease-1"); return err },
		"stager StageRecord":         func() error { return ss.StageRecord(ctx, store.StagedRecord{Ref: ref}) },
	}
	// Every method of every store interface is called.
	if n := 9 + 3 + 7 + 2; len(calls) != n {
		t.Fatalf("%d calls, of %d", len(calls), n)
	}
	for name, call := range calls {
		var err error
		within(t, 5*time.Second, name, func() { err = call() })
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("%s: %v", name, err)
		}
	}
	for _, line := range l.with("auditor: spanner") {
		if !strings.HasSuffix(line, "failed: DeadlineExceeded") || strings.Contains(line, "ws-secret") {
			t.Fatalf("the line %q", line)
		}
	}
	if got := l.with("spanner commit failed"); len(got) != 1 {
		t.Fatalf("the commit's lines: %q", got)
	}
}

// TestTheStagerHasADeadline: the stager's store is under StoreDeadline.
func TestTheStagerHasADeadline(t *testing.T) {
	s, err := NewStager(&blocking{}, func(string, string) {})
	if err != nil {
		t.Fatal(err)
	}
	if b, ok := s.store.(boundStagingStore); !ok || b.d != StoreDeadline {
		t.Fatalf("the stager's store is %#v", s.store)
	}
}

// TestFailuresAreLoggedAtMostEveryPeriod: the first failure of a kind is
// logged at once, the rest withheld and counted until the period has
// passed; a failure's text, which can name a workspace, never is, nor a
// failure that is the caller's own ending, nor an answer the callers act
// on.
func TestFailuresAreLoggedAtMostEveryPeriod(t *testing.T) {
	l := &lines{}
	b := bound{d: time.Second, j: newJournal(l.logf)}
	ctx := context.Background()
	unavailable := status.Error(codes.Unavailable, "session for workspace ws-secret")
	for range 3 {
		b.failed(ctx, "load", "lease-1", unavailable)
	}
	b.failed(ctx, "commit", "", errors.New("store: lease {ws-secret lease-1} twice in one commit"))
	b.failed(ctx, "load", "lease-1", store.ErrNoLease)
	b.failed(ctx, "stage record", "lease-1", fmt.Errorf("%w: ws-secret", store.ErrRetired))
	ended, cancel := context.WithCancel(ctx)
	cancel()
	b.failed(ended, "scan draining", "", context.Canceled)
	want := []string{"auditor: spanner load of lease lease-1 failed: Unavailable", "auditor: spanner commit failed: other"}
	if !slices.Equal(l.got, want) {
		t.Fatalf("logged %q", l.got)
	}
	// Once the period has passed, the next line says how many were
	// withheld.
	b.j.mu.Lock()
	b.j.last["spanner load"] = time.Now().Add(-logEvery)
	b.j.mu.Unlock()
	b.failed(ctx, "load", "lease-2", unavailable)
	if got := l.got[len(l.got)-1]; got != "auditor: spanner load of lease lease-2 failed: Unavailable (and 2 more in the last 30s)" {
		t.Fatalf("logged %q", got)
	}
}

// hanging is the fixture's store with a commit that waits: hang commits
// wait until their context ends; with wedge set, every commit waits until
// it is closed, whatever its context; with slow set, every commit takes
// that long first. Every other call, and every commit not made to wait,
// runs under relaxed: the runtime's deadline, which a test shortens for
// the commit it makes hang, does not bound the emulator's own calls.
type hanging struct {
	*flaky
	hang  atomic.Int32
	wedge chan struct{}
	slow  time.Duration
	// entered, when set, is closed as the first wedged commit begins.
	entered chan struct{}
	enter   sync.Once
}

// setupBound bounds each of the emulator's calls the tests make through
// hanging, in place of the runtime's deadline.
const setupBound = 30 * time.Second

// relaxed is ctx without its deadline, bounded by setupBound instead, and
// ended still when ctx is cancelled.
func relaxed(ctx context.Context) (context.Context, context.CancelFunc) {
	r, cancel := context.WithTimeout(context.WithoutCancel(ctx), setupBound)
	stop := context.AfterFunc(ctx, func() {
		if errors.Is(ctx.Err(), context.Canceled) {
			cancel()
		}
	})
	return r, func() { stop(); cancel() }
}

func (h *hanging) Commit(ctx context.Context, reqs []store.CommitRequest) ([]store.CommitResult, time.Time, error) {
	if h.wedge != nil {
		if h.entered != nil {
			h.enter.Do(func() { close(h.entered) })
		}
		<-h.wedge
		return nil, time.Time{}, errInjected
	}
	if h.hang.Add(-1) >= 0 {
		<-ctx.Done()
		return nil, time.Time{}, ctx.Err()
	}
	time.Sleep(h.slow)
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.Commit(ctx, reqs)
}

func (h *hanging) FindLease(ctx context.Context, id string) (store.LeaseRef, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.FindLease(ctx, id)
}

func (h *hanging) Load(ctx context.Context, ref store.LeaseRef) (store.Loaded, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.Load(ctx, ref)
}

func (h *hanging) ReadLease(ctx context.Context, ref store.LeaseRef) (store.Lease, time.Time, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.ReadLease(ctx, ref)
}

func (h *hanging) LoadWinners(ctx context.Context, ref store.LeaseRef) ([]store.Pack, time.Time, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.LoadWinners(ctx, ref)
}

func (h *hanging) StopForGap(ctx context.Context, ref store.LeaseRef, version, seq int64) (bool, time.Time, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.StopForGap(ctx, ref, version, seq)
}

func (h *hanging) ReadDrainSince(ctx context.Context, ref store.LeaseRef, cursor time.Time) ([]store.DrainRow, time.Time, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.ReadDrainSince(ctx, ref, cursor)
}

func (h *hanging) Reap(ctx context.Context, ref store.LeaseRef, version int64, tick time.Time,
	r store.ReapRow) (store.Refusal, time.Time, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.Reap(ctx, ref, version, tick, r)
}

func (h *hanging) CloseLease(ctx context.Context, ref store.LeaseRef, version int64, drainRead,
	now time.Time) (store.CloseResult, error) {
	ctx, cancel := relaxed(ctx)
	defer cancel()
	return h.flaky.CloseLease(ctx, ref, version, drainRead, now)
}

// TestACommitThatHangsEndsAtItsDeadline: a commit held on the client's
// side returns once its deadline has run out, as one whose outcome is
// unknown: the round goes on, the lease is read again, and the next round
// commits and acknowledges its records, with nothing booked twice. The
// short deadline reaches the hung commit alone; the emulator's own calls
// run under setupBound, and the setup under a guard of its own.
func TestACommitThatHangsEndsAtItsDeadline(t *testing.T) {
	f := newRuntimeFixture(t)
	h, l := &hanging{flaky: f.store}, &lines{}
	h.hang.Store(1)
	rt := f.runtime(func(c *Config) { c.Store, c.deadline, c.logf = h, 100*time.Millisecond, l.logf })
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(2, "b", 7, 0))}
	within(t, 2*setupBound, "the setup", func() { handleAll(rt, ds...) })
	within(t, 2*setupBound, "a round whose commit hangs", func() { round(rt) })
	if got := acks(ds...); !slices.Equal(got, []int{0, 0}) {
		t.Fatalf("acknowledged after a commit that ran out of time: %v", got)
	}
	if got := l.with("auditor: spanner commit failed: DeadlineExceeded"); len(got) != 1 {
		t.Fatalf("logged %q", l.got)
	}
	within(t, 2*setupBound, "the next round", func() { round(rt) })
	if got := acks(ds...); !slices.Equal(got, []int{1, 1}) {
		t.Fatalf("acknowledged after the next round: %v", got)
	}
	if l := f.loaded(); l.Lease.AppliedSeq != 2 || l.Lease.Consumed != 47 || l.Lease.CommitVersion != 1 {
		t.Fatalf("the lease: %+v", l.Lease)
	}
}

// TestASlowRoundIsLogged: a round of commits past SlowCommitRound is
// logged, with how long it took and how many commits it made.
func TestASlowRoundIsLogged(t *testing.T) {
	f := newRuntimeFixture(t)
	l := &lines{}
	rt := f.runtime(func(c *Config) { c.slowRound, c.logf = time.Nanosecond, l.logf })
	handleAll(rt, on(t, f.ref, settle(1, "a", 40, 0)))
	rt.round(context.Background())
	if got := l.with("auditor: a round of commits took "); len(got) != 1 || !strings.Contains(got[0], "(1 commits; slow past 1ns)") {
		t.Fatalf("logged %q", l.got)
	}
}

// TestAWedgedRoundFailsTheMember: a round of commits that never returns,
// its commit deaf to its context, fails the member once it has run past
// its limit: Run ends its context, so its source asks again for the
// records it held, and returns ErrCommitStalled without waiting for the
// round, so the process can exit and the other members take its leases.
func TestAWedgedRoundFailsTheMember(t *testing.T) {
	f := newRuntimeFixture(t)
	h, l := &hanging{flaky: f.store, wedge: make(chan struct{})}, &lines{}
	t.Cleanup(func() { close(h.wedge) })
	rt := f.runtime(func(c *Config) {
		c.Store, c.CommitEvery, c.stall, c.logf = h, 5*time.Millisecond, 300*time.Millisecond, l.logf
	})
	d := on(t, f.ref, settle(1, "a", 40, 0))
	src := &endingSource{d: d}
	var err error
	within(t, 20*time.Second, "Run with a wedged round", func() { err = rt.Run(context.Background(), src) })
	if !errors.Is(err, ErrCommitStalled) {
		t.Fatalf("Run returned %v", err)
	}
	if !src.ended.Load() || d.acked() != 0 {
		t.Fatalf("the source's context ended %v; the record acknowledged %d times", src.ended.Load(), d.acked())
	}
	if got := l.with("auditor: a spanner commit has been under way "); len(got) != 1 {
		t.Fatalf("logged %q", l.got)
	}
}

// endingSource delivers its record, then waits for ctx, and notes that it
// ended: the subscription asks again for every record it holds then.
type endingSource struct {
	d     *fakeDelivery
	ended atomic.Bool
}

func (s *endingSource) Receive(ctx context.Context, handle func(context.Context, Delivery)) error {
	handle(ctx, s.d)
	<-ctx.Done()
	s.ended.Store(true)
	return nil
}

// TestAnIdleRuntimeIsNotStalled: the limit is on a round under way, not on
// the time between rounds: a runtime that has no round to make runs on.
func TestAnIdleRuntimeIsNotStalled(t *testing.T) {
	rt, err := New(Config{Store: &blocking{}, CommitEvery: time.Hour, MaxBatch: 1, Retry: time.Millisecond,
		MaxLife: time.Minute, Records: &fakeRecords{}, Wait: time.Second, stall: time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- rt.Run(ctx, fakeSource{}) }()
	select {
	case err := <-done:
		t.Fatalf("an idle runtime returned %v", err)
	case <-time.After(200 * time.Millisecond):
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// TestATickerRoundWhoseReadHangsGoesOn: the ticker's scan for expired
// leases returns at its deadline, and the round goes on to the draining
// leases, whose read returns at its own.
func TestATickerRoundWhoseReadHangsGoesOn(t *testing.T) {
	b := &blocking{}
	tk, err := NewTicker(TickerConfig{Store: b, Log: leaseLogFunc(nil), Region: "r", Every: time.Hour, Limit: 1,
		Wait: time.Second, deadline: 20 * time.Millisecond, logf: (&lines{}).logf})
	if err != nil {
		t.Fatal(err)
	}
	within(t, 5*time.Second, "the ticker's round", func() { tk.round(context.Background()) })
	if b.count("ScanExpired") != 1 || b.count("ScanDraining") != 1 {
		t.Fatalf("the round scanned %d and %d times", b.count("ScanExpired"), b.count("ScanDraining"))
	}
}

// leaseLogFunc is a LeaseLog that publishes nothing.
type leaseLogFunc func(string, []byte) Waiter

func (f leaseLogFunc) Publish(lease string, data []byte) Waiter { return answered{} }
func (f leaseLogFunc) Resume(string)                            {}

// TestAPendingSweepWhoseReadHangsGoesOn: the pending work's read of its
// packs returns at its deadline, and the sweep goes on to drop what
// retired leases staged.
func TestAPendingSweepWhoseReadHangsGoesOn(t *testing.T) {
	b := &blocking{}
	p, err := NewPending(PendingConfig{Store: b, Records: &fakeRecords{}, Every: time.Hour, Limit: 1, Wait: time.Second,
		Alert: func(string, string) {}, deadline: 20 * time.Millisecond, logf: (&lines{}).logf})
	if err != nil {
		t.Fatal(err)
	}
	within(t, 5*time.Second, "the pending sweep", func() { p.sweep(context.Background()) })
	if b.count("PendingPacks") != 1 || b.count("RetiredStaged") != 1 {
		t.Fatalf("the sweep read %d and %d times", b.count("PendingPacks"), b.count("RetiredStaged"))
	}
}

// TestAWedgedRoundFailsAMemberWhoseSourceFailed: a member whose source
// has failed waits for its round to end before it returns; a round wedged
// meanwhile fails it as well, so it returns rather than waiting for ever.
func TestAWedgedRoundFailsAMemberWhoseSourceFailed(t *testing.T) {
	f := newRuntimeFixture(t)
	h := &hanging{flaky: f.store, wedge: make(chan struct{}), entered: make(chan struct{})}
	t.Cleanup(func() { close(h.wedge) })
	rt := f.runtime(func(c *Config) {
		c.Store, c.CommitEvery, c.stall, c.logf = h, 5*time.Millisecond, 300*time.Millisecond, (&lines{}).logf
	})
	src := failingSource{d: on(t, f.ref, settle(1, "a", 40, 0)), after: h.entered}
	var err error
	within(t, 20*time.Second, "Run with a failed source and a wedged round", func() {
		err = rt.Run(context.Background(), src)
	})
	if !errors.Is(err, ErrCommitStalled) {
		t.Fatalf("Run returned %v", err)
	}
}

// failingSource delivers its record, and fails once after is closed.
type failingSource struct {
	d     *fakeDelivery
	after chan struct{}
}

func (s failingSource) Receive(ctx context.Context, handle func(context.Context, Delivery)) error {
	handle(ctx, s.d)
	<-s.after
	return errors.New("the subscription failed")
}

// TestALongRoundThatMakesProgressRunsOn: the watchdog reads progress, not
// a round's length. A backlog of many dirty leases, one commit a lease
// here, each commit under the limit but the round many times past it, as
// a thousand leases in batches of a hundred at twelve seconds a commit
// would be, runs to its end: every record is acknowledged and Run is not
// failed. TestAWedgedRoundFailsTheMember is the other side: one commit that
// never returns trips it.
func TestALongRoundThatMakesProgressRunsOn(t *testing.T) {
	f := newRuntimeFixture(t)
	const stall, leases = 1500 * time.Millisecond, 10
	h, l := &hanging{flaky: f.store, slow: 250 * time.Millisecond}, &lines{}
	rt := f.runtime(func(c *Config) {
		c.Store, c.MaxBatch, c.CommitEvery, c.stall, c.slowRound, c.logf = h, 1, 5*time.Millisecond, stall, stall, l.logf
	})
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0))}
	for range leases - 1 {
		ds = append(ds, on(t, f.grant(), settle(1, "a", 40, 0)))
	}
	// Every lease dirty before the first round, which so commits them all.
	within(t, 2*setupBound, "the setup", func() { handleAll(rt, ds...) })
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- rt.Run(ctx, fakeSource{}) }()
	deadline := time.Now().Add(2 * setupBound)
	for slices.Contains(acks(ds...), 0) {
		select {
		case err := <-done:
			t.Fatalf("Run returned %v with %v acknowledged", err, acks(ds...))
		case <-time.After(10 * time.Millisecond):
		}
		if time.Now().After(deadline) {
			t.Fatalf("acknowledged %v", acks(ds...))
		}
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run returned %v", err)
	}
	// The round was past the limit: it is logged as slow past it.
	if got := l.with("auditor: a round of commits took "); len(got) == 0 || len(l.with("has been under way")) != 0 {
		t.Fatalf("logged %q", l.got)
	}
}

// deafStore is a store whose FindLease, or Load, waits until wedge is
// closed, whatever its context, once entered has been closed; the rest
// answer at once, as blocking's do not: Load finds a lease of nothing, and
// a reap is refused, after reapTakes.
type deafStore struct {
	*blocking
	deafFind, deafLoad bool
	wedge, entered     chan struct{}
	enter              sync.Once
	reapTakes          time.Duration
}

func (d *deafStore) deaf() {
	d.enter.Do(func() { close(d.entered) })
	<-d.wedge
}

func (d *deafStore) FindLease(ctx context.Context, id string) (store.LeaseRef, error) {
	if d.deafFind {
		d.deaf()
	}
	return store.LeaseRef{Workspace: "w", LeaseID: id}, nil
}

func (d *deafStore) Load(ctx context.Context, ref store.LeaseRef) (store.Loaded, error) {
	if d.deafLoad {
		d.deaf()
	}
	return store.Loaded{}, errInjected
}

func (d *deafStore) Reap(ctx context.Context, _ store.LeaseRef, _ int64, _ time.Time, _ store.ReapRow) (store.Refusal, time.Time, error) {
	time.Sleep(d.reapTakes)
	return store.RefusedNotDue, time.Time{}, nil
}

// TestAWedgedReadInAHandlerFailsTheMember: a handler that reads a lease it
// does not know, its FindLease or its Load deaf to its context, holds the
// lease, which every round passes by, and so is outside any round; the
// watchdog, which reads the calls under way wherever they were made, fails
// the member all the same.
func TestAWedgedReadInAHandlerFailsTheMember(t *testing.T) {
	for _, find := range []bool{true, false} {
		t.Run(map[bool]string{true: "find lease", false: "load"}[find], func(t *testing.T) {
			s := &deafStore{blocking: &blocking{}, deafFind: find, deafLoad: !find, wedge: make(chan struct{}),
				entered: make(chan struct{})}
			t.Cleanup(func() { close(s.wedge) })
			l := &lines{}
			rt, err := New(Config{Store: s, CommitEvery: 5 * time.Millisecond, MaxBatch: 1, Retry: time.Millisecond,
				MaxLife: time.Minute, Records: &fakeRecords{}, Wait: time.Second, stall: 300 * time.Millisecond, logf: l.logf})
			if err != nil {
				t.Fatal(err)
			}
			data, err := encodeSettle("lease-1")
			if err != nil {
				t.Fatal(err)
			}
			d := &fakeDelivery{lease: "lease-1", data: data, at: start}
			var runErr error
			within(t, 20*time.Second, "Run with a wedged handler", func() { runErr = rt.Run(context.Background(), leftSource{d: d}) })
			<-s.entered
			if !errors.Is(runErr, ErrCommitStalled) {
				t.Fatalf("Run returned %v", runErr)
			}
			op := map[bool]string{true: "find lease", false: "load"}[find]
			if got := l.with("auditor: a spanner " + op + " has been under way "); len(got) != 1 {
				t.Fatalf("logged %q", l.got)
			}
		})
	}
}

// encodeSettle is a settle record of lease.
func encodeSettle(lease string) ([]byte, error) {
	r := settle(1, "a", 40, 0)
	r.Lease = lease
	return record.Encode(r)
}

// TestAWedgedReadInARecoveryFailsTheMember: a lease read again after a
// commit whose outcome is unknown is read in a worker of its own, outside
// any round; its Load deaf to its context fails the member.
func TestAWedgedReadInARecoveryFailsTheMember(t *testing.T) {
	f := newRuntimeFixture(t)
	f.store.failCommit = 1
	h, l := &deafLoads{hanging: hanging{flaky: f.store}, wedge: make(chan struct{}), entered: make(chan struct{})}, &lines{}
	t.Cleanup(func() { close(h.wedge) })
	rt := f.runtime(func(c *Config) {
		c.Store, c.CommitEvery, c.stall, c.logf = h, 5*time.Millisecond, 300*time.Millisecond, l.logf
	})
	d := on(t, f.ref, settle(1, "a", 40, 0))
	var err error
	within(t, 2*setupBound, "Run with a wedged recovery", func() { err = rt.Run(context.Background(), fakeSource{ds: []*fakeDelivery{d}}) })
	<-h.entered
	if !errors.Is(err, ErrCommitStalled) || d.acked() != 0 {
		t.Fatalf("Run returned %v; the record acknowledged %d times", err, d.acked())
	}
	if got := l.with("auditor: a spanner load has been under way "); len(got) != 1 {
		t.Fatalf("logged %q", l.got)
	}
}

// deafLoads is hanging whose every Load after the first waits until wedge
// is closed, whatever its context.
type deafLoads struct {
	hanging
	loads          atomic.Int32
	wedge, entered chan struct{}
	enter          sync.Once
}

func (h *deafLoads) Load(ctx context.Context, ref store.LeaseRef) (store.Loaded, error) {
	if h.loads.Add(1) > 1 {
		h.enter.Do(func() { close(h.entered) })
		<-h.wedge
		return store.Loaded{}, errInjected
	}
	return h.hanging.Load(ctx, ref)
}

// TestManyCallsUnderTheirDeadlineDoNotTrip: twenty reaps one after another,
// as a draining lease's handler makes when many holds are due, each well
// under the limit (a fifth of it) and all of them four times past it, as
// twenty reaps at four seconds each are past a minute, never fail the member: the watchdog reads
// each call's age, not the work's.
func TestManyCallsUnderTheirDeadlineDoNotTrip(t *testing.T) {
	const stall = 600 * time.Millisecond
	s := &deafStore{blocking: &blocking{}, reapTakes: stall / 5}
	rt, err := New(Config{Store: s, CommitEvery: time.Hour, MaxBatch: 1, Retry: time.Millisecond, MaxLife: time.Minute,
		Records: &fakeRecords{}, Wait: time.Second, stall: stall, logf: (&lines{}).logf})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- rt.Run(ctx, fakeSource{}) }()
	began := time.Now()
	for range 20 {
		if _, _, err := rt.cfg.Store.Reap(ctx, store.LeaseRef{LeaseID: "l"}, 1, time.Time{}, store.ReapRow{}); err != nil {
			t.Fatal(err)
		}
	}
	if took := time.Since(began); took < 2*stall {
		t.Fatalf("the reaps took %v, not past the limit", took)
	}
	select {
	case err := <-done:
		t.Fatalf("Run returned %v", err)
	default:
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
