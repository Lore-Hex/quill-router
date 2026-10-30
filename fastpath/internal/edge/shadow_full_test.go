package edge

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The full-response shadow over the P1 goldens: each case's request through
// the edge, a fake Python answering the case's golden response (or, for a
// case the goldens record only Python's status for, an answer of that
// status), a fake store holding the case's golden rows, and the real
// gwstate cache over the case's world. Go mints its own ids; the shadow
// compares under Python's.

// recordingCache is a KeyCache that counts the evictions Python's refusals
// make.
type recordingCache struct {
	KeyCache
	mu         sync.Mutex
	evicted    []string
	workspaces []string
	slow       time.Duration
	// more is added to the cache's own counts, as a test makes them grow.
	more gwstate.Counters
}

func (c *recordingCache) Lookup(lookupHash string, now time.Time) gwstate.Read {
	if c.slow > 0 {
		time.Sleep(c.slow)
	}
	return c.KeyCache.Lookup(lookupHash, now)
}

func (c *recordingCache) Counters() gwstate.Counters {
	var n gwstate.Counters
	if inner, ok := c.KeyCache.(cacheCounters); ok {
		n = inner.Counters()
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	n.Unrefreshed += c.more.Unrefreshed
	n.Dropped += c.more.Dropped
	n.ReadFailures += c.more.ReadFailures
	return n
}

// grow adds to the cache's counts.
func (c *recordingCache) grow(unrefreshed, dropped, failures int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.more.Unrefreshed += unrefreshed
	c.more.Dropped += dropped
	c.more.ReadFailures += failures
}

func (c *recordingCache) Evict(lookupHash string) {
	c.mu.Lock()
	c.evicted = append(c.evicted, lookupHash)
	c.mu.Unlock()
	c.KeyCache.Evict(lookupHash)
}

func (c *recordingCache) EvictWorkspace(workspace string) {
	c.mu.Lock()
	c.workspaces = append(c.workspaces, workspace)
	c.mu.Unlock()
	c.KeyCache.EvictWorkspace(workspace)
}

// fakeRows is the store's read-back of Python's rows.
type fakeRows struct {
	mu    sync.Mutex
	by    map[string]store.AuthorizationForCompare
	reads atomic.Int64
	slow  time.Duration
}

func (f *fakeRows) ReadAuthorizationForCompare(ctx context.Context, id string) (store.AuthorizationForCompare, bool, error) {
	f.reads.Add(1)
	if f.slow > 0 {
		select {
		case <-ctx.Done():
			return store.AuthorizationForCompare{}, false, ctx.Err()
		case <-time.After(f.slow):
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	r, ok := f.by[id]
	return r, ok, nil
}

// storedRows are a case's golden rows as the store reads them.
func storedRows(t *testing.T, c authorizeCase) store.AuthorizationForCompare {
	t.Helper()
	a, r := c.Expect.AuthorizationRow, c.Expect.ReservationRow
	str := func(m map[string]any, k string) string { s, _ := m[k].(string); return s }
	num := func(m map[string]any, k string) int64 { n, _ := m[k].(float64); return int64(n) }
	nstr := func(m map[string]any, k string) spanner.NullString {
		s, ok := m[k].(string)
		return spanner.NullString{StringVal: s, Valid: ok}
	}
	nint := func(m map[string]any, k string) spanner.NullInt64 {
		n, ok := m[k].(float64)
		return spanner.NullInt64{Int64: int64(n), Valid: ok}
	}
	ntime := func(m map[string]any, k string) spanner.NullTime {
		n, ok := m[k].(float64)
		return spanner.NullTime{Time: time.UnixMicro(int64(n)).UTC(), Valid: ok}
	}
	settled, _ := a["settled"].(bool)
	out := store.AuthorizationForCompare{Authorization: store.StoredAuthorization{
		AuthorizationID: str(a, "authorization_id"), WorkspaceID: str(a, "workspace_id"), KeyHash: str(a, "key_hash"),
		ReservationID: nstr(a, "reservation_id"), ModelID: str(a, "model_id"), Provider: str(a, "provider"),
		UsageType: str(a, "usage_type"), EstimatedMicrodollars: num(a, "estimated_microdollars"), Settled: settled,
		CreatedAt: time.UnixMicro(num(a, "created_at_us")).UTC(), TerminalAt: ntime(a, "terminal_at_us"),
		Payload: nstr(a, "payload"), IdempotencyFingerprint: nstr(a, "idempotency_fingerprint"),
		FinalizationOutcome: nstr(a, "finalization_outcome"), FinalizedCostMicrodollars: nint(a, "finalized_cost_microdollars"),
		StartedAt: ntime(a, "started_at_us"), HeartbeatSeq: nint(a, "heartbeat_seq"), HeartbeatAt: ntime(a, "heartbeat_at_us"),
		HeartbeatHash: nstr(a, "heartbeat_hash"), SelectedEndpointID: nstr(a, "selected_endpoint_id"),
		DeliveredUsage: nstr(a, "delivered_usage"), PricingSnapshot: nstr(a, "pricing_snapshot"),
		StageDBootKid: nstr(a, "stage_d_boot_kid"), InvocationNonce: nstr(a, "invocation_nonce"),
		GatewayRequestID: nstr(a, "gateway_request_id"),
	}}
	if len(r) > 0 {
		rs, _ := r["settled"].(bool)
		// The commit's timestamp (§5): some instant after the arrival.
		committed := spanner.NullTime{}
		if r["created_at"] == "commit_timestamp" {
			committed = spanner.NullTime{Time: out.Authorization.CreatedAt.Add(3 * time.Millisecond), Valid: true}
		}
		out.Reservation = &store.StoredReservation{
			ReservationID: str(r, "reservation_id"), WorkspaceID: nstr(r, "workspace_id"), KeyHash: nstr(r, "key_hash"),
			WsShard: nint(r, "ws_shard"), CreditShard: num(r, "credit_shard"), KeyShard: nint(r, "key_shard"),
			CreditReservedMicro: nint(r, "credit_reserved_micro"), KeyReservedMicro: nint(r, "key_reserved_micro"),
			ActualMicro: nint(r, "actual_micro"), HoldUsageType: nstr(r, "hold_usage_type"),
			SettledUsageType: nstr(r, "settled_usage_type"), AuthorizationID: nstr(r, "authorization_id"), Settled: rs,
			IdempotencyScope: nstr(r, "idempotency_scope"), IdempotencyFingerprint: nstr(r, "idempotency_fingerprint"),
			CreatedAt: committed, ExpiresAt: ntime(r, "expires_at_us"), TerminalAt: ntime(r, "terminal_at_us"),
		}
	}
	return out
}

// fakePython is one test's Python: it answers every request with respond,
// which the test sets before each, over one transport the test's edges
// share.
type fakePython struct {
	server    *httptest.Server
	transport *http.Transport
	respond   atomic.Pointer[func(http.ResponseWriter)]
	// hits counts the requests that reached it.
	hits atomic.Int64
}

func newFakePython(t *testing.T) *fakePython {
	t.Helper()
	py := &fakePython{transport: &http.Transport{DisableCompression: true, MaxIdleConnsPerHost: 4}}
	py.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		py.hits.Add(1)
		(*py.respond.Load())(w)
	}))
	t.Cleanup(func() {
		py.transport.CloseIdleConnections()
		py.server.Close()
	})
	return py
}

// fullEdge is an edge with the decide shadow on over one case's world, its
// snapshot and policy held, its decide copy read, and a fake Python.
type fullEdge struct {
	edge   *Edge
	python *fakePython
	cache  *recordingCache
	rows   *fakeRows
	logs   *logLines
}

// answer sets what Python answers next.
func (h *fullEdge) answer(respond func(http.ResponseWriter)) { h.python.respond.Store(&respond) }

type logLines struct {
	mu    sync.Mutex
	lines []string
}

func (l *logLines) logf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.lines = append(l.lines, fmt.Sprintf(format, args...))
}

func (l *logLines) all() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Join(l.lines, "\n")
}

// has reports a line that begins with prefix.
func (l *logLines) has(prefix string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return slices.ContainsFunc(l.lines, func(s string) bool { return strings.HasPrefix(s, prefix) })
}

func newFullEdge(t *testing.T, f authorizeFile, c authorizeCase, py *fakePython,
	configure func(*DecideShadowConfig)) *fullEdge {
	t.Helper()
	if py == nil {
		py = newFakePython(t)
	}
	h := &fullEdge{python: py, rows: &fakeRows{by: map[string]store.AuthorizationForCompare{}}, logs: &logLines{}}
	upstream, _ := url.Parse(py.server.URL)
	snapshotURL, _ := url.Parse(py.server.URL + "/internal/fastpath/routing-snapshot")
	policyURL, _ := url.Parse(py.server.URL + "/internal/fastpath/edge-policy")
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys := []SnapshotKey{{Kid: "routing-snapshot-1", PublicKey: public}}
	p := policyOf(t, f, c)
	w := &fakeWorld{c: c}
	h.cache = &recordingCache{KeyCache: warmCache(t, w, lookupHashOf(c.body()), c.at())}
	cfg := DecideShadowConfig{Cache: h.cache, Decides: &fakeDecides{}, Rows: h.rows, SampleEvery: 1,
		ReadsPerSecond: 1e9}
	if configure != nil {
		configure(&cfg)
	}
	e, err := New(Config{Token: f.InternalToken, Upstream: upstream, Boots: bootsOf(t, f, c),
		Clock: func() time.Time { return c.at() }, Logf: h.logs.logf, Transport: py.transport,
		Snapshot:     SnapshotConfig{URL: snapshotURL, Keys: keys, Plane: "gcp/us-central1"},
		Policy:       PolicyConfig{URL: policyURL, Keys: keys, Plane: "gcp/us-central1"},
		DecideShadow: &cfg})
	if err != nil {
		t.Fatal(err)
	}
	snap := snapshotOver(t, f, p)
	e.poller.current.Store(hold(snap, routingsnapshot.Claims{CompiledAt: snap.CompiledAt, NotAfter: snap.NotAfter,
		SHA256: "golden-snapshot"}))
	e.policy.current.Store(&heldPolicy{policy: p, claims: routingsnapshot.PolicyClaims{IssuedAt: p.IssuedAt,
		NotAfter: p.NotAfter, SHA256: "golden-policy"}})
	view := decideAll(p, c.Facts.Key.WorkspaceID, c.at())
	e.full.decides.current.Store(&view)
	h.edge = e
	// Whatever a test does, every byte the shadow reserved comes back once
	// the requests it followed are compared (a leak would starve the
	// shadow over a soak).
	t.Cleanup(func() {
		e.WaitDecideShadow()
		if n := e.full.bytes.Load(); n != 0 {
			t.Errorf("the decide shadow still holds %d bytes of its budget", n)
		}
	})
	return h
}

// send is the case's request through the edge; it returns Python's answer
// as the enclave received it, once the shadow has compared it.
func (h *fullEdge) send(t *testing.T, c authorizeCase) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body()))
	req.Header = c.header()
	rec := httptest.NewRecorder()
	h.edge.ServeHTTP(rec, req)
	h.edge.WaitDecideShadow()
	return rec
}

func answerWith(status int, body []byte, headers ...string) func(http.ResponseWriter) {
	return func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		for i := 0; i+1 < len(headers); i += 2 {
			w.Header().Set(headers[i], headers[i+1])
		}
		w.WriteHeader(status)
		_, _ = w.Write(body)
	}
}

func errorBody(status int, kind, message string) []byte {
	b, _ := json.Marshal(map[string]any{"error": map[string]any{"code": status, "message": message, "type": kind,
		"source": "router"}})
	return b
}

// pythonsLegacyAnswer is, for a case the goldens record only Python's 200
// status for (its pilot's owner refused, so it answered on today's path),
// a 200 of the cohort's shape under the case's own ids: the composition
// from the case's world with Python's pinned ids, zero timing. It stands
// for Python's bytes, which the goldens do not hold for these cases.
func pythonsLegacyAnswer(t *testing.T, f authorizeFile, c authorizeCase, replay bool) []byte {
	t.Helper()
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	if d.ForShadow() == nil {
		t.Fatalf("%s: no decision to stand for Python's answer: %q", c.Name, d.Pass)
	}
	composed, err := d.ForShadow().Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Fatal(err)
	}
	out := composed.Render(gwauth.Timing{}, false)
	if replay {
		out = bytes.Replace(out, []byte(`"idempotent_replay":false`), []byte(`"idempotent_replay":true`), 1)
	}
	return out
}

