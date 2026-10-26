package edge

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// The edge policy's publication (src/trusted_router/services/
// edge_policy_publisher.py; docs/design/fast-admission-e2-decide.md section
// 4): exactly {"body", "kind", "manifest", "v"}, compact with sorted keys;
// refresh_at is inside the signed body.
const (
	policyPublicationKind = "tr-edge-policy-publication"
	// maxPolicyPublication bounds a poll's answer: the live body is about
	// a kilobyte.
	maxPolicyPublication = 1 << 20
	// policySkew is how many seconds a body's issued_at may be ahead of the
	// edge's clock: the publisher stamps its own clock, floored to whole
	// seconds, and the two hosts' clocks differ a little.
	policySkew = 1
)

// policyStatuses are the publisher's 503 words: no copy yet and the signing
// in flight, the Stage D refresh before signing failed, no usable key, a
// fact outside the schema, or no publisher installed.
var policyStatuses = []string{"compiling", "digest-policy-unavailable", "signer-unavailable", "policy-error", "not-installed"}

// PolicyConfig is the edge policy's poll (G8): where it polls, and the waits.
// The key, kid and plane are the routing snapshot's (SnapshotConfig): the
// control plane signs both with the one routing-snapshot key, each under its
// own typ. URL nil polls nothing, and the policy is always absent.
type PolicyConfig struct {
	// URL is Python's GET /internal/fastpath/edge-policy on the edge's own
	// upstream: the poll carries the internal token.
	URL   *url.URL
	Keys  []SnapshotKey
	Plane string
	// Interval is the longest wait between polls (15 s, the body's refresh
	// interval, when 0); MinInterval the shortest and the first backoff
	// (2 s); Timeout a poll's (5 s).
	Interval, MinInterval, Timeout time.Duration
}

// heldPolicy is the one edge policy the edge keeps: the newest that
// verified, until its not_after.
type heldPolicy struct {
	policy *routingsnapshot.EdgePolicy
	claims routingsnapshot.PolicyClaims
	etag   string
}

// policyPoller keeps the newest edge policy: it polls Python, verifies what
// it gets with the pinned key, and swaps it in only when it was issued
// after the copy held. Each Cloud Run instance signs and caches its own
// copy and polls are balanced across them, so a poll can answer an older
// body than one already seen; that body is ignored. A refusal, an outage or
// any 503 keeps the copy held, which is dropped at its not_after: from then
// on the policy is absent and every request passes to Python.
type policyPoller struct {
	fetcher
	cfg     PolicyConfig
	plane   routingsnapshot.Plane
	keys    []routingsnapshot.TrustedKey
	current atomic.Pointer[heldPolicy]
	counts  policyCounts
}

// policyCounts are the poll's outcomes since the edge started.
type policyCounts struct {
	installed, unchanged, older, refused, unavailable, failed, expired atomic.Int64
}

// PolicyCounts are policyCounts read once.
type PolicyCounts struct {
	Installed, Unchanged, Older, Refused, Unavailable, Failed, Expired int64
}

func newPolicyPoller(cfg PolicyConfig, upstream *url.URL, token string, transport http.RoundTripper,
	clock func() time.Time, logf func(string, ...any)) (*policyPoller, error) {
	keys, plane, err := pinnedKeys("edge policy", cfg.Keys, cfg.Plane)
	if err != nil {
		return nil, err
	}
	if cfg.Interval <= 0 {
		cfg.Interval = routingsnapshot.PolicyRefreshSeconds * time.Second
	}
	if cfg.MinInterval <= 0 {
		cfg.MinInterval = 2 * time.Second
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 5 * time.Second
	}
	f, err := newFetcher("policy", cfg.URL, upstream, token, transport, clock, logf, cfg.Interval, cfg.MinInterval, cfg.Timeout)
	if err != nil {
		return nil, errors.New("edge: the edge policy is polled from the edge's own upstream")
	}
	return &policyPoller{fetcher: f, cfg: cfg, plane: plane, keys: keys}, nil
}

