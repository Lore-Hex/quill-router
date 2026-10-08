package frontdoor

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fakeGrants is the store as an owner's top-ups see it: each grant granted,
// to expire at expiry, and kept. These tests make no other call of it.
type fakeGrants struct {
	owner.Spanner
	expiry time.Time
	mu     sync.Mutex
	asked  []store.GrantRequest
}

func (f *fakeGrants) Grant(_ context.Context, req store.GrantRequest) (store.GrantResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, req)
	return store.GrantResult{Expiry: f.expiry}, nil
}

func (f *fakeGrants) all() []store.GrantRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]store.GrantRequest(nil), f.asked...)
}

// granted is the grant of a lease.
func (f *fakeGrants) granted(lease string) (store.GrantRequest, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, g := range f.asked {
		if g.LeaseID == lease {
			return g, true
		}
	}
	return store.GrantRequest{}, false
}

// fakeLog is the settle log, acknowledging each record at once, or once
// hold is closed, when it is set.
type fakeLog struct {
	mu      sync.Mutex
	records map[string][][]byte
	hold    chan struct{}
}

type acked struct{ hold chan struct{} }

func (a acked) Wait(ctx context.Context) (string, error) {
	if a.hold != nil {
		select {
		case <-a.hold:
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	return "id", nil
}

func (f *fakeLog) Publish(lease string, data []byte) owner.Waiter {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.records[lease] = append(f.records[lease], data)
	return acked{f.hold}
}

func (f *fakeLog) published(lease string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.records[lease])
}

func (f *fakeLog) Resume(string) {}

type clock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

// ownerConfig is an owner at node-a whose shards ask for leases of 1,000 as
// they need them, each expiring a minute from the start.
func ownerConfig(c *clock, sp owner.Spanner) owner.Config {
	return owner.Config{Epoch: 1, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, NewAuthorization: store.NewAuthorizationID, Clock: c.Now,
		Spanner: sp, Node: "node-a", RenewEvery: time.Hour, Window: 30 * time.Second,
		TopUps: owner.TopUps{LowWater: 10, Cooldown: time.Millisecond, Horizon: time.Minute, Min: 1000, Max: 1000,
			IdleAfter: time.Hour, MaxLife: time.Hour}}
}

type localFixture struct {
	clock  *clock
	log    *fakeLog
	grants *fakeGrants
	owner  *owner.Owner
	local  *Local
	// minting, when set, holds each authorization minted until it closes.
	minting chan struct{}
	mu      sync.Mutex
}

func newLocal(t *testing.T) *localFixture {
	t.Helper()
	f := &localFixture{clock: &clock{now: start}, log: &fakeLog{records: map[string][][]byte{}},
		grants: &fakeGrants{expiry: start.Add(time.Minute)}}
	cfg := ownerConfig(f.clock, f.grants)
	cfg.NewAuthorization = func(lease string) (string, error) {
		f.mu.Lock()
		gate := f.minting
		f.mu.Unlock()
		if gate != nil {
			<-gate
		}
		return store.NewAuthorizationID(lease)
	}
	o, err := owner.New(cfg, f.log)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(o.Stop)
	l, err := NewLocal(o, "node-a", "us-central1", key)
	if err != nil {
		t.Fatal(err)
	}
	f.owner, f.local = o, l
	return f
}

// held is what the shard 0 leases of ws-1 hold.
func (f *localFixture) held(t *testing.T) int64 {
	t.Helper()
	var held int64
	for _, g := range f.grants.all() {
		if l, ok := f.owner.Lease(g.LeaseID); ok && g.Workspace == "ws-1" && g.WorkspaceShard == 0 {
			held += l.Books().Held
		}
	}
	return held
}

// admit authorizes until a lease the shard's top-up asked for admits it.
func (f *localFixture) admit(t *testing.T, req OwnerAuthorize) (OwnerAdmitted, Envelope) {
	t.Helper()
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(time.Millisecond) {
		got := f.local.Authorize(req)
		switch got.Status {
		case Busy:
			continue
		case Admitted:
			e, err := Open(key, got.Envelope)
			if err != nil {
				t.Fatal(err)
			}
			return got, e
		}
		t.Fatalf("an authorize: %+v", got)
	}
	t.Fatal("no lease came to admit the request")
	return OwnerAdmitted{}, Envelope{}
}

// TestTheOwnerSealsWhatItAdmits: an authorize is held under a lease of its
// shard, and the envelope names the owner, the lease, the hold and its end
// of life; one the owner cannot take is Invalid.
func TestTheOwnerSealsWhatItAdmits(t *testing.T) {
	f := newLocal(t)
	got, e := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Shard: 2, Estimate: 40, Stream: true, Boot: []byte("boot")})
	if lease, err := store.LeaseOfAuthorization(e.Auth); err != nil || lease != e.Lease {
		t.Fatalf("the authorization %q of lease %q: %v", e.Auth, e.Lease, err)
	}
	want := Envelope{Auth: e.Auth, Workspace: "ws-1", Lease: e.Lease, Owner: "node-a", Estimate: 40, Stream: true,
		EndOfLife: start.Add(time.Hour)}
	if e != want || !got.EndOfLife.Equal(want.EndOfLife) {
		t.Fatalf("the envelope %+v, want %+v; answered %v", e, want, got.EndOfLife)
	}
	if l, ok := f.owner.Lease(e.Lease); !ok || l.Books().Held != 40 {
		t.Fatalf("the hold under its lease: %v", ok)
	}
	if g, ok := f.grants.granted(e.Lease); !ok || g.Workspace != "ws-1" || g.WorkspaceShard != 2 || g.Region != "us-central1" {
		t.Fatalf("the lease's grant: %+v %v", g, ok)
	}
	_, other := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Shard: 0, Estimate: 40, Boot: []byte("boot")})
	if g, ok := f.grants.granted(other.Lease); other.Lease == e.Lease || !ok || g.WorkspaceShard != 0 {
		t.Fatalf("shard 0's lease %s, granted %+v; shard 2's %s", other.Lease, g, e.Lease)
	}
	for _, bad := range []OwnerAuthorize{{Workspace: "ws-1", Estimate: -1, Boot: []byte("boot")},
		{Workspace: "ws-1", Estimate: 1}} {
		if got := f.local.Authorize(bad); got.Status != Invalid {
			t.Fatalf("%+v: %+v", bad, got)
		}
	}
}

