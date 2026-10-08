package frontdoor

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fakePeers are the other front doors: each reaches the owners not in
// unreachable, answering as set, or, holding, waits for its context to
// end.
type fakePeers struct {
	ev          *events
	unreachable map[string]bool
	holding     bool
	heartbeat   HeartbeatAnswer
	terminal    OwnerTerminalAnswer
	// onCall, when set, runs at each call, as the time it takes.
	onCall func()
}

func (f *fakePeers) Heartbeat(ctx context.Context, peer, owner string, _ OwnerHeartbeat) (HeartbeatAnswer, error) {
	f.ev.add("peer %s heartbeat to %s", peer, owner)
	if f.onCall != nil {
		f.onCall()
	}
	if f.holding {
		<-ctx.Done()
		return HeartbeatAnswer{}, ctx.Err()
	}
	if f.unreachable[owner] {
		return HeartbeatAnswer{}, ErrUnreachable
	}
	return f.heartbeat, nil
}

func (f *fakePeers) Terminal(_ context.Context, peer, owner string, _ OwnerTerminal) (OwnerTerminalAnswer, error) {
	f.ev.add("peer %s %s", peer, owner)
	if f.onCall != nil {
		f.onCall()
	}
	if f.unreachable[owner] {
		return OwnerTerminalAnswer{}, ErrUnreachable
	}
	return f.terminal, nil
}

// fakeNode is this node's row: the states written to it, in order. With a
// gate, a write tells began and waits for the gate or its context to end,
// a write its context ended taking a while more, and then tells ended.
type fakeNode struct {
	mu           sync.Mutex
	states       []string
	gate         chan struct{}
	began, ended chan string
}

func (f *fakeNode) SetState(ctx context.Context, state string) error {
	f.mu.Lock()
	gate := f.gate
	f.mu.Unlock()
	if gate != nil {
		f.began <- state
		select {
		case <-gate:
		case <-ctx.Done():
			time.Sleep(100 * time.Millisecond)
		}
		defer func() { f.ended <- state }()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.states = append(f.states, state)
	return ctx.Err()
}

// gated makes the node's writes wait for the gate.
func (f *fakeNode) gated() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.gate, f.began, f.ended = make(chan struct{}), make(chan string, 8), make(chan string, 8)
}

func (f *fakeNode) written() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.states)
}

type peersFixture struct {
	*doorFixture
	peers *fakePeers
	node  *fakeNode
	now   time.Time
	mu    sync.Mutex
	// slowAt, once set, is the read of the clock, counted from then, that
	// takes the time it reads and then waits for slow, telling slowed.
	slowAt, reads int
	slow, slowed  chan struct{}
}

func (f *peersFixture) clock() time.Time {
	f.mu.Lock()
	now := f.now
	if f.slowAt > 0 {
		if f.reads++; f.reads == f.slowAt {
			f.mu.Unlock()
			close(f.slowed)
			<-f.slow
			return now
		}
	}
	f.mu.Unlock()
	return now
}

func (f *peersFixture) advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}

// newPeers is a front door at node-a among node-b to node-e, with peers, a
// row in the ring, a withdrawal window of five seconds, and revocations
// after ten seconds, one a minute.
func newPeers(t *testing.T) *peersFixture {
	t.Helper()
	f := &peersFixture{doorFixture: newDoor(t, 1), now: start, node: &fakeNode{}}
	f.peers = &fakePeers{ev: f.ev, unreachable: map[string]bool{}}
	f.view = owners("node-a", "node-b", "node-c", "node-d", "node-e")
	door, err := New(Config{Owners: f.owners, Store: f.store, Records: f.records, Members: fakeMembers{f.view}, Key: key,
		Shards: func(string) int64 { return 1 }, OwnerWait: time.Second, PublishWait: time.Second,
		Self: "node-a", Peers: f.peers, PeerWait: 50 * time.Millisecond, Node: f.node, WithdrawWithin: 5 * time.Second,
		ProbeEvery: time.Hour, RevokeAfter: 10 * time.Second, RevokeEvery: time.Minute, HoldLife: 3 * time.Hour,
		Clock: f.clock})
	if err != nil {
		t.Fatal(err)
	}
	f.door = door
	return f
}

// sealedAt is a sealed envelope of a hold under lease at owner, whose life
// ends an hour after the test's start.
func sealedAt(t *testing.T, owner, lease, auth string) string {
	t.Helper()
	return sealedUntil(t, owner, lease, auth, start.Add(time.Hour))
}

// sealedUntil is sealedAt with the hold's end of life.
func sealedUntil(t *testing.T, owner, lease, auth string, eol time.Time) string {
	t.Helper()
	sealed, err := Seal(key, Envelope{Auth: auth, Workspace: "ws-1", Lease: lease, Owner: owner, Estimate: 40,
		Stream: true, EndOfLife: eol})
	if err != nil {
		t.Fatal(err)
	}
	return sealed
}