// pythonOfCase is Python's answer to the case, and the class the shadow
// must give it.
func pythonOfCase(t *testing.T, f authorizeFile, c authorizeCase) (func(http.ResponseWriter), string) {
	t.Helper()
	e := c.Expect
	switch e.Outcome {
	case "answer":
		if _, ok := evaluatorPasses[c.Name]; ok {
			return answerWith(200, []byte(e.Response)), ClassPythonAnsweredGoPassed + "." + PassEvaluate
		}
		return answerWith(200, []byte(e.Response)), ClassAgreeFull
	case "unavailable":
		if e.Python.Status != 0 {
			// Python failed outright where Go answers the 503 (a
			// FailedPrecondition it counts permanent, §8.4).
			return answerWith(e.Python.Status, []byte(`{"detail":"Internal Server Error"}`)),
				ClassPythonError + "." + fmt.Sprint(e.Python.Status)
		}
		return answerWith(503, []byte(e.Response), "Retry-After", "1"), ClassPythonUnavailable
	}
	kind := ""
	if e.Python.Type != nil {
		kind = *e.Python.Type
	}
	status, reason := e.Python.Status, e.Reason
	if windowReason(reason) && status == 200 {
		// The key's windows gate the decision, not the comparison: Python's
		// 200 is compared as the cohort's (key-windows section 8.2).
		return answerWith(200, pythonsLegacyAnswer(t, f, c, false)), ClassAgreeFull
	}
	if postAdmission(reason) {
		switch {
		case strings.HasPrefix(reason, "owner."):
			// Go never asks the owner in the shadow: it would have
			// answered, and Python's 200 agrees.
			return answerWith(200, pythonsLegacyAnswer(t, f, c, false)), ClassAgreeFull
		case reason == "insert.scope_taken" && status == 200:
			return answerWith(200, pythonsLegacyAnswer(t, f, c, true)),
				ClassPythonAnsweredGoPassed + ".insert.scope_taken"
		case reason == "insert.scope_taken":
			return answerWith(status, errorBody(status, kind, "Idempotency key reused with a different request")),
				ClassGoPassed + ".insert.scope_taken"
		case status == 503:
			return answerWith(status, errorBody(status, kind, unavailableMessage)), ClassPythonUnavailable
		default:
			return answerWith(status, []byte(`{"detail":"Internal Server Error"}`)),
				ClassPythonError + "." + fmt.Sprint(status)
		}
	}
	if reason == PassEvaluateRefused {
		// Go names the refusals its evaluator does not reproduce
		// evaluate.pass (TestEveryGoldenCaseThroughThePipeline).
		in, _ := caseInputs(t, f, c)
		reason = Decide(in).Pass
	}
	if status == 200 {
		return answerWith(200, []byte(`{"data":{"idempotent_replay":false}}`)), ClassPythonAnsweredGoPassed + "." + reason
	}
	message := "refused"
	if reason == PassWorkspacePaused && status == 503 {
		message = pauseMessage
	}
	return answerWith(status, errorBody(status, kind, message)), ClassGoPassed + "." + reason
}

// TestEveryGoldenCaseThroughTheDecideShadow: every answered case Go
// decides is agree_full with no field differing and its rows agreeing (the
// one whose tags a read-back sorts counted as Python's read-back), the two
// whose provider preferences the evaluator passes are
// python_answered_go_passed.evaluate.pass, the two windowed keys passed at
// the key-windows gate compare Python's 200 as agree_full, every other pass
// case is counted under its §3 reason by what Python answered, the
// unavailable case is Python's unavailable 503, and Python's 401, 402 and
// 403 evict the key and its workspace.
func TestEveryGoldenCaseThroughTheDecideShadow(t *testing.T) {
	f := authorizeGoldens(t)
	py := newFakePython(t)
	classes := map[string]int{}
	rowsAgree, readBack, affinityAgree := 0, 0, 0
	for _, c := range f.Cases {
		respond, want := pythonOfCase(t, f, c)
		h := newFullEdge(t, f, c, py, nil)
		h.answer(respond)
		if c.Expect.Outcome == "answer" {
			h.rows.by[c.IDs.AuthorizationID] = storedRows(t, c)
		}
		rec := h.send(t, c)
		probe := httptest.NewRecorder()
		respond(probe)
		if !bytes.Equal(rec.Body.Bytes(), probe.Body.Bytes()) || rec.Code != probe.Code {
			t.Errorf("%s: the enclave got %d %q, not Python's %d", c.Name, rec.Code, rec.Body.Bytes(), probe.Code)
		}
		got := h.edge.DecideShadowCounts()
		if got.Classes[want] != 1 || len(got.Classes) != 1 {
			t.Errorf("%s: classes %v, want %s (fields %v)", c.Name, got.Classes, want, got.Fields)
			continue
		}
		classes[want]++
		if want == ClassAgreeFull && len(got.Fields) != 0 {
			t.Errorf("%s: agree_full with fields %v", c.Name, got.Fields)
		}
		if c.Expect.Outcome == "answer" && want == ClassAgreeFull {
			if got.Rows["agree"] != 1 || got.Cohort[ClassAgreeFull] != 1 {
				t.Errorf("%s: rows %v, fields %v, cohort %v", c.Name, got.Rows, got.Fields, got.Cohort)
			} else {
				rowsAgree++
				if c.Expect.Affinity != nil {
					affinityAgree++
				}
			}
			// Only sorted tags tell a read-back's bytes from an insert's.
			if hasLabel(c.Labels, "read_back.sorted_tags") != (got.ReadBack == 1) {
				t.Errorf("%s: read back %d after insert %q", c.Name, got.ReadBack, c.Insert)
			}
			readBack += got.ReadBack
		}
		// The request names its key once it parses; a boot, body or header
		// pass is before that.
		named := !strings.HasPrefix(c.Expect.Reason, "body.") && !strings.HasPrefix(c.Expect.Reason, "header.") &&
			!strings.HasPrefix(c.Expect.Reason, "boot.")
		switch c.Expect.Python.Status {
		case 401, 402, 403:
			if named && got.Evictions == 0 {
				t.Errorf("%s: Python's %d evicted nothing", c.Name, c.Expect.Python.Status)
			}
		default:
			if got.Evictions != 0 && c.Expect.Outcome == "pass" && c.Expect.Reason != PassWorkspacePaused {
				t.Errorf("%s: %d evictions on Python's %d", c.Name, got.Evictions, c.Expect.Python.Status)
			}
		}
	}
	t.Logf("classes over the goldens: %v; %d row pairs agree (%d naming a cache_affinity_key); %d answers after a "+
		"read-back", classes, rowsAgree, affinityAgree, readBack)
	// A request naming a cache_affinity_key is the cohort's (§1 item 5):
	// its cold answer is agree_full with its rows read back, like any.
	if classes[ClassAgreeFull] != 192+5+2 || rowsAgree != 192 || affinityAgree != 18 || readBack != 1 ||
		classes[ClassPythonUnavailable] != 2 || classes[ClassPythonAnsweredGoPassed+"."+PassEvaluate] != 1 {
		t.Fatalf("agree_full %d, rows %d (%d with a cache affinity key), read back %d, unavailable %d: not the goldens' "+
			"counts", classes[ClassAgreeFull], rowsAgree, affinityAgree, readBack, classes[ClassPythonUnavailable])
	}
}

// warmDiffer reports r a differ whose one field is differ.affinity_warm,
// its rows read against Go's decision in Python's order.
func warmDiffer(r fullResult) bool {
	return r.class == ClassDifferAny && slices.Equal(r.fields, []string{FieldAffinityWarm}) && r.warm != nil &&
		r.decided && r.rows
}

// TestAffinityIsDecidedInTheColdOrder (§1 item 5): a request naming a
// cache_affinity_key is admittable with Go's cold order, as the control
// without a key is; Python's answer with no session is agree_full, rows
// read back; Python's answer re-ranked for a remembered endpoint (within
// its model, or the one warm endpoint with fallbacks off, at its own
// estimate) is a differ named differ.affinity_warm; and anything else is
// differ with the fields that differ, never named warm: an order that is
// no promotion, or a warm endpoint's bytes with anything else changed.
func TestAffinityIsDecidedInTheColdOrder(t *testing.T) {
	f := authorizeGoldens(t)
	for _, name := range []string{"answer:app_categories_empty", "answer:cache_affinity:warm",
		"answer:cache_affinity:warm:fallback_models", "answer:cache_affinity:warm:no_fallbacks"} {
		c := authorizeCaseNamed(t, name)
		in, _ := caseInputs(t, f, c)
		in.Mint = Mint{} // Go's own ids: Python's are substituted
		d := Decide(in)
		dec, ok := d.Admit()
		if d.Pass != "" || !ok || dec.Affinity != (c.Expect.Affinity != nil) {
			t.Fatalf("%s: pass %q, admittable %v", name, d.Pass, ok)
		}
		classOf := func(body string) fullResult { return compareFull(d, pyAnswer{status: 200, body: []byte(body)}) }
		if r := classOf(c.Expect.Response); r.class != ClassAgreeFull || len(r.fields) != 0 || !r.decided || !r.rows ||
			r.warm != nil {
			t.Errorf("%s: the cold bytes: %+v", name, r)
		}
		a := c.Expect.Affinity
		if a == nil {
			continue
		}
		if !slices.Equal(dec.Candidates, a.ColdCandidates) {
			t.Errorf("%s: candidates %v, want the cold %v", name, dec.Candidates, a.ColdCandidates)
		}
		if r := classOf(a.Response); !warmDiffer(r) || !slices.Equal(r.warm.Candidates, a.Candidates) ||
			r.warm.Estimate != a.Estimate {
			t.Errorf("%s: the warm bytes: %+v", name, r)
		}
		// Controls: the warm bytes with the estimate off by one, or a tag
		// changed, are differ, counted by field and not named warm; so is
		// an order that is not one endpoint promoted.
		for _, v := range []struct {
			name   string
			change func(pyjson.Object) pyjson.Object
			field  string
		}{
			{"estimate", func(o pyjson.Object) pyjson.Object {
				return setMember(o, "estimated_cost_microdollars", json.Number(strconv.FormatInt(a.Estimate+1, 10)))
			}, "differ.estimate"},
			{"a tag", func(o pyjson.Object) pyjson.Object {
				return setMember(o, "tags", pyjson.Object{{Key: "x", Value: "y"}})
			}, "differ.tags"},
			{"the order reversed", func(o pyjson.Object) pyjson.Object {
				l, _ := memberOf(o, "route_candidates").([]any)
				reversed := slices.Clone(l)
				slices.Reverse(reversed)
				return setMember(o, "route_candidates", reversed)
			}, "differ.candidates"},
		} {
			r := classOf(string(mutated(t, a.Response, v.change)))
			if len(a.Candidates) < 3 && v.name == "the order reversed" {
				continue
			}
			if r.class != ClassDifferAny || !slices.Contains(r.fields, v.field) || slices.Contains(r.fields, FieldAffinityWarm) ||
				r.warm != nil || !r.decided {
				t.Errorf("%s: %s: %+v, want differ with %s", name, v.name, r, v.field)
			}
		}
	}
}

// rendered is Go's decision rendered in another candidate order (and
// estimate) under Python's ids and timing: the bytes Python would answer
// had it that order, every first-candidate member consistent.
func rendered(t *testing.T, d Decided, c authorizeCase, ids []string, estimate int64) []byte {
	t.Helper()
	dec := d.ForShadow()
	byID := map[string]gwauth.Candidate{}
	for _, cand := range dec.Input.Candidates {
		byID[cand.EndpointID] = cand
	}
	var candidates []gwauth.Candidate
	for _, id := range ids {
		cand, ok := byID[id]
		if !ok {
			e, found := dec.Snapshot.Endpoint(id)
			if !found {
				t.Fatalf("%s: no endpoint %s", c.Name, id)
			}
			cand = gwauth.CandidateOf(e)
		}
		candidates = append(candidates, cand)
	}
	warm := *dec
	warm.Input.Candidates, warm.Input.Estimate = candidates, estimate
	staged, err := warm.Restage(c.IDs.AuthorizationID, c.IDs.ReservationID, dec.Input.IdempotencyKey, dec.Input.At)
	if err != nil {
		t.Fatal(err)
	}
	composed, err := staged.Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Fatal(err)
	}
	return composed.Render(timingOfMap(c.Expect.Timing), false)
}

// TestAffinityWarmIsPythonsRerank (§1 item 5): differ.affinity_warm names
// exactly the order routing_state.rank gives, every first-candidate member
// rendered consistently. Under a provider.order naming a provider Python keeps the
// cold order, so a promotion is differ; under the default partition a
// second model's endpoint moved ahead of the primary model is differ;
// under partition none it is warm (the golden); an empty provider.order
// names no provider, so the warm goldens with one are warm.
func TestAffinityWarmIsPythonsRerank(t *testing.T) {
	f := authorizeGoldens(t)
	decide := func(name string) (Decided, authorizeCase) {
		c := authorizeCaseNamed(t, name)
		in, _ := caseInputs(t, f, c)
		d := Decide(in)
		if d.Pass != "" || d.ForShadow() == nil {
			t.Fatalf("%s: pass %q", name, d.Pass)
		}
		return d, c
	}
	classify := func(d Decided, body []byte) fullResult { return compareFull(d, pyAnswer{status: 200, body: body}) }
	// The control: the rendering reproduces Python's warm bytes.
	d, c := decide("answer:cache_affinity:warm")
	a := c.Expect.Affinity
	if got := rendered(t, d, c, a.Candidates, a.Estimate); !bytes.Equal(got, []byte(a.Response)) {
		t.Fatalf("the rendering is not Python's warm bytes:\n got %s\nwant %s", got, a.Response)
	}
	// A provider.order: [together, fireworks, wafer] is a promotion Python
	// would not make.
	d, c = decide("answer:cache_affinity:cold:order")
	a = c.Expect.Affinity
	promoted := []string{a.ColdCandidates[1], a.ColdCandidates[0], a.ColdCandidates[2]}
	if r := classify(d, rendered(t, d, c, promoted, a.ColdEstimate)); r.class != ClassDifferAny ||
		!slices.Contains(r.fields, "differ.candidates") || r.warm != nil {
		t.Errorf("under a provider.order: %+v, want differ", r)
	}
	// The default partition: the second model's endpoint ahead of the
	// primary model's, every member consistent, is differ; its own golden
	// (within its model's run) is warm.
	d, c = decide("answer:cache_affinity:warm:fallback_models")
	a = c.Expect.Affinity
	across := slices.Concat([]string{a.ColdCandidates[3]}, a.ColdCandidates[:3], a.ColdCandidates[4:])
	if r := classify(d, rendered(t, d, c, across, a.ColdEstimate)); r.class != ClassDifferAny ||
		!slices.Contains(r.fields, "differ.candidates") || r.warm != nil {
		t.Errorf("across models under the default partition: %+v, want differ", r)
	}
	if r := classify(d, []byte(a.Response)); !warmDiffer(r) {
		t.Errorf("within the model's run: %+v, want differ.affinity_warm", r)
	}
	// Partition none: the same move is Python's order (the golden); the
	// within-run order is then not.
	d, c = decide("answer:cache_affinity:warm:partition_none:fallback_models")
	a = c.Expect.Affinity
	if a.Candidates[0] == a.ColdCandidates[0] {
		t.Fatalf("the partition none golden did not move across models: %v", a.Candidates)
	}
	if r := classify(d, []byte(a.Response)); !warmDiffer(r) {
		t.Errorf("partition none: %+v, want differ.affinity_warm", r)
	}
	withinRun := slices.Concat(a.ColdCandidates[:1], []string{a.ColdCandidates[2]}, a.ColdCandidates[1:2],
		a.ColdCandidates[3:])
	if r := classify(d, rendered(t, d, c, withinRun, a.ColdEstimate)); r.class != ClassDifferAny || r.warm != nil {
		t.Errorf("partition none, within the run: %+v, want differ", r)
	}
	// An empty provider.order names no provider.
	for _, name := range []string{"answer:cache_affinity:warm:empty_order",
		"answer:cache_affinity:warm:no_fallbacks:empty_order",
		"answer:cache_affinity:warm:top_level_no_fallbacks:empty_order"} {
		d, c := decide(name)
		if r := classify(d, []byte(c.Expect.Affinity.Response)); !warmDiffer(r) {
			t.Errorf("%s: %+v, want differ.affinity_warm", name, r)
		}
	}
}

