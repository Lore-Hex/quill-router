package ring

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fake is the store's membership as the store's own tests hold it
// (internal/store/membership_test.go): a join takes the address's next
// epoch, a heartbeat lands only with the row's epoch, a leaving row stays
// leaving, and a member is live while its heartbeat is younger than liveFor
// at the read, all on one clock that stands for Spanner's.
type fake struct {
	mu      sync.Mutex
	now     time.Time
	liveFor time.Duration
	rows    map[string]*store.Member
	fail    error
	// beats counts the heartbeats that landed, attempts every one tried.
	beats    map[string]int
	attempts map[string]int
}

func newFake() *fake {
	return &fake{now: time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC), liveFor: 3 * time.Second,
		rows: map[string]*store.Member{}, beats: map[string]int{}, attempts: map[string]int{}}
}

func (f *fake) Join(ctx context.Context, address string, roles []string) (int64, time.Time, error) {
	if err := ctx.Err(); err != nil {
		return 0, time.Time{}, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return 0, time.Time{}, f.fail
	}
	if address == "" || len(roles) == 0 {
		return 0, time.Time{}, errors.New("a member needs an address and roles")
	}
	f.now = f.now.Add(time.Millisecond)
	epoch := int64(1)
	if r, ok := f.rows[address]; ok {
		epoch = r.Epoch + 1
	}
	f.rows[address] = &store.Member{Address: address, Epoch: epoch, Roles: slices.Clone(roles), State: store.Serving,
		StartedAt: f.now, HeartbeatAt: f.now}
	return epoch, f.now, nil
}

func (f *fake) Heartbeat(ctx context.Context, address string, epoch int64, state string) (bool, time.Time, error) {
	if err := ctx.Err(); err != nil {
		return false, time.Time{}, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.attempts[address]++
	if f.fail != nil {
		return false, time.Time{}, f.fail
	}
	if state != store.Serving && state != store.Leaving && state != store.Withdrawn {
		return false, time.Time{}, fmt.Errorf("no member state %q", state)
	}
	f.now = f.now.Add(time.Millisecond)
	r, ok := f.rows[address]
	if !ok || r.Epoch != epoch || (r.State == store.Leaving && state != store.Leaving) {
		return false, f.now, nil
	}
	r.State, r.HeartbeatAt = state, f.now
	f.beats[address]++
	return true, f.now, nil
}

func (f *fake) Members(ctx context.Context) ([]store.Member, time.Time, error) {
	if err := ctx.Err(); err != nil {
		return nil, time.Time{}, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return nil, time.Time{}, f.fail
	}
	var out []store.Member
	for _, r := range f.rows {
		m := *r
		m.Roles = slices.Clone(r.Roles)
		m.Live = f.now.Sub(m.HeartbeatAt) < f.liveFor
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Address < out[j].Address })
	return out, f.now, nil
}

func (f *fake) advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}

func (f *fake) setFail(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fail = err
}

func (f *fake) row(address string) store.Member {
	f.mu.Lock()
	defer f.mu.Unlock()
	return *f.rows[address]
}

func (f *fake) beatCount(address string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.beats[address]
}

func (f *fake) attemptCount(address string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.attempts[address]
}

// tick ticks a node's or a watcher's loop, and returns once the loop took
// it: a loop takes a tick only once its step before has ended.
func tick(t *testing.T, ticks chan<- time.Time) {
	t.Helper()
	select {
	case ticks <- time.Now():
	case <-time.After(time.Second):
		t.Fatal("the loop took no tick")
	}
}

// eventually waits up to a second for cond.
func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("%s did not happen within a second", what)
		}
		time.Sleep(time.Millisecond)
	}
}

func owners(n int) []store.Member {
	var out []store.Member
	for i := range n {
		out = append(out, store.Member{Address: fmt.Sprintf("10.0.0.%d:8443", i+1), Roles: []string{OwnerRole},
			State: store.Serving, Live: true})
	}
	return out
}

