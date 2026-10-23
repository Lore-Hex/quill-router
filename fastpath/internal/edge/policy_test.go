package edge

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

const (
	policyPath = "/internal/fastpath/edge-policy"
	// goldenIssuedAt is edge_policy_v1.body.json's issued_at.
	goldenIssuedAt = 1792368000
)

// policyEdge is an edge with the edge policy configured against a fake
// Python, signed by a key the test made under the production issuer and
// audience (the golden files' TEST key is never trusted by an edge).
type policyEdge struct {
	edge    *Edge
	server  *httptest.Server
	clock   *clock
	logs    *logs
	golden  []byte
	private ed25519.PrivateKey
	kid     string
	polls   atomic.Int64
	handler atomic.Pointer[func(w http.ResponseWriter, r *http.Request)]
}

func newPolicyEdge(t *testing.T, configure func(*Config)) *policyEdge {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	h := &policyEdge{clock: &clock{}, logs: &logs{}, golden: testdata(t, "edge_policy_v1.body.json"),
		private: private, kid: "routing-snapshot-key-test-run"}
	h.clock.setUnix(goldenIssuedAt)
	h.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != policyPath {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		h.polls.Add(1)
		if r.Header.Get(TokenHeader) != token {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if f := h.handler.Load(); f != nil {
			(*f)(w, r)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(h.server.Close)
	upstream, _ := url.Parse(h.server.URL)
	policyURL, _ := url.Parse(h.server.URL + policyPath)
	cfg := Config{Token: token, Upstream: upstream, Boots: &boots{by: map[string]Boot{}}, Clock: h.clock.now, Logf: h.logs.logf,
		Policy: PolicyConfig{URL: policyURL, Keys: []SnapshotKey{{Kid: h.kid, PublicKey: public}}, Plane: "gcp/us-central1"}}
	if configure != nil {
		configure(&cfg)
	}
	e, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	h.edge = e
	return h
}

// issued is the golden body issued at another instant, its window moved
// with it: what another Cloud Run instance's publisher signs.
func (h *policyEdge) issued(t *testing.T, issuedAt int64) []byte {
	t.Helper()
	out := h.golden
	for _, field := range []struct {
		name   string
		offset int64
	}{{"issued_at", 0}, {"refresh_at", routingsnapshot.PolicyRefreshSeconds}, {"not_after", routingsnapshot.PolicyLifetimeSeconds}} {
		from := fmt.Sprintf(`"%s":%d`, field.name, goldenIssuedAt+field.offset)
		to := fmt.Sprintf(`"%s":%d`, field.name, issuedAt+field.offset)
		if bytes.Count(out, []byte(from)) != 1 {
			t.Fatalf("the golden body has no single %s", from)
		}
		out = bytes.Replace(out, []byte(from), []byte(to), 1)
	}
	return out
}

// manifest signs body as the control plane's EdgePolicySigner does, under
// typ.
func (h *policyEdge) manifest(t *testing.T, typ string, body []byte) string {
	t.Helper()
	policy, err := routingsnapshot.ParseEdgePolicy(body)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	header := fmt.Sprintf(`{"alg":"EdDSA","kid":%q,"typ":%q}`, h.kid, typ)
	payload, err := json.Marshal(routingsnapshot.PolicyClaims{Aud: SnapshotAudience, Iss: SnapshotIssuer,
		IssuedAt: policy.IssuedAt, NotAfter: policy.NotAfter, Plane: policy.Plane,
		SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body)), V: 1})
	if err != nil {
		t.Fatal(err)
	}
	signing := b64url([]byte(header)) + "." + b64url(payload)
	return signing + "." + b64url(ed25519.Sign(h.private, []byte(signing)))
}

// policyPublication is the route's answer, as the publisher writes it.
func policyPublication(manifest string, body []byte) []byte {
	return []byte(fmt.Sprintf(`{"body":%q,"kind":%q,"manifest":%q,"v":1}`,
		base64.StdEncoding.EncodeToString(body), policyPublicationKind, manifest))
}

func sha(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// serve has Python answer every poll with this publication of body,
// answering 304 to a matching If-None-Match as the route does.
func (h *policyEdge) serve(t *testing.T, body []byte, answer []byte) {
	t.Helper()
	etag := `"` + sha(body) + `"`
	f := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", etag)
		w.Header().Set("Cache-Control", "no-store")
		if r.Header.Get("If-None-Match") == etag {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(answer)
	}
	h.handler.Store(&f)
}

// serveIssued publishes the golden body issued at issuedAt, signed.
func (h *policyEdge) serveIssued(t *testing.T, issuedAt int64) []byte {
	t.Helper()
	body := h.issued(t, issuedAt)
	h.serve(t, body, policyPublication(h.manifest(t, routingsnapshot.PolicyTyp, body), body))
	return body
}

// serve503 has Python answer every poll 503 with this publisher status.
func (h *policyEdge) serve503(status string, retryAfter string) {
	f := func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if retryAfter != "" {
			w.Header().Set("Retry-After", retryAfter)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = fmt.Fprintf(w, `{"status":%q,"retry_at":0}`, status)
	}
	h.handler.Store(&f)
}

func (h *policyEdge) poll() time.Duration {
	return h.edge.policy.poll(context.Background())
}

// held is the sha256 of the policy the edge's accessor answers, "" for
// none.
func (h *policyEdge) held(t *testing.T) string {
	t.Helper()
	p, digest := h.edge.Policy()
	if (p == nil) != (digest == "") {
		t.Fatalf("the accessor answered a policy %v with digest %q", p != nil, digest)
	}
	return digest
}

// TestThePolicyPollKeepsTheNewestIssuedAt: the golden body installs and
// the accessor answers it, field for field; an older body from another
// instance (the backwards case) verifies but is ignored, and so is another
// body signed in the same second; a newer one replaces it.
func TestThePolicyPollKeepsTheNewestIssuedAt(t *testing.T) {
	h := newPolicyEdge(t, nil)
	if h.held(t) != "" {
		t.Fatal("a policy before any poll")
	}
	first := h.serveIssued(t, goldenIssuedAt)
	if !bytes.Equal(first, h.golden) {
		t.Fatal("the unmoved golden body must be the golden bytes")
	}
	if wait := h.poll(); wait != 15*time.Second {
		t.Fatalf("installed at issued_at, waited %v, want refresh_at's 15 s", wait)
	}
	if h.held(t) != sha(h.golden) {
		t.Fatalf("the golden policy is not held:\n%s", h.logs.text())
	}
	p, _ := h.edge.Policy()
	if p.IssuedAt != goldenIssuedAt || p.GatewayAuthorizeMaxInFlightPerKey != 4 || !p.FastpathPilotEnabled ||
		len(p.AcceptedImageDigests) != 3 || p.MonitorLookupHash == nil || p.PrimaryRegion != "us-central1" {
		t.Fatalf("the accessor's policy is not the golden one: %+v", p)
	}

	// Backwards: another instance's body, issued 10 s earlier and still
	// fresh, verifies and is ignored.
	h.clock.setUnix(goldenIssuedAt + 5)
	older := h.serveIssued(t, goldenIssuedAt-10)
	if wait := h.poll(); wait != 10*time.Second {
		t.Fatalf("after an older body, waited %v, want the held copy's refresh_at", wait)
	}
	if h.held(t) != sha(h.golden) || h.edge.PolicyCounts().Older != 1 {
		t.Fatalf("the older body %s replaced the newer:\n%s", sha(older), h.logs.text())
	}
	if !strings.Contains(h.logs.text(), "outcome=older sha256="+sha(older)) {
		t.Fatalf("the older body is not logged:\n%s", h.logs.text())
	}

	// The same second, other facts: the copy held stays.
	tie := bytes.Replace(h.golden, []byte(`"gateway_authorize_max_in_flight_per_key":4`),
		[]byte(`"gateway_authorize_max_in_flight_per_key":5`), 1)
	h.serve(t, tie, policyPublication(h.manifest(t, routingsnapshot.PolicyTyp, tie), tie))
	h.poll()
	if h.held(t) != sha(h.golden) || h.edge.PolicyCounts().Older != 2 {
		t.Fatalf("a body of the same issued_at replaced the held copy:\n%s", h.logs.text())
	}

	// Newer: installed.
	h.clock.setUnix(goldenIssuedAt + 16)
	newer := h.serveIssued(t, goldenIssuedAt+15)
	if wait := h.poll(); wait != 14*time.Second {
		t.Fatalf("installed one second after issued_at, waited %v", wait)
	}
	if h.held(t) != sha(newer) || h.edge.PolicyCounts().Installed != 2 {
		t.Fatalf("the newer body is not held:\n%s", h.logs.text())
	}
	// And the older one, polled again, is still ignored.
	h.serve(t, older, policyPublication(h.manifest(t, routingsnapshot.PolicyTyp, older), older))
	h.poll()
	if h.held(t) != sha(newer) {
		t.Fatal("the edge went backwards")
	}
}

// TestThePolicyPollAsksWithItsETag: the second poll carries the body's
// sha256; a 304 keeps the copy. Past refresh_at with nothing newer (the
// instance is still signing, or another answered), the next polls come at
// the shortest wait, 2 s, every time: a backoff would leave a few polls
// before not_after. Real failures still back off.
func TestThePolicyPollAsksWithItsETag(t *testing.T) {
	h := newPolicyEdge(t, nil)
	h.serveIssued(t, goldenIssuedAt)
	h.poll()
	var seen atomic.Value
	inner := h.handler.Load()
	f := func(w http.ResponseWriter, r *http.Request) {
		seen.Store(r.Header.Get("If-None-Match"))
		(*inner)(w, r)
	}
	h.handler.Store(&f)
	h.clock.setUnix(goldenIssuedAt + 3)
	if wait := h.poll(); wait != 12*time.Second {
		t.Fatalf("a 304 before refresh_at waited %v", wait)
	}
	if seen.Load() != `"`+sha(h.golden)+`"` || h.edge.PolicyCounts().Unchanged != 1 || h.held(t) != sha(h.golden) {
		t.Fatalf("If-None-Match %v, counts %+v", seen.Load(), h.edge.PolicyCounts())
	}
	h.clock.setUnix(goldenIssuedAt + 20)
	var waits []time.Duration
	for range 6 {
		waits = append(waits, h.poll())
	}
	want := []time.Duration{2 * time.Second, 2 * time.Second, 2 * time.Second, 2 * time.Second, 2 * time.Second, 2 * time.Second}
	if fmt.Sprint(waits) != fmt.Sprint(want) {
		t.Fatalf("past refresh_at (304), waits %v, want %v", waits, want)
	}
	// The same past refresh_at when the instance answers an older body.
	older := h.issued(t, goldenIssuedAt-5)
	h.serve(t, older, policyPublication(h.manifest(t, routingsnapshot.PolicyTyp, older), older))
	waits = nil
	for range 4 {
		waits = append(waits, h.poll())
	}
	if fmt.Sprint(waits) != fmt.Sprint(want[:4]) || h.held(t) != sha(h.golden) {
		t.Fatalf("past refresh_at (older), waits %v", waits)
	}
	// And a 503 meanwhile backs off: it is a failure.
	h.serve503("compiling", "")
	waits = nil
	for range 4 {
		waits = append(waits, h.poll())
	}
	if fmt.Sprint(waits) != fmt.Sprint([]time.Duration{2 * time.Second, 4 * time.Second, 8 * time.Second, 15 * time.Second}) {
		t.Fatalf("503s past refresh_at, waits %v", waits)
	}
	// A newer body resets the backoff to refresh_at.
	h.serveIssued(t, goldenIssuedAt+18)
	if wait := h.poll(); wait != 13*time.Second {
		t.Fatalf("after a newer body, waited %v", wait)
	}
}

// TestEachPublisher503KeepsTheCopyUntilNotAfter: every 503 the publisher
// answers (compiling, digest-policy-unavailable, signer-unavailable,
// policy-error, and anything else) keeps the copy held until its
// not_after, then the policy is absent; the waits follow Retry-After
// within 2 s to 15 s, and back off without one.
func TestEachPublisher503KeepsTheCopyUntilNotAfter(t *testing.T) {
	for _, c := range []struct {
		status, retryAfter, logged string
		wait                       time.Duration
	}{
		{"compiling", "2", "compiling", 2 * time.Second},
		{"digest-policy-unavailable", "5", "digest-policy-unavailable", 5 * time.Second},
		{"signer-unavailable", "", "signer-unavailable", 2 * time.Second},
		{"policy-error", "5", "policy-error", 5 * time.Second},
		{"not-installed", "3600", "not-installed", 15 * time.Second},
		{"something-new", "0", "other", 2 * time.Second},
	} {
		t.Run(c.status, func(t *testing.T) {
			h := newPolicyEdge(t, nil)
			h.serveIssued(t, goldenIssuedAt)
			h.poll()
			h.serve503(c.status, c.retryAfter)
			h.clock.setUnix(goldenIssuedAt + 30)
			if wait := h.poll(); wait != c.wait {
				t.Fatalf("waited %v, want %v", wait, c.wait)
			}
			logged := "poll status=503 outcome=unavailable publisher_status=" + c.logged
			if h.held(t) != sha(h.golden) || !strings.Contains(h.logs.text(), logged) {
				t.Fatalf("the copy is not kept through the 503, or it is not logged:\n%s", h.logs.text())
			}
			h.clock.setUnix(goldenIssuedAt + 59)
			h.poll()
			if h.held(t) != sha(h.golden) {
				t.Fatal("dropped before its not_after")
			}
			h.clock.setUnix(goldenIssuedAt + 60)
			if h.held(t) != "" {
				t.Fatal("the accessor answers a policy at its not_after")
			}
			h.edge.policy.expire()
			if h.edge.policy.current.Load() != nil || h.edge.PolicyCounts().Expired != 1 {
				t.Fatal("the expired copy is still kept")
			}
			if !strings.Contains(h.logs.text(), "fastpath edge policy: expired sha256="+sha(h.golden)) {
				t.Fatalf("the expiry is not logged:\n%s", h.logs.text())
			}
			if counts := h.edge.PolicyCounts(); counts.Unavailable != 2 || counts.Installed != 1 {
				t.Fatalf("counts %+v", counts)
			}
		})
	}
	// With nothing held, failures back off from 2 s to 15 s.
	h := newPolicyEdge(t, nil)
	h.serve503("compiling", "")
	var waits []time.Duration
	for range 5 {
		waits = append(waits, h.poll())
	}
	if fmt.Sprint(waits) != fmt.Sprint([]time.Duration{2 * time.Second, 4 * time.Second, 8 * time.Second, 15 * time.Second, 15 * time.Second}) {
		t.Fatalf("waits %v", waits)
	}
	if h.held(t) != "" {
		t.Fatal("a policy from nowhere")
	}
}

// TestThePolicyPollRefuses: a stale body, a wrong typ (both ways), a test
// key, another plane, and every malformed publication are refused and
// install nothing; a copy already held is kept. Each beside its positive
// control.
func TestThePolicyPollRefuses(t *testing.T) {
	h := newPolicyEdge(t, nil)
	body := h.golden
	good := h.manifest(t, routingsnapshot.PolicyTyp, body)
	refused := func(name, want string, answer []byte) {
		t.Helper()
		before := h.edge.PolicyCounts().Refused
		h.serve(t, body, answer)
		h.edge.policy.current.Store(nil)
		h.poll()
		if h.held(t) != "" || h.edge.PolicyCounts().Refused != before+1 {
			t.Errorf("%s: installed", name)
		}
		if !strings.Contains(h.logs.text(), "outcome=refused reason=\""+want) {
			t.Errorf("%s: no refusal %q logged:\n%s", name, want, h.logs.text())
		}
	}
	h.serve(t, body, policyPublication(good, body))
	h.poll()
	if h.held(t) != sha(body) {
		t.Fatalf("the positive control is refused:\n%s", h.logs.text())
	}

	// Stale: the golden body polled at its not_after; and one more than a
	// second ahead of the edge's clock. One second ahead verifies.
	h.clock.setUnix(goldenIssuedAt + 60)
	refused("stale", "edge policy freshness", policyPublication(good, body))
	h.clock.setUnix(goldenIssuedAt - 2)
	refused("ahead", "edge policy freshness", policyPublication(good, body))
	h.clock.setUnix(goldenIssuedAt - 1)
	h.serve(t, body, policyPublication(good, body))
	h.edge.policy.current.Store(nil)
	h.poll()
	if h.held(t) != sha(body) {
		t.Fatalf("one second of skew is refused:\n%s", h.logs.text())
	}
	h.clock.setUnix(goldenIssuedAt)

	// The typ: a policy signed as a snapshot is refused.
	refused("snapshot typ", "type", policyPublication(h.manifest(t, routingsnapshot.Typ, body), body))
	refused("other typ", "type", policyPublication(h.manifest(t, "tr-edge-policy-v2", body), body))

	// The publication itself: exactly its four fields, canonical.
	canonical := string(policyPublication(good, body))
	for _, c := range []struct{ name, from, to string }{
		{"extra field", `"v":1}`, `"v":1,"x":1}`},
		{"duplicate field", `"v":1}`, `"v":1,"v":1}`},
		{"float v", `"v":1}`, `"v":1.0}`},
		{"spacing", `,"v":1}`, `, "v":1}`},
		{"trailing", `"v":1}`, `"v":1} `},
		{"kind", policyPublicationKind, "tr-routing-snapshot-publication"},
		{"version", `"v":1}`, `"v":2}`},
		{"refresh_at", `"v":1}`, `"refresh_at":1,"v":1}`},
	} {
		mutated := strings.Replace(canonical, c.from, c.to, 1)
		if mutated == canonical {
			t.Fatalf("%s: the mutation does not apply", c.name)
		}
		refused(c.name, "the publication", []byte(mutated))
	}
	missing := fmt.Sprintf(`{"body":%q,"kind":%q,"v":1}`, base64.StdEncoding.EncodeToString(body), policyPublicationKind)
	refused("missing manifest", "the publication", []byte(missing))
	urlSafe := strings.Replace(canonical, base64.StdEncoding.EncodeToString(body), base64.URLEncoding.EncodeToString(body), 1)
	if urlSafe != canonical {
		refused("base64url body", "the publication's body", []byte(urlSafe))
	}
	// The body is not the one signed.
	other := h.issued(t, goldenIssuedAt+1)
	refused("binding", "manifest binding", policyPublication(good, other))

	// A copy held survives a refusal.
	h.serve(t, body, policyPublication(good, body))
	h.poll()
	// (Served under another body's ETag, so the poll is not a 304.)
	h.serve(t, other, policyPublication(h.manifest(t, routingsnapshot.Typ, body), body))
	h.poll()
	if h.held(t) != sha(body) || !strings.Contains(h.logs.text(), `reason="type" kept=`+sha(body)) {
		t.Fatalf("a refusal dropped the copy held:\n%s", h.logs.text())
	}
}

// TestTheSnapshotPollerRefusesAPolicyManifest: decision 9 at the edge. The
// snapshot's poller, given the policy's manifest and body in a snapshot
// publication, refuses it on its typ; the policy's poller, given a snapshot
// manifest signed by the same key, refuses it on its typ.
func TestTheSnapshotPollerRefusesAPolicyManifest(t *testing.T) {
	sh := newSnapshotEdge(t, false, nil)
	ph := newPolicyEdge(t, nil)
	ph.private, ph.kid = sh.private, sh.kid
	policyManifest := ph.manifest(t, routingsnapshot.PolicyTyp, ph.golden)
	answer := publication(policyManifest, ph.golden, sh.clock.now().Unix()+60)
	sh.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(answer) })
	sh.poll(t)
	if sh.edge.poller.snapshot() != nil || !strings.Contains(sh.logs.text(), `outcome=refused reason="type"`) {
		t.Fatalf("the snapshot poller took a policy manifest:\n%s", sh.logs.text())
	}

	// The snapshot's own manifest, by the policy's poller (same key).
	snapshotManifest := sign(t, sh.private, sh.kid, sh.golden, sh.golden.raw)
	h := newPolicyEdge(t, nil)
	h.private, h.kid = sh.private, sh.kid
	h.edge.policy.keys[0].PublicKey = sh.private.Public().(ed25519.PublicKey)
	h.edge.policy.keys[0].Kid = sh.kid
	h.serve(t, sh.golden.raw, policyPublication(snapshotManifest, sh.golden.raw))
	h.poll()
	if h.held(t) != "" || !strings.Contains(h.logs.text(), `outcome=refused reason="type"`) {
		t.Fatalf("the policy poller took a snapshot manifest:\n%s", h.logs.text())
	}
	// Positive control: the same key's policy manifest installs.
	h.serveIssued(t, goldenIssuedAt)
	h.poll()
	if h.held(t) != sha(h.golden) {
		t.Fatalf("the positive control is refused:\n%s", h.logs.text())
	}
}

