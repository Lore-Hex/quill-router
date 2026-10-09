package frontdoor

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// node is a node on the network: an httptest server at its address, with
// the owner's part if it has an owner and a front door.
type node struct {
	srv    *httptest.Server
	addr   string
	local  *Local
	owner  *owner.Owner
	grants *fakeGrants
	door   *FrontDoor
	store  *fakeStore
}

// held is what the node's owner holds under ws-1's shard 0 leases.
func (n *node) held() int64 {
	var held int64
	for _, g := range n.grants.all() {
		if l, ok := n.owner.Lease(g.LeaseID); ok && g.Workspace == "ws-1" && g.WorkspaceShard == 0 {
			held += l.Books().Held
		}
	}
	return held
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
		n.grants = &fakeGrants{expiry: start.Add(time.Minute)}
		o, err := owner.New(ownerConfig(c, n.grants), &fakeLog{records: map[string][][]byte{}})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(o.Stop)
		n.owner = o
		if n.local, err = NewLocal(o, n.addr, "us-central1", key, everyWorkspace); err != nil {
			t.Fatal(err)
		}
	}
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	ev := &events{}
	n.store = &fakeStore{ev: ev}
	door, err := New(Config{Enabled: everyWorkspace, Owners: HTTPOwners{Client: client, Scheme: "http"}, Store: n.store,
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

// TestAGatewaysAuthorizeCrossesTheNetworkWhole: a gateway's authorize over
// the network reaches its shard's owner with all it says, the boot's
// declaration of the heartbeat at stream open too.
func TestAGatewaysAuthorizeCrossesTheNetworkWhole(t *testing.T) {
	f := newDoor(t, 1)
	f.owners.admitted[0] = OwnerAdmitted{Status: Admitted, Envelope: "sealed", EndOfLife: start.Add(time.Hour)}
	srv := httptest.NewServer(Handler(f.door, nil))
	t.Cleanup(srv.Close)
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: srv.URL}
	for _, open := range []bool{true, false} {
		got, err := gw.Authorize(context.Background(), AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 40,
			Stream: true, Boot: []byte("boot"), OpenHeartbeat: open})
		if err != nil || got.Status != Admitted {
			t.Fatalf("the authorize: %+v %v", got, err)
		}
	}
	want := []OwnerAuthorize{
		{Workspace: "ws-1", Estimate: 40, Stream: true, Boot: []byte("boot"), OpenHeartbeat: true},
		{Workspace: "ws-1", Estimate: 40, Stream: true, Boot: []byte("boot")},
	}
	if !reflect.DeepEqual(f.owners.authorizes, want) {
		t.Fatalf("the owner was sent %+v, want %+v", f.owners.authorizes, want)
	}
}

// TestADeclaredStreamWithNoHeartbeatIsReleased: an authorize sent to an
// owner, over the network or in the process, says whether its boot declares
// the heartbeat at stream open, and the owner keeps it: once the
// first-heartbeat allowance and the grace pass, a declared stream's hold
// with no heartbeat issued is released, and neither an undeclared stream's
// nor a declared stream's that heartbeated is.
func TestADeclaredStreamWithNoHeartbeatIsReleased(t *testing.T) {
	for _, network := range []bool{true, false} {
		t.Run(fmt.Sprint("network=", network), func(t *testing.T) { declaredStreamReleased(t, network) })
	}
}