// TestARequestWhoseOwnerIsNotReachedGoesThroughAPeer: a terminal or a
// heartbeat whose owner this front door cannot reach goes through a peer,
// neither this node nor the owner and the same for the authorization each
// time, and the owner's answer through it is the answer; only when the
// peer cannot reach the owner either does the terminal go to the drain log
// and the heartbeat get Retry (§4.3).
func TestARequestWhoseOwnerIsNotReachedGoesThroughAPeer(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"] = true
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}
	sealed := sealedAt(t, "node-b", "lease-1", "gwa-1")
	for range 10 {
		if got := f.door.Settle(ctx, settle(sealed)); got != (TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}) {
			t.Fatalf("a settle through a peer: %+v", got)
		}
	}
	var peers []string
	for _, e := range f.ev.all() {
		var peer, owner string
		if n, _ := fmt.Sscanf(e, "peer %s %s", &peer, &owner); n == 2 {
			peers = append(peers, peer)
		}
	}
	if len(peers) != 10 || peers[0] == "node-a" || peers[0] == "node-b" {
		t.Fatalf("the peers: %v", peers)
	}
	for _, p := range peers {
		if p != peers[0] {
			t.Fatalf("one authorization's retries went through peers %v", peers)
		}
	}
	if len(f.store.appended) != 0 {
		t.Fatalf("appended %+v", f.store.appended)
	}
	f.peers.heartbeat = HeartbeatAnswer{Status: Accepted, Deadline: start.Add(time.Minute)}
	if got := f.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealed, GatewaySeq: 1}); got != f.peers.heartbeat {
		t.Fatalf("a heartbeat through a peer: %+v", got)
	}

	f.peers.terminal = OwnerTerminalAnswer{Status: PastCutoff}
	if got := f.door.Settle(ctx, settle(sealed)); got.Status != Recorded || f.store.appended[0].Cause != "past_cutoff" {
		t.Fatalf("an owner past its cutoff, through a peer: %+v, %+v", got, f.store.appended)
	}
	f.peers.unreachable["node-b"] = true
	if got := f.door.Settle(ctx, settle(sealed)); got.Status != Recorded || f.store.appended[1].Cause != "unreachable" {
		t.Fatalf("an owner no one reaches: %+v, %+v", got, f.store.appended)
	}
	if got := f.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealed, GatewaySeq: 2}); got.Status != Retry {
		t.Fatalf("a heartbeat no one delivers: %+v", got)
	}

	// With no member but this node and the owner, there is no peer.
	f = newPeers(t)
	f.door.cfg.Members = fakeMembers{owners("node-a", "node-b")}
	f.owners.unreachable["node-b"] = true
	if got := f.door.Settle(ctx, settle(sealed)); got.Status != Recorded {
		t.Fatalf("a settle with no peer: %+v", got)
	}
	for _, e := range f.ev.all() {
		if strings.HasPrefix(e, "peer ") {
			t.Fatalf("a peer with none to be: %q", f.ev.all())
		}
	}
}

// TestAHeartbeatTriesItsPeerWithinItsCap: a peer that does not answer
// holds a heartbeat up only PeerWait, not the owner's wait.
func TestAHeartbeatTriesItsPeerWithinItsCap(t *testing.T) {
	f := newPeers(t)
	f.owners.unreachable["node-b"] = true
	f.peers.holding = true
	began := time.Now()
	got := f.door.Heartbeat(context.Background(), HeartbeatOf{Envelope: sealedAt(t, "node-b", "lease-1", "gwa-1"),
		GatewaySeq: 1})
	if got.Status != Retry || time.Since(began) > 500*time.Millisecond {
		t.Fatalf("a heartbeat whose peer holds it: %+v after %v", got, time.Since(began))
	}
}

// TestAFrontDoorCutOffFromTwoOwnersWithdraws: calls to two owners that fail
// here while a peer reaches them, within the window, withdraw the front
// door: its row says so, and it takes no new request. It serves again once
// it reaches each of them that is still a member.
func TestAFrontDoorCutOffFromTwoOwnersWithdraws(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	refund := func(owner string) {
		t.Helper()
		got := f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, owner, "lease-"+owner, "gwa-"+owner), Money: []byte("{}")})
		if got.Status != Won {
			t.Fatalf("a refund through a peer: %+v", got)
		}
	}
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = true, true
	refund("node-b")
	f.advance(6 * time.Second)
	refund("node-c")
	f.door.write(ctx)
	if f.door.Withdrawn() || len(f.node.written()) != 0 {
		t.Fatalf("two owners six seconds apart: withdrawn %v, %v", f.door.Withdrawn(), f.node.written())
	}
	f.advance(time.Second)
	refund("node-b")
	if len(f.node.written()) != 0 {
		t.Fatalf("a request wrote the row: %v", f.node.written())
	}
	f.door.write(ctx)
	if !f.door.Withdrawn() || !slices.Equal(f.node.written(), []string{store.Withdrawn}) {
		t.Fatalf("two owners within the window: withdrawn %v, %v", f.door.Withdrawn(), f.node.written())
	}
	n := len(f.ev.all())
	if got := f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 1, Boot: []byte("b")}); got.Status != Busy ||
		len(f.ev.all()) != n {
		t.Fatalf("an authorize at a withdrawn front door: %+v, %q", got, f.ev.all()[n:])
	}
	f.door.probe(ctx)
	if !f.door.Withdrawn() {
		t.Fatal("served again with its owners still not reached")
	}
	f.owners.unreachable["node-b"] = false
	f.view.Members = slices.DeleteFunc(f.view.Members, func(m store.Member) bool { return m.Address == "node-c" })
	f.door.cfg.Members = fakeMembers{f.view}
	f.door.probe(ctx)
	f.door.write(ctx)
	if f.door.Withdrawn() || !slices.Equal(f.node.written(), []string{store.Withdrawn, store.Serving}) {
		t.Fatalf("its one live owner reached again: withdrawn %v, %v", f.door.Withdrawn(), f.node.written())
	}
	f.owners.admitted[pick("r", 1)] = OwnerAdmitted{Status: Admitted, Envelope: "sealed"}
	if got := f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 1, Boot: []byte("b")}); got.Status != Admitted {
		t.Fatalf("an authorize after serving again: %+v", got)
	}
}

