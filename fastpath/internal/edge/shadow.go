package edge

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// The shadow comparator (docs/design/fast-admission-enclave-direct.md, E2,
// "Measured"): for each authorize the edge passes to Python while it holds a
// routing snapshot, Go evaluates the same request off the request path, and
// once Python has answered, the two answers are compared and counted. Python
// decides; nothing here reaches the bytes the enclave receives. The logs
// carry counts, field names, reasons with every request value redacted, and
// the snapshot's digest: never a body, a model string, a key or a message.

// The comparison's classes.
const (
	ClassAgree    = "agree"
	ClassDiffer   = "differ"
	ClassPassed   = "passed_to_python"
	ClassAffinity = "affinity"
	// ClassAffinityWarm: with fallbacks off, Python's one candidate is a
	// session's warm endpoint in place of Go's first; never agree.
	ClassAffinityWarm = "affinity_warm"
	ClassNoSnapshot   = "no_snapshot"
)

const (
	// maxShadowRequest and maxShadowResponse bound what the comparator
	// keeps of a request and of Python's answer: an authorize body is a
	// few hundred bytes of metadata, its answer a few kilobytes.
	maxShadowRequest  = 1 << 20
	maxShadowResponse = 4 << 20
	// shadowQueue bounds the comparisons waiting; past it one is dropped,
	// never waited for.
	shadowQueue = 256
	// maxShadowReasons bounds the distinct reasons a tally keeps.
	maxShadowReasons = 64
	// maxDifferLogs bounds the per-comparison lines a period logs.
	maxDifferLogs = 20
	// maxMarkupBuckets bounds estimate_high's histogram; past it, "other".
	maxMarkupBuckets = 256
	// maxMarkupBPS is the largest OAuth app markup (oauth_apps.py,
	// _validated_markup_basis_points).
	maxMarkupBPS = 30_000
	// maxShadowBytes bounds the copies the comparator holds at once, in
	// flight and queued: past it a request is not copied, and is counted
	// as passed.
	maxShadowBytes = 64 << 20
)

// authorizeRoutes are the paths Python answers authorize on.
var authorizeRoutes = map[string]bool{
	"/internal/gateway/authorize":    true,
	"/v1/internal/gateway/authorize": true,
}

// routingRefusals are the error types the candidate pipeline raises; any
// other, or any status but 400, is Python refusing for something besides
// routing (the key, the workspace, money, idempotency).
var routingRefusals = map[string]bool{
	"bad_request": true, "model_not_supported": true, "provider_not_supported": true, "context_length_exceeded": true,
}

// The pilot's idempotency prefix (partner_billing.py), whose requests take
// partner pricing.
const partnerIdempotency = "partner:parasail-liberty-2.0:"

type shadowKey struct{}

// shadowCall is one authorize the comparator will compare: the snapshot held
// when it arrived and the instant, the edge's boot verdict, and what of the
// request's headers puts it outside the cohort. It keeps no header value.
type shadowCall struct {
	held    *held
	at      time.Time
	verdict Verdict
	// videoProviders: X-Quill-Video-Allowed-Providers came (video
	// constraints); partnerKey: the Idempotency-Key header names the
	// partner's internal prefix.
	videoProviders, partnerKey bool
	// body is the request's when the edge read it, bodyHeld the bytes of
	// the comparator's budget it holds; else tap holds what the proxy sent.
	body     []byte
	bodyHeld int64
	tap      *tap
}

// finish gives back the request's share of the comparator's budget.
func (call *shadowCall) finish() int64 {
	n := call.bodyHeld
	call.bodyHeld = 0
	return n + call.tap.finish()
}

// byteBudget is a comparator's bound on the bytes its copies hold (shadow
// and fullShadow).
type byteBudget interface {
	reserve(n int64) bool
	release(n int64)
}

// tap passes a body through unchanged, keeping a copy within a bound and
// within the comparator's byte budget. Each Read passes on what the body
// gave it at once: the copy never holds the bytes back.
type tap struct {
	rc      io.ReadCloser
	sh      byteBudget
	limit   int
	length  int64
	onClose func(*tap)

	mu       sync.Mutex
	buf      []byte
	held     int64
	over     bool
	spent    bool
	finished bool
	eof      bool
	closed   bool
}

func (t *tap) Read(p []byte) (int, error) {
	n, err := t.rc.Read(p)
	t.mu.Lock()
	if n > 0 && !t.over {
		switch {
		case len(t.buf)+n > t.limit:
			t.drop()
		case !t.sh.reserve(int64(n)):
			t.drop()
			t.spent = true
		default:
			t.buf = append(t.buf, p[:n]...)
			t.held += int64(n)
		}
	}
	if err == io.EOF {
		t.eof = true
	}
	t.mu.Unlock()
	return n, err
}

func (t *tap) Close() error {
	err := t.rc.Close()
	t.mu.Lock()
	first := !t.closed
	t.closed = true
	t.mu.Unlock()
	if first && t.onClose != nil {
		t.onClose(t)
	}
	return err
}

