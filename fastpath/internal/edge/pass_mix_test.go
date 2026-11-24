package edge

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"reflect"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The decide line's pass mix (decide_path.go: pendingMix, decider.countMix,
// decider.attribute): where a body.float pass's first float is, and whether
// a pass's key belongs to a workspace the switch copy enables. It is counted
// once the forward to Python has returned, changes nothing the edge
// answers, refuses or passes, and its attribution reads the cache without a
// fill, a use or a read of the store.

// attributionCache is the case's cache for the pipeline's own lookups, with
// a Peek the test answers, each lookup and peek counted. failLookup fails
// the test on any Lookup, a call that fills on a miss: for a request passed
// before the key's facts, only Peek may be asked. stale serves a fresh entry
// as stale (cache.key, its facts still held). seq, when set, numbers each
// peek in the order of events the test keeps (peekedAt, the last).
type attributionCache struct {
	KeyCache
	t              *testing.T
	failLookup     bool
	stale          bool
	held           map[string]string
	lookups, peeks atomic.Int64
	seq            *atomic.Int64
	peekedAt       atomic.Int64
}

func (c *attributionCache) Lookup(hash string, now time.Time) gwstate.Read {
	c.lookups.Add(1)
	if c.failLookup {
		c.t.Errorf("a Lookup of %.8s, which fills on a miss, for a request passed before the key's facts", hash)
	}
	r := c.KeyCache.Lookup(hash, now)
	if c.stale && r.Key == gwstate.Fresh {
		r.Key = gwstate.Stale
	}
	return r
}

func (c *attributionCache) Peek(hash string) (string, bool) {
	c.peeks.Add(1)
	if c.seq != nil {
		c.peekedAt.Store(c.seq.Add(1))
	}
	ws, ok := c.held[hash]
	return ws, ok
}

// countLenient counts the decide path's lenient decodes (decider.lenient),
// numbering each in seq's order when seq is set.
func countLenient(dp *decider, seq *atomic.Int64) (calls, at *atomic.Int64) {
	calls, at = &atomic.Int64{}, &atomic.Int64{}
	dp.lenient = func(body []byte) string {
		calls.Add(1)
		if seq != nil {
			at.Store(seq.Add(1))
		}
		return lenientLookupHash(body)
	}
	return calls, at
}

// withoutBootHeader is the case's request with no boot header: the edge
// passes it before reading its body (passedUnread).
func withoutBootHeader(c authorizeCase) authorizeCase {
	c.Request.Headers = slices.DeleteFunc(slices.Clone(c.Request.Headers), func(kv [2]string) bool {
		return strings.EqualFold(kv[0], BootHeader)
	})
	return c
}

