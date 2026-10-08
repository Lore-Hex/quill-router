package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	"cloud.google.com/go/spanner"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// The service's tests run on the Spanner emulator, as the store's do
// (fastpath/README.md), and on Pub/Sub's test server; without the emulator
// they skip.
var (
	emulator *storetest.Emulator
	skipped  string
	shared   *spanner.Client
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	var err error
	if emulator, skipped, err = storetest.Start(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "service tests:", err)
		os.Exit(1)
	}
	if emulator != nil {
		if shared, err = emulator.Database(ctx, "spike", nil); err != nil {
			fmt.Fprintln(os.Stderr, "service tests:", err)
			_ = emulator.Close(ctx)
			os.Exit(1)
		}
	}
	code := m.Run()
	if emulator != nil {
		shared.Close()
		if err := emulator.Close(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "service tests: deleting the emulator's instance:", err)
			code = 1
		}
	}
	os.Exit(code)
}

var key = bytes.Repeat([]byte("k"), frontdoor.MinKeySize)

// pubSub is Pub/Sub's test server with the spike's topics and the
// auditor's subscriptions, the settle log's ordered by key.
func pubSub(t *testing.T) *pubsub.Client {
	t.Helper()
	client, _ := fakePubSub(t)
	return client
}

// fakePubSub is pubSub with its server.
func fakePubSub(t *testing.T) (*pubsub.Client, *pstest.Server) {
	t.Helper()
	ctx := context.Background()
	srv := pstest.NewServer()
	t.Cleanup(func() { _ = srv.Close() })
	conn, err := grpc.NewClient(srv.Addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client, err := pubsub.NewClient(ctx, "spike", option.WithGRPCConn(conn))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	for _, x := range []struct {
		topic, sub string
		ordered    bool
	}{{"settle-log", "auditor", true}, {"records", "stager", false}} {
		topic := "projects/spike/topics/" + x.topic
		if _, err := client.TopicAdminClient.CreateTopic(ctx, &pubsubpb.Topic{Name: topic}); err != nil {
			t.Fatal(err)
		}
		if _, err := client.SubscriptionAdminClient.CreateSubscription(ctx, &pubsubpb.Subscription{
			Name: "projects/spike/subscriptions/" + x.sub, Topic: topic, EnableMessageOrdering: x.ordered,
			AckDeadlineSeconds: 10}); err != nil {
			t.Fatal(err)
		}
	}
	return client, srv
}

// workspace seeds a workspace with credit, at the trust tier leases need.
func workspace(t *testing.T, credits int64) string {
	t.Helper()
	ws := storetest.UniqueID("ws")
	if _, err := shared.Apply(context.Background(), []*spanner.Mutation{spanner.InsertMap("tr_credit_balance",
		map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": credits, "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	return ws
}

// config is a process at ln that runs both roles, on the test's topics.
func config(ln net.Listener) Config {
	cfg := Defaults()
	cfg.Admission, cfg.Auditor = true, true
	cfg.Address, cfg.Listener, cfg.Region, cfg.Key = ln.Addr().String(), ln, "us-central1", key
	cfg.SettleTopic, cfg.RecordTopic = "projects/spike/topics/settle-log", "projects/spike/topics/records"
	cfg.SettleSubscription, cfg.RecordSubscription = "projects/spike/subscriptions/auditor",
		"projects/spike/subscriptions/stager"
	cfg.Store.RequiredTier = 3
	return cfg
}

// start runs a process until stop is called or the test ends, and fails
// the test if it ends with an error.
func start(t *testing.T, cfg Config, c Clients) (stop func()) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- Run(ctx, cfg, c) }()
	var once sync.Once
	stop = func() {
		once.Do(func() {
			cancel()
			select {
			case err := <-done:
				if err != nil {
					t.Errorf("the process ended with %v", err)
				}
			case <-time.After(30 * time.Second):
				t.Error("the process did not stop")
			}
		})
	}
	t.Cleanup(stop)
	return stop
}

// eventually retries f until it reports done, or fails the test after
// within.
func eventually(t *testing.T, within time.Duration, what string, f func() (bool, error)) {
	t.Helper()
	deadline := time.Now().Add(within)
	for {
		done, err := f()
		if err != nil {
			t.Fatalf("%s: %v", what, err)
		}
		if done {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s: not within %v", what, within)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// TestARequestIsAdmittedSettledAndBooked: one process with both roles, its
// gateways' requests over HTTP. An authorize is admitted under a lease its
// owner's top-up was granted; its settle is won, its full record on the
// record topic first; the auditor applies the settle from the settle log and
// commits it as the authorization's winner, charged; and the pending work
// writes its records from the staged full record and marks the pack done.
func TestARequestIsAdmittedSettledAndBooked(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ws := workspace(t, 100_000)
	start(t, config(ln), Clients{Spanner: shared, PubSub: pubSub(t)})

	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	var got frontdoor.Authorized
	eventually(t, 20*time.Second, "an admission", func() (bool, error) {
		var err error
		got, err = gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40,
			Boot: []byte("boot")})
		return err == nil && got.Status == frontdoor.Admitted, nil
	})
	e, err := frontdoor.Open(key, got.Envelope)
	if err != nil {
		t.Fatal(err)
	}
	settled, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: got.Envelope, Charge: 55,
		Full: []byte(`{"request":"r1","boot":"boot","charge":55}`), Money: []byte(`{"cost":55}`)})
	if err != nil || settled.Status != frontdoor.Won || settled.Charge != 55 {
		t.Fatalf("the settle: %+v %v", settled, err)
	}

	s, err := store.New(shared, config(ln).Store)
	if err != nil {
		t.Fatal(err)
	}
	eventually(t, 30*time.Second, "the settle booked", func() (bool, error) {
		d, err := s.Disposition(ctx, e.Auth)
		return err == nil && d.Outcome == "settled" && d.Cost.Int64 == 55, err
	})
	ref := store.LeaseRef{Workspace: ws, LeaseID: e.Lease}
	eventually(t, 30*time.Second, "the settle's work done", func() (bool, error) {
		packs, _, err := s.LoadWinners(ctx, ref)
		for _, p := range packs {
			for _, w := range p.Winners {
				if w.AuthorizationID == e.Auth {
					return p.WorkDoneAt.Valid, err
				}
			}
		}
		return false, err
	})
	lease, _, err := s.ReadLease(ctx, ref)
	if err != nil || lease.Consumed != 55 {
		t.Fatalf("the lease: %+v %v", lease, err)
	}
}

// TestADeclaredStreamWithNoHeartbeatIsReleased: a stream whose boot declares
// the heartbeat at stream open, admitted through a gateway's authorize and
// sent no heartbeat, is released by its owner once the default
// first-heartbeat allowance and the grace have passed, and not before, as
// the release record's publish time on the settle log shows; and the
// auditor books the release, uncharged.
func TestADeclaredStreamWithNoHeartbeatIsReleased(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ws := workspace(t, 100_000)
	cfg := config(ln)
	// The least grace the store takes with the default publish deadline and
	// skew, and reap rounds every second, so the release comes soon after
	// the default allowance.
	cfg.Store.Grace, cfg.Owner.RenewEvery = 10*time.Second, time.Second
	ps, srv := fakePubSub(t)
	start(t, cfg, Clients{Spanner: shared, PubSub: ps})

	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	var got frontdoor.Authorized
	// asked is when the authorize that was admitted was sent: no later than
	// its admission.
	var asked time.Time
	eventually(t, 20*time.Second, "an admission", func() (bool, error) {
		var err error
		asked = time.Now()
		got, err = gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40, Stream: true,
			Boot: []byte("boot"), OpenHeartbeat: true})
		return err == nil && got.Status == frontdoor.Admitted, nil
	})
	e, err := frontdoor.Open(key, got.Envelope)
	if err != nil {
		t.Fatal(err)
	}
	s, err := store.New(shared, cfg.Store)
	if err != nil {
		t.Fatal(err)
	}
	eventually(t, time.Minute, "the release booked", func() (bool, error) {
		d, err := s.Disposition(ctx, e.Auth)
		return err == nil && d.Outcome == "released" && d.Cost.Valid && d.Cost.Int64 == 0, err
	})
	var released []time.Time
	for _, m := range srv.Messages() {
		if r, err := record.Decode(m.Data); err == nil && r.Kind == record.Release && r.Auth == e.Auth {
			released = append(released, m.PublishTime)
		}
	}
	if len(released) != 1 {
		t.Fatalf("%d release records of %s on the settle log", len(released), e.Auth)
	}
	if took, least := released[0].Sub(asked), Defaults().Owner.FirstHeartbeat+cfg.Store.Grace; took < least {
		t.Fatalf("released %v after its admission, before its allowance and the grace, %v", took, least)
	}
}

