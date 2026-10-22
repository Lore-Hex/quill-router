package service

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"cloud.google.com/go/spanner/apiv1/spannerpb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"

	"github.com/Lore-Hex/quill-router/fastpath/internal/edge"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// TestTheEdgePassesTheEnclavesCallsToPythonWithItsVerdict: a process with
// the edge configured serves it beside the node, reading the enclave's
// boot from the entity table and passing the call to Python (a test
// server here) with the verdict on the boot's signature; Python's answer
// comes back as it was.
func TestTheEdgePassesTheEnclavesCallsToPythonWithItsVerdict(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	kid := storetest.UniqueID("boot")
	body := `{"kid":"` + kid + `","jwk":{"kty":"OKP","crv":"Ed25519","x":"` + base64.RawURLEncoding.EncodeToString(public) +
		`"},"approved":true,"verified":true,"image_digest":"sha256:abc","attestation_kind":"gcp-cs-jwt","registered_at":"2026-10-19T00:00:00Z"}`
	if _, err := shared.Apply(ctx, []*spanner.Mutation{spanner.Insert("tr_entities", []string{"kind", "id", "body", "updated_at"},
		[]any{"spend_lease_boot", kid, body, spanner.CommitTimestamp})}); err != nil {
		t.Fatal(err)
	}
	type seen struct{ verdict, path, body, token string }
	saw := make(chan seen, 4)
	python := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		saw <- seen{verdict: r.Header.Get(edge.VerdictHeader), path: r.URL.Path, body: string(b), token: r.Header.Get(edge.TokenHeader)}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":{"code":402}}`))
	}))
	defer python.Close()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	edgeLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cfg := config(ln)
	cfg.Edge = EdgeConfig{Address: edgeLn.Addr().String(), Listener: edgeLn, Upstream: python.URL, Token: "token-1"}
	start(t, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})

	request := `{"api_key_lookup_hash":"lookup-1","model":"m"}`
	sig := ed25519.Sign(private, edge.Digest(http.MethodPost, "/internal/gateway/authorize", []byte(request)))
	client := &http.Client{Timeout: 10 * time.Second}
	var resp *http.Response
	eventually(t, 20*time.Second, "the edge serving", func() (bool, error) {
		req, _ := http.NewRequest(http.MethodPost, "http://"+edgeLn.Addr().String()+"/internal/gateway/authorize", strings.NewReader(request))
		req.Header.Set(edge.TokenHeader, "token-1")
		req.Header.Set(edge.BootHeader, "kid="+kid+",sig="+base64.RawURLEncoding.EncodeToString(sig))
		var err error
		resp, err = client.Do(req)
		return err == nil, nil
	})
	defer resp.Body.Close()
	answer, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusPaymentRequired || string(answer) != `{"error":{"code":402}}` {
		t.Fatalf("python's answer: %d %s", resp.StatusCode, answer)
	}
	got := <-saw
	if got.verdict != string(edge.Verified) || got.path != "/internal/gateway/authorize" || got.body != request || got.token != "token-1" {
		t.Fatalf("python saw %+v", got)
	}

	// An unknown boot and an altered signature are Invalid: the verdict
	// came from the store's boot and the signature over these bytes.
	for name, header := range map[string]string{
		"unknown boot":      "kid=boot-none,sig=" + base64.RawURLEncoding.EncodeToString(sig),
		"altered signature": "kid=" + kid + ",sig=" + base64.RawURLEncoding.EncodeToString(append([]byte{sig[0] ^ 1}, sig[1:]...)),
	} {
		req, _ := http.NewRequest(http.MethodPost, "http://"+edgeLn.Addr().String()+"/internal/gateway/authorize", strings.NewReader(request))
		req.Header.Set(edge.TokenHeader, "token-1")
		req.Header.Set(edge.BootHeader, header)
		r, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		if got := <-saw; got.verdict != string(edge.Invalid) {
			t.Fatalf("%s: verdict %q", name, got.verdict)
		}
	}
	// The membership row names the edge.
	eventually(t, 10*time.Second, "the membership row", func() (bool, error) {
		row, err := shared.Single().ReadRow(ctx, "tr_fastpath_member", spanner.Key{ln.Addr().String()}, []string{"roles"})
		if err != nil {
			return false, err
		}
		var roles []string
		if err := row.Column(0, &roles); err != nil {
			return false, err
		}
		return strings.Join(roles, ",") == "owner,frontdoor,auditor,edge", nil
	})

	// The node's own front door still serves beside it, on its own port.
	health, err := client.Post("http://"+ln.Addr().String()+"/v1/authorize", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	health.Body.Close()
	if health.StatusCode == http.StatusNotFound {
		t.Fatal("the front door is gone")
	}
}

