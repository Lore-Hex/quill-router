package auditor

import (
	"context"
	"errors"
	"fmt"
	"os"
	"slices"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The runtime's tests run on the Spanner emulator, as the store's own do
// (fastpath/README.md); without it they skip, and the member's tests run.
var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "auditor tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "auditor tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "auditor tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

var grantee = store.Owner{Node: "owner-1", Epoch: 3}

// flaky is the store, with failures the tests ask for: a call that fails
// before it reaches Spanner, or a commit that lands and then answers an
// error.
type flaky struct {
	*store.Store
	mu                             sync.Mutex
	failFind, failLoad, failCommit int
	lostCommits, lostStops         int
	failLoadOf                     map[string]int // by lease ID
	// gone makes ReadLease find no lease.
	gone    bool
	commits [][]store.CommitRequest
	// finding, when set, is told of each FindLease, which then waits for
	// found to close, whatever its context.
	finding, found chan struct{}
}

func (f *flaky) take(n *int) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if *n > 0 {
		*n--
		return true
	}
	return false
}

var errInjected = errors.New("a failure the test injected")

func (f *flaky) FindLease(ctx context.Context, id string) (store.LeaseRef, error) {
	if f.finding != nil {
		select {
		case f.finding <- struct{}{}:
		default:
		}
		<-f.found
	}
	if f.take(&f.failFind) {
		return store.LeaseRef{}, errInjected
	}
	return f.Store.FindLease(ctx, id)
}

func (f *flaky) ReadLease(ctx context.Context, ref store.LeaseRef) (store.Lease, time.Time, error) {
	f.mu.Lock()
	gone := f.gone
	f.mu.Unlock()
	if gone {
		return store.Lease{}, time.Time{}, store.ErrNoLease
	}
	return f.Store.ReadLease(ctx, ref)
}

func (f *flaky) Load(ctx context.Context, ref store.LeaseRef) (store.Loaded, error) {
	f.mu.Lock()
	of := f.failLoadOf[ref.LeaseID] > 0
	if of {
		f.failLoadOf[ref.LeaseID]--
	}
	f.mu.Unlock()
	if of || f.take(&f.failLoad) {
		return store.Loaded{}, errInjected
	}
	return f.Store.Load(ctx, ref)
}

func (f *flaky) failLoads(lease string, n int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failLoadOf == nil {
		f.failLoadOf = map[string]int{}
	}
	f.failLoadOf[lease] = n
}

func (f *flaky) StopForGap(ctx context.Context, ref store.LeaseRef, version, seq int64) (bool, time.Time, error) {
	ok, at, err := f.Store.StopForGap(ctx, ref, version, seq)
	if err == nil && f.take(&f.lostStops) {
		return false, time.Time{}, errInjected
	}
	return ok, at, err
}

func (f *flaky) Commit(ctx context.Context, reqs []store.CommitRequest) ([]store.CommitResult, time.Time, error) {
	f.mu.Lock()
	f.commits = append(f.commits, slices.Clone(reqs))
	f.mu.Unlock()
	if f.take(&f.failCommit) {
		return nil, time.Time{}, errInjected
	}
	got, at, err := f.Store.Commit(ctx, reqs)
	if err == nil && f.take(&f.lostCommits) {
		return nil, time.Time{}, errInjected
	}
	return got, at, err
}

func (f *flaky) commitCalls() [][]store.CommitRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.commits)
}

// fakeDelivery is a record the test delivers; acks counts its
// acknowledgements.
type fakeDelivery struct {
	lease string
	data  []byte
	at    time.Time
	mu    sync.Mutex
	acks  int
}

func (d *fakeDelivery) Lease() string        { return d.lease }
func (d *fakeDelivery) Data() []byte         { return d.data }
func (d *fakeDelivery) Published() time.Time { return d.at }
func (d *fakeDelivery) Ack() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.acks++
}

func (d *fakeDelivery) acked() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.acks
}

// runtimeFixture is a runtime on the emulator's store, and a lease of 1,000
// it granted.
type runtimeFixture struct {
	t      *testing.T
	s      *store.Store
	store  *flaky
	ref    store.LeaseRef
	alerts []string
	mu     sync.Mutex
}

