package edge

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"go/ast"
	"go/parser"
	gotoken "go/token"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The decide path (decide_path.go) over the P1 goldens and against each of
// its rules: the case's request through the edge with -edge-decide on, a
// fake front door answering the case's admission, a fake store answering
// the case's insert outcome, and a fake Python that counts what reaches it.

// fakeAdmitter is the node's front door: authorize answers each admit (the
// case's envelope by default), and every admit and refund is kept.
type fakeAdmitter struct {
	authorize func(ctx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized
	mu        sync.Mutex
	asked     []frontdoor.AuthorizeOf
	refunds   []frontdoor.RefundOf
	// refundLive is whether each refund's context was live, with a
	// deadline at most RefundBudget away, when it was sent.
	refundLive []bool
}

func (f *fakeAdmitter) Authorize(ctx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized {
	f.mu.Lock()
	f.asked = append(f.asked, a)
	f.mu.Unlock()
	return f.authorize(ctx, a)
}

func (f *fakeAdmitter) Refund(ctx context.Context, r frontdoor.RefundOf) frontdoor.TerminalAnswer {
	d, ok := ctx.Deadline()
	f.mu.Lock()
	f.refunds = append(f.refunds, r)
	f.refundLive = append(f.refundLive, ctx.Err() == nil && ok && time.Until(d) <= RefundBudget)
	f.mu.Unlock()
	return frontdoor.TerminalAnswer{Status: frontdoor.Won}
}

func (f *fakeAdmitter) seen() ([]frontdoor.AuthorizeOf, []frontdoor.RefundOf, []bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.asked), slices.Clone(f.refunds), slices.Clone(f.refundLive)
}

// fakeInserter is the store's insert: insert answers each, with code as
// the transaction's error's, and the rows of each are kept.
type fakeInserter struct {
	insert func(ctx context.Context) store.InsertOutcome
	code   codes.Code
	mu     sync.Mutex
	rows   []gwauth.Composed
}

func (f *fakeInserter) InsertLeasedAuthorization(ctx context.Context, a gwauth.AuthorizationRow,
	r gwauth.ReservationRow) (store.InsertOutcome, codes.Code) {
	f.mu.Lock()
	f.rows = append(f.rows, gwauth.Composed{Authorization: a, Reservation: r})
	f.mu.Unlock()
	return f.insert(ctx), f.code
}

func (f *fakeInserter) inserted() []gwauth.Composed {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.rows)
}

// decideEdge is an edge with the decide path on over one case's world, its
// snapshot and policy held, its decide copy on for the case's workspace, the
// fake front door bound and a fake Python answering 418 to whatever passes.
type decideEdge struct {
	edge     *Edge
	python   *fakePython
	cache    *recordingCache
	admitter *fakeAdmitter
	rows     *fakeInserter
	holder   *LateAdmitter
	policy   *routingsnapshot.EdgePolicy
	logs     *logLines
}

func newDecideEdge(t *testing.T, f authorizeFile, c authorizeCase, py *fakePython) *decideEdge {
	t.Helper()
	return newDecideEdgeWith(t, f, c, py, nil)
}

// newDecideEdgeWith is newDecideEdge with the full shadow on too when
// shadow is not nil, as production runs both (-edge-decide and
// -edge-decide-shadow): the shadow follows what the decide path passes.
func newDecideEdgeWith(t *testing.T, f authorizeFile, c authorizeCase, py *fakePython,
	shadow *DecideShadowConfig) *decideEdge {
	t.Helper()
	if py == nil {
		py = newFakePython(t)
	}
	py.respond.Store(func() *func(http.ResponseWriter) {
		f := answerWith(http.StatusTeapot, []byte(`{"python":true}`))
		return &f
	}())
	eol := endOfLifeOf(t, c)
	h := &decideEdge{python: py, holder: &LateAdmitter{}, logs: &logLines{},
		admitter: &fakeAdmitter{authorize: func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
			return frontdoor.Authorized{Status: frontdoor.Admitted, Envelope: c.Admitted.Envelope, EndOfLife: eol}
		}},
		rows: &fakeInserter{insert: func(context.Context) store.InsertOutcome { return store.Inserted }}}
	upstream, _ := url.Parse(py.server.URL)
	snapshotURL, _ := url.Parse(py.server.URL + "/internal/fastpath/routing-snapshot")
	policyURL, _ := url.Parse(py.server.URL + "/internal/fastpath/edge-policy")
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys := []SnapshotKey{{Kid: "routing-snapshot-1", PublicKey: public}}
	h.policy = policyOf(t, f, c)
	h.cache = &recordingCache{KeyCache: warmCache(t, &fakeWorld{c: c}, lookupHashOf(c.body()), c.at())}
	e, err := New(Config{Token: f.InternalToken, Upstream: upstream, Boots: bootsOf(t, f, c),
		Clock: func() time.Time { return c.at() }, Logf: h.logs.logf, Transport: py.transport,
		Snapshot: SnapshotConfig{URL: snapshotURL, Keys: keys, Plane: "gcp/us-central1"},
		Policy:   PolicyConfig{URL: policyURL, Keys: keys, Plane: "gcp/us-central1"},
		Admitter: h.holder,
		Decide:   &DecideConfig{Cache: h.cache, Decides: &fakeDecides{}, Rows: h.rows, Mint: pinnedMint(c)},
		DecideShadow: func() *DecideShadowConfig {
			if shadow != nil && shadow.Cache == nil {
				shadow.Cache = h.cache
			}
			return shadow
		}()})
	if err != nil {
		t.Fatal(err)
	}
	snap := snapshotOver(t, f, h.policy)
	e.poller.current.Store(hold(snap, routingsnapshot.Claims{CompiledAt: snap.CompiledAt, NotAfter: snap.NotAfter,
		SHA256: "golden-snapshot"}))
	e.policy.current.Store(&heldPolicy{policy: h.policy, claims: routingsnapshot.PolicyClaims{IssuedAt: h.policy.IssuedAt,
		NotAfter: h.policy.NotAfter, SHA256: "golden-policy"}})
	view := decideAll(h.policy, c.Facts.Key.WorkspaceID, c.at())
	e.decider.decides.current.Store(&view)
	if e.full != nil {
		e.full.decides.current.Store(&view)
		t.Cleanup(e.WaitDecideShadow)
	}
	h.holder.Bind(h.admitter)
	h.edge = e
	t.Cleanup(e.WaitDecide)
	return h
}

// request is the case's request as the enclave sends it, on ctx.
func request(c authorizeCase, ctx context.Context) *http.Request {
	req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body())).WithContext(ctx)
	req.Header = c.header()
	return req
}

// send is the case's request through the edge, and the edge's answer once
// its refunds are sent.
func (h *decideEdge) send(t *testing.T, c authorizeCase) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.edge.ServeHTTP(rec, request(c, context.Background()))
	h.edge.decider.refunds.Wait()
	return rec
}

// goTiming is §6's timing object as Go writes it, any values.
var goTiming = regexp.MustCompile(`"timing":\{"total_ms":\d+,"spanner_rpcs":\d+,"key_lookup_ms":\d+,"routing_ms":\d+,` +
	`"store_ms":\d+,"post_commit_ms":\d+\}`)

// withGoldenTiming is body with Go's timing values replaced by the
// goldens' zeros: the keys and their order must be §6's, and the values are
// diagnostics (the comparator ignores them).
func withGoldenTiming(body []byte) string {
	return goTiming.ReplaceAllString(string(body),
		`"timing":{"total_ms":0,"spanner_rpcs":0,"key_lookup_ms":0,"routing_ms":0,"store_ms":0,"post_commit_ms":0}`)
}

