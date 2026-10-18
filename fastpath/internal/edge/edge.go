// Package edge is the fast path's front for the enclave's control-plane
// calls (docs/design/fast-admission-enclave-direct.md, E1): it stands behind
// the one host the enclave reaches, checks the internal token and the boot
// signature the enclave puts on each request, and passes the request to
// Python unchanged with its verdict in a header, returning Python's answer
// unchanged. Python decides everything; the verdict is compared with its
// own, so the edge's checks are proven on real traffic before anything is
// decided here.
package edge

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	// BootHeader carries the enclave's boot signature: `kid=K,sig=S`, S the
	// URL-safe base64 of an Ed25519 signature over Digest (gateway_boot.py).
	BootHeader = "X-TR-Boot-Auth"
	// VerdictHeader carries the edge's verdict on BootHeader to Python.
	VerdictHeader = "X-TR-Edge-Boot"
	// TokenHeader carries the internal service token the enclave sends; a
	// bearer token in Authorization is the same (require_internal_gateway).
	TokenHeader = "X-Trustedrouter-Internal-Token"

	maxBody = 16 << 20
	maxKid  = 128
	maxSig  = 256
)

var bootAuthDomain = []byte("tr-authorize-v1")

// Verdict is what the edge found about a request's boot signature.
type Verdict string

const (
	// Verified: a registered, verified boot signed the request, and its
	// image digest is accepted.
	Verified Verdict = "verified"
	// Invalid: the header is malformed, the boot is unknown or unverified,
	// or the signature does not verify.
	Invalid Verdict = "invalid"
	// NotAccepted: the signature verifies but the boot's image digest is
	// not in the accepted set.
	NotAccepted Verdict = "not_accepted"
	// Unread: the boot or the accepted set could not be read in time; Python
	// decides as it would alone.
	Unread Verdict = "unread"
	// Absent: the request carries no boot signature.
	Absent Verdict = "absent"
	// Unchecked: the body is larger than the edge reads; passed through.
	Unchecked Verdict = "unchecked"
)

// Boot is a registered enclave boot: its key id, its Ed25519 public key,
// whether its registration was verified, and the image digest it attested.
type Boot struct {
	Kid         string
	PublicKey   ed25519.PublicKey
	Verified    bool
	ImageDigest string
}

// Boots reads a boot by its key id: the boot and true, or false for none.
type Boots interface {
	Boot(ctx context.Context, kid string) (Boot, bool, error)
}

// Digests says whether an image digest is accepted (the signed Stage D
// policy Python resolves); an error means the set is not known now.
type Digests interface {
	Accepted(ctx context.Context, digest string) (bool, error)
}

// Config is an edge's: the internal token, Python's internal surface, where
// boots and the accepted digests come from, and how long a boot read is
// kept (a boot's key never changes; a miss is kept a tenth as long).
type Config struct {
	Token    string
	Upstream *url.URL
	Boots    Boots
	Digests  Digests
	BootTTL  time.Duration
	Clock    func() time.Time
}

// Edge serves the enclave's control-plane paths.
type Edge struct {
	cfg   Config
	proxy *httputil.ReverseProxy
	mu    sync.Mutex
	boots map[string]cachedBoot
}

type cachedBoot struct {
	boot  Boot
	found bool
	until time.Time
}

// New makes an edge; it needs a token, an upstream with no path, and both
// readers.
func New(cfg Config) (*Edge, error) {
	if cfg.Token == "" || cfg.Upstream == nil || cfg.Upstream.Host == "" || cfg.Boots == nil || cfg.Digests == nil {
		return nil, errors.New("edge: a token, an upstream with a host, a boot reader and a digest source")
	}
	if cfg.Upstream.Path != "" && cfg.Upstream.Path != "/" {
		return nil, errors.New("edge: the upstream is a host, not a path")
	}
	if cfg.BootTTL <= 0 {
		cfg.BootTTL = 10 * time.Minute
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	e := &Edge{cfg: cfg, boots: map[string]cachedBoot{}}
	upstream := *cfg.Upstream
	upstream.Path, upstream.RawPath = "", ""
	e.proxy = &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(&upstream)
			r.Out.Host = upstream.Host
		},
	}
	return e, nil
}