// TestARequestFindsItsOwnerOnAnotherNode: two admission nodes and an
// auditor member, each a process of its own. A request that comes to one
// node for a workspace whose shard the other owns is admitted by the other's
// owner, over the network, and so is its settle; the auditor books it.
func TestARequestFindsItsOwnerOnAnotherNode(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ps := pubSub(t)
	// The nodes have a database of their own, so their ring holds them
	// alone, not other tests' nodes' rows, live for a while after they stop.
	db, err := emulator.Database(ctx, storetest.UniqueID("route"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	listen := func() net.Listener {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		return ln
	}
	a, b := listen(), listen()
	// A workspace whose one shard the second node owns, of the two.
	var ws string
	for {
		ws = storetest.UniqueID("ws")
		key := ring.ShardKey(ws, 0)
		if ring.Score(b.Addr().String(), key) > ring.Score(a.Addr().String(), key) {
			break
		}
	}
	if _, err := db.Apply(ctx, []*spanner.Mutation{spanner.InsertMap("tr_credit_balance", map[string]any{
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	for _, ln := range []net.Listener{a, b} {
		cfg := config(ln)
		cfg.Auditor = false
		start(t, cfg, Clients{Spanner: db, PubSub: ps})
	}
	member := config(a)
	member.Admission, member.Address, member.Listener = false, "", nil
	start(t, member, Clients{Spanner: db, PubSub: ps})

	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + a.Addr().String()}
	// Until the first node's view has the second, it owns the shard itself.
	var e frontdoor.Envelope
	eventually(t, 20*time.Second, "an admission by the other node's owner", func() (bool, error) {
		got, err := gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40,
			Boot: []byte("boot")})
		if err != nil || got.Status != frontdoor.Admitted {
			return false, nil
		}
		e, err = frontdoor.Open(key, got.Envelope)
		return err == nil && e.Owner == b.Addr().String(), err
	})
	sealed, err := frontdoor.Seal(key, e)
	if err != nil {
		t.Fatal(err)
	}
	settled, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: sealed, Charge: 30,
		Full: []byte(`{"request":"r1","boot":"boot","charge":30}`), Money: []byte(`{"cost":30}`)})
	if err != nil || settled.Status != frontdoor.Won || settled.Charge != 30 {
		t.Fatalf("the settle through the other node: %+v %v", settled, err)
	}
	s, err := store.New(db, config(a).Store)
	if err != nil {
		t.Fatal(err)
	}
	eventually(t, 30*time.Second, "the settle booked", func() (bool, error) {
		d, err := s.Disposition(ctx, e.Auth)
		return err == nil && d.Outcome == "settled" && d.Cost.Int64 == 30, err
	})
}

// short is a configuration whose leases expire and drain within seconds.
func short(cfg Config) Config {
	cfg.Store.Window, cfg.Store.Skew, cfg.Store.PublishDeadline = 2*time.Second, 200*time.Millisecond,
		500*time.Millisecond
	cfg.Store.MaxLife, cfg.Store.Grace = 3*time.Second, 1500*time.Millisecond
	cfg.Owner.RenewEvery, cfg.Owner.HeartbeatEvery, cfg.Owner.AnswerWait = 500*time.Millisecond, time.Second,
		2*time.Second
	cfg.FrontDoor.OwnerWait = 500 * time.Millisecond
	cfg.Runtime.CommitEvery, cfg.Ticker.Every, cfg.Pending.Every = 200*time.Millisecond, 200*time.Millisecond,
		time.Second
	return cfg
}

// TestAStoppedOwnersLeaseDrains: a request admitted by one node's owner,
// which then stops with no time to hand its leases off, as a killed owner
// does. Its settle, coming to the other node, finds the owner gone and goes
// to the lease's drain log; the lease, no longer renewed,
// expires, and the auditor marks it draining, ticks it, books the drain log
// and closes it once its unlisted holds' life is past; the settle is the
// authorization's winner (spike plan §5, K1).
func TestAStoppedOwnersLeaseDrains(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ps := pubSub(t)
	listen := func() net.Listener {
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		return ln
	}
	a, b := listen(), listen()
	var ws string
	for {
		ws = storetest.UniqueID("ws")
		key := ring.ShardKey(ws, 0)
		if ring.Score(b.Addr().String(), key) > ring.Score(a.Addr().String(), key) {
			break
		}
	}
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.InsertMap("tr_credit_balance", map[string]any{
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	stops := map[net.Listener]func(){}
	for _, ln := range []net.Listener{a, b} {
		cfg := short(config(ln))
		cfg.Auditor, cfg.HandOff = false, 0
		stops[ln] = start(t, cfg, Clients{Spanner: shared, PubSub: ps})
	}
	member := short(config(a))
	member.Admission, member.Address, member.Listener = false, "", nil
	start(t, member, Clients{Spanner: shared, PubSub: ps})

	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + a.Addr().String()}
	var got frontdoor.Authorized
	var e frontdoor.Envelope
	eventually(t, 20*time.Second, "an admission by the other node's owner", func() (bool, error) {
		var err error
		got, err = gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40,
			Boot: []byte("boot")})
		if err != nil || got.Status != frontdoor.Admitted {
			return false, nil
		}
		e, err = frontdoor.Open(key, got.Envelope)
		return err == nil && e.Owner == b.Addr().String(), err
	})
	stops[b]()
	settled, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: got.Envelope, Charge: 30,
		Full: []byte(`{"request":"r1","boot":"boot","charge":30}`), Money: []byte(`{"cost":30}`)})
	if err != nil || settled.Status != frontdoor.Recorded {
		t.Fatalf("the settle with its owner gone: %+v %v", settled, err)
	}
	s, err := store.New(shared, short(config(a)).Store)
	if err != nil {
		t.Fatal(err)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: e.Lease}
	eventually(t, 60*time.Second, "the lease closed, its settle booked", func() (bool, error) {
		lease, _, err := s.ReadLease(ctx, ref)
		if err != nil || lease.State != "closed" {
			return false, err
		}
		d, err := s.Disposition(ctx, e.Auth)
		return err == nil && d.Outcome == "settled" && d.Cost.Int64 == 30, err
	})
	// No hand-off listed the lease's holds: it closed by time.
	if lease, _, err := s.ReadLease(ctx, ref); err != nil || lease.HoldsListedSeq.Valid {
		t.Fatalf("the lease of an owner gone with no hand-off: %+v %v", lease, err)
	}
}