// ownerAnswers are the front door's answers the goldens' owner.* passes
// stand for.
var ownerAnswers = map[string]frontdoor.Status{
	PassOwnerOff: frontdoor.Off, PassOwnerBusy: frontdoor.Busy, PassOwnerInvalid: frontdoor.Invalid,
	PassOwnerFailed: frontdoor.Failed,
}

// insertFaults are the goldens' leased inserts (§10.1's insert), each as
// the commit's error, how many times its callback ran, and whether the row
// is there for the read-back: what the store's insert meets.
var insertFaults = map[string]struct {
	code   codes.Code // codes.OK: committed
	runs   int32
	landed bool
}{
	"inserted":            {codes.OK, 1, true},
	"unknown_landed":      {codes.DeadlineExceeded, 1, true},
	"unknown_not_landed":  {codes.DeadlineExceeded, 1, false},
	"invalid_argument":    {codes.InvalidArgument, 1, false},
	"failed_precondition": {codes.FailedPrecondition, 1, false},
	"aborted":             {codes.Aborted, 2, false},
}

// insertOf is the store's outcome for a case: its insert's error
// classified by store.ClassifyInsert (so a golden label cannot drift from
// §8.4's table), then the store's one read-back. A case Python found the
// scope taken for is AlreadyExists with no row of ours. With it, the code
// the store reports.
func insertOf(t *testing.T, c authorizeCase) (store.InsertOutcome, codes.Code) {
	t.Helper()
	fault, ok := insertFaults[c.Insert]
	if !ok {
		t.Fatalf("%s: insert %q", c.Name, c.Insert)
	}
	if c.Expect.Reason == PassInsertScopeTaken {
		fault.code, fault.landed = codes.AlreadyExists, false
	}
	var err error
	if fault.code != codes.OK {
		err = status.Error(fault.code, c.Insert)
	}
	outcome, alreadyExists := store.ClassifyInsert(err, fault.runs)
	switch {
	case outcome != store.Unknown:
		return outcome, fault.code
	case fault.landed:
		return store.Landed, fault.code
	case alreadyExists:
		return store.ScopeTaken, fault.code
	}
	return store.Unknown, fault.code
}

// TestEveryGoldenCaseThroughTheDecidePath: with the decide path on, every
// answered case is answered by the edge with Python's 200 byte for byte
// (timing values aside), Python never reached, its rows inserted as Python
// wrote them, and nothing refunded (a request naming a cache_affinity_key
// among them, in Python's cold order, §1 item 5); the unavailable case is Python's 503
// exactly, Retry-After 1, the hold kept, counted by its insert's code; every
// pass case reaches Python
// with its §3 reason counted, the owner asked only for a reason after the
// admit, and a refund sent for exactly the insert's passes (its own
// envelope, Python's refund money, on a live context of its own).
func TestEveryGoldenCaseThroughTheDecidePath(t *testing.T) {
	f := authorizeGoldens(t)
	py := newFakePython(t)
	answered, unavailable, passed, refunded, affinity := 0, 0, 0, 0, 0
	unavailableCodes := map[string]int{}
	for _, c := range f.Cases {
		h := newDecideEdge(t, f, c, py)
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
		hits := py.hits.Load()
		rec := h.send(t, c)
		reached := py.hits.Load() - hits
		asked, refunds, live := h.admitter.seen()
		counts := h.edge.DecideCounts()
		_, evaluatorPassed := evaluatorPasses[c.Name]
		switch {
		case c.Expect.Outcome == "answer" && !evaluatorPassed:
			if rec.Code != http.StatusOK || withGoldenTiming(rec.Body.Bytes()) != c.Expect.Response || reached != 0 {
				t.Errorf("%s: %d %s (Python reached %d)\nwant %s", c.Name, rec.Code, rec.Body.Bytes(), reached,
					c.Expect.Response)
				continue
			}
			if rec.Header().Get("Content-Type") != "application/json" ||
				rec.Header().Get("Content-Length") != strconv.Itoa(rec.Body.Len()) || rec.Header().Get("Content-Encoding") != "" {
				t.Errorf("%s: headers %v", c.Name, rec.Header())
			}
			rows := h.rows.inserted()
			if len(rows) != 1 || !rowsAreGoldens(t, c, rows[0]) {
				t.Errorf("%s: %d inserts", c.Name, len(rows))
			}
			// The lease is asked for the reservation, not the rows' estimate.
			want := frontdoor.AuthorizeOf{Workspace: c.Facts.Key.WorkspaceID, Request: c.IDs.AuthorizationID,
				Estimate: c.Expect.Reservation, Boot: []byte(c.Boot.Kid)}
			if len(asked) != 1 || asked[0].Workspace != want.Workspace || asked[0].Request != want.Request ||
				asked[0].Estimate != want.Estimate || string(asked[0].Boot) != string(want.Boot) || asked[0].Stream ||
				asked[0].OpenHeartbeat {
				t.Errorf("%s: the admit %+v, want %+v", c.Name, asked, want)
			}
			if len(refunds) != 0 || counts.Answered != 1 || (counts.ReadBack == 1) != (outcome == store.Landed) {
				t.Errorf("%s: refunds %d, counts %+v", c.Name, len(refunds), counts)
			}
			if c.Expect.Affinity != nil {
				affinity++
			}
			answered++
		case c.Expect.Outcome == "unavailable":
			if rec.Code != http.StatusServiceUnavailable || withGoldenTiming(rec.Body.Bytes()) != c.Expect.Response ||
				rec.Header().Get("Retry-After") != "1" || rec.Header().Get("Content-Type") != "application/json" ||
				reached != 0 || len(refunds) != 0 || counts.Unavailable != 1 {
				t.Errorf("%s: %d %v %s, refunds %d, Python reached %d", c.Name, rec.Code, rec.Header(), rec.Body.Bytes(),
					len(refunds), reached)
			}
			if !maps.Equal(counts.UnavailableByCode, map[string]int64{codeName(code): 1}) {
				t.Errorf("%s: unavailable by code %v, want %s", c.Name, counts.UnavailableByCode, codeName(code))
			}
			unavailableCodes[codeName(code)]++
			unavailable++
		default:
			if evaluatorPassed {
				reason = PassEvaluate
			}
			want := reason
			if reason == PassEvaluateRefused && counts.Passed[PassEvaluate] == 1 {
				// A refusal Go's evaluator does not reproduce passes as
				// evaluate.pass (TestEveryGoldenCaseThroughThePipeline).
				want = PassEvaluate
			}
			if rec.Code != http.StatusTeapot || reached != 1 || counts.Passed[want] != 1 || counts.Answered != 0 {
				t.Errorf("%s: %d, Python reached %d, counts %+v; want a pass for %s", c.Name, rec.Code, reached, counts,
					want)
				continue
			}
			admits := strings.HasPrefix(reason, "owner.") && reason != PassOwnerUnreachable ||
				strings.HasPrefix(reason, "insert.")
			if (len(asked) == 1) != admits || len(asked) > 1 {
				t.Errorf("%s: asked the owner %d times", c.Name, len(asked))
			}
			if strings.HasPrefix(reason, "insert.") {
				if len(refunds) != 1 || refunds[0].Envelope != c.Admitted.Envelope ||
					string(refunds[0].Money) != `{"charge":0,"estimate":0}` || !live[0] || counts.Refunded != 1 {
					t.Errorf("%s: refunds %+v (live %v)", c.Name, refunds, live)
				}
				refunded++
			} else if len(refunds) != 0 {
				t.Errorf("%s: %d refunds of a pass with no hold", c.Name, len(refunds))
			}
			passed++
		}
		if n := h.edge.decider.inflight.inFlight(lookupHashOf(c.body())); n != 0 {
			t.Errorf("%s: %d still in flight", c.Name, n)
		}
	}
	t.Logf("%d answered (%d naming a cache_affinity_key), %d unavailable, %d passed (%d refunded)", answered, affinity,
		unavailable, passed, refunded)
	if answered != 192 || affinity != 18 || unavailable != 3 || refunded != 7 {
		t.Fatalf("answered %d (%d with a cache affinity key), unavailable %d, refunded %d: not the goldens' counts",
			answered, affinity, unavailable, refunded)
	}
	// The goldens' three unavailable answers, a code each: the clock ahead
	// of Spanner's among them (§8.4).
	if !maps.Equal(unavailableCodes, map[string]int{"deadline_exceeded": 1, "failed_precondition": 1, "aborted": 1}) {
		t.Fatalf("the unavailable answers' codes: %v", unavailableCodes)
	}
}

