package edge

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// The publication's literals (src/trusted_router/services/
// routing_snapshot_publisher.py): the control plane signs as its issuer for
// the edge's audience, and wraps the manifest and the body in a response of
// this kind.
const (
	SnapshotIssuer   = "https://trustedrouter.com"
	SnapshotAudience = "tr-fastpath-edge"
	publicationKind  = "tr-routing-snapshot-publication"
	// maxPublication bounds a poll's answer: the live body is about 3 MB,
	// 4 MB in base64.
	maxPublication = 32 << 20
)

// SnapshotConfig is the edge's routing snapshot (docs/design/
// routing-snapshot-v1.md, "Publishing"; E2 of fast-admission-enclave-direct.md):
// where it polls, the one key it trusts, the plane it expects, and whether
// the shadow comparator runs. URL nil polls nothing; the edge then is E1's.
type SnapshotConfig struct {
	// URL is Python's GET /internal/fastpath/routing-snapshot on the edge's
	// own upstream: the poll carries the internal token, which goes nowhere
	// the edge does not already send it.
	URL *url.URL
	// Keys are the pinned manifest keys, never the test key: one, or two
	// while a rotation moves the control plane from one to the other
	// (docs/runbooks/routing-snapshot-key.md).
	Keys []SnapshotKey
	// Plane is the plane the publisher signs for, `cloud/region`
	// (`gcp/us-central1`).
	Plane string
	// Shadow runs the comparator on the authorizes the edge passes on.
	Shadow bool
	// Interval is the longest wait between polls (a minute when 0);
	// MinInterval the shortest (five seconds), Timeout a poll's (thirty).
	Interval, MinInterval, Timeout time.Duration
}

// SnapshotKey is a pinned manifest key: its kid and Ed25519 public key.
type SnapshotKey struct {
	Kid       string
	PublicKey ed25519.PublicKey
}

// held is the one snapshot the edge keeps: the last that verified, until its
// not_after.
type held struct {
	snapshot *routingsnapshot.Snapshot
	claims   routingsnapshot.Claims
	etag     string
	// endpointModel maps an endpoint id to its model, for the affinity
	// shape; byokProvider a BYOK endpoint's id to its provider, and
	// providers are every provider slug, for BYOK presence.
	endpointModel map[string]string
	byokProvider  map[string]string
	providers     []string
}

// poller keeps the last good routing snapshot: it polls Python, verifies
// what it gets with the pinned key, and swaps it in; a refusal, an outage
// or a 503 keeps the copy it has, which is dropped at its not_after.
type poller struct {
	cfg     SnapshotConfig
	plane   routingsnapshot.Plane
	token   string
	client  *http.Client
	clock   func() time.Time
	logf    func(string, ...any)
	keys    []routingsnapshot.TrustedKey
	current atomic.Pointer[held]
	// failures and refreshAt are the poll loop's own.
	failures  int
	refreshAt int64
}