func newRuntimeFixture(t *testing.T) *runtimeFixture {
	t.Helper()
	if emulator == nil {
		t.Skip(skipped)
	}
	s, err := store.New(shared, store.Config{LiveFor: time.Hour, Window: 30 * time.Second, Skew: 2 * time.Second,
		PublishDeadline: 5 * time.Second, MaxLife: 5 * time.Minute, Grace: time.Minute, Allowance: 1_000_000,
		RequiredTier: 3})
	if err != nil {
		t.Fatal(err)
	}
	f := &runtimeFixture{t: t, s: s, store: &flaky{Store: s}}
	f.ref = f.grant()
	return f
}

func (f *runtimeFixture) grant() store.LeaseRef {
	f.t.Helper()
	ctx := context.Background()
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.InsertMap("tr_credit_balance", map[string]any{
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000), "trust_tier": int64(3)})}); err != nil {
		f.t.Fatal(err)
	}
	id := store.NewLeaseID()
	got, err := f.s.Grant(ctx, store.GrantRequest{Workspace: ws, LeaseID: id, Region: "us-central1", Owner: grantee,
		Amount: 1000, KeyStatusVersion: 7})
	if err != nil || got.Refused != "" {
		f.t.Fatalf("the grant: %+v %v", got, err)
	}
	return store.LeaseRef{Workspace: ws, LeaseID: id}
}

func (f *runtimeFixture) runtime(changes ...func(*Config)) *Runtime {
	f.t.Helper()
	cfg := Config{Store: f.store, Skew: 2 * time.Second, CommitEvery: time.Hour, MaxBatch: 10, Retry: time.Millisecond,
		ForgetAfter: time.Hour,
		Alert: func(lease, what string) {
			f.mu.Lock()
			defer f.mu.Unlock()
			f.alerts = append(f.alerts, what)
		}}
	for _, change := range changes {
		change(&cfg)
	}
	rt, err := New(cfg)
	if err != nil {
		f.t.Fatal(err)
	}
	return rt
}

func (f *runtimeFixture) alerted() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.alerts)
}

// on is a record of the fixture's lease, delivered.
func on(t *testing.T, ref store.LeaseRef, r record.Record) *fakeDelivery {
	t.Helper()
	r.Lease = ref.LeaseID
	data, err := record.Encode(r)
	if err != nil {
		t.Fatalf("the test's record %+v: %v", r, err)
	}
	return &fakeDelivery{lease: ref.LeaseID, data: data, at: start}
}

// round is one round of commits, and the reads again it leads to.
func round(rt *Runtime) {
	rt.commitAll(context.Background())
	rt.workers.Wait()
}

func handleAll(rt *Runtime, ds ...*fakeDelivery) {
	for _, d := range ds {
		rt.handle(context.Background(), d)
	}
}

func acks(ds ...*fakeDelivery) []int {
	out := make([]int, len(ds))
	for i, d := range ds {
		out[i] = d.acked()
	}
	return out
}

func (f *runtimeFixture) loaded() store.Loaded {
	f.t.Helper()
	l, err := f.s.Load(context.Background(), f.ref)
	if err != nil {
		f.t.Fatal(err)
	}
	return l
}

// TestTheRuntimeAcknowledgesWhatACommitMadeDurable: records are applied as
// they come and acknowledged once the commit that books them lands; a
// redelivery is acknowledged with no commit.
func TestTheRuntimeAcknowledgesWhatACommitMadeDurable(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	ds := []*fakeDelivery{on(t, f.ref, hb(1, "a", 1, 10)), on(t, f.ref, settle(2, "a", 40, 0)),
		on(t, f.ref, ckpt(3, record.CheckpointOf{Consumed: 40, KeyStatus: 7}))}
	handleAll(rt, ds...)
	if got := acks(ds...); !slices.Equal(got, []int{0, 0, 0}) {
		t.Fatalf("acknowledged before the commit: %v", got)
	}
	round(rt)
	if got := acks(ds...); !slices.Equal(got, []int{1, 1, 1}) {
		t.Fatalf("acknowledged after the commit: %v", got)
	}
	l := f.loaded()
	if l.Lease.AppliedSeq != 3 || l.Lease.AuditOsum != 40 || l.Lease.Consumed != 40 || l.Lease.CommitVersion != 1 ||
		len(l.Holds) != 0 {
		t.Fatalf("the lease after the commit: %+v", l)
	}
	again := on(t, f.ref, settle(2, "a", 40, 0))
	handleAll(rt, again)
	round(rt)
	if again.acked() != 1 || len(f.store.commitCalls()) != 1 {
		t.Fatalf("a redelivery: acknowledged %d, %d commits", again.acked(), len(f.store.commitCalls()))
	}
}