// baseDecide is an edge over the plain answered case.
func baseDecide(t *testing.T) (*decideEdge, authorizeCase) {
	t.Helper()
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	return newDecideEdge(t, f, c, nil), c
}

// TestOnlyARowOfOursIsAnswered: of each insert outcome, Inserted and Landed
// answer 200 (Landed as after a read-back), Unknown answers Python's 503
// and keeps the hold, and ScopeTaken and Failed refund and pass. An admit
// with no envelope passes, with nothing to refund with.
func TestOnlyARowOfOursIsAnswered(t *testing.T) {
	for _, tc := range []struct {
		outcome  store.InsertOutcome
		status   int
		refunds  int
		toPython bool
	}{
		{store.Inserted, http.StatusOK, 0, false},
		{store.Landed, http.StatusOK, 0, false},
		{store.Unknown, http.StatusServiceUnavailable, 0, false},
		{store.ScopeTaken, http.StatusTeapot, 1, true},
		{store.Failed, http.StatusTeapot, 1, true},
	} {
		h, c := baseDecide(t)
		h.rows.insert = func(context.Context) store.InsertOutcome { return tc.outcome }
		rec := h.send(t, c)
		_, refunds, _ := h.admitter.seen()
		if rec.Code != tc.status || len(refunds) != tc.refunds || (h.python.hits.Load() == 1) != tc.toPython {
			t.Errorf("%v: %d, %d refunds, Python reached %d", tc.outcome, rec.Code, len(refunds), h.python.hits.Load())
		}
		if tc.outcome == store.Unknown && !bytes.HasPrefix(rec.Body.Bytes(),
			[]byte(`{"error":{"code":503,"message":"Authorization unavailable; retry","type":"service_unavailable",`)) {
			t.Errorf("unknown: %s", rec.Body.Bytes())
		}
	}
	h, c := baseDecide(t)
	h.admitter.authorize = func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
		return frontdoor.Authorized{Status: frontdoor.Admitted, EndOfLife: c.at()}
	}
	rec := h.send(t, c)
	_, refunds, _ := h.admitter.seen()
	if rec.Code != http.StatusTeapot || len(refunds) != 0 || len(h.rows.inserted()) != 0 ||
		h.edge.DecideCounts().Passed[PassOwnerInvalid] != 1 {
		t.Errorf("admitted with no envelope: %d, %d refunds, %d inserts", rec.Code, len(refunds), len(h.rows.inserted()))
	}
}

// TestTheInFlightLimitIsPerLookupHash (decision 14): with the policy's
// limit of two and two of a key's requests between their admit and their
// answer, a third passes (inflight.key) without asking the owner; once
// they have answered, the key holds no entry and the next is admitted.
func TestTheInFlightLimitIsPerLookupHash(t *testing.T) {
	h, c := baseDecide(t)
	h.policy.GatewayAuthorizeMaxInFlightPerKey = 2
	release := make(chan struct{})
	var blocked atomic.Int32
	h.rows.insert = func(context.Context) store.InsertOutcome {
		// The first two hold their slots; one past the limit that got this
		// far answers at once, so the test fails rather than hangs.
		if blocked.Add(1) <= 2 {
			<-release
		}
		return store.Inserted
	}
	recs := make([]*httptest.ResponseRecorder, 2)
	var wg sync.WaitGroup
	for i := range recs {
		recs[i] = httptest.NewRecorder()
		wg.Add(1)
		go func() {
			defer wg.Done()
			h.edge.ServeHTTP(recs[i], request(c, context.Background()))
		}()
	}
	waitFor(t, func() bool { return blocked.Load() == 2 })
	third := h.send(t, c)
	asked, _, _ := h.admitter.seen()
	if third.Code != http.StatusTeapot || h.edge.DecideCounts().Passed[PassInFlightKey] != 1 || len(asked) != 2 {
		t.Errorf("the third: %d, counts %+v, admits %d", third.Code, h.edge.DecideCounts(), len(asked))
	}
	close(release)
	wg.Wait()
	for i, rec := range recs {
		if rec.Code != http.StatusOK {
			t.Errorf("request %d: %d", i, rec.Code)
		}
	}
	if n := h.edge.decider.inflight.inFlight(lookupHashOf(c.body())); n != 0 {
		t.Fatalf("%d still in flight", n)
	}
	h.rows.insert = func(context.Context) store.InsertOutcome { return store.Inserted }
	if rec := h.send(t, c); rec.Code != http.StatusOK {
		t.Errorf("after the two answered: %d", rec.Code)
	}
	var k inFlightKeys
	if k.enter("key", 0) || !k.enter("key", 1) || k.enter("key", 1) || !k.enter("other", 1) {
		t.Error("enter does not hold the limit per key")
	}
	k.leave("key")
	k.leave("other")
	if len(k.n) != 0 {
		t.Errorf("entries left: %v", k.n)
	}
}

