// Package edge is the fast path's front for the enclave's control-plane
// calls (docs/design/fast-admission-enclave-direct.md, E1): it stands behind
// the one host the enclave reaches, checks the internal token and the boot
// signature the enclave puts on each request, and passes the request to
// Python unchanged with its verdict in a header, returning Python's answer
// unchanged. Python decides everything; the verdict is compared with
// Python's own check of the same signature, so the edge's check is proven
// on real traffic before anything is decided here.
//
// The verdict is the signature alone: the header well formed, a registered
// and verified boot under its key id, and that boot's key over the method,
// the path and the exact body. Python's further questions, whether the
// body names the key the signature is bound to and whether the boot's
// image digest is accepted, are Python's until the edge decides (E2).
package edge

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/textproto"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyb64"
)

const (
	// BootHeader carries the enclave's boot signature: `kid=K,sig=S`, S the
	// base64url of an Ed25519 signature over Digest (gateway_boot.py).
	BootHeader = "X-TR-Boot-Auth"
	// VerdictHeader carries the edge's verdict on BootHeader to Python.
	VerdictHeader = "X-TR-Edge-Boot"
	// TokenHeader carries the internal service token the enclave sends; a
	// bearer token in Authorization is read first, as Python reads it
	// (require_internal_gateway).
	TokenHeader = "X-Trustedrouter-Internal-Token"

	maxBody = 16 << 20
	maxKid  = 128
	maxSig  = 256
)

var bootAuthDomain = []byte("tr-authorize-v1")

// pythonSpace is what str.strip() strips from a header value Python has
// decoded as Latin-1, one byte a character: ASCII whitespace, the C0
// separators, NEL and the no-break space. Trimmed byte by byte, since the
// value's bytes are not UTF-8 to Python.
var pythonSpace = [256]bool{'\t': true, '\n': true, '\v': true, '\f': true, '\r': true, 0x1c: true, 0x1d: true,
	0x1e: true, 0x1f: true, ' ': true, 0x85: true, 0xa0: true}

func trimPythonSpace(s string) string {
	start, end := 0, len(s)
	for start < end && pythonSpace[s[start]] {
		start++
	}
	for end > start && pythonSpace[s[end-1]] {
		end--
	}
	return s[start:end]
}

// Verdict is what the edge found about a request's boot signature.
type Verdict string

const (
	// Verified: one registered, verified boot signed the request.
	Verified Verdict = "verified"
	// Invalid: the header is malformed or repeated, the boot is unknown or
	// unverified, or the signature does not verify.
	Invalid Verdict = "invalid"
	// Unread: the boot could not be read within BootWait; Python decides as
	// it would alone.
	Unread Verdict = "unread"
	// Absent: the request carries no boot signature.
	Absent Verdict = "absent"
	// Unchecked: the body is larger than the edge reads, or the path has
	// bytes outside printable ASCII, which Python's server decodes by rules
	// the edge does not repeat; passed through.
	Unchecked Verdict = "unchecked"
)

// Boot is a registered enclave boot: its key id, its Ed25519 public key and
// whether its registration was verified.
type Boot struct {
	Kid       string
	PublicKey ed25519.PublicKey
	Verified  bool
}

// Boots reads a boot by its key id: the boot and true, or false for none.
type Boots interface {
	Boot(ctx context.Context, kid string) (Boot, bool, error)
}