// TestALeaseTheOwnerDoesNotHoldIsPastItsCutoff: a request naming a lease
// the owner does not hold is answered as one past its cutoff (§4.3).
func TestALeaseTheOwnerDoesNotHoldIsPastItsCutoff(t *testing.T) {
	f := newLocal(t)
	ctx := context.Background()
	if got := f.local.Terminal(ctx, OwnerTerminal{Lease: "another", Auth: "a", Kind: record.Refund}); got.Status != PastCutoff {
		t.Fatalf("a terminal: %+v", got)
	}
	if got := f.local.Heartbeat(ctx, OwnerHeartbeat{Lease: "another", Auth: "a", GatewaySeq: 1}); got.Status != Retry {
		t.Fatalf("a heartbeat: %+v", got)
	}
}

// hash is a heartbeat snapshot's hash the tests name by s.
func hash(s string) []byte { return digestOf([]byte(s)) }

// TestTheOwnersAnswersToATerminal: the winner, once acknowledged, answers a
// terminal and every later one; a hold the lease never had is Invalid; past
// the cutoff the answer is PastCutoff.
func TestTheOwnersAnswersToATerminal(t *testing.T) {
	f := newLocal(t)
	ctx := context.Background()
	_, e := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Boot: []byte("boot")})
	settled := OwnerTerminal{Lease: e.Lease, Auth: e.Auth, Kind: record.Settle, Charge: 30, Digest: hash("full")}
	if got := f.local.Terminal(ctx, settled); got != (OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 30}) {
		t.Fatalf("a settle: %+v", got)
	}
	refund := OwnerTerminal{Lease: e.Lease, Auth: e.Auth, Kind: record.Refund}
	if got := f.local.Terminal(ctx, refund); got != (OwnerTerminalAnswer{Status: Won, Kind: record.Settle, Charge: 30}) {
		t.Fatalf("a refund after the settle: %+v", got)
	}
	never, err := store.NewAuthorizationID(e.Lease)
	if err != nil {
		t.Fatal(err)
	}
	if got := f.local.Terminal(ctx, OwnerTerminal{Lease: e.Lease, Auth: never, Kind: record.Refund}); got.Status != Invalid {
		t.Fatalf("a hold the lease never had: %+v", got)
	}
	if got := f.local.Terminal(ctx, OwnerTerminal{Lease: e.Lease, Auth: e.Auth, Kind: record.Reap}); got.Status != Invalid {
		t.Fatalf("a reap from a front door: %+v", got)
	}
	_, open := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Boot: []byte("boot")})
	for name, bad := range map[string]OwnerTerminal{
		"a short digest":    {Lease: open.Lease, Auth: open.Auth, Kind: record.Settle, Charge: 1, Digest: []byte("short")},
		"a negative charge": {Lease: open.Lease, Auth: open.Auth, Kind: record.Settle, Charge: -1, Digest: hash("full")},
	} {
		if got := f.local.Terminal(ctx, bad); got.Status != Invalid {
			t.Fatalf("%s: %+v", name, got)
		}
	}
	_, other := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Boot: []byte("boot")})
	f.clock.advance(59 * time.Second)
	if got := f.local.Terminal(ctx, OwnerTerminal{Lease: other.Lease, Auth: other.Auth, Kind: record.Refund}); got.Status != PastCutoff {
		t.Fatalf("a refund past the cutoff: %+v", got)
	}
}