// TestALeaseWhoseOwnerNoOneReachesIsRevoked: a lease whose owner neither
// this front door nor its peer has reached for RevokeAfter is revoked,
// once; one lease a RevokeEvery; one reached meanwhile starts over; and a
// withdrawn front door revokes none.
func TestALeaseWhoseOwnerNoOneReachesIsRevoked(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	fail := func(lease string) {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
		f.door.write(ctx)
	}
	revoked := func() []string {
		var out []string
		for _, e := range f.ev.all() {
			var lease string
			if n, _ := fmt.Sscanf(e, "revoke %s", &lease); n == 1 {
				out = append(out, lease)
			}
		}
		return out
	}
	fail("l1")
	fail("l2")
	f.advance(10*time.Second - time.Microsecond)
	fail("l1")
	if got := revoked(); len(got) != 0 {
		t.Fatalf("revoked before ten seconds: %v", got)
	}
	f.advance(time.Microsecond)
	fail("l1")
	fail("l2")
	fail("l1")
	if got := revoked(); !slices.Equal(got, []string{"l1"}) {
		t.Fatalf("revoked at ten seconds, one a minute: %v", got)
	}
	f.advance(time.Minute)
	fail("l2")
	fail("l1")
	if got := revoked(); !slices.Equal(got, []string{"l1", "l2"}) {
		t.Fatalf("revoked a minute later: %v", got)
	}
	f.advance(time.Minute)
	fail("l1")
	if got := revoked(); !slices.Equal(got, []string{"l1", "l2"}) {
		t.Fatalf("a lease revoked again: %v", got)
	}

	// A revocation that failed is tried again, at the rate's next turn.
	f = newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	f.store.failRevoke = 1
	fail("l5")
	f.advance(10 * time.Second)
	fail("l5")
	f.advance(time.Minute)
	fail("l5")
	if got := revoked(); !slices.Equal(got, []string{"l5", "l5"}) {
		t.Fatalf("a revocation that failed, then again: %v", got)
	}

	f = newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	fail("l3")
	f.advance(11 * time.Second)
	f.owners.unreachable["node-b"] = false
	f.owners.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	fail("l3")
	f.owners.unreachable["node-b"] = true
	fail("l3")
	f.advance(9 * time.Second)
	fail("l3")
	if got := revoked(); len(got) != 0 {
		t.Fatalf("a lease reached meanwhile: revoked %v", got)
	}

	f = newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	f.door.mu.Lock()
	f.door.withdrawn = true
	f.door.mu.Unlock()
	fail("l4")
	f.advance(10 * time.Second) // its holds alive still
	fail("l4")
	if got := revoked(); len(got) != 0 {
		t.Fatalf("a withdrawn front door revoked %v", got)
	}
}

// TestPeersInTheProcess: a front door that cannot reach an owner has its
// peer in the process relay the terminal to it, and the owner's winner is
// the answer.
func TestPeersInTheProcess(t *testing.T) {
	ctx := context.Background()
	f := newLocal(t) // the owner, at node-a
	_, e := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Boot: []byte("boot")})
	sealed, err := Seal(key, e)
	if err != nil {
		t.Fatal(err)
	}
	ev := &events{}
	view := owners("node-x", "node-a", "node-c")
	door := func(self string, to Owners, peers Peers) *FrontDoor {
		d, err := New(Config{Owners: to, Store: &fakeStore{ev: ev}, Records: &fakeRecords{ev: ev},
			Members: fakeMembers{view}, Key: key, Shards: func(string) int64 { return 1 }, OwnerWait: time.Second,
			PublishWait: time.Second, Self: self, Peers: peers, PeerWait: time.Second})
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	c := door("node-c", Direct{"node-a": f.local}, nil)
	x := door("node-x", Direct{}, DirectPeers{"node-c": c})
	got := x.Settle(ctx, settle(sealed))
	if got != (TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}) {
		t.Fatalf("a settle relayed through node-c: %+v", got)
	}
	if _, err := (DirectPeers{}).Terminal(ctx, "node-y", "node-a", OwnerTerminal{}); !errors.Is(err, ErrUnreachable) {
		t.Fatalf("a peer not in the process: %v", err)
	}
}

// TestAPeersAnswerStartsALeaseOver: a lease whose owner a peer reached is
// not failing: its time to revocation starts again at its next failure.
func TestAPeersAnswerStartsALeaseOver(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	refund := func() {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", "l1", "gwa-l1"), Money: []byte("{}")})
		f.door.write(ctx)
	}
	refund()
	f.advance(9 * time.Second)
	f.peers.unreachable["node-b"] = false
	refund() // through the peer
	f.peers.unreachable["node-b"] = true
	f.advance(time.Second)
	refund()
	f.advance(9 * time.Second)
	refund()
	for _, e := range f.ev.all() {
		if strings.HasPrefix(e, "revoke ") {
			t.Fatalf("revoked nine seconds after a peer reached its owner: %q", f.ev.all())
		}
	}
	f.advance(time.Second)
	refund()
	if !slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("not revoked ten seconds after its last failure began: %q", f.ev.all())
	}
}

// withdrawn withdraws the fixture's front door: calls to node-b and node-c
// fail here while a peer reaches them.
func (f *peersFixture) withdrawn(t *testing.T) {
	t.Helper()
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = true, true
	for _, o := range []string{"node-b", "node-c"} {
		f.door.Refund(context.Background(), RefundOf{Envelope: sealedAt(t, o, "lease-"+o, "gwa-"+o), Money: []byte("{}")})
	}
	if !f.door.Withdrawn() {
		t.Fatal("not withdrawn")
	}
}

