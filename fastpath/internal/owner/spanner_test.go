package owner

import (
	"context"
	"errors"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fakeSpanner is the store as an owner sees it: each round's renewals,
// which a lease takes, at the expiry the fake answers, or is refused; each
// shortfall write tried, which can fail, be held, or be refused; and each
// draining write.
type fakeSpanner struct {
	mu           sync.Mutex
	expiry       time.Time
	refuse       map[string]bool
	rounds       [][]string
	writes       []int64
	landed       []int64
	failWrites   int
	refuseWrites bool
	gate         chan struct{}
	drained      []string
	// drainGate holds a draining write until closed; one cancelled takes
	// afterCancel to return, and drainReturned is closed once it has.
	drainGate     chan struct{}
	afterCancel   time.Duration
	drainReturned chan struct{}
	// drainHold, when set, holds a cancelled draining write until closed.
	drainHold chan struct{}
}

func newFakeSpanner() *fakeSpanner { return &fakeSpanner{refuse: map[string]bool{}} }

func (f *fakeSpanner) Renew(ctx context.Context, owner store.Owner, refs []store.LeaseRef) ([]store.RenewResult, time.Time, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var round []string
	out := make([]store.RenewResult, len(refs))
	for i, ref := range refs {
		round = append(round, ref.LeaseID)
		out[i] = store.RenewResult{Ref: ref, Renewed: !f.refuse[ref.LeaseID]}
		if out[i].Renewed {
			out[i].Expiry = f.expiry
		}
	}
	f.rounds = append(f.rounds, round)
	return out, f.expiry, nil
}

func (f *fakeSpanner) ShortfallWrite(ctx context.Context, owner store.Owner, ref store.LeaseRef, total int64) (store.ShortfallResult, error) {
	f.mu.Lock()
	f.writes = append(f.writes, total)
	gate := f.gate
	f.mu.Unlock()
	if gate != nil {
		select {
		case <-gate:
		case <-ctx.Done():
			return store.ShortfallResult{}, ctx.Err()
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	switch {
	case f.refuseWrites:
		return store.ShortfallResult{Refused: store.RefusedClosed}, nil
	case f.failWrites > 0:
		f.failWrites--
		return store.ShortfallResult{}, errors.New("unavailable")
	}
	f.landed = append(f.landed, total)
	return store.ShortfallResult{}, nil
}

func (f *fakeSpanner) OwnerMarkDraining(ctx context.Context, owner store.Owner, ref store.LeaseRef) (bool, time.Time, error) {
	f.mu.Lock()
	gate, afterCancel, returned, hold := f.drainGate, f.afterCancel, f.drainReturned, f.drainHold
	f.mu.Unlock()
	if gate != nil {
		select {
		case <-gate:
		case <-ctx.Done():
			if hold != nil {
				<-hold
			}
			time.Sleep(afterCancel)
			if returned != nil {
				close(returned)
			}
			return false, time.Time{}, ctx.Err()
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.drained = append(f.drained, ref.LeaseID)
	return true, time.Time{}, nil
}

func (f *fakeSpanner) state() (rounds [][]string, writes, landed []int64, drained []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.rounds), slices.Clone(f.writes), slices.Clone(f.landed), slices.Clone(f.drained)
}

func spannerFixture(t *testing.T, allocation int64, overrun func(int64) int64) (*fixture, *fakeSpanner) {
	t.Helper()
	sp := newFakeSpanner()
	sp.expiry = start.Add(10 * time.Minute)
	f := newFixture(t, allocation, overrun, func(c *Config) {
		c.Spanner, c.Node, c.RenewEvery, c.Window, c.KeyStatus = sp, "owner-1", time.Hour, 30*time.Second, 7
	})
	return f, sp
}

func (f *fixture) lastRecord(t *testing.T) record.Record {
	t.Helper()
	recs := f.log.records(t, "lease-1")
	if len(recs) == 0 {
		t.Fatal("no record")
	}
	return recs[len(recs)-1]
}

// TestARenewalTakesSpannersExpiry: the lease's cutoff moves to the expiry
// Spanner answered, not one from the owner's clock.
func TestARenewalTakesSpannersExpiry(t *testing.T) {
	f, sp := spannerFixture(t, 1000, nil)
	f.clock.advance(57 * time.Second)
	if err := f.owner.Renew(context.Background()); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(5 * time.Second) // past the grant's cutoff, before the renewed one
	f.admit(t, 1, false)
	f.lease.mu.Lock()
	expiry := f.lease.expiry
	f.lease.mu.Unlock()
	if !expiry.Equal(sp.expiry) {
		t.Fatalf("the lease's expiry is %v, and Spanner answered %v", expiry, sp.expiry)
	}
	if rounds, _, _, _ := sp.state(); len(rounds) != 1 || !slices.Equal(rounds[0], []string{"lease-1"}) {
		t.Fatalf("the renewals: %v", rounds)
	}
}

// TestEachRenewalPublishesACheckpoint: the lease's consumed, over the
// terminals before it and not what they free while unacknowledged, its open
// holds' count, sum and latest end of life, and the key-status version;
// nothing returned while it admits.
func TestEachRenewalPublishesACheckpoint(t *testing.T) {
	f, _ := spannerFixture(t, 100_000, nil)
	ctx := context.Background()
	a := f.admit(t, 100, false)
	var held int64
	for i := 1; i <= 20; i++ {
		f.clock.advance(time.Second)
		f.admit(t, int64(10*i), false)
		held += int64(10 * i)
	}
	f.log.hold()
	settled := make(chan error, 1)
	go func() {
		_, err := f.lease.Settle(ctx, a, 30, sum("a"))
		settled <- err
	}()
	waitFor(t, "the settle's record", func() bool { return len(f.log.records(t, "lease-1")) == 1 })
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	f.log.letGo()
	if err := <-settled; err != nil {
		t.Fatal(err)
	}
	r := f.lastRecord(t)
	want := record.CheckpointOf{Consumed: 30, Open: 20, OpenSum: held, LatestEnd: start.Add(20*time.Second + time.Hour),
		KeyStatus: 7}
	if r.Kind != record.Checkpoint || r.Seq != 2 || *r.Checkpoint != want {
		t.Fatalf("the checkpoint: %+v %+v, want %+v", r, r.Checkpoint, want)
	}
	if b := f.lease.Books(); b.Allocation != 100_000 {
		t.Fatalf("a checkpoint while admitting moved the allocation: %+v", b)
	}
}

// TestAClosingLeaseReturnsItsFreeRoomAndFinishes: a lease that admits
// nothing more returns its free room in its next checkpoint, keeping its
// open holds and their buffer; once none is open its final checkpoint, once
// acknowledged, is followed by the draining write, and the owner lets it go.
func TestAClosingLeaseReturnsItsFreeRoomAndFinishes(t *testing.T) {
	f, sp := spannerFixture(t, 1000, func(e int64) int64 { return e / 10 })
	ctx := context.Background()
	a := f.admit(t, 200, false) // held 200, its buffer 20
	f.lease.Close()
	if _, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrClosing) {
		t.Fatalf("an admission under a closing lease: %v", err)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if r := f.lastRecord(t); r.Checkpoint == nil || r.Checkpoint.Return != 780 || r.Checkpoint.Final {
		t.Fatalf("the closing lease's checkpoint: %+v", r)
	}
	if b := f.lease.Books(); b.Allocation != 220 || b.Free() != 0 {
		t.Fatalf("the books after the return: %+v", b)
	}
	if _, err := f.lease.Settle(ctx, a, 150, sum("a")); err != nil {
		t.Fatal(err)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	r := f.lastRecord(t)
	if want := (record.CheckpointOf{Consumed: 150, KeyStatus: 7, Return: 70, Final: true}); r.Checkpoint == nil ||
		*r.Checkpoint != want {
		t.Fatalf("the final checkpoint: %+v, want %+v", r.Checkpoint, want)
	}
	waitFor(t, "the lease let go", func() bool {
		_, held := f.owner.Lease("lease-1")
		return !held
	})
	if _, _, _, drained := sp.state(); !slices.Equal(drained, []string{"lease-1"}) {
		t.Fatalf("the draining writes: %v", drained)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if n := len(f.log.records(t, "lease-1")); n != 3 {
		t.Fatalf("%d records, and the final checkpoint was the last", n)
	}
}

// TestAFinalCheckpointWaitsForItsAcknowledgement: the draining write follows
// the final checkpoint's acknowledgement, not its hand-over.
func TestAFinalCheckpointWaitsForItsAcknowledgement(t *testing.T) {
	f, sp := spannerFixture(t, 1000, nil)
	f.lease.Close()
	f.log.hold()
	if err := f.owner.Renew(context.Background()); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond)
	if _, _, _, drained := sp.state(); len(drained) != 0 {
		t.Fatal("the lease was marked draining before its final checkpoint was acknowledged")
	}
	f.log.letGo()
	waitFor(t, "the draining write", func() bool {
		_, _, _, drained := sp.state()
		return len(drained) == 1
	})
}

// TestARefusedRenewalDropsTheLease: a lease that takes no renewal is
// draining, revoked or another process's, and the owner stops using it at
// once (LeaseLifecycle's OwnerDrops): it holds it no more, publishes nothing
// for it, renews it no more, and answers a terminal under it past_cutoff and
// a heartbeat retry.
func TestARefusedRenewalDropsTheLease(t *testing.T) {
	f, sp := spannerFixture(t, 1000, nil)
	ctx := context.Background()
	a, s := f.admit(t, 100, false), f.admit(t, 100, true)
	sp.mu.Lock()
	sp.refuse["lease-1"] = true
	sp.mu.Unlock()
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if _, held := f.owner.Lease("lease-1"); held {
		t.Fatal("a lease whose renewal was refused is still held")
	}
	if _, err := f.lease.Settle(ctx, a, 10, sum("a")); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("a settle under a dropped lease: %v", err)
	}
	if _, err := f.lease.Heartbeat(ctx, s, HeartbeatOf{GatewaySeq: 1, Hash: sum("h1"), Usage: 1, Running: 1,
		Basis: []byte("terms")}); !errors.Is(err, ErrRetry) {
		t.Fatalf("a heartbeat under a dropped lease: %v", err)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if rounds, _, _, _ := sp.state(); len(rounds) != 1 {
		t.Fatalf("a dropped lease renewed again: %v", rounds)
	}
	if n := len(f.log.records(t, "lease-1")); n != 0 {
		t.Fatalf("%d records under a dropped lease", n)
	}
}

// TestAStoppedOwnerTakesNoLease: Stop lets every lease go, and a Take after
// it, its writer cancelled, is refused.
func TestAStoppedOwnerTakesNoLease(t *testing.T) {
	f, _ := spannerFixture(t, 1000, nil)
	f.owner.Stop()
	if _, err := f.owner.Take("lease-2", "ws-1", 100, start.Add(time.Minute)); err == nil {
		t.Fatal("a stopped owner took a lease")
	}
}

// TestTheShortfallWriterStoresEachTotal: one write in flight per lease; a
// shortfall that arises meanwhile waits, and the next write carries the total
// as it then stands; a failed write is retried until it lands; a refused one
// ends the writer.
func TestTheShortfallWriterStoresEachTotal(t *testing.T) {
	f, sp := spannerFixture(t, 100, nil)
	ctx := context.Background()
	a, b, c, d := f.admit(t, 25, false), f.admit(t, 25, false), f.admit(t, 25, false), f.admit(t, 25, false)
	settle := func(auth string, charge int64) int64 {
		t.Helper()
		if _, err := f.lease.Settle(ctx, auth, charge, sum(auth)); err != nil {
			t.Fatal(err)
		}
		return f.lease.Books().Shortfall
	}
	sp.mu.Lock()
	sp.gate = make(chan struct{})
	sp.mu.Unlock()
	first := settle(a, 90)
	waitFor(t, "the first write", func() bool {
		_, writes, _, _ := sp.state()
		return len(writes) == 1
	})
	second := settle(b, 40)
	time.Sleep(20 * time.Millisecond)
	if _, writes, _, _ := sp.state(); !slices.Equal(writes, []int64{first}) {
		t.Fatalf("writes while one is in flight: %v", writes)
	}
	sp.mu.Lock()
	sp.failWrites = 2
	close(sp.gate)
	sp.gate = nil
	sp.mu.Unlock()
	waitFor(t, "the total stored", func() bool {
		_, _, landed, _ := sp.state()
		return slices.Equal(landed, []int64{second})
	})
	// The held write failed, and the retries carry the total as it stands.
	if _, writes, _, _ := sp.state(); !slices.Equal(writes, []int64{first, second, second}) {
		t.Fatalf("the writes: %v", writes)
	}
	sp.mu.Lock()
	sp.refuseWrites = true
	sp.mu.Unlock()
	third := settle(c, 60)
	waitFor(t, "the refused write", func() bool {
		_, writes, _, _ := sp.state()
		return len(writes) == 4
	})
	if fourth := settle(d, 50); fourth <= third {
		t.Fatalf("the last settle raised the total from %d to %d", third, fourth)
	}
	time.Sleep(3 * firstBackoff)
	if _, writes, _, _ := sp.state(); !slices.Equal(writes, []int64{first, second, second, third}) {
		t.Fatalf("writes after a refusal: %v", writes)
	}
}

// TestALeaseWhosePublishesFailIsNotRenewed: once its publishes have failed
// for longer than the expiry window, the owner stops renewing the lease; it
// expires and drains, and past its cutoff the owner lets it go.
func TestALeaseWhosePublishesFailIsNotRenewed(t *testing.T) {
	f, sp := spannerFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 100, false)
	f.log.fail("lease-1", 1_000_000)
	go func() { _, _ = f.lease.Settle(ctx, a, 10, sum("a")) }()
	waitFor(t, "the failure", func() bool {
		_, err := f.lease.Admit(Admission{Estimate: 1, Boot: boot})
		return errors.Is(err, ErrPublishing)
	})
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	f.clock.advance(31 * time.Second)
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if rounds, _, _, _ := sp.state(); len(rounds) != 1 {
		t.Fatalf("renewals of a lease failing past the window: %v", rounds)
	}
	if _, held := f.owner.Lease("lease-1"); !held {
		t.Fatal("let go before its cutoff")
	}
	f.clock.advance(10 * time.Minute)
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if _, held := f.owner.Lease("lease-1"); held {
		t.Fatal("a lease not renewed and past its cutoff is still held")
	}
}

// TestALeaseWhosePublishesRecoverIsRenewed: the window counts from when the
// lease's publishes began failing, and only while they do.
func TestALeaseWhosePublishesRecoverIsRenewed(t *testing.T) {
	f, sp := spannerFixture(t, 1000, nil)
	ctx := context.Background()
	a := f.admit(t, 100, false)
	f.log.fail("lease-1", 1)
	if _, err := f.lease.Settle(ctx, a, 10, sum("a")); err != nil {
		t.Fatalf("a settle republished after one failure: %v", err)
	}
	f.clock.advance(31 * time.Second)
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if rounds, _, _, _ := sp.state(); len(rounds) != 1 || !slices.Equal(rounds[0], []string{"lease-1"}) {
		t.Fatalf("a lease whose publishes recovered is not renewed: %v", rounds)
	}
}

// TestAShortfallOutlivesTheLease: the shortfall writer retries until its
// write lands, though the lease meanwhile finished, was marked draining and
// let go (§4.2).
func TestAShortfallOutlivesTheLease(t *testing.T) {
	f, sp := spannerFixture(t, 100, nil)
	ctx := context.Background()
	a := f.admit(t, 100, false)
	sp.mu.Lock()
	sp.gate = make(chan struct{})
	sp.mu.Unlock()
	if _, err := f.lease.Settle(ctx, a, 150, sum("a")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the write", func() bool {
		_, writes, _, _ := sp.state()
		return len(writes) == 1
	})
	f.lease.Close()
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the lease let go", func() bool {
		_, held := f.owner.Lease("lease-1")
		return !held
	})
	// The held write fails after the lease is let go; the writer retries it.
	sp.mu.Lock()
	sp.failWrites = 1
	close(sp.gate)
	sp.gate = nil
	sp.mu.Unlock()
	waitFor(t, "the total stored", func() bool {
		_, _, landed, _ := sp.state()
		return slices.Equal(landed, []int64{50})
	})
	if _, writes, _, _ := sp.state(); !slices.Equal(writes, []int64{50, 50}) {
		t.Fatalf("the writes: %v", writes)
	}
}

// TestLettingAnAbandonedLeaseGoIsOneStep: a renewal's answer before the
// check keeps the lease, and one after it changes nothing.
func TestLettingAnAbandonedLeaseGoIsOneStep(t *testing.T) {
	f, _ := spannerFixture(t, 100, nil)
	f.lease.mu.Lock()
	f.lease.failedAt = start // its publishes failing since: past the window a minute on
	f.lease.mu.Unlock()
	f.clock.advance(time.Minute)
	f.lease.Renewed(start.Add(time.Hour))
	if f.lease.letIfAbandoned(f.clock.Now()) {
		t.Fatal("a lease a renewal's answer moved past the cutoff is let go")
	}
	f.clock.advance(time.Hour)
	if !f.lease.letIfAbandoned(f.clock.Now()) {
		t.Fatal("a lease no longer renewed and past its cutoff is kept")
	}
	f.lease.Renewed(start.Add(5 * time.Hour))
	f.lease.mu.Lock()
	expiry := f.lease.expiry
	f.lease.mu.Unlock()
	if !expiry.Equal(start.Add(time.Hour)) {
		t.Fatalf("a renewal's answer after the lease was let go moved its expiry to %v", expiry)
	}
}

// TestLetWaitsForTheFinish: the draining write is one of the lease's
// workers, so Let returns only once it has, though cancelled it takes 150 ms.
func TestLetWaitsForTheFinish(t *testing.T) {
	f, sp := spannerFixture(t, 100, nil)
	sp.mu.Lock()
	sp.drainGate, sp.afterCancel, sp.drainReturned = make(chan struct{}), 150*time.Millisecond, make(chan struct{})
	sp.mu.Unlock()
	f.lease.Close()
	if err := f.owner.Renew(context.Background()); err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond) // the final checkpoint acknowledged, the draining write held
	f.owner.Let("lease-1")
	select {
	case <-sp.drainReturned:
	default:
		t.Fatal("Let returned before the draining write did")
	}
}