// TestARefusedCommitAppliesTheRecordsAgain: a member whose commit another
// member's refused reads the lease again and applies its records again;
// what the other committed it skips, and nothing is booked twice.
func TestARefusedCommitAppliesTheRecordsAgain(t *testing.T) {
	f := newRuntimeFixture(t)
	a, b := f.runtime(), f.runtime()
	r1, r2, r3 := settle(1, "a", 40, 0), settle(2, "b", 7, 0), settle(3, "c", 5, 0)
	b1 := on(t, f.ref, r1)
	handleAll(b, b1) // b reads the lease at version 0
	handleAll(a, on(t, f.ref, r1), on(t, f.ref, r2))
	round(a)
	b2, b3 := on(t, f.ref, r2), on(t, f.ref, r3)
	handleAll(b, b2, b3)
	round(b) // refused: read again, 1 and 2 skipped, 3 applied
	if got := acks(b1, b2, b3); !slices.Equal(got, []int{0, 0, 0}) {
		t.Fatalf("acknowledged after a refused commit: %v", got)
	}
	round(b)
	if got := acks(b1, b2, b3); !slices.Equal(got, []int{1, 1, 1}) {
		t.Fatalf("acknowledged after the commit: %v", got)
	}
	if l := f.loaded(); l.Lease.AppliedSeq != 3 || l.Lease.Consumed != 52 || l.Lease.CommitVersion != 2 {
		t.Fatalf("the lease: %+v", l.Lease)
	}
}

// TestACommitWhoseAnswerIsLost: the runtime reads the lease again; the
// commit had landed, so its records are skipped, and acknowledged at the
// next round with nothing booked twice.
func TestACommitWhoseAnswerIsLost(t *testing.T) {
	f := newRuntimeFixture(t)
	f.store.lostCommits = 1
	rt := f.runtime()
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(2, "b", 7, 0))}
	handleAll(rt, ds...)
	round(rt)
	if got := acks(ds...); !slices.Equal(got, []int{0, 0}) {
		t.Fatalf("acknowledged when the commit's answer was lost: %v", got)
	}
	round(rt)
	if got := acks(ds...); !slices.Equal(got, []int{1, 1}) || len(f.store.commitCalls()) != 1 {
		t.Fatalf("acknowledged %v after %d commits", got, len(f.store.commitCalls()))
	}
	if l := f.loaded(); l.Lease.AppliedSeq != 2 || l.Lease.Consumed != 47 {
		t.Fatalf("the lease: %+v", l.Lease)
	}
}

// TestAGapStopsTheLease: what came before the gap is committed, then the
// lease is stopped at the gap's record; every record of it is acknowledged,
// those to come too, and a person is told.
func TestAGapStopsTheLease(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(3, "c", 5, 0))}
	handleAll(rt, ds...)
	if got := acks(ds...); !slices.Equal(got, []int{1, 1}) {
		t.Fatalf("acknowledged at the gap: %v", got)
	}
	l := f.loaded()
	if l.Lease.AppliedSeq != 1 || l.Lease.Consumed != 40 || l.Lease.GapSeq != (spanner.NullInt64{Int64: 3, Valid: true}) {
		t.Fatalf("the lease stopped at the gap: %+v", l.Lease)
	}
	later := on(t, f.ref, settle(4, "d", 1, 0))
	handleAll(rt, later)
	if later.acked() != 1 || !slices.Contains(f.alerted(), "a gap in the lease's records stopped it") {
		t.Fatalf("a record after the gap: acknowledged %d, alerts %v", later.acked(), f.alerted())
	}
}