// TestTheRowGetsTheStatesInTheOrderWanted: the row is written the latest
// state wanted when Run writes, and a state wanted while a write is under
// way is written after it, so the row ends at the latest.
func TestTheRowGetsTheStatesInTheOrderWanted(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.withdrawn(t)
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = false, false
	f.door.probe(ctx)
	f.door.write(ctx)
	if got := f.node.written(); !slices.Equal(got, []string{store.Serving}) {
		t.Fatalf("withdrawn and serving again before Run wrote: %v", got)
	}

	f = newPeers(t)
	f.withdrawn(t)
	f.node.gated()
	wrote := make(chan struct{})
	go func() {
		f.door.write(ctx)
		close(wrote)
	}()
	if got := <-f.node.began; got != store.Withdrawn {
		t.Fatalf("the first write: %s", got)
	}
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = false, false
	f.door.probe(ctx)
	close(f.node.gate)
	<-wrote
	if got := f.node.written(); !slices.Equal(got, []string{store.Withdrawn, store.Serving}) {
		t.Fatalf("the row's writes: %v", got)
	}
}

// TestARequestWaitsForNoWrite: a heartbeat that withdraws the front door,
// or that makes a lease due for revocation, answers without the write,
// which Run makes.
func TestARequestWaitsForNoWrite(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.node.gated()
	f.peers.heartbeat = HeartbeatAnswer{Status: Accepted}
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = true, true
	beat := func(owner, lease string) HeartbeatAnswer {
		t.Helper()
		return f.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealedAt(t, owner, lease, "gwa-"+lease), GatewaySeq: 1})
	}
	began := time.Now()
	beat("node-b", "l1")
	beat("node-c", "l2")
	if !f.door.Withdrawn() || time.Since(began) > 500*time.Millisecond {
		t.Fatalf("withdrawn %v after %v", f.door.Withdrawn(), time.Since(began))
	}
	select {
	case got := <-f.node.began:
		t.Fatalf("a request wrote the row: %s", got)
	default:
	}

	g := newPeers(t)
	g.owners.unreachable["node-b"], g.peers.unreachable["node-b"] = true, true
	g.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealedAt(t, "node-b", "l3", "gwa-l3"), GatewaySeq: 1})
	g.advance(10 * time.Second)
	g.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealedAt(t, "node-b", "l3", "gwa-l3"), GatewaySeq: 1})
	if slices.Contains(g.ev.all(), "revoke l3") {
		t.Fatalf("a request revoked: %q", g.ev.all())
	}
	g.door.write(ctx)
	if !slices.Contains(g.ev.all(), "revoke l3") {
		t.Fatalf("Run did not revoke: %q", g.ev.all())
	}
}

// TestRunEndsWithItsWrites: Run returns once a write under way has ended
// with its context, and once it has, no request writes.
func TestRunEndsWithItsWrites(t *testing.T) {
	f := newPeers(t)
	f.node.gated()
	ctx, cancel := context.WithCancel(context.Background())
	ran := make(chan struct{})
	go func() {
		_ = f.door.Run(ctx)
		close(ran)
	}()
	f.withdrawn(t)
	<-f.node.began
	cancel()
	select {
	case <-ran:
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not end")
	}
	select {
	case <-f.node.ended:
	default:
		t.Fatal("Run ended before its write")
	}
	f.owners.unreachable["node-d"], f.owners.unreachable["node-e"] = true, true
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	for range 2 {
		for _, o := range []string{"node-d", "node-e", "node-b"} {
			f.door.Refund(context.Background(), RefundOf{Envelope: sealedAt(t, o, "lease-"+o, "gwa-"+o), Money: []byte("{}")})
		}
		f.advance(time.Hour)
	}
	select {
	case got := <-f.node.began:
		t.Fatalf("a write after Run ended: %s", got)
	default:
	}
	for _, e := range f.ev.all() {
		if strings.HasPrefix(e, "revoke ") {
			t.Fatalf("a revocation after Run ended: %q", f.ev.all())
		}
	}

	// A revocation under way when Run's context ends.
	g := newPeers(t)
	g.store.gate, g.store.revoking, g.store.revoked = make(chan struct{}), make(chan string, 8), make(chan string, 8)
	g.owners.unreachable["node-b"], g.peers.unreachable["node-b"] = true, true
	fail := func(lease string) {
		g.door.Refund(context.Background(), RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
	}
	ctx, cancel = context.WithCancel(context.Background())
	ran = make(chan struct{})
	go func() {
		_ = g.door.Run(ctx)
		close(ran)
	}()
	fail("l1")
	fail("l2")
	g.advance(10 * time.Second)
	fail("l1")
	fail("l2")
	<-g.store.revoking
	cancel()
	select {
	case <-ran:
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not end")
	}
	select {
	case <-g.store.revoked:
	default:
		t.Fatal("Run ended before its revocation")
	}
	g.advance(time.Hour)
	fail("l2")
	select {
	case got := <-g.store.revoking:
		t.Fatalf("a revocation after Run ended: %s", got)
	case <-time.After(100 * time.Millisecond):
	}
}

// TestARevocationIsDecidedWhenItIsMade: the writer decides each revocation
// as it makes it, from the failures kept then: a lease whose owner answered
// and that failed again since starts over, however long it had failed
// before; and the leases due go a RevokeEvery apart, however late the
// writer runs.
func TestARevocationIsDecidedWhenItIsMade(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	refund := func(lease string) {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
	}
	refund("l1")
	f.advance(10 * time.Second)
	refund("l1") // due
	f.peers.unreachable["node-b"] = false
	refund("l2") // node-b reached through the peer
	f.peers.unreachable["node-b"] = true
	f.advance(time.Microsecond)
	refund("l1") // failing again, from now
	f.door.write(ctx)
	if slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("revoked from a failure its owner answered since: %q", f.ev.all())
	}
	f.advance(10 * time.Second)
	refund("l1")
	f.door.write(ctx)
	if !slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("not revoked ten seconds after failing again: %q", f.ev.all())
	}

	g := newPeers(t)
	g.owners.unreachable["node-b"], g.peers.unreachable["node-b"] = true, true
	fail := func(lease string) {
		t.Helper()
		g.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
	}
	revoked := func() []string {
		var out []string
		for _, e := range g.ev.all() {
			if lease, ok := strings.CutPrefix(e, "revoke "); ok {
				out = append(out, lease)
			}
		}
		return out
	}
	fail("l1")
	g.advance(10 * time.Second)
	fail("l1") // due, the writer late
	g.advance(50 * time.Second)
	fail("l2")
	g.advance(10 * time.Second)
	fail("l2") // due a minute later
	g.door.write(ctx)
	g.door.write(ctx)
	if got := revoked(); !slices.Equal(got, []string{"l1"}) {
		t.Fatalf("a late writer: revoked %v", got)
	}
	g.advance(time.Minute - time.Microsecond)
	g.door.write(ctx)
	if got := revoked(); !slices.Equal(got, []string{"l1"}) {
		t.Fatalf("within a minute of the last revocation: revoked %v", got)
	}
	g.advance(time.Microsecond)
	g.door.write(ctx)
	if got := revoked(); !slices.Equal(got, []string{"l1", "l2"}) {
		t.Fatalf("a minute after the last revocation: revoked %v", got)
	}
}