// Config is an edge's: the internal token, Python's internal surface, where
// boots come from, how long a boot read is kept (a boot's key never
// changes; a miss is kept a tenth as long), and how long a boot read may
// take before the verdict is Unread and the request goes on.
type Config struct {
	Token     string
	Upstream  *url.URL
	Boots     Boots
	BootTTL   time.Duration
	BootWait  time.Duration
	Clock     func() time.Time
	Transport http.RoundTripper
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

type verdictKey struct{}

// New makes an edge; it needs a token, an upstream with no path and a boot
// reader.
func New(cfg Config) (*Edge, error) {
	if cfg.Token == "" || cfg.Upstream == nil || cfg.Upstream.Host == "" || cfg.Boots == nil {
		return nil, errors.New("edge: a token, an upstream with a host and a boot reader")
	}
	if cfg.Upstream.Path != "" && cfg.Upstream.Path != "/" {
		return nil, errors.New("edge: the upstream is a host, not a path")
	}
	if cfg.BootTTL <= 0 {
		cfg.BootTTL = 10 * time.Minute
	}
	if cfg.BootWait <= 0 {
		cfg.BootWait = 2 * time.Second
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	if cfg.Transport == nil {
		// Python's bytes as they are: no compression negotiated on the
		// enclave's behalf, so its response headers and body pass unchanged.
		cfg.Transport = &http.Transport{
			DialContext:         (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
			TLSHandshakeTimeout: 10 * time.Second,
			DisableCompression:  true,
			MaxIdleConnsPerHost: 256,
			IdleConnTimeout:     90 * time.Second,
		}
	}
	e := &Edge{cfg: cfg, boots: map[string]cachedBoot{}}
	upstream := *cfg.Upstream
	upstream.Path, upstream.RawPath = "", ""
	e.proxy = &httputil.ReverseProxy{
		Transport: cfg.Transport,
		// Every write reaches the enclave as Python made it, so a response
		// Python streams is streamed.
		FlushInterval: -1,
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(&upstream)
			r.Out.Host = upstream.Host
			// The query as it came: the proxy drops parameters it cannot
			// parse (a semicolon) before Rewrite, and Python, not the edge,
			// reads the query, so it must see what the enclave sent.
			r.Out.URL.RawQuery = r.In.URL.RawQuery
			// The verdict goes on after the hop-by-hop headers are dropped,
			// so a caller's `Connection: X-TR-Edge-Boot` cannot remove it.
			r.Out.Header.Del(VerdictHeader)
			if v, ok := r.In.Context().Value(verdictKey{}).(Verdict); ok {
				r.Out.Header.Set(VerdictHeader, string(v))
			}
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			answer(w, http.StatusBadGateway, "The control plane was not reached; retry", "bad_gateway")
		},
		// A redirect Python builds names the upstream, which the caller
		// cannot reach: it is given back under the authority the caller used.
		ModifyResponse: func(resp *http.Response) error {
			location := resp.Header.Get("Location")
			if location == "" || resp.Request == nil {
				return nil
			}
			target, err := url.Parse(location)
			if err != nil || target.Host != upstream.Host {
				return nil
			}
			caller, ok := resp.Request.Context().Value(callerKey{}).(callerAuthority)
			if !ok {
				return nil
			}
			target.Scheme, target.Host = caller.scheme, caller.host
			resp.Header.Set("Location", target.String())
			return nil
		},
	}
	return e, nil
}

// callerAuthority is the scheme and host the caller used, carried in the
// request's context for a redirect's rewrite.
type callerAuthority struct{ scheme, host string }

type callerKey struct{}

func withCaller(r *http.Request) context.Context {
	scheme := "http"
	if r.TLS != nil || asciiEqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		scheme = "https"
	}
	return context.WithValue(r.Context(), callerKey{}, callerAuthority{scheme: scheme, host: r.Host})
}

// answer writes an error in the control plane's envelope (errors.py,
// error_body), so the enclave decodes it as it decodes Python's.
func answer(w http.ResponseWriter, code int, message, kind string) {
	body, _ := json.Marshal(map[string]any{"error": map[string]any{
		"code": code, "message": message, "type": kind, "source": "router",
	}})
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", fmt.Sprint(len(body)))
	w.WriteHeader(code)
	_, _ = w.Write(body)
}

// ServeHTTP passes the request to Python with the edge's verdict in
// VerdictHeader. A request without the internal token is passed as it is,
// for Python's own 401, and reads nothing here. The enclave's requests
// are JSON bodies with no trailers; the edge carries none.
func (e *Edge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Host == "" {
		// Every caller names a host (the load balancer's); a redirect for
		// one that did not has no form that keeps its authority.
		answer(w, http.StatusBadRequest, "A Host header is required", "bad_request")
		return
	}
	r = r.WithContext(withCaller(r))
	if !e.tokenOK(r) {
		e.proxy.ServeHTTP(w, r)
		return
	}
	headers := r.Header.Values(BootHeader)
	// A boot header the request names as hop-by-hop (`Connection:
	// X-TR-Boot-Auth`) is dropped by the proxy: Python sees none.
	if len(headers) == 0 || listedAsHopByHop(r.Header, BootHeader) {
		e.proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), verdictKey{}, Absent)))
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		answer(w, http.StatusBadRequest, "The request body could not be read", "bad_request")
		return
	}
	if len(body) > maxBody {
		// The rest streams after what was read; the length declared stays
		// declared, since the body is the same.
		out := r.Clone(context.WithValue(r.Context(), verdictKey{}, Unchecked))
		out.Body = io.NopCloser(io.MultiReader(bytes.NewReader(body), r.Body))
		e.proxy.ServeHTTP(w, out)
		return
	}
	// The path as the proxy sends it and Python reads it: an empty one (a
	// request target in absolute form) is the root.
	path := r.URL.Path
	if path == "" {
		path = "/"
	}
	verdict := Unchecked
	if printableASCII(path) {
		verdict = e.verdict(r.Context(), headers, r.Method, path, body)
	}
	out := r.Clone(context.WithValue(r.Context(), verdictKey{}, verdict))
	out.Body = io.NopCloser(bytes.NewReader(body))
	out.ContentLength = int64(len(body))
	e.proxy.ServeHTTP(w, out)
}

