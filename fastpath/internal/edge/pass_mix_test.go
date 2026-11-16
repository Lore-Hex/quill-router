package edge

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"maps"
	"net/http"
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

// TestTheDecideLineCarriesThePassMix: the period's line ends with the pass
// mix, after every field the line carried before it (a reader of the older
// line finds its fields where they were), reads back as its counts, and the
// next period's line starts again from nothing.
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
		`passed_enabled={"body.float":2,"boot.signature":1} passed_unknown={"boot.signature":1} enabled_unknown=1`; !strings.HasSuffix(lines[0], want) ||
		!strings.Contains(lines[0], ` passed={"body.float":2,"boot.signature":2,"workspace.not_enabled":1} `) ||
		!strings.Contains(lines[0], "insert_call_ms_max=0"+want) {
		t.Fatalf("the period's line: %s\nwant it to end %s", lines[0], want)
	}
	if want := ` float_at={} passed_enabled={} passed_unknown={} enabled_unknown=0`; !strings.HasSuffix(lines[1], want) {
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
	const corpusKey = "b57b533cfd309e134fe4b62ad6f0a17741de686ef5b1535dffd8e664e9eec507"
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