// drop stops the copy and gives back its share of the budget; mu is held.
func (t *tap) drop() {
	t.sh.release(t.held)
	t.over, t.buf, t.held = true, nil, 0
}

// finish ends the copy, whatever still passes, and says how much of the
// budget it held, for its caller to give back.
func (t *tap) finish() int64 {
	if t == nil {
		return 0
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	n := t.held
	t.over, t.buf, t.held, t.finished = true, nil, 0, true
	return n
}

// bytes are what passed, when all of it passed within the bound; spent says
// the budget, not the bound, stopped the copy.
func (t *tap) bytes() (b []byte, complete, spent bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.over || !(t.eof || (t.length >= 0 && int64(len(t.buf)) == t.length)) {
		return nil, false, t.spent
	}
	return t.buf, true, false
}

// comparison is a call with Python's answer: its status, its encoding, its
// bytes (complete only when read through within the bound), and when it
// ended.
type comparison struct {
	call     *shadowCall
	status   int
	encoding string
	response []byte
	complete bool
	spent    bool
	answer   *tap
	done     time.Time
	// passed, when set, is why the request is not compared, known before
	// Python answered (it was not reached, or the body is past the bound).
	passed string
}

// result is a comparison's class, the fields that differ, the reason a
// request went to Python, and how the inputs the edge cannot know were met.
type result struct {
	class  string
	fields []string
	reason string
	// byok and stageD are "determined" (no value of the unknown inputs
	// changes Go's answer), "consistent" (some value gives Python's), or
	// "" (not compared).
	byok, stageD string
	// markup is set for an estimate_high: the smallest integer basis points
	// in 1..30000 that, as an OAuth app's markup (gateway.py adds
	// estimate*bps//10000), make Go's estimate Python's, or "none". It is
	// read, not trusted: real markups are a few spikes, a pricing bug a
	// smear.
	markup string
	// warmEstimate is "checked" or "unchecked" for affinity_warm.
	warmEstimate string
	goOutcome    string
	pythonStatus int
}

func passed(reason string) result { return result{class: ClassPassed, reason: reason} }

// tally is one snapshot's counts for a period.
type tally struct {
	classes map[string]int
	fields  map[string]int
	reasons map[string]int
	byok    map[string]int
	stageD  map[string]int
	// markup is estimate_high's histogram by the smallest app markup that
	// explains it; warm counts affinity_warm's estimates checked or not.
	markup map[string]int
	warm   map[string]int
}

func newTally() *tally {
	return &tally{classes: map[string]int{}, fields: map[string]int{}, reasons: map[string]int{},
		byok: map[string]int{}, stageD: map[string]int{}, markup: map[string]int{}, warm: map[string]int{}}
}

// shadow runs the comparisons on one goroutine and logs their counts.
type shadow struct {
	logf  func(string, ...any)
	clock func() time.Time
	every time.Duration
	queue chan comparison

	// noSnapshot and dropped are counted on the request path, atomically;
	// the rest under mu, by the comparator's goroutine.
	noSnapshot, dropped atomic.Int64
	// bytes are the budget's bytes held by copies; budget is its bound.
	bytes      atomic.Int64
	budget     int64
	mu         sync.Mutex
	tallies    map[string]*tally
	differLogs int
}

func newShadow(logf func(string, ...any), clock func() time.Time, every time.Duration) *shadow {
	if every <= 0 {
		every = time.Minute
	}
	return &shadow{logf: logf, clock: clock, every: every, queue: make(chan comparison, shadowQueue),
		tallies: map[string]*tally{}, budget: maxShadowBytes}
}

// reserve takes n bytes of the budget, or none and false when they are not
// there.
func (sh *shadow) reserve(n int64) bool {
	if sh.bytes.Add(n) > sh.budget {
		sh.bytes.Add(-n)
		return false
	}
	return true
}

func (sh *shadow) release(n int64) {
	if n != 0 {
		sh.bytes.Add(-n)
	}
}

// finish gives back what a comparison held of the budget.
func (sh *shadow) finish(c comparison) {
	sh.release(c.call.finish() + c.answer.finish())
}

// begin is the call for an authorize the edge is about to pass on, nil when
// there is none to compare; a request with no snapshot held is counted here.
func (e *Edge) begin(r *http.Request) *shadowCall {
	if e.shadow == nil || r.Method != http.MethodPost || !authorizeRoutes[r.URL.Path] {
		return nil
	}
	h := e.poller.snapshot()
	if h == nil {
		e.shadow.noSnapshot.Add(1)
		return nil
	}
	return &shadowCall{held: h, at: e.cfg.Clock(),
		videoProviders: len(r.Header.Values("X-Quill-Video-Allowed-Providers")) > 0,
		partnerKey:     strings.Contains(r.Header.Get("Idempotency-Key"), partnerIdempotency)}
}

// attach puts the call on the request the proxy sends; a body the edge has
// not read is tapped as it passes. A body the edge read that is past the
// comparator's bound, or past what is left of its budget, is not kept: the
// request is counted as passed at once, and r goes on as it came.
func (sh *shadow) attach(call *shadowCall, r *http.Request, verdict Verdict, body []byte) *http.Request {
	call.verdict = verdict
	switch {
	case body != nil && len(body) > maxShadowRequest:
		sh.submit(comparison{call: call, passed: "the request body is over the comparator's bound"})
		return r
	case body != nil && !sh.reserve(int64(len(body))):
		sh.submit(comparison{call: call, passed: "the comparator's byte budget is spent"})
		return r
	case body != nil:
		call.body, call.bodyHeld = body, int64(len(body))
	case r.Body == nil || r.Body == http.NoBody:
		call.body = []byte{}
	}
	out := r.WithContext(context.WithValue(r.Context(), shadowKey{}, call))
	if call.body == nil {
		call.tap = &tap{rc: r.Body, sh: sh, limit: maxShadowRequest, length: r.ContentLength}
		out.Body = call.tap
	}
	return out
}

// tapResponse keeps a copy of Python's answer as it passes to the enclave;
// once the proxy has closed it, the comparison is queued. The answer's
// status, headers and bytes are untouched.
func (sh *shadow) tapResponse(resp *http.Response, call *shadowCall) {
	status, encoding := resp.StatusCode, resp.Header.Get("Content-Encoding")
	resp.Body = &tap{rc: resp.Body, sh: sh, limit: maxShadowResponse, length: resp.ContentLength, onClose: func(t *tap) {
		bytes, complete, spent := t.bytes()
		sh.submit(comparison{call: call, status: status, encoding: encoding, response: bytes, complete: complete,
			spent: spent, answer: t, done: sh.clock()})
	}}
}

// submit queues a comparison, or drops it when the queue is full: the proxy
// never waits for the comparator.
func (sh *shadow) submit(c comparison) {
	select {
	case sh.queue <- c:
	default:
		sh.dropped.Add(1)
		sh.finish(c)
	}
}

// run compares what is queued and logs the counts each period, until ctx
// ends.
func (sh *shadow) run(ctx context.Context) {
	ticker := time.NewTicker(sh.every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			sh.flush()
			return
		case c := <-sh.queue:
			sh.process(c)
		case <-ticker.C:
			sh.flush()
		}
	}
}

