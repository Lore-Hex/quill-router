package frontdoor

// Streams on the fast-path lease, the front door's part
// (docs/design/fast-admission-streams.md section 4.6): EchoGranted carried
// to the owner, sent only when set so nodes from before it read every
// heartbeat they did, and the disposition read by envelope.

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

func TestAHeartbeatCarriesEchoGrantedToItsOwner(t *testing.T) {
	ctx := context.Background()
	_, sealed := envelope(t)
	f := newDoor(t, 1)
	f.owners.heartbeat = HeartbeatAnswer{Status: Accepted, Deadline: start.Add(time.Minute)}
	if got := f.door.Heartbeat(ctx, HeartbeatOf{Envelope: sealed, GatewaySeq: 4, Hash: []byte("hash"), Usage: 7,
		Running: 12, EchoGranted: true}); got.Status != Accepted {
		t.Fatalf("the owner's answer: %+v", got)
	}
	if len(f.owners.heartbeats) != 1 || !f.owners.heartbeats[0].EchoGranted || !f.owners.heartbeats[0].Echoed.IsZero() {
		t.Fatalf("forwarded %+v", f.owners.heartbeats)
	}
}

// TestEchoGrantedIsSentOnlyWhenSet: a heartbeat without it encodes as
// before, so a front door or an owner from before the field reads it; one
// with it is refused there, which is why owners take it before front doors,
// and front doors before the control plane sends it.
func TestEchoGrantedIsSentOnlyWhenSet(t *testing.T) {
	ctx := context.Background()
	for _, v := range []any{HeartbeatOf{GatewaySeq: 1}, OwnerHeartbeat{GatewaySeq: 1}} {
		b, err := json.Marshal(v)
		if err != nil || bytes.Contains(b, []byte("EchoGranted")) {
			t.Fatalf("%T without EchoGranted: %s %v", v, b, err)
		}
	}
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
	var gatewaysHeartbeat struct {
		Envelope   string
		GatewaySeq int64
		Hash       []byte
		Usage      int64
		Running    int64
		Echoed     time.Time
		Basis      []byte
	}
	door := before(&gatewaysHeartbeat, HeartbeatAnswer{Status: Accepted, Deadline: start})
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: door.URL}
	var ownersHeartbeat struct {
		Lease, Auth string
		GatewaySeq  int64
		Hash        []byte
		Usage       int64
		Running     int64
		Echoed      time.Time
		Basis       []byte
	}
	owner := before(&ownersHeartbeat, HeartbeatAnswer{Status: Accepted, Deadline: start})
	owners := HTTPOwners{Client: &http.Client{Timeout: 5 * time.Second}, Scheme: "http"}
	for _, granted := range []bool{false, true} {
		got, err := gw.Heartbeat(ctx, HeartbeatOf{Envelope: "sealed", GatewaySeq: 2, Hash: []byte("h"), Usage: 1,
			Running: 1, EchoGranted: granted})
		if taken := err == nil && got.Status == Accepted; taken == granted {
			t.Fatalf("EchoGranted %v, a heartbeat to a front door from before: %+v %v", granted, got, err)
		}
		answered, err := owners.Heartbeat(ctx, owner.Listener.Addr().String(), OwnerHeartbeat{Lease: "l", Auth: "a",
			GatewaySeq: 2, Hash: []byte("h"), Usage: 1, Running: 1, EchoGranted: granted})
		if taken := err == nil && answered.Status == Accepted; taken == granted {
			t.Fatalf("EchoGranted %v, a heartbeat to an owner from before: %+v %v", granted, answered, err)
		}
	}
	if gatewaysHeartbeat.GatewaySeq != 2 || ownersHeartbeat.Auth != "a" {
		t.Fatalf("what the nodes from before read: %+v, %+v", gatewaysHeartbeat, ownersHeartbeat)
	}
}