// TestTheRuntimesFenceTickStoresS: a member that loaded the lease open reads the
// fence when the tick comes, and its commit stores S.
func TestTheRuntimesFenceTickStoresS(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	handleAll(rt, on(t, f.ref, settle(1, "a", 40, 0)))
	ctx := context.Background()
	if ok, _, err := f.s.OwnerMarkDraining(ctx, grantee, f.ref); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	row, _, err := f.s.ReadLease(ctx, f.ref)
	if err != nil || !row.FenceTime.Valid {
		t.Fatalf("the drained lease: %+v %v", row, err)
	}
	d := on(t, f.ref, tick(1, row.FenceTime.Time.Add(2*time.Second)))
	handleAll(rt, d)
	rt.commitAll(ctx)
	if l := f.loaded(); l.Lease.BoundarySeq != (spanner.NullInt64{Int64: 1, Valid: true}) || d.acked() != 1 {
		t.Fatalf("S after the fence tick: %+v, acknowledged %d", l.Lease, d.acked())
	}
}

// TestAManifestReadsTheWinnersFirst: a member that loaded an open lease
// reads its winners before a manifest lists the holds, and does not put
// back a hold another member's commit decided.
func TestAManifestReadsTheWinnersFirst(t *testing.T) {
	f := newRuntimeFixture(t)
	a := f.runtime()
	handleAll(a, on(t, f.ref, settle(1, "a", 40, 0)))
	round(a)
	held := []record.HeldHold{{Auth: "a", Estimate: 100, Deadline: deadline, Boot: boot},
		{Auth: "b", Estimate: 50, Deadline: deadline, Boot: boot}}
	b := f.runtime()
	handleAll(b, on(t, f.ref, chunkRec(2, held...)), on(t, f.ref, manifestRec(3, digestOf(t, held...), 2)))
	round(b)
	l := f.loaded()
	if len(l.Holds) != 1 || l.Holds[0].AuthorizationID != "b" || !l.Holds[0].Listed ||
		l.Lease.HoldsListedSeq != (spanner.NullInt64{Int64: 3, Valid: true}) || len(l.Chunks) != 0 {
		t.Fatalf("the listing: %+v", l)
	}
}

// TestRecordsOfNoLease are acknowledged as they come, and a person told.
func TestRecordsOfNoLease(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	none := store.LeaseRef{Workspace: "ws", LeaseID: store.NewLeaseID()}
	ds := []*fakeDelivery{on(t, none, settle(1, "a", 40, 0)), on(t, none, settle(2, "b", 7, 0))}
	handleAll(rt, ds...)
	if got := acks(ds...); !slices.Equal(got, []int{1, 1}) ||
		!slices.Contains(f.alerted(), "a record of a lease the store does not have") {
		t.Fatalf("records of no lease: acknowledged %v, alerts %v", got, f.alerted())
	}
}

// TestARecordNoMemberCanRead is never applied: a person is told, it is
// acknowledged with the commit after it, and an owner record after it is
// a gap.
func TestARecordNoMemberCanRead(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	bad := &fakeDelivery{lease: f.ref.LeaseID, data: []byte(`{"v":1,`), at: start}
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0)), bad}
	handleAll(rt, ds...)
	round(rt)
	if got := acks(ds...); !slices.Equal(got, []int{1, 1}) ||
		!slices.Contains(f.alerted(), "a record the auditor cannot read") {
		t.Fatalf("a record no member can read: acknowledged %v, alerts %v", got, f.alerted())
	}
	handleAll(rt, on(t, f.ref, settle(3, "c", 5, 0)))
	if l := f.loaded(); l.Lease.GapSeq != (spanner.NullInt64{Int64: 3, Valid: true}) {
		t.Fatalf("the record after it: %+v", l.Lease)
	}
}