// process compares one comparison, counts it, and gives back its copies'
// share of the budget.
func (sh *shadow) process(c comparison) {
	sh.record(c.call.held.claims.SHA256, sh.safeCompare(c))
	sh.finish(c)
}

// safeCompare is compare, a panic in it counted rather than ending the node:
// the comparator is an observation, and the edge it runs beside serves on.
func (sh *shadow) safeCompare(c comparison) (r result) {
	defer func() {
		if recover() != nil {
			r = passed("the comparator failed")
		}
	}()
	return sh.compare(c)
}

func (sh *shadow) record(digest string, r result) {
	sh.mu.Lock()
	defer sh.mu.Unlock()
	t := sh.tallies[digest]
	if t == nil {
		t = newTally()
		sh.tallies[digest] = t
	}
	t.classes[r.class]++
	for _, f := range r.fields {
		t.fields[f]++
	}
	if r.reason != "" {
		bounded(t.reasons, r.reason)
	}
	if r.byok != "" {
		t.byok[r.byok]++
	}
	if r.stageD != "" {
		t.stageD[r.stageD]++
	}
	if r.markup != "" {
		boundedTo(t.markup, r.markup, maxMarkupBuckets)
	}
	if r.warmEstimate != "" {
		t.warm[r.warmEstimate]++
	}
	if len(r.fields) > 0 && sh.differLogs < maxDifferLogs {
		sh.differLogs++
		sh.logf("fastpath edge shadow differs: snapshot=%s class=%s fields=%s go=%s python_status=%d byok=%s stage_d=%s "+
			"markup_bps=%s", digest, r.class, strings.Join(r.fields, ","), r.goOutcome, r.pythonStatus, orNone(r.byok),
			orNone(r.stageD), orNone(r.markup))
	}
}

func bounded(m map[string]int, key string) { boundedTo(m, key, maxShadowReasons) }

func boundedTo(m map[string]int, key string, most int) {
	if _, ok := m[key]; !ok && len(m) >= most {
		key = "other"
	}
	m[key]++
}

func orNone(s string) string {
	if s == "" {
		return "none"
	}
	return s
}