// TestThePolicyPollTimesOut: a poll that Python does not answer ends at the
// poll's timeout (5 s unless configured) as a failed poll, the copy kept.
func TestThePolicyPollTimesOut(t *testing.T) {
	h := newPolicyEdge(t, func(c *Config) { c.Policy.Timeout = 100 * time.Millisecond })
	h.serveIssued(t, goldenIssuedAt)
	h.poll()
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	f := func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-release:
		case <-time.After(4 * time.Second): // an ignored timeout fails, not hangs
		}
	}
	h.handler.Store(&f)
	started := time.Now()
	if wait := h.poll(); wait != 2*time.Second {
		t.Fatalf("a timed-out poll waited %v", wait)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("the poll took %v", elapsed)
	}
	if h.held(t) != sha(h.golden) || h.edge.PolicyCounts().Failed != 1 ||
		!strings.Contains(h.logs.text(), "fastpath edge policy: poll outcome=failed stage=transport") {
		t.Fatalf("the timeout is not a failed poll:\n%s", h.logs.text())
	}

	d := newPolicyEdge(t, nil).edge.policy
	if d.cfg.Timeout != 5*time.Second || d.cfg.MinInterval != 2*time.Second || d.cfg.Interval != 15*time.Second ||
		d.timeout != 5*time.Second {
		t.Fatalf("defaults %+v", d.cfg)
	}
}