// TestThePassMixAttributesEachPass: each pass is counted once under its
// reason as of a workspace the switch copy enables (passed_enabled), of one
// it does not (in neither map), or unknown (passed_unknown), and a
// body.float pass under where its first float is (float_at). A request
// passed before the key's facts is attributed by Peek alone, never a Lookup
// (which fills), and a pass after them by what the pipeline already read,
// with no lookup of its own; the body is decoded leniently only when the
// strict parse did not read its lookup hash (a cohort reason's is the
// parse's); nothing is admitted, inserted or refunded. The blind spot is
// shown too: a key the cache does not hold, a body the edge did not read or
// cannot read, and a node with no front door bound are unknown.
func TestThePassMixAttributesEachPass(t *testing.T) {
	f := authorizeGoldens(t)
	const ws = "ws_e2_cohort"
	hash := authorizeCaseNamed(t, "pass:body.float").Facts.Key.LookupHash
	heldHere := map[string]string{hash: ws}
	for _, tc := range []struct {
		name, caseName string
		request        func(authorizeCase) authorizeCase
		held           map[string]string
		off, unbound   bool
		stale          bool
		// before: the request passes before the key's facts, so any Lookup
		// fails the test.
		before         bool
		reason         string
		enabled        bool // counted in passed_enabled
		unknown        bool // counted in passed_unknown
		floatAt        string
		lookups, peeks int64
		lenient        int64 // lenient decodes
	}{
		{name: "a float, its key held, enabled", caseName: "pass:body.float", held: heldHere, before: true,
			reason: gwauth.ReasonFloat, enabled: true, floatAt: gwauth.FloatAtMetadata, peeks: 1, lenient: 1},
		{name: "a float, its key held, not enabled", caseName: "pass:body.float", held: heldHere, off: true,
			before: true, reason: gwauth.ReasonFloat, floatAt: gwauth.FloatAtMetadata, peeks: 1, lenient: 1},
		{name: "a float, its key never held (the blind spot)", caseName: "pass:body.float", before: true,
			reason: gwauth.ReasonFloat, unknown: true, floatAt: gwauth.FloatAtMetadata, peeks: 1, lenient: 1},
		{name: "a declared field written as a float", caseName: "pass:body.float:integral_spelling", held: heldHere,
			before: true, reason: gwauth.ReasonFloat, enabled: true,
			floatAt: gwauth.FloatAtDeclared + "estimated_input_tokens", peeks: 1, lenient: 1},
		{name: "a cohort reason after the parse", caseName: "pass:body.internal_model:monitor", held: heldHere,
			before: true, reason: gwauth.ReasonInternalModel, enabled: true, peeks: 1},
		{name: "a schema reason", caseName: "pass:body.lax_type", held: heldHere, before: true,
			reason: gwauth.ReasonLaxType, enabled: true, peeks: 1, lenient: 1},
		{name: "a boot that fails, its body read leniently", caseName: "pass:boot.signature:bad_signature",
			held: heldHere, before: true, reason: PassBootSignature, enabled: true, peeks: 1, lenient: 1},
		{name: "a body that is not JSON", caseName: "pass:body.unparseable:bom", held: heldHere, before: true,
			reason: gwauth.ReasonUnparseable, unknown: true, lenient: 1},
		{name: "no boot header, the body never read", caseName: "pass:body.float", request: withoutBootHeader,
			held: heldHere, before: true, reason: bootPass(BootSeen{Verdict: Absent}), unknown: true},
		{name: "a policy gate before the key", caseName: "pass:policy.pilot_off", held: heldHere, before: true,
			reason: PassPilotOff, enabled: true, peeks: 1},
		{name: "a gate after the key's facts", caseName: "pass:workspace.tier:low", reason: "workspace.tier",
			enabled: true, lookups: 1},
		{name: "a gate after the key's facts, not enabled", caseName: "pass:workspace.tier:low", off: true,
			reason: "workspace.tier", lookups: 1},
		{name: "a lookup hash that names no key", caseName: "pass:key.missing", reason: PassKeyMissing, lookups: 1},
		{name: "the switch copy's own gate", caseName: "answer:app_categories_empty", off: true,
			reason: PassWorkspaceNotEnabled, lookups: 1},
		{name: "a stale entry's facts", caseName: "answer:app_categories_empty", stale: true, reason: PassCacheKey,
			enabled: true, lookups: 1},
		{name: "no front door bound", caseName: "pass:owner.unreachable", unbound: true,
			reason: PassOwnerUnreachable, unknown: true, lookups: 1},
	} {
		c := authorizeCaseNamed(t, tc.caseName)
		if tc.request != nil {
			c = tc.request(c)
		}
		h := newDecideEdge(t, f, c, nil)
		cache := &attributionCache{KeyCache: h.cache, t: t, failLookup: tc.before, stale: tc.stale, held: tc.held}
		h.edge.decider.cfg.Cache = cache
		lenients, _ := countLenient(h.edge.decider, nil)
		if tc.off {
			h.admitter.off = map[string]bool{ws: true}
		}
		if tc.unbound {
			h.holder.Bind(nil)
		}
		hits := h.python.hits.Load()
		h.send(t, c)
		got := h.edge.DecideCounts()
		asked, refunds, _ := h.admitter.seen()
		if h.python.hits.Load()-hits != 1 || !maps.Equal(got.Passed, map[string]int64{tc.reason: 1}) ||
			got.Answered+got.Unavailable != 0 || len(asked) != 0 || len(refunds) != 0 || len(h.rows.inserted()) != 0 {
			t.Errorf("%s: passed %v, asked %d, refunds %d, inserts %d; want one pass for %s", tc.name, got.Passed,
				len(asked), len(refunds), len(h.rows.inserted()), tc.reason)
			continue
		}
		wantEnabled, wantUnknown, wantFloat := map[string]int64{}, map[string]int64{}, map[string]int64{}
		if tc.enabled {
			wantEnabled[tc.reason] = 1
		}
		if tc.unknown {
			wantUnknown[tc.reason] = 1
		}
		if tc.floatAt != "" {
			wantFloat[tc.floatAt] = 1
		}
		if !maps.Equal(got.PassedEnabled, wantEnabled) || !maps.Equal(got.PassedUnknown, wantUnknown) ||
			!maps.Equal(got.FloatAt, wantFloat) || got.EnabledUnknown != int64(len(wantUnknown)) {
			t.Errorf("%s: enabled %v, unknown %v (%d), float_at %v; want %v, %v, %v", tc.name, got.PassedEnabled,
				got.PassedUnknown, got.EnabledUnknown, got.FloatAt, wantEnabled, wantUnknown, wantFloat)
		}
		if l, p, d := cache.lookups.Load(), cache.peeks.Load(), lenients.Load(); l != tc.lookups || p != tc.peeks ||
			d != tc.lenient {
			t.Errorf("%s: %d lookups, %d peeks and %d lenient decodes, want %d, %d and %d", tc.name, l, p, d,
				tc.lookups, tc.peeks, tc.lenient)
		}
	}
}

// TestTheAttributionNeverFillsTheCache: with the node's real cache cold, a
// request passed before the key's facts is counted unknown (its key is not
// held) and leaves the cache as it was: no read of the store, no entry, no
// count. The same request on a cache that holds its key is attributed with
// no read either.
func TestTheAttributionNeverFillsTheCache(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "pass:body.float")
	h := newDecideEdge(t, f, c, nil)
	world := &fakeWorld{c: c}
	cold, err := gwstate.NewCache(gwstate.CacheConfig{Reader: world, Clock: func() time.Time { return c.at() }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cold.Close)
	h.edge.decider.cfg.Cache = cold
	h.send(t, c)
	if got := h.edge.DecideCounts(); !maps.Equal(got.PassedUnknown, map[string]int64{gwauth.ReasonFloat: 1}) ||
		len(got.PassedEnabled) != 0 {
		t.Fatalf("a cold cache: %+v", got)
	}
	cold.Close() // waits for any read a fill would have begun
	if keys, credits := cold.Len(); world.reads.Load() != 0 || keys != 0 || credits != 0 ||
		cold.Counters() != (gwstate.Counters{}) {
		t.Fatalf("the attribution filled the cache: %d reads, %d keys, %d credit entries, %+v", world.reads.Load(),
			keys, credits, cold.Counters())
	}
	// The harness's own warm cache holds the key: attributed, and still no
	// read and no lookup counted.
	warm := warmCache(t, &fakeWorld{c: c}, c.Facts.Key.LookupHash, c.at())
	before := warm.Counters()
	h.edge.decider.cfg.Cache = warm
	h.send(t, c)
	if got := h.edge.DecideCounts(); !maps.Equal(got.PassedEnabled, map[string]int64{gwauth.ReasonFloat: 1}) {
		t.Fatalf("a warm cache: %+v", got)
	}
	if after := warm.Counters(); after != before {
		t.Fatalf("a peek counted as a lookup or a read: %+v, was %+v", after, before)
	}
}