func keys(n int) []string {
	var out []string
	for i := range n {
		out = append(out, ShardKey(fmt.Sprintf("ws-%d", i/16), int64(i%16)))
	}
	return out
}

// TestScoreIsFixed: every process scores alike. A change to Score moves
// almost every shard at once, across a fleet running two versions.
func TestScoreIsFixed(t *testing.T) {
	for _, c := range []struct {
		address, key string
		want         uint64
	}{
		{"10.0.0.1:8443", "ws-1/0", 0x9dd26672c688f7cd},
		{"10.0.0.2:8443", "ws-1/0", 0x73ce448d7cb73001},
	} {
		if got := Score(c.address, c.key); got != c.want {
			t.Errorf("Score(%q, %q) = %#x, want %#x", c.address, c.key, got, c.want)
		}
	}
	if ShardKey("ws-1", 0) != "ws-1/0" {
		t.Errorf("ShardKey is %q", ShardKey("ws-1", 0))
	}
}

// TestOwnerIsTheHighestScoreAmongServingOwners: only live, serving members
// with the owner role take leases, and of them the highest score wins.
func TestOwnerIsTheHighestScoreAmongServingOwners(t *testing.T) {
	members := owners(4)
	members[0].Live = false
	members[1].State = store.Leaving
	members[2].Roles = []string{"frontdoor"}
	members = append(members, store.Member{Address: "10.0.0.9:8443", Roles: []string{"frontdoor", OwnerRole},
		State: store.Withdrawn, Live: true})
	v := View{Members: members}
	if got := v.Owners(); len(got) != 1 || got[0].Address != members[3].Address {
		t.Fatalf("the owners are %+v", got)
	}
	for _, k := range keys(50) {
		if m, ok := v.Owner(k); !ok || m.Address != members[3].Address {
			t.Fatalf("%s's owner is %+v %v", k, m, ok)
		}
	}
	all := View{Members: owners(5)}
	for _, k := range keys(200) {
		m, ok := all.Owner(k)
		if !ok {
			t.Fatalf("%s has no owner", k)
		}
		for _, other := range all.Members {
			if s := Score(other.Address, k); s > Score(m.Address, k) {
				t.Fatalf("%s goes to %s, and %s scores higher", k, m.Address, other.Address)
			}
		}
	}
	if _, ok := (View{Members: members[:3]}).Owner("ws/0"); ok {
		t.Fatal("a view with no serving owner names one")
	}
}

// TestAMemberGoingOrComingMovesOnlyItsShards: rendezvous hashing's point.
// When a member goes, only the shards it owned move; when one comes, shards
// move only to it.
func TestAMemberGoingOrComingMovesOnlyItsShards(t *testing.T) {
	ten := View{Members: owners(10)}
	nine := View{Members: slices.Delete(slices.Clone(ten.Members), 4, 5)}
	gone := ten.Members[4].Address
	moved := 0
	for _, k := range keys(10000) {
		before, _ := ten.Owner(k)
		after, _ := nine.Owner(k)
		switch {
		case before.Address == gone:
			moved++
		case before.Address != after.Address:
			t.Fatalf("%s moved from %s to %s, and %s went", k, before.Address, after.Address, gone)
		}
		back, _ := ten.Owner(k)
		if back.Address != before.Address {
			t.Fatalf("%s's owner is not stable", k)
		}
	}
	if moved < 800 || moved > 1200 {
		t.Fatalf("%d of 10,000 shards were the gone member's", moved)
	}
}

// TestShardsSpreadEvenly: each of ten owners gets about a tenth.
func TestShardsSpreadEvenly(t *testing.T) {
	v := View{Members: owners(10)}
	count := map[string]int{}
	for _, k := range keys(10000) {
		m, _ := v.Owner(k)
		count[m.Address]++
	}
	for _, m := range v.Members {
		if n := count[m.Address]; n < 800 || n > 1200 {
			t.Errorf("%s owns %d of 10,000 shards", m.Address, n)
		}
	}
}