// TestTheEdgesHealthLeavesBeforeTheProcessStops: both Run's context and
// Leave turn the balancer away while the node's leaving write is under way.
func TestTheEdgesHealthLeavesBeforeTheProcessStops(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	for _, signal := range []string{"context", "Leave"} {
		t.Run(signal, func(t *testing.T) {
			entered, release := make(chan struct{}), make(chan struct{})
			unblock := sync.OnceFunc(func() { close(release) })
			var once sync.Once
			unary := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker,
				opts ...grpc.CallOption) error {
				if q, ok := req.(*spannerpb.ExecuteSqlRequest); ok && tagOf(q) == "fastpath-heartbeat" &&
					q.GetParams().GetFields()["state"].GetStringValue() == store.Leaving {
					once.Do(func() { close(entered) })
					select {
					case <-release:
					case <-ctx.Done():
						return ctx.Err()
					}
				}
				return invoker(ctx, method, req, reply, cc, opts...)
			}
			db, err := emulator.Client(context.Background(), "spike",
				option.WithGRPCDialOption(grpc.WithChainUnaryInterceptor(unary)))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(db.Close)
			ln, edgeLn := listen(t), listen(t)
			leave := make(chan struct{})
			cfg := config(ln)
			cfg.Auditor, cfg.Leave, cfg.Store.LiveFor = false, leave, 10*time.Second
			cfg.Edge = EdgeConfig{Address: edgeLn.Addr().String(), Listener: edgeLn,
				Upstream: "http://python.invalid", Token: "token-1"}
			clients := Clients{Spanner: db, PubSub: pubSub(t)}
			ctx, cancel := context.WithCancel(context.Background())
			done := make(chan struct{})
			var runErr error
			go func() {
				runErr = Run(ctx, cfg, clients)
				close(done)
			}()
			t.Cleanup(func() {
				unblock()
				cancel()
				select {
				case <-done:
					if runErr != nil {
						t.Errorf("the process ended: %v", runErr)
					}
				case <-time.After(20 * time.Second):
					t.Error("the process did not stop")
				}
			})
			client := &http.Client{Timeout: 5 * time.Second}
			eventually(t, 20*time.Second, "the node serving", func() (bool, error) {
				resp, err := client.Get("http://" + ln.Addr().String() + "/owner/ping")
				if err != nil {
					return false, nil
				}
				resp.Body.Close()
				return resp.StatusCode == http.StatusNoContent, nil
			})
			health := func(code int, body string) {
				t.Helper()
				resp, err := client.Get("http://" + edgeLn.Addr().String() + "/internal/gateway/edge/health")
				if err != nil {
					t.Fatal(err)
				}
				defer resp.Body.Close()
				answer, _ := io.ReadAll(resp.Body)
				if resp.StatusCode != code || string(answer) != body {
					t.Fatalf("health: %d %q", resp.StatusCode, answer)
				}
			}
			health(http.StatusOK, `{"status":"serving"}`)
			if signal == "context" {
				cancel()
			} else {
				close(leave)
			}
			select {
			case <-entered:
			case <-done:
				t.Fatalf("the process stopped before leaving: %v", runErr)
			case <-time.After(10 * time.Second):
				t.Fatal("the node did not begin leaving")
			}
			health(http.StatusServiceUnavailable, `{"status":"leaving"}`)
			select {
			case <-done:
				t.Fatalf("the process stopped before its leaving write ended: %v", runErr)
			default:
			}
			unblock()
		})
	}
}

func TestAnEdgeNeedsItsUpstreamAndToken(t *testing.T) {
	cfg := Defaults()
	cfg.Admission, cfg.Address, cfg.Region, cfg.Key = true, "127.0.0.1:1", "us-central1", key
	cfg.SettleTopic, cfg.RecordTopic = "projects/spike/topics/settle-log", "projects/spike/topics/records"
	cfg.Edge = EdgeConfig{Address: "127.0.0.1:2"}
	if err := cfg.valid(); err == nil || !strings.Contains(err.Error(), "edge") {
		t.Fatalf("an edge without its upstream and token: %v", err)
	}
	cfg.Edge.Upstream, cfg.Edge.Token = "http://python.internal", "t"
	if err := cfg.valid(); err != nil {
		t.Fatalf("a whole edge: %v", err)
	}
	// An edge leaves with the admission node; a member without one would
	// answer the balancer serving for ever.
	cfg.Admission, cfg.Auditor = false, true
	cfg.SettleSubscription, cfg.RecordSubscription = "projects/spike/subscriptions/auditor", "projects/spike/subscriptions/stager"
	if err := cfg.valid(); err == nil || !strings.Contains(err.Error(), "admission node") {
		t.Fatalf("an edge without an admission node: %v", err)
	}
}

