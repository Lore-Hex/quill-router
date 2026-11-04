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
// authorizations, fails the next one when fail is set, and answers
// ErrRetired for those in retired.
type gatedStaging struct {
	mu      sync.Mutex
	gate    chan struct{}
	entered chan struct{}
	fail    bool
	retired map[string]bool
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
		if g.retired[r.AuthorizationID] {
			errs[i] = store.ErrRetired
		}
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
// answered as it would be alone: a retired lease's acknowledged, the rest
// acknowledged once staged. A transaction that fails asks for each of its
// records again, and one whose caller stops waiting is asked for again
// though it may be staged.
func TestTheStagerStagesWhatComesMeanwhileTogether(t *testing.T) {
	g := &gatedStaging{retired: map[string]bool{}}
	s, err := NewStager(g, func(string, string) {})
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
	g.retired[rest[2].auth] = true
	wgRest := run(ctx, rest...)
	waitFor(t, s, len(rest))
	close(gate)
	wgFirst.Wait()
	wgRest.Wait()
	if len(g.batches) != 2 || !slices.Equal(g.batches[0], []string{first.auth}) ||
		!slices.Equal(slices.Sorted(slices.Values(g.batches[1])), auths(rest...)) {
		t.Fatalf("the transactions: %v", g.batches)
	}
	settled("staged together", 1, 0, append([]*fakeStaged{first}, rest...)...)

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
	deadline := time.Now().Add(5 * time.Second)
	for {
		g.mu.Lock()
		n := len(g.batches)
		g.mu.Unlock()
		if n == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the record whose caller left was not staged: %v", g.batches)
		}
		time.Sleep(time.Millisecond)
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
