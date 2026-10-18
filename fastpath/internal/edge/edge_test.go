package edge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
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

// pythonVector is boot_auth_digest("post", "/internal/gateway/authorize",
// b'{"a":1}').hex() from src/trusted_router/gateway_boot.py.
const pythonVector = "7408d891ba6811f0bd03e127e94faccb74fb7395460cfbc71d1564abd658f95a"

const (
	authorizePath = "/internal/gateway/authorize"
	heartbeatPath = "/internal/gateway/heartbeat"
	authorizeBody = `{"api_key_lookup_hash":"lookup-1","model":"m"}`
)

type boots struct {
	by    map[string]Boot
	reads atomic.Int64
	fail  bool
	slow  time.Duration
}

func (b *boots) Boot(ctx context.Context, kid string) (Boot, bool, error) {
	b.reads.Add(1)
	if b.slow > 0 {
		select {
		case <-ctx.Done():
			return Boot{}, false, ctx.Err()
		case <-time.After(b.slow):
		}
	}
	if b.fail {
		return Boot{}, false, errors.New("spanner away")
	}
	boot, ok := b.by[kid]
	return boot, ok, nil
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
	last     *seen
	private  ed25519.PrivateKey
	now      time.Time
	respond  func(w http.ResponseWriter)
}

var plainClient = &http.Client{Transport: &http.Transport{DisableCompression: true}}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{now: time.Date(2026, 10, 18, 20, 0, 0, 0, time.UTC)}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	h.private = private
	h.boots = &boots{by: map[string]Boot{
		"boot-1": {Kid: "boot-1", PublicKey: public, Verified: true},
		"boot-2": {Kid: "boot-2", PublicKey: public, Verified: false},
	}}
	h.respond = func(w http.ResponseWriter) {
		w.Header().Set("X-Answer", "python")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":{"code":402}}`))
	}
	h.upstream = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		h.last = &seen{method: r.Method, path: r.URL.Path, query: r.URL.RawQuery, verdict: r.Header.Get(VerdictHeader),
			host: r.Host, body: string(body), headers: r.Header.Clone()}
		h.respond(w)
	}))
	t.Cleanup(h.upstream.Close)
	upstream, _ := url.Parse(h.upstream.URL)
	edge, err := New(Config{Token: token, Upstream: upstream, Boots: h.boots, BootWait: 200 * time.Millisecond,
		Clock: func() time.Time { return h.now }})
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
	// The test's own client negotiates nothing either, so what it sees is
	// what the edge sent.
	resp, err := plainClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func (h *harness) signed(t *testing.T, kid, method, path, body string, extra map[string]string) *http.Response {
	t.Helper()
	headers := map[string]string{TokenHeader: token, BootHeader: h.header(kid, method, path, []byte(body))}
	for k, v := range extra {
		headers[k] = v
	}
	return h.send(t, method, path, body, headers)
}

func TestAVerifiedBootsRequestPassesWithItsVerdictAndPythonsAnswer(t *testing.T) {
	h := newHarness(t)
	resp := h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, map[string]string{"Content-Type": "application/json"})
	if resp.StatusCode != http.StatusPaymentRequired || resp.Header.Get("X-Answer") != "python" {
		t.Fatalf("python's answer was not passed: %d %v", resp.StatusCode, resp.Header)
	}
	answer, _ := io.ReadAll(resp.Body)
	if string(answer) != `{"error":{"code":402}}` {
		t.Fatalf("python's body was not passed: %q", answer)
	}
	if h.last.verdict != string(Verified) || h.last.method != http.MethodPost || h.last.path != authorizePath ||
		h.last.body != authorizeBody || h.last.headers.Get(BootHeader) == "" || h.last.headers.Get(TokenHeader) != token ||
		h.last.headers.Get("Content-Type") != "application/json" {
		t.Fatalf("the request was not passed as it came: %+v", h.last)
	}
}

func TestTheQueryAndAnEscapedPathPassAsTheyCame(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/authorizations/gwa%2F1/disposition"
	resp := h.send(t, http.MethodGet, path+"?x=1&y=2", "", map[string]string{TokenHeader: token})
	if resp.StatusCode != http.StatusPaymentRequired || h.last.query != "x=1&y=2" || h.last.verdict != string(Absent) {
		t.Fatalf("the query: %d %+v", resp.StatusCode, h.last)
	}
}