// listedAsHopByHop: the Connection header names the header, so the proxy
// drops it before Python sees the request.
func listedAsHopByHop(h http.Header, name string) bool {
	for _, value := range h.Values("Connection") {
		for _, token := range strings.Split(value, ",") {
			// Trimmed as the proxy trims the tokens it drops (ASCII
			// whitespace only), so the edge sees what the proxy will see.
			if asciiEqualFold(textproto.TrimString(token), name) {
				return true
			}
		}
	}
	return false
}

// asciiEqualFold compares case-insensitively over ASCII letters alone, as
// HTTP does (strings.EqualFold folds Unicode too, so a long s or a Kelvin
// sign would match a header name the proxy keeps apart).
func asciiEqualFold(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := 0; i < len(a); i++ {
		x, y := a[i], b[i]
		if 'A' <= x && x <= 'Z' {
			x += 'a' - 'A'
		}
		if 'A' <= y && y <= 'Z' {
			y += 'a' - 'A'
		}
		if x != y {
			return false
		}
	}
	return true
}

// printableASCII: every byte of the decoded path is printable ASCII, which
// the enclave's paths are; beyond that, Python's server decodes by its own
// rules (replacement characters, control characters) the edge does not
// repeat, and the verdict is Unchecked.
func printableASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] > 0x7e {
			return false
		}
	}
	return true
}

// tokenOK reads the token as require_internal_gateway does: a bearer token
// first, then the header.
func (e *Edge) tokenOK(r *http.Request) bool {
	supplied := ""
	if auth := forPython(r.Header, "Authorization"); len(auth) >= 7 && strings.EqualFold(auth[:7], "bearer ") {
		supplied = trimPythonSpace(auth[7:])
	}
	if supplied == "" {
		supplied = forPython(r.Header, TokenHeader)
	}
	return supplied != "" && subtle.ConstantTimeCompare([]byte(supplied), []byte(e.cfg.Token)) == 1
}

// forPython is a header's first value as Python will see it: none when the
// request names the header as hop-by-hop, since the proxy drops it.
func forPython(h http.Header, name string) string {
	if listedAsHopByHop(h, name) {
		return ""
	}
	return h.Get(name)
}

// verdict checks a boot header against the request it signs.
func (e *Edge) verdict(ctx context.Context, headers []string, method, path string, body []byte) Verdict {
	if len(headers) != 1 {
		return Invalid
	}
	kid, sig, ok := ParseBootHeader(headers[0])
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
	return Verified
}

// boot reads a boot, from the cache within its TTL, else from the reader
// within BootWait.
func (e *Edge) boot(ctx context.Context, kid string) (Boot, bool, error) {
	now := e.cfg.Clock()
	e.mu.Lock()
	c, ok := e.boots[kid]
	e.mu.Unlock()
	if ok && now.Before(c.until) {
		return c.boot, c.found, nil
	}
	ctx, cancel := context.WithTimeout(ctx, e.cfg.BootWait)
	defer cancel()
	boot, found, err := e.cfg.Boots.Boot(ctx, kid)
	if err != nil {
		return Boot{}, false, err
	}
	ttl := e.cfg.BootTTL
	if !found {
		ttl /= 10
	}
	e.mu.Lock()
	if len(e.boots) >= maxCachedBoots {
		for k, c := range e.boots {
			if !now.Before(c.until) {
				delete(e.boots, k)
			}
		}
	}
	if len(e.boots) < maxCachedBoots {
		// The key is its own string: a substring of the header would keep
		// the whole header, however padded, alive with the entry.
		e.boots[strings.Clone(kid)] = cachedBoot{boot: boot, found: found, until: now.Add(ttl)}
	}
	e.mu.Unlock()
	return boot, found, nil
}

// maxCachedBoots bounds the cache: a fleet has tens of boots, and unknown
// key ids are kept a tenth as long; past the bound, expired entries are
// swept, and if none expired the read is not kept.
const maxCachedBoots = 4096

// ParseBootHeader reads `kid=K,sig=S` as parse_boot_auth_header does: both
// fields once, in any order, nothing else, each part stripped of the
// whitespace Python strips from the header's Latin-1 bytes, S decoded by
// Python's base64url rules.
func ParseBootHeader(value string) (kid string, sig []byte, ok bool) {
	fields := map[string]string{}
	for _, part := range strings.Split(value, ",") {
		name, field, found := strings.Cut(trimPythonSpace(part), "=")
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
	sig, err := pyb64.Decode(encoded)
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