// TestPythonsInFlightRefusalOfAnInFlightPassIsAPass: with the decide path
// and the full shadow both on, a request the decide path passed at its own
// in-flight limit (inflight.key) that Python refuses at its per-key
// in-flight gate (429 rate_limited, "Too many concurrent requests for this
// API key") is passed.inflight.key: Go answered nothing, and its gate made
// Python's call. Not a would-admit, and nothing pages. Every other refusal
// of such a pass (a 402, the ingress limiter's 429, a window's 429) stays a
// would-admit and pages, and so does Python's in-flight refusal of a
// request the decide path passed for another reason after its gate let the
// request in (owner.busy); TestOnlyAnInFlightPassAgreesWithAnInFlightRefusal
// holds the unread 429 and the shadow alone. The 2026-10-31 ramp's 83 pages
// were the first case.
func TestPythonsInFlightRefusalOfAnInFlightPassIsAPass(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	inFlight := errorBody(429, "rate_limited", "Too many concurrent requests for this API key")
	for _, tc := range []struct {
		name    string
		atLimit bool // a request of the key holds the one slot
		status  int
		body    []byte
		pass    string
		want    string
		pages   bool
	}{
		{"in_flight_refusal", true, 429, inFlight, PassInFlightKey, ClassGoPassed + "." + PassInFlightKey, false},
		{"payment_required", true, 402, errorBody(402, "payment_required", "Insufficient credits"), PassInFlightKey,
			ClassWouldAdmit + ".402.payment_required", true},
		{"ingress_limiter", true, 429, errorBody(429, "rate_limited", "Rate limit exceeded"), PassInFlightKey,
			ClassWouldAdmit + ".429.rate_limited", true},
		{"window", true, 429, errorBody(429, "key_window_limit_exceeded", "API key daily spend limit exceeded; x"),
			PassInFlightKey, ClassWouldAdmit + ".429.key_window_limit_exceeded", true},
		{"owner_busy", false, 429, inFlight, PassOwnerBusy, ClassWouldAdmit + ".429.rate_limited", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var mu sync.Mutex
			var alerts []string
			shadow := &DecideShadowConfig{Decides: &fakeDecides{}, Rows: &fakeRows{by: map[string]store.AuthorizationForCompare{}},
				SampleEvery: 1, ReadsPerSecond: 1e9, Alert: func(kind, _, what string) {
					mu.Lock()
					defer mu.Unlock()
					alerts = append(alerts, kind+"|"+what)
				}}
			h := newDecideEdgeWith(t, f, c, nil, shadow)
			h.policy.GatewayAuthorizeMaxInFlightPerKey = 1
			release := make(chan struct{})
			var wg sync.WaitGroup
			if tc.atLimit {
				var held atomic.Int32
				h.rows.insert = func(context.Context) store.InsertOutcome {
					if held.Add(1) == 1 {
						<-release
					}
					return store.Inserted
				}
				wg.Add(1)
				go func() {
					defer wg.Done()
					h.edge.ServeHTTP(httptest.NewRecorder(), request(c, context.Background()))
				}()
				waitFor(t, func() bool { return held.Load() == 1 })
			} else {
				h.admitter.authorize = func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
					return frontdoor.Authorized{Status: frontdoor.Busy}
				}
			}
			respond := answerWith(tc.status, tc.body, "Retry-After", "1")
			h.python.respond.Store(&respond)
			rec := h.send(t, c)
			h.edge.WaitDecideShadow()
			close(release)
			wg.Wait()
			got := h.edge.DecideShadowCounts().Classes
			if rec.Code != tc.status || h.edge.DecideCounts().Passed[tc.pass] != 1 || got[tc.want] != 1 {
				t.Fatalf("Python's %d to a pass: edge %d, passes %v, classes %v, want %s", tc.status, rec.Code,
					h.edge.DecideCounts().Passed, got, tc.want)
			}
			mu.Lock()
			defer mu.Unlock()
			if paged := len(alerts) == 1 && alerts[0] == AlertWouldAdmit+
				"|the edge would have answered an authorize Python refused: "+tc.want; paged != tc.pages ||
				(!tc.pages && len(alerts) != 0) {
				t.Errorf("alerts %q, want a page %v", alerts, tc.pages)
			}
		})
	}
}