// noCache is the decide line's cache counts, which follow the pass mix, for
// a period whose lookups found every key fresh, or that looked none up.
const noCache = ` cache_miss={} cache_wait={} cache_wait_ms_sum=0 cache_wait_ms_max=0`

// TestTheDecideLineCarriesThePassMix: the period's line carries the pass
// mix after every field the line carried before it (a reader of the older
// line finds its fields where they were), and the cache's counts after it;
// it reads back as its counts, and the next period's line starts again from
// nothing.
func TestTheDecideLineCarriesThePassMix(t *testing.T) {
	f := authorizeGoldens(t)
	base := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newDecideEdge(t, f, base, nil)
	for _, c := range []authorizeCase{
		authorizeCaseNamed(t, "pass:body.float"),
		authorizeCaseNamed(t, "pass:body.float:integral_spelling"),
		authorizeCaseNamed(t, "pass:boot.signature:bad_signature"),
		withoutBootHeader(base),
	} {
		h.send(t, c)
	}
	h.admitter.off = map[string]bool{base.Facts.Key.WorkspaceID: true}
	h.send(t, base)
	h.edge.decider.flush()
	h.edge.decider.flush()
	var lines []string
	for _, l := range strings.Split(h.logs.all(), "\n") {
		if strings.Contains(l, "fastpath edge decide: ") {
			lines = append(lines, l)
		}
	}
	if len(lines) != 2 {
		t.Fatalf("the lines: %q", lines)
	}
	if want := ` float_at={"declared.estimated_input_tokens":1,"metadata":1} ` +
		`passed_enabled={"body.float":2,"boot.signature":1} passed_unknown={"boot.signature":1} enabled_unknown=1 ` +
		`mix_dropped=0` + noCache; !strings.HasSuffix(lines[0], want) ||
		!strings.Contains(lines[0], ` passed={"body.float":2,"boot.signature":2,"workspace.not_enabled":1} `) ||
		!strings.Contains(lines[0], "insert_call_ms_max=0"+want) {
		t.Fatalf("the period's line: %s\nwant it to end %s", lines[0], want)
	}
	if want := ` float_at={} passed_enabled={} passed_unknown={} enabled_unknown=0 mix_dropped=0` + noCache; !strings.HasSuffix(lines[1], want) {
		t.Fatalf("the next period's line: %s", lines[1])
	}
	parsed := parsedLines(t, h.logs)
	if len(parsed) != 2 || parsed[0].Counts["enabled_unknown"] != 1 ||
		!maps.Equal(parsed[0].ByName["float_at"], map[string]int64{"declared.estimated_input_tokens": 1, "metadata": 1}) ||
		!maps.Equal(parsed[0].ByName["passed_enabled"], map[string]int64{"body.float": 2, "boot.signature": 1}) ||
		!maps.Equal(parsed[0].ByName["passed_unknown"], map[string]int64{"boot.signature": 1}) ||
		len(parsed[1].ByName["float_at"]) != 0 || parsed[1].Counts["enabled_unknown"] != 0 {
		t.Fatalf("read back: %+v", parsed)
	}
	// The line before the pass mix still reads, its counts as they were.
	old := `2026/10/24 12:00:00 fastpath edge decide: answered=0 read_back=0 unavailable=0 refunded=0 ` +
		`refund_failed=0 unavailable_by_code={} passed={"body.float":3} slow={} windows={} decided_via={} tickets={} ` +
		`ticket_call_ms_sum=0 ticket_call_ms_max=0 ticket_wait_ms_sum=0 ticket_wait_ms_max=0 insert_call_ms_sum=0 ` +
		`insert_call_ms_max=0`
	if l, ok, err := ParseCountsLine(old); !ok || err != nil || l.ByName["passed"]["body.float"] != 3 ||
		l.ByName["float_at"] != nil {
		t.Fatalf("the older line: %+v %v %v", l, ok, err)
	}
}

// bootSigner signs a body as the goldens' enclave does, with the file's
// boot test key (its seed), for a body the goldens did not sign.
type bootSigner struct {
	kid string
	key ed25519.PrivateKey
}

func newBootSigner(t *testing.T) bootSigner {
	t.Helper()
	var file struct {
		BootTestKey struct {
			Kid     string `json:"kid"`
			SeedHex string `json:"seed_hex"`
		} `json:"boot_test_key"`
	}
	raw, err := readTestdata("authorize_cases_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	seed, err := hex.DecodeString(file.BootTestKey.SeedHex)
	if err != nil || len(seed) != ed25519.SeedSize {
		t.Fatalf("the seed: %v", err)
	}
	return bootSigner{kid: file.BootTestKey.Kid, key: ed25519.NewKeyFromSeed(seed)}
}

// sign is c with body and headers in place of its own, and a boot header
// signed over the new body.
func (s bootSigner) sign(c authorizeCase, body string, headers [][2]string) authorizeCase {
	c.Request.Body, c.Request.BodyExpand = body, nil
	var kept [][2]string
	for _, kv := range c.Request.Headers {
		if !strings.EqualFold(kv[0], BootHeader) && !strings.EqualFold(kv[0], "content-type") {
			kept = append(kept, kv)
		}
	}
	kept = append(kept, headers...)
	sig := ed25519.Sign(s.key, Digest(http.MethodPost, c.Request.Path, []byte(body)))
	c.Request.Headers = append(kept, [2]string{BootHeader, "kid=" + s.kid + ",sig=" + base64.RawURLEncoding.EncodeToString(sig)})
	return c
}

// carriedTransport keeps what the proxy carries of each pass it forwards
// (decidePassed: the lookup hash and workspace Python's refusal evicts, and
// the reason), its arrival and pending mix aside, then sends it on.
type carriedTransport struct {
	base http.RoundTripper
	mu   sync.Mutex
	seen []decidePassed
}

func (c *carriedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if p, ok := r.Context().Value(decidePassedKey{}).(decidePassed); ok {
		p.arrived, p.mix = time.Time{}, nil
		c.mu.Lock()
		c.seen = append(c.seen, p)
		c.mu.Unlock()
	}
	return c.base.RoundTrip(r)
}

func (c *carriedTransport) carried() []decidePassed {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.seen)
}