// twoNodes starts two admission nodes and an auditor member on a database
// of their own, so that their ring holds them alone, and makes a workspace
// whose one shard the second node owns; set changes each node's
// configuration, told whether it is the shard's owner. It returns the
// nodes' listeners, each node's stop, the database and the workspace.
func twoNodes(t *testing.T, name string, set func(owner bool, cfg *Config)) (a, b net.Listener,
	stops map[net.Listener]func(), db *spanner.Client, ws string) {
	t.Helper()
	ctx := context.Background()
	ps := pubSub(t)
	db, err := emulator.Database(ctx, storetest.UniqueID(name), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	a, b = listen(t), listen(t)
	ws = ownedBy(t, db, b, a, b)
	stops = map[net.Listener]func(){}
	for _, ln := range []net.Listener{a, b} {
		cfg := config(ln)
		cfg.Auditor = false
		if set != nil {
			set(ln == b, &cfg)
		}
		stops[ln] = start(t, cfg, Clients{Spanner: db, PubSub: ps})
	}
	member := config(a)
	member.Admission, member.Address, member.Listener = false, "", nil
	start(t, member, Clients{Spanner: db, PubSub: ps})
	return a, b, stops, db, ws
}

// memberState is the state the ring's row for the node at ln says.
func memberState(t *testing.T, s *store.Store, ln net.Listener) string {
	t.Helper()
	members, _, err := s.Members(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range members {
		if m.Address == ln.Addr().String() {
			return m.State
		}
	}
	t.Fatalf("no row for %s", ln.Addr())
	return ""
}

// TestAStoppingNodesOwnerHandsItsLeasesOff: a request admitted by one
// node's owner, which is then stopped with time to hand its leases off
// (spike plan K2). The node is marked leaving, and its owner lists the
// lease's open hold in hand-off records and marks the lease draining
// itself, its expiry still ahead; the hold's settle, coming to the other
// node, goes to the drain log; and the auditor stores the listed hold and
// closes the lease once the settle is booked, long before an unknown hold's
// life would have ended.
func TestAStoppingNodesOwnerHandsItsLeasesOff(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	a, b, stops, db, ws := twoNodes(t, "handoff", nil)
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + a.Addr().String()}
	sealed, e := admitted(t, gw, ws, "r1", b)
	stops[b]()
	s, err := store.New(db, config(a).Store)
	if err != nil {
		t.Fatal(err)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: e.Lease}
	lease, _, err := s.ReadLease(ctx, ref)
	if err != nil || lease.State != "draining" || !lease.Expiry.After(time.Now()) {
		t.Fatalf("the lease once its owner stopped: %+v %v", lease, err)
	}
	if state := memberState(t, s, b); state != store.Leaving {
		t.Fatalf("the stopped node's row says %s", state)
	}
	// The auditor installs the hand-off's hold, open, as listed.
	eventually(t, 30*time.Second, "the handed-off hold stored", func() (bool, error) {
		loaded, err := s.Load(ctx, ref)
		if err != nil || !loaded.Lease.HoldsListedSeq.Valid {
			return false, err
		}
		for _, h := range loaded.Holds {
			if h.AuthorizationID == e.Auth {
				return h.Listed && h.Estimate == 40, nil
			}
		}
		return false, nil
	})
	settled, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: sealed, Charge: 30,
		Full: []byte(`{"request":"r1","boot":"boot","charge":30}`), Money: []byte(`{"cost":30}`)})
	if err != nil || settled.Status != frontdoor.Recorded {
		t.Fatalf("the settle with its owner gone: %+v %v", settled, err)
	}
	eventually(t, 2*time.Minute, "the listed hold stored, the lease closed, its settle booked", func() (bool, error) {
		lease, _, err := s.ReadLease(ctx, ref)
		if err != nil || lease.State != "closed" || !lease.HoldsListedSeq.Valid {
			return false, err
		}
		d, err := s.Disposition(ctx, e.Auth)
		return err == nil && d.Outcome == "settled" && d.Cost.Int64 == 30, err
	})
}