// TestAProcessWithTheSnapshotPollsItBesideTheEdge: configured, the edge's
// routing snapshot is polled from the upstream with the internal token by
// the running process, and authorizes still get Python's answer as it was.
func TestAProcessWithTheSnapshotPollsItBesideTheEdge(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	polls := make(chan string, 16)
	python := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/internal/fastpath/routing-snapshot" {
			select {
			case polls <- r.Header.Get(edge.TokenHeader):
			default:
			}
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"status":"snapshot-refused","retry_at":0}`))
			return
		}
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":{"code":402}}`))
	}))
	defer python.Close()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	edgeLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	cfg := config(ln)
	cfg.Edge = EdgeConfig{Address: edgeLn.Addr().String(), Listener: edgeLn, Upstream: python.URL, Token: "token-1",
		SnapshotURL: python.URL + "/internal/fastpath/routing-snapshot", SnapshotKid: "routing-snapshot-1",
		SnapshotPublicKey: base64.RawURLEncoding.EncodeToString(public), SnapshotPlane: "gcp/us-central1", Shadow: true}
	start(t, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})
	select {
	case got := <-polls:
		if got != "token-1" {
			t.Fatalf("the poll carried %q, not the internal token", got)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the process did not poll the routing snapshot")
	}
	client := &http.Client{Timeout: 10 * time.Second}
	var resp *http.Response
	eventually(t, 20*time.Second, "the edge serving", func() (bool, error) {
		req, _ := http.NewRequest(http.MethodPost, "http://"+edgeLn.Addr().String()+"/internal/gateway/authorize",
			strings.NewReader(`{"model":"m"}`))
		req.Header.Set(edge.TokenHeader, "token-1")
		var err error
		resp, err = client.Do(req)
		return err == nil, nil
	})
	defer resp.Body.Close()
	if answer, _ := io.ReadAll(resp.Body); resp.StatusCode != http.StatusPaymentRequired || string(answer) != `{"error":{"code":402}}` {
		t.Fatalf("python's answer: %d %s", resp.StatusCode, answer)
	}
}

// TestTheEdgesSnapshotGoesWithTheEdgeAndItsKey: the routing snapshot is the
// edge's, needs its key, kid and plane, and the shadow needs the snapshot;
// with no URL the edge polls nothing, which is the default.
func TestTheEdgesSnapshotGoesWithTheEdgeAndItsKey(t *testing.T) {
	cfg := Defaults()
	cfg.Admission, cfg.Address, cfg.Region, cfg.Key = true, "127.0.0.1:1", "us-central1", key
	cfg.SettleTopic, cfg.RecordTopic = "projects/spike/topics/settle-log", "projects/spike/topics/records"
	if cfg.Edge.SnapshotURL != "" || cfg.Edge.Shadow {
		t.Fatalf("the defaults poll or compare: %+v", cfg.Edge)
	}
	public := base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.PublicKeySize))
	whole := EdgeConfig{Address: "127.0.0.1:2", Upstream: "http://python.internal", Token: "t",
		SnapshotURL: "http://python.internal/internal/fastpath/routing-snapshot", SnapshotPublicKey: public,
		SnapshotKid: "routing-snapshot-1", SnapshotPlane: "gcp/us-central1", Shadow: true}
	cfg.Edge = whole
	if err := cfg.valid(); err != nil {
		t.Fatalf("a whole snapshot: %v", err)
	}
	snapshot, err := snapshotConfig(whole)
	if err != nil || snapshot.URL == nil || len(snapshot.Keys) != 1 || len(snapshot.Keys[0].PublicKey) != ed25519.PublicKeySize ||
		!snapshot.Shadow || snapshot.Keys[0].Kid != "routing-snapshot-1" || snapshot.Plane != "gcp/us-central1" {
		t.Fatalf("the snapshot's configuration: %+v %v", snapshot, err)
	}
	// A rotation's two keys, paired with their kids in order.
	rotation := whole
	rotation.SnapshotPublicKey = public + "," + base64.RawURLEncoding.EncodeToString(append(make([]byte, 31), 1))
	rotation.SnapshotKid = "routing-snapshot-1,routing-snapshot-2"
	if both, err := snapshotConfig(rotation); err != nil || len(both.Keys) != 2 || both.Keys[1].Kid != "routing-snapshot-2" ||
		both.Keys[1].PublicKey[31] != 1 {
		t.Fatalf("a rotation's keys: %+v %v", both, err)
	}
	if none, err := snapshotConfig(EdgeConfig{}); err != nil || none.URL != nil || none.Shadow {
		t.Fatalf("no URL polls nothing: %+v %v", none, err)
	}
	for _, c := range []struct {
		name   string
		change func(*EdgeConfig)
	}{
		{"no edge", func(e *EdgeConfig) { e.Address, e.Upstream, e.Token = "", "", "" }},
		{"a shadow with no snapshot", func(e *EdgeConfig) { e.SnapshotURL = "" }},
		{"no key", func(e *EdgeConfig) { e.SnapshotPublicKey = "" }},
		{"no kid", func(e *EdgeConfig) { e.SnapshotKid = "" }},
		{"no plane", func(e *EdgeConfig) { e.SnapshotPlane = "" }},
	} {
		cfg.Edge = whole
		c.change(&cfg.Edge)
		if err := cfg.valid(); err == nil {
			t.Errorf("%s: valid", c.name)
		}
	}
	for _, c := range []struct {
		name   string
		change func(*EdgeConfig)
	}{
		{"a short key", func(e *EdgeConfig) { e.SnapshotPublicKey = public[:20] }},
		{"a padded key", func(e *EdgeConfig) { e.SnapshotPublicKey = public + "=" }},
		{"another scheme", func(e *EdgeConfig) { e.SnapshotURL = "ftp://python.internal/x" }},
		{"a key without its kid", func(e *EdgeConfig) { e.SnapshotPublicKey = public + "," + public }},
	} {
		edgeCfg := whole
		c.change(&edgeCfg)
		if _, err := snapshotConfig(edgeCfg); err == nil {
			t.Errorf("%s: accepted", c.name)
		}
	}
}