// TestAWinnerAcknowledgedPastTheCutoffIsRecorded: a terminal whose record
// the log acknowledges after the owner's cutoff is Recorded, and the
// lease's order decides it (§4.5).
func TestAWinnerAcknowledgedPastTheCutoffIsRecorded(t *testing.T) {
	f := newLocal(t)
	ctx := context.Background()
	_, e := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Boot: []byte("boot")})
	n := f.log.published(e.Lease)
	f.log.mu.Lock()
	f.log.hold = make(chan struct{})
	f.log.mu.Unlock()
	answer := make(chan OwnerTerminalAnswer, 1)
	go func() {
		answer <- f.local.Terminal(ctx, OwnerTerminal{Lease: e.Lease, Auth: e.Auth, Kind: record.Refund})
	}()
	for f.log.published(e.Lease) == n {
		time.Sleep(time.Millisecond)
	}
	f.clock.advance(59 * time.Second)
	close(f.log.hold)
	if got := <-answer; got.Status != Recorded {
		t.Fatalf("a refund acknowledged past the cutoff: %+v", got)
	}
}

// TestTheOwnersAnswersToAHeartbeat: today's heartbeat answers (§4.5), and
// Retry past the cutoff.
func TestTheOwnersAnswersToAHeartbeat(t *testing.T) {
	f := newLocal(t)
	ctx := context.Background()
	_, e := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Stream: true, Boot: []byte("boot")})
	first := OwnerHeartbeat{Lease: e.Lease, Auth: e.Auth, GatewaySeq: 2, Hash: hash("h2"), Usage: 5, Running: 10,
		Basis: []byte("terms")}
	got := f.local.Heartbeat(ctx, first)
	if got.Status != Accepted || !got.Deadline.Equal(start.Add(30*time.Second)) {
		t.Fatalf("a first heartbeat: %+v", got)
	}
	for _, c := range []struct {
		name string
		hb   OwnerHeartbeat
		want Status
	}{
		{"an earlier sequence", OwnerHeartbeat{Lease: e.Lease, Auth: e.Auth, GatewaySeq: 1, Hash: hash("h1"), Usage: 5,
			Running: 10, Echoed: got.Deadline}, Stale},
		{"a running charge past the hold", OwnerHeartbeat{Lease: e.Lease, Auth: e.Auth, GatewaySeq: 3, Hash: hash("h3"),
			Usage: 5, Running: 41, Echoed: got.Deadline}, Rejected},
	} {
		if got := f.local.Heartbeat(ctx, c.hb); got.Status != c.want {
			t.Fatalf("%s: %+v, want %s", c.name, got, c.want)
		}
	}
	_, plain := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Boot: []byte("boot")})
	if got := f.local.Heartbeat(ctx, OwnerHeartbeat{Lease: plain.Lease, Auth: plain.Auth, GatewaySeq: 1, Hash: hash("p"),
		Basis: []byte("terms")}); got.Status != Rejected {
		t.Fatalf("a heartbeat of a request that does not stream: %+v", got)
	}
	if got := f.local.Terminal(ctx, OwnerTerminal{Lease: e.Lease, Auth: e.Auth, Kind: record.Refund}); got.Status != Won {
		t.Fatalf("the stream's refund: %+v", got)
	}
	if got := f.local.Heartbeat(ctx, OwnerHeartbeat{Lease: e.Lease, Auth: e.Auth, GatewaySeq: 4, Hash: hash("h4"), Usage: 6,
		Running: 11, Echoed: start.Add(30 * time.Second)}); got.Status != Decided {
		t.Fatalf("a heartbeat after the refund: %+v", got)
	}
	_, s := f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 40, Stream: true, Boot: []byte("boot")})
	f.clock.advance(59 * time.Second)
	if got := f.local.Heartbeat(ctx, OwnerHeartbeat{Lease: s.Lease, Auth: s.Auth, GatewaySeq: 1, Hash: hash("s1"),
		Basis: []byte("terms")}); got.Status != Retry {
		t.Fatalf("a heartbeat past the cutoff: %+v", got)
	}
}