// TestFailedReadsAreTriedAgain: the runtime waits and reads again until the
// store answers.
func TestFailedReadsAreTriedAgain(t *testing.T) {
	f := newRuntimeFixture(t)
	f.store.failFind, f.store.failLoad = 2, 2
	rt := f.runtime()
	d := on(t, f.ref, settle(1, "a", 40, 0))
	handleAll(rt, d)
	round(rt)
	if l := f.loaded(); l.Lease.AppliedSeq != 1 || d.acked() != 1 {
		t.Fatalf("after failed reads: %+v, acknowledged %d", l.Lease, d.acked())
	}
}

// TestCommitsAreBatched: one commit carries at most MaxBatch leases.
func TestCommitsAreBatched(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime(func(c *Config) { c.MaxBatch = 2 })
	refs := []store.LeaseRef{f.ref, f.grant(), f.grant()}
	for _, ref := range refs {
		handleAll(rt, on(t, ref, settle(1, "a", 40, 0)))
	}
	round(rt)
	calls := f.store.commitCalls()
	if len(calls) != 2 || len(calls[0]) != 2 || len(calls[1]) != 1 {
		t.Fatalf("the commits: %d, of %v", len(calls), calls)
	}
}

// TestABusyLeaseWaitsForTheNextRound: a lease whose handler holds it is
// left to the next round, which commits it.
func TestABusyLeaseWaitsForTheNextRound(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	d := on(t, f.ref, settle(1, "a", 40, 0))
	handleAll(rt, d)
	h := rt.held(f.ref.LeaseID)
	h.mu.Lock()
	round(rt)
	h.mu.Unlock()
	if len(f.store.commitCalls()) != 0 || d.acked() != 0 {
		t.Fatal("a busy lease was committed")
	}
	round(rt)
	if d.acked() != 1 {
		t.Fatal("the next round did not commit it")
	}
}

// fakeSource delivers its records in order, then waits for ctx.
type fakeSource struct{ ds []*fakeDelivery }

func (s fakeSource) Receive(ctx context.Context, handle func(context.Context, Delivery)) error {
	for _, d := range s.ds {
		handle(ctx, d)
	}
	<-ctx.Done()
	return nil
}