// TestTheDecideBudgetIsTheAdmitsDeadline (decision 12): the owner's admit
// gets a context whose deadline is DecideBudget after the request arrived,
// and an owner that answers nothing by then passes (budget.spent), with no
// insert and no refund (nothing was admitted); a budget already spent
// before the admit (a slow boot read) passes without asking the owner.
func TestTheDecideBudgetIsTheAdmitsDeadline(t *testing.T) {
	h, c := baseDecide(t)
	var deadline time.Time
	h.admitter.authorize = func(ctx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized {
		deadline, _ = ctx.Deadline()
		select {
		case <-ctx.Done():
		case <-time.After(DecideBudget + time.Second):
		}
		return frontdoor.Authorized{Status: frontdoor.Busy}
	}
	start := time.Now()
	rec := h.send(t, c)
	took := time.Since(start)
	_, refunds, _ := h.admitter.seen()
	if rec.Code != http.StatusTeapot || h.edge.DecideCounts().Passed[PassBudgetSpent] != 1 || len(refunds) != 0 ||
		len(h.rows.inserted()) != 0 {
		t.Errorf("an owner past the budget: %d, counts %+v", rec.Code, h.edge.DecideCounts())
	}
	if d := deadline.Sub(start); d > DecideBudget+5*time.Millisecond || d < DecideBudget-100*time.Millisecond ||
		took > DecideBudget+500*time.Millisecond {
		t.Errorf("the admit's deadline %v after arrival, the pass after %v", d, took)
	}

	f := authorizeGoldens(t)
	h = newDecideEdge(t, f, c, nil)
	b := bootsOf(t, f, c)
	b.slow = DecideBudget + 100*time.Millisecond
	h.edge.cfg.Boots, h.edge.cfg.BootWait = b, 2*DecideBudget
	rec = h.send(t, c)
	asked, _, _ := h.admitter.seen()
	if rec.Code != http.StatusTeapot || len(asked) != 0 || h.edge.DecideCounts().Passed[PassBudgetSpent] != 1 {
		t.Errorf("a budget spent before the admit: %d, %d admits, counts %+v", rec.Code, len(asked),
			h.edge.DecideCounts())
	}
}

// TestTheBudgetsAreTheContracts (decision 12): the worst answer comes 3.5 s
// after arrival (the decide budget, the insert's, its read-back's), which
// leaves the enclave's 25 s; any budget growing fails here.
func TestTheBudgetsAreTheContracts(t *testing.T) {
	if DecideBudget != 1500*time.Millisecond || store.InsertDeadline != 1500*time.Millisecond ||
		store.ReadBackDeadline != 500*time.Millisecond || RefundBudget != time.Second ||
		DecideBudget+store.InsertDeadline+store.ReadBackDeadline != 3500*time.Millisecond {
		t.Fatalf("decide %v, insert %v, read-back %v, refund %v", DecideBudget, store.InsertDeadline,
			store.ReadBackDeadline, RefundBudget)
	}
}

// TestAnEnclaveThatHangsUpDoesNotCancelTheAdmitOrTheInsert: the request's
// context ends while the owner admits and again while the store inserts;
// neither sees its context end (the admit keeps its budget's deadline, the
// insert none of the request's), so the hold is inserted under, not left.
func TestAnEnclaveThatHangsUpDoesNotCancelTheAdmitOrTheInsert(t *testing.T) {
	h, c := baseDecide(t)
	ctx, hangUp := context.WithCancel(context.Background())
	var admitLive, insertLive, insertDeadline atomic.Bool
	eol := endOfLifeOf(t, c)
	h.admitter.authorize = func(actx context.Context, a frontdoor.AuthorizeOf) frontdoor.Authorized {
		hangUp()
		time.Sleep(50 * time.Millisecond)
		admitLive.Store(actx.Err() == nil)
		return frontdoor.Authorized{Status: frontdoor.Admitted, Envelope: c.Admitted.Envelope, EndOfLife: eol}
	}
	h.rows.insert = func(ictx context.Context) store.InsertOutcome {
		time.Sleep(50 * time.Millisecond)
		_, has := ictx.Deadline()
		insertLive.Store(ictx.Err() == nil)
		insertDeadline.Store(has)
		return store.Inserted
	}
	rec := httptest.NewRecorder()
	h.edge.ServeHTTP(rec, request(c, ctx))
	if ctx.Err() == nil || !admitLive.Load() || !insertLive.Load() || insertDeadline.Load() || rec.Code != http.StatusOK {
		t.Errorf("hung up %v: admit live %v, insert live %v (deadline %v), %d", ctx.Err() != nil, admitLive.Load(),
			insertLive.Load(), insertDeadline.Load(), rec.Code)
	}
}

// TestAPassThatPythonRefusesEvictsTheKey (decision 2): a request the decide
// path passed after it read the key (the owner busy) that Python answers
// 401, 402, 403 or its pause 503 evicts the lookup hash and its workspace;
// Python's other 503s and its 409 evict nothing; and the enclave gets
// Python's bytes whole, the ones read to tell a pause apart included.
func TestAPassThatPythonRefusesEvictsTheKey(t *testing.T) {
	pause := errorBody(503, "workspace_paused", pauseMessage)
	for _, tc := range []struct {
		status int
		body   []byte
		evicts bool
	}{
		{401, errorBody(401, "invalid_api_key", "Invalid API key"), true},
		{402, errorBody(402, "payment_required", "x"), true},
		{403, errorBody(403, "forbidden", "x"), true},
		{503, pause, true},
		{503, errorBody(503, "service_unavailable", unavailableMessage), false},
		{409, errorBody(409, "conflict", "x"), false},
	} {
		h, c := baseDecide(t)
		h.admitter.authorize = func(context.Context, frontdoor.AuthorizeOf) frontdoor.Authorized {
			return frontdoor.Authorized{Status: frontdoor.Busy}
		}
		answer := answerWith(tc.status, tc.body)
		h.python.respond.Store(&answer)
		rec := h.send(t, c)
		h.cache.mu.Lock()
		evicted, workspaces := slices.Clone(h.cache.evicted), slices.Clone(h.cache.workspaces)
		h.cache.mu.Unlock()
		if rec.Code != tc.status || !bytes.Equal(rec.Body.Bytes(), tc.body) {
			t.Errorf("%d: the enclave got %d %s", tc.status, rec.Code, rec.Body.Bytes())
		}
		want := tc.evicts && slices.Equal(evicted, []string{c.Facts.Key.LookupHash}) &&
			slices.Equal(workspaces, []string{c.Facts.Key.WorkspaceID})
		if tc.evicts != want || (!tc.evicts && len(evicted)+len(workspaces) != 0) {
			t.Errorf("%d %s: evicted %v %v", tc.status, tc.body, evicted, workspaces)
		}
	}
}

// TestTheDecidePathIsDark: off (no Decide, the default), the edge asks no
// owner and inserts nothing for a request it would decide; Python answers
// it. On but with the decide row off, it passes too (decide.off).
func TestTheDecidePathIsDark(t *testing.T) {
	h, c := baseDecide(t)
	upstream, _ := url.Parse(h.python.server.URL)
	f := authorizeGoldens(t)
	off, err := New(Config{Token: f.InternalToken, Upstream: upstream, Boots: bootsOf(t, f, c),
		Clock: func() time.Time { return c.at() }, Transport: h.python.transport, Admitter: h.holder})
	if err != nil {
		t.Fatal(err)
	}
	if off.DecideOn() || off.decider != nil {
		t.Fatal("the default decides")
	}
	rec := httptest.NewRecorder()
	off.ServeHTTP(rec, request(c, context.Background()))
	asked, _, _ := h.admitter.seen()
	if rec.Code != http.StatusTeapot || len(asked) != 0 || len(h.rows.inserted()) != 0 {
		t.Fatalf("off: %d, %d admits", rec.Code, len(asked))
	}
	view := DecideView{On: map[DecideKey]bool{}, ReadAt: c.at(), MaxAge: decideStaleness * DecideEvery}
	h.edge.decider.decides.current.Store(&view)
	if rec := h.send(t, c); rec.Code != http.StatusTeapot || h.edge.DecideCounts().Passed[PassDecideOff] != 1 {
		t.Fatalf("decide row off: %d %+v", rec.Code, h.edge.DecideCounts())
	}
	if _, err := New(Config{Token: "t", Upstream: upstream, Boots: &boots{}, Decide: &DecideConfig{}}); err == nil {
		t.Fatal("a decide path with no snapshot or policy was made")
	}
}

// TestOnlyAdmitHandsOutADecision: the decision a Decided holds is named
// only in decide.go (shadowOnly), read through ForShadow only by the shadow
// (shadow_full.go), and no Decided is written unkeyed (which would set the
// field by position): the decide path, and the service and commands that
// run it, reach a decision only through Admit.
func TestOnlyAdmitHandsOutADecision(t *testing.T) {
	var files []string
	for _, pattern := range []string{"*.go", "../service/*.go", "../../cmd/*/*.go"} {
		matched, err := filepath.Glob(pattern)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, matched...)
	}
	fset := gotoken.NewFileSet()
	seen := map[string]bool{}
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		file, err := parser.ParseFile(fset, name, src, 0)
		if err != nil {
			t.Fatal(err)
		}
		seen["file:"+filepath.Dir(name)] = true
		ast.Inspect(file, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.SelectorExpr:
				switch x.Sel.Name {
				case "ForShadow":
					seen["ForShadow"] = true
					if name != "shadow_full.go" {
						t.Errorf("%s: %s calls ForShadow", fset.Position(x.Pos()), name)
					}
				case "shadowOnly":
					if name != "decide.go" {
						t.Errorf("%s: %s names shadowOnly", fset.Position(x.Pos()), name)
					}
				case "Admit":
					seen["Admit"] = true
				}
			case *ast.KeyValueExpr:
				if id, ok := x.Key.(*ast.Ident); ok && id.Name == "shadowOnly" && name != "decide.go" {
					t.Errorf("%s: %s sets shadowOnly", fset.Position(x.Pos()), name)
				}
			case *ast.CompositeLit:
				typeName := ""
				switch ty := x.Type.(type) {
				case *ast.Ident:
					typeName = ty.Name
				case *ast.SelectorExpr:
					typeName = ty.Sel.Name
				}
				if typeName != "Decided" {
					return true
				}
				for _, elt := range x.Elts {
					if _, keyed := elt.(*ast.KeyValueExpr); !keyed {
						t.Errorf("%s: %s writes a Decided unkeyed", fset.Position(x.Pos()), name)
						break
					}
				}
			}
			return true
		})
	}
	if !seen["ForShadow"] || !seen["Admit"] || !seen["file:../service"] || !seen["file:../../cmd/fastpath"] {
		t.Fatalf("the positive control: %v", seen)
	}
}

// alerts records the alerts an edge raises, by kind.
type alerts struct {
	mu   sync.Mutex
	what map[string][]string
}

func (a *alerts) raise(kind, subject, what string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.what == nil {
		a.what = map[string][]string{}
	}
	a.what[kind] = append(a.what[kind], subject+": "+what)
}

func (a *alerts) taken() map[string][]string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := a.what
	a.what = nil
	return out
}