// TestRerankReadsTheOrderAndThePartition: what routing_state.rank reads
// of provider: an order names a provider only when non-empty (a list or a
// comma string), and the sort is partitioned by model unless
// provider.sort.partition is none.
func TestRerankReadsTheOrderAndThePartition(t *testing.T) {
	f := authorizeGoldens(t)
	in, _ := caseInputs(t, f, authorizeCaseNamed(t, "answer:cache_affinity:warm"))
	for _, v := range []struct {
		provider         string
		ordered, byModel bool
	}{
		{`null`, false, true},
		{`{}`, false, true},
		{`{"order":[]}`, false, true},
		{`{"order":""}`, false, true},
		{`{"order":["fireworks"]}`, true, true},
		{`{"order":"fireworks, together"}`, true, true},
		{`{"sort":"price"}`, false, true},
		{`{"sort":{"sort":"price","partition":"model"}}`, false, true},
		{`{"sort":{"sort":"price","partition":"none"}}`, false, false},
		{`{"order":["together"],"sort":{"partition":"none"}}`, true, false},
	} {
		req := routingsnapshot.Request{Model: "z-ai/glm-5.3", Provider: json.RawMessage(v.provider)}
		ordered, byModel, ok := in.Snapshot.Rerank(req)
		if !ok || ordered != v.ordered || byModel != v.byModel {
			t.Errorf("%s: ordered %v byModel %v ok %v, want %v %v", v.provider, ordered, byModel, ok, v.ordered, v.byModel)
		}
	}
	if _, _, ok := in.Snapshot.Rerank(routingsnapshot.Request{Model: "z-ai/glm-5.3",
		Provider: json.RawMessage(`{"order":[1]}`)}); ok {
		t.Error("an order of numbers parses")
	}
}

// warmRows are Python's rows for a warm answer: the cold golden rows with
// the authorization's first candidate and candidate lists those of
// Python's warm order (an order within the same model, at the same
// estimate).
func warmRows(t *testing.T, c authorizeCase) store.AuthorizationForCompare {
	t.Helper()
	a := c.Expect.Affinity
	rows := storedRows(t, c)
	tree, err := pyjson.Decode([]byte(rows.Authorization.Payload.StringVal))
	if err != nil {
		t.Fatal(err)
	}
	payload := tree.(pyjson.Object)
	first := strings.SplitN(strings.TrimPrefix(a.Candidates[0], memberOf(payload, "model_id").(string)+"@"), "/", 2)[0]
	ids := make([]any, len(a.Candidates))
	for i, id := range a.Candidates {
		ids[i] = id
	}
	payload = setMember(setMember(setMember(payload, "candidate_endpoint_ids", ids), "endpoint_id", a.Candidates[0]),
		"provider", first)
	raw, err := pyjson.Dumps(payload, pyjson.Options{})
	if err != nil {
		t.Fatal(err)
	}
	rows.Authorization.Payload.StringVal, rows.Authorization.Provider = string(raw), first
	return rows
}

// TestAWarmRerankIsADifferWithItsRowsRead (§1 item 5, §11): Python's warm
// answer to a request naming a cache_affinity_key is counted as any
// cohort answer is: a differ with the one field differ.affinity_warm, in
// classes, cohort and decide, and its rows read back against Go's decision
// in Python's order (they agree). The control: the same answer over rows
// in Go's cold order reads them as differing, so the read compared Python's
// order. The count line names the warm differ among the fields, with no
// count kept apart.
func TestAWarmRerankIsADifferWithItsRowsRead(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:cache_affinity:warm")
	for _, v := range []struct {
		name string
		rows store.AuthorizationForCompare
		want map[string]int
	}{
		{"rows in Python's order", warmRows(t, c), map[string]int{"agree": 1}},
		{"rows in Go's cold order", storedRows(t, c), map[string]int{"differ": 1}},
	} {
		h := newFullEdge(t, f, c, nil, nil)
		h.answer(answerWith(200, []byte(c.Expect.Affinity.Response)))
		h.rows.by[c.IDs.AuthorizationID] = v.rows
		h.send(t, c)
		got := h.edge.DecideShadowCounts()
		if !maps.Equal(got.Classes, map[string]int{ClassDifferAny: 1}) || !maps.Equal(got.Cohort, got.Classes) ||
			!maps.Equal(got.Decide, map[string]int{"on": 1}) || !maps.Equal(got.Rows, v.want) ||
			got.Fields[FieldAffinityWarm] != 1 || h.rows.reads.Load() != 1 {
			t.Fatalf("%s: counts %+v, rows read %d", v.name, got, h.rows.reads.Load())
		}
		if v.want["agree"] == 1 && len(got.Fields) != 1 {
			t.Errorf("%s: fields %v, want %s alone", v.name, got.Fields, FieldAffinityWarm)
		}
		if v.want["differ"] == 1 && got.Fields["row.provider"] != 1 {
			t.Errorf("%s: fields %v, want the provider's row difference", v.name, got.Fields)
		}
		h.edge.full.flush()
		logs := h.logs.all()
		if v.want["agree"] == 1 && !strings.Contains(logs, "agree_full=0 differ=1 would_admit_python_refused=0 "+
			`read_back=0 classes={"differ":1} fields={"differ.affinity_warm":1} rows={"agree":1} decide={"on":1} `+
			`cohort={"differ":1}`) || strings.Contains(logs, "affinity=") {
			t.Fatalf("%s: the summary: %s", v.name, logs)
		}
	}
}

// TestAWarmRerankAfterARereadIsCountedReadBack (§6, §11): with the key's
// stored tags out of order, Python's warm 200 answered from a read-back
// (tags sorted) is Go's decision in Python's order rendered after a
// read-back: a differ named differ.affinity_warm, counted read_back as a
// cold answer from a read-back is, its rows read back and agreeing.
func TestAWarmRerankAfterARereadIsCountedReadBack(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:cache_affinity:warm")
	keyBody := strings.Replace(*c.Key.KeyBody, `"usage_microdollars":`, `"tags":{"zeta":"1","alpha":"2"},"usage_microdollars":`, 1)
	if keyBody == *c.Key.KeyBody || strings.Count(keyBody, `"tags":`) != 1 {
		t.Fatal("the key body is not the one this test stores tags in")
	}
	c.Key.KeyBody = &keyBody
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	dec, ok := d.Admit()
	if !ok {
		t.Fatalf("pass %q", d.Pass)
	}
	a := c.Expect.Affinity
	byID := map[string]gwauth.Candidate{}
	for _, cand := range dec.Input.Candidates {
		byID[cand.EndpointID] = cand
	}
	warm := *dec
	warm.Input.Candidates, warm.Input.Estimate = nil, a.Estimate
	for _, id := range a.Candidates {
		warm.Input.Candidates = append(warm.Input.Candidates, byID[id])
	}
	staged, err := warm.Restage(c.IDs.AuthorizationID, c.IDs.ReservationID, dec.Input.IdempotencyKey, dec.Input.At)
	if err != nil {
		t.Fatal(err)
	}
	composed, err := staged.Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Fatal(err)
	}
	inserted, reread := composed.Render(c.Expect.timing(), false), composed.Render(c.Expect.timing(), true)
	if bytes.Equal(inserted, reread) {
		t.Fatal("the read-back rendering is the inserted one: the tags did not sort")
	}
	rows := warmRows(t, c)
	tree, err := pyjson.Decode([]byte(rows.Authorization.Payload.StringVal))
	if err != nil {
		t.Fatal(err)
	}
	payload := append(tree.(pyjson.Object), pyjson.Member{Key: "tags",
		Value: pyjson.Object{{Key: "alpha", Value: "2"}, {Key: "zeta", Value: "1"}}})
	raw, err := pyjson.Dumps(payload, pyjson.Options{SortKeys: true, EnsureASCII: true})
	if err != nil {
		t.Fatal(err)
	}
	rows.Authorization.Payload.StringVal = string(raw)
	for _, v := range []struct {
		name     string
		body     []byte
		readBack int
	}{
		{"as inserted", inserted, 0},
		{"after a read-back", reread, 1},
	} {
		h := newFullEdge(t, f, c, nil, nil)
		h.answer(answerWith(200, v.body))
		h.rows.by[c.IDs.AuthorizationID] = rows
		h.send(t, c)
		got := h.edge.DecideShadowCounts()
		if !maps.Equal(got.Classes, map[string]int{ClassDifferAny: 1}) ||
			!maps.Equal(got.Fields, map[string]int{FieldAffinityWarm: 1}) ||
			!maps.Equal(got.Rows, map[string]int{"agree": 1}) || got.ReadBack != v.readBack {
			t.Errorf("%s: counts %+v", v.name, got)
		}
	}
}

// TestAnAffinityRequestThatPassesIsCountedAsAnyOther (§11): a request
// naming a cache_affinity_key that passes, on a cold key cache or on a
// header after the parse, is counted in classes as the same pass of a
// request without the key is (the stale and followed figures, and the
// settlement header pass share, read them), whatever Python answered.
func TestAnAffinityRequestThatPassesIsCountedAsAnyOther(t *testing.T) {
	f := authorizeGoldens(t)
	for _, name := range []string{"answer:cache_affinity:warm", "answer:app_categories_empty"} {
		c := authorizeCaseNamed(t, name)
		h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) {
			cold, err := gwstate.NewCache(gwstate.CacheConfig{Reader: &fakeWorld{c: c, slow: time.Second}})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(cold.Close)
			cfg.Cache = &recordingCache{KeyCache: cold}
		})
		h.answer(answerWith(200, []byte(`{"data":{"idempotent_replay":false}}`)))
		h.send(t, c)
		if got := h.edge.DecideShadowCounts(); !maps.Equal(got.Classes, map[string]int{ClassStale + ".key": 1}) {
			t.Errorf("%s on a cold cache: classes %v", name, got.Classes)
		}
		for _, status := range []int{200, 403} {
			c := c
			c.Request.Headers = append(slices.Clone(c.Request.Headers), [2]string{"X-TR-Settlement-Mode", "sync"})
			h := newFullEdge(t, f, c, nil, nil)
			class := ClassPythonAnsweredGoPassed + "." + gwauth.ReasonSettlementAsync
			if status == 200 {
				h.answer(answerWith(200, []byte(`{"data":{"idempotent_replay":false}}`)))
			} else {
				h.answer(answerWith(status, errorBody(status, "forbidden", "refused")))
				class = ClassGoPassed + "." + gwauth.ReasonSettlementAsync
			}
			h.send(t, c)
			if got := h.edge.DecideShadowCounts(); !maps.Equal(got.Classes, map[string]int{class: 1}) {
				t.Errorf("%s with the settlement header on Python's %d: classes %v", name, status, got.Classes)
			}
		}
	}
}

