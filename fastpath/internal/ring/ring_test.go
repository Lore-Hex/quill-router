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
	beats   map[string]int
}

func newFake() *fake {
	return &fake{now: time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC), liveFor: 3 * time.Second,
		rows: map[string]*store.Member{}, beats: map[string]int{}}
}

func (f *fake) Join(_ context.Context, address string, roles []string) (int64, time.Time, error) {
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

func (f *fake) Heartbeat(_ context.Context, address string, epoch int64, state string) (bool, time.Time, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
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

func (f *fake) Members(context.Context) ([]store.Member, time.Time, error) {
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
// process's heartbeat is refused, and it stops.
func TestANodeStartedElsewhereIsLost(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	old, err := Start(ctx, f, "a:1", []string{OwnerRole}, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer old.Stop()
	if epoch, _, err := f.Join(ctx, "a:1", []string{OwnerRole}); err != nil || epoch != 2 {
		t.Fatalf("the restart: %d %v", epoch, err)
	}
	select {
	case <-old.Lost():
	case <-time.After(time.Second):
		t.Fatal("the old process is not lost")
	}
	if err := old.SetState(ctx, store.Withdrawn); !errors.Is(err, ErrLost) {
		t.Fatalf("a lost node's state change: %v", err)
	}
	if f.row("a:1").Epoch != 2 || f.row("a:1").State != store.Serving {
		t.Fatalf("the old process wrote the new one's row: %+v", f.row("a:1"))
	}
}

// TestAFailedHeartbeatIsTriedAgain: a heartbeat that fails is not a loss.
func TestAFailedHeartbeatIsTriedAgain(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	n, err := Start(ctx, f, "a:1", []string{OwnerRole}, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer n.Stop()
	f.setFail(errors.New("unavailable"))
	time.Sleep(10 * time.Millisecond)
	before := f.beatCount("a:1")
	f.setFail(nil)
	eventually(t, "heartbeats after the failures", func() bool { return f.beatCount("a:1") > before })
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

// TestAWatcherKeepsItsLastView: a read that fails leaves the view, and its
// time, as they were; and the view a caller gets is its own copy.
func TestAWatcherKeepsItsLastView(t *testing.T) {
	f := newFake()
	ctx := context.Background()
	if _, _, err := f.Join(ctx, "a:1", []string{OwnerRole}); err != nil {
		t.Fatal(err)
	}
	w, err := Watch(ctx, f, time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Stop()
	// The baseline, taken before reads begin to fail, built apart from
	// anything the watcher returned.
	members, readAt, err := f.Members(ctx)
	if err != nil {
		t.Fatal(err)
	}
	want := View{Members: members, ReadAt: readAt}
	eventually(t, "a read of the baseline", func() bool {
		v, _ := w.View()
		return reflect.DeepEqual(v, want)
	})
	_, before := w.View()
	f.setFail(errors.New("unavailable"))
	time.Sleep(20 * time.Millisecond)
	v, after := w.View()
	if !reflect.DeepEqual(v, want) || after.Before(before) {
		t.Fatalf("a failed read changed the view: %+v at %v, then %+v at %v", want, before, v, after)
	}
	// Reads go on failing, so the time stays the last success's.
	time.Sleep(5 * time.Millisecond)
	if _, again := w.View(); !again.Equal(after) {
		t.Fatalf("the view's time moved with no successful read: %v, then %v", after, again)
	}
	v.Members[0].Roles[0], v.Members[0].Live = "frontdoor", false
	if again, _ := w.View(); !reflect.DeepEqual(again, want) {
		t.Fatalf("a caller's change reached the watcher's view: %+v", again)
	}
	if _, err := Watch(ctx, f, time.Millisecond); err == nil {
		t.Fatal("a watch starts with no first read")
	}
}

// slowMembers is the fake whose reads take a while, counted.
type slowMembers struct {
	*fake
	mu    sync.Mutex
	reads int
	took  time.Duration
}

func (s *slowMembers) Members(ctx context.Context) ([]store.Member, time.Time, error) {
	s.mu.Lock()
	s.reads++
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
	if _, at := w.View(); at.After(began.Add(5 * time.Millisecond)) {
		t.Fatalf("a read that took 30 ms is dated %v after it began", at.Sub(began))
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
	landErr error
}

func (b *blocking) Heartbeat(ctx context.Context, address string, epoch int64, state string) (bool, time.Time, error) {
	b.mu.Lock()
	block, landErr := b.block, b.landErr
	b.mu.Unlock()
	if block != nil {
		select {
		case <-block:
		case <-ctx.Done():
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
	b.block = make(chan struct{})
	b.mu.Unlock()
	result := make(chan error, 1)
	go func() { result <- n.SetState(context.Background(), store.Withdrawn) }()
	time.Sleep(5 * time.Millisecond)
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
	if err := <-result; err == nil {
		t.Fatal("the stuck write succeeded")
	}
	b.mu.Lock()
	b.block = nil
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