// TestDirectReachesItsOwners: an address with no owner in the process is
// not reached, nor is any once the request has ended, nor one whose answer
// takes longer than the request waits.
func TestDirectReachesItsOwners(t *testing.T) {
	f := newLocal(t)
	d := Direct{"node-a": f.local}
	f.admit(t, OwnerAuthorize{Workspace: "ws-1", Estimate: 1, Boot: []byte("boot")})
	f.mu.Lock()
	f.minting = make(chan struct{})
	f.mu.Unlock()
	wctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	began := time.Now()
	_, err := d.Authorize(wctx, "node-a", OwnerAuthorize{Workspace: "ws-1", Estimate: 1, Boot: []byte("boot")})
	cancel()
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(began) > 5*time.Second {
		t.Fatalf("an admission held past the wait: %v after %v", err, time.Since(began))
	}
	close(f.minting)
	// The admission the wait gave up on lands now: two holds of 1.
	for deadline := time.Now().Add(5 * time.Second); f.held(t) != 2; time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("the admission past the wait never landed: %d held", f.held(t))
		}
	}
	if _, err := d.Terminal(context.Background(), "node-b", OwnerTerminal{}); !errors.Is(err, ErrUnreachable) {
		t.Fatalf("another node: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := d.Heartbeat(ctx, "node-a", OwnerHeartbeat{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("an ended request: %v", err)
	}
	// An ended request starts nothing at the owner: no hold is admitted.
	before := f.held(t)
	if _, err := d.Authorize(ctx, "node-a", OwnerAuthorize{Workspace: "ws-1", Estimate: 7, Boot: []byte("boot")}); !errors.Is(err, context.Canceled) {
		t.Fatalf("an ended authorize: %v", err)
	}
	time.Sleep(50 * time.Millisecond)
	if after := f.held(t); after != before {
		t.Fatalf("an ended authorize held %d, before %d", after, before)
	}
	if got, err := d.Terminal(context.Background(), "node-a", OwnerTerminal{Lease: "another", Kind: record.Refund}); err != nil ||
		got.Status != PastCutoff {
		t.Fatalf("node-a's owner: %+v %v", got, err)
	}
}