// TestWouldAdmitPythonRefusedIsCritical: the positive control of the
// critical class. Python refuses a case Go would answer: the class is
// would_admit_python_refused.<status>.<type>, a CRITICAL line is logged at
// once, and the key and its workspace are evicted; Python's pause 503 is
// one too, its other 503s are not.
func TestWouldAdmitPythonRefusedIsCritical(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	py := newFakePython(t)
	for _, v := range []struct {
		status        int
		kind, message string
		want          string
		evicts        bool
	}{
		{403, "forbidden", "billing_paused", ClassWouldAdmit + ".403.forbidden", true},
		{401, "invalid_api_key", "Invalid API key", ClassWouldAdmit + ".401.invalid_api_key", true},
		{402, "payment_required", "Insufficient credits", ClassWouldAdmit + ".402.payment_required", true},
		{503, "service_unavailable", pauseMessage, ClassWouldAdmit + ".503.service_unavailable", true},
		{400, "model_not_supported", "No route", ClassWouldAdmit + ".400.model_not_supported", false},
		{503, "service_unavailable", unavailableMessage, ClassPythonUnavailable, false},
		{500, "internal", "boom", ClassPythonError + ".500", false},
	} {
		h := newFullEdge(t, f, c, py, nil)
		h.answer(answerWith(v.status, errorBody(v.status, v.kind, v.message)))
		h.send(t, c)
		got := h.edge.DecideShadowCounts()
		if got.Classes[v.want] != 1 {
			t.Errorf("%d %s: classes %v, want %s", v.status, v.message, got.Classes, v.want)
		}
		critical := strings.Contains(h.logs.all(), "CRITICAL: class="+v.want)
		if critical != strings.HasPrefix(v.want, ClassWouldAdmit) {
			t.Errorf("%d %s: critical logged %v; logs %s", v.status, v.message, critical, h.logs.all())
		}
		h.cache.mu.Lock()
		evicted, workspaces := h.cache.evicted, h.cache.workspaces
		h.cache.mu.Unlock()
		if v.evicts != (len(evicted) == 1 && evicted[0] == c.Facts.Key.LookupHash && len(workspaces) == 1 &&
			workspaces[0] == c.Facts.Key.WorkspaceID) {
			t.Errorf("%d %s: evicted %v and %v", v.status, v.message, evicted, workspaces)
		}
		if v.evicts {
			if r := h.cache.KeyCache.Lookup(c.Facts.Key.LookupHash, c.at()); r.Key != gwstate.Cold {
				t.Errorf("%d: the key is still held after the eviction: %+v", v.status, r)
			}
		}
		// The logs carry no id, key or message.
		for _, secret := range []string{c.Facts.Key.LookupHash, c.Facts.Key.Hash, c.IDs.AuthorizationID, v.message} {
			if strings.Contains(h.logs.all(), secret) {
				t.Errorf("the logs carry %q", secret)
			}
		}
	}
}

// TestAColdKeyIsEvictedOnPythonsRefusal (G7's note): a lookup the cache
// had nothing for (Cold, so Go passed with cache.key) is evicted all the
// same when Python answers 401: the read the lookup started may have read
// before the change Python saw.
func TestAColdKeyIsEvictedOnPythonsRefusal(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) {
		cold, err := gwstate.NewCache(gwstate.CacheConfig{Reader: &fakeWorld{c: c, slow: time.Second}})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(cold.Close)
		cfg.Cache = &recordingCache{KeyCache: cold}
	})
	cache := h.edge.full.cfg.Cache.(*recordingCache)
	h.answer(answerWith(401, errorBody(401, "invalid_api_key", "Invalid API key")))
	h.send(t, c)
	got := h.edge.DecideShadowCounts()
	if got.Classes[ClassStale+".key"] != 1 {
		t.Fatalf("classes %v", got.Classes)
	}
	cache.mu.Lock()
	defer cache.mu.Unlock()
	if len(cache.evicted) != 1 || cache.evicted[0] != c.Facts.Key.LookupHash || len(cache.workspaces) != 0 {
		t.Fatalf("evicted %v, workspaces %v", cache.evicted, cache.workspaces)
	}
}

// mutated is a golden 200 with its data changed by change, re-encoded as
// the 200 is.
func mutated(t *testing.T, raw string, change func(data pyjson.Object) pyjson.Object) []byte {
	t.Helper()
	tree, err := pyjson.Decode([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	top := tree.(pyjson.Object)
	top[0].Value = change(top[0].Value.(pyjson.Object))
	out, err := pyjson.DumpsPydantic(top)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func setMember(o pyjson.Object, key string, v any) pyjson.Object {
	for i := range o {
		if o[i].Key == key {
			o[i].Value = v
		}
	}
	return o
}

// TestEachDifferenceIsCountedByItsField: positive controls for §11's
// differ fields, each a golden 200 with one thing changed; the unchanged
// golden agrees, and Go's own ids, unsubstituted, would not.
func TestEachDifferenceIsCountedByItsField(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	in.Mint = Mint{} // Go's own ids, not Python's
	d := Decide(in)
	if d.ForShadow() == nil {
		t.Fatal(d.Pass)
	}
	golden := c.Expect.Response
	if got := compareFull(d, pyAnswer{status: 200, body: []byte(golden)}); got.class != ClassAgreeFull {
		t.Fatalf("the golden: %+v", got)
	}
	composed, _ := d.ForShadow().Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if string(composed.Render(c.Expect.timing(), false)) == golden {
		t.Fatal("Go's own ids render Python's bytes: the substitution is not tested")
	}
	// The float twin's bytes, the microdollars the same (the encoder would
	// write the float as pydantic-core does, so the bytes are changed).
	twin := strings.Replace(golden, `"estimated_cost":0.00264,`, `"estimated_cost":0.0026400,`, 1)
	if got := compareFull(d, pyAnswer{status: 200, body: []byte(twin)}); twin == golden || got.class != ClassDifferAny ||
		strings.Join(got.fields, ",") != "differ.estimate_float" {
		t.Errorf("the float twin: %+v", got)
	}
	for _, v := range []struct {
		name   string
		change func(pyjson.Object) pyjson.Object
		want   string
	}{
		{"microdollars", func(o pyjson.Object) pyjson.Object {
			return setMember(o, "estimated_cost_microdollars", json.Number("2641"))
		}, "differ.estimate"},
		{"stage d", func(o pyjson.Object) pyjson.Object {
			return setMember(o, "stage_d", pyjson.Object{{Key: "eligible", Value: false},
				{Key: "reason", Value: "heartbeat_undeclared"}})
		}, "differ.stage_d"},
		{"generation id rule", func(o pyjson.Object) pyjson.Object {
			return setMember(o, "generation_id", "gen-00000000000050008000000000000000")
		}, "differ.generation_id"},
		{"region", func(o pyjson.Object) pyjson.Object { return setMember(o, "region", "us-east4") }, "differ.region"},
		{"candidates order", func(o pyjson.Object) pyjson.Object {
			l := memberOf(o, "route_candidates").([]any)
			l[0], l[1] = l[1], l[0]
			return o
		}, "differ.candidates"},
		{"a candidate's field", func(o pyjson.Object) pyjson.Object {
			l := memberOf(o, "route_candidates").([]any)
			l[1] = setMember(l[1].(pyjson.Object), "provider_name", "Someone")
			return o
		}, "differ.route_candidates.provider_name"},
		{"a member more", func(o pyjson.Object) pyjson.Object {
			return append(o[:len(o)-1:len(o)-1], pyjson.Member{Key: "extra", Value: true}, o[len(o)-1])
		}, "differ.extra"},
		{"timing's keys", func(o pyjson.Object) pyjson.Object {
			tm := memberOf(o, "timing").(pyjson.Object)
			tm[0], tm[1] = tm[1], tm[0]
			return o
		}, "differ.timing"},
		{"members' order", func(o pyjson.Object) pyjson.Object {
			for i := range o {
				if o[i].Key == "region" {
					o[i], o[i+1] = o[i+1], o[i]
					break
				}
			}
			return o
		}, "differ.key_order"},
	} {
		got := compareFull(d, pyAnswer{status: 200, body: mutated(t, golden, v.change)})
		if got.class != ClassDifferAny || len(got.fields) != 1 || got.fields[0] != v.want {
			t.Errorf("%s: %s %v, want %s", v.name, got.class, got.fields, v.want)
		}
	}
	// Timing values are Python's own: other values still agree.
	other := mutated(t, golden, func(o pyjson.Object) pyjson.Object {
		tm := memberOf(o, "timing").(pyjson.Object)
		tm[0].Value = json.Number("41")
		return o
	})
	if got := compareFull(d, pyAnswer{status: 200, body: other}); got.class != ClassAgreeFull {
		t.Errorf("other timing values: %+v", got)
	}
}

// TestEveryRowColumnIsCompared (P3's note): every typed column, because
// Python reads a typed column before its payload copy and a NULL hides a
// payload that differs; the reservation's expires_at at least Go's; a row
// Python's settle reached first, or one not leased, is not compared; a
// minted idempotency key is Python's, a presented one is not substituted.
func TestEveryRowColumnIsCompared(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	in.Mint = Mint{}
	d := Decide(in)
	if d.ForShadow() == nil || d.ForShadow().Identity.Presented {
		t.Fatalf("not a minted-key decision: %q", d.Pass)
	}
	if outcome, fields := compareRows(d.ForShadow(), storedRows(t, c), nil, time.Second); outcome != "agree" || len(fields) != 0 {
		t.Fatalf("the golden rows: %s %v", outcome, fields)
	}
	set := func(s string) spanner.NullString { return spanner.NullString{StringVal: s, Valid: true} }
	for _, v := range []struct {
		name    string
		change  func(*store.AuthorizationForCompare)
		outcome string
		want    []string
	}{
		{"a typed nonce Go leaves NULL", func(r *store.AuthorizationForCompare) {
			r.Authorization.InvocationNonce = set("n")
		}, "differ", []string{"row.invocation_nonce"}},
		{"the typed fingerprint", func(r *store.AuthorizationForCompare) {
			r.Authorization.IdempotencyFingerprint = set(strings.Repeat("0", 64))
		}, "differ", []string{"row.idempotency_fingerprint"}},
		{"a typed column Go leaves NULL", func(r *store.AuthorizationForCompare) {
			r.Authorization.PricingSnapshot = set("{}")
		}, "differ", []string{"row.pricing_snapshot"}},
		{"the typed boot kid NULL", func(r *store.AuthorizationForCompare) {
			r.Authorization.StageDBootKid = spanner.NullString{}
		}, "differ", []string{"row.stage_d_boot_kid"}},
		{"a payload member", func(r *store.AuthorizationForCompare) {
			r.Authorization.Payload = set(strings.Replace(r.Authorization.Payload.StringVal, `"stage_d_reason":"not_streaming"`,
				`"stage_d_reason":"stage_d_disabled"`, 1))
		}, "differ", []string{"row.payload.stage_d_reason"}},
		{"the reservation's created_at NULL", func(r *store.AuthorizationForCompare) {
			r.Reservation.CreatedAt = spanner.NullTime{}
		}, "differ", []string{"row.reservation.created_at"}},
		{"the reservation committed at another instant", func(r *store.AuthorizationForCompare) {
			r.Reservation.CreatedAt.Time = r.Reservation.CreatedAt.Time.Add(-time.Hour)
		}, "agree", nil},
		{"an earlier expiry", func(r *store.AuthorizationForCompare) {
			r.Reservation.ExpiresAt.Time = r.Reservation.ExpiresAt.Time.Add(-time.Microsecond)
		}, "differ", []string{"row.reservation.expires_at"}},
		{"a later expiry", func(r *store.AuthorizationForCompare) {
			r.Reservation.ExpiresAt.Time = r.Reservation.ExpiresAt.Time.Add(3 * time.Millisecond)
		}, "agree", nil},
		{"a reserved amount", func(r *store.AuthorizationForCompare) {
			r.Reservation.CreditReservedMicro = spanner.NullInt64{Int64: 5, Valid: true}
		}, "differ", []string{"row.reservation.credit_reserved_micro"}},
		{"no reservation", func(r *store.AuthorizationForCompare) { r.Reservation = nil }, "differ",
			[]string{"row.reservation.missing"}},
		{"settled first", func(r *store.AuthorizationForCompare) { r.Authorization.Settled = true }, "moved_on", nil},
		{"settled first, its payload rewritten", func(r *store.AuthorizationForCompare) {
			r.Authorization.Settled = true
			r.Authorization.Payload = set(`{"settled":true}`)
			r.Reservation.CreditReservedMicro = spanner.NullInt64{Int64: 9, Valid: true}
		}, "moved_on", nil},
		{"settled first, a column settle leaves alone", func(r *store.AuthorizationForCompare) {
			r.Authorization.Settled = true
			r.Authorization.IdempotencyFingerprint = set(strings.Repeat("0", 64))
		}, "moved_on_differ", []string{"row.idempotency_fingerprint"}},
		{"created past the answer", func(r *store.AuthorizationForCompare) {
			r.Authorization.CreatedAt = r.Authorization.CreatedAt.Add(2*time.Second + time.Millisecond)
			r.Reservation.ExpiresAt.Time = r.Reservation.ExpiresAt.Time.Add(2*time.Second + time.Millisecond)
			r.Authorization.Payload = set(strings.Replace(r.Authorization.Payload.StringVal, `"created_at":"2026-10-19T02:00:00Z"`,
				`"created_at":"2026-10-19T02:00:02.001000Z"`, 1))
		}, "differ", []string{"row.created_at_skew"}},
		{"created before the arrival", func(r *store.AuthorizationForCompare) {
			r.Authorization.CreatedAt = r.Authorization.CreatedAt.Add(-time.Second - time.Microsecond)
			r.Reservation.ExpiresAt.Time = r.Reservation.ExpiresAt.Time.Add(-time.Second - time.Microsecond)
			r.Authorization.Payload = set(strings.Replace(r.Authorization.Payload.StringVal, `"created_at":"2026-10-19T02:00:00Z"`,
				`"created_at":"2026-10-19T01:59:58.999999Z"`, 1))
		}, "differ", []string{"row.created_at_skew"}},
		{"an expiry past the latency and the skew", func(r *store.AuthorizationForCompare) {
			r.Reservation.ExpiresAt.Time = r.Reservation.ExpiresAt.Time.Add(2*time.Second + time.Microsecond)
		}, "differ", []string{"row.reservation.expires_at"}},
		{"a minted key not of the minted form", func(r *store.AuthorizationForCompare) {
			r.Authorization.Payload = set(strings.Replace(r.Authorization.Payload.StringVal,
				`"idempotency_key":"57cb4348-7025-4788-b765-56c01f2f9aab"`, `"idempotency_key":"not-a-uuid"`, 1))
			r.Reservation.IdempotencyScope = set(gwauth.ScopeOf(c.Facts.Key.WorkspaceID, c.Facts.Key.Hash, "not-a-uuid"))
		}, "differ", []string{"row.idempotency_key_form"}},
		{"heartbeat first", func(r *store.AuthorizationForCompare) {
			r.Authorization.HeartbeatSeq = spanner.NullInt64{Int64: 1, Valid: true}
		}, "moved_on", nil},
		{"not leased", func(r *store.AuthorizationForCompare) {
			r.Authorization.Payload = set(strings.Replace(r.Authorization.Payload.StringVal, `"fastpath_envelope"`, `"x"`, 1))
		}, "python_not_leased", nil},
	} {
		rows := storedRows(t, c)
		if rows.Reservation != nil {
			copied := *rows.Reservation
			rows.Reservation = &copied
		}
		v.change(&rows)
		outcome, fields := compareRows(d.ForShadow(), rows, nil, time.Second)
		if outcome != v.outcome || strings.Join(fields, ",") != strings.Join(v.want, ",") {
			t.Errorf("%s: %s %v, want %s %v", v.name, outcome, fields, v.outcome, v.want)
		}
	}
	// A presented key is the request's: Python's own would be a difference.
	presented := authorizeCaseNamed(t, "answer:idempotency:body")
	pin, _ := caseInputs(t, f, presented)
	pin.Mint = Mint{}
	pd := Decide(pin)
	if pd.ForShadow() == nil || !pd.ForShadow().Identity.Presented {
		t.Fatalf("not a presented-key decision: %q", pd.Pass)
	}
	rows := storedRows(t, presented)
	if outcome, fields := compareRows(pd.ForShadow(), rows, nil, time.Second); outcome != "agree" {
		t.Fatalf("the presented key's golden rows: %s %v", outcome, fields)
	}
	rows.Authorization.Payload = spanner.NullString{Valid: true, StringVal: strings.Replace(rows.Authorization.Payload.StringVal,
		`"idempotency_key":"`, `"idempotency_key":"x`, 1)}
	if outcome, fields := compareRows(pd.ForShadow(), rows, nil, time.Second); outcome != "differ" ||
		strings.Join(fields, ",") != "row.payload.idempotency_key" {
		t.Fatalf("another presented key: %s %v", outcome, fields)
	}
}

// TestRowsAreSampledAndRateLimited: one comparable request in SampleEvery,
// and no more than ReadsPerSecond a second.
func TestRowsAreSampledAndRateLimited(t *testing.T) {
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{}, Rows: &fakeRows{},
		SampleEvery: 20}, func(string, ...any) {}, time.Now, 0)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for range 100 {
		if sh.sample() {
			n++
		}
	}
	if n != 5 {
		t.Fatalf("%d of 100 sampled at one in 20", n)
	}
	sh.cfg.SampleEvery, sh.limiter.tokens, sh.limiter.at = 1, sh.cfg.ReadsPerSecond, time.Time{}
	n = 0
	for range 50 {
		if sh.sample() {
			n++
		}
	}
	if n < 5 || n > 6 {
		t.Fatalf("%d of 50 at once allowed at 5 a second", n)
	}
	if DefaultRowSampleEvery != 1 || DefaultRowReadsPerSecond != 5 {
		t.Fatal("§11's sampling is every comparable request, at most 5 a second")
	}
}

// TestASlowShadowNeverDelaysTheAnswer: with every read of the cache
// taking seconds and every job busy, Python's answer reaches the enclave
// at once and byte for byte; the request not followed is counted dropped.
func TestASlowShadowNeverDelaysTheAnswer(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) { cfg.Jobs = 1 })
	h.cache.slow = 2 * time.Second
	h.rows.slow = time.Second
	h.rows.by[c.IDs.AuthorizationID] = storedRows(t, c)
	h.answer(answerWith(200, []byte(c.Expect.Response)))
	for i := range 2 {
		req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body()))
		req.Header = c.header()
		rec := httptest.NewRecorder()
		began := time.Now()
		h.edge.ServeHTTP(rec, req)
		if took := time.Since(began); took > 500*time.Millisecond {
			t.Fatalf("request %d took %v behind a slow shadow", i, took)
		}
		if rec.Code != 200 || rec.Body.String() != c.Expect.Response {
			t.Fatalf("request %d: %d %s", i, rec.Code, rec.Body.String())
		}
	}
	if got := h.edge.DecideShadowCounts().Dropped; got != 1 {
		t.Fatalf("dropped %d, want the second request", got)
	}
	h.edge.WaitDecideShadow()
	if got := h.edge.DecideShadowCounts(); got.Classes[ClassAgreeFull] != 1 || got.Rows["agree"] != 1 {
		t.Fatalf("the followed request: %+v", got)
	}
}