// flush logs and resets the period's counts: one line a snapshot, and one
// for the authorizes with no snapshot or no room in the queue.
func (sh *shadow) flush() {
	sh.mu.Lock()
	defer sh.mu.Unlock()
	digests := make([]string, 0, len(sh.tallies))
	for d := range sh.tallies {
		digests = append(digests, d)
	}
	sort.Strings(digests)
	for _, d := range digests {
		t := sh.tallies[d]
		sh.logf("fastpath edge shadow: snapshot=%s agree=%d differ=%d affinity=%d affinity_warm=%d passed_to_python=%d "+
			"differ_fields=%s estimate_high_markup_bps=%s affinity_warm_estimate=%s passed_reasons=%s byok=%s stage_d=%s",
			d, t.classes[ClassAgree], t.classes[ClassDiffer], t.classes[ClassAffinity], t.classes[ClassAffinityWarm],
			t.classes[ClassPassed], jsonOf(t.fields), jsonOf(t.markup), jsonOf(t.warm), jsonOf(t.reasons),
			jsonOf(t.byok), jsonOf(t.stageD))
	}
	if noSnapshot, dropped := sh.noSnapshot.Swap(0), sh.dropped.Swap(0); noSnapshot > 0 || dropped > 0 {
		sh.logf("fastpath edge shadow: no_snapshot=%d dropped=%d", noSnapshot, dropped)
	}
	sh.tallies, sh.differLogs = map[string]*tally{}, 0
}

// jsonOf is a count map as a JSON object, its keys sorted and unescaped.
func jsonOf[V int | int64](m map[string]V) string {
	var b bytes.Buffer
	encoder := json.NewEncoder(&b)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(m)
	return strings.TrimSuffix(b.String(), "\n")
}

// compare is one comparison: Go's answer from the snapshot held when the
// request arrived, against Python's.
func (sh *shadow) compare(c comparison) result {
	call := c.call
	if c.passed != "" {
		return passed(c.passed)
	}
	if c.spent {
		return passed("the comparator's byte budget is spent")
	}
	if !c.complete {
		return passed("python's answer was not read through within the comparator's bound")
	}
	body := call.body
	if body == nil {
		var ok, spent bool
		if body, ok, spent = call.tap.bytes(); spent {
			return passed("the comparator's byte budget is spent")
		} else if !ok {
			return passed("the request body was not read through within the comparator's bound")
		}
	}
	if len(body) > maxShadowRequest {
		return passed("the request body is over the comparator's bound")
	}
	req, affinityKey, why := requestOf(body, call)
	if why != "" {
		return passed(why)
	}
	h := call.held
	s := h.snapshot
	req.At = call.at

	// BYOK presence is the workspace's, which the edge cannot read. When Go
	// answers alike for none and for every provider, it does not matter;
	// otherwise Go answers with the providers of the BYOK candidates
	// Python returned, the least presence that can explain Python's answer
	// (no presence at all when Python refused): admission keeps a BYOK
	// candidate exactly when its provider is present, so if any presence
	// gives Python's answer, that one does, and a difference means none
	// does. Python's presence is taken as its answer implies it, never
	// guessed.
	none := s.Evaluate(req)
	if none.Outcome == routingsnapshot.PassToPython {
		return passed(redact(none.Reason))
	}
	py, why := pythonAnswerOf(c.status, c.encoding, c.response)
	if why != "" {
		return passed(why)
	}
	every := req
	every.BYOKProviders = h.providers
	byok, goAnswer := "determined", none
	if !sameResult(none, s.Evaluate(every)) {
		byok = "consistent"
		implied := req
		implied.BYOKProviders = h.impliedBYOK(py.candidates)
		goAnswer = s.Evaluate(implied)
		if goAnswer.Outcome == routingsnapshot.PassToPython {
			return passed(redact(goAnswer.Reason))
		}
		req = implied
	}
	// Python read its clock between the request's arrival and its answer:
	// a price epoch or an availability instant between them is not a
	// difference.
	later := req
	later.At = c.done
	if !sameResult(goAnswer, s.Evaluate(later)) {
		return passed("the snapshot's answer changed while python decided")
	}

	out := result{byok: byok, pythonStatus: py.status}
	switch goAnswer.Outcome {
	case routingsnapshot.Answered:
		out.goOutcome = "answered"
	default:
		out.goOutcome = "refused"
	}
	affinity, warm := false, false
	switch {
	case goAnswer.Outcome == routingsnapshot.Answered && py.answered:
		estimate, stageD := goAnswer.ModelEstimate, goAnswer
		estimated := true
		if !slices.Equal(goAnswer.Candidates, py.candidates) {
			switch {
			case affinityKey && h.promotedWithinItsModel(goAnswer.Candidates, py.candidates):
				affinity = true
			case affinityKey && h.warmInsteadOfFirst(req, goAnswer.Candidates, py.candidates):
				// Python re-ranked, then kept one: its candidate and its
				// estimate are the warm endpoint's. Stage D is read over
				// Python's candidate, and the estimate checked where Go,
				// pinned to the warm endpoint's provider, answers exactly
				// that endpoint.
				warm, stageD.Candidates = true, py.candidates
				estimate, estimated = h.warmEstimate(req, py.candidates[0])
				out.warmEstimate = "unchecked"
				if estimated {
					out.warmEstimate = "checked"
				}
			default:
				out.fields = append(out.fields, "candidates")
			}
		}
		if estimated && estimate != py.estimate {
			if py.estimate > estimate {
				out.fields = append(out.fields, "estimate_high")
				out.markup = markupBPS(estimate, py.estimate)
			} else {
				out.fields = append(out.fields, "estimate_low")
			}
		}
		out.stageD = stageDOf(s, call.verdict, req, stageD, py.stageD)
		if out.stageD == "differ" {
			out.fields = append(out.fields, "stage_d_reason")
		}
	case goAnswer.Outcome == routingsnapshot.Refused && !py.answered:
		want := *goAnswer.Refusal
		if want.Status != py.status {
			out.fields = append(out.fields, "refusal.status")
		}
		if want.Type != py.refusal.Type {
			out.fields = append(out.fields, "refusal.type")
		}
		if want.Message != py.refusal.Message {
			out.fields = append(out.fields, "refusal.message")
		}
		if want.Param != py.refusal.Param {
			out.fields = append(out.fields, "refusal.param")
		}
	default:
		out.fields = append(out.fields, "outcome")
	}
	switch {
	case warm:
		// Its own class whatever else differs, which is counted by field
		// within it: the warm endpoint's share stays visible.
		out.class = ClassAffinityWarm
		for i, f := range out.fields {
			out.fields[i] = ClassAffinityWarm + "." + f
		}
	case len(out.fields) > 0:
		out.class = ClassDiffer
	case affinity:
		out.class = ClassAffinity
	default:
		out.class = ClassAgree
	}
	return out
}

