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
}

func (f *fakePeers) Heartbeat(ctx context.Context, peer, owner string, _ OwnerHeartbeat) (HeartbeatAnswer, error) {
	f.ev.add("peer %s heartbeat to %s", peer, owner)
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
	if f.unreachable[owner] {
		return OwnerTerminalAnswer{}, ErrUnreachable
	}
	return f.terminal, nil
}

// fakeNode is this node's row: the states written to it, in order.
type fakeNode struct {
	mu     sync.Mutex
	states []string
}

func (f *fakeNode) SetState(_ context.Context, state string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.states = append(f.states, state)
	return nil
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
}

func (f *peersFixture) clock() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
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
		ProbeEvery: time.Hour, RevokeAfter: 10 * time.Second, RevokeEvery: time.Minute, Clock: f.clock})
	if err != nil {
		t.Fatal(err)
	}
	f.door = door
	return f
}

// sealedAt is a sealed envelope of a hold under lease at owner.
func sealedAt(t *testing.T, owner, lease, auth string) string {
	t.Helper()
	sealed, err := Seal(key, Envelope{Auth: auth, Workspace: "ws-1", Lease: lease, Owner: owner, Estimate: 40,
		Stream: true, EndOfLife: start.Add(time.Hour)})
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
	for range 5 {
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
	if len(peers) != 5 || peers[0] == "node-a" || peers[0] == "node-b" {
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
	f.door.work.Wait()
	if f.door.Withdrawn() || len(f.node.written()) != 0 {
		t.Fatalf("two owners six seconds apart: withdrawn %v, %v", f.door.Withdrawn(), f.node.written())
	}
	f.advance(time.Second)
	refund("node-b")
	f.door.work.Wait()
	if !f.door.Withdrawn() || !slices.Equal(f.node.written(), []string{store.Withdrawn}) {
		t.Fatalf("two owners within the window: withdrawn %v, %v", f.door.Withdrawn(), f.node.written())
	}
	n := len(f.ev.all())
	if got := f.door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 1, Boot: []byte("b")}); got.Status != Busy ||
		len(f.ev.all()) != n {
		t.Fatalf("an authorize at a withdrawn front door: %+v, %q", got, f.ev.all()[n:])
	}
	f.door.probe(ctx)
	f.door.work.Wait()
	if !f.door.Withdrawn() {
		t.Fatal("served again with its owners still not reached")
	}
	f.owners.unreachable["node-b"] = false
	f.view.Members = slices.DeleteFunc(f.view.Members, func(m store.Member) bool { return m.Address == "node-c" })
	f.door.cfg.Members = fakeMembers{f.view}
	f.door.probe(ctx)
	f.door.work.Wait()
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
		f.door.work.Wait()
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
	f.advance(time.Hour)
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