// TestTheFinalCheckpointIsTheLast: a renewal while the final checkpoint's
// acknowledgement is held publishes nothing more.
func TestTheFinalCheckpointIsTheLast(t *testing.T) {
	f, _ := spannerFixture(t, 100, nil)
	ctx := context.Background()
	f.lease.Close()
	f.log.hold()
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if recs := f.log.records(t, "lease-1"); len(recs) != 1 || !recs[0].Checkpoint.Final {
		t.Fatalf("the records after the final checkpoint: %+v", recs)
	}
	f.log.letGo()
}

// TestARefusedBatchDropsEveryLeaseAtOnce: a lease whose finish is slow to
// end does not keep another refused in the same batch in use meanwhile.
func TestARefusedBatchDropsEveryLeaseAtOnce(t *testing.T) {
	f, sp := spannerFixture(t, 1000, nil)
	ctx := context.Background()
	other, err := f.owner.Take("lease-2", "ws-1", 1000, start.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	// lease-1 finishes: its draining write is held, and once cancelled is
	// held again until the test lets it return.
	sp.mu.Lock()
	sp.drainGate, sp.drainHold = make(chan struct{}), make(chan struct{})
	sp.mu.Unlock()
	f.lease.Close()
	if err := f.owner.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond) // the final checkpoint acknowledged, the draining write held
	sp.mu.Lock()
	sp.refuse["lease-1"], sp.refuse["lease-2"] = true, true
	sp.mu.Unlock()
	renewed := make(chan error, 1)
	go func() { renewed <- f.owner.Renew(ctx) }()
	waitFor(t, "lease-2 dropped", func() bool {
		_, held := f.owner.Lease("lease-2")
		return !held
	})
	if _, err := other.Admit(Admission{Estimate: 1, Boot: boot}); !errors.Is(err, ErrPastCutoff) {
		t.Fatalf("an admission under a lease dropped beside a slow one: %v", err)
	}
	select {
	case <-renewed:
		t.Fatal("the round returned while lease-1's finish is held")
	default:
	}
	close(sp.drainHold)
	if err := <-renewed; err != nil {
		t.Fatal(err)
	}
}

// TestAbandonmentAndARenewalRace: run together, either the renewal's answer
// keeps the lease, or the lease is let go and the answer changes nothing;
// never both.
func TestAbandonmentAndARenewalRace(t *testing.T) {
	for range 300 {
		f, _ := spannerFixture(t, 100, nil)
		f.lease.mu.Lock()
		f.lease.failedAt = start
		f.lease.mu.Unlock()
		f.clock.advance(time.Minute) // past the window and the cutoff
		now := f.clock.Now()
		later := start.Add(time.Hour)
		var abandoned bool
		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			abandoned = f.lease.letIfAbandoned(now)
		}()
		go func() {
			defer wg.Done()
			f.lease.Renewed(later)
		}()
		wg.Wait()
		f.lease.mu.Lock()
		extended := f.lease.expiry.Equal(later)
		f.lease.mu.Unlock()
		if abandoned == extended {
			t.Fatalf("let go %v, and the renewal's answer taken %v", abandoned, extended)
		}
		f.owner.Stop()
	}
}