// policy is the policy held, nil when there is none or it has passed its
// not_after. A read costs one atomic load.
func (p *policyPoller) policy() *heldPolicy {
	h := p.current.Load()
	if h == nil {
		return nil
	}
	if now := p.clock().Unix(); now+policySkew < h.claims.IssuedAt || now >= h.claims.NotAfter {
		return nil
	}
	return h
}

func (p *policyPoller) counted() PolicyCounts {
	c := &p.counts
	return PolicyCounts{Installed: c.installed.Load(), Unchanged: c.unchanged.Load(), Older: c.older.Load(),
		Refused: c.refused.Load(), Unavailable: c.unavailable.Load(), Failed: c.failed.Load(), Expired: c.expired.Load()}
}

// expire drops the policy held once its not_after has passed.
func (p *policyPoller) expire() {
	h := p.current.Load()
	if h != nil && p.clock().Unix() >= h.claims.NotAfter && p.current.CompareAndSwap(h, nil) {
		p.counts.expired.Add(1)
		p.logf("fastpath edge policy: expired sha256=%s issued_at=%d not_after=%d; every request passes",
			h.claims.SHA256, h.claims.IssuedAt, h.claims.NotAfter)
	}
}

// run polls until ctx ends, in its own goroutine: nothing on the proxy path
// waits for it.
func (p *policyPoller) run(ctx context.Context) {
	p.loop(ctx, p.expire, p.poll, func() (int64, bool) {
		if h := p.current.Load(); h != nil {
			return h.claims.NotAfter, true
		}
		return 0, false
	})
}

// poll asks Python once and says how long to wait before the next poll.
func (p *policyPoller) poll(ctx context.Context) time.Duration {
	have := p.policy()
	etag := ""
	if have != nil {
		etag = have.etag
	}
	resp, stage, err := p.get(ctx, etag, maxPolicyPublication)
	if errors.Is(err, errStopped) {
		return p.cfg.Interval
	}
	if err != nil {
		p.counts.failed.Add(1)
		return p.failed(stage, err, keptPolicy(have))
	}
	switch {
	case resp.status == http.StatusNotModified && have != nil:
		p.failures = 0
		p.counts.unchanged.Add(1)
		p.logf("fastpath edge policy: poll status=304 outcome=unchanged sha256=%s", have.claims.SHA256)
		return p.after(have)
	case resp.status == http.StatusOK:
		if len(resp.body) > maxPolicyPublication {
			p.counts.failed.Add(1)
			return p.failed("read", errors.New("the publication is over the edge's bound"), keptPolicy(have))
		}
		h, err := p.verify(resp.body)
		if err != nil {
			p.failures++
			p.counts.refused.Add(1)
			p.logf("fastpath edge policy: poll status=200 outcome=refused reason=%q kept=%s", reasonOf(err), keptPolicy(have))
			return p.backoff()
		}
		p.failures = 0
		switch {
		case have != nil && have.claims.SHA256 == h.claims.SHA256:
			p.counts.unchanged.Add(1)
			p.logf("fastpath edge policy: poll status=200 outcome=unchanged sha256=%s", have.claims.SHA256)
			return p.after(have)
		case have != nil && h.claims.IssuedAt <= have.claims.IssuedAt:
			// Another instance's older copy (or one signed in the same
			// second): the newest seen stays.
			p.counts.older.Add(1)
			p.logf("fastpath edge policy: poll status=200 outcome=older sha256=%s issued_at=%d kept=%s kept_issued_at=%d",
				h.claims.SHA256, h.claims.IssuedAt, have.claims.SHA256, have.claims.IssuedAt)
			return p.after(have)
		}
		p.current.Store(h)
		p.counts.installed.Add(1)
		p.logf("fastpath edge policy: poll status=200 outcome=installed sha256=%s size=%d issued_at=%d not_after=%d "+
			"accepted_image_digests=%d", h.claims.SHA256, h.claims.Size, h.claims.IssuedAt, h.claims.NotAfter,
			len(h.policy.AcceptedImageDigests))
		return p.after(h)
	default:
		// Any 503 (compiling, digest-policy-unavailable,
		// signer-unavailable, policy-error), 404 (publishing off) or
		// anything else: the copy held stays until its not_after.
		status := ""
		if resp.status == http.StatusServiceUnavailable {
			status = publisherStatus(resp.body, policyStatuses)
		}
		p.failures++
		p.counts.unavailable.Add(1)
		wait := p.backoff()
		if retry, ok := retryAfter(resp.header.Get("Retry-After"), p.clock()); ok {
			// Within the refresh interval whatever Python asks: a copy
			// lives a minute, so the edge keeps asking.
			wait = min(max(retry, p.cfg.MinInterval), p.cfg.Interval)
		}
		p.logf("fastpath edge policy: poll status=%d outcome=unavailable publisher_status=%s retry_after=%s kept=%s",
			resp.status, status, wait, keptPolicy(have))
		return wait
	}
}

