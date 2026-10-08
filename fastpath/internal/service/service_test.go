package service

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
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
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
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
	return client
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
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.InsertMap("tr_credit_balance", map[string]any{
		"workspace_id": ws, "shard": int64(0), "total_credits": int64(100_000), "trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	for _, ln := range []net.Listener{a, b} {
		cfg := config(ln)
		cfg.Auditor = false
		start(t, cfg, Clients{Spanner: shared, PubSub: ps})
	}
	member := config(a)
	member.Admission, member.Address, member.Listener = false, "", nil
	start(t, member, Clients{Spanner: shared, PubSub: ps})

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
	s, err := store.New(shared, config(a).Store)
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
	cfg.Publish.Deadline = 500 * time.Millisecond
	cfg.Owner.RenewEvery, cfg.Owner.HeartbeatEvery, cfg.Owner.AnswerWait = 500*time.Millisecond, time.Second,
		2*time.Second
	cfg.FrontDoor.OwnerWait = 500 * time.Millisecond
	cfg.Runtime.CommitEvery, cfg.Ticker.Every, cfg.Pending.Every = 200*time.Millisecond, 200*time.Millisecond,
		time.Second
	return cfg
}

// TestAStoppedOwnersLeaseDrains: a request admitted by one node's owner,
// which then stops. Its settle, coming to the other node, finds the owner
// gone and goes to the lease's drain log; the lease, no longer renewed,
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
		cfg.Auditor = false
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
}

// TestEveryPartAgreesOnTheStoresTimes: the owner, the front door and the
// auditor take the times they must agree on from the store's configuration.
func TestEveryPartAgreesOnTheStoresTimes(t *testing.T) {
	cfg := Defaults()
	cfg.Store.Window, cfg.Store.Skew, cfg.Store.MaxLife, cfg.Store.Grace = 11*time.Second, 3*time.Second,
		17*time.Minute, 5*time.Minute
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