// recordedEdge is a decide edge whose proxy keeps what it carries.
type recordedEdge struct {
	*decideEdge
	carried *carriedTransport
}

func recording(h *decideEdge) recordedEdge {
	c := &carriedTransport{base: h.python.transport}
	h.edge.proxy.Transport = tracedTransport{base: c}
	return recordedEdge{decideEdge: h, carried: c}
}

// sentOnce is what one request through a decide edge did: the edge's
// answer (timing values aside, Python's own Date, and the Content-Length,
// which follows the timing values' digits; the body is compared), whether
// it reached Python and what the proxy carried of it, what it asked of the
// front door, the rows it inserted, the refunds, and its counts but the
// pass mix.
type sentOnce struct {
	code    int
	header  http.Header
	body    string
	reached int64
	carried []decidePassed
	asked   []frontdoor.AuthorizeOf
	refunds []frontdoor.RefundOf
	rows    []gwauth.Composed
	counts  DecideCounts
}

func sendRecorded(t *testing.T, h recordedEdge, c authorizeCase) sentOnce {
	t.Helper()
	hits := h.python.hits.Load()
	askedBefore, refundsBefore, _ := h.admitter.seen()
	rowsBefore, carriedBefore := len(h.rows.inserted()), len(h.carried.carried())
	rec := h.send(t, c)
	asked, refunds, _ := h.admitter.seen()
	header := rec.Header().Clone()
	header.Del("Date")
	header.Del("Content-Length")
	counts := h.edge.DecideCounts()
	counts.FloatAt, counts.PassedEnabled, counts.PassedUnknown, counts.EnabledUnknown = nil, nil, nil, 0
	counts.Slow = nil // timing, not an answer
	return sentOnce{code: rec.Code, header: header, body: withGoldenTiming(rec.Body.Bytes()),
		reached: h.python.hits.Load() - hits, carried: h.carried.carried()[carriedBefore:],
		asked: asked[len(askedBefore):], refunds: refunds[len(refundsBefore):], rows: h.rows.inserted()[rowsBefore:],
		counts: counts}
}

// checkMix holds a measuring edge's pass mix to its bounds: every float_at
// key of the vocabulary and their sum the body.float passes, and every
// passed_enabled and passed_unknown key a reason passed, each count within
// its reason's.
func checkMix(t *testing.T, name string, got DecideCounts) {
	t.Helper()
	names := gwauth.FloatAtNames()
	var located int64
	for k, n := range got.FloatAt {
		if !names[k] {
			t.Errorf("%s: float_at key %q is not of the vocabulary", name, k)
		}
		located += n
	}
	if located != got.Passed[gwauth.ReasonFloat] {
		t.Errorf("%s: %d floats located of %d body.float passes", name, located, got.Passed[gwauth.ReasonFloat])
	}
	for reason, n := range got.Passed {
		if got.PassedEnabled[reason]+got.PassedUnknown[reason] > n {
			t.Errorf("%s: %s counted %d enabled and %d unknown of %d", name, reason, got.PassedEnabled[reason],
				got.PassedUnknown[reason], n)
		}
	}
	for _, m := range []map[string]int64{got.PassedEnabled, got.PassedUnknown} {
		for reason := range m {
			if got.Passed[reason] == 0 {
				t.Errorf("%s: %q counted, and no pass of it", name, reason)
			}
		}
	}
}