// TestALeavingNodeKeepsItsLeasesAndTakesNoNew: a stream admitted by one
// node's owner, which is then marked leaving (spike plan K3). Its row says
// so; a new request for the workspace is admitted by the other node's
// owner, and one sent straight to the leaving owner, as by a front door
// whose view is old, is Busy; the stream's heartbeat and settle, through
// the other node's front door, are its own owner's still, and taken; and
// once its hold has ended, the leaving owner's lease drains.
func TestALeavingNodeKeepsItsLeasesAndTakesNoNew(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	leave := make(chan struct{})
	a, b, _, db, ws := twoNodes(t, "leaving", func(owner bool, cfg *Config) {
		if owner {
			cfg.Leave = leave
		}
	})
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + a.Addr().String()}
	var stream frontdoor.Authorized
	eventually(t, 20*time.Second, "a stream admitted by the leaving node's owner", func() (bool, error) {
		var err error
		stream, err = gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40,
			Stream: true, Boot: []byte("boot")})
		if err != nil || stream.Status != frontdoor.Admitted {
			return false, nil
		}
		e, err := frontdoor.Open(key, stream.Envelope)
		return err == nil && e.Owner == b.Addr().String(), err
	})
	close(leave)
	s, err := store.New(db, config(a).Store)
	if err != nil {
		t.Fatal(err)
	}
	eventually(t, 10*time.Second, "the leaving node's row", func() (bool, error) {
		return memberState(t, s, b) == store.Leaving, nil
	})
	admitted(t, gw, ws, "r2", a)
	owners := frontdoor.HTTPOwners{Client: &http.Client{Timeout: 10 * time.Second}, Scheme: "http"}
	got, err := owners.Authorize(ctx, b.Addr().String(), frontdoor.OwnerAuthorize{Workspace: ws, Estimate: 40,
		Boot: []byte("boot")})
	if err != nil || got.Status != frontdoor.Busy {
		t.Fatalf("a request straight to the leaving owner: %+v %v", got, err)
	}
	hash := sha256.Sum256([]byte("r1/1"))
	hb, err := gw.Heartbeat(ctx, frontdoor.HeartbeatOf{Envelope: stream.Envelope, GatewaySeq: 1, Hash: hash[:],
		Basis: []byte("terms")})
	if err != nil || hb.Status != frontdoor.Accepted {
		t.Fatalf("the stream's heartbeat: %+v %v", hb, err)
	}
	settled, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: stream.Envelope, Charge: 30,
		Full: []byte(`{"request":"r1","boot":"boot","charge":30}`), Money: []byte(`{"cost":30}`)})
	if err != nil || settled.Status != frontdoor.Won {
		t.Fatalf("the stream's settle: %+v %v", settled, err)
	}
	e, err := frontdoor.Open(key, stream.Envelope)
	if err != nil {
		t.Fatal(err)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: e.Lease}
	eventually(t, 30*time.Second, "the leaving owner's lease drained", func() (bool, error) {
		lease, _, err := s.ReadLease(ctx, ref)
		return err == nil && lease.State != "open", err
	})
}

// TestTheClockOffsetIsTheOwners: an owner whose clock readings are offset
// (spike plan K6), here an hour back, gives a hold its end of life by its
// offset clock: an hour sooner than a true clock would, more than any
// request's latency.
func TestTheClockOffsetIsTheOwners(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln := listen(t)
	ws := workspace(t, 100_000)
	cfg := config(ln)
	cfg.ClockOffset = -time.Hour
	start(t, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	var got frontdoor.Authorized
	var asked, answered time.Time
	eventually(t, 20*time.Second, "an admission", func() (bool, error) {
		var err error
		asked = time.Now()
		got, err = gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: ws, Request: "r1", Estimate: 40,
			Boot: []byte("boot")})
		answered = time.Now()
		return err == nil && got.Status == frontdoor.Admitted, nil
	})
	life := cfg.ClockOffset + cfg.Store.MaxLife
	if lo, hi := asked.Add(life), answered.Add(life); got.EndOfLife.Before(lo) || got.EndOfLife.After(hi) {
		t.Fatalf("an end of life of %v, not between %v and %v", got.EndOfLife, lo, hi)
	}
}