// after is the wait after a poll that left h held: until its refresh_at;
// past it, the instance answering has nothing newer yet (its own signing is
// running, or another instance answered), so ask again at the shortest
// wait. That is bounded: an instance signs at most once a refresh interval
// however often it is polled, and a copy lives only four intervals, so a
// backoff here would leave the edge with few polls before its not_after.
// Real failures back off (failed, refused, unavailable).
func (p *policyPoller) after(h *heldPolicy) time.Duration {
	if p.clock().Unix() < h.policy.RefreshAt {
		return p.until(h.policy.RefreshAt)
	}
	return p.cfg.MinInterval
}

func keptPolicy(h *heldPolicy) string {
	if h == nil {
		return "none"
	}
	return h.claims.SHA256
}

// verify reads a publication, exactly {"body", "kind", "manifest", "v"} in
// its canonical encoding (so no duplicate, unknown or missing field, no
// float), and verifies the manifest against the body's bytes with the
// pinned key, the plane and audience, the edge-policy typ, the test key
// refused and issued_at no more than a second ahead of the edge's clock.
func (p *policyPoller) verify(raw []byte) (*heldPolicy, error) {
	var publication struct {
		Body     string `json:"body"`
		Kind     string `json:"kind"`
		Manifest string `json:"manifest"`
		V        int64  `json:"v"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&publication); err != nil {
		return nil, errors.New("the publication's fields are not v1's")
	}
	// The publisher writes sorted keys, compact: the one encoding of these
	// four fields. Anything else (a duplicate, a missing field, spacing,
	// trailing bytes) is not its publication.
	canonical, err := json.Marshal(publication)
	if err != nil || !bytes.Equal(canonical, raw) {
		return nil, errors.New("the publication is not its canonical encoding")
	}
	if publication.V != 1 || publication.Kind != policyPublicationKind {
		return nil, errors.New("the publication is not v1")
	}
	body, err := base64.StdEncoding.Strict().DecodeString(publication.Body)
	if err != nil || base64.StdEncoding.EncodeToString(body) != publication.Body {
		return nil, errors.New("the publication's body is not standard base64")
	}
	policy, claims, err := routingsnapshot.VerifyEdgePolicy(publication.Manifest, p.keys, body,
		routingsnapshot.PolicyVerifyOptions{VerifyOptions: routingsnapshot.VerifyOptions{
			Now: p.clock().Unix(), ExpectedPlane: p.plane, ExpectedAud: SnapshotAudience, AllowTestKey: false},
			Skew: policySkew})
	if err != nil {
		return nil, err
	}
	return &heldPolicy{policy: policy, claims: claims, etag: `"` + claims.SHA256 + `"`}, nil
}

// Policy is the edge policy held and its body's sha256: the newest that
// verified, until its not_after. nil when no policy is configured, none has
// verified yet, or the one held has passed its not_after; with none, every
// request passes to Python. The policy is shared: callers must not modify
// it. Nothing reads it yet: the decide pipeline (G10) will.
func (e *Edge) Policy() (*routingsnapshot.EdgePolicy, string) {
	if e.policy == nil {
		return nil, ""
	}
	h := e.policy.policy()
	if h == nil {
		return nil, ""
	}
	return h.policy, h.claims.SHA256
}

// PolicyCounts are the edge policy poll's outcomes since the edge started;
// zero when no policy is configured.
func (e *Edge) PolicyCounts() PolicyCounts {
	if e.policy == nil {
		return PolicyCounts{}
	}
	return e.policy.counted()
}