// TestUnavailableAnswersAreCountedByCodeAndAlerted (G12): an Unknown insert
// answers the 503, counted by the code the store reports, so a clock ahead
// of Spanner's (FailedPrecondition) is told from a lost commit answer
// (DeadlineExceeded). Each period with one raises AlertUnavailable with the
// period's counts by code; SustainedPeriods of them with no 200 between
// raise AlertUnavailableSustained, and a period with a 200 and none resets
// the run. A period of 200s alone raises nothing.
func TestUnavailableAnswersAreCountedByCodeAndAlerted(t *testing.T) {
	h, c := baseDecide(t)
	var raised alerts
	h.edge.decider.cfg.Alert = raised.raise
	unknown := func(code codes.Code) {
		h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return store.Unknown }, code
		if rec := h.send(t, c); rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("an unknown insert: %d", rec.Code)
		}
	}
	h.edge.decider.flush()
	if got := raised.taken(); len(got) != 0 {
		t.Fatalf("an empty period alerts: %v", got)
	}
	h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return store.Inserted }, codes.OK
	if rec := h.send(t, c); rec.Code != http.StatusOK {
		t.Fatalf("an insert: %d", rec.Code)
	}
	h.edge.decider.flush()
	if got := raised.taken(); len(got) != 0 {
		t.Fatalf("a period of 200s alerts: %v", got)
	}
	unknown(codes.FailedPrecondition)
	unknown(codes.FailedPrecondition)
	unknown(codes.DeadlineExceeded)
	counts := h.edge.DecideCounts()
	if counts.Unavailable != 3 || !maps.Equal(counts.UnavailableByCode,
		map[string]int64{"failed_precondition": 2, "deadline_exceeded": 1}) {
		t.Fatalf("the counts: %+v", counts)
	}
	h.edge.decider.flush()
	if !strings.Contains(h.logs.all(), `fastpath edge decide: answered=0 read_back=0 unavailable=3 refunded=0 `+
		`refund_failed=0 unavailable_by_code={"deadline_exceeded":1,"failed_precondition":2} passed={} slow={}`) {
		t.Fatalf("the period's line: %s", h.logs.all())
	}
	got := raised.taken()
	if len(got) != 1 || len(got[AlertUnavailable]) != 1 ||
		!strings.Contains(got[AlertUnavailable][0], `edge decide: the edge answered 3 authorizes unavailable`) ||
		!strings.Contains(got[AlertUnavailable][0], `{"deadline_exceeded":1,"failed_precondition":2}`) {
		t.Fatalf("the first period's alerts: %v", got)
	}
	// Two more periods running: the third is sustained.
	unknown(codes.FailedPrecondition)
	h.edge.decider.flush()
	if got := raised.taken(); len(got) != 1 || len(got[AlertUnavailable]) != 1 {
		t.Fatalf("the second period's alerts: %v", got)
	}
	unknown(codes.FailedPrecondition)
	h.edge.decider.flush()
	got = raised.taken()
	if len(got) != 2 || len(got[AlertUnavailable]) != 1 || len(got[AlertUnavailableSustained]) != 1 ||
		!strings.Contains(got[AlertUnavailableSustained][0], `in 3 periods with no 200 between them`) ||
		!strings.Contains(got[AlertUnavailableSustained][0], `{"failed_precondition":1}`) {
		t.Fatalf("the third period's alerts: %v", got)
	}
	// A period with a 200 and no unavailable answer resets the run: the
	// next one is not sustained.
	h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return store.Inserted }, codes.OK
	h.send(t, c)
	h.edge.decider.flush()
	unknown(codes.Unavailable)
	h.edge.decider.flush()
	if got := raised.taken(); len(got) != 1 || len(got[AlertUnavailable]) != 1 {
		t.Fatalf("after a quiet period: %v", got)
	}
}

// TestSlowPassesAreCountedAndAlerted (decision 12, G12): a pass that spent
// more than SlowAtEdge at the edge before it was forwarded is counted
// at_edge, and one whose answer from Python, or the proxy's failure to
// reach it, came more than SlowInAll after its arrival is counted in_all;
// a period with either raises AlertSlowPass. At the contract's thresholds
// the same passes count neither.
func TestSlowPassesAreCountedAndAlerted(t *testing.T) {
	h, c := baseDecide(t)
	var raised alerts
	h.edge.decider.cfg.Alert = raised.raise
	h.rows.insert = func(context.Context) store.InsertOutcome { return store.Failed }
	if rec := h.send(t, c); rec.Code != http.StatusTeapot {
		t.Fatalf("a failed insert: %d", rec.Code)
	}
	h.edge.decider.flush()
	if c := h.edge.DecideCounts(); len(c.Slow) != 0 || len(raised.taken()) != 0 {
		t.Fatalf("a fast pass is slow: %+v", c)
	}
	h.edge.decider.slowAtEdge = 0
	h.send(t, c)
	if got := h.edge.DecideCounts().Slow; !maps.Equal(got, map[string]int64{"at_edge": 1}) {
		t.Fatalf("slow at the edge: %v", got)
	}
	h.edge.decider.slowAtEdge, h.edge.decider.slowInAll = SlowAtEdge, 0
	h.send(t, c)
	// Python not reached: the proxy's failure is the pass's end.
	h.python.respond.Store(func() *func(http.ResponseWriter) {
		f := func(http.ResponseWriter) { panic(http.ErrAbortHandler) }
		return &f
	}())
	if rec := h.send(t, c); rec.Code != http.StatusBadGateway {
		t.Fatalf("Python not reached: %d", rec.Code)
	}
	if got := h.edge.DecideCounts().Slow; !maps.Equal(got, map[string]int64{"at_edge": 1, "in_all": 2}) {
		t.Fatalf("slow in all: %v", got)
	}
	h.edge.decider.flush()
	if !strings.Contains(h.logs.all(), `passed={"insert.failed":3} slow={"at_edge":1,"in_all":2}`) {
		t.Fatalf("the period's line: %s", h.logs.all())
	}
	got := raised.taken()
	if len(got) != 1 || len(got[AlertSlowPass]) != 1 ||
		!strings.Contains(got[AlertSlowPass][0], `{"at_edge":1,"in_all":2}`) {
		t.Fatalf("the alerts: %v", got)
	}
}

// TestTheDecideLineCountsItsPeriodAlone: each period's line carries that
// period's counts, totals not carried over, so a span's lines add up to its
// counts; DecideCounts keeps the totals.
func TestTheDecideLineCountsItsPeriodAlone(t *testing.T) {
	h, c := baseDecide(t)
	h.send(t, c)
	h.edge.decider.flush()
	h.edge.decider.flush()
	lines := h.logs.all()
	if !strings.Contains(lines, "fastpath edge decide: answered=1 ") ||
		!strings.Contains(lines, "fastpath edge decide: answered=0 ") || h.edge.DecideCounts().Answered != 1 {
		t.Fatalf("the periods' lines: %s", lines)
	}
}

func TestCodeNames(t *testing.T) {
	for code, want := range map[codes.Code]string{codes.OK: "ok", codes.FailedPrecondition: "failed_precondition",
		codes.DeadlineExceeded: "deadline_exceeded", codes.Unavailable: "unavailable", codes.Code(99): "code(99)"} {
		if got := codeName(code); got != want {
			t.Errorf("%v: %q, want %q", code, got, want)
		}
	}
}

// TestSparseUnavailableAnswersAreSustained: a fault that turns every decided
// request into the 503, under traffic too sparse for every period to see
// one (a request every few minutes a node), is sustained all the same: the
// periods that decided nothing between them neither add to the run nor end
// it.
func TestSparseUnavailableAnswersAreSustained(t *testing.T) {
	h, c := baseDecide(t)
	var raised alerts
	h.edge.decider.cfg.Alert = raised.raise
	h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return store.Unknown },
		codes.FailedPrecondition
	sustained := 0
	for range SustainedPeriods {
		if rec := h.send(t, c); rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("an unknown insert: %d", rec.Code)
		}
		h.edge.decider.flush()
		sustained += len(raised.taken()[AlertUnavailableSustained])
		// Two periods that decided nothing.
		h.edge.decider.flush()
		h.edge.decider.flush()
		if got := raised.taken(); len(got) != 0 {
			t.Fatalf("an empty period alerts: %v", got)
		}
	}
	if sustained != 1 {
		t.Fatalf("%d sustained alerts over %d sparse unavailable periods, want 1", sustained, SustainedPeriods)
	}
}