// TestAStartThatBlocksEndsWithItsContext: a process whose start blocks,
// waiting on the parts' context, stops once its own context ends, with no
// leaving, since its parts never started.
func TestAStartThatBlocksEndsWithItsContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	left := false
	done := make(chan error, 1)
	go func() {
		done <- lifecycle(ctx, func(p *parts) {
			p.whenLeaving(func() { left = true })
			<-p.ctx.Done()
			p.startFailed(ctx, p.ctx.Err())
		})
	}()
	select {
	case err := <-done:
		t.Fatalf("the process returned with its start blocked: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	cancel()
	select {
	case err := <-done:
		if err != nil || left {
			t.Fatalf("a process stopped as its start blocked: %v, left %v", err, left)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the process did not stop")
	}
}

// TestAProcessLeavesBeforeItsPartsStop: once a process's parts have
// started, its context's end runs what whenLeaving was given, in order,
// while every part still runs, and then stops them; a part's failure stops
// them with no leaving, and is what the process returns.
func TestAProcessLeavesBeforeItsPartsStop(t *testing.T) {
	var mu sync.Mutex
	var events []string
	note := func(e string) {
		mu.Lock()
		defer mu.Unlock()
		events = append(events, e)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	started := make(chan *parts, 1)
	go func() {
		done <- lifecycle(ctx, func(p *parts) {
			p.run("part", func(ctx context.Context) error {
				<-ctx.Done()
				note("the part stopped")
				return nil
			})
			p.whenLeaving(func() { note("leaving 1") })
			p.whenLeaving(func() { note("leaving 2") })
			started <- p
		})
	}()
	<-(<-started).armed
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if want := []string{"leaving 1", "leaving 2", "the part stopped"}; !reflect.DeepEqual(events, want) {
		t.Fatalf("the process's stop went %q, want %q", events, want)
	}

	events = nil
	failed := errors.New("failed")
	err := lifecycle(context.Background(), func(p *parts) {
		p.run("part", func(ctx context.Context) error { return failed })
		p.whenLeaving(func() { note("leaving") })
	})
	if !errors.Is(err, failed) || len(events) != 0 {
		t.Fatalf("a failed part: %v, and %q", err, events)
	}
}

// TestEveryPartAgreesOnTheStoresTimes: the owner, the front door, the
// auditor and the publishers take the times they must agree on from the
// store's configuration.
func TestEveryPartAgreesOnTheStoresTimes(t *testing.T) {
	cfg := Defaults()
	cfg.Store.Window, cfg.Store.Skew, cfg.Store.MaxLife, cfg.Store.Grace = 11*time.Second, 3*time.Second,
		17*time.Minute, 5*time.Minute
	cfg.Store.PublishDeadline = 7 * time.Second
	if d := cfg.publish().Deadline; d != 7*time.Second {
		t.Fatalf("a publish's deadline %v, and the owner's is 7s", d)
	}
	got := cfg.agreed()
	o, f, r := got.Owner, got.FrontDoor, got.Runtime
	if o.Skew != 3*time.Second || o.Window != 11*time.Second || o.Grace != 5*time.Minute ||
		o.HoldLife != 17*time.Minute || f.HoldLife != 17*time.Minute+11*time.Second || r.Skew != 3*time.Second ||
		r.Grace != 5*time.Minute || r.MaxLife != 17*time.Minute {
		t.Fatalf("the owner's %+v, the front door's hold life %v, the auditor's %+v", o, f.HoldLife, r)
	}
}

// TestANodeWhoseRowIsTakenStops: a node whose ring row another process
// joins under stops, its epoch no longer the row's.
func TestANodeWhoseRowIsTakenStops(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cfg := config(ln)
	cfg.Auditor = false
	run, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(run, cfg, Clients{Spanner: shared, PubSub: pubSub(t)}) }()
	s, err := store.New(shared, cfg.Store)
	if err != nil {
		t.Fatal(err)
	}
	address := ln.Addr().String()
	eventually(t, 10*time.Second, "the node's row", func() (bool, error) {
		members, _, err := s.Members(ctx)
		for _, m := range members {
			if m.Address == address {
				return true, err
			}
		}
		return false, err
	})
	if _, _, err := s.Join(ctx, address, []string{"owner"}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "ring row") {
			t.Fatalf("the node stopped with %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the node whose row was taken kept running")
	}
}

// ownedBy is a new workspace, seeded with credit, whose one shard the node
// at by owns among the nodes at among, by rendezvous hashing.
func ownedBy(t *testing.T, db *spanner.Client, by net.Listener, among ...net.Listener) string {
	t.Helper()
	for {
		ws := storetest.UniqueID("ws")
		key, best := ring.ShardKey(ws, 0), true
		for _, ln := range among {
			if ln != by && ring.Score(ln.Addr().String(), key) >= ring.Score(by.Addr().String(), key) {
				best = false
			}
		}
		if !best {
			continue
		}
		if _, err := db.Apply(context.Background(), []*spanner.Mutation{spanner.InsertMap("tr_credit_balance",
			map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000),
				"trust_tier": int64(3)})}); err != nil {
			t.Fatal(err)
		}
		return ws
	}
}

func listen(t *testing.T) net.Listener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return ln
}

// admitted authorizes request through gw until the owner at owner admits
// it, and returns its envelope, sealed and opened.
func admitted(t *testing.T, gw frontdoor.Gateway, ws, request string, owner net.Listener) (string, frontdoor.Envelope) {
	t.Helper()
	var sealed string
	var e frontdoor.Envelope
	eventually(t, 20*time.Second, "an admission by "+owner.Addr().String(), func() (bool, error) {
		got, err := gw.Authorize(context.Background(), frontdoor.AuthorizeOf{Workspace: ws, Request: request,
			Estimate: 40, Boot: []byte("boot")})
		if err != nil || got.Status != frontdoor.Admitted {
			return false, nil
		}
		sealed = got.Envelope
		e, err = frontdoor.Open(key, sealed)
		return err == nil && e.Owner == owner.Addr().String(), err
	})
	return sealed, e
}