// TestTheDecideShadowIsDark: off (no DecideShadow, the default), the edge
// holds no shadow, follows nothing and starts nothing per request, and the
// enclave gets the same bytes as with it on. The positive control is the
// same edge with the shadow on, whose followed requests each hold a
// goroutine while its cache is slow.
func TestTheDecideShadowIsDark(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	py := newFakePython(t)
	on := newFullEdge(t, f, c, py, nil)
	on.answer(answerWith(200, []byte(c.Expect.Response), "X-Python", "1"))
	withShadow := on.send(t, c)
	if on.edge.DecideShadowCounts().Classes[ClassAgreeFull] != 1 {
		t.Fatalf("on: %+v", on.edge.DecideShadowCounts())
	}
	upstream, _ := url.Parse(py.server.URL)
	off, err := New(Config{Token: f.InternalToken, Upstream: upstream, Boots: bootsOf(t, f, c),
		Clock: func() time.Time { return c.at() }, Transport: py.transport})
	if err != nil {
		t.Fatal(err)
	}
	if off.DecideShadowOn() || off.full != nil {
		t.Fatal("the default holds a decide shadow")
	}
	ran := make(chan struct{})
	go func() {
		off.Run(context.Background()) // nothing configured: returns at once
		close(ran)
	}()
	select {
	case <-ran:
	case <-time.After(time.Second):
		t.Fatal("Run started something with nothing configured")
	}
	send := func(e *Edge) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body()))
		req.Header = c.header()
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		return rec
	}
	without := send(off)
	if without.Code != withShadow.Code || without.Body.String() != withShadow.Body.String() ||
		without.Header().Get("X-Python") != withShadow.Header().Get("X-Python") ||
		without.Header().Get("Content-Length") != withShadow.Header().Get("Content-Length") {
		t.Fatalf("off %d %q, on %d %q", without.Code, without.Body.String(), withShadow.Code, withShadow.Body.String())
	}
	if got := off.DecideShadowCounts(); got.Classes != nil || got.Dropped != 0 {
		t.Fatalf("counts with the shadow off: %+v", got)
	}
	const requests = 20
	baseline := runtime.NumGoroutine()
	for range requests {
		send(off)
	}
	time.Sleep(20 * time.Millisecond)
	if n := runtime.NumGoroutine(); n > baseline+2 {
		t.Fatalf("%d goroutines after %d requests with the shadow off, %d before", n, requests, baseline)
	}
	// Positive control: on, with a slow cache, each followed request holds
	// its goroutine until the cache answers.
	on.cache.slow = 500 * time.Millisecond
	baseline = runtime.NumGoroutine()
	for range requests {
		send(on.edge)
	}
	if n := runtime.NumGoroutine(); n < baseline+requests {
		t.Fatalf("%d goroutines while %d requests are followed, %d before: the count does not see them", n, requests,
			baseline)
	}
	on.edge.WaitDecideShadow()
	// The shadow needs its snapshot, its policy and its parts.
	for name, cfg := range map[string]Config{
		"no snapshot or policy": {Token: "t", Upstream: upstream, Boots: &boots{},
			DecideShadow: &DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{}, Rows: &fakeRows{}}},
	} {
		if _, err := New(cfg); err == nil {
			t.Errorf("%s: a decide shadow was made", name)
		}
	}
}

// TestTheDecideShadowLogsCountsAlone: the period's INFO line per snapshot
// and policy, with the class counts, and no request value.
func TestTheDecideShadowLogsCountsAlone(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, nil)
	h.answer(answerWith(200, []byte(c.Expect.Response)))
	h.rows.by[c.IDs.AuthorizationID] = storedRows(t, c)
	h.send(t, c)
	h.edge.full.flush()
	logs := h.logs.all()
	if !strings.Contains(logs, "fastpath edge decide shadow: dropped=0 evictions=0 cache_unrefreshed=0 cache_dropped=0 "+
		"cache_read_failures=0") {
		t.Fatalf("the counts line: %s", logs)
	}
	if !strings.Contains(logs, "fastpath edge decide shadow: snapshot=golden-snapshot policy=golden-policy agree_full=1 "+
		"differ=0 would_admit_python_refused=0 read_back=0") || !strings.Contains(logs, `decide={"on":1}`) ||
		!strings.Contains(logs, `rows={"agree":1}`) || !strings.Contains(logs, `cohort={"agree_full":1}`) {
		t.Fatalf("the summary: %s", logs)
	}
	for _, secret := range []string{c.Facts.Key.LookupHash, c.Facts.Key.Hash, c.IDs.AuthorizationID, "z-ai/glm-5.3"} {
		if strings.Contains(logs, secret) {
			t.Errorf("the logs carry %q", secret)
		}
	}
}

// TestALateAnswerGivesItsBytesBack: the shadow's wait ends before
// Python's body does; the body still passes to the enclave whole, the
// request is counted unread, and every byte the tap reserved, before the
// wait ended or after, comes back to the budget.
func TestALateAnswerGivesItsBytesBack(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) { cfg.Wait = 50 * time.Millisecond })
	body := []byte(c.Expect.Response)
	half := len(body) / 2
	h.answer(func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		_, _ = w.Write(body[:half])
		w.(http.Flusher).Flush()
		time.Sleep(300 * time.Millisecond)
		_, _ = w.Write(body[half:])
	})
	rec := h.send(t, c)
	if rec.Body.String() != c.Expect.Response {
		t.Fatalf("the enclave got %q", rec.Body.String())
	}
	if got := h.edge.DecideShadowCounts(); got.Classes[ClassUnread+".wait"] != 1 || got.Decide["on"] != 1 {
		t.Fatalf("counts %+v", got)
	}
	if n := h.edge.full.bytes.Load(); n != 0 {
		t.Fatalf("%d bytes held after a late answer", n)
	}
	// Positive control: while the job waits, the tap does hold bytes.
	h2 := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) { cfg.Wait = time.Second })
	release := make(chan struct{})
	h2.answer(func(w http.ResponseWriter) {
		w.WriteHeader(200)
		_, _ = w.Write(body[:half])
		w.(http.Flusher).Flush()
		<-release
		_, _ = w.Write(body[half:])
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body()))
		req.Header = c.header()
		h2.edge.ServeHTTP(httptest.NewRecorder(), req)
	}()
	waitFor(t, func() bool { return h2.edge.full.bytes.Load() > int64(len(c.body())) })
	close(release)
	<-done
	h2.edge.WaitDecideShadow()
}

// TestAnUnreadRefusalIsStillARefusal: Python's status classifies whether
// or not its body was read: a 403 past the bound is
// would_admit_python_refused and evicts, a decided request with no answer
// counts its decide gate, and a 5xx unread is Python's error.
func TestAnUnreadRefusalIsStillARefusal(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	for _, v := range []struct {
		a    pyAnswer
		want string
	}{
		{pyAnswer{status: 403, why: "bound"}, ClassWouldAdmit + ".403.unread"},
		{pyAnswer{status: 401, why: "budget"}, ClassWouldAdmit + ".401.unread"},
		{pyAnswer{status: 503, why: "bound"}, ClassPythonError + ".503.unread"},
		{pyAnswer{status: 200, why: "bound"}, ClassUnread + ".bound"},
		{pyAnswer{why: "not_reached"}, ClassUnread + ".not_reached"},
	} {
		got := compareFull(d, v.a)
		if got.class != v.want || !got.decided || got.gate != "" {
			t.Errorf("%+v: %+v, want %s, decided", v.a, got, v.want)
		}
	}
	h := newFullEdge(t, f, c, nil, nil)
	h.edge.full.evict(d, pyAnswer{status: 403, why: "bound"}, c.body())
	h.cache.mu.Lock()
	defer h.cache.mu.Unlock()
	if len(h.cache.evicted) != 1 || len(h.cache.workspaces) != 1 {
		t.Fatalf("an unread 403 evicted %v and %v", h.cache.evicted, h.cache.workspaces)
	}
}

// TestARefusalEvictsTheKeyOfABodyThatDidNotParse: Decide named no key (the
// body changed past its boot signature, which passes first, and holds a
// float the strict parse would pass too); Python's 401 evicts the lookup
// hash the body names all the same.
func TestARefusalEvictsTheKeyOfABodyThatDidNotParse(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	c.Request.Body = strings.Replace(c.Request.Body, `"estimated_input_tokens":1000`, `"estimated_input_tokens":1000.0`, 1)
	h := newFullEdge(t, f, c, nil, nil)
	h.answer(answerWith(401, errorBody(401, "invalid_api_key", "Invalid API key")))
	h.send(t, c)
	if got := h.edge.DecideShadowCounts(); got.Classes[ClassGoPassed+"."+PassBootSignature] != 1 {
		t.Fatalf("counts %+v", got)
	}
	h.cache.mu.Lock()
	defer h.cache.mu.Unlock()
	if len(h.cache.evicted) != 1 || h.cache.evicted[0] != c.Facts.Key.LookupHash {
		t.Fatalf("evicted %v", h.cache.evicted)
	}
	if got := lenientLookupHash([]byte(`{"api_key_lookup_hash":7}`)); got != "" {
		t.Fatalf("a number named a key: %q", got)
	}
}