// TestTimesAreReadInTheOrderSeen: a failure whose clock is read before an
// answer from its owner and that is numbered after it would start a
// failure period that answer should have ended; read under the front door's
// lock, the time and its number agree, and the lease's next failure starts
// over.
func TestTimesAreReadInTheOrderSeen(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	refund := func(lease string) {
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
	}
	f.mu.Lock()
	f.slowAt, f.slow, f.slowed = 2, make(chan struct{}), make(chan struct{}) // l1's failure everywhere
	f.mu.Unlock()
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		refund("l1") // fails here, at the peer, then reads the clock slowly
	}()
	<-f.slowed
	f.advance(5 * time.Second)
	f.peers.unreachable["node-b"] = false
	go func() {
		defer wg.Done()
		refund("l2") // node-b answers through the peer
	}()
	time.Sleep(50 * time.Millisecond)
	close(f.slow)
	wg.Wait()
	f.peers.unreachable["node-b"] = true
	f.advance(5 * time.Second)
	refund("l1")
	f.door.write(ctx)
	if slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("revoked five seconds after its owner answered: %q", f.ev.all())
	}
}

// TestRevocationsAreSpacedFromTheirEnd: a revocation that takes a while
// holds the next a RevokeEvery from when it ended.
func TestRevocationsAreSpacedFromTheirEnd(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	fail := func(lease string) {
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
	}
	fail("l1")
	fail("l2")
	f.advance(10 * time.Second)
	fail("l1")
	fail("l2") // both due
	f.store.onRevoke = func() { f.advance(time.Minute) }
	f.door.write(ctx)
	f.store.onRevoke = nil
	revoked := func() int {
		n := 0
		for _, e := range f.ev.all() {
			if strings.HasPrefix(e, "revoke ") {
				n++
			}
		}
		return n
	}
	if n := revoked(); n != 1 {
		t.Fatalf("%d revocations as the first ended a minute after it began", n)
	}
	f.advance(time.Minute - time.Microsecond)
	f.door.write(ctx)
	if n := revoked(); n != 1 {
		t.Fatalf("%d revocations within a minute of the first's end", n)
	}
	f.advance(time.Microsecond)
	f.door.write(ctx)
	if n := revoked(); n != 2 {
		t.Fatalf("%d revocations a minute after the first's end", n)
	}
}

// TestAProbeKeepsAnOwnerThatFailedHereAgain: an owner whose call fails here
// during its probe keeps the front door withdrawn, though the peer could
// not reach it either.
func TestAProbeKeepsAnOwnerThatFailedHereAgain(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.withdrawn(t)
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = false, false
	f.owners.pinging, f.owners.pingGate = make(chan string), make(chan struct{})
	probed := make(chan struct{})
	go func() {
		f.door.probe(ctx)
		close(probed)
	}()
	first := <-f.owners.pinging
	f.owners.unreachable[first], f.peers.unreachable[first] = true, true
	f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, first, "lease-x", "gwa-x"), Money: []byte("{}")})
	f.owners.unreachable[first], f.peers.unreachable[first] = false, false
	close(f.owners.pingGate)
	go func() {
		for range f.owners.pinging {
		}
	}()
	<-probed
	if !f.door.Withdrawn() {
		t.Fatalf("served again with %s's call failing here during its probe", first)
	}

	// An owner that answered its ping, then failed here, at the peer too,
	// or in a relay for a peer, while another's ping was under way.
	for name, fail := range map[string]func(g *peersFixture, owner string){
		"a request": func(g *peersFixture, owner string) {
			g.owners.unreachable[owner], g.peers.unreachable[owner] = true, true
			g.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, owner, "lease-y", "gwa-y"), Money: []byte("{}")})
			g.owners.unreachable[owner], g.peers.unreachable[owner] = false, false
		},
		"a relay": func(g *peersFixture, owner string) {
			g.owners.unreachable[owner] = true
			if _, err := g.door.RelayTerminal(ctx, owner, OwnerTerminal{}); err == nil {
				t.Fatal("a relay to an owner not reached answered")
			}
			g.owners.unreachable[owner] = false
		},
	} {
		g := newPeers(t)
		g.withdrawn(t)
		g.owners.unreachable["node-b"], g.owners.unreachable["node-c"] = false, false
		g.owners.pinging, g.owners.pingGate = make(chan string), make(chan struct{})
		probed := make(chan struct{})
		go func() {
			g.door.probe(ctx)
			close(probed)
		}()
		answered := <-g.owners.pinging
		g.owners.pingGate <- struct{}{}
		<-g.owners.pinging // the other's ping under way
		fail(g, answered)
		g.owners.pingGate <- struct{}{}
		<-probed
		if !g.door.Withdrawn() {
			t.Fatalf("%s: served again with %s failing here after it answered its ping", name, answered)
		}
	}
}

