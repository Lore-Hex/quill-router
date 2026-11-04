package auditor

import (
	"context"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// gatedStaging is a StagingStore whose StageRecords, while gate is set,
// tells entered and waits for gate to close: the records that come
// meanwhile wait for the next transaction. It notes each transaction's
// authorizations, fails the next one when fail is set, and gives each
// record in answers its answer there.
type gatedStaging struct {
	mu      sync.Mutex
	gate    chan struct{}
	entered chan struct{}
	fail    bool
	answers map[string]error
	batches [][]string
}

func (g *gatedStaging) FindLease(_ context.Context, leaseID string) (store.LeaseRef, error) {
	return store.LeaseRef{Workspace: "ws", LeaseID: leaseID}, nil
}

func (g *gatedStaging) StageRecords(_ context.Context, rs []store.StagedRecord) ([]error, error) {
	g.mu.Lock()
	gate, entered := g.gate, g.entered
	g.gate = nil
	g.mu.Unlock()
	if gate != nil {
		entered <- struct{}{}
		<-gate
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	var auths []string
	errs := make([]error, len(rs))
	for i, r := range rs {
		auths = append(auths, r.AuthorizationID)
		errs[i] = g.answers[r.AuthorizationID]
	}
	g.batches = append(g.batches, auths)
	if g.fail {
		g.fail = false
		return nil, errInjected
	}
	return errs, nil
}

// hold holds the next transaction until the gate it returns closes; entered
// tells when it has begun.
func (g *gatedStaging) hold() (gate, entered chan struct{}) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.gate, g.entered = make(chan struct{}), make(chan struct{}, 1)
	return g.gate, g.entered
}

func fullRecord(t *testing.T) *fakeStaged {
	t.Helper()
	a, err := store.NewAuthorizationID(store.NewLeaseID())
	if err != nil {
		t.Fatal(err)
	}
	return &fakeStaged{auth: a, kind: settlelog.FullRecord, data: []byte("record " + a)}
}

// waitFor waits until n records wait for the next transaction.
func waitFor(t *testing.T, s *Stager, n int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		s.mu.Lock()
		got := len(s.waiting)
		s.mu.Unlock()
		if got == n {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d records wait, want %d", got, n)
		}
		time.Sleep(time.Millisecond)
	}
}

