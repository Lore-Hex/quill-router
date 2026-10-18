package edge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const token = "internal-token-1"

type boots struct {
	by    map[string]Boot
	reads atomic.Int64
	fail  bool
}

func (b *boots) Boot(_ context.Context, kid string) (Boot, bool, error) {
	b.reads.Add(1)
	if b.fail {
		return Boot{}, false, errors.New("spanner away")
	}
	boot, ok := b.by[kid]
	return boot, ok, nil
}

type digests struct {
	accepted map[string]bool
	fail     bool
}

func (d *digests) Accepted(_ context.Context, digest string) (bool, error) {
	if d.fail {
		return false, errors.New("policy away")
	}
	return d.accepted[digest], nil
}

type seen struct {
	method, path, query, verdict, host, body string
	headers                                  http.Header
}

type harness struct {
	edge     *Edge
	server   *httptest.Server
	upstream *httptest.Server
	boots    *boots
	digests  *digests
	last     *seen
	private  ed25519.PrivateKey
	now      time.Time
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{now: time.Date(2026, 10, 18, 20, 0, 0, 0, time.UTC)}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	h.private = private
	h.boots = &boots{by: map[string]Boot{
		"boot-1": {Kid: "boot-1", PublicKey: public, Verified: true, ImageDigest: "sha256:good"},
		"boot-2": {Kid: "boot-2", PublicKey: public, Verified: false, ImageDigest: "sha256:good"},
		"boot-3": {Kid: "boot-3", PublicKey: public, Verified: true, ImageDigest: "sha256:old"},
	}}
	h.digests = &digests{accepted: map[string]bool{"sha256:good": true}}
	h.upstream = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		h.last = &seen{method: r.Method, path: r.URL.Path, query: r.URL.RawQuery, verdict: r.Header.Get(VerdictHeader),
			host: r.Host, body: string(body), headers: r.Header.Clone()}
		w.Header().Set("X-Answer", "python")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":{"code":402}}`))
	}))
	t.Cleanup(h.upstream.Close)
	upstream, _ := url.Parse(h.upstream.URL)
	edge, err := New(Config{Token: token, Upstream: upstream, Boots: h.boots, Digests: h.digests, Clock: func() time.Time { return h.now }})
	if err != nil {
		t.Fatal(err)
	}
	h.edge = edge
	h.server = httptest.NewServer(edge)
	t.Cleanup(h.server.Close)
	return h
}

func (h *harness) header(kid, method, path string, body []byte) string {
	sig := ed25519.Sign(h.private, Digest(method, path, body))
	return "kid=" + kid + ",sig=" + base64.RawURLEncoding.EncodeToString(sig)
}

func (h *harness) send(t *testing.T, method, path string, body string, headers map[string]string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(method, h.server.URL+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func TestAVerifiedBootsRequestPassesWithItsVerdictAndPythonsAnswer(t *testing.T) {
	h := newHarness(t)
	body := `{"api_key_lookup_hash":"k","model":"m"}`
	path := "/internal/gateway/authorize"
	resp := h.send(t, http.MethodPost, path+"?x=1", body, map[string]string{
		TokenHeader: token, BootHeader: h.header("boot-1", http.MethodPost, path, []byte(body)), "Content-Type": "application/json",
	})
	if resp.StatusCode != http.StatusPaymentRequired || resp.Header.Get("X-Answer") != "python" {
		t.Fatalf("python's answer was not passed: %d %v", resp.StatusCode, resp.Header)
	}
	answer, _ := io.ReadAll(resp.Body)
	if string(answer) != `{"error":{"code":402}}` {
		t.Fatalf("python's body was not passed: %q", answer)
	}
	if h.last.verdict != string(Verified) || h.last.method != http.MethodPost || h.last.path != path || h.last.query != "x=1" ||
		h.last.body != body || h.last.headers.Get(BootHeader) == "" || h.last.headers.Get(TokenHeader) != token {
		t.Fatalf("the request was not passed as it came: %+v", h.last)
	}
}

func TestEveryWayABootSignatureFailsHasItsVerdict(t *testing.T) {
	h := newHarness(t)
	body := `{"a":1}`
	path := "/internal/gateway/authorize"
	good := h.header("boot-1", http.MethodPost, path, []byte(body))
	otherBody := h.header("boot-1", http.MethodPost, path, []byte(`{"a":2}`))
	otherPath := h.header("boot-1", http.MethodPost, "/internal/gateway/heartbeat", []byte(body))
	otherMethod := h.header("boot-1", http.MethodGet, path, []byte(body))
	cases := []struct {
		name, header string
		want         Verdict
	}{
		{"verified", good, Verified},
		{"another body", otherBody, Invalid},
		{"another path", otherPath, Invalid},
		{"another method", otherMethod, Invalid},
		{"unknown boot", h.header("boot-9", http.MethodPost, path, []byte(body)), Invalid},
		{"unverified boot", h.header("boot-2", http.MethodPost, path, []byte(body)), Invalid},
		{"digest not accepted", h.header("boot-3", http.MethodPost, path, []byte(body)), NotAccepted},
		{"malformed", "kid=boot-1", Invalid},
		{"duplicate field", "kid=boot-1,kid=boot-1,sig=AA", Invalid},
		{"short signature", "kid=boot-1,sig=AAAA", Invalid},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			resp := h.send(t, http.MethodPost, path, body, map[string]string{TokenHeader: token, BootHeader: c.header})
			if resp.StatusCode != http.StatusPaymentRequired {
				t.Fatalf("not passed: %d", resp.StatusCode)
			}
			if h.last.verdict != string(c.want) {
				t.Fatalf("verdict %q, want %q", h.last.verdict, c.want)
			}
		})
	}
}