func sameResult(a, b routingsnapshot.Result) bool {
	if a.Outcome != b.Outcome || !slices.Equal(a.Candidates, b.Candidates) || a.ModelEstimate != b.ModelEstimate ||
		a.ServiceTier != b.ServiceTier || a.Reason != b.Reason || (a.Refusal == nil) != (b.Refusal == nil) {
		return false
	}
	return a.Refusal == nil || *a.Refusal == *b.Refusal
}

// impliedBYOK are the providers of the BYOK endpoints among Python's
// candidates.
func (h *held) impliedBYOK(candidates []string) []string {
	var out []string
	for _, id := range candidates {
		if provider, ok := h.byokProvider[id]; ok && !slices.Contains(out, provider) {
			out = append(out, provider)
		}
	}
	return out
}

// promotedWithinItsModel: py is cold with one endpoint moved to the front of
// its model's run, or to the front of the list, which is the order
// routing_state.rank gives a session's sticky endpoint (the warm cache)
// with the sort partitioned by model or not at all.
func (h *held) promotedWithinItsModel(cold, py []string) bool {
	if len(cold) != len(py) {
		return false
	}
	i := 0
	for i < len(cold) && cold[i] == py[i] {
		i++
	}
	if i == len(cold) {
		return false
	}
	j := slices.Index(cold, py[i])
	if j <= i {
		return false
	}
	moved := slices.Concat(cold[:i], []string{cold[j]}, cold[i:j], cold[j+1:])
	if !slices.Equal(moved, py) {
		return false
	}
	model := h.endpointModel[cold[j]]
	return i == 0 || (h.endpointModel[cold[i-1]] != model && h.endpointModel[cold[i]] == model)
}

// warmInsteadOfFirst: with fallbacks off Python re-ranks and then keeps
// one candidate, so a session's warm endpoint replaces Go's first. It is
// that when both lists are one long and Python's endpoint is one Go admits
// for the same model with fallbacks on (and no provider.order, under which
// Python does not re-rank).
func (h *held) warmInsteadOfFirst(req routingsnapshot.Request, cold, py []string) bool {
	if len(cold) != 1 || len(py) != 1 || h.endpointModel[py[0]] == "" || h.endpointModel[py[0]] != h.endpointModel[cold[0]] {
		return false
	}
	on, ok := withFallbacks(req)
	if !ok {
		return false
	}
	// A BYOK endpoint Python chose is one whose provider it found present.
	on.BYOKProviders = append(slices.Clone(on.BYOKProviders), h.impliedBYOK(py)...)
	r := h.snapshot.Evaluate(on)
	return r.Outcome == routingsnapshot.Answered && slices.Contains(r.Candidates, py[0])
}

// warmEstimate is Go's estimate for the warm endpoint: the request, its
// fallbacks still off, with provider.only the endpoint's provider. ok is
// false unless Go then answers exactly that endpoint, when its estimate is
// not one Go can name.
func (h *held) warmEstimate(req routingsnapshot.Request, endpoint string) (int64, bool) {
	fields := map[string]json.RawMessage{}
	if !isJSONNull(req.Provider) && (json.Unmarshal(req.Provider, &fields) != nil || fields == nil) {
		return 0, false
	}
	only, err := json.Marshal([]string{h.endpointProvider[endpoint]})
	if err != nil {
		return 0, false
	}
	fields["only"] = only
	provider, err := json.Marshal(fields)
	if err != nil {
		return 0, false
	}
	pinned := req
	pinned.Provider = provider
	pinned.BYOKProviders = append(slices.Clone(req.BYOKProviders), h.impliedBYOK([]string{endpoint})...)
	r := h.snapshot.Evaluate(pinned)
	if r.Outcome != routingsnapshot.Answered || !slices.Equal(r.Candidates, []string{endpoint}) {
		return 0, false
	}
	return r.ModelEstimate, true
}