func declaredStreamReleased(t *testing.T, network bool) {
	ctx := context.Background()
	f := newLocalWith(t, func(c *owner.Config) {
		c.Grace, c.FirstHeartbeat, c.Records = 10*time.Second, 5*time.Second, fakeRecordTopic{}
	})
	var owners Owners = Direct{"node-a": f.local}
	address := "node-a"
	if network {
		srv := httptest.NewServer(Handler(nil, f.local))
		t.Cleanup(srv.Close)
		owners, address = HTTPOwners{Client: &http.Client{Timeout: 5 * time.Second}, Scheme: "http"},
			srv.Listener.Addr().String()
	}
	admit := func(open bool) Envelope {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(time.Millisecond) {
			got, err := owners.Authorize(ctx, address, OwnerAuthorize{Workspace: "ws-1",
				Estimate: 40, Stream: true, Boot: []byte("boot"), OpenHeartbeat: open})
			if err != nil {
				t.Fatal(err)
			}
			if got.Status == Busy {
				continue
			}
			e, err := Open(key, got.Envelope)
			if got.Status != Admitted || err != nil {
				t.Fatalf("an authorize: %+v %v", got, err)
			}
			return e
		}
		t.Fatal("no lease came to admit the request")
		return Envelope{}
	}
	declared, undeclared, beating := admit(true), admit(false), admit(true)
	if got := f.local.Heartbeat(ctx, OwnerHeartbeat{Lease: beating.Lease, Auth: beating.Auth, GatewaySeq: 1,
		Hash: hash("b1"), Basis: []byte("terms")}); got.Status != Accepted {
		t.Fatalf("the heartbeat: %+v", got)
	}
	f.clock.advance(14 * time.Second)
	if err := f.owner.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	if got := f.log.kinds(t, declared.Lease)[declared.Auth]; len(got) != 0 {
		t.Fatalf("released before its allowance and grace passed: %v", got)
	}
	f.clock.advance(2 * time.Second)
	if err := f.owner.Reap(ctx); err != nil {
		t.Fatal(err)
	}
	kinds := f.log.kinds(t, declared.Lease)
	if declared.Lease != undeclared.Lease || declared.Lease != beating.Lease ||
		!slices.Equal(kinds[declared.Auth], []record.Kind{record.Release}) || len(kinds[undeclared.Auth]) != 0 ||
		!slices.Equal(kinds[beating.Auth], []record.Kind{record.Heartbeat}) {
		t.Fatalf("the records of %s, %s and %s: %v", declared.Auth, undeclared.Auth, beating.Auth, kinds)
	}
}

// TestAnUndeclaredAuthorizeReadsAsBefore: an authorize that declares no
// heartbeat at stream open is sent as authorizes were before the
// declaration, so a node from before it, which refuses a field it does not
// know, takes it, the gateway's and the front door's both; a declared one it
// refuses.
func TestAnUndeclaredAuthorizeReadsAsBefore(t *testing.T) {
	ctx := context.Background()
	// before is a node from before the declaration, which reads the
	// authorize into into, refusing a field it does not know, and answers
	// answer.
	before := func(into, answer any) *httptest.Server {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			dec := json.NewDecoder(r.Body)
			dec.DisallowUnknownFields()
			if err := dec.Decode(into); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(answer)
		}))
		t.Cleanup(srv.Close)
		return srv
	}
	var gatewaysAuthorize struct {
		Workspace, Request string
		Estimate           int64
		Stream             bool
		Boot               []byte
	}
	door := before(&gatewaysAuthorize, Authorized{Status: Admitted, Envelope: "sealed"})
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: door.URL}
	var ownersAuthorize struct {
		Workspace string
		Shard     int64
		Estimate  int64
		Stream    bool
		Boot      []byte
	}
	owner := before(&ownersAuthorize, OwnerAdmitted{Status: Admitted, Envelope: "sealed"})
	owners := HTTPOwners{Client: &http.Client{Timeout: 5 * time.Second}, Scheme: "http"}
	for _, open := range []bool{false, true} {
		got, err := gw.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 40, Stream: true,
			Boot: []byte("boot"), OpenHeartbeat: open})
		if taken := err == nil && got.Status == Admitted; taken == open {
			t.Fatalf("declared %v, the gateway's authorize to a front door from before: %+v %v", open, got, err)
		}
		admitted, err := owners.Authorize(ctx, owner.Listener.Addr().String(), OwnerAuthorize{Workspace: "ws-1",
			Estimate: 40, Stream: true, Boot: []byte("boot"), OpenHeartbeat: open})
		if taken := err == nil && admitted.Status == Admitted; taken == open {
			t.Fatalf("declared %v, the authorize to an owner from before: %+v %v", open, admitted, err)
		}
	}
	if gatewaysAuthorize.Workspace != "ws-1" || ownersAuthorize.Workspace != "ws-1" || !ownersAuthorize.Stream {
		t.Fatalf("what the nodes from before read: %+v, %+v", gatewaysAuthorize, ownersAuthorize)
	}
}

// answering is a server that answers each request with status and body.
func answering(t *testing.T, status int, body string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv.Listener.Addr().String()
}