// TestTheWindowIsBetweenThePeersAnswers: a failure here is evidence
// against the front door once a peer answers, and is dated then; evidence
// for two owners within the window of one another withdraws it, however
// far apart the failures here were.
func TestTheWindowIsBetweenThePeersAnswers(t *testing.T) {
	ctx := context.Background()
	for name, c := range map[string]struct {
		slowFirst, slowSecond time.Duration
		apart                 time.Duration
		withdraws             bool
	}{
		"failures 5.05 seconds apart, answers 4.95":     {100 * time.Millisecond, 0, 5050 * time.Millisecond, true},
		"failures 4.95 seconds apart, answers 5.05":     {0, 100 * time.Millisecond, 4950 * time.Millisecond, false},
		"answers exactly the window apart":              {0, 0, 5 * time.Second, true},
		"failures 9.8 seconds apart, answers 4.9":       {4900 * time.Millisecond, 0, 9800 * time.Millisecond, true},
		"failures 4.9 seconds apart, answers 9.8 apart": {0, 4900 * time.Millisecond, 4900 * time.Millisecond, false},
	} {
		f := newPeers(t)
		f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
		f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = true, true
		refund := func(owner string, slow time.Duration) {
			f.peers.onCall = func() { f.advance(slow) }
			f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, owner, "lease-"+owner, "gwa-"+owner), Money: []byte("{}")})
			f.peers.onCall = nil
		}
		began := f.clock()
		refund("node-b", c.slowFirst)
		f.advance(began.Add(c.apart).Sub(f.clock()))
		refund("node-c", c.slowSecond)
		if f.door.Withdrawn() != c.withdraws {
			t.Fatalf("%s: withdrawn %v", name, f.door.Withdrawn())
		}
	}
}

// TestAnAuthorizeThatFailsHereCounts: an authorize whose owner a call here
// does not reach is a failure here, as a probe reads it; one whose request
// ended is not.
func TestAnAuthorizeThatFailsHereCounts(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	owner, _ := f.view.Owner(ring.ShardKey("ws-1", 0))
	f.owners.unreachable[owner.Address] = true
	ended, cancel := context.WithCancel(ctx)
	cancel()
	authorize := func(ctx context.Context) {
		f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 1, Boot: []byte("b")})
	}
	here := func() uint64 {
		f.door.mu.Lock()
		defer f.door.mu.Unlock()
		return f.door.here[owner.Address].seq
	}
	authorize(ended)
	if got := here(); got != 0 {
		t.Fatalf("an ended authorize counted as a failure here: %d", got)
	}
	authorize(ctx)
	if got := here(); got == 0 {
		t.Fatal("an authorize whose owner failed here did not count")
	}
}

// TestARevokedMarkLastsFromTheRevocationsEnd: a revocation that takes a
// while marks its lease from its end, since a renewal may land while it is
// under way, and the lease's holds may live HoldLife from then.
func TestARevokedMarkLastsFromTheRevocationsEnd(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	eol := start.Add(3*time.Hour + 10*time.Minute) // a hold the last renewal, under the revocation, admitted
	fail := func() {
		f.door.Refund(ctx, RefundOf{Envelope: sealedUntil(t, "node-b", "l1", "gwa-l1", eol), Money: []byte("{}")})
		f.door.write(ctx)
	}
	fail()
	f.advance(10 * time.Second)
	f.store.onRevoke = func() { f.advance(time.Minute) }
	fail() // revoked from 10 seconds to 70
	f.store.onRevoke = nil
	f.advance(3*time.Hour - 5*time.Second) // past HoldLife from the revocation's start, not its end
	f.door.forget()
	fail()
	f.advance(10 * time.Second)
	fail()
	n := 0
	for _, e := range f.ev.all() {
		if e == "revoke l1" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("l1 revoked %d times, its hold alive past HoldLife from the revocation's start", n)
	}
}

// TestAnEndedHoldIsNoEvidence: a request for a hold past its end of life,
// as its envelope states, makes its lease no closer to revocation; and the
// marks of leases revoked go once their holds have all ended.
func TestAnEndedHoldIsNoEvidence(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	fail := func(lease string, eol time.Time) {
		f.door.Refund(ctx, RefundOf{Envelope: sealedUntil(t, "node-b", lease, "gwa-"+lease, eol), Money: []byte("{}")})
		f.door.write(ctx)
	}
	ended := start.Add(-time.Second)
	fail("l1", ended)
	f.advance(10 * time.Second)
	fail("l1", ended)
	if slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("a lease revoked on ended holds' failures: %q", f.ev.all())
	}
	fail("l2", start.Add(time.Hour))
	f.advance(10 * time.Second)
	fail("l2", start.Add(time.Hour))
	if !slices.Contains(f.ev.all(), "revoke l2") {
		t.Fatalf("l2 not revoked: %q", f.ev.all())
	}
	f.advance(3*time.Hour - time.Second)
	f.door.forget()
	f.door.mu.Lock()
	kept := len(f.door.revoked)
	f.door.mu.Unlock()
	if kept != 1 {
		t.Fatalf("%d revoked leases kept within their holds' life", kept)
	}
	f.advance(2 * time.Second)
	f.door.forget()
	f.door.mu.Lock()
	kept = len(f.door.revoked)
	f.door.mu.Unlock()
	if kept != 0 {
		t.Fatalf("%d revoked leases kept past their holds' life", kept)
	}
}

