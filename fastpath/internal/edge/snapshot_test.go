package edge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

const snapshotPath = "/internal/fastpath/routing-snapshot"

func testdata(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// goldenBody is the golden snapshot body and the instants its manifest
// binds.
type goldenBody struct {
	raw            []byte
	compiledAt     int64
	notAfter       int64
	sourceRevision string
	plane          routingsnapshot.Plane
}

func loadGoldenBody(t *testing.T) goldenBody {
	t.Helper()
	raw := testdata(t, "routing_snapshot_v2.body.json")
	var fields struct {
		CompiledAt     int64                 `json:"compiled_at"`
		NotAfter       int64                 `json:"not_after"`
		SourceRevision string                `json:"source_revision"`
		Plane          routingsnapshot.Plane `json:"plane"`
	}
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	return goldenBody{raw: raw, compiledAt: fields.CompiledAt, notAfter: fields.NotAfter,
		sourceRevision: fields.SourceRevision, plane: fields.Plane}
}

func b64url(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// sign is a manifest over body by a key the test made, as the control
// plane's signer makes one (routing_snapshot_signing.py): never the public
// test key, which the edge refuses.
func sign(t *testing.T, private ed25519.PrivateKey, kid string, g goldenBody, body []byte) string {
	t.Helper()
	sum := sha256.Sum256(body)
	header := fmt.Sprintf(`{"alg":"EdDSA","kid":%q,"typ":%q}`, kid, routingsnapshot.Typ)
	payload, err := json.Marshal(routingsnapshot.Claims{Aud: SnapshotAudience, CompiledAt: g.compiledAt, Iss: SnapshotIssuer,
		NotAfter: g.notAfter, Plane: g.plane, SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body)),
		SourceRevision: g.sourceRevision, V: 2})
	if err != nil {
		t.Fatal(err)
	}
	signing := b64url([]byte(header)) + "." + b64url(payload)
	return signing + "." + b64url(ed25519.Sign(private, []byte(signing)))
}

func publication(manifest string, body []byte, refreshAt int64) []byte {
	out, _ := json.Marshal(map[string]any{"v": 1, "kind": publicationKind, "manifest": manifest,
		"body": base64.StdEncoding.EncodeToString(body), "refresh_at": refreshAt})
	return out
}

// pythonSide is the edge's upstream: the snapshot route and authorize, each
// answered as the test says.
type pythonSide struct {
	server    *httptest.Server
	snapshot  atomic.Pointer[func(w http.ResponseWriter, r *http.Request)]
	authorize atomic.Pointer[func(w http.ResponseWriter, r *http.Request)]
	polls     atomic.Int64
	mu        sync.Mutex
	seen      []http.Header
	bodies    [][]byte
}

func newPythonSide(t *testing.T) *pythonSide {
	t.Helper()
	py := &pythonSide{}
	py.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		py.mu.Lock()
		py.seen = append(py.seen, r.Header.Clone())
		py.bodies = append(py.bodies, body)
		py.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == snapshotPath:
			py.polls.Add(1)
			if f := py.snapshot.Load(); f != nil {
				(*f)(w, r)
				return
			}
			w.WriteHeader(http.StatusNotFound)
		case r.Method == http.MethodPost && authorizeRoutes[r.URL.Path]:
			if f := py.authorize.Load(); f != nil {
				(*f)(w, r)
				return
			}
			w.WriteHeader(http.StatusTeapot)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(py.server.Close)
	return py
}

func (py *pythonSide) onSnapshot(f func(w http.ResponseWriter, r *http.Request)) {
	py.snapshot.Store(&f)
}
func (py *pythonSide) onAuthorize(f func(w http.ResponseWriter, r *http.Request)) {
	py.authorize.Store(&f)
}

func (py *pythonSide) lastHeaders() http.Header {
	py.mu.Lock()
	defer py.mu.Unlock()
	return py.seen[len(py.seen)-1]
}

// clock is a settable instant every part of a test edge reads.
type clock struct{ nanos atomic.Int64 }