// TestACompressedAnswerIsDecodedOffTheProxy: Python's gzip is decoded in
// the job, its decoded bytes held of the budget while compared, and the
// comparison agrees; the enclave gets the compressed bytes.
func TestACompressedAnswerIsDecodedOffTheProxy(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, nil)
	var zipped bytes.Buffer
	zw := gzip.NewWriter(&zipped)
	_, _ = zw.Write([]byte(c.Expect.Response))
	_ = zw.Close()
	h.answer(answerWith(200, zipped.Bytes(), "Content-Encoding", "gzip"))
	h.rows.by[c.IDs.AuthorizationID] = storedRows(t, c)
	rec := h.send(t, c)
	if !bytes.Equal(rec.Body.Bytes(), zipped.Bytes()) {
		t.Fatal("the enclave did not get Python's compressed bytes")
	}
	if got := h.edge.DecideShadowCounts(); got.Classes[ClassAgreeFull] != 1 || got.Rows["agree"] != 1 {
		t.Fatalf("counts %+v", got)
	}
	h.answer(answerWith(200, []byte("not gzip"), "Content-Encoding", "gzip"))
	h.send(t, c)
	if got := h.edge.DecideShadowCounts(); got.Classes[ClassUnread+".encoding"] != 1 {
		t.Fatalf("a body that is not gzip: %+v", got)
	}
}

// TestTheCriticalClassIsAnAlert: with the service's alert path given, the
// would_admit_python_refused comparison raises an alert of its own kind.
func TestTheCriticalClassIsAnAlert(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	var mu sync.Mutex
	var alerts []string
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) {
		cfg.Alert = func(kind, subject, what string) {
			mu.Lock()
			defer mu.Unlock()
			alerts = append(alerts, kind+"|"+subject+"|"+what)
		}
	})
	h.answer(answerWith(403, errorBody(403, "forbidden", "billing_paused")))
	h.send(t, c)
	mu.Lock()
	defer mu.Unlock()
	if len(alerts) != 1 || alerts[0] != AlertWouldAdmit+"|snapshot=golden-snapshot policy=golden-policy|"+
		"the edge would have answered an authorize Python refused: "+ClassWouldAdmit+".403.forbidden" {
		t.Fatalf("alerts %q", alerts)
	}
}

// TestRunEndsWhileRequestsAreServed: Run's context ends while handlers
// still begin following requests (a server shutting down serves its
// in-flight requests). Run must not wait on what the handlers add to, which
// is WaitDecideShadow's, called once the handlers have returned: the
// service's order. Under -race, rounds of eight serving goroutines.
func TestRunEndsWhileRequestsAreServed(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	py := newFakePython(t)
	for range 100 {
		h := newFullEdge(t, f, c, py, nil)
		h.answer(answerWith(200, []byte(c.Expect.Response)))
		ctx, cancel := context.WithCancel(context.Background())
		ran := make(chan struct{})
		go func() {
			defer close(ran)
			h.edge.Run(ctx)
		}()
		var serving sync.WaitGroup
		for range 8 {
			serving.Add(1)
			go func() {
				defer serving.Done()
				for range 5 {
					req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body()))
					req.Header = c.header()
					h.edge.ServeHTTP(httptest.NewRecorder(), req)
				}
			}()
		}
		time.Sleep(2 * time.Millisecond)
		cancel()
		<-ran
		serving.Wait()
		h.edge.WaitDecideShadow()
		got := h.edge.DecideShadowCounts()
		if n := got.Classes[ClassAgreeFull] + int(got.Dropped); n != 40 {
			t.Fatalf("%d of 40 requests compared or dropped: %+v", n, got)
		}
	}
}

// TestAGzipIsDecodedWithinTheBudget: what may be decoded is reserved
// before a byte is, so a budget nearly spent refuses the decode rather than
// allocate past it, and gives every byte back.
func TestAGzipIsDecodedWithinTheBudget(t *testing.T) {
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{}, Rows: &fakeRows{}},
		func(string, ...any) {}, time.Now, 0)
	if err != nil {
		t.Fatal(err)
	}
	var zipped bytes.Buffer
	zw := gzip.NewWriter(&zipped)
	_, _ = zw.Write(bytes.Repeat([]byte("x"), 1000))
	_ = zw.Close()
	sh.budget = 100
	if out, held, why := sh.decode("gzip", zipped.Bytes()); why != "budget" || out != nil || held != 0 ||
		sh.bytes.Load() != 0 {
		t.Fatalf("past the budget: %d bytes, held %d, %q, budget holds %d", len(out), held, why, sh.bytes.Load())
	}
	sh.budget = 2000
	out, held, why := sh.decode("gzip", zipped.Bytes())
	if why != "" || len(out) != 1000 || held != 1000 || sh.bytes.Load() != 1000 {
		t.Fatalf("within it: %d bytes, held %d, %q, budget holds %d", len(out), held, why, sh.bytes.Load())
	}
	sh.release(held)
	if _, _, why := sh.decode("br", zipped.Bytes()); why != "encoding" || sh.bytes.Load() != 0 {
		t.Fatalf("another encoding: %q", why)
	}
}

// closingReader is a store whose key reads block until their context ends,
// and that counts the reads begun after the cache was closed.
type closingReader struct {
	fakeWorld
	closed           atomic.Bool
	began, afterward atomic.Int64
}

func (r *closingReader) ReadKeyContext(ctx context.Context, lookupHash string) (store.KeyContext, bool, error) {
	r.began.Add(1)
	if r.closed.Load() {
		r.afterward.Add(1)
	}
	<-ctx.Done()
	return store.KeyContext{}, false, ctx.Err()
}

// closedLookups counts the lookups made after its reader's cache closed.
type closedLookups struct {
	KeyCache
	reader    *closingReader
	afterward atomic.Int64
}

func (c *closedLookups) Lookup(lookupHash string, now time.Time) gwstate.Read {
	if c.reader.closed.Load() {
		c.afterward.Add(1)
	}
	return c.KeyCache.Lookup(lookupHash, now)
}

// TestTheCacheClosesAfterTheLastLookup: a request whose key the cache does
// not hold (its Lookup starts a read, which blocks) is with a slow Python
// when the edge stops and the enclave hangs up; the service's order,
// WaitDecideShadow and then Close, makes no Lookup after Close (gwstate's
// contract), so no read begins after it, and Close ends the one blocked.
func TestTheCacheClosesAfterTheLastLookup(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	reader := &closingReader{fakeWorld: fakeWorld{c: c}}
	cache, err := gwstate.NewCache(gwstate.CacheConfig{Reader: reader, Clock: func() time.Time { return c.at() },
		ReadTimeout: time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	lookups := &closedLookups{KeyCache: cache, reader: reader}
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) { cfg.Cache = lookups })
	// Only the authorize answers slowly: Run's snapshot and policy polls
	// reach the same fake Python.
	answering := make(chan struct{})
	var once sync.Once
	h.python.server.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		if r.Method != http.MethodPost || r.URL.Path != c.Request.Path {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		once.Do(func() { close(answering) })
		time.Sleep(time.Second)
		w.WriteHeader(200)
	})
	ctx, cancel := context.WithCancel(context.Background())
	run := make(chan struct{})
	go func() {
		defer close(run)
		h.edge.Run(ctx)
	}()
	served := make(chan struct{})
	reqCtx, hangUp := context.WithCancel(context.Background())
	go func() {
		defer close(served)
		req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body())).WithContext(reqCtx)
		req.Header = c.header()
		h.edge.ServeHTTP(httptest.NewRecorder(), req)
	}()
	<-answering
	// The cold key's read is under way (blocked in the store) at the stop.
	waitFor(t, func() bool { return reader.began.Load() == 1 })
	cancel()
	hangUp()
	<-run
	<-served
	// The service's order: the handlers have returned, then the shadow's
	// requests are compared, then the cache closes.
	h.edge.WaitDecideShadow()
	reader.closed.Store(true)
	cache.Close()
	if n := lookups.afterward.Load(); n != 0 {
		t.Fatalf("%d lookups after Close", n)
	}
	if n := reader.afterward.Load(); n != 0 || reader.began.Load() != 1 {
		t.Fatalf("%d reads began after Close, %d in all", n, reader.began.Load())
	}
	if got := h.edge.DecideShadowCounts(); got.Classes[ClassStale+".key"] != 1 {
		t.Fatalf("counts %+v", got)
	}
}

// TestAWindowRefusalOfAnUncheckedDecisionIsNotAWouldAdmit (docs/design/
// fast-admission-e2-key-windows.md section 8.2): Go's decision for a key
// whose windows Python enforces carries no window verdict yet, so Python's
// 429 key_window_limit_exceeded to it (or a 429 whose body was not read) is
// window_unchecked, never the critical would-admit; any other refusal is a
// would-admit as before, and so is a window refusal of an alert-only key,
// whose windows Python never checks. Python's 200 compares as the cohort's.
func TestAWindowRefusalOfAnUncheckedDecisionIsNotAWouldAdmit(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	enforcing := Decide(windowedInputs(t, map[string]any{"limit_daily_microdollars": 5000}, ""))
	alertOnly := Decide(windowedInputs(t, map[string]any{"limit_daily_microdollars": 5000, "budget_alert_only": true}, ""))
	if enforcing.ForShadow() == nil || enforcing.ForShadow().Windows == nil || alertOnly.ForShadow() == nil ||
		alertOnly.ForShadow().Windows != nil {
		t.Fatalf("the decisions: %q %q", enforcing.Pass, alertOnly.Pass)
	}
	window := errorBody(429, windowRefusal, "API key daily spend limit exceeded")
	for _, v := range []struct {
		name string
		d    Decided
		a    pyAnswer
		want string
	}{
		{"window refusal", enforcing, pyAnswer{status: 429, body: window},
			ClassWindowUnchecked + ".429." + windowRefusal},
		{"429 unread", enforcing, pyAnswer{status: 429, why: "bound"}, ClassWindowUnchecked + ".429.unread"},
		{"another 429", enforcing, pyAnswer{status: 429, body: errorBody(429, "rate_limited", "slow down")},
			ClassWouldAdmit + ".429.rate_limited"},
		{"402", enforcing, pyAnswer{status: 402, body: errorBody(402, "insufficient_credits", "no")},
			ClassWouldAdmit + ".402.insufficient_credits"},
		{"alert only, window refusal", alertOnly, pyAnswer{status: 429, body: window},
			ClassWouldAdmit + ".429." + windowRefusal},
		{"alert only, 429 unread", alertOnly, pyAnswer{status: 429, why: "bound"}, ClassWouldAdmit + ".429.unread"},
	} {
		r := compareFull(v.d, v.a)
		if r.class != v.want || !r.decided || r.gate != v.d.DecideGate {
			t.Errorf("%s: %+v, want class %s", v.name, r, v.want)
		}
	}
	in := windowedInputs(t, map[string]any{"limit_daily_microdollars": 5000}, "")
	cc := c
	body := keyBodyWith(t, cc, map[string]any{"limit_daily_microdollars": 5000})
	cc.Key.KeyBody = &body
	if r := compareFull(Decide(in), pyAnswer{status: 200, body: pythonsLegacyAnswer(t, f, cc, false)}); r.class !=
		ClassAgreeFull || r.gate != PassKeyWindow {
		t.Errorf("Python's 200: %+v", r)
	}
}

// windowCase is an enforcing windowed key's decision (a daily limit of a
// million microdollars, the policy's mode absent) and its window check over
// rows holding used of the day.
func windowCase(t *testing.T, used int64) (Decided, *windowRead, authorizeCase) {
	t.Helper()
	fields := map[string]any{"limit_daily_microdollars": 1_000_000}
	d := Decide(windowedInputs(t, fields, ""))
	dec := d.ForShadow()
	if dec == nil || dec.Windows == nil {
		t.Fatalf("no window check: %q", d.Pass)
	}
	at := d.ForShadow().Input.At
	rows := dailyRows(at, 1_000_000, used, 1)
	v := keywindow.Evaluate(dec.Windows.Limits, 1, rows, dec.Estimate, at)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	body := keyBodyWith(t, c, fields)
	c.Key.KeyBody = &body
	return d, &windowRead{verdict: v, made: true, took: 3 * time.Millisecond, rows: rows, at: at}, c
}

// rateLimited is Python's answer with its RateLimit-* headers.
func rateLimited(a pyAnswer, limit, remaining, reset int64) pyAnswer {
	a.rateLimit = [3]string{strconv.FormatInt(limit, 10), strconv.FormatInt(remaining, 10),
		strconv.FormatInt(reset, 10)}
	return a
}