// TestALeaseIsRevokedOnce: a revoked lease whose calls go on failing past
// the hour the front door keeps what it saw is not revoked again.
func TestALeaseIsRevokedOnce(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	eol := start.Add(3 * time.Hour) // within HoldLife of the revocation, as every hold of l1 is
	for range 13 {
		f.door.Refund(ctx, RefundOf{Envelope: sealedUntil(t, "node-b", "l1", "gwa-l1", eol), Money: []byte("{}")})
		f.door.write(ctx)
		f.door.forget()
		f.advance(20 * time.Minute)
	}
	n := 0
	for _, e := range f.ev.all() {
		if e == "revoke l1" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("l1 revoked %d times over four hours of failures", n)
	}

	// Nor once its calls have stopped for hours, and come again.
	g := newPeers(t)
	g.owners.unreachable["node-b"], g.peers.unreachable["node-b"] = true, true
	fail := func() {
		g.door.Refund(ctx, RefundOf{Envelope: sealedUntil(t, "node-b", "l1", "gwa-l1", eol), Money: []byte("{}")})
		g.door.write(ctx)
	}
	fail()
	g.advance(10 * time.Second)
	fail()
	g.advance(2 * time.Hour)
	g.door.forget()
	fail()
	g.advance(10 * time.Second)
	fail()
	n = 0
	for _, e := range g.ev.all() {
		if e == "revoke l1" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("l1 revoked %d times, its calls failing again after two quiet hours", n)
	}
}

// TestEveryAnswerIsAReach: an owner that answers a relay for a peer, an
// authorize, or a probe has its leases start over, as one that answers a
// heartbeat or a terminal does.
func TestEveryAnswerIsAReach(t *testing.T) {
	ctx := context.Background()
	for name, reach := range map[string]func(f *peersFixture, owner string){
		"a relay": func(f *peersFixture, owner string) {
			if _, err := f.door.RelayTerminal(ctx, owner, OwnerTerminal{}); err != nil {
				t.Fatal(err)
			}
		},
		"an authorize": func(f *peersFixture, owner string) {
			f.owners.admitted[0] = OwnerAdmitted{Status: Busy}
			f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 1, Boot: []byte("b")})
		},
	} {
		f := newPeers(t)
		owner, _ := f.view.Owner(ring.ShardKey("ws-1", 0))
		f.owners.unreachable[owner.Address], f.peers.unreachable[owner.Address] = true, true
		fail := func() {
			t.Helper()
			f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, owner.Address, "l1", "gwa-l1"), Money: []byte("{}")})
			f.door.write(ctx)
		}
		fail()
		f.advance(9 * time.Second)
		f.owners.unreachable[owner.Address] = false
		reach(f, owner.Address)
		f.owners.unreachable[owner.Address] = true
		f.advance(time.Second)
		fail()
		if slices.Contains(f.ev.all(), "revoke l1") {
			t.Fatalf("%s: revoked a second after its owner answered: %q", name, f.ev.all())
		}
	}

	// A probe: node-b and node-c withdraw the front door, then node-b
	// fails everywhere for l1, then a probe reaches both.
	f := newPeers(t)
	f.withdrawn(t)
	f.peers.unreachable["node-b"] = true
	fail := func() {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", "l1", "gwa-l1"), Money: []byte("{}")})
		f.door.write(ctx)
	}
	fail()
	f.advance(9 * time.Second)
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = false, false
	f.door.probe(ctx)
	if f.door.Withdrawn() {
		t.Fatal("still withdrawn")
	}
	f.owners.unreachable["node-b"] = true
	f.advance(time.Second)
	fail()
	if slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("a probe: revoked a second after its owner answered: %q", f.ev.all())
	}
}

// TestARevocationIsCheckedJustBeforeItIsMade: a lease due for revocation
// is not revoked if, before Run revokes it, the front door withdraws, and
// the row says withdrawn; nor if its owner answers meanwhile, for another
// lease.
func TestARevocationIsCheckedJustBeforeItIsMade(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-d"], f.peers.unreachable["node-d"] = true, true
	fail := func() {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-d", "l1", "gwa-l1"), Money: []byte("{}")})
	}
	fail()
	f.advance(10 * time.Second)
	fail() // l1 due
	f.withdrawn(t)
	f.door.write(ctx)
	if slices.Contains(f.ev.all(), "revoke l1") || !slices.Equal(f.node.written(), []string{store.Withdrawn}) {
		t.Fatalf("a revocation due before a withdrawal: %q, the row %v", f.ev.all(), f.node.written())
	}

	g := newPeers(t)
	g.owners.unreachable["node-d"], g.peers.unreachable["node-d"] = true, true
	g.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	refund := func(lease string) {
		t.Helper()
		g.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-d", lease, "gwa-"+lease), Money: []byte("{}")})
	}
	refund("l1")
	g.advance(10 * time.Second)
	refund("l1") // l1 due
	g.peers.unreachable["node-d"] = false
	refund("l2") // node-d reached through the peer
	g.door.write(ctx)
	if slices.Contains(g.ev.all(), "revoke l1") {
		t.Fatalf("a revocation due before its owner answered: %q", g.ev.all())
	}
}

// TestReachingAnOwnerStartsEachOfItsLeasesOver: an owner a peer reaches for
// one lease is reached for all: another lease's time to revocation starts
// again at its next failure.
func TestReachingAnOwnerStartsEachOfItsLeasesOver(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.peers.unreachable["node-b"] = true, true
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	refund := func(lease string) {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, "node-b", lease, "gwa-"+lease), Money: []byte("{}")})
		f.door.write(ctx)
	}
	refund("l1")
	f.advance(9 * time.Second)
	f.peers.unreachable["node-b"] = false
	refund("l2") // node-b reached through the peer
	f.peers.unreachable["node-b"] = true
	f.advance(time.Second)
	refund("l1")
	f.advance(10*time.Second - time.Microsecond)
	refund("l1")
	if slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("l1 revoked within ten seconds of its owner answering: %q", f.ev.all())
	}
	f.advance(time.Microsecond)
	refund("l1")
	if !slices.Contains(f.ev.all(), "revoke l1") {
		t.Fatalf("l1 not revoked ten seconds after its failure began again: %q", f.ev.all())
	}
}