// withFallbacks is req with allow_fallbacks unset, top-level and in
// provider; ok is false when provider names an order, or is not an object.
func withFallbacks(req routingsnapshot.Request) (routingsnapshot.Request, bool) {
	req.AllowFallbacks = nil
	if isJSONNull(req.Provider) {
		return req, true
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(req.Provider, &fields) != nil || fields == nil {
		return req, false
	}
	if order, ok := fields["order"]; ok && !isJSONNull(order) {
		return req, false
	}
	delete(fields, "allow_fallbacks")
	provider, err := json.Marshal(fields)
	if err != nil {
		return req, false
	}
	req.Provider = provider
	return req, true
}

// markupBPS is the smallest integer basis points in 1..30000 that, as an
// app markup gateway.py adds (estimate*bps//10000), make goEstimate py,
// or "none". Below 10,000 microdollars every higher estimate up to four
// times Go's has one, a pricing bug as well as a markup: the histogram of
// these, not any one of them, tells the two apart.
func markupBPS(goEstimate, py int64) string {
	if goEstimate <= 0 || py <= goEstimate || goEstimate > math.MaxInt64/maxMarkupBPS {
		return "none"
	}
	marked := func(bps int64) int64 { return goEstimate + goEstimate*bps/10_000 }
	bps := int64(sort.Search(maxMarkupBPS, func(i int) bool { return marked(int64(i)+1) >= py })) + 1
	if bps > maxMarkupBPS || marked(bps) != py {
		return "none"
	}
	return strconv.FormatInt(bps, 10)
}

// stageDOf compares Python's Stage D reason with Go's. The edge knows the
// snapshot's settings and whether the request carried a boot signature at
// all; it cannot know the key's budget_strict, the workspace (so pilot
// membership when the list is not empty), whether Python accepted the boot
// (its image digest and key binding), whether the boot declared heartbeats,
// or the settlement backend. Each of those is tried both ways:
// "determined" when every way gives one reason, "consistent" when Python's
// is among the reasons some way gives, "differ" when it is not. A reason
// Go does not evaluate (the abliterate estimated-usage reason) is "".
func stageDOf(s *routingsnapshot.Snapshot, verdict Verdict, req routingsnapshot.Request, r routingsnapshot.Result,
	python string) string {
	if python == "abliterate_estimated_usage" {
		return ""
	}
	st := s.Settings.StageD
	in := routingsnapshot.StageDInputs{HeartbeatEnabled: st.HeartbeatEnabled,
		RequireHeartbeatDeclaration: st.RequireHeartbeatDeclaration, Pilot: len(st.PilotWorkspaces) == 0,
		// The edge passes custom and user models, partner billing, the
		// additional cost and native batch routes to Python.
		StandardEndpointPricing: true}
	var unknown []*bool
	if st.EligibilityEnabled {
		unknown = append(unknown, &in.EligibilityEnabled)
	}
	if len(st.PilotWorkspaces) > 0 {
		unknown = append(unknown, &in.Pilot)
	}
	if verdict != Absent {
		unknown = append(unknown, &in.BootAccepted)
	}
	unknown = append(unknown, &in.HeartbeatDeclared)
	if s.Settings.RequestRecordWriteMode == "typed" {
		unknown = append(unknown, &in.SettlementBackend)
	}
	reasons := map[string]bool{}
	for mask := 0; mask < 1<<len(unknown); mask++ {
		for i, field := range unknown {
			*field = mask>>i&1 == 1
		}
		if reason, ok := s.StageDReason(in, req, r); ok {
			reasons[reason] = true
		}
	}
	switch {
	case !reasons[python]:
		return "differ"
	case len(reasons) == 1:
		return "determined"
	default:
		return "consistent"
	}
}

// pythonAnswer is what the comparator reads of Python's authorize answer:
// the candidates in order, the estimate, the Stage D reason, or the
// refusal.
type pythonAnswer struct {
	status     int
	answered   bool
	candidates []string
	estimate   int64
	stageD     string
	refusal    routingsnapshot.Refusal
}

var errorType = regexp.MustCompile(`^[a-z_]{1,64}$`)

func pythonAnswerOf(status int, encoding string, raw []byte) (pythonAnswer, string) {
	py := pythonAnswer{status: status}
	switch strings.ToLower(strings.TrimSpace(encoding)) {
	case "", "identity":
	case "gzip":
		zr, err := gzip.NewReader(bytes.NewReader(raw))
		if err != nil {
			return py, "python's answer is not the authorize shape"
		}
		raw, err = io.ReadAll(io.LimitReader(zr, maxShadowResponse+1))
		if err != nil || len(raw) > maxShadowResponse {
			return py, "python's answer was not read through within the comparator's bound"
		}
	default:
		return py, "python's answer is in an encoding the comparator does not read"
	}
	var top map[string]json.RawMessage
	if json.Unmarshal(raw, &top) != nil {
		return py, "python's answer is not the authorize shape"
	}
	if status == http.StatusOK {
		var data map[string]json.RawMessage
		if json.Unmarshal(top["data"], &data) != nil || data == nil {
			return py, "python's answer is not the authorize shape"
		}
		var replay bool
		if json.Unmarshal(data["idempotent_replay"], &replay) != nil {
			return py, "python's answer is not the authorize shape"
		}
		if replay {
			// The answer is an earlier authorization's, decided at its own
			// instant.
			return py, "an idempotent replay"
		}
		var candidates []map[string]json.RawMessage
		var stageD map[string]json.RawMessage
		if json.Unmarshal(data["route_candidates"], &candidates) != nil || candidates == nil ||
			json.Unmarshal(data["estimated_cost_microdollars"], &py.estimate) != nil ||
			json.Unmarshal(data["stage_d"], &stageD) != nil || json.Unmarshal(stageD["reason"], &py.stageD) != nil {
			return py, "python's answer is not the authorize shape"
		}
		for _, c := range candidates {
			var id string
			if json.Unmarshal(c["endpoint_id"], &id) != nil {
				return py, "python's answer is not the authorize shape"
			}
			py.candidates = append(py.candidates, id)
		}
		py.answered = true
		return py, ""
	}
	var envelope map[string]json.RawMessage
	if status < 400 || json.Unmarshal(top["error"], &envelope) != nil || envelope == nil ||
		json.Unmarshal(envelope["type"], &py.refusal.Type) != nil ||
		json.Unmarshal(envelope["message"], &py.refusal.Message) != nil {
		return py, "python answered status " + strconv.Itoa(status) + " outside the authorize shape"
	}
	if param, ok := envelope["param"]; ok && !isJSONNull(param) {
		if json.Unmarshal(param, &py.refusal.Param) != nil {
			return py, "python's answer is not the authorize shape"
		}
	}
	py.refusal.Status = status
	if status != http.StatusBadRequest || !routingRefusals[py.refusal.Type] {
		// Refused before or after routing: the key, the workspace, money,
		// idempotency or limits, which the snapshot does not decide.
		kind := py.refusal.Type
		if !errorType.MatchString(kind) {
			kind = "other"
		}
		return py, "python refused outside routing: " + strconv.Itoa(status) + " " + kind
	}
	return py, ""
}

func isJSONNull(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null"))
}

// requestOf reads the authorize body as Evaluate takes it, the fields as the
// enclave sent them. A body that carries an input Evaluate assumes absent
// (docs/design/routing-snapshot-v1.md, "What Go evaluates"), or a field not
// of the schema's type, goes to Python with the reason; so does an explicit
// null estimated_input_tokens, which the schema refuses, where only its
// omission is the default of 1. affinity says the body names a cache
// affinity key, under which Python's health re-rank may promote one
// endpoint (the affinity class).
func requestOf(body []byte, call *shadowCall) (req routingsnapshot.Request, affinity bool, why string) {
	if !utf8.Valid(body) {
		return req, false, "the request body is not UTF-8"
	}
	var f map[string]json.RawMessage
	if json.Unmarshal(body, &f) != nil || f == nil {
		return req, false, "the request body is not a JSON object"
	}
	present := func(name string) bool {
		v, ok := f[name]
		return ok && !isJSONNull(v)
	}
	const types = "a field that is not of the authorize schema's type"
	model, ok := jsonString(f["model"])
	if !ok || model == nil {
		return req, false, "model is missing or not a string"
	}
	models, ok := jsonStrings(f["models"])
	if !ok {
		return req, false, types
	}
	// The inputs authorize_candidates takes that Evaluate assumes absent.
	if v, ok := f["additional_cost_reservation_microdollars"]; ok && !bytes.Equal(bytes.TrimSpace(v), []byte("0")) {
		return req, false, "an additional cost reservation"
	}
	if v, ok := f["inference_receipt"]; ok && !bytes.Equal(bytes.TrimSpace(v), []byte("false")) {
		return req, false, "an inference receipt's fee"
	}
	if present("video_resolution") || call.videoProviders {
		return req, false, "a video resolution or allowed providers"
	}
	if present("speech_input_characters") || present("transcription_duration_ms") {
		return req, false, "speech or transcription usage"
	}
	idempotency, ok := jsonString(f["idempotency_key"])
	if !ok {
		return req, false, types
	}
	if call.partnerKey || (idempotency != nil && strings.Contains(*idempotency, partnerIdempotency)) {
		return req, false, "a partner billing mode"
	}
	for _, m := range append([]string{*model}, models...) {
		lower := strings.ToLower(m)
		if strings.Contains(lower, "tr-custom-model/") || strings.Contains(lower, "tr-user-model/") {
			return req, false, "a custom or user-provided model"
		}
		if strings.Contains(lower, "parasail/liberty-2.0") {
			return req, false, "a partner billing mode"
		}
	}
	if v, ok := f["estimated_input_tokens"]; ok && isJSONNull(v) {
		return req, false, "an explicit null estimated_input_tokens"
	}
	req.Model, req.Models = *model, models
	if present("provider") {
		req.Provider = f["provider"]
	}
	if present("allow_fallbacks") {
		req.AllowFallbacks = f["allow_fallbacks"]
	}
	for _, field := range []struct {
		name string
		into **string
	}{{"route_type", &req.RouteType}, {"region", &req.Region}, {"service_tier", &req.ServiceTier}} {
		if *field.into, ok = jsonString(f[field.name]); !ok {
			return req, false, types
		}
	}
	for _, field := range []struct {
		name string
		into *[]string
	}{{"requested_parameters", &req.RequestedParameters}, {"input_modalities", &req.InputModalities}} {
		if *field.into, ok = jsonStrings(f[field.name]); !ok {
			return req, false, types
		}
	}
	for _, field := range []struct {
		name string
		into **int64
	}{{"estimated_input_tokens", &req.EstimatedInputTokens}, {"max_tokens", &req.MaxTokens},
		{"max_completion_tokens", &req.MaxCompletionTokens}, {"max_output_tokens", &req.MaxOutputTokens}} {
		if *field.into, ok = jsonInt(f[field.name]); !ok {
			return req, false, types
		}
	}
	if req.Stream, ok = jsonBool(f["stream"]); !ok {
		return req, false, types
	}
	key, ok := jsonString(f["cache_affinity_key"])
	if !ok {
		return req, false, types
	}
	return req, key != nil && *key != "", ""
}

// jsonString is a field that is absent or null (nil), or a JSON string; ok
// is false for any other value.
func jsonString(raw json.RawMessage) (*string, bool) {
	if isJSONNull(raw) {
		return nil, true
	}
	var s string
	if raw[0] != '"' || json.Unmarshal(raw, &s) != nil {
		return nil, false
	}
	return &s, true
}

// jsonStrings is a list of strings, nil when absent or null; an item that is
// not a string (a null with it) is not of the type.
func jsonStrings(raw json.RawMessage) ([]string, bool) {
	if isJSONNull(raw) {
		return nil, true
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		s, ok := jsonString(item)
		if !ok || s == nil {
			return nil, false
		}
		out = append(out, *s)
	}
	return out, true
}

// jsonInt is an integer that fits int64 (no fraction, no exponent), nil
// when absent or null.
func jsonInt(raw json.RawMessage) (*int64, bool) {
	if isJSONNull(raw) {
		return nil, true
	}
	var v int64
	if raw[0] == '"' || json.Unmarshal(raw, &v) != nil {
		return nil, false
	}
	return &v, true
}

func jsonBool(raw json.RawMessage) (*bool, bool) {
	if isJSONNull(raw) {
		return nil, true
	}
	var v bool
	if json.Unmarshal(raw, &v) != nil {
		return nil, false
	}
	return &v, true
}

// knownProviderReasons are the evaluator's reasons that name a provider field
// by its schema name; any other "provider.<name>" reason names a field the
// body made up, which is the body's content.
var knownProviderReasons = map[string]bool{}

func init() {
	for _, name := range []string{"min_privacy", "max_price", "preferred_max_latency", "preferred_min_throughput",
		"require_parameters", "data_collection spelling", "sort shape", "sort is not printable ASCII", "sort spelling",
		"usage spelling", "jurisdiction spelling", "zdr is not a boolean"} {
		knownProviderReasons["provider."+name] = true
	}
	for _, field := range []string{"order", "only", "ignore"} {
		knownProviderReasons["provider."+field+" is not a list or a string"] = true
		knownProviderReasons["provider."+field+" has an item that is not a provider name"] = true
	}
}

// redact is an evaluator's reason with every value from the request taken
// out: quoted values (model strings, slugs, route types) become <value>, and
// a provider field the schema does not name becomes <field>. What is left is
// one of a fixed set of sentences.
func redact(reason string) string {
	var b strings.Builder
	for i := 0; i < len(reason); {
		if reason[i] == '"' {
			if quoted, err := strconv.QuotedPrefix(reason[i:]); err == nil {
				b.WriteString("<value>")
				i += len(quoted)
				continue
			}
			// An unterminated quote: nothing after it is kept.
			b.WriteString("<value>")
			break
		}
		b.WriteByte(reason[i])
		i++
	}
	out := b.String()
	if strings.HasPrefix(out, "provider.") && !knownProviderReasons[out] {
		return "provider.<field>"
	}
	return out
}