// TestEchoGrantedCrossesTheNetwork: a gateway's later heartbeat with
// EchoGranted and no echo is accepted by the node's owner, which takes the
// deadline it granted; one with neither is refused, as before.
func TestEchoGrantedCrossesTheNetwork(t *testing.T) {
	ctx := context.Background()
	n := newNode(t, true, nil, nil)
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: n.srv.URL}
	var got Authorized
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(time.Millisecond) {
		var err error
		if got, err = gw.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: "r", Estimate: 40, Stream: true,
			Boot: []byte("boot")}); err != nil {
			t.Fatal(err)
		}
		if got.Status != Busy {
			break
		}
	}
	if got.Status != Admitted {
		t.Fatalf("the authorize: %+v", got)
	}
	if hb, err := gw.Heartbeat(ctx, HeartbeatOf{Envelope: got.Envelope, GatewaySeq: 1, Hash: hash("h1"), Usage: 2,
		Running: 3, Basis: []byte("terms")}); err != nil || hb.Status != Accepted {
		t.Fatalf("the first heartbeat: %+v %v", hb, err)
	}
	if hb, err := gw.Heartbeat(ctx, HeartbeatOf{Envelope: got.Envelope, GatewaySeq: 2, Hash: hash("h2"), Usage: 3,
		Running: 4}); err != nil || hb.Status != Rejected {
		t.Fatalf("a later heartbeat with no echo: %+v %v", hb, err)
	}
	if hb, err := gw.Heartbeat(ctx, HeartbeatOf{Envelope: got.Envelope, GatewaySeq: 2, Hash: hash("h2"), Usage: 3,
		Running: 2, EchoGranted: true}); err != nil || hb.Status != Accepted {
		t.Fatalf("a later heartbeat with EchoGranted and a lower running charge: %+v %v", hb, err)
	}
}

func TestADispositionIsReadByItsEnvelope(t *testing.T) {
	ctx := context.Background()
	_, sealed := envelope(t)
	for name, c := range map[string]struct {
		disposition store.Disposition
		fail        bool
		want        DispositionAnswer
	}{
		"a reap": {disposition: store.Disposition{Outcome: "reaped_snapshot",
			Cost: spanner.NullInt64{Int64: 31, Valid: true}, From: "winner"},
			want: DispositionAnswer{Status: Settled, Outcome: "reaped_snapshot", Cost: 31, CostKnown: true}},
		"a settle with no cost known": {disposition: store.Disposition{Outcome: "settled", From: "record"},
			want: DispositionAnswer{Status: Settled, Outcome: "settled"}},
		"a release": {disposition: store.Disposition{Outcome: "released", Cost: spanner.NullInt64{Valid: true},
			From: "closed"}, want: DispositionAnswer{Status: Settled, Outcome: "released", CostKnown: true}},
		"none found": {disposition: store.Disposition{Outcome: "pending", From: "nowhere"},
			want: DispositionAnswer{Status: Pending}},
		"no outcome":         {want: DispositionAnswer{Status: Pending}},
		"a read that failed": {fail: true, want: DispositionAnswer{Status: Failed}},
	} {
		f := newDoor(t, 1)
		f.store.disposition, f.store.failDisp = c.disposition, c.fail
		if got := f.door.Disposition(ctx, DispositionOf{Envelope: sealed}); got != c.want {
			t.Errorf("%s: %+v, want %+v", name, got, c.want)
		}
		// Nothing is asked of an owner, nothing appended.
		if evs := f.ev.all(); len(evs) != 1 || evs[0] != "disposition gwa-1" {
			t.Errorf("%s: %q", name, evs)
		}
	}
	f := newDoor(t, 1)
	if got := f.door.Disposition(ctx, DispositionOf{Envelope: sealed[:len(sealed)-2] + "AA"}); got.Status != Invalid ||
		len(f.ev.all()) != 0 {
		t.Fatalf("an envelope whose seal does not hold: %+v %q", got, f.ev.all())
	}

	// Over the network, from a gateway.
	n := newNode(t, false, nil, nil)
	n.store.disposition = store.Disposition{Outcome: "refunded", Cost: spanner.NullInt64{Valid: true}, From: "winner"}
	gw := Gateway{Client: &http.Client{Timeout: 5 * time.Second}, Base: n.srv.URL}
	got, err := gw.Disposition(ctx, DispositionOf{Envelope: sealed})
	if err != nil || got != (DispositionAnswer{Status: Settled, Outcome: "refunded", CostKnown: true}) {
		t.Fatalf("a disposition over the network: %+v %v", got, err)
	}
}