// TestTheDecideAnswersAreTheSameWithoutThePassMix: every golden case, and
// every fingerprint vector (its key the goldens', signed with the goldens'
// boot key), through two decide edges, one counting the pass mix and one
// not: each answer is the same bytes and headers (timing values aside),
// reaches Python or not alike, with the same lookup hash, workspace and
// reason carried for the eviction, asks the front door the same, inserts the
// same rows and refunds the same, and every count but the pass mix is the
// same. The measuring edge's mix stays within its bounds, and is not empty:
// the measurement ran.
func TestTheDecideAnswersAreTheSameWithoutThePassMix(t *testing.T) {
	f := authorizeGoldens(t)
	py := newFakePython(t)
	setUp := func(c authorizeCase, measure bool) recordedEdge {
		h := newDecideEdge(t, f, c, py)
		h.edge.decider.measure = measure
		reason := c.Expect.Reason
		switch {
		case reason == PassOwnerUnreachable:
			h.holder.Bind(nil)
		case ownerAnswers[reason] != "":
			status := ownerAnswers[reason]
			h.admitter.authorize = func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
				return frontdoor.Authorized{Status: status}
			}
		}
		outcome, code := insertOf(t, c)
		h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return outcome }, code
		return recording(h)
	}
	compare := func(name string, on, off sentOnce) bool {
		t.Helper()
		if on.code != off.code || on.body != off.body || !reflect.DeepEqual(on.header, off.header) ||
			on.reached != off.reached || !reflect.DeepEqual(on.carried, off.carried) ||
			!reflect.DeepEqual(on.asked, off.asked) ||
			!reflect.DeepEqual(on.refunds, off.refunds) || !reflect.DeepEqual(on.rows, off.rows) ||
			!reflect.DeepEqual(on.counts, off.counts) {
			t.Errorf("%s: with the pass mix %d %v %s (reached %d, carried %+v, %d asks, %d rows, %+v); without %d %v "+
				"%s (reached %d, carried %+v, %d asks, %d rows, %+v)", name, on.code, on.header, on.body, on.reached,
				on.carried, len(on.asked), len(on.rows), on.counts, off.code, off.header, off.body, off.reached,
				off.carried, len(off.asked), len(off.rows), off.counts)
			return false
		}
		return true
	}
	mix := DecideCounts{FloatAt: map[string]int64{}, PassedEnabled: map[string]int64{}, PassedUnknown: map[string]int64{}}
	add := func(got DecideCounts) {
		for _, p := range []struct{ into, from map[string]int64 }{{mix.FloatAt, got.FloatAt},
			{mix.PassedEnabled, got.PassedEnabled}, {mix.PassedUnknown, got.PassedUnknown}} {
			for k, n := range p.from {
				p.into[k] += n
			}
		}
	}
	carriedNamed := 0
	for _, c := range f.Cases {
		on, off := setUp(c, true), setUp(c, false)
		a := sendRecorded(t, on, c)
		if !compare(c.Name, a, sendRecorded(t, off, c)) {
			continue
		}
		for _, p := range a.carried {
			if p.lookupHash != "" && p.workspace != "" && p.reason != "" {
				carriedNamed++
			}
		}
		measured, unmeasured := on.edge.DecideCounts(), off.edge.DecideCounts()
		checkMix(t, c.Name, measured)
		if len(unmeasured.FloatAt)+len(unmeasured.PassedEnabled)+len(unmeasured.PassedUnknown) != 0 {
			t.Errorf("%s: a mix counted with the measurement off: %+v", c.Name, unmeasured)
		}
		add(measured)
	}
	// The comparison of what the proxy carries saw passes that name a key
	// and a workspace, the ones an eviction reads.
	if len(mix.FloatAt) == 0 || len(mix.PassedEnabled) == 0 || len(mix.PassedUnknown) == 0 || carriedNamed < 50 {
		t.Fatalf("the goldens' mix: %+v; %d passes carried a key and a workspace", mix, carriedNamed)
	}

	// The fingerprint corpus, through one pair of edges over the plain
	// answered case's world.
	var corpus struct {
		Cases []struct {
			Name    string      `json:"name"`
			Headers [][2]string `json:"headers"`
			Body    string      `json:"body"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(testdata(t, "authorize_fingerprint_v1.json"), &corpus); err != nil {
		t.Fatal(err)
	}
	base := authorizeCaseNamed(t, "answer:app_categories_empty")
	signer := newBootSigner(t)
	on, off := setUp(base, true), setUp(base, false)
	// Every body in the corpus that names a key names the same one; each is
	// sent under the base case's key instead, so read the corpus's from the
	// bodies.
	corpusKeys := map[string]bool{}
	for _, v := range corpus.Cases {
		var body struct {
			LookupHash string `json:"api_key_lookup_hash"`
		}
		if json.Unmarshal([]byte(v.Body), &body) == nil && body.LookupHash != "" {
			corpusKeys[body.LookupHash] = true
		}
	}
	if len(corpusKeys) != 1 {
		t.Fatalf("the corpus names %d keys, want one", len(corpusKeys))
	}
	var corpusKey string
	for k := range corpusKeys {
		corpusKey = k
	}
	answered := 0
	for _, v := range corpus.Cases {
		c := signer.sign(base, strings.ReplaceAll(v.Body, corpusKey, base.Facts.Key.LookupHash), v.Headers)
		a, b := sendRecorded(t, on, c), sendRecorded(t, off, c)
		if !compare(v.Name, a, b) {
			continue
		}
		if a.code == http.StatusOK {
			answered++
		}
	}
	measured := on.edge.DecideCounts()
	checkMix(t, "the fingerprint corpus", measured)
	if len(corpus.Cases) < 14_000 || answered < 1_000 || len(measured.FloatAt) < 4 ||
		measured.PassedEnabled[gwauth.ReasonFloat] == 0 {
		t.Fatalf("%d vectors, %d answered, mix %+v", len(corpus.Cases), answered, measured)
	}
	t.Logf("goldens' mix %+v; corpus: %d vectors, %d answered, float_at %v, passed_enabled %v, passed_unknown %v",
		mix, len(corpus.Cases), answered, measured.FloatAt, measured.PassedEnabled, measured.PassedUnknown)
}

// TestThePassMixWaitsForTheForward: the pass mix's work, the lenient decode
// and the cache's Peek, comes after Python has the request, never before
// the forward: a pass before the key's facts reaches Python first, and is
// attributed after.
func TestThePassMixWaitsForTheForward(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "pass:body.float")
	h := newDecideEdge(t, f, c, nil)
	var seq, pythonAt atomic.Int64
	reply := answerWith(http.StatusTeapot, []byte(`{"python":true}`))
	respond := func(w http.ResponseWriter) {
		pythonAt.Store(seq.Add(1))
		reply(w)
	}
	h.python.respond.Store(&respond)
	cache := &attributionCache{KeyCache: h.cache, t: t, failLookup: true,
		held: map[string]string{c.Facts.Key.LookupHash: c.Facts.Key.WorkspaceID}, seq: &seq}
	h.edge.decider.cfg.Cache = cache
	lenients, lenientAt := countLenient(h.edge.decider, &seq)
	h.send(t, c)
	py, decoded, peeked := pythonAt.Load(), lenientAt.Load(), cache.peekedAt.Load()
	if lenients.Load() != 1 || py == 0 || decoded <= py || peeked <= decoded {
		t.Fatalf("python at %d, the lenient decode at %d (%d of them), the peek at %d: the mix's work must follow "+
			"the forward", py, decoded, lenients.Load(), peeked)
	}
	if got := h.edge.DecideCounts(); !maps.Equal(got.PassedEnabled, map[string]int64{gwauth.ReasonFloat: 1}) {
		t.Fatalf("the pass mix: %+v", got)
	}
}

// paddedBody is an authorize body of exactly n bytes, its key the goldens',
// estimated_input_tokens a string (body.lax_type, so the strict parse reads
// no lookup hash), padded in metadata.
func paddedBody(t *testing.T, hash string, n int) string {
	t.Helper()
	head := `{"api_key_lookup_hash":"` + hash + `","model":"z-ai/glm-5.3","estimated_input_tokens":"1000",` +
		`"metadata":{"pad":"`
	tail := `"}}`
	if n < len(head)+len(tail) {
		t.Fatalf("%d bytes is too short", n)
	}
	return head + strings.Repeat("a", n-len(head)-len(tail)) + tail
}

// TestALargeBodyIsNeverDecodedForTheMix: the lenient decode reads a body
// only under lenientMaxBody; at that size or more, and at 5 MiB, past the
// strict parse's 4 MiB (body.unparseable, never decoded), the pass is
// counted unknown with no decode and no peek.
func TestALargeBodyIsNeverDecodedForTheMix(t *testing.T) {
	f := authorizeGoldens(t)
	base := authorizeCaseNamed(t, "pass:body.float")
	hash, ws := base.Facts.Key.LookupHash, base.Facts.Key.WorkspaceID
	signer := newBootSigner(t)
	jsonType := [][2]string{{"content-type", "application/json"}}
	for _, tc := range []struct {
		name        string
		size        int
		reason      string
		decoded     bool
		wantEnabled bool
	}{
		{"just under the bound", lenientMaxBody - 1, gwauth.ReasonLaxType, true, true},
		{"at the bound", lenientMaxBody, gwauth.ReasonLaxType, false, false},
		{"5 MiB", 5 << 20, gwauth.ReasonUnparseable, false, false},
	} {
		body := paddedBody(t, hash, tc.size)
		if len(body) != tc.size {
			t.Fatalf("%s: %d bytes", tc.name, len(body))
		}
		c := signer.sign(base, body, jsonType)
		h := newDecideEdge(t, f, base, nil)
		cache := &attributionCache{KeyCache: h.cache, t: t, failLookup: true, held: map[string]string{hash: ws}}
		h.edge.decider.cfg.Cache = cache
		lenients, _ := countLenient(h.edge.decider, nil)
		h.send(t, c)
		got := h.edge.DecideCounts()
		wantEnabled, wantUnknown := map[string]int64{}, map[string]int64{}
		if tc.wantEnabled {
			wantEnabled[tc.reason] = 1
		} else {
			wantUnknown[tc.reason] = 1
		}
		decodes, peeks := lenients.Load(), cache.peeks.Load()
		if !maps.Equal(got.Passed, map[string]int64{tc.reason: 1}) || !maps.Equal(got.PassedEnabled, wantEnabled) ||
			!maps.Equal(got.PassedUnknown, wantUnknown) || (decodes == 1) != tc.decoded || decodes > 1 ||
			(peeks == 1) != tc.decoded || peeks > 1 {
			t.Errorf("%s: passed %v, enabled %v, unknown %v, %d decodes, %d peeks", tc.name, got.Passed,
				got.PassedEnabled, got.PassedUnknown, decodes, peeks)
		}
	}
}

// TestARefusedPassIsAttributedBeforeItsEviction: a pass that names its
// lookup hash (here policy.pilot_off, before the key's facts) and that
// Python refuses (402, 403) has its key evicted on the request's goroutine,
// before the mix worker reads the cache. The mix reads the key first
// (resolveBeforeEvict), so the pass is still counted as of its enabled
// workspace, and the key is gone after.
func TestARefusedPassIsAttributedBeforeItsEviction(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "pass:policy.pilot_off")
	for _, status := range []int{http.StatusPaymentRequired, http.StatusForbidden} {
		h := newDecideEdge(t, f, c, nil)
		refusal := answerWith(status, errorBody(status, "refused", "refused"))
		h.python.respond.Store(&refusal)
		h.send(t, c)
		got := h.edge.DecideCounts()
		if !maps.Equal(got.Passed, map[string]int64{PassPilotOff: 1}) ||
			!maps.Equal(got.PassedEnabled, map[string]int64{PassPilotOff: 1}) || len(got.PassedUnknown) != 0 {
			t.Errorf("%d: passed %v, enabled %v, unknown %v; want the pass counted enabled", status, got.Passed,
				got.PassedEnabled, got.PassedUnknown)
		}
		h.cache.mu.Lock()
		evicted := slices.Clone(h.cache.evicted)
		h.cache.mu.Unlock()
		if _, held := h.cache.Peek(c.Facts.Key.LookupHash); held || !slices.Contains(evicted, c.Facts.Key.LookupHash) {
			t.Errorf("%d: the key was not evicted (evicted %v, still held %v)", status, evicted, held)
		}
	}
}

// blockingPeekCache is a cache whose Peek, the mix worker's read, waits
// for release; entered is closed once a Peek has begun.
type blockingPeekCache struct {
	KeyCache
	held    map[string]string
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (c *blockingPeekCache) Peek(hash string) (string, bool) {
	c.once.Do(func() { close(c.entered) })
	<-c.release
	ws, ok := c.held[hash]
	return ws, ok
}

// unreachable is a transport to a Python that cannot be reached.
type unreachable struct{}

func (unreachable) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("connection refused")
}

// TestTheCountIsOffTheHandlersPath: with the mix worker held inside its
// read of the cache, the edge, served over a real connection, answers the
// pass, and then answers a second request on the same keep-alive
// connection, which net/http reads only once the first handler has
// returned; and a pass whose Python cannot be reached gets its 502 at once.
// Each pass's mix is counted once the read is let go.
func TestTheCountIsOffTheHandlersPath(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "pass:body.float")
	held := map[string]string{c.Facts.Key.LookupHash: c.Facts.Key.WorkspaceID}
	serve := func(h *decideEdge) (*blockingPeekCache, *http.Client, *httptest.Server) {
		cache := &blockingPeekCache{KeyCache: h.cache, held: held, entered: make(chan struct{}),
			release: make(chan struct{})}
		h.edge.decider.cfg.Cache = cache
		srv := httptest.NewServer(h.edge)
		t.Cleanup(srv.Close)
		t.Cleanup(func() {
			select {
			case <-cache.release:
			default:
				close(cache.release)
			}
		})
		client := &http.Client{Timeout: 5 * time.Second,
			Transport: &http.Transport{MaxConnsPerHost: 1, DisableCompression: true}}
		t.Cleanup(client.CloseIdleConnections)
		return cache, client, srv
	}
	post := func(client *http.Client, url string, reused *bool) (int, error) {
		req, err := http.NewRequest(http.MethodPost, url+c.Request.Path, bytes.NewReader(c.body()))
		if err != nil {
			return 0, err
		}
		req.Header = c.header()
		if reused != nil {
			req = req.WithContext(httptrace.WithClientTrace(req.Context(), &httptrace.ClientTrace{
				GotConn: func(info httptrace.GotConnInfo) { *reused = info.Reused }}))
		}
		resp, err := client.Do(req)
		if err != nil {
			return 0, err
		}
		defer resp.Body.Close()
		_, err = io.Copy(io.Discard, resp.Body)
		return resp.StatusCode, err
	}
	waitEntered := func(cache *blockingPeekCache) {
		select {
		case <-cache.entered:
		case <-time.After(5 * time.Second):
			t.Fatal("the mix worker never read the cache")
		}
	}

	h := newDecideEdge(t, f, c, nil)
	cache, client, srv := serve(h)
	url := srv.URL
	if code, err := post(client, url, nil); err != nil || code != http.StatusTeapot {
		t.Fatalf("the first pass: %d %v", code, err)
	}
	waitEntered(cache)
	reused := false
	if code, err := post(client, url, &reused); err != nil || code != http.StatusTeapot || !reused {
		t.Fatalf("the second request on the connection, the first's count still running: %d %v (reused %v)",
			code, err, reused)
	}
	close(cache.release)
	// A pass's mix is handed off as its handler returns, and the proxy
	// flushes each write, so the client can have the second answer whole
	// before the second hand-off is made. The server's Close returns once
	// every handler has: both mixes are handed off by then.
	srv.Close()
	h.edge.decider.mixPending.Wait()
	if got := h.edge.DecideCounts(); !maps.Equal(got.PassedEnabled, map[string]int64{gwauth.ReasonFloat: 2}) {
		t.Fatalf("the two passes' mix: %+v", got)
	}

	// Python not reached: the ErrorHandler's 502, which net/http sends only
	// as the handler returns, comes while the count is held.
	h = newDecideEdge(t, f, c, nil)
	h.edge.proxy.Transport = tracedTransport{base: unreachable{}}
	cache, client, srv = serve(h)
	url = srv.URL
	start := time.Now()
	if code, err := post(client, url, nil); err != nil || code != http.StatusBadGateway {
		t.Fatalf("the unreachable pass: %d %v", code, err)
	}
	waitEntered(cache)
	took := time.Since(start)
	close(cache.release)
	h.edge.decider.mixPending.Wait()
	if got := h.edge.DecideCounts(); !maps.Equal(got.Passed, map[string]int64{gwauth.ReasonFloat: 1}) ||
		!maps.Equal(got.PassedEnabled, map[string]int64{gwauth.ReasonFloat: 1}) ||
		!maps.Equal(got.FloatAt, map[string]int64{gwauth.FloatAtMetadata: 1}) {
		t.Fatalf("the unreachable pass's mix (502 in %v): %+v", took, got)
	}
}

// TestACohortPassCarriesNoLookupHash: the proxy carries a lookup hash for
// the eviction only once the pipeline has gone past the request's cohort
// (Decided.LookupHash): a cohort reason, and any reason before it, carries
// none, though the pass mix reads the parse's own (parsedLookupHash). A
// gate after the cohort carries it.
func TestACohortPassCarriesNoLookupHash(t *testing.T) {
	f := authorizeGoldens(t)
	for _, tc := range []struct {
		caseName, reason string
		carries          bool
	}{
		{"pass:body.internal_model:monitor", gwauth.ReasonInternalModel, false},
		{"pass:body.lax_type", gwauth.ReasonLaxType, false},
		{"pass:body.float", gwauth.ReasonFloat, false},
		{"pass:policy.pilot_off", PassPilotOff, true},
	} {
		c := authorizeCaseNamed(t, tc.caseName)
		h := recording(newDecideEdge(t, f, c, nil))
		h.send(t, c)
		want := decidePassed{reason: tc.reason}
		if tc.carries {
			want.lookupHash = c.Facts.Key.LookupHash
		}
		if got := h.carried.carried(); len(got) != 1 || got[0] != want {
			t.Errorf("%s: carried %+v, want %+v", tc.caseName, got, want)
		}
		if got := h.edge.DecideCounts(); !maps.Equal(got.PassedEnabled, map[string]int64{tc.reason: 1}) {
			t.Errorf("%s: the mix %+v", tc.caseName, got)
		}
	}
}

// TestAFullMixQueueDropsAndCounts: the hand-off never waits. With the
// worker held in a read and the queue full, a mix is dropped and counted
// (mix_dropped); the queued ones are counted once the read is let go; and
// once the decide path has stopped, a hand-off is dropped too.
func TestAFullMixQueueDropsAndCounts(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "pass:body.float")
	h := newDecideEdge(t, f, c, nil)
	cache := &blockingPeekCache{KeyCache: h.cache, held: map[string]string{"lh": "ws"},
		entered: make(chan struct{}), release: make(chan struct{})}
	h.edge.decider.cfg.Cache = cache
	// A failure before the read is let go must not leave WaitDecide waiting
	// on the held worker (cleanups run last first: this one before it).
	released := sync.OnceFunc(func() { close(cache.release) })
	t.Cleanup(released)
	enabled := func(string) bool { return true }
	mix := func() *pendingMix { return &pendingMix{reason: "r", lookupHash: "lh", enabled: enabled} }
	dp := h.edge.decider
	dp.handOff(mix())
	select {
	case <-cache.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the worker never read")
	}
	for range mixQueue + 2 {
		dp.handOff(mix())
	}
	if got := h.edge.DecideCounts(); got.MixDropped != 2 {
		t.Fatalf("%d dropped with the queue full, want 2", got.MixDropped)
	}
	released()
	dp.mixPending.Wait()
	if got := h.edge.DecideCounts(); got.PassedEnabled["r"] != mixQueue+1 || got.MixDropped != 2 {
		t.Fatalf("counted %d, dropped %d", got.PassedEnabled["r"], got.MixDropped)
	}
	dp.flush()
	if !strings.Contains(h.logs.all(), " mix_dropped=2") {
		t.Fatalf("the line: %s", h.logs.all())
	}
	h.edge.WaitDecide()
	dp.handOff(mix())
	if got := h.edge.DecideCounts(); got.MixDropped != 3 || got.PassedEnabled["r"] != mixQueue+1 {
		t.Fatalf("after the stop: counted %d, dropped %d", got.PassedEnabled["r"], got.MixDropped)
	}
}

