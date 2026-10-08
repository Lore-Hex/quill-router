package frontdoor

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// node is a node on the network: an httptest server at its address, with
// the owner's part if it has an owner and a front door.
type node struct {
	srv   *httptest.Server
	addr  string
	local *Local
	door  *FrontDoor
	store *fakeStore
}

// newNode starts a node. With an owner, its owner admits under leases its
// shards' top-ups are granted; its front door reaches owners through
// client, the node itself its one member, and peers through peers.
func newNode(t *testing.T, withOwner bool, client *http.Client, peers Peers) *node {
	t.Helper()
	n := &node{srv: httptest.NewUnstartedServer(nil)}
	n.addr = n.srv.Listener.Addr().String()
	if withOwner {
		c := &clock{now: start}
		o, err := owner.New(ownerConfig(c, &fakeGrants{expiry: start.Add(time.Minute)}),
			&fakeLog{records: map[string][][]byte{}})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(o.Stop)
		if n.local, err = NewLocal(o, n.addr, "us-central1", key); err != nil {
			t.Fatal(err)
		}
	}
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	ev := &events{}
	n.store = &fakeStore{ev: ev}
	door, err := New(Config{Owners: HTTPOwners{Client: client, Scheme: "http"}, Store: n.store,
		Records: &fakeRecords{ev: ev}, Members: fakeMembers{owners(n.addr)}, Key: key,
		Shards: func(string) int64 { return 1 }, OwnerWait: 2 * time.Second, PublishWait: time.Second, Self: n.addr,
		Peers: peers, PeerWait: time.Second})
	if err != nil {
		t.Fatal(err)
	}
	n.door = door
	n.srv.Config.Handler = Handler(n.door, n.local)
	n.srv.Start()
	t.Cleanup(n.srv.Close)
	return n
}

// TestANodeServesItsGatewaysOverTheNetwork: a gateway's authorize,
// heartbeat, settle and refund, through the node's front door to its owner,
// each over the network.
func TestANodeServesItsGatewaysOverTheNetwork(t *testing.T) {
	ctx := context.Background()
	n := newNode(t, true, nil, nil)
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: n.srv.URL}
	admit := func(stream bool) Authorized {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(time.Millisecond) {
			got, err := gw.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 40, Stream: stream,
				Boot: []byte("boot")})
			if err != nil {
				t.Fatal(err)
			}
			if got.Status != Busy {
				return got
			}
		}
		t.Fatal("no lease came to admit the request")
		return Authorized{}
	}
	got := admit(true)
	if got.Status != Admitted || !got.EndOfLife.Equal(start.Add(time.Hour)) {
		t.Fatalf("the authorize: %+v", got)
	}
	e, err := Open(key, got.Envelope)
	if err != nil || e.Owner != n.addr {
		t.Fatalf("the envelope %+v %v, the node at %s", e, err, n.addr)
	}
	hb, err := gw.Heartbeat(ctx, HeartbeatOf{Envelope: got.Envelope, GatewaySeq: 1, Hash: hash("h1"), Usage: 2,
		Running: 3, Basis: []byte("terms")})
	if err != nil || hb.Status != Accepted {
		t.Fatalf("the heartbeat: %+v %v", hb, err)
	}
	won, err := gw.Settle(ctx, settle(got.Envelope))
	if err != nil || won != (TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}) {
		t.Fatalf("the settle: %+v %v", won, err)
	}
	other := admit(false)
	refunded, err := gw.Refund(ctx, RefundOf{Envelope: other.Envelope, Money: []byte("{}")})
	if err != nil || refunded != (TerminalAnswer{Status: Won, Kind: record.Refund}) {
		t.Fatalf("the refund: %+v %v", refunded, err)
	}
	if err := (HTTPOwners{Client: http.DefaultClient, Scheme: "http"}).Ping(ctx, n.addr); err != nil {
		t.Fatalf("a ping: %v", err)
	}
}

// TestAnOwnerNotReachedOverTheNetwork: no answer, an answer other than a
// 200 though its body decodes, or one that is not JSON, is an owner not
// reached.
func TestAnOwnerNotReachedOverTheNetwork(t *testing.T) {
	ctx := context.Background()
	h := HTTPOwners{Client: &http.Client{Timeout: 2 * time.Second}, Scheme: "http"}
	closed := httptest.NewServer(http.NotFoundHandler())
	gone := closed.Listener.Addr().String()
	closed.Close()
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("{}"))
	}))
	defer failing.Close()
	garbled := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer garbled.Close()
	for _, addr := range []string{gone, failing.Listener.Addr().String(), garbled.Listener.Addr().String()} {
		if _, err := h.Authorize(ctx, addr, OwnerAuthorize{}); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("an authorize to %s: %v", addr, err)
		}
		if _, err := h.Heartbeat(ctx, addr, OwnerHeartbeat{}); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("a heartbeat to %s: %v", addr, err)
		}
		if _, err := h.Terminal(ctx, addr, OwnerTerminal{}); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("a terminal to %s: %v", addr, err)
		}
		if err := h.Ping(ctx, addr); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("a ping to %s: %v", addr, err)
		}
	}
	ended, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := h.Terminal(ended, garbled.Listener.Addr().String(), OwnerTerminal{}); err == nil {
		t.Fatal("a call whose context had ended answered")
	}
}