// TestTheShadowComparesTheWindowCheck (docs/design/
// fast-admission-e2-key-windows.md section 8.2): with the shadow's own
// window check, Go's admit compares Python's 200 as the cohort's and makes
// Python's window refusal the critical would-admit; Go's pass is benign
// against Python's 200 (python_answered_go_passed by its reason) and agrees
// with Python's refusal (passed); and Python's RateLimit-* headers are
// compared with Go's limit, remaining and reset, the window of a 429 with
// its message's. Without a check the comparison is read.none, as before.
func TestTheShadowComparesTheWindowCheck(t *testing.T) {
	f := authorizeGoldens(t)
	allowed, admit, c := windowCase(t, 0)
	_, spent, _ := windowCase(t, 1_000_000)
	if !admit.verdict.Admit() || spent.verdict.Pass != keywindow.PassExceeded {
		t.Fatalf("the verdicts: %+v %+v", admit.verdict, spent.verdict)
	}
	v := admit.verdict
	py200 := pyAnswer{status: 200, body: pythonsLegacyAnswer(t, f, c, false)}
	message := "API key daily spend limit exceeded; resets at 2026-10-20T00:00:00Z"
	py429 := pyAnswer{status: 429, body: errorBody(429, windowRefusal, message)}
	has := func(r fullResult, want ...string) bool {
		for _, w := range want {
			if !slices.Contains(r.windows, w) {
				return false
			}
		}
		return true
	}
	for _, x := range []struct {
		name    string
		w       *windowRead
		a       pyAnswer
		class   string
		windows []string
	}{
		{"admit, Python's 200 agrees", admit, rateLimited(py200, v.Limit, v.Remaining, v.ResetSeconds), ClassAgreeFull,
			[]string{"read.made", "latency.le_10ms", "verdict.admit", "window.agree", "window.same_inputs.agree"}},
		{"admit, Python's 200 a second later", admit, rateLimited(py200, v.Limit, v.Remaining, v.ResetSeconds-1),
			ClassAgreeFull, []string{"window.reset_1s", "window.same_inputs.agree"}},
		{"admit, Python spent more", admit, rateLimited(py200, v.Limit, v.Remaining-5000, v.ResetSeconds),
			ClassAgreeFull, []string{"window.differ.remaining.le_1000000"}},
		{"admit, Python's other limit", admit, rateLimited(py200, v.Limit+1, v.Remaining, v.ResetSeconds),
			ClassAgreeFull, []string{"window.differ.limit"}},
		{"admit, Python's 200 without headers", admit, py200, ClassAgreeFull, []string{"window.python_none"}},
		{"admit, Python refused the window it read more spent", admit, rateLimited(py429, v.Limit, 0, v.ResetSeconds),
			ClassWindowRaceUnconfirmed + ".429." + windowRefusal, []string{"verdict.admit", "window.differ.remaining.le_1000000"}},
		{"admit, Python refused with the same inputs", admit, rateLimited(py429, v.Limit, v.Remaining, v.ResetSeconds),
			ClassWouldAdmit + ".429." + windowRefusal, []string{"window.same_inputs.disagree"}},
		{"spent, Python refused", spent, rateLimited(py429, 1_000_000, 0, v.ResetSeconds),
			ClassGoPassed + "." + keywindow.PassExceeded,
			[]string{"verdict." + keywindow.PassExceeded, "window.agree", "window.same_inputs.agree"}},
		{"spent, Python refused another window", spent,
			rateLimited(pyAnswer{status: 429, body: errorBody(429, windowRefusal,
				"API key weekly spend limit exceeded; resets at 2026-10-26T00:00:00Z")}, 1_000_000, 0, v.ResetSeconds),
			ClassGoPassed + "." + keywindow.PassExceeded, []string{"window.differ.window"}},
		{"spent, Python admitted", spent, rateLimited(py200, 1_000_000, 0, v.ResetSeconds),
			ClassPythonAnsweredGoPassed + "." + keywindow.PassExceeded, []string{"window.same_inputs.disagree"}},
		{"unread, Python admitted", &windowRead{verdict: keywindow.Verdict{Pass: keywindow.PassUnread}, made: true,
			took: time.Second}, py200, ClassPythonAnsweredGoPassed + "." + keywindow.PassUnread,
			[]string{"latency.gt_300ms", "verdict." + keywindow.PassUnread}},
		{"no check, Python refused", nil, py429, ClassWindowUnchecked + ".429." + windowRefusal, []string{"read.none"}},
		{"no check, Python admitted", nil, py200, ClassAgreeFull, []string{"read.none"}},
	} {
		r := compareWindowed(allowed, x.a, x.w)
		if r.class != x.class || !has(r, x.windows...) || !r.decided || r.gate != PassKeyWindow {
			t.Errorf("%s: %s %v, want %s %v", x.name, r.class, r.windows, x.class, x.windows)
		}
		if strings.HasPrefix(r.class, ClassPythonAnsweredGoPassed) && (r.rows || len(r.fields) != 0) {
			t.Errorf("%s: a pass compared as an answer: %+v", x.name, r)
		}
	}
	// A key without a window check: no window counts.
	in, _ := baseInputs(t)
	if r := compareWindowed(Decide(in), py200, nil); len(r.windows) != 0 {
		t.Errorf("an uncapped key's window counts: %v", r.windows)
	}
}

// TestTheShadowReadsTheWindowsOffTheRequestPath: the shadow makes the
// decide path's read for an enforcing key it follows, within its own
// ReadsPerSecond, and counts it; Python's answer reaches the enclave
// unchanged. With no reader it reads nothing (read.none).
func TestTheShadowReadsTheWindowsOffTheRequestPath(t *testing.T) {
	f := authorizeGoldens(t)
	_, admit, c := windowCase(t, 0)
	v := admit.verdict
	windows := &fakeWindows{read: func(context.Context) ([]keywindow.Row, error) {
		return dailyRows(c.at(), 1_000_000, 0, 1), nil
	}}
	py := pythonsLegacyAnswer(t, f, c, false)
	headers := []string{"RateLimit-Limit", strconv.FormatInt(v.Limit, 10), "RateLimit-Remaining",
		strconv.FormatInt(v.Remaining, 10), "RateLimit-Reset", strconv.FormatInt(v.ResetSeconds, 10)}
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) { cfg.Windows = windows })
	h.answer(answerWith(200, py, headers...))
	if rec := h.send(t, c); rec.Code != http.StatusOK || !bytes.Equal(rec.Body.Bytes(), py) {
		t.Fatalf("the enclave got %d", rec.Code)
	}
	got := h.edge.DecideShadowCounts()
	calls := windows.seen()
	if len(calls) != 1 || calls[0].keyHash != c.Facts.Key.Hash ||
		got.Classes[ClassAgreeFull] != 1 || got.Windows["read.made"] != 1 || got.Windows["verdict.admit"] != 1 ||
		got.Windows["window.agree"] != 1 || got.Decide[PassKeyWindow] != 1 {
		t.Fatalf("reads %+v, counts %+v", calls, got)
	}
	// The limiter: with no token left the shadow does not read.
	h.edge.full.windowLimiter.Lock()
	h.edge.full.cfg.ReadsPerSecond, h.edge.full.windowLimiter.tokens = 1e-9, 0
	h.edge.full.windowLimiter.Unlock()
	h.send(t, c)
	if len(windows.seen()) != 1 || h.edge.DecideShadowCounts().Windows["read.none"] != 1 {
		t.Fatalf("read past the limiter: %d reads, %v", len(windows.seen()), h.edge.DecideShadowCounts().Windows)
	}
	h = newFullEdge(t, f, c, nil, nil)
	h.answer(answerWith(200, py, headers...))
	h.send(t, c)
	if got := h.edge.DecideShadowCounts(); got.Windows["read.none"] != 1 || got.Classes[ClassAgreeFull] != 1 {
		t.Fatalf("no reader: %+v", got)
	}
}

// windowRefusal429 is Python's 429 for a window, with its RateLimit-* headers.
func windowRefusal429(window string, limit, remaining, reset int64) pyAnswer {
	return rateLimited(pyAnswer{status: 429, body: errorBody(429, windowRefusal,
		"API key "+window+" spend limit exceeded; resets at 2026-10-20T00:00:00Z")}, limit, remaining, reset)
}

// TestAWindowRaceCandidateIsUnconfirmedUntilReRead (section 8.2 item 3): a
// window refusal of a request Go read and would admit is a race candidate
// only when the 429 names a window whose limit Go holds, with that limit,
// and a RateLimit-Remaining below what Go's rows leave there; until a re-read
// settles it, it is window_race_unconfirmed, which pages. Every other window
// refusal is the would-admit outright.
func TestAWindowRaceCandidateIsUnconfirmedUntilReRead(t *testing.T) {
	allowed, admit, _ := windowCase(t, 400_000)
	v := admit.verdict
	if !v.Admit() || v.Remaining != 600_000 {
		t.Fatalf("the verdict: %+v", v)
	}
	candidate := ClassWindowRaceUnconfirmed + ".429." + windowRefusal
	would := ClassWouldAdmit + ".429." + windowRefusal
	for _, x := range []struct {
		name  string
		a     pyAnswer
		class string
	}{
		{"Python's remaining below Go's", windowRefusal429("daily", v.Limit, 0, v.ResetSeconds), candidate},
		{"Python's remaining one below Go's", windowRefusal429("daily", v.Limit, v.Remaining-1, v.ResetSeconds),
			candidate},
		{"the same remaining", windowRefusal429("daily", v.Limit, v.Remaining, v.ResetSeconds), would},
		{"more remaining than Go's", windowRefusal429("daily", v.Limit, v.Remaining+1, v.ResetSeconds), would},
		{"another limit", windowRefusal429("daily", v.Limit+1, 0, v.ResetSeconds), would},
		{"a window Go holds no limit for", windowRefusal429("weekly", v.Limit, 0, v.ResetSeconds), would},
		{"no headers", pyAnswer{status: 429, body: windowRefusal429("daily", 0, 0, 0).body}, would},
		{"another message", rateLimited(pyAnswer{status: 429, body: errorBody(429, windowRefusal, "slow down")},
			v.Limit, 0, v.ResetSeconds), would},
		{"a body not read", rateLimited(pyAnswer{status: 429, why: "bound"}, v.Limit, 0, v.ResetSeconds),
			ClassWouldAdmit + ".429.unread"},
	} {
		r := compareWindowed(allowed, x.a, admit)
		if r.class != x.class || (r.race != nil) != (x.class == candidate) {
			t.Errorf("%s: %s (candidate %v), want %s", x.name, r.class, r.race != nil, x.class)
		}
	}
}

// raceShadow is a bare shadow for confirmRace and raceAlerts: windows reads
// its rows, its clock is clock, alerts are taken.
func raceShadow(t *testing.T, windows ShadowWindowReader, clock func() time.Time) (*fullShadow, *alerts, *logLines) {
	t.Helper()
	a, logs := &alerts{}, &logLines{}
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{},
		Rows: &fakeRows{by: map[string]store.AuthorizationForCompare{}}, Windows: windows, ReadsPerSecond: 1e9,
		Alert: a.raise}, logs.logf, clock, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return sh, a, logs
}

// readAt is Python's answer with its WindowReadTimestampHeader as Python
// writes it (nine fractional digits).
func readAt(a pyAnswer, at time.Time) pyAnswer {
	a.windowReadAt = at.UTC().Format("2006-01-02T15:04:05.000000000Z07:00")
	return a
}