// TestAProbeKeepsWhatItDidNotTry: an owner a request finds unreachable here
// while a probe runs, or finds so again, keeps the front door withdrawn
// until a later probe reaches it; an owner no longer a member is forgotten,
// though another does not answer.
func TestAProbeKeepsWhatItDidNotTry(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	refund := func(owner string) {
		t.Helper()
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, owner, "lease-"+owner, "gwa-"+owner), Money: []byte("{}")})
	}
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = true, true
	refund("node-b")
	refund("node-c")
	if !f.door.Withdrawn() {
		t.Fatal("not withdrawn")
	}
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = false, false
	f.owners.pinging, f.owners.pingGate = make(chan string), make(chan struct{})
	probed := make(chan struct{})
	go func() {
		f.door.probe(ctx)
		close(probed)
	}()
	<-f.owners.pinging // the first owner's ping is under way
	f.owners.unreachable["node-d"] = true
	refund("node-d") // found unreachable here during the probe
	close(f.owners.pingGate)
	go func() {
		for range f.owners.pinging {
		}
	}()
	<-probed
	if !f.door.Withdrawn() {
		t.Fatal("served again with an owner found unreachable during the probe")
	}
	f.owners.unreachable["node-d"] = false
	f.door.probe(ctx)
	if f.door.Withdrawn() {
		t.Fatal("still withdrawn with every owner answering")
	}

	// The same owner found unreachable again during its probe, at the same
	// time by the clock, which has not moved.
	h := newPeers(t)
	h.withdrawn(t)
	h.owners.unreachable["node-b"], h.owners.unreachable["node-c"] = false, false
	h.owners.pinging, h.owners.pingGate = make(chan string), make(chan struct{})
	probing := make(chan struct{})
	go func() {
		h.door.probe(ctx)
		close(probing)
	}()
	first := <-h.owners.pinging
	h.owners.unreachable[first] = true
	h.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, first, "lease-"+first, "gwa-"+first), Money: []byte("{}")})
	h.owners.unreachable[first] = false
	close(h.owners.pingGate)
	go func() {
		for range h.owners.pinging {
		}
	}()
	<-probing
	if !h.door.Withdrawn() {
		t.Fatalf("served again with %s found unreachable during its probe", first)
	}

	g := newPeers(t)
	g.peers.terminal = OwnerTerminalAnswer{Status: Won, Kind: record.Refund}
	g.owners.unreachable["node-b"], g.owners.unreachable["node-c"] = true, true
	for _, o := range []string{"node-b", "node-c"} {
		g.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, o, "lease-"+o, "gwa-"+o), Money: []byte("{}")})
	}
	g.view.Members = slices.DeleteFunc(g.view.Members, func(m store.Member) bool { return m.Address == "node-c" })
	g.door.cfg.Members = fakeMembers{g.view}
	g.door.probe(ctx) // node-b still does not answer
	g.door.mu.Lock()
	_, kept := g.door.unreached["node-c"]
	g.door.mu.Unlock()
	if kept || !g.door.Withdrawn() {
		t.Fatalf("a departed owner kept %v; withdrawn %v", kept, g.door.Withdrawn())
	}
}

// TestAnEndedRequestIsNoEvidence: a request that ended before its owner
// answered tries no peer and counts toward neither withdrawing nor
// revoking, though its owners are two within the window and a lease's
// calls span the time to revoke it.
func TestAnEndedRequestIsNoEvidence(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	f.owners.unreachable["node-b"], f.owners.unreachable["node-c"] = true, true
	ended, cancel := context.WithCancel(ctx)
	cancel()
	for _, o := range []string{"node-b", "node-c", "node-b", "node-c"} {
		f.door.Refund(ended, RefundOf{Envelope: sealedAt(t, o, "lease-"+o, "gwa-"+o), Money: []byte("{}")})
		f.door.Heartbeat(ended, HeartbeatOf{Envelope: sealedAt(t, o, "lease-"+o, "gwa-"+o), GatewaySeq: 1})
		f.advance(4 * time.Second)
	}
	f.door.write(ctx)
	for _, e := range f.ev.all() {
		if strings.HasPrefix(e, "peer ") || strings.HasPrefix(e, "revoke ") {
			t.Fatalf("an ended request taken as evidence: %q", f.ev.all())
		}
	}
	f.door.mu.Lock()
	failing, unreached := len(f.door.failing), len(f.door.unreached)
	f.door.mu.Unlock()
	if f.door.Withdrawn() || len(f.node.written()) != 0 || failing != 0 || unreached != 0 {
		t.Fatalf("withdrawn %v, the row %v, %d leases failing, %d owners unreached", f.door.Withdrawn(),
			f.node.written(), failing, unreached)
	}
}

// TestAFrontDoorWhoseOwnersNoOneReachesServes: owners that fail here and
// at the peer too are their own failure, not this front door's: it stays
// serving.
func TestAFrontDoorWhoseOwnersNoOneReachesServes(t *testing.T) {
	ctx := context.Background()
	f := newPeers(t)
	for _, o := range []string{"node-b", "node-c"} {
		f.owners.unreachable[o], f.peers.unreachable[o] = true, true
		f.door.Refund(ctx, RefundOf{Envelope: sealedAt(t, o, "lease-"+o, "gwa-"+o), Money: []byte("{}")})
	}
	f.door.write(ctx)
	if f.door.Withdrawn() || len(f.node.written()) != 0 {
		t.Fatalf("withdrawn %v, %v", f.door.Withdrawn(), f.node.written())
	}
}