// refusing is a transport that cannot reach one address.
type refusing struct{ addr string }

func (r refusing) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.Host == r.addr {
		return nil, fmt.Errorf("%s is cut off", r.addr)
	}
	return http.DefaultTransport.RoundTrip(req)
}

// TestAPeerRelaysOverTheNetwork: a front door cut off from an owner sends
// its terminal through a peer that reaches it, over the network; when the
// peer cannot reach the owner either, the terminal goes to the drain log.
func TestAPeerRelaysOverTheNetwork(t *testing.T) {
	ctx := context.Background()
	b := newNode(t, true, nil, nil)
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: b.srv.URL}
	var got Authorized
	for deadline := time.Now().Add(5 * time.Second); got.Status != Admitted && time.Now().Before(deadline); time.Sleep(time.Millisecond) {
		var err error
		if got, err = gw.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 40, Boot: []byte("boot")}); err != nil {
			t.Fatal(err)
		}
	}
	if got.Status != Admitted {
		t.Fatalf("the authorize at b: %+v", got)
	}
	c := newNode(t, false, nil, nil)
	cutOff := &http.Client{Timeout: 2 * time.Second, Transport: refusing{b.addr}}
	x := newNode(t, false, cutOff, HTTPPeers{Client: &http.Client{Timeout: 5 * time.Second}, Scheme: "http"})
	x.door.cfg.Members = fakeMembers{owners(x.addr, b.addr, c.addr)}
	if won := x.door.Settle(ctx, settle(got.Envelope)); won != (TerminalAnswer{Status: Won, Kind: record.Settle, Charge: 55}) {
		t.Fatalf("a settle relayed through c: %+v", won)
	}
	if len(x.store.appended) != 0 {
		t.Fatalf("appended %+v", x.store.appended)
	}
	c.door.cfg.Owners = HTTPOwners{Client: cutOff, Scheme: "http"}
	if ans, err := (HTTPPeers{Client: http.DefaultClient, Scheme: "http"}).Terminal(ctx, c.addr, b.addr,
		OwnerTerminal{Lease: "l", Auth: "a", Kind: record.Refund}); !errors.Is(err, ErrUnreachable) {
		t.Fatalf("a relay whose peer cannot reach the owner: %+v %v", ans, err)
	}
	if rec := x.door.Refund(ctx, RefundOf{Envelope: got.Envelope, Money: []byte("{}")}); rec.Status != Recorded ||
		len(x.store.appended) != 1 {
		t.Fatalf("a refund no one delivers: %+v, appended %+v", rec, x.store.appended)
	}
}

// TestTheHandlerTakesOnlyItsJSON: a request that is not one JSON value of
// its kind is refused, and a node without an owner serves no owner's part.
func TestTheHandlerTakesOnlyItsJSON(t *testing.T) {
	n := newNode(t, false, nil, nil)
	for _, c := range []struct {
		method, path, body string
		want               int
	}{
		{"POST", "/v1/refund", `{"Envelope":"x","Money":"e30=","Extra":1}`, http.StatusBadRequest},
		{"POST", "/v1/refund", `{"Envelope":"x","Money":"e30="} {}`, http.StatusBadRequest},
		{"POST", "/v1/refund", `{"Envelope":` + strings.Repeat(" ", maxBody) + `"x"}`, http.StatusBadRequest},
		{"GET", "/v1/refund", ``, http.StatusMethodNotAllowed},
		{"GET", "/owner/ping", ``, http.StatusNotFound},
		{"POST", "/v1/refund", `{"Envelope":"x","Money":"e30="}`, http.StatusOK},
	} {
		req, err := http.NewRequest(c.method, n.srv.URL+c.path, strings.NewReader(c.body))
		if err != nil {
			t.Fatal(err)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != c.want {
			t.Fatalf("%s %s %.40q: %d, want %d", c.method, c.path, c.body, resp.StatusCode, c.want)
		}
	}
}