// TestRunCommitsOnItsOwn: Run commits every CommitEvery, and returns once
// ctx ends.
func TestRunCommitsOnItsOwn(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime(func(c *Config) { c.CommitEvery = 5 * time.Millisecond })
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(2, "b", 7, 0))}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- rt.Run(ctx, fakeSource{ds}) }()
	deadline := time.Now().Add(10 * time.Second)
	for !slices.Equal(acks(ds...), []int{1, 1}) {
		if time.Now().After(deadline) {
			t.Fatalf("acknowledged %v", acks(ds...))
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// TestACommitsFaultsAreTold: a person is told of an audit fault, and of a
// charge past the allocation, once the commit that stores it lands.
func TestACommitsFaultsAreTold(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	handleAll(rt, on(t, f.ref, settle(1, "a", 1500, 0)), on(t, f.ref, ckpt(2, record.CheckpointOf{Consumed: 9,
		KeyStatus: 7})))
	if len(f.alerted()) != 0 {
		t.Fatalf("told before the commit: %v", f.alerted())
	}
	round(rt)
	if got := f.alerted(); !slices.Contains(got, "a charge past the lease's allocation") ||
		!slices.Contains(got, "an audit fault: the owner's checkpoint disagrees with its records") {
		t.Fatalf("the alerts: %v", got)
	}
}

// TestAGapAfterARefusedCommitBooksWhatCameBefore: a member's commit before a
// gap is refused, so it reads the lease again and applies every record it
// kept: those another member committed it skips, and the rest it commits
// before it stops the lease.
func TestAGapAfterARefusedCommitBooksWhatCameBefore(t *testing.T) {
	f := newRuntimeFixture(t)
	a, b := f.runtime(), f.runtime()
	r1, r2, r4 := settle(1, "a", 40, 0), settle(2, "b", 7, 0), settle(4, "d", 1, 0)
	handleAll(a, on(t, f.ref, r1), on(t, f.ref, r2)) // a reads the lease at version 0
	handleAll(b, on(t, f.ref, r1))
	round(b)
	handleAll(a, on(t, f.ref, r4)) // the gap: a's commit is refused, it reads again, and commits 2
	l := f.loaded()
	if l.Lease.AppliedSeq != 2 || l.Lease.Consumed != 47 || l.Lease.GapSeq != (spanner.NullInt64{Int64: 4, Valid: true}) {
		t.Fatalf("the lease stopped at the gap: %+v", l.Lease)
	}
}

// TestALeaseStoppedAtAGapIsDone: a runtime that reads a lease stopped at a
// gap acknowledges its records as they come.
func TestALeaseStoppedAtAGapIsDone(t *testing.T) {
	f := newRuntimeFixture(t)
	handleAll(f.runtime(), on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(3, "c", 5, 0)))
	d := on(t, f.ref, settle(4, "d", 1, 0))
	rt := f.runtime()
	handleAll(rt, d)
	if d.acked() != 1 || rt.held(f.ref.LeaseID).lease != nil {
		t.Fatalf("a record of a stopped lease: acknowledged %d", d.acked())
	}
}

// TestAFailedReadAgainHoldsUpOnlyItsLease: a lease whose commit was refused,
// and whose reads then keep failing, leaves the round to commit the other
// leases, and is read again once Spanner answers.
func TestAFailedReadAgainHoldsUpOnlyItsLease(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime(func(c *Config) { c.MaxBatch = 2 })
	other := f.runtime()
	refs := []store.LeaseRef{f.ref, f.grant(), f.grant()}
	a1 := on(t, refs[0], settle(1, "a", 40, 0))
	handleAll(rt, a1) // rt reads lease A at version 0
	handleAll(other, on(t, refs[0], settle(1, "a", 40, 0)))
	round(other) // A at version 1: rt's commit of it will be refused
	a2, b1, c1 := on(t, refs[0], settle(2, "b", 7, 0)), on(t, refs[1], settle(1, "a", 40, 0)), on(t, refs[2], settle(1, "a", 40, 0))
	handleAll(rt, a2, b1, c1)
	f.store.failLoads(refs[0].LeaseID, 1_000_000)
	done := make(chan struct{})
	go func() {
		rt.commitAll(context.Background())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("a lease whose reads fail held up the round")
	}
	if got := acks(b1, c1); !slices.Equal(got, []int{1, 1}) {
		t.Fatalf("the other leases' records: acknowledged %v", got)
	}
	f.store.failLoads(refs[0].LeaseID, 0)
	rt.workers.Wait()
	round(rt)
	if got := acks(a1, a2); !slices.Equal(got, []int{1, 1}) {
		t.Fatalf("lease A's records once read again: acknowledged %v", got)
	}
	if l, err := f.s.Load(context.Background(), refs[0]); err != nil || l.Lease.AppliedSeq != 2 || l.Lease.Consumed != 47 {
		t.Fatalf("lease A: %+v %v", l.Lease, err)
	}
}

// TestALostCommitStillTells: a commit that stored a fault, and whose answer
// was lost, is told of when the member reads the lease again.
func TestALostCommitStillTells(t *testing.T) {
	f := newRuntimeFixture(t)
	f.store.lostCommits = 1
	rt := f.runtime()
	handleAll(rt, on(t, f.ref, settle(1, "a", 1500, 0)), on(t, f.ref, ckpt(2, record.CheckpointOf{Consumed: 9,
		KeyStatus: 7})))
	round(rt)
	if got := f.alerted(); !slices.Contains(got, "a charge past the lease's allocation") ||
		!slices.Contains(got, "an audit fault: the owner's checkpoint disagrees with its records") {
		t.Fatalf("the alerts after a lost commit: %v", got)
	}
}

// TestALostGapStopStillTells: a stop at a gap whose answer was lost is told
// of when the member reads the lease again, and finds it stopped.
func TestALostGapStopStillTells(t *testing.T) {
	f := newRuntimeFixture(t)
	f.store.lostStops = 1
	rt := f.runtime()
	ds := []*fakeDelivery{on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(3, "c", 5, 0))}
	handleAll(rt, ds...)
	if got := acks(ds...); !slices.Equal(got, []int{1, 1}) ||
		!slices.Contains(f.alerted(), "a gap in the lease's records stopped it") {
		t.Fatalf("after a lost stop: acknowledged %v, alerts %v", got, f.alerted())
	}
}