// TestALeaseIsRenewedWhileItsOwnerRuns: the owner renews its lease, every
// RenewEvery, a window ahead, so it stays open past its first expiry.
func TestALeaseIsRenewedWhileItsOwnerRuns(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln := listen(t)
	ws := ownedBy(t, shared, ln)
	cfg := short(config(ln))
	cfg.Auditor = false
	start(t, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	_, e := admitted(t, gw, ws, "r1", ln)
	s, err := store.New(shared, cfg.Store)
	if err != nil {
		t.Fatal(err)
	}
	ref := store.LeaseRef{Workspace: ws, LeaseID: e.Lease}
	first, _, err := s.ReadLease(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	eventually(t, 3*cfg.Store.Window, "the lease renewed past its first expiry", func() (bool, error) {
		l, _, err := s.ReadLease(ctx, ref)
		return err == nil && l.State == "open" && l.Expiry.After(first.Expiry.Add(cfg.Store.Window)), err
	})
}

// TestAnUnreachableOwnersLeaseIsRevoked: three nodes, and a lease whose
// owner stops. Its settles, coming to another node, reach the owner neither
// there nor through the third node, a peer, and go to the drain log; once
// those failures span RevokeAfter, the front door revokes the lease (§4.3).
func TestAnUnreachableOwnersLeaseIsRevoked(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ps := pubSub(t)
	// The three nodes have a database of their own, so their ring holds
	// them alone: the shared one keeps other tests' nodes' rows, live for a
	// while after they stop, which a could pick to relay through.
	db, err := emulator.Database(ctx, storetest.UniqueID("relay"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	// c, the third node, is the peer a sends a terminal through when it
	// cannot reach b: its listener counts the relays it is asked for.
	relays := &sniffed{Listener: listen(t), path: []byte("POST /peer/terminal ")}
	a, b, c := listen(t), listen(t), net.Listener(relays)
	ws := ownedBy(t, db, b, a, b, c)
	stops := map[net.Listener]func(){}
	for _, ln := range []net.Listener{a, b, c} {
		cfg := short(config(ln))
		cfg.Auditor = false
		cfg.FrontDoor.RevokeAfter, cfg.FrontDoor.RevokeEvery = time.Second, 100*time.Millisecond
		stops[ln] = start(t, cfg, Clients{Spanner: db, PubSub: ps})
	}
	s, err := store.New(db, short(config(a)).Store)
	if err != nil {
		t.Fatal(err)
	}
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + a.Addr().String()}
	// a's view holds c before b stops: a admits a workspace c owns at c.
	admitted(t, gw, ownedBy(t, db, c, a, b, c), "rc", c)
	first, e := admitted(t, gw, ws, "r1", b)
	second, _ := admitted(t, gw, ws, "r2", b)
	stops[b]()
	for i, sealed := range []string{first, second} {
		if i > 0 {
			time.Sleep(1200 * time.Millisecond)
		}
		got, err := gw.Settle(ctx, frontdoor.SettleOf{Envelope: sealed, Charge: 30,
			Full: []byte(`{"charge":30}`), Money: []byte(`{"cost":30}`)})
		if err != nil || got.Status != frontdoor.Recorded {
			t.Fatalf("settle %d with its owner gone: %+v %v", i, got, err)
		}
	}
	// Before a recorded its terminals, it tried b through c, which could not
	// reach b either.
	if n := relays.seen.Load(); n == 0 {
		t.Fatal("a recorded its terminals without asking the third node to relay them")
	}
	eventually(t, 10*time.Second, "the lease revoked", func() (bool, error) {
		l, _, err := s.ReadLease(ctx, store.LeaseRef{Workspace: ws, LeaseID: e.Lease})
		return err == nil && l.Revoked, err
	})
}

// TestPubSubIsTheRegions: the client reaches the region's own endpoint, so
// owners and the auditor's ticks are ordered as one, unless an emulator is
// named.
func TestPubSubIsTheRegions(t *testing.T) {
	t.Setenv("PUBSUB_EMULATOR_HOST", "")
	want := []option.ClientOption{option.WithEndpoint("us-central1-pubsub.googleapis.com:443")}
	if got := PubSubOptions("us-central1"); !reflect.DeepEqual(got, want) {
		t.Fatalf("the options %v", got)
	}
	t.Setenv("PUBSUB_EMULATOR_HOST", "127.0.0.1:8085")
	if got := PubSubOptions("us-central1"); got != nil {
		t.Fatalf("with an emulator, the options %v", got)
	}
}

// TestAProcessStoppedAsItStartsStopsCleanly: a process whose context ends
// before its parts have started returns no error.
func TestAProcessStoppedAsItStartsStopsCleanly(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := Run(ctx, config(listen(t)), Clients{Spanner: shared, PubSub: pubSub(t)}); err != nil {
		t.Fatalf("a process stopped as it started: %v", err)
	}
}

// TestAServerThatFailsFailsTheProcess: a node whose listener fails ends
// the process with that failure, kept though the process's context ends as
// the server closes.
func TestAServerThatFailsFailsTheProcess(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// The server closes its listener as it returns: the process's context
	// ends then, before the part that runs the server has returned.
	ln := &watchedListener{Listener: listen(t), closed: cancel}
	done := make(chan error, 1)
	go func() { done <- Run(ctx, config(ln), Clients{Spanner: shared, PubSub: pubSub(t)}) }()
	_ = ln.Listener.Close() // the listener fails
	select {
	case err := <-done:
		if !errors.Is(err, net.ErrClosed) {
			t.Fatalf("a process whose listener failed ended with %v", err)
		}
	case <-time.After(30 * time.Second):
		t.Fatal("the process did not stop")
	}
}

// TestASubscriptionThatFailsFailsTheProcess: a subscription that fails,
// the stager's or the auditor's, fails the process as soon as its Receive
// returns, while a callback of it still runs: the node stops serving, and
// the failure is kept though the process's context ends before the
// callback returns.
func TestASubscriptionThatFailsFailsTheProcess(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	for _, c := range []struct {
		sub, alert string
		send       func(t *testing.T, ps *pubsub.Client, cfg Config)
	}{
		{"stager", "no lease's authorization", func(t *testing.T, ps *pubsub.Client, cfg Config) {
			records, err := settlelog.OpenRecords(ps, cfg.RecordTopic, cfg.publish())
			if err != nil {
				t.Fatal(err)
			}
			defer records.Stop()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if _, err := records.Publish("not-an-authorization", settlelog.FullRecord, []byte("x")).Wait(ctx); err != nil {
				t.Fatal(err)
			}
		}},
		{"auditor", "cannot read", func(t *testing.T, ps *pubsub.Client, cfg Config) {
			log, err := settlelog.OpenLog(ps, cfg.SettleTopic, cfg.publish())
			if err != nil {
				t.Fatal(err)
			}
			defer log.Stop()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			if _, err := log.Publish("no-lease", []byte("not a record"), nil).Wait(ctx); err != nil {
				t.Fatal(err)
			}
		}},
	} {
		t.Run(c.sub, func(t *testing.T) {
			ps, srv := fakePubSub(t)
			// Streams end and open again, so the client finds a deleted
			// subscription gone, which it does not retry.
			srv.SetStreamTimeout(200 * time.Millisecond)
			ln := listen(t)
			cfg := config(ln)
			alerted, release := make(chan struct{}), make(chan struct{})
			letGo := sync.OnceFunc(func() { close(release) })
			t.Cleanup(letGo)
			var once sync.Once
			cfg.Alert = func(subject, what string) {
				if strings.Contains(what, c.alert) {
					once.Do(func() {
						close(alerted)
						<-release
					})
				}
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { done <- Run(ctx, cfg, Clients{Spanner: shared, PubSub: ps}) }()
			c.send(t, ps, cfg)
			select {
			case <-alerted:
			case <-time.After(20 * time.Second):
				t.Fatal("the callback was given nothing")
			}
			if err := ps.SubscriptionAdminClient.DeleteSubscription(context.Background(),
				&pubsubpb.DeleteSubscriptionRequest{Subscription: "projects/spike/subscriptions/" + c.sub}); err != nil {
				t.Fatal(err)
			}
			// Past the client's ten-second shutdown timeout, its Receive
			// returns the failure; the callback still holds.
			eventually(t, 40*time.Second, "the node stopped serving", func() (bool, error) {
				conn, err := net.DialTimeout("tcp", ln.Addr().String(), time.Second)
				if err != nil {
					return true, nil
				}
				_ = conn.Close()
				return false, nil
			})
			cancel()
			letGo()
			select {
			case err := <-done:
				if err == nil || !strings.Contains(err.Error(), "service: "+c.sub) {
					t.Fatalf("a process whose %s subscription failed ended with %v", c.sub, err)
				}
			case <-time.After(30 * time.Second):
				t.Fatal("the process did not stop")
			}
		})
	}
}

// TestANodeThatCannotListenTakesNoRow: a node that cannot listen at its
// address, as when another node serves there, fails before it takes the
// ring's row, so the node that serves keeps its row and runs on.
func TestANodeThatCannotListenTakesNoRow(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln := listen(t)
	cfg := config(ln)
	cfg.Auditor = false
	run, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(run, cfg, Clients{Spanner: shared, PubSub: pubSub(t)}) }()
	s, err := store.New(shared, cfg.Store)
	if err != nil {
		t.Fatal(err)
	}
	address := ln.Addr().String()
	epoch := func() int64 {
		members, _, err := s.Members(ctx)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range members {
			if m.Address == address {
				return m.Epoch
			}
		}
		return 0
	}
	eventually(t, 10*time.Second, "the node's row", func() (bool, error) { return epoch() > 0, nil })
	serving := epoch()

	second := config(ln)
	second.Auditor, second.Listener = false, nil
	if err := Run(ctx, second, Clients{Spanner: shared, PubSub: pubSub(t)}); err == nil {
		t.Fatal("a second node at the same address ran")
	}
	if got := epoch(); got != serving {
		t.Fatalf("the row's epoch went from %d to %d", serving, got)
	}
	// The node that serves runs on past a few of its ring rounds.
	select {
	case err := <-done:
		t.Fatalf("the node that serves stopped: %v", err)
	case <-time.After(3 * cfg.Ring):
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// TestAGivenListenerIsClosed: a listener given to Run is closed however Run
// returns: stopped as it starts, with an owner that cannot start, or with a
// configuration it refuses.
func TestAGivenListenerIsClosed(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	stopped, cancel := context.WithCancel(context.Background())
	cancel()
	for _, c := range []struct {
		name  string
		ctx   context.Context
		set   func(cfg *Config)
		fails bool
	}{
		{"stopped as it starts", stopped, func(*Config) {}, false},
		{"an owner that cannot start", context.Background(), func(cfg *Config) { cfg.Owner.AnswerWait = 0 }, true},
		{"a configuration refused", context.Background(), func(cfg *Config) { cfg.Shards = 0 }, true},
	} {
		closed := make(chan struct{})
		ln := &watchedListener{Listener: listen(t), closed: sync.OnceFunc(func() { close(closed) })}
		cfg := config(ln)
		c.set(&cfg)
		if err := Run(c.ctx, cfg, Clients{Spanner: shared, PubSub: pubSub(t)}); (err != nil) != c.fails {
			t.Fatalf("%s: Run returned %v", c.name, err)
		}
		select {
		case <-closed:
		default:
			t.Fatalf("%s: the listener was left open", c.name)
		}
	}
}

// TestAListenerRunMadeIsClosedOnAFailedStart: a listener Run opened at the
// node's address is closed when a part after it cannot start, so the
// address is free again.
func TestAListenerRunMadeIsClosedOnAFailedStart(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	free := listen(t)
	address := free.Addr().String()
	_ = free.Close()
	cfg := config(free)
	cfg.Listener, cfg.Owner.AnswerWait = nil, 0
	if err := Run(context.Background(), cfg, Clients{Spanner: shared, PubSub: pubSub(t)}); err == nil {
		t.Fatal("a node whose owner cannot start ran")
	}
	ln, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("the address was left taken: %v", err)
	}
	_ = ln.Close()
}

// sniffed is a listener whose connections count the requests they carry
// whose first line begins with path.
type sniffed struct {
	net.Listener
	path []byte
	seen atomic.Int64
}

func (s *sniffed) Accept() (net.Conn, error) {
	c, err := s.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &sniffedConn{Conn: c, s: s}, nil
}

type sniffedConn struct {
	net.Conn
	s *sniffed
	// tail is the last bytes read, shorter than the path, so a path split
	// between two reads is counted once.
	tail []byte
}

func (c *sniffedConn) Read(b []byte) (int, error) {
	n, err := c.Conn.Read(b)
	seen := append(c.tail, b[:n]...)
	c.s.seen.Add(int64(bytes.Count(seen, c.s.path)))
	c.tail = append([]byte(nil), seen[max(0, len(seen)-len(c.s.path)+1):]...)
	return n, err
}

// watchedListener is a listener whose Close calls closed first.
type watchedListener struct {
	net.Listener
	closed func()
}

func (l *watchedListener) Close() error {
	l.closed()
	return l.Listener.Close()
}

// TestAStoppingNodeEndsItsRequests: a request still being read when its
// node stops gets Stopping to finish, then its connection is closed: it is
// not served once the node has stopped.
func TestAStoppingNodeEndsItsRequests(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ln := listen(t)
	cfg := config(ln)
	cfg.Auditor, cfg.Stopping = false, 200*time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, cfg, Clients{Spanner: shared, PubSub: pubSub(t)}) }()
	var conn net.Conn
	eventually(t, 10*time.Second, "the node serving", func() (bool, error) {
		c, err := net.Dial("tcp", ln.Addr().String())
		if err != nil {
			return false, nil
		}
		conn = c
		return true, nil
	})
	defer conn.Close()
	body := `{"workspace":"ws-1","request":"r1","estimate":40,"boot":"Ym9vdA=="}`
	if _, err := fmt.Fprintf(conn, "POST /v1/authorize HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"+
		"Content-Length: %d\r\n\r\n%s", len(body), body[:10]); err != nil {
		t.Fatal(err)
	}
	time.Sleep(50 * time.Millisecond) // the request being read
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the node did not stop")
	}
	_, _ = conn.Write([]byte(body[10:]))
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	answer, _ := io.ReadAll(conn)
	if bytes.Contains(answer, []byte("200 OK")) {
		t.Fatalf("a request was served after its node stopped: %q", answer)
	}
}

// TestAnOwnerCallEndsBeforeItsOwnerStops: a front door's call to its own
// owner can run on after the request that made it has ended; the process
// stops the owner only once that call has returned.
func TestAnOwnerCallEndsBeforeItsOwnerStops(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ln := listen(t)
	ws := ownedBy(t, shared, ln)
	cfg := config(ln)
	cfg.Auditor, cfg.Stopping = false, 100*time.Millisecond
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	cfg.Owner.Overrun = func(e int64) int64 {
		// An admission once its lease is warm: it holds the owner's call.
		once.Do(func() {
			close(entered)
			<-release
		})
		return 0
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, cfg, Clients{Spanner: shared, PubSub: pubSub(t)}) }()
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	go func() {
		for i := 0; ; i++ {
			select {
			case <-entered:
				return
			default:
			}
			_, _ = gw.Authorize(context.Background(), frontdoor.AuthorizeOf{Workspace: ws, Request: fmt.Sprint(i),
				Estimate: 40, Boot: []byte("boot")})
			time.Sleep(20 * time.Millisecond)
		}
	}()
	select {
	case <-entered:
	case <-time.After(20 * time.Second):
		t.Fatal("no admission reached the owner")
	}
	cancel()
	select {
	case err := <-done:
		t.Fatalf("the process stopped, %v, while its owner's call ran", err)
	case <-time.After(500 * time.Millisecond):
	}
	close(release)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the process did not stop")
	}
}