// TestTheStagerStagesWhatComesMeanwhileTogether: the records that come
// while a transaction stages another go in one transaction after it, each
// answered as it would be alone: one its lease's retirement refuses is
// acknowledged, one with no lease is told and acknowledged, one whose write
// failed is asked for again, and the rest are acknowledged once staged. A
// transaction that fails asks for each of its records again. One whose
// caller stops waiting is asked for again and no later transaction takes
// it, and Wait returns once none runs.
func TestTheStagerStagesWhatComesMeanwhileTogether(t *testing.T) {
	g := &gatedStaging{answers: map[string]error{}}
	var mu sync.Mutex
	var alerts []string
	s, err := NewStager(g, func(auth, what string) {
		mu.Lock()
		defer mu.Unlock()
		alerts = append(alerts, auth+" "+what)
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	run := func(ctx context.Context, ms ...*fakeStaged) *sync.WaitGroup {
		var wg sync.WaitGroup
		for _, m := range ms {
			wg.Go(func() { s.Handle(ctx, m) })
		}
		return &wg
	}
	auths := func(ms ...*fakeStaged) []string {
		var out []string
		for _, m := range ms {
			out = append(out, m.auth)
		}
		slices.Sort(out)
		return out
	}
	settled := func(name string, acked, nacked int, ms ...*fakeStaged) {
		t.Helper()
		for _, m := range ms {
			if m.acked != acked || m.nacked != nacked {
				t.Fatalf("%s: acknowledged %d, again %d", name, m.acked, m.nacked)
			}
		}
	}

	gate, entered := g.hold()
	first := fullRecord(t)
	wgFirst := run(ctx, first)
	<-entered
	rest := []*fakeStaged{fullRecord(t), fullRecord(t), fullRecord(t), fullRecord(t), fullRecord(t)}
	g.mu.Lock()
	g.answers[rest[1].auth] = errInjected
	g.answers[rest[2].auth] = store.ErrRetired
	g.answers[rest[3].auth] = store.ErrNoLease
	g.mu.Unlock()
	wgRest := run(ctx, rest...)
	waitFor(t, s, len(rest))
	close(gate)
	wgFirst.Wait()
	wgRest.Wait()
	if len(g.batches) != 2 || !slices.Equal(g.batches[0], []string{first.auth}) ||
		!slices.Equal(slices.Sorted(slices.Values(g.batches[1])), auths(rest...)) {
		t.Fatalf("the transactions: %v", g.batches)
	}
	settled("staged together", 1, 0, first, rest[0], rest[2], rest[3], rest[4])
	settled("its write failed", 0, 1, rest[1])
	if want := []string{rest[3].auth + " " + AlertFullRecordNoLease}; !slices.Equal(alerts, want) {
		t.Fatalf("told %v, want %v", alerts, want)
	}

	g.mu.Lock()
	g.batches = nil
	g.mu.Unlock()
	gate, entered = g.hold()
	lead := fullRecord(t)
	wgLead := run(ctx, lead)
	<-entered
	failed := []*fakeStaged{fullRecord(t), fullRecord(t)}
	g.fail = false
	wgFailed := run(ctx, failed...)
	waitFor(t, s, len(failed))
	g.mu.Lock()
	g.fail = true
	g.mu.Unlock()
	// The held transaction is the one that fails, so the lead is asked for
	// again and the two after it are staged.
	close(gate)
	wgLead.Wait()
	wgFailed.Wait()
	settled("a failed transaction", 0, 1, lead)
	settled("the next transaction", 1, 0, failed...)

	g.mu.Lock()
	g.batches = nil
	g.mu.Unlock()
	gate, entered = g.hold()
	holder := fullRecord(t)
	wgHolder := run(ctx, holder)
	<-entered
	gone, cancel := context.WithCancel(ctx)
	left := fullRecord(t)
	wgLeft := run(gone, left)
	waitFor(t, s, 1)
	cancel()
	wgLeft.Wait()
	settled("a caller that stopped waiting", 0, 1, left)
	close(gate)
	wgHolder.Wait()
	settled("the holder", 1, 0, holder)
	s.Wait()
	g.mu.Lock()
	defer g.mu.Unlock()
	if len(g.batches) != 1 || !slices.Equal(g.batches[0], []string{holder.auth}) {
		t.Fatalf("the transactions after a caller left: %v", g.batches)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running || len(s.waiting) != 0 {
		t.Fatalf("after Wait: running %v, %d waiting", s.running, len(s.waiting))
	}
}

// TestTheStagerTakesAtMostABatch: a transaction takes at most
// store.MaxStageRecords records, and at most store.MaxStageBytes of bodies
// unless one record alone is larger.
func TestTheStagerTakesAtMostABatch(t *testing.T) {
	s := &Stager{}
	for range store.MaxStageRecords + 3 {
		s.waiting = append(s.waiting, &staging{r: store.StagedRecord{Body: []byte("x")}})
	}
	if got := len(s.take()); got != store.MaxStageRecords {
		t.Fatalf("took %d", got)
	}
	if got := len(s.take()); got != 3 {
		t.Fatalf("then took %d", got)
	}
	half := make([]byte, store.MaxStageBytes/2+1)
	huge := make([]byte, store.MaxStageBytes+1)
	s.waiting = []*staging{{r: store.StagedRecord{Body: half}}, {r: store.StagedRecord{Body: half}},
		{r: store.StagedRecord{Body: huge}}}
	for i, want := range []int{1, 1, 1, 0} {
		if got := len(s.take()); got != want {
			t.Fatalf("take %d: %d, want %d", i, got, want)
		}
	}
}

// TestTheStagerWaitsForItsTransaction: Wait does not return while a
// transaction runs, and does once it ends.
func TestTheStagerWaitsForItsTransaction(t *testing.T) {
	g := &gatedStaging{answers: map[string]error{}}
	s, err := NewStager(g, func(string, string) {})
	if err != nil {
		t.Fatal(err)
	}
	gate, entered := g.hold()
	m := fullRecord(t)
	go s.Handle(context.Background(), m)
	<-entered
	waited := make(chan struct{})
	go func() {
		s.Wait()
		close(waited)
	}()
	select {
	case <-waited:
		t.Fatal("Wait returned while a transaction ran")
	case <-time.After(50 * time.Millisecond):
	}
	close(gate)
	select {
	case <-waited:
	case <-time.After(5 * time.Second):
		t.Fatal("Wait did not return once the transaction ended")
	}
}

// TestTheStagerTakesOnlyThoseStillWaiting: take drops the records whose
// callers have stopped waiting, without counting them against the batch's
// bounds, and a byte bound reached after one leaves the rest waiting.
func TestTheStagerTakesOnlyThoseStillWaiting(t *testing.T) {
	gone, cancel := context.WithCancel(context.Background())
	cancel()
	live := context.Background()
	at := func(ctx context.Context, body []byte) *staging {
		return &staging{ctx: ctx, r: store.StagedRecord{Body: body}}
	}
	s := &Stager{}
	for range 10 {
		s.waiting = append(s.waiting, at(gone, []byte("x")))
	}
	for range store.MaxStageRecords + 2 {
		s.waiting = append(s.waiting, at(live, []byte("x")))
	}
	if got := len(s.take()); got != store.MaxStageRecords || len(s.waiting) != 2 {
		t.Fatalf("took %d, left %d", got, len(s.waiting))
	}
	half := make([]byte, store.MaxStageBytes/2+1)
	s.waiting = []*staging{at(live, half), at(gone, half), at(live, half), at(live, []byte("x"))}
	if got := len(s.take()); got != 1 || len(s.waiting) != 2 {
		t.Fatalf("took %d, left %d", got, len(s.waiting))
	}
	if got := len(s.take()); got != 2 || s.waiting != nil {
		t.Fatalf("then took %d, left %v", got, s.waiting)
	}
	s.waiting = []*staging{at(gone, half), at(gone, half)}
	if got := len(s.take()); got != 0 || s.waiting != nil {
		t.Fatalf("of those gone took %d, left %v", got, s.waiting)
	}
}