// TestADoneLeaseStaysKnown: for ForgetAfter, a lease that is done has its
// later records acknowledged without being read again, and is not told of
// again; then it is forgotten, and read again if a record of it comes.
func TestADoneLeaseStaysKnown(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	handleAll(rt, on(t, f.ref, settle(1, "a", 40, 0)), on(t, f.ref, settle(3, "c", 5, 0)))
	round(rt)
	later := on(t, f.ref, settle(4, "d", 1, 0))
	handleAll(rt, later)
	gaps := func() (n int) {
		for _, a := range f.alerted() {
			if a == "a gap in the lease's records stopped it" {
				n++
			}
		}
		return n
	}
	if later.acked() != 1 || gaps() != 1 {
		t.Fatalf("a record of a lease done: acknowledged %d, %d gap alerts", later.acked(), gaps())
	}
	rt.cfg.ForgetAfter = 0
	round(rt)
	handleAll(rt, on(t, f.ref, settle(5, "e", 1, 0)))
	if gaps() != 2 {
		t.Fatalf("a lease forgotten and read again: %d gap alerts", gaps())
	}
}

// TestAClosedLeasesRecordsAreAcknowledged as they come: none is booked.
func TestAClosedLeasesRecordsAreAcknowledged(t *testing.T) {
	f := newRuntimeFixture(t)
	ctx := context.Background()
	if ok, _, err := f.s.OwnerMarkDraining(ctx, grantee, f.ref); err != nil || !ok {
		t.Fatalf("the draining write: %v %v", ok, err)
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.UpdateMap("tr_lease", map[string]any{
		"workspace_id": f.ref.Workspace, "lease_id": f.ref.LeaseID, "state": "closed",
		"closed_at": time.Now(), "close_kind": "operator"})}); err != nil {
		t.Fatal(err)
	}
	rt := f.runtime()
	d := on(t, f.ref, settle(1, "a", 40, 0))
	handleAll(rt, d)
	if l := f.loaded(); d.acked() != 1 || l.Lease.AppliedSeq != 0 {
		t.Fatalf("a record of a closed lease: acknowledged %d, the lease %+v", d.acked(), l.Lease)
	}
}

// TestARecoveryNeverWaitsForTheLease: the round that starts a lease's read
// again returns at once, though a handler holds the lease; the worker reads
// it once the handler lets it go.
func TestARecoveryNeverWaitsForTheLease(t *testing.T) {
	f := newRuntimeFixture(t)
	rt := f.runtime()
	d := on(t, f.ref, settle(1, "a", 40, 0))
	handleAll(rt, d)
	h := rt.held(f.ref.LeaseID)
	h.mu.Lock()
	h.lease, h.recovering = nil, true // as the round leaves a lease whose commit was refused
	started := make(chan struct{})
	go func() {
		rt.recover(context.Background(), h)
		close(started)
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("starting a lease's read again waited for the handler that holds it")
	}
	h.mu.Unlock()
	rt.workers.Wait()
	round(rt)
	if d.acked() != 1 {
		t.Fatalf("the lease read again: acknowledged %d", d.acked())
	}
}

// TestADoneLeasesReadTellsItsRow: a member that reads a lease stopped at a
// gap, or closed, tells what its row holds: the gap, an audit fault, a
// charge past the allocation.
func TestADoneLeasesReadTellsItsRow(t *testing.T) {
	f := newRuntimeFixture(t)
	ctx := context.Background()
	got, _, err := f.s.Commit(ctx, []store.CommitRequest{{Ref: f.ref, AppliedSeq: 2, AuditOsum: 1500,
		Money: []store.MoneyOp{store.Book(1500, 0)}, AuditFault: ptr(int64(2)),
		Winners: []store.Winner{{AuthorizationID: "a", Kind: "settle", Charge: 1500, RecordID: "o1"}}}})
	if err != nil || len(got) != 1 || got[0].Refused != "" {
		t.Fatalf("the commit: %+v %v", got, err)
	}
	if ok, _, err := f.s.StopForGap(ctx, f.ref, got[0].NewVersion, 4); err != nil || !ok {
		t.Fatalf("the stop: %v %v", ok, err)
	}
	rt := f.runtime()
	handleAll(rt, on(t, f.ref, settle(3, "c", 5, 0)))
	alerts := f.alerted()
	for _, want := range []string{"a gap in the lease's records stopped it",
		"an audit fault: the owner's checkpoint disagrees with its records", "a charge past the lease's allocation"} {
		if !slices.Contains(alerts, want) {
			t.Fatalf("the alerts %v, without %q", alerts, want)
		}
	}
}

