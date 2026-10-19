package service

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/edge"
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
}