func TestEveryWayABootSignatureFailsHasItsVerdict(t *testing.T) {
	h := newHarness(t)
	body := authorizeBody
	good := h.header("boot-1", http.MethodPost, authorizePath, []byte(body))
	cases := []struct {
		name, header string
		want         Verdict
	}{
		{"verified", good, Verified},
		{"another body", h.header("boot-1", http.MethodPost, authorizePath, []byte(`{"api_key_lookup_hash":"other"}`)), Invalid},
		{"another path", h.header("boot-1", http.MethodPost, heartbeatPath, []byte(body)), Invalid},
		{"another method", h.header("boot-1", http.MethodGet, authorizePath, []byte(body)), Invalid},
		{"unknown boot", h.header("boot-9", http.MethodPost, authorizePath, []byte(body)), Invalid},
		{"unverified boot", h.header("boot-2", http.MethodPost, authorizePath, []byte(body)), Invalid},
		{"malformed", "kid=boot-1", Invalid},
		{"duplicate field", "kid=boot-1,kid=boot-1,sig=AA", Invalid},
		{"short signature", "kid=boot-1,sig=AAAA", Invalid},
		{"signature with stray padding", good + "===", Invalid},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			resp := h.send(t, http.MethodPost, authorizePath, body, map[string]string{TokenHeader: token, BootHeader: c.header})
			if resp.StatusCode != http.StatusPaymentRequired {
				t.Fatalf("not passed: %d", resp.StatusCode)
			}
			if h.last.verdict != string(c.want) {
				t.Fatalf("verdict %q, want %q", h.last.verdict, c.want)
			}
		})
	}
}

func TestASignatureInTheStandardAlphabetVerifiesAsItDoesInPython(t *testing.T) {
	h := newHarness(t)
	sig := ed25519.Sign(h.private, Digest(http.MethodPost, authorizePath, []byte(authorizeBody)))
	for _, encoded := range []string{base64.StdEncoding.EncodeToString(sig), base64.RawStdEncoding.EncodeToString(sig),
		base64.URLEncoding.EncodeToString(sig)} {
		h.send(t, http.MethodPost, authorizePath, authorizeBody, map[string]string{TokenHeader: token, BootHeader: "kid=boot-1,sig=" + encoded})
		if h.last.verdict != string(Verified) {
			t.Fatalf("%s: %q", encoded, h.last.verdict)
		}
	}
}

func TestTwoBootHeadersAreInvalidAsPythonReadsThem(t *testing.T) {
	h := newHarness(t)
	req, _ := http.NewRequest(http.MethodPost, h.server.URL+authorizePath, strings.NewReader(authorizeBody))
	req.Header.Set(TokenHeader, token)
	req.Header.Add(BootHeader, h.header("boot-1", http.MethodPost, authorizePath, []byte(authorizeBody)))
	req.Header.Add(BootHeader, h.header("boot-1", http.MethodPost, authorizePath, []byte(authorizeBody)))
	resp, err := plainClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if h.last.verdict != string(Invalid) || len(h.last.headers.Values(BootHeader)) != 2 {
		t.Fatalf("two headers: %q, passed %d", h.last.verdict, len(h.last.headers.Values(BootHeader)))
	}
}

func TestAGetIsSignedOverAnEmptyBody(t *testing.T) {
	h := newHarness(t)
	path := "/internal/gateway/authorizations/gwa-1/disposition"
	resp := h.signed(t, "boot-1", http.MethodGet, path, "", nil)
	if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != string(Verified) || h.last.method != http.MethodGet {
		t.Fatalf("a signed GET: %d %+v", resp.StatusCode, h.last)
	}
}

func TestTheTokenIsReadAsPythonReadsIt(t *testing.T) {
	h := newHarness(t)
	boot := h.header("boot-1", http.MethodPost, authorizePath, []byte(authorizeBody))
	// Without the token, or with a wrong one: passed untouched, nothing read.
	for _, headers := range []map[string]string{
		{BootHeader: boot},
		{TokenHeader: "wrong", BootHeader: boot},
		// A bearer token is read first: a wrong one is not rescued by the header.
		{"Authorization": "Bearer wrong", TokenHeader: token, BootHeader: boot},
	} {
		resp := h.send(t, http.MethodPost, authorizePath, authorizeBody, headers)
		if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != "" || h.boots.reads.Load() != 0 {
			t.Fatalf("%v: %d verdict %q reads %d", headers, resp.StatusCode, h.last.verdict, h.boots.reads.Load())
		}
	}
	// The right bearer token with a wrong header is the token, as in Python.
	h.send(t, http.MethodPost, authorizePath, authorizeBody, map[string]string{"Authorization": "Bearer " + token, TokenHeader: "wrong", BootHeader: boot})
	if h.last.verdict != string(Verified) {
		t.Fatalf("a bearer token: verdict %q", h.last.verdict)
	}
}