// leftSource is a source whose Receive returns once ctx ends, though a
// handler it started still runs, as the client library's can after its
// shutdown timeout.
type leftSource struct{ d Delivery }

func (s leftSource) Receive(ctx context.Context, handle func(context.Context, Delivery)) error {
	go handle(ctx, s.d)
	<-ctx.Done()
	return nil
}

// TestRunWaitsForItsHandlers: Run returns only once a handler its source
// left running has ended. The handler is held in a read that does not end
// with ctx; Run waits for it past ctx's end, and returns once it ends.
func TestRunWaitsForItsHandlers(t *testing.T) {
	f := newRuntimeFixture(t)
	f.store.finding, f.store.found = make(chan struct{}, 1), make(chan struct{})
	rt := f.runtime()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- rt.Run(ctx, leftSource{d: on(t, f.ref, settle(1, "a", 40, 0))}) }()
	<-f.store.finding
	cancel()
	select {
	case <-done:
		t.Fatal("Run returned while its handler ran")
	case <-time.After(300 * time.Millisecond):
	}
	close(f.store.found)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not return once its handler ended")
	}
}

// TestABehindReadTellsWhatTheRowHolds: a member that read the lease open
// and then needs its row, for a tick, tells what another member stored
// there since, and a lease that member stopped at a gap is done; so is one
// the store no longer has, which is told.
func TestABehindReadTellsWhatTheRowHolds(t *testing.T) {
	f := newRuntimeFixture(t)
	a, b := f.runtime(), f.runtime()
	handleAll(a, on(t, f.ref, settle(1, "a", 40, 0)))
	round(a) // a has the lease, open, at version 1
	handleAll(b, on(t, f.ref, settle(2, "b", 1500, 0)), on(t, f.ref, ckpt(3, record.CheckpointOf{Consumed: 9,
		KeyStatus: 7})))
	round(b)
	handleAll(b, on(t, f.ref, settle(5, "d", 1, 0))) // the gap: b stops the lease
	if l := f.loaded().Lease; !l.GapSeq.Valid || !l.AuditFaultSeq.Valid || l.FaultUsage == 0 {
		t.Fatalf("the row b wrote: %+v", l)
	}
	f.mu.Lock()
	f.alerts = nil
	f.mu.Unlock()
	tk := on(t, f.ref, tick(1, start))
	handleAll(a, tk)
	got := f.alerted()
	for _, want := range []string{"a gap in the lease's records stopped it",
		"an audit fault: the owner's checkpoint disagrees with its records", "a charge past the lease's allocation"} {
		if !slices.Contains(got, want) {
			t.Fatalf("a's alerts %v lack %q", got, want)
		}
	}
	if tk.acked() != 1 {
		t.Fatalf("the tick of a lease a found stopped: acknowledged %d", tk.acked())
	}
	later := on(t, f.ref, settle(6, "e", 1, 0))
	handleAll(a, later)
	if later.acked() != 1 {
		t.Fatalf("a later record of the lease a found stopped: acknowledged %d", later.acked())
	}

	f = newRuntimeFixture(t)
	rt := f.runtime()
	handleAll(rt, on(t, f.ref, settle(1, "a", 40, 0)))
	round(rt)
	f.store.mu.Lock()
	f.store.gone = true
	f.store.mu.Unlock()
	tk = on(t, f.ref, tick(1, start))
	handleAll(rt, tk)
	if tk.acked() != 1 || !slices.Contains(f.alerted(), "a record of a lease the store does not have") {
		t.Fatalf("a lease the store no longer has: acknowledged %d, alerts %v", tk.acked(), f.alerted())
	}
}