func (c *clock) now() time.Time    { return time.Unix(0, c.nanos.Load()).UTC() }
func (c *clock) set(t time.Time)   { c.nanos.Store(t.UnixNano()) }
func (c *clock) setUnix(sec int64) { c.set(time.Unix(sec, 0)) }

// logs keeps what a test edge logs.
type logs struct {
	mu    sync.Mutex
	lines []string
}

func (l *logs) logf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.lines = append(l.lines, fmt.Sprintf(format, args...))
}

func (l *logs) count() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.lines)
}

func (l *logs) text() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Join(l.lines, "\n")
}

// snapshotEdge is an edge with the routing snapshot configured against a
// fake Python, signed by a key the test made.
type snapshotEdge struct {
	edge    *Edge
	python  *pythonSide
	clock   *clock
	logs    *logs
	golden  goldenBody
	private ed25519.PrivateKey
	kid     string
	server  *httptest.Server
	boots   *boots
}

func newSnapshotEdge(t *testing.T, shadowOn bool, configure func(*Config)) *snapshotEdge {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	h := &snapshotEdge{python: newPythonSide(t), clock: &clock{}, logs: &logs{}, golden: loadGoldenBody(t),
		private: private, kid: "routing-snapshot-key-test-run"}
	h.clock.setUnix(h.golden.compiledAt + 1800)
	bootPublic, bootPrivate, _ := ed25519.GenerateKey(rand.Reader)
	_ = bootPrivate
	h.boots = &boots{by: map[string]Boot{"boot-1": {Kid: "boot-1", PublicKey: bootPublic, Verified: true}}}
	upstream, _ := url.Parse(h.python.server.URL)
	snapshotURL, _ := url.Parse(h.python.server.URL + snapshotPath)
	cfg := Config{Token: token, Upstream: upstream, Boots: h.boots, BootWait: 200 * time.Millisecond,
		Clock: h.clock.now, Logf: h.logs.logf,
		Snapshot: SnapshotConfig{URL: snapshotURL, Keys: []SnapshotKey{{Kid: h.kid, PublicKey: public}}, Plane: "gcp/us-central1",
			Shadow: shadowOn, Timeout: 5 * time.Second}}
	if configure != nil {
		configure(&cfg)
	}
	e, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	h.edge = e
	h.server = httptest.NewServer(e)
	t.Cleanup(h.server.Close)
	return h
}