// TestThePolicyConfigIsChecked: the URL on the upstream's host, the
// pinned key never the test key, and the plane as cloud/region; a policy
// signed for another plane is refused.
func TestThePolicyConfigIsChecked(t *testing.T) {
	public, _, _ := ed25519.GenerateKey(rand.Reader)
	upstream, _ := url.Parse("http://python.internal:8080")
	good := PolicyConfig{URL: &url.URL{Scheme: "http", Host: "python.internal:8080", Path: policyPath},
		Keys: []SnapshotKey{{Kid: "k1", PublicKey: public}}, Plane: "gcp/us-central1"}
	build := func(p PolicyConfig) error {
		_, err := New(Config{Token: token, Upstream: upstream, Boots: &boots{}, Policy: p})
		return err
	}
	if err := build(good); err != nil {
		t.Fatalf("the positive control: %v", err)
	}
	testPublic, _ := base64.RawURLEncoding.DecodeString(routingsnapshot.TestPublicKey)
	for name, mutate := range map[string]func(*PolicyConfig){
		"other host": func(p *PolicyConfig) { p.URL = &url.URL{Scheme: "http", Host: "elsewhere:8080", Path: policyPath} },
		"other scheme": func(p *PolicyConfig) {
			p.URL = &url.URL{Scheme: "https", Host: "python.internal:8080", Path: policyPath}
		},
		"no key":    func(p *PolicyConfig) { p.Keys = nil },
		"test key":  func(p *PolicyConfig) { p.Keys = []SnapshotKey{{Kid: "k1", PublicKey: testPublic}} },
		"test kid":  func(p *PolicyConfig) { p.Keys = []SnapshotKey{{Kid: routingsnapshot.TestKID, PublicKey: public}} },
		"no plane":  func(p *PolicyConfig) { p.Plane = "" },
		"bad plane": func(p *PolicyConfig) { p.Plane = "gcp" },
	} {
		p := good
		mutate(&p)
		if err := build(p); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}

	h := newPolicyEdge(t, func(c *Config) { c.Policy.Plane = "gcp/europe-west4" })
	h.serveIssued(t, goldenIssuedAt)
	h.poll()
	if h.held(t) != "" || !strings.Contains(h.logs.text(), `reason="manifest plane"`) {
		t.Fatalf("a policy for another plane:\n%s", h.logs.text())
	}
}