func TestANodeJoinsAndHeartbeats(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	n, err := Start(ctx, f, "a:1", []string{OwnerRole}, time.Millisecond)
	if err != nil || n.Epoch() != 1 || n.State() != store.Serving {
		t.Fatalf("the start: %+v %v", n, err)
	}
	eventually(t, "two heartbeats", func() bool { return f.beatCount("a:1") >= 2 })
	n.Stop()
	stopped := f.beatCount("a:1")
	time.Sleep(10 * time.Millisecond)
	if f.beatCount("a:1") != stopped {
		t.Fatal("a stopped node heartbeats")
	}
	if _, err := Start(ctx, f, "a:1", nil, 0); err == nil {
		t.Fatal("a node starts with no heartbeat interval")
	}
}

// TestLeavingIsForGood: a leaving node keeps leaving; withdrawn and serving
// go back and forth.
func TestLeavingIsForGood(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	n, err := Start(ctx, f, "a:1", []string{OwnerRole}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	defer n.Stop()
	for _, state := range []string{store.Withdrawn, store.Serving, store.Leaving} {
		if err := n.SetState(ctx, state); err != nil || n.State() != state || f.row("a:1").State != state {
			t.Fatalf("to %s: node %s, row %s, %v", state, n.State(), f.row("a:1").State, err)
		}
	}
	for _, state := range []string{store.Serving, store.Withdrawn} {
		if err := n.SetState(ctx, state); err == nil || errors.Is(err, ErrLost) || n.State() != store.Leaving ||
			f.row("a:1").State != store.Leaving {
			t.Fatalf("a leaving node becomes %s: %v", state, err)
		}
	}
	// The node refused the change itself, so it is not lost, and leaving
	// still lands.
	select {
	case <-n.Lost():
		t.Fatal("a refused state change lost the node")
	default:
	}
	if err := n.SetState(ctx, store.Leaving); err != nil {
		t.Fatalf("leaving again: %v", err)
	}
}

// TestANodeStartedElsewhereIsLost: once its address joins again, the old
// process's heartbeat is refused, and it stops: its loop ends, and it writes
// no heartbeat after.
func TestANodeStartedElsewhereIsLost(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	ticks := make(chan time.Time)
	old, err := start(ctx, f, "a:1", []string{OwnerRole}, time.Hour, ticks)
	if err != nil {
		t.Fatal(err)
	}
	defer old.Stop()
	if epoch, _, err := f.Join(ctx, "a:1", []string{OwnerRole}); err != nil || epoch != 2 {
		t.Fatalf("the restart: %d %v", epoch, err)
	}
	tick(t, ticks) // a heartbeat, which the row refuses
	select {
	case <-old.Lost():
	case <-time.After(time.Second):
		t.Fatal("the old process is not lost")
	}
	select {
	case <-old.done:
	case <-time.After(time.Second):
		t.Fatal("a lost node's loop goes on")
	}
	attempts := f.attemptCount("a:1")
	select {
	case ticks <- time.Now():
		t.Fatal("a lost node's loop took a tick")
	case <-time.After(50 * time.Millisecond):
	}
	if err := old.SetState(ctx, store.Withdrawn); !errors.Is(err, ErrLost) {
		t.Fatalf("a lost node's state change: %v", err)
	}
	if got := f.attemptCount("a:1"); got != attempts {
		t.Fatalf("a lost node tried %d heartbeats more", got-attempts)
	}
	if f.row("a:1").Epoch != 2 || f.row("a:1").State != store.Serving {
		t.Fatalf("the old process wrote the new one's row: %+v", f.row("a:1"))
	}
}

// TestAFailedHeartbeatIsTriedAgain: a heartbeat that fails is not a loss;
// the next tick tries again, and lands once the store is back.
func TestAFailedHeartbeatIsTriedAgain(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	ticks := make(chan time.Time)
	n, err := start(ctx, f, "a:1", []string{OwnerRole}, time.Hour, ticks)
	if err != nil {
		t.Fatal(err)
	}
	defer n.Stop()
	f.setFail(errors.New("unavailable"))
	tick(t, ticks)
	tick(t, ticks) // taken once the first, failed heartbeat ended
	if f.attemptCount("a:1") < 1 || f.beatCount("a:1") != 0 {
		t.Fatalf("%d heartbeats tried and %d landed while they fail", f.attemptCount("a:1"), f.beatCount("a:1"))
	}
	f.setFail(nil)
	tick(t, ticks) // taken once the second, failed heartbeat ended
	tick(t, ticks) // taken once the third ended, which lands
	if f.beatCount("a:1") < 1 {
		t.Fatal("no heartbeat landed after the failures")
	}
	select {
	case <-n.Lost():
		t.Fatal("failed heartbeats lost the node")
	default:
	}
}

// TestAWatcherSeesAMemberGo: once a member's heartbeat is older than the
// liveness window at a read, its shards go to the others.
func TestAWatcherSeesAMemberGo(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	a, err := Start(ctx, f, "a:1", []string{OwnerRole}, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer a.Stop()
	b, err := Start(ctx, f, "b:1", []string{OwnerRole}, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	w, err := Watch(ctx, f, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Stop()
	var bKey string
	eventually(t, "both owners in view", func() bool {
		v, _ := w.View()
		return len(v.Owners()) == 2
	})
	v, _ := w.View()
	for _, k := range keys(100) {
		if m, _ := v.Owner(k); m.Address == "b:1" {
			bKey = k
			break
		}
	}
	if bKey == "" {
		t.Fatal("b owns none of 100 shards")
	}
	b.Stop()
	f.advance(4 * time.Second)
	eventually(t, "b's shard moving to a", func() bool {
		v, _ := w.View()
		m, ok := v.Owner(bKey)
		return ok && m.Address == "a:1"
	})
}

// gated is the fake whose reads each wait for the test to let them go, and
// report when they begin and end, so a test knows exactly which read made a
// view.
type gated struct {
	*fake
	next     chan struct{}
	began    chan struct{}
	finished chan error
	waiting  bool // a read has begun that the test has not let go
	// afterCancel is how long a cancelled read takes to return, and
	// returned is closed once one has.
	afterCancel time.Duration
	returned    chan struct{}
}

func newGated(f *fake) *gated {
	return &gated{fake: f, next: make(chan struct{}), began: make(chan struct{}, 100), finished: make(chan error, 100)}
}

func (g *gated) Members(ctx context.Context) ([]store.Member, time.Time, error) {
	select {
	case g.began <- struct{}{}:
	case <-ctx.Done():
		return nil, time.Time{}, ctx.Err()
	}
	select {
	case <-g.next:
	case <-ctx.Done():
		time.Sleep(g.afterCancel)
		select {
		case g.finished <- ctx.Err():
		default:
		}
		if g.returned != nil {
			close(g.returned)
		}
		return nil, time.Time{}, ctx.Err()
	}
	members, at, err := g.fake.Members(ctx)
	g.finished <- err
	return members, at, err
}

// begun waits up to within for a read to begin, unless one has.
func (g *gated) begun(t *testing.T, within time.Duration) {
	t.Helper()
	if g.waiting {
		return
	}
	select {
	case <-g.began:
		g.waiting = true
	case <-time.After(within):
		t.Fatal("no read began")
	}
}

// read lets the read under way go and waits for it to end, then ticks the
// watcher, which takes the tick only once it has applied that read's result,
// and begins its next read. So when read returns, the view is the one the
// read made, or kept, and the next read is under way, held.
func (g *gated) read(t *testing.T, ticks chan<- time.Time) error {
	t.Helper()
	g.begun(t, time.Second)
	g.waiting = false
	select {
	case g.next <- struct{}{}:
	case <-time.After(time.Second):
		t.Fatal("the read under way ended unanswered")
	}
	var err error
	select {
	case err = <-g.finished:
	case <-time.After(time.Second):
		t.Fatal("the read did not end")
	}
	tick(t, ticks)
	g.begun(t, time.Second)
	return err
}

// TestAWatcherKeepsItsLastView: a read that fails leaves the view, and its
// time, exactly as the last successful read left them; and the view a
// caller gets is its own copy.
func TestAWatcherKeepsItsLastView(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	if _, _, err := f.Join(ctx, "a:1", []string{OwnerRole}); err != nil {
		t.Fatal(err)
	}
	g := newGated(f)
	ticks := make(chan time.Time)
	watched := make(chan *Watcher, 1)
	go func() {
		w, err := watch(ctx, g, time.Hour, ticks)
		if err != nil {
			t.Error(err)
		}
		watched <- w
	}()
	if err := g.read(t, ticks); err != nil {
		t.Fatal(err)
	}
	w := <-watched
	defer w.Stop() // which ends the read under way
	// The baseline: one more successful read, built apart from anything the
	// watcher returned.
	if err := g.read(t, ticks); err != nil {
		t.Fatal(err)
	}
	members, readAt, err := f.Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	want := View{Members: members, ReadAt: readAt}
	v, at := w.View()
	if !reflect.DeepEqual(v, want) {
		t.Fatalf("the view after a read: %+v, want %+v", v, want)
	}
	f.setFail(errors.New("unavailable"))
	for range 3 {
		if err := g.read(t, ticks); err == nil {
			t.Fatal("a read succeeded while reads fail")
		}
		got, gotAt := w.View()
		if !reflect.DeepEqual(got, want) || !gotAt.Equal(at) {
			t.Fatalf("a failed read changed the view: %+v at %v, then %+v at %v", want, at, got, gotAt)
		}
	}
	v.Members[0].Roles[0], v.Members[0].Live = "frontdoor", false
	if again, _ := w.View(); !reflect.DeepEqual(again, want) {
		t.Fatalf("a caller's change reached the watcher's view: %+v", again)
	}
	// Reads still fail, so a new watch has no first read.
	if _, err := Watch(ctx, f, time.Millisecond); err == nil {
		t.Fatal("a watch starts with no first read")
	}
}

// TestStopCancelsAReadUnderWay: Stop ends a read the store has not answered,
// well before the read's own deadline, an interval.
func TestStopCancelsAReadUnderWay(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	g := newGated(f)
	ticks := make(chan time.Time)
	watched := make(chan *Watcher, 1)
	go func() {
		w, err := watch(ctx, g, time.Hour, ticks)
		if err != nil {
			t.Error(err)
		}
		watched <- w
	}()
	// The first read, and then the next, under way and held; cancelled, it
	// takes 150 ms more to return.
	if err := g.read(t, ticks); err != nil {
		t.Fatal(err)
	}
	g.afterCancel, g.returned = 150*time.Millisecond, make(chan struct{})
	w := <-watched
	stopped := make(chan struct{})
	go func() {
		w.Stop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("Stop waits on the read under way")
	}
	select {
	case <-g.returned:
	default:
		t.Fatal("Stop returned before the read under way did")
	}
	select {
	case err := <-g.finished:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("the read under way ended with %v", err)
		}
	default:
		t.Fatal("the read under way did not end")
	}
}

// slowMembers is the fake whose reads take a while, counted.
type slowMembers struct {
	*fake
	mu      sync.Mutex
	reads   int
	took    time.Duration
	entered time.Time // when the first read reached the store
}

func (s *slowMembers) Members(ctx context.Context) ([]store.Member, time.Time, error) {
	s.mu.Lock()
	s.reads++
	if s.entered.IsZero() {
		s.entered = time.Now()
	}
	took := s.took
	s.mu.Unlock()
	select {
	case <-time.After(took):
	case <-ctx.Done():
		return nil, time.Time{}, ctx.Err()
	}
	return s.fake.Members(ctx)
}

func (s *slowMembers) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.reads
}

// TestAViewsTimeCountsItsRead and the watcher reads nothing after Stop.
func TestAViewsTimeCountsItsReadAndNoReadFollowsStop(t *testing.T) {
	sm := &slowMembers{fake: newFake(), took: 30 * time.Millisecond}
	ctx := context.Background()
	began := time.Now()
	w, err := Watch(ctx, sm, 10*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	sm.mu.Lock()
	entered := sm.entered
	sm.mu.Unlock()
	if _, at := w.View(); at.Before(began) || at.After(entered) {
		t.Fatalf("a read that reached the store %v after Watch began, and took 30 ms, is dated %v after",
			entered.Sub(began), at.Sub(began))
	}
	time.Sleep(50 * time.Millisecond)
	w.Stop()
	stopped := sm.count()
	time.Sleep(60 * time.Millisecond)
	if sm.count() != stopped {
		t.Fatalf("%d reads after Stop", sm.count()-stopped)
	}
}

// TestNoStateIsMeantThatIsNone: a state the store does not know is refused
// before the node means it, so its heartbeats go on.
func TestNoStateIsMeantThatIsNone(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	n, err := Start(ctx, f, "a:1", []string{OwnerRole}, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer n.Stop()
	if err := n.SetState(ctx, "gone"); err == nil {
		t.Fatal("a node means a state there is not")
	}
	before := f.beatCount("a:1")
	eventually(t, "heartbeats after the refusal", func() bool { return f.beatCount("a:1") > before+2 })
	if f.row("a:1").State != store.Serving {
		t.Fatalf("the row says %s", f.row("a:1").State)
	}
}

// blocking is the fake whose heartbeats block until released, or fail
// after landing, as a write that timed out may have.
type blocking struct {
	*fake
	mu      sync.Mutex
	block   chan struct{}
	entered chan struct{}
	landErr error
	// afterCancel is how long a cancelled write takes to return, and
	// returned is closed once one has.
	afterCancel time.Duration
	returned    chan struct{}
}

func (b *blocking) Heartbeat(ctx context.Context, address string, epoch int64, state string) (bool, time.Time, error) {
	b.mu.Lock()
	block, entered, landErr, afterCancel, returned := b.block, b.entered, b.landErr, b.afterCancel, b.returned
	b.mu.Unlock()
	if block != nil {
		if entered != nil {
			select {
			case entered <- struct{}{}:
			default:
			}
		}
		select {
		case <-block:
		case <-ctx.Done():
			time.Sleep(afterCancel)
			if returned != nil {
				close(returned)
			}
			return false, time.Time{}, ctx.Err()
		}
	}
	written, at, err := b.fake.Heartbeat(ctx, address, epoch, state)
	if err == nil && landErr != nil {
		return false, time.Time{}, landErr
	}
	return written, at, err
}

// TestALeaveThatTimedOutStaysMeant: a leaving heartbeat that landed but
// answered with an error is not followed by a serving one, which the row
// would refuse, losing the node; the node keeps saying it is leaving.
func TestALeaveThatTimedOutStaysMeant(t *testing.T) {
	b := &blocking{fake: newFake()}
	ctx := context.Background()
	slow, err := Start(ctx, b, "b:1", []string{OwnerRole}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	defer slow.Stop()
	b.mu.Lock()
	b.landErr = context.DeadlineExceeded
	b.mu.Unlock()
	if err := slow.SetState(ctx, store.Leaving); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("the leave: %v", err)
	}
	b.mu.Lock()
	b.landErr = nil
	b.mu.Unlock()
	// Before any heartbeat confirms the leave, serving or withdrawing again
	// is refused by the node itself, and loses nothing.
	for _, state := range []string{store.Serving, store.Withdrawn} {
		if err := slow.SetState(ctx, state); err == nil || errors.Is(err, ErrLost) {
			t.Fatalf("%s right after a leave that timed out: %v", state, err)
		}
	}
	select {
	case <-slow.Lost():
		t.Fatal("the unconfirmed leave lost the node")
	default:
	}
	n, err := Start(ctx, b, "a:1", []string{OwnerRole}, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer n.Stop()
	b.mu.Lock()
	b.landErr = context.DeadlineExceeded
	b.mu.Unlock()
	if err := n.SetState(ctx, store.Leaving); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("the leave: %v", err)
	}
	b.mu.Lock()
	b.landErr = nil
	b.mu.Unlock()
	before := b.beatCount("a:1")
	eventually(t, "heartbeats after the leave", func() bool { return b.beatCount("a:1") > before+2 })
	select {
	case <-n.Lost():
		t.Fatal("a leave that timed out lost the node")
	default:
	}
	if b.row("a:1").State != store.Leaving || n.State() != store.Leaving {
		t.Fatalf("the row says %s and the node %s", b.row("a:1").State, n.State())
	}
	if err := n.SetState(ctx, store.Serving); err == nil {
		t.Fatal("a node that meant to leave serves again")
	}
}

// TestStopEndsAWriteUnderWay: a state change stuck in its write does not
// hold Stop, and once Stop has begun the node writes nothing more.
func TestStopEndsAWriteUnderWay(t *testing.T) {
	b := &blocking{fake: newFake()}
	ctx := context.Background()
	n, err := Start(ctx, b, "a:1", []string{OwnerRole}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	b.mu.Lock()
	b.block, b.entered = make(chan struct{}), make(chan struct{}, 1)
	b.afterCancel, b.returned = 150*time.Millisecond, make(chan struct{}) // cancelled, it takes 150 ms to return
	b.mu.Unlock()
	result := make(chan error, 1)
	go func() { result <- n.SetState(context.Background(), store.Withdrawn) }()
	select {
	case <-b.entered:
	case <-time.After(time.Second):
		t.Fatal("the write did not begin")
	}
	stopped := make(chan struct{})
	go func() {
		n.Stop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("Stop waits on a write stuck under way")
	}
	select {
	case <-b.returned:
	default:
		t.Fatal("Stop returned before the write under way did")
	}
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatalf("the stuck write ended with %v", err)
	}
	b.mu.Lock()
	b.block, b.returned = nil, nil
	b.mu.Unlock()
	beats := b.beatCount("a:1")
	if err := n.SetState(ctx, store.Serving); !errors.Is(err, ErrStopped) {
		t.Fatalf("a stopped node's state change: %v", err)
	}
	time.Sleep(5 * time.Millisecond)
	if b.beatCount("a:1") != beats {
		t.Fatal("a stopped node wrote a heartbeat")
	}
	if _, err := Start(ctx, b, "", []string{OwnerRole}, time.Second); err == nil {
		t.Fatal("a node starts with no address")
	}
}

// TestTheFakeRefusesCancelledCalls: as the store's calls end with their
// context, the fake's do, and change nothing.
func TestTheFakeRefusesCancelledCalls(t *testing.T) {
	f := newFake()
	ctx, cancel := context.WithCancel(context.Background())
	if _, _, err := f.Join(context.Background(), "a:1", []string{OwnerRole}); err != nil {
		t.Fatal(err)
	}
	cancel()
	if _, _, err := f.Join(ctx, "b:1", []string{OwnerRole}); err == nil {
		t.Error("a cancelled join")
	}
	if _, _, err := f.Heartbeat(ctx, "a:1", 1, store.Leaving); err == nil || f.row("a:1").State != store.Serving {
		t.Error("a cancelled heartbeat")
	}
	if _, _, err := f.Members(ctx); err == nil {
		t.Error("a cancelled read")
	}
}