// serveGolden has Python publish the golden body under the test's key,
// answering 304 to a matching If-None-Match as the route does.
func (h *snapshotEdge) serveGolden(t *testing.T, refreshAt int64) string {
	t.Helper()
	manifest := sign(t, h.private, h.kid, h.golden, h.golden.raw)
	sum := sha256.Sum256(h.golden.raw)
	etag := `"` + hex.EncodeToString(sum[:]) + `"`
	answer := publication(manifest, h.golden.raw, refreshAt)
	h.python.onSnapshot(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", etag)
		w.Header().Set("Cache-Control", "no-store")
		if r.Header.Get(TokenHeader) != token {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if r.Header.Get("If-None-Match") == etag {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(answer)
	})
	return hex.EncodeToString(sum[:])
}

func (h *snapshotEdge) poll(t *testing.T) time.Duration {
	t.Helper()
	return h.edge.poller.poll(context.Background())
}

// TestThePollInstallsAVerifiedSnapshotThenAsksWithItsETag: the first poll
// carries the internal token and installs what verifies under the pinned
// key; the next sends the body's sha256 as If-None-Match, and Python's 304
// keeps the copy held, the same one, waiting until refresh_at.
func TestThePollInstallsAVerifiedSnapshotThenAsksWithItsETag(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	now := h.clock.now().Unix()
	digest := h.serveGolden(t, now+40)
	if h.edge.poller.snapshot() != nil {
		t.Fatal("a snapshot before any poll")
	}
	if wait := h.poll(t); wait != 40*time.Second {
		t.Fatalf("the wait after a publication is until refresh_at: %v", wait)
	}
	first := h.edge.poller.snapshot()
	if first == nil || first.claims.SHA256 != digest {
		t.Fatalf("the verified snapshot is not held: %+v", first)
	}
	if got := h.python.lastHeaders(); got.Get(TokenHeader) != token || got.Get("If-None-Match") != "" {
		t.Fatalf("the first poll's headers: %v", got)
	}
	if wait := h.poll(t); wait != 40*time.Second {
		t.Fatalf("the wait after a 304: %v", wait)
	}
	if got := h.python.lastHeaders().Get("If-None-Match"); got != `"`+digest+`"` {
		t.Fatalf("If-None-Match %q, want the body's sha256", got)
	}
	if h.edge.poller.snapshot() != first {
		t.Fatal("a 304 replaced the snapshot held")
	}
	logged := h.logs.text()
	if !strings.Contains(logged, "outcome=installed sha256="+digest) || !strings.Contains(logged, "status=304 outcome=unchanged") {
		t.Fatalf("the poll's outcomes are not logged:\n%s", logged)
	}
	// A 304 past refresh_at says Python has nothing newer: the longest wait.
	h.clock.setUnix(now + 41)
	if wait := h.poll(t); wait != time.Minute {
		t.Fatalf("a 304 past refresh_at waited %v", wait)
	}
	h.clock.setUnix(now)
	// The waits after a publication stay within their bounds: refresh_at
	// past is the shortest, far off the longest.
	for _, c := range []struct {
		refreshAt int64
		want      time.Duration
	}{{now - 10, 5 * time.Second}, {now + 7200, time.Minute}} {
		h.edge.poller.current.Store(nil)
		h.serveGolden(t, c.refreshAt)
		if wait := h.poll(t); wait != c.want {
			t.Fatalf("refresh_at %d waited %v, want %v", c.refreshAt-now, wait, c.want)
		}
	}
}

// TestRetryAfterIsHonouredAndTheCopyKept: a 503 says when to ask again, in
// seconds or as a date, and the snapshot held stays; a 503 without one backs
// off.
func TestRetryAfterIsHonouredAndTheCopyKept(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	h.serveGolden(t, h.clock.now().Unix()+60)
	h.poll(t)
	held := h.edge.poller.snapshot()
	if held == nil {
		t.Fatal("no snapshot installed")
	}
	retry := "120"
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", retry)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"status":"snapshot-refused","retry_at":1792371600}`)
	})
	if wait := h.poll(t); wait != 120*time.Second {
		t.Fatalf("Retry-After: 120 waited %v", wait)
	}
	if h.edge.poller.snapshot() != held {
		t.Fatal("a 503 dropped the snapshot held")
	}
	retry = h.clock.now().Add(300 * time.Second).Format(http.TimeFormat)
	if wait := h.poll(t); wait != 300*time.Second {
		t.Fatalf("Retry-After as a date waited %v", wait)
	}
	// Positive control: without Retry-After the wait is the backoff, not 120.
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"status":"signer-unavailable"}`)
	})
	if wait := h.poll(t); wait == 120*time.Second || wait > time.Minute {
		t.Fatalf("a 503 without Retry-After waited %v", wait)
	}
	logged := h.logs.text()
	for _, want := range []string{"status=503 outcome=unavailable publisher_status=snapshot-refused retry_after=2m0s",
		"publisher_status=signer-unavailable", "kept=" + held.claims.SHA256} {
		if !strings.Contains(logged, want) {
			t.Fatalf("%q is not logged:\n%s", want, logged)
		}
	}
	if h.edge.poller.snapshot() != held {
		t.Fatal("the snapshot held was dropped")
	}
}

// TestAPublicationThatDoesNotVerifyKeepsTheLastGoodCopy: a manifest under
// another key, the public test key's golden manifest, a body changed after
// signing, another plane, and a publication that is not v1's shape are each
// refused; the copy held stays, and the refusal is logged by its reason.
func TestAPublicationThatDoesNotVerifyKeepsTheLastGoodCopy(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	refresh := h.clock.now().Unix() + 60
	h.serveGolden(t, refresh)
	h.poll(t)
	held := h.edge.poller.snapshot()
	if held == nil {
		t.Fatal("no snapshot installed")
	}
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	var goldenManifest struct {
		JWS string `json:"jws"`
	}
	if err := json.Unmarshal(testdata(t, "routing_snapshot_v2.manifest.json"), &goldenManifest); err != nil {
		t.Fatal(err)
	}
	tampered := append([]byte(nil), h.golden.raw...)
	tampered[len(tampered)-2] = ' '
	europe := h.golden
	europe.plane = routingsnapshot.Plane{Cloud: "gcp", Region: "europe-west4"}
	for _, c := range []struct {
		name, reason string
		answer       []byte
	}{
		{"another key", "signature", publication(sign(t, other, h.kid, h.golden, h.golden.raw), h.golden.raw, refresh)},
		{"the test key", "key", publication(goldenManifest.JWS, h.golden.raw, refresh)},
		{"a changed body", "manifest binding", publication(sign(t, h.private, h.kid, h.golden, h.golden.raw), tampered, refresh)},
		{"another plane", "manifest plane", publication(sign(t, h.private, h.kid, europe, h.golden.raw), h.golden.raw, refresh)},
		{"not v1", "the publication has fields beyond v1's", []byte(`{"v":1,"kind":"tr-routing-snapshot-publication",` +
			`"manifest":"a.b.c","body":"","refresh_at":1,"extra":1}`)},
	} {
		answer := c.answer
		h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(answer) })
		before := h.logs.count()
		h.poll(t)
		if h.edge.poller.snapshot() != held {
			t.Fatalf("%s: the last good copy was replaced or dropped", c.name)
		}
		h.logs.mu.Lock()
		line := strings.Join(h.logs.lines[before:], "\n")
		h.logs.mu.Unlock()
		if !strings.Contains(line, "outcome=refused reason=") || !strings.Contains(line, c.reason) ||
			!strings.Contains(line, "kept="+held.claims.SHA256) {
			t.Fatalf("%s: the refusal is not logged with %q: %s", c.name, c.reason, line)
		}
	}
	// Positive control: the same poll with the good publication verifies.
	h.serveGolden(t, refresh)
	h.edge.poller.current.Store(nil)
	h.poll(t)
	if h.edge.poller.snapshot() == nil {
		t.Fatal("the good publication no longer verifies")
	}
}

// TestARotationTrustsBothPinnedKeys: with the old key and the new pinned,
// a snapshot signed with either installs, and one signed with neither does
// not.
func TestARotationTrustsBothPinnedKeys(t *testing.T) {
	next, nextPrivate, _ := ed25519.GenerateKey(rand.Reader)
	h := newSnapshotEdge(t, false, func(c *Config) {
		c.Snapshot.Keys = append(c.Snapshot.Keys, SnapshotKey{Kid: "routing-snapshot-key-next", PublicKey: next})
	})
	refresh := h.clock.now().Unix() + 60
	h.serveGolden(t, refresh)
	h.poll(t)
	if h.edge.poller.snapshot() == nil {
		t.Fatal("the old key's snapshot did not install")
	}
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	for _, c := range []struct {
		name    string
		private ed25519.PrivateKey
		kid     string
		install bool
	}{
		{"the new key", nextPrivate, "routing-snapshot-key-next", true},
		{"neither key", other, "routing-snapshot-key-next", false},
	} {
		answer := publication(sign(t, c.private, c.kid, h.golden, h.golden.raw), h.golden.raw, refresh)
		h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(answer) })
		h.edge.poller.current.Store(nil)
		h.poll(t)
		if got := h.edge.poller.snapshot() != nil; got != c.install {
			t.Fatalf("%s: installed %v", c.name, got)
		}
	}
}

// TestTheTestKeyIsNeverTrustedAndTheSnapshotComesFromTheUpstream: the edge
// refuses to start with the public test key, by kid or by public key, or a
// snapshot URL on another host than its upstream, which would carry the
// internal token elsewhere.
func TestTheTestKeyIsNeverTrustedAndTheSnapshotComesFromTheUpstream(t *testing.T) {
	testKey, err := base64.RawURLEncoding.DecodeString(routingsnapshot.TestPublicKey)
	if err != nil {
		t.Fatal(err)
	}
	h := newSnapshotEdge(t, true, nil)
	base := h.edge.cfg
	for _, c := range []struct {
		name      string
		configure func(*Config)
	}{
		{"the test key", func(c *Config) { c.Snapshot.Keys = []SnapshotKey{{Kid: "k", PublicKey: testKey}} }},
		{"the test kid", func(c *Config) {
			c.Snapshot.Keys = []SnapshotKey{{Kid: routingsnapshot.TestKID, PublicKey: c.Snapshot.Keys[0].PublicKey}}
		}},
		{"the test key beside a good one", func(c *Config) {
			c.Snapshot.Keys = append(slices.Clone(c.Snapshot.Keys), SnapshotKey{Kid: "k2", PublicKey: testKey})
		}},
		{"two keys of one kid", func(c *Config) {
			c.Snapshot.Keys = append(slices.Clone(c.Snapshot.Keys), c.Snapshot.Keys[0])
		}},
		{"another scheme", func(c *Config) { c.Snapshot.URL, _ = url.Parse("https://" + c.Upstream.Host + snapshotPath) }},
		// The same scheme, so only the host check refuses these two.
		{"another port", func(c *Config) {
			c.Snapshot.URL, _ = url.Parse("http://" + c.Upstream.Hostname() + ":1" + snapshotPath)
		}},
		{"another host", func(c *Config) {
			c.Snapshot.URL, _ = url.Parse("http://elsewhere.example:" + c.Upstream.Port() + snapshotPath)
		}},
		{"a user in the URL", func(c *Config) { c.Snapshot.URL, _ = url.Parse("http://u@" + c.Upstream.Host + snapshotPath) }},
		{"a user in the upstream", func(c *Config) {
			upstream := *c.Upstream
			upstream.User = url.User("u")
			c.Upstream = &upstream
		}},
		{"no plane", func(c *Config) { c.Snapshot.Plane = "gcp" }},
		{"no key", func(c *Config) { c.Snapshot.Keys = nil }},
		{"a short key", func(c *Config) { c.Snapshot.Keys = []SnapshotKey{{Kid: "k", PublicKey: testKey[:31]}} }},
		{"a shadow with no snapshot", func(c *Config) { c.Snapshot.URL = nil }},
	} {
		cfg := base
		c.configure(&cfg)
		if _, err := New(cfg); err == nil || !strings.HasPrefix(err.Error(), "edge: ") {
			t.Errorf("New accepted %s: %v", c.name, err)
		}
	}
	// Positive control: the configuration the cases change is accepted.
	if _, err := New(base); err != nil {
		t.Fatalf("the base configuration: %v", err)
	}
}

// TestARedirectNeverCarriesTheToken: the upstream answers the poll with a
// redirect to another server; the poll does not follow it, so the other
// server never sees the internal token, and the poll counts as failed.
func TestARedirectNeverCarriesTheToken(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	var reached atomic.Int64
	var tokens atomic.Int64
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Add(1)
		if r.Header.Get(TokenHeader) != "" || strings.Contains(fmt.Sprint(r.Header), token) {
			tokens.Add(1)
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	t.Cleanup(elsewhere.Close)
	h.python.onSnapshot(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+snapshotPath, http.StatusFound)
	})
	h.poll(t)
	if reached.Load() != 0 || tokens.Load() != 0 {
		t.Fatalf("the redirect was followed: reached %d, with the token %d", reached.Load(), tokens.Load())
	}
	if h.edge.poller.failures != 1 || !strings.Contains(h.logs.text(), "poll status=302 outcome=unavailable") {
		t.Fatalf("the redirect did not count as a failed poll (%d):\n%s", h.edge.poller.failures, h.logs.text())
	}
	// Positive control: a client that follows redirects would carry the
	// token there.
	req, _ := http.NewRequest(http.MethodGet, h.python.server.URL+snapshotPath, nil)
	req.Header.Set(TokenHeader, token)
	if resp, err := http.DefaultClient.Do(req); err == nil {
		resp.Body.Close()
	}
	if tokens.Load() != 1 {
		t.Fatalf("the control did not reach the other server with the token: %d", tokens.Load())
	}
}

// TestALongRetryAfterWithNothingHeldWaitsTheLongestAtMost: with a snapshot
// held, Retry-After is honoured whole; with none, the edge asks again within
// the longest wait rather than going without for the Retry-After.
func TestALongRetryAfterWithNothingHeldWaitsTheLongestAtMost(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "3600")
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	if wait := h.poll(t); wait != time.Minute {
		t.Fatalf("nothing held: Retry-After 3600 waited %v", wait)
	}
	h.serveGolden(t, h.clock.now().Unix()+60)
	h.poll(t)
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "3600")
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	if wait := h.poll(t); wait != time.Hour {
		t.Fatalf("a snapshot held: Retry-After 3600 waited %v", wait)
	}
}

// TestTheSnapshotIsDroppedAtItsNotAfter: held until the second before
// not_after, gone at it, and the memory freed once the poll loop sees it;
// the next poll asks for the whole body.
func TestTheSnapshotIsDroppedAtItsNotAfter(t *testing.T) {
	h := newSnapshotEdge(t, false, nil)
	h.serveGolden(t, h.golden.notAfter)
	h.poll(t)
	if h.edge.poller.snapshot() == nil {
		t.Fatal("no snapshot installed")
	}
	h.clock.setUnix(h.golden.notAfter - 1)
	h.edge.poller.expire()
	if h.edge.poller.snapshot() == nil || h.edge.poller.current.Load() == nil {
		t.Fatal("dropped before its not_after")
	}
	h.clock.setUnix(h.golden.notAfter)
	if h.edge.poller.snapshot() != nil {
		t.Fatal("read at its not_after")
	}
	h.edge.poller.expire()
	if h.edge.poller.current.Load() != nil {
		t.Fatal("kept in memory past its not_after")
	}
	if !strings.Contains(h.logs.text(), "outcome=installed") || !strings.Contains(h.logs.text(), "expired sha256=") {
		t.Fatalf("the expiry is not logged:\n%s", h.logs.text())
	}
	// Past not_after the golden manifest is stale, so the poll is refused,
	// and it asked without If-None-Match.
	h.poll(t)
	if got := h.python.lastHeaders().Get("If-None-Match"); got != "" {
		t.Fatalf("a poll with nothing held sent If-None-Match %q", got)
	}
	if h.edge.poller.snapshot() != nil {
		t.Fatal("a stale snapshot was installed")
	}
	if !strings.Contains(h.logs.text(), "snapshot freshness") {
		t.Fatalf("the stale publication's refusal is not logged:\n%s", h.logs.text())
	}
}

// TestThePollNeverHoldsUpTheProxy: with the snapshot route hung, the edge's
// Run polling it, authorizes pass through at once with Python's answer.
func TestThePollNeverHoldsUpTheProxy(t *testing.T) {
	h := newSnapshotEdge(t, true, nil)
	h.serveGolden(t, h.clock.now().Unix()+60)
	h.poll(t)
	if h.edge.poller.snapshot() == nil {
		t.Fatal("no snapshot installed")
	}
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	h.python.onSnapshot(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-release:
		}
	})
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = io.WriteString(w, `{"error":{"code":402,"message":"Insufficient credits","type":"insufficient_credits"}}`)
	})
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() {
		h.edge.Run(ctx)
		close(stopped)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for h.python.polls.Load() < 2 {
		if time.Now().After(deadline) {
			t.Fatal("Run did not poll")
		}
		time.Sleep(time.Millisecond)
	}
	started := time.Now()
	for i := 0; i < 20; i++ {
		resp := h.post(t, `{"model":"deepseek/deepseek-v4-flash","estimated_input_tokens":10}`, nil)
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusPaymentRequired {
			t.Fatalf("Python's answer did not pass: %d", resp.StatusCode)
		}
	}
	if elapsed := time.Since(started); elapsed > 3*time.Second {
		t.Fatalf("authorizes waited on the hung poll: %v", elapsed)
	}
	cancel()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not stop with its context")
	}
}

// post sends an authorize through the edge with the internal token.
func (h *snapshotEdge) post(t *testing.T, body string, headers map[string]string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, h.server.URL+"/internal/gateway/authorize", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(TokenHeader, token)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := plainClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}