func TestAGetIsSignedOverAnEmptyBody(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/authorizations/gwa-1/disposition"
	resp := h.send(t, http.MethodGet, path, "", map[string]string{TokenHeader: token, BootHeader: h.header("boot-1", http.MethodGet, path, nil)})
	if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != string(Verified) || h.last.method != http.MethodGet {
		t.Fatalf("a signed GET: %d %+v", resp.StatusCode, h.last)
	}
}

func TestARequestWithoutTheTokenIsPassedUntouchedAndReadsNothing(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/authorize"
	for _, headers := range []map[string]string{
		{BootHeader: h.header("boot-1", http.MethodPost, path, []byte(`{}`))},
		{TokenHeader: "wrong", BootHeader: h.header("boot-1", http.MethodPost, path, []byte(`{}`))},
	} {
		resp := h.send(t, http.MethodPost, path, `{}`, headers)
		if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != "" || h.boots.reads.Load() != 0 {
			t.Fatalf("without the token: %d verdict %q reads %d", resp.StatusCode, h.last.verdict, h.boots.reads.Load())
		}
	}
	// A bearer token is the same token.
	resp := h.send(t, http.MethodPost, path, `{}`, map[string]string{"Authorization": "Bearer " + token})
	if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != string(Absent) {
		t.Fatalf("a bearer token: %d verdict %q", resp.StatusCode, h.last.verdict)
	}
}

func TestACallerCannotForgeTheVerdict(t *testing.T) {
	h := newHarness(t)
	resp := h.send(t, http.MethodPost, "/internal/gateway/authorize", `{}`, map[string]string{TokenHeader: token, VerdictHeader: "verified"})
	if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != string(Absent) {
		t.Fatalf("a forged verdict: %d %q", resp.StatusCode, h.last.verdict)
	}
}

func TestReadersThatAreAwayLeaveTheVerdictUnread(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/authorize"
	header := h.header("boot-1", http.MethodPost, path, []byte(`{}`))
	h.boots.fail = true
	h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: header})
	if h.last.verdict != string(Unread) {
		t.Fatalf("boots away: %q", h.last.verdict)
	}
	h.boots.fail = false
	h.digests.fail = true
	h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: header})
	if h.last.verdict != string(Unread) {
		t.Fatalf("digests away: %q", h.last.verdict)
	}
}

func TestABootIsReadOnceWithinItsTTLAndAMissForATenth(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/authorize"
	header := h.header("boot-1", http.MethodPost, path, []byte(`{}`))
	for range 3 {
		h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: header})
	}
	if h.boots.reads.Load() != 1 {
		t.Fatalf("reads within the TTL: %d", h.boots.reads.Load())
	}
	h.now = h.now.Add(10*time.Minute + time.Second)
	h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: header})
	if h.boots.reads.Load() != 2 {
		t.Fatalf("reads past the TTL: %d", h.boots.reads.Load())
	}
	missing := h.header("boot-9", http.MethodPost, path, []byte(`{}`))
	h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: missing})
	h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: missing})
	if h.boots.reads.Load() != 3 {
		t.Fatalf("a miss within a tenth: %d", h.boots.reads.Load())
	}
	h.now = h.now.Add(time.Minute + time.Second)
	h.send(t, http.MethodPost, path, `{}`, map[string]string{TokenHeader: token, BootHeader: missing})
	if h.boots.reads.Load() != 4 {
		t.Fatalf("a miss past a tenth: %d", h.boots.reads.Load())
	}
}

func TestABodyPastTheBoundIsPassedUnchecked(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/settle"
	body := strings.Repeat("x", maxBody+1)
	resp := h.send(t, http.MethodPost, path, body, map[string]string{TokenHeader: token, BootHeader: "kid=boot-1,sig=AA"})
	if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != string(Unchecked) || len(h.last.body) != len(body) {
		t.Fatalf("a large body: %d %q %d", resp.StatusCode, h.last.verdict, len(h.last.body))
	}
}

// TestTheDigestIsPythons pins the digest layout to gateway_boot.py's
// boot_auth_digest: sha256(domain || METHOD || path || sha256(body)), with
// a vector computed by that function.
func TestTheDigestIsPythons(t *testing.T) {
	got := hex.EncodeToString(Digest("post", "/internal/gateway/authorize", []byte(`{"a":1}`)))
	if got != pythonVector {
		t.Fatalf("digest %s, want %s", got, pythonVector)
	}
}

func TestNewRefusesAMissingPartOrAnUpstreamPath(t *testing.T) {
	upstream, _ := url.Parse("http://python.internal")
	withPath, _ := url.Parse("http://python.internal/base")
	b, d := &boots{}, &digests{}
	for _, cfg := range []Config{
		{Upstream: upstream, Boots: b, Digests: d},
		{Token: token, Boots: b, Digests: d},
		{Token: token, Upstream: upstream, Digests: d},
		{Token: token, Upstream: upstream, Boots: b},
		{Token: token, Upstream: withPath, Boots: b, Digests: d},
	} {
		if _, err := New(cfg); err == nil {
			t.Fatalf("accepted %+v", cfg)
		}
	}
}

// pythonVector is boot_auth_digest("post", "/internal/gateway/authorize",
// b'{"a":1}').hex() from src/trusted_router/gateway_boot.py.
const pythonVector = "7408d891ba6811f0bd03e127e94faccb74fb7395460cfbc71d1564abd658f95a"