// TestAHandOffNeverStrandsAMixAtTheStop: hand-offs racing the stop, over
// many rounds, each end counted or dropped; none is queued after the worker
// has ended, where nothing would count it and every later wait for the mix
// worker would hang.
func TestAHandOffNeverStrandsAMixAtTheStop(t *testing.T) {
	const rounds, senders, each = 300, 8, 20
	for round := range rounds {
		dp := newDecider(DecideConfig{}, func(string, ...any) {}, time.Now, time.Minute)
		start := make(chan struct{})
		var wg sync.WaitGroup
		for range senders {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				for range each {
					dp.handOff(&pendingMix{reason: "r"})
				}
			}()
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			dp.stopMixes()
		}()
		done := make(chan struct{})
		go func() {
			close(start)
			wg.Wait()
			dp.stopMixes()
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Fatalf("round %d: the stop still waits on a mix no worker will count", round)
		}
		dp.mu.Lock()
		counted, dropped := sum(dp.total.passedUnknown), dp.total.mixDropped
		dp.mu.Unlock()
		if counted+dropped != senders*each {
			t.Fatalf("round %d: %d counted and %d dropped of %d", round, counted, dropped, senders*each)
		}
	}
}

// TestAQueuedMixHoldsABodyOnlyWhenItNeedsIt: a pass's mix keeps its body
// for the worker only while neither its workspace nor a lookup hash is
// known and the body is under lenientMaxBody.
func TestAQueuedMixHoldsABodyOnlyWhenItNeedsIt(t *testing.T) {
	small, large := make([]byte, lenientMaxBody-1), make([]byte, lenientMaxBody)
	stale := gwstate.Read{Key: gwstate.Stale, Found: true}
	stale.Facts.Key.WorkspaceID = "ws"
	for _, tc := range []struct {
		name    string
		decided Decided
		body    []byte
		kept    bool
	}{
		{"nothing known, under the bound", Decided{}, small, true},
		{"nothing known, at the bound", Decided{}, large, false},
		{"the workspace known", Decided{Workspace: "ws", LookupHash: "lh"}, small, false},
		{"the pipeline's lookup hash", Decided{LookupHash: "lh"}, small, false},
		{"the parse's lookup hash", Decided{parsedLookupHash: "lh"}, small, false},
		{"a stale entry held", Decided{Read: stale}, small, false},
		{"a key that names no workspace", Decided{Read: gwstate.Read{Key: gwstate.Fresh}}, small, false},
	} {
		m := pendingMixOf(gwauth.ReasonLaxType, tc.decided, tc.body, func(string) bool { return true })
		if (m.body != nil) != tc.kept {
			t.Errorf("%s: body held %v, want %v", tc.name, m.body != nil, tc.kept)
		}
	}
}