// TestTheEdgePolicyIsDark: with no policy URL nothing polls, the accessor
// answers none, and Run returns at once; with one, Run polls it and stops
// with its context.
func TestTheEdgePolicyIsDark(t *testing.T) {
	h := newPolicyEdge(t, func(c *Config) { c.Policy = PolicyConfig{} })
	h.serveIssued(t, goldenIssuedAt)
	if h.edge.policy != nil {
		t.Fatal("a poller with no URL")
	}
	done := make(chan struct{})
	go func() {
		h.edge.Run(context.Background())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run with nothing configured did not return")
	}
	if p, digest := h.edge.Policy(); p != nil || digest != "" || h.polls.Load() != 0 ||
		h.edge.PolicyCounts() != (PolicyCounts{}) {
		t.Fatal("a policy with none configured")
	}

	on := newPolicyEdge(t, nil)
	on.serveIssued(t, goldenIssuedAt)
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() {
		on.edge.Run(ctx)
		close(stopped)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for on.held(t) == "" {
		if time.Now().After(deadline) {
			t.Fatalf("Run did not install the policy:\n%s", on.logs.text())
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not stop with its context")
	}
}

// TestTheAccessorAnswersNoPolicyFromTheFuture: a copy held answers only
// while issued_at <= now + 1 s and now < not_after, so a clock stepped
// back leaves the edge with none (every request passes) until it catches
// up.
func TestTheAccessorAnswersNoPolicyFromTheFuture(t *testing.T) {
	h := newPolicyEdge(t, nil)
	h.serveIssued(t, goldenIssuedAt)
	h.poll()
	for _, c := range []struct {
		at   int64
		held bool
	}{{goldenIssuedAt - 2, false}, {goldenIssuedAt - 1, true}, {goldenIssuedAt, true}, {goldenIssuedAt + 59, true},
		{goldenIssuedAt + 60, false}} {
		h.clock.setUnix(c.at)
		if got := h.held(t) != ""; got != c.held {
			t.Errorf("at issued_at%+d the accessor answered a policy: %v, want %v", c.at-goldenIssuedAt, got, c.held)
		}
	}
}

// TestThePolicyBodyIsStrictBase64: the body's base64 is standard, padded,
// with no line breaks and zero padding bits; each refusal beside the same
// body in strict base64, which installs.
func TestThePolicyBodyIsStrictBase64(t *testing.T) {
	h := newPolicyEdge(t, nil)
	monitor := `"monitor_lookup_hash":"e4b55020e0693f129b6580f704d47899e84dfbca5169d6b0f92a4b0159bed9b9"`
	body := bytes.Replace(h.golden, []byte(monitor), []byte(`"monitor_lookup_hash":null`), 1)
	if len(body)%3 == 0 {
		t.Fatal("the body must need padding")
	}
	manifest := h.manifest(t, routingsnapshot.PolicyTyp, body)
	encoded := base64.StdEncoding.EncodeToString(body)
	answer := func(b64 string) []byte {
		out, err := json.Marshal(struct {
			Body     string `json:"body"`
			Kind     string `json:"kind"`
			Manifest string `json:"manifest"`
			V        int64  `json:"v"`
		}{b64, policyPublicationKind, manifest, 1})
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
	trimmed := strings.TrimRight(encoded, "=")
	last := strings.IndexByte(alphabet, trimmed[len(trimmed)-1])
	paddingBits := trimmed[:len(trimmed)-1] + string(alphabet[last+1]) + encoded[len(trimmed):]
	if decoded, err := base64.StdEncoding.DecodeString(paddingBits); err != nil || !bytes.Equal(decoded, body) {
		t.Fatalf("the lax decoder must read the padding-bits form as the body: %v", err)
	}
	for name, b64 := range map[string]string{
		// json.Marshal escapes the newline: the publication stays its
		// canonical encoding, so only the base64 check can refuse it.
		"a line break":     encoded[:76] + "\n" + encoded[76:],
		"padding bits set": paddingBits,
		"unpadded":         trimmed,
		"url alphabet":     strings.NewReplacer("+", "-", "/", "_").Replace(encoded),
	} {
		if name == "url alphabet" && !strings.ContainsAny(encoded, "+/") {
			continue
		}
		h.serve(t, body, answer(b64))
		h.edge.policy.current.Store(nil)
		h.poll()
		if h.held(t) != "" || !strings.Contains(h.logs.text(), `reason="the publication's body is not standard base64"`) {
			t.Errorf("%s: installed, or refused for another reason:\n%s", name, h.logs.text())
		}
	}
	h.serve(t, body, answer(encoded))
	h.poll()
	if h.held(t) != sha(body) {
		t.Fatalf("the strict control is refused:\n%s", h.logs.text())
	}
}