// TestTheStagerWaitsForItsCallbacks: the record topic's subscription can end
// with a callback still under way, after its client's shutdown timeout; the
// process stops only once the callback has returned.
func TestTheStagerWaitsForItsCallbacks(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ps := pubSub(t)
	cfg := config(listen(t))
	cfg.Admission, cfg.Address, cfg.Listener = false, "", nil
	alerted, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	cfg.Alert = func(subject, what string) {
		if strings.Contains(what, "no lease's authorization") {
			once.Do(func() {
				close(alerted)
				<-release
			})
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, cfg, Clients{Spanner: shared, PubSub: ps}) }()
	records, err := settlelog.OpenRecords(ps, cfg.RecordTopic, cfg.publish())
	if err != nil {
		t.Fatal(err)
	}
	defer records.Stop()
	wctx, wcancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer wcancel()
	// A full record of no lease's authorization: the stager tells of it.
	if _, err := records.Publish("not-an-authorization", settlelog.FullRecord, []byte("x")).Wait(wctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-alerted:
	case <-time.After(20 * time.Second):
		t.Fatal("the stager was given nothing")
	}
	cancel()
	// Past the client's ten-second shutdown timeout, the callback still
	// holds.
	select {
	case err := <-done:
		t.Fatalf("the process stopped, %v, while the stager's callback ran", err)
	case <-time.After(12 * time.Second):
	}
	close(release)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the process did not stop")
	}
}