// TestWaitDecideCountsTheMixesStillQueued: mixes still queued when the
// decide path stops (the worker held in a read) are counted before the last
// line is logged, and WaitDecide returns only once they are.
func TestWaitDecideCountsTheMixesStillQueued(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "pass:body.float")
	h := newDecideEdge(t, f, c, nil)
	cache := &blockingPeekCache{KeyCache: h.cache, held: map[string]string{"lh": "ws"},
		entered: make(chan struct{}), release: make(chan struct{})}
	h.edge.decider.cfg.Cache = cache
	released := sync.OnceFunc(func() { close(cache.release) })
	t.Cleanup(released)
	enabled := func(string) bool { return true }
	for range 5 {
		h.edge.decider.handOff(&pendingMix{reason: "r", lookupHash: "lh", enabled: enabled})
	}
	select {
	case <-cache.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the worker never read")
	}
	done := make(chan struct{})
	go func() {
		h.edge.WaitDecide()
		close(done)
	}()
	select {
	case <-done:
		t.Fatal("WaitDecide returned with mixes still queued")
	case <-time.After(100 * time.Millisecond):
	}
	released()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("WaitDecide did not return once the mixes were counted")
	}
	var last string
	for _, l := range strings.Split(h.logs.all(), "\n") {
		if strings.Contains(l, "fastpath edge decide: ") {
			last = l
		}
	}
	if !strings.Contains(last, ` passed_enabled={"r":5} `) {
		t.Fatalf("the last line: %s", last)
	}
}