// TestOnlyAReReadAtPythonsTimestampConfirmsAWindowRace (section 8.2 item 3):
// the candidate's re-read is of the rows at exactly Python's read timestamp,
// evaluated in its window at the request's instant. It confirms the race
// when Go's remaining there is exactly Python's (Go sums the rows Python
// summed as Python does, so a settle landed between the first two reads),
// and refutes it on any other remaining, above or below (Go's reading or
// arithmetic differs from Python's on the same rows: the would-admit,
// paged). What a strong read would see now plays no part. No timestamp from
// Python, or one that does not parse, makes no re-read; no re-read (the
// limiter), a failed one, rows that do not give the window, or a window
// reset since (now, or at Python's read) leave it unconfirmed.
func TestOnlyAReReadAtPythonsTimestampConfirmsAWindowRace(t *testing.T) {
	allowed, admit, c := windowCase(t, 400_000)
	v := admit.verdict
	at := admit.at
	pyAt := at.Add(150*time.Millisecond + 7)
	py := readAt(windowRefusal429("daily", v.Limit, 100_000, v.ResetSeconds), pyAt)
	failed := errors.New("synthetic re-read failure")
	day, _, _ := keywindow.Floors(at)
	rowsAt := func(used int64, shards int) func(context.Context, time.Time) ([]keywindow.Row, error) {
		return func(context.Context, time.Time) ([]keywindow.Row, error) {
			return dailyRows(at, 1_000_000, used, shards), nil
		}
	}
	for _, x := range []struct {
		name    string
		answer  pyAnswer
		reread  func(context.Context, time.Time) ([]keywindow.Row, error)
		clock   time.Time
		noToken bool
		class   string
		count   string
		reads   int
	}{
		{"Go's remaining at Python's instant is Python's", py, rowsAt(900_000, 1), at, false, ClassWindowRace,
			"race.confirmed", 1},
		{"Go's remaining there one above Python's", py, rowsAt(899_999, 1), at, false, ClassWouldAdmit,
			"race.refuted", 1},
		{"Go's remaining there below Python's", py, rowsAt(950_000, 1), at, false, ClassWouldAdmit,
			"race.refuted", 1},
		{"Go's remaining there one below Python's", py, rowsAt(900_001, 1), at, false, ClassWouldAdmit,
			"race.refuted", 1},
		{"the re-read sees the first read's rows", py, rowsAt(400_000, 1), at, false, ClassWouldAdmit,
			"race.refuted", 1},
		{"the re-read failed", py, func(context.Context, time.Time) ([]keywindow.Row, error) { return nil, failed },
			at, false, ClassWindowRaceUnconfirmed, "race.unconfirmed.unread", 1},
		{"the re-read's rows are not the key's shards", py, rowsAt(900_000, 2), at, false,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.rows", 1},
		{"the window reset before the re-read", py, rowsAt(900_000, 1), day.Add(24 * time.Hour), false,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.reset", 1},
		{"the window resets a microsecond after the re-read", py, rowsAt(900_000, 1),
			day.Add(24*time.Hour - time.Microsecond), false, ClassWindowRace, "race.confirmed", 1},
		{"Python read in the next window", readAt(py, day.Add(24*time.Hour)), rowsAt(900_000, 1), at, false,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.reset", 1},
		{"the limiter refused the re-read", py, rowsAt(900_000, 1), at, true, ClassWindowRaceUnconfirmed,
			"race.unconfirmed.limiter", 0},
		{"Python sent no read timestamp", windowRefusal429("daily", v.Limit, 100_000, v.ResetSeconds),
			rowsAt(900_000, 1), at, false, ClassWindowRaceUnconfirmed, "race.unconfirmed.read_at_none", 0},
		{"Python's read timestamp has no zone", readAt(py, pyAt), rowsAt(900_000, 1), at, false,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.read_at_malformed", 0},
		{"Python's read timestamp is not one", py, rowsAt(900_000, 1), at, false, ClassWindowRaceUnconfirmed,
			"race.unconfirmed.read_at_malformed", 0},
		{"Python's read timestamp is too long", py, rowsAt(900_000, 1), at, false, ClassWindowRaceUnconfirmed,
			"race.unconfirmed.read_at_malformed", 0},
	} {
		a := x.answer
		switch x.name {
		case "Python's read timestamp has no zone":
			a.windowReadAt = strings.TrimSuffix(a.windowReadAt, "Z")
		case "Python's read timestamp is not one":
			a.windowReadAt = "yesterday"
		case "Python's read timestamp is too long":
			a.windowReadAt = pyAt.Format(time.RFC3339Nano) + strings.Repeat(" ", 20)
		}
		// A strong read now would see a later settle (1,000,000 spent): it
		// must not be what settles the race.
		windows := &fakeWindows{read: func(context.Context) ([]keywindow.Row, error) {
			return dailyRows(at, 1_000_000, 1_000_000, 1), nil
		}, readAt: x.reread}
		clock := x.clock
		sh, _, _ := raceShadow(t, windows, func() time.Time { return clock })
		if x.noToken {
			sh.cfg.ReadsPerSecond, sh.windowLimiter.tokens = 1e-9, 0
		}
		r := compareWindowed(allowed, a, admit)
		if r.race == nil {
			t.Fatalf("%s: no race candidate: %s", x.name, r.class)
		}
		sh.confirmRace(allowed.ForShadow(), admit, &r)
		calls := windows.seen()
		if r.class != x.class+".429."+windowRefusal || !slices.Contains(r.windows, x.count) {
			t.Errorf("%s: %s %v, want %s and %s", x.name, r.class, r.windows, x.class, x.count)
		}
		if len(calls) != x.reads {
			t.Errorf("%s: re-reads %+v, want %d", x.name, calls, x.reads)
		}
		want := pyAt
		if x.name == "Python read in the next window" {
			want = day.Add(24 * time.Hour)
		}
		for _, call := range calls {
			if call.keyHash != c.Facts.Key.Hash || !call.at.Equal(want) {
				t.Errorf("%s: a re-read of %s at %s, want at Python's %s", x.name, call.keyHash, call.at, want)
			}
		}
		if r.race.first != 600_000 || r.race.python != 100_000 {
			t.Errorf("%s: the race's figures %+v", x.name, *r.race)
		}
	}
}

// TestPythonReadAtParsesOnlyAnInstant: the header's instant to the
// nanosecond, in UTC; nothing for an absent header, and a malformed one for
// anything that is not an RFC 3339 instant.
func TestPythonReadAtParsesOnlyAnInstant(t *testing.T) {
	want := time.Date(2026, 10, 19, 12, 0, 0, 123_456_789, time.UTC)
	for _, x := range []struct {
		h   string
		at  time.Time
		why string
	}{
		{"2026-10-19T12:00:00.123456789Z", want, ""},
		{"2026-10-19T14:00:00.123456789+02:00", want, ""},
		{"2026-10-19T12:00:00Z", want.Truncate(time.Second), ""},
		{"", time.Time{}, "read_at_none"},
		{"2026-10-19T12:00:00.123456789", time.Time{}, "read_at_malformed"},
		{"1792000000123456789", time.Time{}, "read_at_malformed"},
		{"0001-01-01T00:00:00Z", time.Time{}, "read_at_malformed"},
		{"2026-10-19T12:00:00.123456789Z" + strings.Repeat("0", 20), time.Time{}, "read_at_malformed"},
	} {
		at, why := pythonReadAt(x.h)
		if !at.Equal(x.at) || why != x.why || (why == "" && at.Location() != time.UTC) {
			t.Errorf("%q: %s %q, want %s %q", x.h, at, why, x.at, x.why)
		}
	}
}

// TestAPinnedReReadPagesAnUndercountAndNotARace, over the edge: Python's 429
// carries the read timestamp of its window check's snapshot, and the re-read
// is of the rows as they stood then. A Go read that undercounts the same way
// on every request (here it never sees 50,000 of the day Python sees) pages,
// even on a busy key whose settles after Python's read (100,000 of them)
// bring a strong re-read down to Python's remaining, which would have hidden
// it as a race; a settle that lands between Go's first read and Python's is
// a confirmed race, counted and traced, never paged, however much lands
// after; a re-read that fails, or a 429 with no timestamp or one that does
// not parse, pages as unconfirmed.
func TestAPinnedReReadPagesAnUndercountAndNotARace(t *testing.T) {
	f := authorizeGoldens(t)
	_, admit, c := windowCase(t, 400_000)
	v := admit.verdict
	at := c.at()
	pyAt := at.Add(200*time.Millisecond + 3)
	header := pyAt.UTC().Format("2006-01-02T15:04:05.000000000Z07:00")
	failed := errors.New("synthetic re-read failure")
	// history is a key's day usage as the store versions it: what Go's first
	// read sees, what the key had spent at Python's read, and what it has
	// spent by the time of a strong re-read; Go's reads all miss undercount
	// of it.
	type history struct {
		first, atPython, latest, undercount int64
	}
	for _, x := range []struct {
		name     string
		requests int
		h        history
		header   string
		failAt   bool
		class    string
		count    string
		paged    bool
	}{
		{"a busy key's persistent undercount", 20, history{450_000, 900_000, 1_000_000, 50_000}, header, false,
			ClassWouldAdmit, "race.refuted", true},
		{"a quiet key's persistent undercount", 5, history{900_000, 900_000, 900_000, 500_000}, header, false,
			ClassWouldAdmit, "race.refuted", true},
		{"a real race with later settles", 5, history{400_000, 900_000, 1_000_000, 0}, header, false,
			ClassWindowRace, "race.confirmed", false},
		{"the re-read fails", 3, history{400_000, 900_000, 1_000_000, 0}, header, true,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.unread", true},
		{"no read timestamp", 3, history{400_000, 900_000, 1_000_000, 0}, "", false,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.read_at_none", true},
		{"a read timestamp that does not parse", 3, history{400_000, 900_000, 1_000_000, 0}, "not-a-time", false,
			ClassWindowRaceUnconfirmed, "race.unconfirmed.read_at_malformed", true},
	} {
		headers := []string{"RateLimit-Limit", "1000000", "RateLimit-Remaining", "100000", "RateLimit-Reset",
			strconv.FormatInt(v.ResetSeconds, 10)}
		if x.header != "" {
			headers = append(headers, WindowReadTimestampHeader, x.header)
		}
		respond := answerWith(429, errorBody(429, windowRefusal,
			"API key daily spend limit exceeded; resets at 2026-10-20T00:00:00Z"), headers...)
		h := x.h
		// checked is whether this request's window check has read: its first
		// strong read is that check, before Python's read; any later strong
		// read sees every settle since.
		var checked atomic.Bool
		windows := &fakeWindows{read: func(context.Context) ([]keywindow.Row, error) {
			if checked.CompareAndSwap(false, true) {
				return dailyRows(at, 1_000_000, h.first-h.undercount, 1), nil
			}
			return dailyRows(at, 1_000_000, h.latest-h.undercount, 1), nil
		}, readAt: func(_ context.Context, asOf time.Time) ([]keywindow.Row, error) {
			switch {
			case x.failAt:
				return nil, failed
			case asOf.Before(pyAt):
				return dailyRows(at, 1_000_000, h.first-h.undercount, 1), nil
			case asOf.Equal(pyAt):
				return dailyRows(at, 1_000_000, h.atPython-h.undercount, 1), nil
			}
			return dailyRows(at, 1_000_000, h.latest-h.undercount, 1), nil
		}}
		a := &alerts{}
		fe := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) { cfg.Windows, cfg.Alert = windows, a.raise })
		fe.answer(respond)
		for range x.requests {
			checked.Store(false)
			fe.send(t, c)
		}
		got := fe.edge.DecideShadowCounts()
		class := x.class + ".429." + windowRefusal
		taken := a.taken()
		paged := len(taken[AlertWouldAdmit])
		// Five races of five reads in the hour: the backstop, not the page.
		if backstop := len(taken[AlertWindowRace]) > 0; backstop != (x.class == ClassWindowRace) {
			t.Errorf("%s: race alerts %q", x.name, taken[AlertWindowRace])
		}
		if got.Classes[class] != x.requests || len(got.Classes) != 1 || (paged > 0) != x.paged ||
			got.Windows[x.count] != x.requests {
			t.Errorf("%s: classes %v, windows %v, %d pages", x.name, got.Classes, got.Windows, paged)
		}
		calls := windows.seen()
		pinned := 0
		for _, call := range calls {
			if !call.at.IsZero() {
				pinned++
				if !call.at.Equal(pyAt) {
					t.Errorf("%s: a re-read at %s, not Python's %s", x.name, call.at, pyAt)
				}
			}
		}
		wantPinned := x.requests
		if x.header != header {
			wantPinned = 0
		}
		if len(calls) != x.requests+wantPinned || pinned != wantPinned {
			t.Errorf("%s: %d reads, %d at a timestamp, for %d requests", x.name, len(calls), pinned, x.requests)
		}
		// Each send is its own period (WaitDecideShadow logs it), so no
		// page is lost to the period's bound.
		if x.paged && paged != x.requests {
			t.Errorf("%s: %d pages for %d would-admits", x.name, paged, x.requests)
		}
		if x.class == ClassWindowRace && !fe.logs.has("fastpath edge decide shadow window race: class="+class+
			" key="+c.Facts.Key.Hash[:min(len(c.Facts.Key.Hash), keyHashPrefixLen)]+
			" window=daily go=600000 reread=100000 python=100000 gap=500000 ") {
			t.Errorf("%s: no race line traces it: %q", x.name, fe.logs.all())
		}
	}
}

// TestTheRaceRateAndAKeysRepeatsAlert (section 8.3): AlertWindowRace, once a
// period, when the last hour's confirmed races pass RaceRateShare of its
// window reads (at least RaceRateMin of them), or one key races more than
// RaceKeyRepeats times; periods and races older than an hour no longer
// count.
func TestTheRaceRateAndAKeysRepeatsAlert(t *testing.T) {
	race := func(key string) fullResult {
		return fullResult{class: ClassWindowRace + ".429." + windowRefusal, windows: []string{"read.made"},
			race: &raceCandidate{keyHashPrefix: key}}
	}
	read := fullResult{class: ClassAgreeFull, windows: []string{"read.made"}}
	now := time.Now()
	for _, x := range []struct {
		name         string
		reads, races int
		keys         int
		alert        string
	}{
		{"3 races in 5,000 reads", 5000, 3, 1, "3 window races of 5000 window reads"},
		{"3 races in 7,000 reads", 7000, 3, 1, ""},
		{"2 races in 100 reads", 100, 2, 1, ""},
		{"one key 4 times", 1_000_000, 4, 4, "raced 4 times"},
		{"one key 3 times", 1_000_000, 3, 3, ""},
	} {
		sh, a, _ := raceShadow(t, nil, time.Now)
		for i := range x.races {
			sh.races.count(race(fmt.Sprintf("kh%06d", i%max(1, x.races/x.keys))), now)
		}
		for range x.reads - x.races {
			sh.races.count(read, now)
		}
		sh.raceAlerts(now)
		got := a.taken()[AlertWindowRace]
		if (x.alert == "") != (len(got) == 0) || (len(got) > 0 && !strings.Contains(got[0], x.alert)) {
			t.Errorf("%s: alerts %q", x.name, got)
		}
	}
	// The hour rolls: a period over an hour old no longer counts.
	sh, a, _ := raceShadow(t, nil, time.Now)
	hourAgo := now.Add(-61 * time.Minute)
	for range 4 {
		sh.races.count(race("kh000000"), hourAgo)
	}
	sh.raceAlerts(hourAgo)
	if got := a.taken()[AlertWindowRace]; len(got) != 1 || !strings.Contains(got[0], "raced 4 times") {
		t.Fatalf("4 races of one key in 4 reads: %q", got)
	}
	sh.races.count(read, now)
	sh.raceAlerts(now)
	if got := a.taken()[AlertWindowRace]; len(got) != 0 {
		t.Fatalf("the hour-old races still count: %q", got)
	}
}