func TestACallerCannotForgeOrSuppressTheVerdict(t *testing.T) {
	h := newHarness(t)
	h.send(t, http.MethodPost, authorizePath, `{}`, map[string]string{TokenHeader: token, VerdictHeader: "verified"})
	if h.last.verdict != string(Absent) {
		t.Fatalf("a forged verdict: %q", h.last.verdict)
	}
	h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, map[string]string{"Connection": "close, " + VerdictHeader})
	if h.last.verdict != string(Verified) {
		t.Fatalf("a suppressed verdict: %q", h.last.verdict)
	}
}

func TestABootReaderThatIsAwayOrSlowLeavesTheVerdictUnread(t *testing.T) {
	h := newHarness(t)
	h.boots.fail = true
	h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	if h.last.verdict != string(Unread) {
		t.Fatalf("boots away: %q", h.last.verdict)
	}
	h.boots.fail = false
	h.boots.slow = time.Second
	started := time.Now()
	h.signed(t, "boot-5", http.MethodPost, authorizePath, authorizeBody, nil)
	if h.last.verdict != string(Unread) || time.Since(started) > 800*time.Millisecond {
		t.Fatalf("a slow boot read: %q after %v", h.last.verdict, time.Since(started))
	}
}

func TestABootIsReadOnceWithinItsTTLAndAMissForATenth(t *testing.T) {
	h := newHarness(t)
	for range 3 {
		h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	}
	if h.boots.reads.Load() != 1 {
		t.Fatalf("reads within the TTL: %d", h.boots.reads.Load())
	}
	h.now = h.now.Add(10*time.Minute + time.Second)
	h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, nil)
	if h.boots.reads.Load() != 2 {
		t.Fatalf("reads past the TTL: %d", h.boots.reads.Load())
	}
	h.signed(t, "boot-9", http.MethodPost, authorizePath, authorizeBody, nil)
	h.signed(t, "boot-9", http.MethodPost, authorizePath, authorizeBody, nil)
	if h.boots.reads.Load() != 3 {
		t.Fatalf("a miss within a tenth: %d", h.boots.reads.Load())
	}
	h.now = h.now.Add(time.Minute + time.Second)
	h.signed(t, "boot-9", http.MethodPost, authorizePath, authorizeBody, nil)
	if h.boots.reads.Load() != 4 {
		t.Fatalf("a miss past a tenth: %d", h.boots.reads.Load())
	}
}

func TestABodyPastTheBoundIsPassedUnchecked(t *testing.T) {
	h := newHarness(t)
	body := strings.Repeat("x", maxBody+1)
	resp := h.send(t, http.MethodPost, "/internal/gateway/settle", body, map[string]string{TokenHeader: token, BootHeader: "kid=boot-1,sig=AA"})
	if resp.StatusCode != http.StatusPaymentRequired || h.last.verdict != string(Unchecked) || len(h.last.body) != len(body) {
		t.Fatalf("a large body: %d %q %d", resp.StatusCode, h.last.verdict, len(h.last.body))
	}
}

func TestPythonsCompressedAnswerPassesAsItsBytes(t *testing.T) {
	h := newHarness(t)
	h.respond = func(w http.ResponseWriter) {
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Set("Content-Length", "5")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("gzip!"))
	}
	resp := h.send(t, http.MethodPost, authorizePath, authorizeBody, map[string]string{TokenHeader: token})
	got, _ := io.ReadAll(resp.Body)
	if resp.Header.Get("Content-Encoding") != "gzip" || resp.Header.Get("Content-Length") != "5" || string(got) != "gzip!" {
		t.Fatalf("the bytes changed: %v %q", resp.Header, got)
	}
	if h.last.headers.Get("Accept-Encoding") != "" {
		t.Fatalf("the edge negotiated %q on the enclave's behalf", h.last.headers.Get("Accept-Encoding"))
	}
}