func newPoller(cfg SnapshotConfig, upstream *url.URL, token string, transport http.RoundTripper,
	clock func() time.Time, logf func(string, ...any)) (*poller, error) {
	u := cfg.URL
	if u.Scheme != upstream.Scheme || u.Host != upstream.Host || u.User != nil {
		return nil, errors.New("edge: the routing snapshot is polled from the edge's own upstream")
	}
	if len(cfg.Keys) == 0 || len(cfg.Keys) > 2 {
		return nil, errors.New("edge: the routing snapshot needs one or two pinned keys")
	}
	var keys []routingsnapshot.TrustedKey
	for _, k := range cfg.Keys {
		if len(k.PublicKey) != ed25519.PublicKeySize || k.Kid == "" {
			return nil, errors.New("edge: a routing snapshot key needs its Ed25519 public key and kid")
		}
		if k.Kid == routingsnapshot.TestKID || base64.RawURLEncoding.EncodeToString(k.PublicKey) == routingsnapshot.TestPublicKey {
			return nil, errors.New("edge: the routing snapshot's test key is never trusted")
		}
		if slices.ContainsFunc(keys, func(t routingsnapshot.TrustedKey) bool { return t.Kid == k.Kid }) {
			return nil, errors.New("edge: two routing snapshot keys share a kid")
		}
		keys = append(keys, routingsnapshot.TrustedKey{Kid: k.Kid, Purpose: routingsnapshot.Purpose, PublicKey: k.PublicKey,
			Iss: SnapshotIssuer, Aud: SnapshotAudience})
	}
	cloud, region, found := strings.Cut(cfg.Plane, "/")
	if !found || cloud == "" || region == "" || strings.Contains(region, "/") {
		return nil, errors.New("edge: the routing snapshot needs the plane it is signed for, as cloud/region")
	}
	if cfg.Interval <= 0 {
		cfg.Interval = time.Minute
	}
	if cfg.MinInterval <= 0 {
		cfg.MinInterval = 5 * time.Second
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 30 * time.Second
	}
	return &poller{cfg: cfg, plane: routingsnapshot.Plane{Cloud: cloud, Region: region}, token: token, clock: clock, logf: logf, keys: keys,
		client: &http.Client{Transport: transport,
			// A redirect would carry the token elsewhere: the answer is the
			// redirect, which is not a publication.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

// snapshot is the snapshot held, nil when there is none or it has passed its
// not_after. A read costs one atomic load.
func (p *poller) snapshot() *held {
	h := p.current.Load()
	if h == nil {
		return nil
	}
	if now := p.clock().Unix(); now < h.claims.CompiledAt || now >= h.claims.NotAfter {
		return nil
	}
	return h
}

// expire drops the snapshot held once its not_after has passed, so that what
// the edge keeps is never more than one current snapshot.
func (p *poller) expire() {
	h := p.current.Load()
	if h != nil && p.clock().Unix() >= h.claims.NotAfter && p.current.CompareAndSwap(h, nil) {
		p.logf("fastpath edge snapshot: expired sha256=%s not_after=%d", h.claims.SHA256, h.claims.NotAfter)
	}
}

// run polls until ctx ends, in its own goroutine: nothing on the proxy path
// waits for it.
func (p *poller) run(ctx context.Context) {
	for {
		p.expire()
		wait := p.poll(ctx)
		if h := p.current.Load(); h != nil {
			// Wake at not_after to drop it, even when the next poll is later.
			wait = min(wait, max(time.Unix(h.claims.NotAfter, 0).Sub(p.clock()), 0))
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

// poll asks Python once and says how long to wait before the next poll.
func (p *poller) poll(ctx context.Context) time.Duration {
	ctx, cancel := context.WithTimeout(ctx, p.cfg.Timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.cfg.URL.String(), nil)
	if err != nil {
		return p.failed("request", err)
	}
	req.Header.Set(TokenHeader, p.token)
	have := p.snapshot()
	if have != nil {
		req.Header.Set("If-None-Match", have.etag)
	}
	resp, err := p.client.Do(req)
	if err != nil {
		if ctx.Err() != nil && errors.Is(err, context.Canceled) {
			return p.cfg.Interval
		}
		return p.failed("transport", err)
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotModified && have != nil:
		p.failures = 0
		p.logf("fastpath edge snapshot: poll status=304 outcome=unchanged sha256=%s", have.claims.SHA256)
		if !p.clock().Before(time.Unix(p.refreshAt, 0)) {
			// Past refresh_at and still the same body: Python has nothing
			// newer yet, so the next poll waits the longest.
			return p.cfg.Interval
		}
		return p.next()
	case resp.StatusCode == http.StatusOK:
		raw, err := io.ReadAll(io.LimitReader(resp.Body, maxPublication+1))
		if err != nil {
			return p.failed("read", err)
		}
		if len(raw) > maxPublication {
			return p.failed("read", errors.New("the publication is over the edge's bound"))
		}
		h, refreshAt, err := p.verify(raw)
		if err != nil {
			p.failures++
			p.logf("fastpath edge snapshot: poll status=200 outcome=refused reason=%q kept=%s", reasonOf(err), keptOf(have))
			return p.backoff()
		}
		p.failures = 0
		if have != nil && have.claims.SHA256 == h.claims.SHA256 {
			// The same body: keep the one held, and its memory.
			p.refreshAt = refreshAt
			p.logf("fastpath edge snapshot: poll status=200 outcome=unchanged sha256=%s", have.claims.SHA256)
			return p.next()
		}
		p.current.Store(h)
		p.refreshAt = refreshAt
		p.logf("fastpath edge snapshot: poll status=200 outcome=installed sha256=%s size=%d compiled_at=%d not_after=%d "+
			"source_revision=%s", h.claims.SHA256, h.claims.Size, h.claims.CompiledAt, h.claims.NotAfter, h.claims.SourceRevision)
		return p.next()
	default:
		// 503 (no window is current, or no signer), 404 (publishing off),
		// or anything else: the copy held stays until its not_after.
		status := ""
		if resp.StatusCode == http.StatusServiceUnavailable {
			status = publisherStatus(resp.Body)
		}
		p.failures++
		wait := p.backoff()
		retry, ok := retryAfter(resp.Header.Get("Retry-After"), p.clock())
		if ok {
			wait = max(retry, p.cfg.MinInterval)
			if have == nil {
				// With nothing held, a long Retry-After would leave the edge
				// with no snapshot for its length: ask again within the
				// longest wait.
				wait = min(wait, p.cfg.Interval)
			}
		}
		p.logf("fastpath edge snapshot: poll status=%d outcome=unavailable publisher_status=%s retry_after=%s kept=%s",
			resp.StatusCode, status, wait, keptOf(have))
		return wait
	}
}

// next is the wait after a publication: until its refresh_at, between the
// shortest and longest waits.
func (p *poller) next() time.Duration {
	wait := time.Unix(p.refreshAt, 0).Sub(p.clock())
	return min(max(wait, p.cfg.MinInterval), p.cfg.Interval)
}

func (p *poller) backoff() time.Duration {
	wait := p.cfg.MinInterval
	for i := 1; i < p.failures && wait < p.cfg.Interval; i++ {
		wait *= 2
	}
	return min(wait, p.cfg.Interval)
}

func (p *poller) failed(stage string, err error) time.Duration {
	p.failures++
	p.logf("fastpath edge snapshot: poll outcome=failed stage=%s error=%q kept=%s", stage, err.Error(), keptOf(p.snapshot()))
	return p.backoff()
}

func keptOf(h *held) string {
	if h == nil {
		return "none"
	}
	return h.claims.SHA256
}

func reasonOf(err error) string {
	var refusal *routingsnapshot.VerifyError
	if errors.As(err, &refusal) {
		return refusal.Reason
	}
	return err.Error()
}

// verify reads a publication, `{"v", "kind", "manifest", "body",
// "refresh_at"}`, exactly those fields, and verifies its manifest against the
// body's bytes with the pinned key, the plane and audience, and the test key
// refused.
func (p *poller) verify(raw []byte) (*held, int64, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, 0, errors.New("the publication is not a JSON object")
	}
	var publication struct {
		V         int64  `json:"v"`
		Kind      string `json:"kind"`
		Manifest  string `json:"manifest"`
		Body      string `json:"body"`
		RefreshAt int64  `json:"refresh_at"`
	}
	for _, name := range []string{"v", "kind", "manifest", "body", "refresh_at"} {
		if _, ok := fields[name]; !ok {
			return nil, 0, fmt.Errorf("the publication has no %s", name)
		}
	}
	if len(fields) != 5 {
		return nil, 0, errors.New("the publication has fields beyond v1's")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&publication); err != nil {
		return nil, 0, errors.New("the publication's fields are not v1's types")
	}
	if publication.V != 1 || publication.Kind != publicationKind {
		return nil, 0, errors.New("the publication is not v1")
	}
	body, err := base64.StdEncoding.Strict().DecodeString(publication.Body)
	if err != nil || base64.StdEncoding.EncodeToString(body) != publication.Body {
		return nil, 0, errors.New("the publication's body is not standard base64")
	}
	snapshot, claims, err := routingsnapshot.Verify(publication.Manifest, p.keys, body, routingsnapshot.VerifyOptions{
		Now: p.clock().Unix(), ExpectedPlane: p.plane, ExpectedAud: SnapshotAudience, AllowTestKey: false})
	if err != nil {
		return nil, 0, err
	}
	return hold(snapshot, claims), publication.RefreshAt, nil
}

func hold(s *routingsnapshot.Snapshot, claims routingsnapshot.Claims) *held {
	h := &held{snapshot: s, claims: claims, etag: `"` + claims.SHA256 + `"`,
		endpointModel: make(map[string]string, len(s.Endpoints)), byokProvider: map[string]string{}}
	for _, e := range s.Endpoints {
		h.endpointModel[e.ID] = e.ModelID
		if e.UsageType == "BYOK" {
			h.byokProvider[e.ID] = e.Provider
		}
	}
	for _, provider := range s.Providers {
		h.providers = append(h.providers, provider.Slug)
	}
	return h
}

// publisherStatus is the 503's status, one of the publisher's own words;
// anything else is "other".
func publisherStatus(body io.Reader) string {
	var answer struct {
		Status string `json:"status"`
	}
	raw, _ := io.ReadAll(io.LimitReader(body, 4096))
	if json.Unmarshal(raw, &answer) == nil {
		switch answer.Status {
		case "signer-unavailable", "snapshot-refused", "not-installed":
			return answer.Status
		}
	}
	return "other"
}

// retryAfter reads Retry-After: seconds, or an HTTP date. A longest wait of
// a day keeps a wrong date from stopping the poll.
func retryAfter(value string, now time.Time) (time.Duration, bool) {
	if value == "" {
		return 0, false
	}
	var wait time.Duration
	if seconds, err := strconv.ParseInt(value, 10, 64); err == nil && seconds >= 0 {
		wait = time.Duration(min(seconds, int64(24*time.Hour/time.Second))) * time.Second
	} else if at, err := http.ParseTime(value); err == nil {
		wait = max(at.Sub(now), 0)
	} else {
		return 0, false
	}
	return min(wait, 24*time.Hour), true
}