// TestAnEnforcingWindowedKeyIsNeverAdmittedUnread (docs/design/
// fast-admission-e2-key-windows.md section 4.2): under every mode, enforce
// included, a key whose windows Python's authorize enforces passes
// (key_window.unread) without asking the owner when the decide path has no
// window reader; an alert-only key the mode covers is answered as an
// uncapped one, and under off it passes with its reason.
func TestAnEnforcingWindowedKeyIsNeverAdmittedUnread(t *testing.T) {
	f := authorizeGoldens(t)
	base := authorizeCaseNamed(t, "answer:app_categories_empty")
	enforcing := map[string]any{"limit_daily_microdollars": 5000}
	alertOnly := map[string]any{"limit_daily_microdollars": 5000, "budget_alert_only": true}
	for _, v := range []struct {
		name   string
		fields map[string]any
		mode   string
		pass   string
	}{
		{"enforcing, enforce", enforcing, routingsnapshot.KeyWindowsEnforce, keywindow.PassUnread},
		{"enforcing, alert", enforcing, routingsnapshot.KeyWindowsAlert, PassKeyWindow},
		{"enforcing, absent", enforcing, "", PassKeyWindow},
		{"alert only, absent", alertOnly, "", PassKeyWindowAlert},
		{"alert only, alert", alertOnly, routingsnapshot.KeyWindowsAlert, ""},
		{"alert only, enforce", alertOnly, routingsnapshot.KeyWindowsEnforce, ""},
	} {
		c := base
		body := keyBodyWith(t, c, v.fields)
		c.Key.KeyBody = &body
		h := newDecideEdge(t, f, c, nil)
		if v.mode != "" {
			mode := v.mode
			h.policy.FastpathPilotKeyWindows = &mode
		}
		rec := h.send(t, c)
		admits, _, _ := h.admitter.seen()
		counts := h.edge.DecideCounts()
		if v.pass != "" {
			if rec.Code != http.StatusTeapot || len(admits) != 0 || counts.Passed[v.pass] != 1 {
				t.Errorf("%s: %d, %d admits, passed %v; want a pass %q", v.name, rec.Code, len(admits), counts.Passed,
					v.pass)
			}
			continue
		}
		if rec.Code != http.StatusOK || len(admits) != 1 || counts.Answered != 1 {
			t.Errorf("%s: %d, %d admits, passed %v; want answered", v.name, rec.Code, len(admits), counts.Passed)
		}
	}
}

// fakeWindows is a ShadowWindowReader: what it answers, and each read's key,
// deadline and, for a read at a timestamp, that timestamp. readAt answers a
// read at a timestamp; nil answers it as read does.
type fakeWindows struct {
	mu     sync.Mutex
	read   func(ctx context.Context) ([]keywindow.Row, error)
	readAt func(ctx context.Context, at time.Time) ([]keywindow.Row, error)
	calls  []windowCall
}

type windowCall struct {
	keyHash  string
	deadline time.Time
	at       time.Time // zero for a strong read
}

func (f *fakeWindows) ReadKeyWindows(ctx context.Context, keyHash string) ([]keywindow.Row, error) {
	deadline, _ := ctx.Deadline()
	f.mu.Lock()
	f.calls = append(f.calls, windowCall{keyHash: keyHash, deadline: deadline})
	read := f.read
	f.mu.Unlock()
	return read(ctx)
}

func (f *fakeWindows) ReadKeyWindowsAt(ctx context.Context, keyHash string, at time.Time) ([]keywindow.Row, error) {
	deadline, _ := ctx.Deadline()
	f.mu.Lock()
	f.calls = append(f.calls, windowCall{keyHash: keyHash, deadline: deadline, at: at})
	read, readAt := f.read, f.readAt
	f.mu.Unlock()
	if readAt != nil {
		return readAt(ctx, at)
	}
	return read(ctx)
}

func (f *fakeWindows) seen() []windowCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.calls)
}

// dailyRows are a key's tr_key_limit rows for a daily limit at at: shards
// rows, the first holding used of the day and every one the limit mirrored.
func dailyRows(at time.Time, limit, used int64, shards int) []keywindow.Row {
	day, _, _ := keywindow.Floors(at)
	rows := make([]keywindow.Row, shards)
	for i := range rows {
		rows[i] = keywindow.Row{Shard: int64(i), DayUsage: spanner.NullInt64{Valid: true},
			DayStart: spanner.NullTime{Time: day, Valid: true}, DayLimit: spanner.NullInt64{Int64: limit, Valid: true}}
	}
	rows[0].DayUsage.Int64 = used
	return rows
}

// windowedDecideEdge is a decide edge over the plain answered case with its
// key's body changed by fields, the policy's mode set (absent for ""), and
// reads answered by read.
func windowedDecideEdge(t *testing.T, fields map[string]any, mode string,
	read func(context.Context) ([]keywindow.Row, error)) (*decideEdge, authorizeCase, *fakeWindows) {
	t.Helper()
	return windowedDecideEdgeOf(t, "answer:app_categories_empty", fields, mode, read)
}

// windowedDecideEdgeOf is windowedDecideEdge over the golden case named.
func windowedDecideEdgeOf(t *testing.T, name string, fields map[string]any, mode string,
	read func(context.Context) ([]keywindow.Row, error)) (*decideEdge, authorizeCase, *fakeWindows) {
	t.Helper()
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, name)
	body := keyBodyWith(t, c, fields)
	c.Key.KeyBody = &body
	h := newDecideEdge(t, f, c, nil)
	if mode != "" {
		h.policy.FastpathPilotKeyWindows = &mode
	}
	windows := &fakeWindows{read: read}
	h.edge.decider.cfg.Windows = windows
	return h, c, windows
}