func TestAPythonThatIsNotReachedAnswersInItsEnvelope(t *testing.T) {
	h := newHarness(t)
	h.upstream.Close()
	resp := h.send(t, http.MethodPost, authorizePath, authorizeBody, map[string]string{TokenHeader: token})
	var body struct {
		Error struct {
			Code    int    `json:"code"`
			Type    string `json:"type"`
			Message string `json:"message"`
			Source  string `json:"source"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil || resp.StatusCode != http.StatusBadGateway ||
		body.Error.Code != 502 || body.Error.Type != "bad_gateway" || body.Error.Message == "" || body.Error.Source != "router" {
		t.Fatalf("python not reached: %d %+v %v", resp.StatusCode, body, err)
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
	b := &boots{}
	for _, cfg := range []Config{
		{Upstream: upstream, Boots: b},
		{Token: token, Boots: b},
		{Token: token, Upstream: upstream},
		{Token: token, Upstream: withPath, Boots: b},
	} {
		if _, err := New(cfg); err == nil {
			t.Fatalf("accepted %+v", cfg)
		}
	}
}

func TestAQueryWithASemicolonReachesPythonAsItCame(t *testing.T) {
	h := newHarness(t)
	h.send(t, http.MethodPost, "/internal/gateway/settle-outbox/drain?limit=1;ignored=2", `{}`, map[string]string{TokenHeader: token})
	if h.last.query != "limit=1;ignored=2" {
		t.Fatalf("the query: %q", h.last.query)
	}
}

func TestABootHeaderNamedAsHopByHopIsAbsentForPython(t *testing.T) {
	h := newHarness(t)
	h.signed(t, "boot-1", http.MethodPost, authorizePath, authorizeBody, map[string]string{"Connection": "close, " + BootHeader})
	if h.last.verdict != string(Absent) || h.last.headers.Get(BootHeader) != "" || h.boots.reads.Load() != 0 {
		t.Fatalf("a hop-by-hop boot header: verdict %q, python saw %q, reads %d", h.last.verdict, h.last.headers.Get(BootHeader), h.boots.reads.Load())
	}
}

func TestAPathBeyondPrintableASCIIIsUnchecked(t *testing.T) {
	h := newHarness(t)
	for _, path := range []string{"/internal/gateway/authorize%FF", "/internal/gateway/authorize%0A", "/internal/gateway/authoriz%C3%A9"} {
		header := h.header("boot-1", http.MethodPost, path, []byte(authorizeBody))
		h.send(t, http.MethodPost, path, authorizeBody, map[string]string{TokenHeader: token, BootHeader: header})
		if h.last.verdict != string(Unchecked) {
			t.Fatalf("%s: %q", path, h.last.verdict)
		}
	}
}

func TestTheHeaderIsStrippedAsPythonStripsLatin1(t *testing.T) {
	h := newHarness(t)
	good := h.header("boot-1", http.MethodPost, authorizePath, []byte(authorizeBody))
	// A no-break space byte (A0) and NEL (85) are whitespace to str.strip()
	// on a Latin-1 decoded header; the UTF-8 no-break space (C2 A0) is two
	// characters there, neither stripped as a pair.
	cases := map[string]Verdict{
		good + "\xa0":     Verified,
		"\x85" + good:     Verified,
		good + " \t":      Verified,
		good + "\xc2\xa0": Invalid,
	}
	for header, want := range cases {
		req, _ := http.NewRequest(http.MethodPost, h.server.URL+authorizePath, strings.NewReader(authorizeBody))
		req.Header.Set(TokenHeader, token)
		req.Header[BootHeader] = []string{header}
		resp, err := plainClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if h.last.verdict != string(want) {
			t.Fatalf("%q: %q, want %q", header, h.last.verdict, want)
		}
	}
}

func TestAnOversizedBodyKeepsItsDeclaredLength(t *testing.T) {
	h := newHarness(t)
	body := strings.Repeat("x", maxBody+1)
	req, _ := http.NewRequest(http.MethodPost, h.server.URL+"/internal/gateway/settle", strings.NewReader(body))
	req.Header.Set(TokenHeader, token)
	req.Header.Set(BootHeader, "kid=boot-1,sig=AA")
	req.ContentLength = int64(len(body))
	resp, err := plainClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if h.last.headers.Get("Content-Length") != fmt.Sprint(len(body)) || len(h.last.body) != len(body) || h.last.verdict != string(Unchecked) {
		t.Fatalf("an oversized body: length %q, got %d, verdict %q", h.last.headers.Get("Content-Length"), len(h.last.body), h.last.verdict)
	}
}