// ServeHTTP passes the request to Python with the edge's verdict in
// VerdictHeader. A request without the internal token is passed as it is,
// for Python's own 401, and reads nothing here.
func (e *Edge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	r = r.Clone(r.Context())
	r.Header.Del(VerdictHeader)
	if !e.tokenOK(r) {
		e.proxy.ServeHTTP(w, r)
		return
	}
	header := r.Header.Get(BootHeader)
	if header == "" {
		r.Header.Set(VerdictHeader, string(Absent))
		e.proxy.ServeHTTP(w, r)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		http.Error(w, "edge: the body could not be read", http.StatusBadRequest)
		return
	}
	if len(body) > maxBody {
		r.Body = io.NopCloser(io.MultiReader(bytes.NewReader(body), r.Body))
		r.ContentLength = -1
		r.Header.Set(VerdictHeader, string(Unchecked))
		e.proxy.ServeHTTP(w, r)
		return
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.Header.Set(VerdictHeader, string(e.verdict(r.Context(), header, r.Method, r.URL.Path, body)))
	e.proxy.ServeHTTP(w, r)
}

func (e *Edge) tokenOK(r *http.Request) bool {
	supplied := r.Header.Get(TokenHeader)
	if supplied == "" {
		if auth := r.Header.Get("Authorization"); len(auth) > 7 && strings.EqualFold(auth[:7], "bearer ") {
			supplied = strings.TrimSpace(auth[7:])
		}
	}
	return supplied != "" && subtle.ConstantTimeCompare([]byte(supplied), []byte(e.cfg.Token)) == 1
}

// verdict checks a boot header against the request it signs.
func (e *Edge) verdict(ctx context.Context, header, method, path string, body []byte) Verdict {
	kid, sig, ok := ParseBootHeader(header)
	if !ok {
		return Invalid
	}
	boot, found, err := e.boot(ctx, kid)
	if err != nil {
		return Unread
	}
	if !found || !boot.Verified || boot.Kid != kid || len(boot.PublicKey) != ed25519.PublicKeySize {
		return Invalid
	}
	if !ed25519.Verify(boot.PublicKey, Digest(method, path, body), sig) {
		return Invalid
	}
	accepted, err := e.cfg.Digests.Accepted(ctx, boot.ImageDigest)
	if err != nil {
		return Unread
	}
	if !accepted {
		return NotAccepted
	}
	return Verified
}

func (e *Edge) boot(ctx context.Context, kid string) (Boot, bool, error) {
	now := e.cfg.Clock()
	e.mu.Lock()
	c, ok := e.boots[kid]
	e.mu.Unlock()
	if ok && now.Before(c.until) {
		return c.boot, c.found, nil
	}
	boot, found, err := e.cfg.Boots.Boot(ctx, kid)
	if err != nil {
		return Boot{}, false, err
	}
	ttl := e.cfg.BootTTL
	if !found {
		ttl /= 10
	}
	e.mu.Lock()
	e.boots[kid] = cachedBoot{boot: boot, found: found, until: now.Add(ttl)}
	e.mu.Unlock()
	return boot, found, nil
}

// ParseBootHeader reads `kid=K,sig=S` as parse_boot_auth_header does: both
// fields once, in any order, nothing else; S URL-safe base64 without padding.
func ParseBootHeader(value string) (kid string, sig []byte, ok bool) {
	fields := map[string]string{}
	for _, part := range strings.Split(value, ",") {
		name, field, found := strings.Cut(strings.TrimSpace(part), "=")
		if !found {
			return "", nil, false
		}
		if _, dup := fields[name]; dup {
			return "", nil, false
		}
		fields[name] = field
	}
	if len(fields) != 2 {
		return "", nil, false
	}
	kid, hasKid := fields["kid"]
	encoded, hasSig := fields["sig"]
	if !hasKid || !hasSig || kid == "" || len(kid) > maxKid || encoded == "" || len(encoded) > maxSig {
		return "", nil, false
	}
	sig, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(encoded, "="))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return "", nil, false
	}
	return kid, sig, true
}

// Digest is what the enclave signs (boot_auth_digest): the SHA-256 of the
// domain, the method in upper case, the path and the SHA-256 of the exact
// body bytes.
func Digest(method, path string, body []byte) []byte {
	bodyDigest := sha256.Sum256(body)
	h := sha256.New()
	h.Write(bootAuthDomain)
	h.Write([]byte(strings.ToUpper(method)))
	h.Write([]byte(path))
	h.Write(bodyDigest[:])
	return h.Sum(nil)
}