// TestAnOwnerNotReachedOverTheNetwork: no answer, an answer other than a
// 200 though its body reads, a 200 whose body is not one JSON value of the
// answer's kind, one cut short, or an answer from a node the one addressed
// redirects to, is an owner not reached.
func TestAnOwnerNotReachedOverTheNetwork(t *testing.T) {
	ctx := context.Background()
	h := HTTPOwners{Client: &http.Client{Timeout: 2 * time.Second}, Scheme: "http"}
	closed := httptest.NewServer(http.NotFoundHandler())
	gone := closed.Listener.Addr().String()
	closed.Close()
	good := answering(t, http.StatusOK, `{"Status":"won"}`)
	redirecting := func(to string) string {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "http://"+to+r.URL.Path, http.StatusTemporaryRedirect)
		}))
		t.Cleanup(srv.Close)
		return srv.Listener.Addr().String()
	}
	pinged := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer pinged.Close()
	cut := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "100")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"Status":"won"}`))
		w.(http.Flusher).Flush() // the client reads the value before the cut
		panic(http.ErrAbortHandler)
	}))
	defer cut.Close()
	for name, addr := range map[string]string{
		"no answer":                gone,
		"a 500 that reads":         answering(t, http.StatusInternalServerError, `{}`),
		"not JSON":                 answering(t, http.StatusOK, "not json"),
		"a field the answer lacks": answering(t, http.StatusOK, `{"Error":"owner not reached"}`),
		"two values":               answering(t, http.StatusOK, `{} {}`),
		"null":                     answering(t, http.StatusOK, `null`),
		"past its bound":           answering(t, http.StatusOK, `{}`+strings.Repeat(" ", maxBody)),
		"cut short":                cut.Listener.Addr().String(),
		"a redirect":               redirecting(good),
	} {
		if _, err := h.Authorize(ctx, addr, OwnerAuthorize{}); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%s: an authorize: %v", name, err)
		}
		if _, err := h.Heartbeat(ctx, addr, OwnerHeartbeat{}); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%s: a heartbeat: %v", name, err)
		}
		if _, err := h.Terminal(ctx, addr, OwnerTerminal{}); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%s: a terminal: %v", name, err)
		}
	}
	if got, err := h.Terminal(ctx, good, OwnerTerminal{}); err != nil || got.Status != Won {
		t.Fatalf("an owner that answers: %+v %v", got, err)
	}
	for name, addr := range map[string]string{"no answer": gone, "a 500": answering(t, http.StatusInternalServerError, `{}`),
		"a redirect": redirecting(pinged.Listener.Addr().String())} {
		if err := h.Ping(ctx, addr); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%s: a ping: %v", name, err)
		}
	}
	if err := h.Ping(ctx, pinged.Listener.Addr().String()); err != nil {
		t.Fatalf("a ping answered: %v", err)
	}

	// A call whose context ends, before it is sent or while it waits, ends
	// with it, though the owner would answer.
	ended, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := h.Terminal(ended, good, OwnerTerminal{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("a call whose context had ended: %v", err)
	}
	got := make(chan struct{})
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body) // so the server sees the caller go
		close(got)
		<-r.Context().Done()
	}))
	defer slow.Close()
	waiting, cancel := context.WithCancel(ctx)
	go func() {
		<-got
		cancel()
	}()
	began := time.Now()
	if _, err := h.Terminal(waiting, slow.Listener.Addr().String(), OwnerTerminal{}); !errors.Is(err, context.Canceled) ||
		time.Since(began) > time.Second {
		t.Fatalf("a call whose context ended while it waited: %v after %v", err, time.Since(began))
	}

	// A call whose context ends as the answer's body ends.
	ending, cancel := context.WithCancel(ctx)
	defer cancel()
	c := HTTPOwners{Client: &http.Client{Timeout: 2 * time.Second, Transport: endingAt{cancel}}, Scheme: "http"}
	if _, err := c.Terminal(ending, good, OwnerTerminal{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("a call whose context ended at its answer's end: %v", err)
	}
	ending, cancel = context.WithCancel(ctx)
	defer cancel()
	c = HTTPOwners{Client: &http.Client{Timeout: 2 * time.Second, Transport: endingAt{cancel}}, Scheme: "http"}
	if err := c.Ping(ending, pinged.Listener.Addr().String()); !errors.Is(err, context.Canceled) {
		t.Fatalf("a ping whose context ended at its answer's end: %v", err)
	}
}

// endingAt is a transport that ends the call's context once the answer's
// body has been read to its end.
type endingAt struct{ cancel context.CancelFunc }

func (e endingAt) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := http.DefaultTransport.RoundTrip(req)
	if err == nil {
		resp.Body = endingBody{resp.Body, e.cancel}
	}
	return resp, err
}

type endingBody struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b endingBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if errors.Is(err, io.EOF) {
		b.cancel()
	}
	return n, err
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
		{"POST", "/v1/refund", `{"Envelope":"x","Money":"e30="}]`, http.StatusBadRequest},
		{"POST", "/v1/refund", `{"Envelope":"x","Money":"e30="}}`, http.StatusBadRequest},
		{"POST", "/v1/refund", `null`, http.StatusBadRequest},
		{"POST", "/v1/refund", ``, http.StatusBadRequest},
		{"POST", "/v1/refund", `{"Envelope":` + strings.Repeat(" ", maxBody) + `"x"}`, http.StatusBadRequest},
		{"POST", "/v1/refund", `{"Envelope":"x","Money":"e30="}` + strings.Repeat(" ", maxBody), http.StatusBadRequest},
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

// TestABodyPastItsBoundIsAnsweredAtOnce: a request whose body goes past the
// bound is answered 400 without the server waiting for the rest of it.
func TestABodyPastItsBoundIsAnsweredAtOnce(t *testing.T) {
	n := newNode(t, false, nil, nil)
	conn, err := net.Dial("tcp", n.addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	fmt.Fprintf(conn, "POST /v1/refund HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"+
		"Content-Length: %d\r\n\r\n", n.addr, maxBody+2)
	if _, err := conn.Write(bytes.Repeat([]byte(" "), maxBody+1)); err != nil {
		t.Fatal(err)
	}
	if err := conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil || !strings.HasPrefix(line, "HTTP/1.1 400") {
		t.Fatalf("the answer to a body past its bound, its last byte not sent: %q %v", line, err)
	}
}

// TestARequestCutShortOrLeftStartsNothing: a request whose body ends before
// its length is not dispatched: a refund whose owner no one reaches appends
// nothing. Nor is one whose caller has gone by the time it is read: an
// owner's authorize admits nothing.
func TestARequestCutShortOrLeftStartsNothing(t *testing.T) {
	n := newNode(t, false, nil, nil)
	body := fmt.Sprintf(`{"Envelope":%q,"Money":"e30="}`, sealedAt(t, "127.0.0.1:1", "lease-1", "gwa-1"))
	appends := func() int {
		k := 0
		for _, e := range n.store.ev.all() {
			if strings.HasPrefix(e, "append ") {
				k++
			}
		}
		return k
	}
	// send sends the refund with a length; cut, the client then closes its
	// side, so the body ends before the length.
	send := func(length int, cut bool) {
		t.Helper()
		conn, err := net.Dial("tcp", n.addr)
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		fmt.Fprintf(conn, "POST /v1/refund HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"+
			"Content-Length: %d\r\nConnection: close\r\n\r\n%s", n.addr, length, body)
		if cut {
			if err := conn.(*net.TCPConn).CloseWrite(); err != nil {
				t.Fatal(err)
			}
		}
		_, _ = io.ReadAll(conn)
	}
	send(len(body)+10, true)
	if k := appends(); k != 0 {
		t.Fatalf("a request cut short appended %d times", k)
	}
	send(len(body), false)
	if k := appends(); k != 1 {
		t.Fatalf("the whole request appended %d times", k)
	}

	m := newNode(t, true, nil, nil)
	h := Handler(nil, m.local)
	authorize := func(ctx context.Context) OwnerAdmitted {
		t.Helper()
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest("POST", "/owner/authorize",
			strings.NewReader(`{"Workspace":"ws-1","Shard":0,"Estimate":7,"Boot":"Ym9vdA=="}`)).WithContext(ctx))
		var got OwnerAdmitted
		_ = json.Unmarshal(rec.Body.Bytes(), &got)
		return got
	}
	// The first authorizes ask for the shard's lease, until it is granted.
	for deadline := time.Now().Add(5 * time.Second); authorize(context.Background()).Status != Admitted; time.Sleep(time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("no lease came to admit")
		}
	}
	before := m.held()
	gone, cancel := context.WithCancel(context.Background())
	cancel()
	authorize(gone)
	if held := m.held(); held != before {
		t.Fatalf("an authorize whose caller had gone: held %d, before %d", held, before)
	}
	if got := authorize(context.Background()); got.Status != Admitted || m.held() != before+7 {
		t.Fatalf("an authorize whose caller waits: %+v, held %d, before %d", got, m.held(), before)
	}
}