// TestTheWindowCheckDecidesAnEnforcingKey (docs/design/
// fast-admission-e2-key-windows.md sections 4.2 to 4.4): under enforce the
// decide path reads the key's rows once, by its key hash, within
// WindowReadBudget, and admits only on an allowed verdict, whose 200 counts
// the read among its Spanner calls; every other verdict passes with its
// key_window.* reason before the owner is asked. Rows the cached shard count
// does not expect (the key's shards grew or shrank since) pass. An
// alert-only key, and an enforcing key under alert, are never read.
func TestTheWindowCheckDecidesAnEnforcingKey(t *testing.T) {
	enforcing := map[string]any{"limit_daily_microdollars": 1_000_000, "usage_shard_count": 2}
	failed := errors.New("synthetic read failure")
	for _, v := range []struct {
		name string
		rows func(at time.Time) ([]keywindow.Row, error)
		pass string
	}{
		{"headroom", func(at time.Time) ([]keywindow.Row, error) { return dailyRows(at, 1_000_000, 0, 2), nil }, ""},
		{"spent", func(at time.Time) ([]keywindow.Row, error) {
			return dailyRows(at, 1_000_000, 1_000_000, 2), nil
		}, keywindow.PassExceeded},
		{"mirror drifted", func(at time.Time) ([]keywindow.Row, error) { return dailyRows(at, 2_000_000, 0, 2), nil },
			keywindow.PassConfigDrift},
		{"no rows", func(time.Time) ([]keywindow.Row, error) { return nil, nil }, keywindow.PassRows},
		{"a shard missing", func(at time.Time) ([]keywindow.Row, error) {
			return dailyRows(at, 1_000_000, 0, 2)[:1], nil
		}, keywindow.PassRows},
		{"read failed", func(time.Time) ([]keywindow.Row, error) { return nil, failed }, keywindow.PassUnread},
		{"the key's shards grew", func(at time.Time) ([]keywindow.Row, error) {
			return dailyRows(at, 1_000_000, 0, 3), nil
		}, keywindow.PassRows},
	} {
		var at time.Time
		h, c, windows := windowedDecideEdge(t, enforcing, routingsnapshot.KeyWindowsEnforce,
			func(context.Context) ([]keywindow.Row, error) { return v.rows(at) })
		at = c.at()
		before := time.Now()
		rec := h.send(t, c)
		admits, _, _ := h.admitter.seen()
		calls := windows.seen()
		if len(calls) != 1 || calls[0].keyHash != c.Facts.Key.Hash ||
			calls[0].deadline.After(time.Now().Add(WindowReadBudget)) || calls[0].deadline.Before(before) {
			t.Errorf("%s: reads %+v", v.name, calls)
		}
		counts := h.edge.DecideCounts()
		if counts.Windows["reads"] != 1 || counts.Windows[windowOutcome(keywindow.Verdict{Pass: v.pass,
			Decided: v.pass == "", Allowed: v.pass == ""})] != 1 {
			t.Errorf("%s: window counts %v", v.name, counts.Windows)
		}
		if v.pass != "" {
			if rec.Code != http.StatusTeapot || len(admits) != 0 || counts.Passed[v.pass] != 1 {
				t.Errorf("%s: %d, %d admits, passed %v; want %q", v.name, rec.Code, len(admits), counts.Passed, v.pass)
			}
			continue
		}
		if rec.Code != http.StatusOK || len(admits) != 1 || counts.Answered != 1 ||
			!bytes.Contains(rec.Body.Bytes(), []byte(`"spanner_rpcs":2,`)) {
			t.Errorf("%s: %d, %d admits, passed %v: %s", v.name, rec.Code, len(admits), counts.Passed, rec.Body.Bytes())
		}
	}
	for _, v := range []struct {
		name   string
		fields map[string]any
		mode   string
	}{
		{"alert only, enforce", map[string]any{"limit_daily_microdollars": 5, "budget_alert_only": true},
			routingsnapshot.KeyWindowsEnforce},
		{"enforcing, alert", enforcing, routingsnapshot.KeyWindowsAlert},
		{"enforcing, absent", enforcing, ""},
	} {
		h, c, windows := windowedDecideEdge(t, v.fields, v.mode, func(context.Context) ([]keywindow.Row, error) {
			return nil, failed
		})
		h.send(t, c)
		if calls := windows.seen(); len(calls) != 0 {
			t.Errorf("%s: read %d times", v.name, len(calls))
		}
	}
}

// TestTheWindowCheckIsAgainstTheEstimate (section 4.2): Python checks the
// model estimate, never the reservation the lease holds, which the output
// hold cap makes smaller. With what the day has left between the two, the
// key passes key_window.exceeded, though the reservation would fit; with the
// estimate's room it is answered (the positive control).
func TestTheWindowCheckIsAgainstTheEstimate(t *testing.T) {
	const name = "answer:estimate:hold_cap:deepseek/deepseek-v4-flash:1000+131072"
	const limit = int64(10_000_000)
	fields := map[string]any{"limit_daily_microdollars": limit}
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, name)
	body := keyBodyWith(t, c, fields)
	c.Key.KeyBody = &body
	in, _ := caseInputs(t, f, c)
	at := c.at()
	mode := routingsnapshot.KeyWindowsEnforce
	in.Policy.FastpathPilotKeyWindows = &mode
	dec := Decide(in).ForShadow()
	if dec == nil || dec.Windows == nil || dec.Reservation >= dec.Estimate {
		t.Fatalf("the case must hold a reservation below its estimate: %+v", dec)
	}
	// What the day has left: the reservation and half the way to the
	// estimate.
	left := dec.Reservation + (dec.Estimate-dec.Reservation)/2
	for _, v := range []struct {
		left int64
		pass string
	}{
		{left, keywindow.PassExceeded},
		{dec.Estimate, ""},
	} {
		h, c, _ := windowedDecideEdgeOf(t, name, fields, routingsnapshot.KeyWindowsEnforce,
			func(context.Context) ([]keywindow.Row, error) { return dailyRows(at, limit, limit-v.left, 1), nil })
		rec := h.send(t, c)
		admits, _, _ := h.admitter.seen()
		counts := h.edge.DecideCounts()
		if v.pass != "" {
			if rec.Code != http.StatusTeapot || len(admits) != 0 || counts.Passed[v.pass] != 1 {
				t.Errorf("%d left: %d, %d admits, passed %v; want %q", v.left, rec.Code, len(admits), counts.Passed,
					v.pass)
			}
			continue
		}
		if rec.Code != http.StatusOK || len(admits) != 1 || admits[0].Estimate != dec.Reservation {
			t.Errorf("%d left: %d, %d admits, passed %v", v.left, rec.Code, len(admits), counts.Passed)
		}
	}
}

// TestASlowWindowReadPassesWithinItsBudget: a read that does not answer ends
// at WindowReadBudget and passes (key_window.unread), well inside the decide
// budget, without asking the owner.
func TestASlowWindowReadPassesWithinItsBudget(t *testing.T) {
	h, c, windows := windowedDecideEdge(t, map[string]any{"limit_daily_microdollars": 5},
		routingsnapshot.KeyWindowsEnforce, func(ctx context.Context) ([]keywindow.Row, error) {
			<-ctx.Done()
			return nil, ctx.Err()
		})
	start := time.Now()
	rec := h.send(t, c)
	took := time.Since(start)
	admits, _, _ := h.admitter.seen()
	if rec.Code != http.StatusTeapot || len(admits) != 0 || len(windows.seen()) != 1 ||
		h.edge.DecideCounts().Passed[keywindow.PassUnread] != 1 {
		t.Fatalf("%d, %d admits, passed %v", rec.Code, len(admits), h.edge.DecideCounts().Passed)
	}
	if took < WindowReadBudget || took > DecideBudget {
		t.Fatalf("the pass came %v after the request", took)
	}
	if WindowReadBudget >= DecideBudget {
		t.Fatal("the window read's budget must end inside the decide budget")
	}
}

// TestUnreadWindowChecksAreAlerted: a period whose window reads failed past
// WindowUnreadShare, at least WindowUnreadMin of them, raises
// AlertWindowUnread once; fewer, or none, raise nothing.
func TestUnreadWindowChecksAreAlerted(t *testing.T) {
	for _, v := range []struct {
		reads, unread int64
		alert         bool
	}{
		{3, 3, true},
		{200, 3, true},
		{400, 3, false},
		{2, 2, false},
		{100, 0, false},
	} {
		h, _ := baseDecide(t)
		a := &alerts{}
		h.edge.decider.cfg.Alert = a.raise
		h.edge.decider.count(func(t *decideTally) {
			t.windows["reads"], t.windows["unread"] = v.reads, v.unread
		})
		h.edge.decider.flush()
		if got := len(a.taken()[AlertWindowUnread]); (got == 1) != v.alert || got > 1 {
			t.Errorf("%d unread of %d: %d alerts", v.unread, v.reads, got)
		}
	}
}