// TestAFailedProcessIsNotServing: a part's failure ends the parts without
// their leaving, so the edge's answer follows the parts' context too.
func TestAFailedProcessIsNotServing(t *testing.T) {
	p := &parts{caller: context.Background(), armed: make(chan struct{})}
	p.ctx, p.cancel = context.WithCancel(context.Background())
	defer p.cancel()
	if !p.serving() {
		t.Fatal("a running process is serving")
	}
	p.fail(errors.New("a part failed"))
	if p.serving() {
		t.Fatal("a failed process still serving")
	}
	q := &parts{caller: context.Background(), armed: make(chan struct{})}
	q.ctx, q.cancel = context.WithCancel(context.Background())
	defer q.cancel()
	q.retiring.Store(true)
	if q.serving() {
		t.Fatal("a retiring process still serving")
	}
}

// TestAnEdgeUpstreamIsAnHTTPURL: a scheme the edge cannot speak fails the
// process before any row is taken, as a taken port does.
func TestAnEdgeUpstreamIsAnHTTPURL(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cfg := config(ln)
	cfg.Edge = EdgeConfig{Address: "127.0.0.1:0", Upstream: "ftp://python.internal", Token: "t"}
	err = Run(context.Background(), cfg, Clients{Spanner: shared, PubSub: pubSub(t)})
	if err == nil || !strings.Contains(err.Error(), "not an http or https URL") {
		t.Fatalf("an ftp upstream: %v", err)
	}
	if _, err := shared.Single().ReadRow(context.Background(), "tr_fastpath_member", spanner.Key{ln.Addr().String()}, []string{"roles"}); err == nil {
		t.Fatal("the node took its ring row")
	}
}

// TestAnEdgeThatCannotListenFailsBeforeTheNodeJoins: the edge binds first,
// so a port another serves on fails the process with no ring row taken,
// and a supplied listener is closed however Run returns.
func TestAnEdgeThatCannotListenFailsBeforeTheNodeJoins(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer taken.Close()
	cfg := config(ln)
	cfg.Edge = EdgeConfig{Address: taken.Addr().String(), Upstream: "http://python.internal", Token: "t"}
	err = Run(ctx, cfg, Clients{Spanner: shared, PubSub: pubSub(t)})
	if err == nil || !strings.Contains(err.Error(), "address already in use") {
		t.Fatalf("an edge on a taken port: %v", err)
	}
	if _, err := shared.Single().ReadRow(ctx, "tr_fastpath_member", spanner.Key{ln.Addr().String()}, []string{"roles"}); err == nil {
		t.Fatal("the node took its ring row before the edge failed")
	}
	// A supplied edge listener is closed when the configuration is refused.
	edgeLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	cfg = config(ln)
	cfg.Edge = EdgeConfig{Address: edgeLn.Addr().String(), Listener: edgeLn}
	if err := Run(ctx, cfg, Clients{Spanner: shared, PubSub: pubSub(t)}); err == nil {
		t.Fatal("an edge without its upstream and token ran")
	}
	if _, err := edgeLn.Accept(); err == nil || !strings.Contains(err.Error(), "closed") {
		t.Fatalf("the supplied listener was not closed: %v", err)
	}
}
