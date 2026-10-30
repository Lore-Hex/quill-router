package edge

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The full-response shadow (docs/design/fast-admission-e2-decide.md §11,
// G10; -edge-decide-shadow): for each authorize the edge passes to Python,
// the decide pipeline (decide.go) runs off the request path, and once
// Python has answered, what Go would have answered is compared with what
// Python did: the whole 200 byte for byte after Python's per-request values
// (ids, timing) are put in Go's place, and, for one request in 20 and at
// most 5 a second, the two rows Python wrote against Go's would-be rows.
// Python decides; nothing computed here reaches the bytes the enclave
// receives, and the proxy never waits for it. The logs carry counts, class
// and field names and digests: never a body, an id, a key or a message.

// The comparison's classes (§11). A class with a dot is counted by its
// suffix too: a reason, a status and type, or a cache.
const (
	ClassAgreeFull = "agree_full"
	ClassDifferAny = "differ"
	// FieldAffinityWarm is the one field of a differ whose Python 200 is
	// Go's decision rendered in routing_state.rank's order for an endpoint
	// a Python process remembered for the request's cache_affinity_key
	// (affinity.go): Go decides the cold order (§1 item 5), so a warm
	// session is a differ like any other, named so the watch can count the
	// differs that are warm re-ranks. Its rows are read back against Go's
	// decision in that order.
	FieldAffinityWarm = "differ.affinity_warm"
	// ClassWouldAdmit is critical: Go would have answered 200 where Python
	// refused (status.type follows).
	ClassWouldAdmit = "would_admit_python_refused"
	// ClassPythonAnsweredGoPassed: Python answered 200, Go would have
	// passed (the reason follows).
	ClassPythonAnsweredGoPassed = "python_answered_go_passed"
	// ClassGoPassed: Go would have passed and Python refused or failed too.
	ClassGoPassed = "passed"
	// ClassStale: Go would have passed for a cold or stale cache.
	ClassStale = "stale"
	// ClassPythonUnavailable: Python's own insert outcome was unknown and not
	// found (decision 1's 503), which the shadow does not reach.
	ClassPythonUnavailable = "python_unavailable"
	// ClassPythonError: Python failed (5xx but its pause 503 and its
	// unavailable 503), which is no refusal on the facts.
	ClassPythonError = "python_error"
	// ClassUnread: Python's answer was not read through (not reached, past
	// the bound or the budget, not answered within the wait).
	ClassUnread = "unread"
	// ClassWindowUnchecked: Python refused a window of a key whose windows
	// it enforces (429 key_window_limit_exceeded, or a 429 whose body was
	// not read), and Go's decision carries no window verdict to compare
	// (Decision.Windows, not read): neither an agreement nor a would-admit
	// (docs/design/fast-admission-e2-key-windows.md section 8.2).
	ClassWindowUnchecked = "window_unchecked"
	// ClassWindowRace: Go read the key's windows and would have admitted,
	// Python refused a window it read more spent (its 429 names a window
	// whose limit Go holds, with RateLimit-Remaining below what Go's rows
	// leave there), and Go's own re-read after Python's answer has caught up
	// with Python's remaining (confirmRace): a settle landed between the two
	// first reads, the race Python's own instances have with each other
	// (section 8.3, at most 0.05% of compares). Counted, never paged one by
	// one; AlertWindowRace watches its rate and its repeats per key.
	ClassWindowRace = "window_race"
	// ClassWindowRaceUnconfirmed: a race candidate no re-read settled (the
	// limiter, a failed or late re-read, a window reset since). It pages as
	// a would-admit does, since it cannot be told from a defect of Go's read.
	ClassWindowRaceUnconfirmed = "window_race_unconfirmed"
)

// windowRefusal is the error type of Python's 429 for a spend window
// (gateway.py, ErrorType.KEY_WINDOW_LIMIT_EXCEEDED).
const windowRefusal = "key_window_limit_exceeded"

const (
	// DefaultDecideShadowJobs bounds the requests the shadow follows at
	// once, each from its arrival to Python's answer; past it a request is
	// not followed (dropped).
	DefaultDecideShadowJobs = 512
	// DefaultRowSampleEvery and DefaultRowReadsPerSecond bound the row
	// read-backs (§11): every comparable request, at most 5 a second (the
	// limiter, not the sampling, bounds the reads Spanner serves).
	DefaultRowSampleEvery    = 1
	DefaultRowReadsPerSecond = 5
	// rowReadTimeout bounds one read-back.
	rowReadTimeout = 2 * time.Second
	// defaultShadowWait is how long a followed request waits for Python's
	// answer: past the enclave's 25 s per attempt.
	defaultShadowWait = 30 * time.Second
	// maxCriticalLogs bounds the critical lines a period logs.
	maxCriticalLogs = 20
	// rowSkew is the clock skew a row's instants may show beyond the
	// request's own latency: Python's clock and the edge's are two hosts'.
	rowSkew = time.Second
)

// AlertWouldAdmit is the alert kind (the fastpath_alert label) of a
// would_admit_python_refused comparison: the edge would have answered an
// authorize Python refused. Its alert policy is I1's or G12's.
const AlertWouldAdmit = "edge_would_admit_python_refused"

// The 503 bodies' messages (errors.py assert_workspace_billing_active;
// gateway.py's insert outcome unknown, decision 1).
const (
	pauseMessage       = "Workspace billing is paused"
	unavailableMessage = "Authorization unavailable; retry"
)

// KeyCache is the key and workspace cache the shadow reads and evicts
// (gwstate.Cache).
type KeyCache interface {
	Lookup(lookupHash string, now time.Time) gwstate.Read
	Evict(lookupHash string)
	EvictWorkspace(workspace string)
}

// AuthorizationReader reads back the rows Python wrote
// (store.Store.ReadAuthorizationForCompare).
type AuthorizationReader interface {
	ReadAuthorizationForCompare(ctx context.Context, authorizationID string) (store.AuthorizationForCompare, bool, error)
}

// DecideShadowConfig turns the full-response shadow on: the cache it reads
// (and evicts on Python's 401, 402, 403 or pause 503), the decide table
// and the rows it reads, and its bounds; a zero bound is its default. It
// needs the routing snapshot and the edge policy configured.
type DecideShadowConfig struct {
	Cache   KeyCache
	Decides DecideReader
	Rows    AuthorizationReader
	// Windows reads an enforcing windowed key's tr_key_limit rows for the
	// shadow's window check (key-windows section 8.2), within ReadsPerSecond
	// of its own; nil checks none.
	Windows KeyWindowReader
	// Jobs bounds the requests followed at once; SampleEvery and
	// ReadsPerSecond the row read-backs; DecideEvery the decide copy's
	// refresh; Wait how long a request waits for Python's answer.
	Jobs           int
	SampleEvery    int
	ReadsPerSecond float64
	DecideEvery    time.Duration
	Wait           time.Duration
	// Mint makes the ids Go would mint (tests pin them); the shadow
	// compares under Python's.
	Mint Mint
	// Alert raises an alert of a kind (the service's alert line, labelled
	// fastpath_alert); nil logs it.
	Alert func(kind, subject, what string)
}

// cacheCounters is the cache's counts, when it keeps them (gwstate.Cache).
type cacheCounters interface{ Counters() gwstate.Counters }

type fullKey struct{}

// fullShadow follows the authorizes the edge passes on.
type fullShadow struct {
	cfg     DecideShadowConfig
	logf    func(string, ...any)
	clock   func() time.Time
	every   time.Duration
	decides *decideCopy
	jobs    chan struct{}
	wg      sync.WaitGroup

	bytes  atomic.Int64
	budget int64

	dropped, evictions, sampled atomic.Int64
	// periodEvictions are this period's, for its line.
	periodEvictions atomic.Int64

	limiter struct {
		sync.Mutex
		tokens float64
		at     time.Time
	}
	// windowLimiter bounds the window reads as limiter the row read-backs.
	windowLimiter struct {
		sync.Mutex
		tokens float64
		at     time.Time
	}

	mu       sync.Mutex
	tallies  map[string]*fullTally
	totals   *fullTally
	critical int
	differs  int
	// raceLogs bounds the race lines a period logs; races is the rolling
	// hour AlertWindowRace reads.
	raceLogs int
	races    raceWatch
	// cacheAt is the cache's counts at the last line, which logs the
	// period's: the cache keeps running totals.
	cacheAt gwstate.Counters
}

// fullTally is one snapshot's and policy's counts: the classes, the
// fields that differ (differ.<field>, row.<column>), the row read-backs by
// outcome, the decide row's gate for the requests Go would decide and
// their classes (cohort), and the answers Python made from a read-back.
type fullTally struct {
	classes, fields, rows, decide, cohort map[string]int
	readBack                              int
	// windows are the window checks' counts (compareWindowed).
	windows map[string]int
	// streams are the requests passed as body.stream, by the Stage D
	// reason the edge reads for them (Decided.StreamStageD).
	streams map[string]int
	// async are the async-v1 counts (§4.4 of
	// docs/design/fast-admission-e2-g15-async.md), by the line's map name
	// (asyncCounts).
	async map[string]map[string]int
}

// The async-v1 count maps, each keyed by population first (plain,
// affinity, or for async_seen unparsed), in the line's order:
//
//   - async_seen: every followed request whose X-TR-Settlement-Mode is
//     exactly ["async-v1"], whatever it passed with;
//   - async_compared: <pop>.agree or <pop>.differ, an async-v1 request Go
//     composed whose Python 200 was read and compared, by whether its async
//     members alone matched;
//   - async_fields: <pop>.<field>, each async member that differed;
//   - async_shape: <pop>.<shape>, Go's composed shape (snapshot, or
//     false_only.<reason>);
//   - async_python_admitted: <pop>, Python's shape C (its admission on) to
//     any followed async-v1 request, whatever Go did with it; not compared.
const (
	asyncSeen           = "async_seen"
	asyncCompared       = "async_compared"
	asyncFields         = "async_fields"
	asyncShape          = "async_shape"
	asyncPythonAdmitted = "async_python_admitted"
)

var asyncCounts = []string{asyncSeen, asyncCompared, asyncFields, asyncShape, asyncPythonAdmitted}

func newFullTally() *fullTally {
	t := &fullTally{classes: map[string]int{}, fields: map[string]int{}, rows: map[string]int{}, decide: map[string]int{},
		cohort: map[string]int{}, windows: map[string]int{}, streams: map[string]int{}, async: map[string]map[string]int{}}
	for _, name := range asyncCounts {
		t.async[name] = map[string]int{}
	}
	return t
}

func (t *fullTally) clone() *fullTally {
	c := &fullTally{classes: clonedCounts(t.classes), fields: clonedCounts(t.fields), rows: clonedCounts(t.rows),
		decide: clonedCounts(t.decide), cohort: clonedCounts(t.cohort), readBack: t.readBack,
		windows: clonedCounts(t.windows), streams: clonedCounts(t.streams), async: map[string]map[string]int{}}
	for name, m := range t.async {
		c.async[name] = clonedCounts(m)
	}
	return c
}

func clonedCounts(m map[string]int) map[string]int {
	out := make(map[string]int, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func newFullShadow(cfg DecideShadowConfig, logf func(string, ...any), clock func() time.Time,
	every time.Duration) (*fullShadow, error) {
	if cfg.Cache == nil || cfg.Decides == nil || cfg.Rows == nil {
		return nil, errors.New("edge: the decide shadow needs its cache, the decide table and the rows")
	}
	if cfg.Jobs <= 0 {
		cfg.Jobs = DefaultDecideShadowJobs
	}
	if cfg.SampleEvery <= 0 {
		cfg.SampleEvery = DefaultRowSampleEvery
	}
	if cfg.ReadsPerSecond <= 0 {
		cfg.ReadsPerSecond = DefaultRowReadsPerSecond
	}
	if cfg.Wait <= 0 {
		cfg.Wait = defaultShadowWait
	}
	if every <= 0 {
		every = time.Minute
	}
	sh := &fullShadow{cfg: cfg, logf: logf, clock: clock, every: every,
		decides: newDecideCopy(cfg.Decides, cfg.DecideEvery, clock, logf), jobs: make(chan struct{}, cfg.Jobs),
		budget: maxShadowBytes, tallies: map[string]*fullTally{}, totals: newFullTally()}
	sh.limiter.tokens = cfg.ReadsPerSecond
	sh.windowLimiter.tokens = cfg.ReadsPerSecond
	return sh, nil
}

func (sh *fullShadow) reserve(n int64) bool {
	if sh.bytes.Add(n) > sh.budget {
		sh.bytes.Add(-n)
		return false
	}
	return true
}

func (sh *fullShadow) release(n int64) {
	if n != 0 {
		sh.bytes.Add(-n)
	}
}

// pyAnswer is Python's answer as the shadow read it: its status (0 when
// none came), its bytes as JSON (decoded from gzip by follow), and, when
// they were not read through, why. latency is from the request's arrival
// to the answer's end. rateLimit are its RateLimit-Limit, -Remaining and
// -Reset headers, Python's window decision in its own words (key-windows
// section 8.2), "" where absent.
type pyAnswer struct {
	status    int
	encoding  string
	body      []byte
	why       string
	latency   time.Duration
	rateLimit [3]string
}

// fullJob is one followed request: what the pipeline reads, held at its
// arrival, and Python's answer once it is in. Its tap is whoever's finishes
// second: follow, at its end, marks the job abandoned under mu and finishes
// the tap it finds; a response that comes later is never tapped.
type fullJob struct {
	in      DecideInputs
	digest  string
	held    int64
	answer  chan pyAnswer
	arrived time.Time // the real clock's, for the latency

	mu        sync.Mutex
	tap       *tap
	abandoned bool
}

// finished ends the job's tap, whatever still passes through it, and is
// what the tap held of the budget.
func (j *fullJob) finished() int64 {
	j.mu.Lock()
	t := j.tap
	j.abandoned = true
	j.mu.Unlock()
	return t.finish()
}

// deliver hands the job Python's answer; only the first is kept, and it
// never waits.
func (j *fullJob) deliver(a pyAnswer) {
	select {
	case j.answer <- a:
	default:
	}
}

// beginFull follows an authorize the edge is about to pass on, when the
// decide shadow is on: the request's one clock reading, the snapshot, the
// policy and the decide copy held now, and the body the edge read (nil when
// it read none). A request it cannot follow (every job busy, or the byte
// budget spent) is counted as dropped and passes as it came.
func (e *Edge) beginFull(r *http.Request, at time.Time, body []byte, boot BootSeen) *http.Request {
	sh := e.full
	if sh == nil || r.Method != http.MethodPost || !authorizeRoutes[r.URL.Path] {
		return r
	}
	if !sh.reserve(int64(len(body))) {
		sh.dropped.Add(1)
		return r
	}
	select {
	case sh.jobs <- struct{}{}:
	default:
		sh.release(int64(len(body)))
		sh.dropped.Add(1)
		return r
	}
	job := &fullJob{held: int64(len(body)), answer: make(chan pyAnswer, 1), arrived: time.Now(), in: DecideInputs{
		Request: RequestOf(r, at, body, boot),
		Lookup:  sh.cfg.Cache.Lookup, Decide: sh.decides.view(), Mint: sh.cfg.Mint,
	}}
	snapshotDigest, policyDigest := "none", "none"
	if e.poller != nil {
		if h := e.poller.snapshot(); h != nil {
			job.in.Snapshot, snapshotDigest = h.snapshot, h.claims.SHA256
		}
	}
	if e.policy != nil {
		if h := e.policy.policy(); h != nil {
			job.in.Policy, policyDigest = h.policy, h.claims.SHA256
		}
	}
	job.digest = "snapshot=" + snapshotDigest + " policy=" + policyDigest
	sh.wg.Add(1)
	go sh.follow(job)
	return r.WithContext(context.WithValue(r.Context(), fullKey{}, job))
}

// tapFull keeps a copy of Python's answer as it passes to the enclave, and
// hands it to the job once the proxy has closed it; nothing is decoded on
// the proxy's goroutine. A job follow has already given up is not tapped.
func (sh *fullShadow) tapFull(resp *http.Response, job *fullJob) {
	status, encoding := resp.StatusCode, resp.Header.Get("Content-Encoding")
	rateLimit := [3]string{resp.Header.Get("RateLimit-Limit"), resp.Header.Get("RateLimit-Remaining"),
		resp.Header.Get("RateLimit-Reset")}
	job.mu.Lock()
	defer job.mu.Unlock()
	if job.abandoned {
		return
	}
	job.tap = &tap{rc: resp.Body, sh: sh, limit: maxShadowResponse, length: resp.ContentLength, onClose: func(t *tap) {
		raw, complete, spent := t.bytes()
		a := pyAnswer{status: status, encoding: encoding, body: raw, latency: time.Since(job.arrived),
			rateLimit: rateLimit}
		switch {
		case spent:
			a.why, a.body = "budget", nil
		case !complete:
			a.why, a.body = "bound", nil
		}
		job.deliver(a)
	}}
	resp.Body = job.tap
}

// decode is Python's answer's bytes as JSON, gunzipped when Python
// compressed them (decision 13: Go never does), and the bytes of the
// budget the decoded copy holds: what may be decoded is reserved before a
// byte is, and what was not used given back. why says why there is none.
func (sh *fullShadow) decode(encoding string, raw []byte) (out []byte, held int64, why string) {
	switch strings.ToLower(strings.TrimSpace(encoding)) {
	case "", "identity":
		return raw, 0, ""
	case "gzip":
	default:
		return nil, 0, "encoding"
	}
	zr, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		return nil, 0, "encoding"
	}
	limit := min(int64(maxShadowResponse), sh.budget-sh.bytes.Load())
	if limit <= 0 || !sh.reserve(limit) {
		return nil, 0, "budget"
	}
	out, err = io.ReadAll(io.LimitReader(zr, limit+1))
	switch {
	case err != nil:
		sh.release(limit)
		return nil, 0, "encoding"
	case int64(len(out)) > limit && limit < maxShadowResponse:
		sh.release(limit)
		return nil, 0, "budget"
	case int64(len(out)) > limit:
		sh.release(limit)
		return nil, 0, "bound"
	}
	sh.release(limit - int64(len(out)))
	return out, int64(len(out)), ""
}

// follow decides the request, waits for Python's answer, compares, evicts
// what Python's refusal says is behind, and reads back the rows when
// sampled; then gives back its job and its bytes.
func (sh *fullShadow) follow(job *fullJob) {
	defer sh.wg.Done()
	defer func() { <-sh.jobs }()
	decided := sh.safeDecide(job.in)
	// An enforcing windowed key's window check, at about the instant
	// Python makes its own, before its answer is awaited.
	windows := sh.checkWindows(decided, job.in.Request.At)
	var a pyAnswer
	wait := time.NewTimer(sh.cfg.Wait)
	select {
	case a = <-job.answer:
		wait.Stop()
	case <-wait.C:
		a = pyAnswer{why: "wait"}
	}
	// The bytes as JSON, here and not on the proxy's goroutine; a gzip's
	// decoded bytes are the budget's too.
	var decodedHeld int64
	if a.why == "" && a.status != 0 {
		var why string
		if a.body, decodedHeld, why = sh.decode(a.encoding, a.body); why != "" {
			a.why = why
		}
	}
	r := sh.safeCompare(decided, a, windows)
	if r.race != nil {
		sh.confirmRace(decided.ForShadow(), windows, &r)
	}
	sh.evict(decided, a, job.in.Request.Body)
	if r.rows && sh.sample() {
		d := decided.ForShadow()
		if r.warm != nil {
			d = r.warm
		}
		sh.readRows(d, &r, a.latency)
	}
	sh.record(job.digest, r)
	sh.release(job.held + decodedHeld + job.finished())
}

func (sh *fullShadow) safeDecide(in DecideInputs) (d Decided) {
	defer func() {
		if recover() != nil {
			d = Decided{Pass: "shadow.decide_failed"}
		}
	}()
	return Decide(in)
}

func (sh *fullShadow) safeCompare(d Decided, a pyAnswer, w *windowRead) (r fullResult) {
	defer func() {
		if recover() != nil {
			r = fullResult{class: ClassUnread + ".compare_failed"}
		}
	}()
	return compareWindowed(d, a, w)
}

// fullResult is one comparison: its class, the fields that differ, whether
// Python answered from a read-back, the decide row's gate (when Go would
// decide), whether the rows are worth reading back, Python's ids for that
// read, and, for a differ.affinity_warm, Go's decision in Python's warm
// order, whose rows that read compares; windows are the window check's
// counts (compareWindowed).
type fullResult struct {
	class    string
	fields   []string
	readBack bool
	gate     string
	decided  bool
	rows     bool
	py       pythonIDs
	rowsOf   string
	warm     *Decision
	async    asyncResult
	windows  []string
	stream   string
	race     *raceCandidate
}

// asyncResult is one followed request's async-v1 counts (asyncCounts): its
// population ("" for a request not exactly async-v1), Go's composed shape
// ("" when Go composed none), whether its async members were compared
// ("agree" or "differ", "" when not) and the members that differed, and
// whether Python answered shape C.
type asyncResult struct {
	pop, shape, compared string
	fields               []string
	pythonAdmitted       bool
}

// pythonIDs are Python's per-request values from its 200.
type pythonIDs struct {
	authorization, reservation string
}

// compareFull is the comparison of what Go would have answered with what
// Python answered (compareClass), with the request's async-v1 population
// and, when Go composed it, its shape. Python's async members are read
// once, from any 200 read through, and feed both the comparison and the
// ticket count: Python's shape C to an async-v1 request is
// async_python_admitted whatever Go did with it (passed before the compose
// or composed), and never compared (G15 §3.6, §4.4).
func compareFull(d Decided, a pyAnswer) fullResult {
	var py asyncMembers
	if a.status == http.StatusOK && a.why == "" {
		py = pythonAsyncMembers(a.body)
	}
	r := compareClass(d, a, py)
	r.async.pop = d.AsyncPop
	r.stream = d.StreamStageD
	if dec := d.ForShadow(); dec != nil && d.AsyncPop != "" {
		r.async.shape = asyncShapeName(dec.Staged.Async())
	}
	if d.AsyncPop != "" && py.shape() == asyncShapeTicket {
		r.async.pythonAdmitted, r.async.compared, r.async.fields = true, "", nil
	}
	return r
}

// asyncShapeName is a composed shape as async_shape counts it: snapshot,
// or false_only.<reason>.
func asyncShapeName(a gwauth.Async) string {
	if a.Shape == gwauth.AsyncFalseOnly {
		return a.Shape + "." + a.Reason
	}
	return a.Shape
}

// compareClass classes the comparison. Python's status classifies whenever
// it came, its body read or not: a refusal whose body was not read is a
// refusal all the same. py are Python's async members (compareFull).
func compareClass(d Decided, a pyAnswer, py asyncMembers) fullResult {
	read := a.why == ""
	if d.ForShadow() == nil {
		reason := d.Pass
		switch {
		case strings.HasPrefix(reason, "cache."):
			return fullResult{class: ClassStale + "." + strings.TrimPrefix(reason, "cache.")}
		case a.status == http.StatusOK:
			return fullResult{class: ClassPythonAnsweredGoPassed + "." + reason}
		case a.status == 0:
			return fullResult{class: ClassUnread + "." + a.why}
		}
		return fullResult{class: ClassGoPassed + "." + reason}
	}
	r := fullResult{decided: true, gate: d.DecideGate}
	// Go's decision for an enforcing windowed key holds no verdict on its
	// windows: Python's window refusal is not one Go would have made.
	unchecked := d.ForShadow().Windows != nil && a.status == http.StatusTooManyRequests
	if !read {
		switch {
		case a.status == 0 || a.status == http.StatusOK:
			r.class = ClassUnread + "." + a.why
		case a.status >= 500:
			// A pause 503 is told from Python's others by its body alone.
			r.class = ClassPythonError + "." + strconv.Itoa(a.status) + ".unread"
		case unchecked:
			r.class = ClassWindowUnchecked + "." + strconv.Itoa(a.status) + ".unread"
		default:
			r.class = ClassWouldAdmit + "." + strconv.Itoa(a.status) + ".unread"
		}
		return r
	}
	refusal := errorOf(a)
	switch {
	case a.status == http.StatusOK:
		return compareAnswer(d.ForShadow(), a.body, py, r)
	case a.status == http.StatusConflict && refusal.kind == "conflict":
		// Another body under the key: Go's insert would have found the
		// scope taken and passed (insert.scope_taken).
		r.class = ClassGoPassed + ".insert.scope_taken"
	case a.status == http.StatusServiceUnavailable && refusal.message == unavailableMessage:
		r.class = ClassPythonUnavailable
	case a.status >= 500 && !(a.status == http.StatusServiceUnavailable && refusal.message == pauseMessage):
		r.class = ClassPythonError + "." + strconv.Itoa(a.status)
	case unchecked && refusal.kind == windowRefusal:
		r.class = ClassWindowUnchecked + "." + strconv.Itoa(a.status) + "." + refusal.kind
	default:
		r.class = ClassWouldAdmit + "." + strconv.Itoa(a.status) + "." + refusal.kind
	}
	return r
}

// refusalOf is an error answer's type (bounded to a name) and message.
type refusalOf struct{ kind, message string }

func errorOf(a pyAnswer) refusalOf {
	out := refusalOf{kind: "other"}
	if a.status == http.StatusOK {
		return out
	}
	tree, err := pyjson.Decode(a.body)
	top, ok := tree.(pyjson.Object)
	if err != nil || !ok {
		return out
	}
	e, _ := memberOf(top, "error").(pyjson.Object)
	if kind, _ := memberOf(e, "type").(string); errorType.MatchString(kind) {
		out.kind = kind
	}
	out.message, _ = memberOf(e, "message").(string)
	return out
}

func memberOf(o pyjson.Object, key string) any {
	for _, m := range o {
		if m.Key == key {
			return m.Value
		}
	}
	return nil
}

// timingKeys are §6 item 15's, in order.
var timingKeys = []string{"total_ms", "spanner_rpcs", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms"}

// compareAnswer is Python's 200 against Go's: Go's decision staged again
// under Python's authorization and reservation ids (the generation id
// follows by rule) and rendered with Python's timing values, byte for byte,
// as inserted or as after a read-back (sorted tags, §6). An idempotent
// replay is the scope taken (Go's insert would have passed). An async-v1
// request's async members (G15 §4.4) are compared too, apart from the
// whole: against Go's additions from the candidates the comparison used,
// Python's warm ones for a differ.affinity_warm, Go's cold ones otherwise,
// each member by its bytes; py are Python's (pythonAsyncMembers).
func compareAnswer(d *Decision, raw []byte, py asyncMembers, r fullResult) fullResult {
	tree, err := pyjson.Decode(raw)
	top, ok := tree.(pyjson.Object)
	data, isData := memberOf(top, "data").(pyjson.Object)
	if err != nil || !ok || !isData || len(top) != 1 {
		r.class, r.fields = ClassDifferAny, []string{"differ.shape"}
		r.async = compareAsync(d.Staged.Async(), py)
		return r
	}
	if replay, _ := memberOf(data, "idempotent_replay").(bool); replay {
		r.class = ClassPythonAnsweredGoPassed + ".insert.scope_taken"
		return r
	}
	r.async = compareAsync(d.Staged.Async(), py)
	r.py.authorization, _ = memberOf(data, "authorization_id").(string)
	r.py.reservation, _ = memberOf(data, "credit_reservation_id").(string)
	r.rows = r.py.authorization != ""
	timing, shaped := timingOf(memberOf(data, "timing"))
	staged, err := d.Restage(r.py.authorization, r.py.reservation, d.Input.IdempotencyKey, d.Input.At)
	if err != nil {
		// Python's ids are not ones Go would write: compared as Go's own.
		staged = d.Staged
		r.fields = append(r.fields, "differ.ids")
	}
	// The envelope is not in the 200; any will do to render it.
	composed, err := staged.Finish("shadow", d.Input.At.UTC())
	if err != nil {
		r.class, r.fields = ClassDifferAny, append(r.fields, "differ.compose")
		return r
	}
	if len(r.fields) == 0 && shaped {
		if ok, readBack := renders(composed, timing, raw); ok {
			r.class, r.readBack = ClassAgreeFull, readBack
			return r
		}
		if warm, warmStaged, ok, readBack := warmBytes(d, staged, data, raw, timing); ok {
			// Python's bytes are Go's decision with a remembered
			// endpoint promoted: Go's answer differs, for a session Go
			// cannot read (§1 item 5). Its async members are the warm
			// candidates' (G15 §4.4).
			r.class, r.fields, r.warm, r.readBack = ClassDifferAny, []string{FieldAffinityWarm}, warm, readBack
			r.async = compareAsync(warmStaged.Async(), py)
			return r
		}
	}
	goTree, _ := pyjson.Decode(composed.Render(timing, false))
	goTop, _ := goTree.(pyjson.Object)
	goData, _ := memberOf(goTop, "data").(pyjson.Object)
	r.fields = append(r.fields, differences(goData, data, shaped, asyncDifferencesOf(staged.Async(), py))...)
	if len(r.fields) == 0 {
		r.fields = []string{"differ.bytes"}
	}
	r.class = ClassDifferAny
	return r
}

// renders: raw is composed's 200 with Python's timing, as inserted, or,
// readBack, as after a read-back (tags sorted, §6).
func renders(composed gwauth.Composed, timing gwauth.Timing, raw []byte) (ok, readBack bool) {
	switch {
	case bytes.Equal(composed.Render(timing, false), raw):
		return true, false
	case bytes.Equal(composed.Render(timing, true), raw):
		return true, true
	}
	return false, false
}

// warmBytes: Python's 200 is Go's decision, under Python's ids as staged,
// rendered with Python's candidates where they are a warm re-rank of Go's
// cold order (Decision.warmOf), as inserted or, readBack, after a
// read-back; warm is that decision and restaged its staging under
// Python's ids.
func warmBytes(d *Decision, staged gwauth.Staged, data pyjson.Object, raw []byte,
	timing gwauth.Timing) (warm *Decision, restaged gwauth.Staged, ok, readBack bool) {
	warm, ok = d.warmOf(candidateIDs(memberOf(data, "route_candidates")))
	if !ok {
		return nil, gwauth.Staged{}, false, false
	}
	in := staged.Input()
	restaged, err := warm.Restage(in.AuthorizationID, in.ReservationID, in.IdempotencyKey, in.At)
	if err != nil {
		return nil, gwauth.Staged{}, false, false
	}
	composed, err := restaged.Finish("shadow", in.At.UTC())
	if err != nil {
		return nil, gwauth.Staged{}, false, false
	}
	ok, readBack = renders(composed, timing, raw)
	return warm, restaged, ok, readBack
}

// The async members' differ fields (G15 §4.4).
const (
	FieldAsyncEligible       = "differ.async_eligible"
	FieldBillingSnapshot     = "differ.billing_snapshot"
	FieldBillingSnapshotHash = "differ.billing_snapshot_hash"
	FieldAsyncShape          = "differ.async_shape"
)

// asyncMemberKeys are the members an async-v1 200 can add, in the order
// Python adds them (G15 §2.2).
var asyncMemberKeys = []string{"async_eligible", "billing_snapshot", "billing_snapshot_hash", "settlement_ticket",
	"settlement_status_url"}

// asyncShapeTicket is Python's shape C, which Go never composes.
const asyncShapeTicket = "ticket"

// asyncMembers are a 200's async members as sent: each member's bytes, by
// name (§4.4 compares the members' bytes, not their decoded values).
type asyncMembers map[string][]byte

// pythonAsyncMembers are the async members of Python's 200 body, each one's
// bytes exactly as Python sent them: nil when the body is not an object
// whose data is one.
func pythonAsyncMembers(raw []byte) asyncMembers {
	var top struct {
		Data map[string]json.RawMessage `json:"data"`
	}
	if json.Unmarshal(raw, &top) != nil || top.Data == nil {
		return nil
	}
	out := asyncMembers{}
	for _, k := range asyncMemberKeys {
		if v, ok := top.Data[k]; ok {
			out[k] = v
		}
	}
	return out
}

// shape is the shape the members are: gwauth's AsyncNone, AsyncFalseOnly
// (A) or AsyncSnapshot (B), a ticket (C), or "other".
func (m asyncMembers) shape() string {
	var present []string
	for _, k := range asyncMemberKeys {
		if _, ok := m[k]; ok {
			present = append(present, k)
		}
	}
	switch {
	case len(present) == 0:
		return gwauth.AsyncNone
	case slices.Equal(present, asyncMemberKeys[:1]):
		return gwauth.AsyncFalseOnly
	case slices.Equal(present, asyncMemberKeys[:3]):
		return gwauth.AsyncSnapshot
	case slices.Contains(present, "settlement_ticket"):
		return asyncShapeTicket
	}
	return "other"
}

// asyncDifferencesOf are the fields on which Go's composed async members,
// each encoded as the 200 renders it, and Python's differ: async_eligible,
// the shape, and, both being shape B, the snapshot's bytes, or with those
// equal the hash's (an encoder defect).
func asyncDifferencesOf(goAsync gwauth.Async, py asyncMembers) []string {
	g := asyncMembers{}
	for _, m := range goAsync.Members {
		b, err := pyjson.DumpsPydantic(m.Value)
		if err != nil {
			return []string{FieldAsyncShape} // Stage encoded them already
		}
		g[m.Key] = b
	}
	var out []string
	if !bytes.Equal(g["async_eligible"], py["async_eligible"]) {
		out = append(out, FieldAsyncEligible)
	}
	shape := g.shape()
	switch {
	case shape != py.shape():
		out = append(out, FieldAsyncShape)
	case shape != gwauth.AsyncSnapshot:
	case !bytes.Equal(g["billing_snapshot"], py["billing_snapshot"]):
		out = append(out, FieldBillingSnapshot)
	case !bytes.Equal(g["billing_snapshot_hash"], py["billing_snapshot_hash"]):
		out = append(out, FieldBillingSnapshotHash)
	}
	return out
}

// compareAsync compares Go's composed async members with Python's (nil
// when Python's 200 did not read as one): nothing for a request Go composed
// none for. A Python ticket is compareFull's to count, never compared.
func compareAsync(goAsync gwauth.Async, py asyncMembers) asyncResult {
	var out asyncResult
	if goAsync.Shape == "" || goAsync.Shape == gwauth.AsyncNone {
		return out
	}
	out.compared, out.fields = "agree", asyncDifferencesOf(goAsync, py)
	if len(out.fields) > 0 {
		out.compared = "differ"
	}
	return out
}

// candidateIDs are the endpoint ids of a 200's route_candidates, in order.
func candidateIDs(v any) []string {
	l, _ := v.([]any)
	var out []string
	for _, c := range l {
		o, _ := c.(pyjson.Object)
		id, _ := memberOf(o, "endpoint_id").(string)
		out = append(out, id)
	}
	return out
}

// timingOf is Python's data.timing: its values when it has §6's six keys
// in order, each a non-negative integer (shaped); else zeros.
func timingOf(v any) (gwauth.Timing, bool) {
	o, ok := v.(pyjson.Object)
	if !ok || len(o) != len(timingKeys) {
		return gwauth.Timing{}, false
	}
	values := make([]int64, len(o))
	for i, m := range o {
		n, isNumber := m.Value.(interface{ String() string })
		if m.Key != timingKeys[i] || !isNumber {
			return gwauth.Timing{}, false
		}
		x, err := strconv.ParseInt(n.String(), 10, 64)
		if err != nil || x < 0 {
			return gwauth.Timing{}, false
		}
		values[i] = x
	}
	return gwauth.Timing{TotalMS: values[0], SpannerRPCs: values[1], KeyLookupMS: values[2], RoutingMS: values[3],
		StoreMS: values[4], PostCommitMS: values[5]}, true
}

// differences are the fields of data on which Go's and Python's differ
// (§11): each top-level member by name, the estimate's float twin apart
// from its microdollars, the candidates' order, each candidate member by
// name, the timing's shape, and the members' order. The async members are
// compared by their bytes (asyncDifferencesOf), and async is what that
// found.
func differences(goData, pyData pyjson.Object, timingShaped bool, async []string) []string {
	var out []string
	add := func(f string) {
		if !slices.Contains(out, "differ."+f) {
			out = append(out, "differ."+f)
		}
	}
	keys := func(o pyjson.Object) []string {
		k := make([]string, len(o))
		for i, m := range o {
			k[i] = m.Key
		}
		return k
	}
	goKeys, pyKeys := keys(goData), keys(pyData)
	for _, k := range union(goKeys, pyKeys) {
		if slices.Contains(asyncMemberKeys, k) {
			continue // by their bytes: async
		}
		g, p := memberOf(goData, k), memberOf(pyData, k)
		switch k {
		case "timing":
			if !timingShaped {
				add("timing")
			}
			continue
		case "estimated_cost":
			if !reflect.DeepEqual(g, p) && reflect.DeepEqual(memberOf(goData, "estimated_cost_microdollars"),
				memberOf(pyData, "estimated_cost_microdollars")) {
				add("estimate_float")
			}
			continue
		case "estimated_cost_microdollars":
			if !reflect.DeepEqual(g, p) {
				add("estimate")
			}
			continue
		case "route_candidates":
			if !reflect.DeepEqual(g, p) {
				candidateDifferences(g, p, add)
			}
			continue
		}
		if !slices.Contains(goKeys, k) || !slices.Contains(pyKeys, k) || !reflect.DeepEqual(g, p) {
			add(k)
		}
	}
	for _, f := range async {
		add(strings.TrimPrefix(f, "differ."))
	}
	if len(out) == 0 && !slices.Equal(goKeys, pyKeys) {
		add("key_order")
	}
	return out
}

func candidateDifferences(g, p any, add func(string)) {
	gl, _ := g.([]any)
	pl, _ := p.([]any)
	if !slices.Equal(candidateIDs(g), candidateIDs(p)) {
		add("candidates")
		return
	}
	for i := range gl {
		go1, _ := gl[i].(pyjson.Object)
		py1, _ := pl[i].(pyjson.Object)
		var gk, pk []string
		for _, m := range go1 {
			gk = append(gk, m.Key)
		}
		for _, m := range py1 {
			pk = append(pk, m.Key)
		}
		for _, k := range union(gk, pk) {
			if !slices.Contains(gk, k) || !slices.Contains(pk, k) || !reflect.DeepEqual(memberOf(go1, k), memberOf(py1, k)) {
				add("route_candidates." + k)
			}
		}
		if !slices.Equal(gk, pk) && slices.Equal(sortedCopy(gk), sortedCopy(pk)) {
			add("route_candidates.key_order")
		}
	}
}

func union(a, b []string) []string {
	out := slices.Clone(a)
	for _, k := range b {
		if !slices.Contains(out, k) {
			out = append(out, k)
		}
	}
	return out
}

func sortedCopy(s []string) []string {
	c := slices.Clone(s)
	sort.Strings(c)
	return c
}

// evict drops what the cache holds for a key Python refused, 401, 402 or
// 403 (by the status alone, its body read or not), or whose workspace it
// paused (503, told by its message): the lookup hash the request named,
// cached or not (read from the body leniently when the request did not
// parse), and its workspace when the cache said which.
func (sh *fullShadow) evict(d Decided, a pyAnswer, body []byte) {
	switch {
	case a.status == http.StatusUnauthorized, a.status == http.StatusPaymentRequired, a.status == http.StatusForbidden:
	case a.status == http.StatusServiceUnavailable && a.why == "" && errorOf(a).message == pauseMessage:
	default:
		return
	}
	lookup := d.LookupHash
	if lookup == "" {
		lookup = lenientLookupHash(body)
	}
	if lookup != "" {
		sh.cfg.Cache.Evict(lookup)
		sh.evictions.Add(1)
		sh.periodEvictions.Add(1)
	}
	if d.Workspace != "" {
		sh.cfg.Cache.EvictWorkspace(d.Workspace)
		sh.evictions.Add(1)
		sh.periodEvictions.Add(1)
	}
}

// lenientLookupHash is the body's api_key_lookup_hash for an eviction when
// the strict parse passed the request: any top-level string of it,
// stripped, printable ASCII, at most 256 bytes; "" otherwise. An eviction
// of a hash the cache does not hold is harmless.
func lenientLookupHash(body []byte) string {
	var b struct {
		Lookup *string `json:"api_key_lookup_hash"`
	}
	if json.Unmarshal(body, &b) != nil || b.Lookup == nil {
		return ""
	}
	s := strings.TrimSpace(*b.Lookup)
	if s == "" || len(s) > 256 || !printableASCII(s) {
		return ""
	}
	return s
}

// sample is one comparable request in SampleEvery, within ReadsPerSecond
// (a bucket of that many tokens refilled at that rate).
func (sh *fullShadow) sample() bool {
	if sh.sampled.Add(1)%int64(sh.cfg.SampleEvery) != 0 {
		return false
	}
	l := &sh.limiter
	l.Lock()
	defer l.Unlock()
	now := time.Now()
	if !l.at.IsZero() {
		l.tokens = min(sh.cfg.ReadsPerSecond, l.tokens+now.Sub(l.at).Seconds()*sh.cfg.ReadsPerSecond)
	}
	l.at = now
	if l.tokens < 1 {
		return false
	}
	l.tokens--
	return true
}

// readRows reads back the rows Python wrote for its authorization and
// compares every column with the rows Go would have written for the same
// decision, staged under Python's ids, instant, envelope and end of life
// (and its minted idempotency key, when the request presented none).
func (sh *fullShadow) readRows(d *Decision, r *fullResult, latency time.Duration) {
	ctx, cancel := context.WithTimeout(context.Background(), rowReadTimeout)
	stored, found, err := sh.cfg.Rows.ReadAuthorizationForCompare(ctx, r.py.authorization)
	cancel()
	switch {
	case err != nil:
		r.rowsOf = "read_failed"
		return
	case !found:
		r.rowsOf = "missing"
		return
	}
	r.rowsOf, r.fields = compareRows(d, stored, r.fields, latency)
}

// mintedKeyForm is str(uuid4()), the form of a key Go mints (and Python).
var mintedKeyForm = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

// compareRows is Python's rows against Go's: the outcome for the rows
// tally, and the fields with any row.<column> appended. Python's values put
// in Go's place are checked as far as Go can: its created_at within the
// request's arrival and its answer (latency) and rowSkew, its minted
// idempotency key of the form Go mints. A row Python's settle or heartbeat
// reached first is compared on the columns those leave alone (moved_on).
func compareRows(d *Decision, stored store.AuthorizationForCompare, fields []string,
	latency time.Duration) (string, []string) {
	a := stored.Authorization
	moved := a.Settled || a.TerminalAt.Valid || a.StartedAt.Valid || a.HeartbeatSeq.Valid || a.FinalizationOutcome.Valid
	payloadTree, err := pyjson.Decode([]byte(a.Payload.StringVal))
	payload, ok := payloadTree.(pyjson.Object)
	if !a.Payload.Valid || err != nil || !ok {
		if !moved {
			return "differ", append(fields, "row.payload")
		}
		payload = nil
	}
	envelope, leased := memberOf(payload, "fastpath_envelope").(string)
	if !leased && !moved {
		// Python took today's path, not the pilot's lease (its owner was
		// busy, say): its rows are not the cohort's.
		return "python_not_leased", fields
	}
	before := len(fields)
	arrival := d.Input.At
	if a.CreatedAt.Before(arrival.Add(-rowSkew)) || a.CreatedAt.After(arrival.Add(latency+rowSkew)) {
		fields = append(fields, "row.created_at_skew")
	}
	key, scopeKnown := d.Input.IdempotencyKey, true
	if !d.Identity.Presented {
		minted, _ := memberOf(payload, "idempotency_key").(string)
		switch {
		case minted == "" && moved:
			// A settle that rewrote the payload took Python's minted key
			// with it: the scope is not Go's to remake.
			scopeKnown = false
		case !mintedKeyForm.MatchString(minted):
			fields = append(fields, "row.idempotency_key_form")
			key = minted
		default:
			key = minted
		}
	}
	eol := a.CreatedAt
	if text, isText := memberOf(payload, "fastpath_end_of_life").(string); isText {
		if eol, err = time.Parse(time.RFC3339Nano, text); err != nil {
			return "differ", append(fields, "row.payload.fastpath_end_of_life")
		}
	} else if !moved {
		return "differ", append(fields, "row.payload.fastpath_end_of_life")
	}
	staged, err := d.Restage(a.AuthorizationID, a.ReservationID.StringVal, key, a.CreatedAt)
	if err != nil {
		return "differ", append(fields, "row.ids")
	}
	composed, err := staged.Finish(envelope, eol)
	if err != nil {
		return "differ", append(fields, "row.payload.fastpath_end_of_life")
	}
	fields = append(fields, authorizationDifferences(composed.Authorization, a, payload, moved)...)
	if !scopeKnown && stored.Reservation != nil {
		composed.Reservation.IdempotencyScope = stored.Reservation.IdempotencyScope.StringVal
	}
	fields = append(fields, reservationDifferences(composed.Reservation, stored.Reservation, latency, moved)...)
	switch {
	case len(fields) > before && moved:
		return "moved_on_differ", fields
	case len(fields) > before:
		return "differ", fields
	case moved:
		return "moved_on", fields
	}
	return "agree", fields
}

func nullString(s *string) any {
	if s == nil {
		return nil
	}
	return *s
}

func ns(v interface{ IsNull() bool }, value any) any {
	if v.IsNull() {
		return nil
	}
	return value
}

// authorizationDifferences: every column of tr_gateway_authorization (the
// 13 typed columns included, which Python reads before the payload's copy
// of each, so a typed column Go would leave NULL that Python fills, or the
// reverse, is caught only here), and the payload member by member.
// With moved (Python's settle or heartbeat reached the row first) only the
// columns those leave alone are compared, and not the payload.
func authorizationDifferences(g gwauth.AuthorizationRow, p store.StoredAuthorization, pyPayload pyjson.Object,
	moved bool) []string {
	var out []string
	cols := []struct {
		name     string
		go_, py_ any
	}{
		{"authorization_id", g.AuthorizationID, p.AuthorizationID},
		{"workspace_id", g.WorkspaceID, p.WorkspaceID},
		{"key_hash", g.KeyHash, p.KeyHash},
		{"reservation_id", g.ReservationID, ns(p.ReservationID, p.ReservationID.StringVal)},
		{"model_id", g.ModelID, p.ModelID},
		{"provider", g.Provider, p.Provider},
		{"usage_type", g.UsageType, p.UsageType},
		{"estimated_microdollars", g.EstimatedMicrodollars, p.EstimatedMicrodollars},
		{"settled", false, p.Settled},
		{"created_at", g.CreatedAt.UnixMicro(), p.CreatedAt.UnixMicro()},
		{"terminal_at", nil, ns(p.TerminalAt, p.TerminalAt.Time)},
		{"idempotency_fingerprint", g.IdempotencyFingerprint, ns(p.IdempotencyFingerprint, p.IdempotencyFingerprint.StringVal)},
		{"finalization_outcome", nil, ns(p.FinalizationOutcome, p.FinalizationOutcome.StringVal)},
		{"finalized_cost_microdollars", nil, ns(p.FinalizedCostMicrodollars, p.FinalizedCostMicrodollars.Int64)},
		{"started_at", nil, ns(p.StartedAt, p.StartedAt.Time)},
		{"heartbeat_seq", nil, ns(p.HeartbeatSeq, p.HeartbeatSeq.Int64)},
		{"heartbeat_at", nil, ns(p.HeartbeatAt, p.HeartbeatAt.Time)},
		{"heartbeat_hash", nil, ns(p.HeartbeatHash, p.HeartbeatHash.StringVal)},
		{"selected_endpoint_id", nil, ns(p.SelectedEndpointID, p.SelectedEndpointID.StringVal)},
		{"delivered_usage", nil, ns(p.DeliveredUsage, p.DeliveredUsage.StringVal)},
		{"pricing_snapshot", nil, ns(p.PricingSnapshot, p.PricingSnapshot.StringVal)},
		{"stage_d_boot_kid", g.StageDBootKid, ns(p.StageDBootKid, p.StageDBootKid.StringVal)},
		{"invocation_nonce", nullString(g.InvocationNonce), ns(p.InvocationNonce, p.InvocationNonce.StringVal)},
		{"gateway_request_id", nil, ns(p.GatewayRequestID, p.GatewayRequestID.StringVal)},
	}
	for _, c := range cols {
		if moved && !settleStable[c.name] {
			continue
		}
		if c.go_ != c.py_ {
			out = append(out, "row."+c.name)
		}
	}
	if !moved && g.Payload != p.Payload.StringVal {
		goTree, _ := pyjson.Decode([]byte(g.Payload))
		goPayload, _ := goTree.(pyjson.Object)
		var keys []string
		for _, m := range goPayload {
			keys = append(keys, m.Key)
		}
		for _, m := range pyPayload {
			keys = append(keys, m.Key)
		}
		named := false
		for _, k := range union(keys, nil) {
			if !reflect.DeepEqual(memberOf(goPayload, k), memberOf(pyPayload, k)) {
				f := "row.payload." + k
				if !slices.Contains(out, f) {
					out = append(out, f)
					named = true
				}
			}
		}
		if !named {
			out = append(out, "row.payload")
		}
	}
	return out
}

// settleStable are the columns of either row that settle, refund and
// heartbeat leave as the authorize wrote them.
var settleStable = map[string]bool{
	"authorization_id": true, "workspace_id": true, "key_hash": true, "reservation_id": true, "model_id": true,
	"provider": true, "usage_type": true, "estimated_microdollars": true, "created_at": true,
	"idempotency_fingerprint": true, "stage_d_boot_kid": true, "invocation_nonce": true,
	"ws_shard": true, "credit_shard": true, "key_shard": true, "hold_usage_type": true, "idempotency_scope": true,
}

// reservationDifferences: every column of tr_reservation; expires_at is
// Python's at least Go's (Python reads its clock again for it, §5) and at
// most the request's latency and rowSkew later. created_at is the commit's
// timestamp on either path (§5), which no reading of Go's names: it is
// compared as present, not by value.
func reservationDifferences(g gwauth.ReservationRow, p *store.StoredReservation, latency time.Duration,
	moved bool) []string {
	if p == nil {
		return []string{"row.reservation.missing"}
	}
	var out []string
	cols := []struct {
		name     string
		go_, py_ any
	}{
		{"reservation_id", g.ReservationID, p.ReservationID},
		{"workspace_id", g.WorkspaceID, ns(p.WorkspaceID, p.WorkspaceID.StringVal)},
		{"key_hash", g.KeyHash, ns(p.KeyHash, p.KeyHash.StringVal)},
		{"ws_shard", int64(0), ns(p.WsShard, p.WsShard.Int64)},
		{"credit_shard", int64(0), p.CreditShard},
		{"key_shard", int64(0), ns(p.KeyShard, p.KeyShard.Int64)},
		{"credit_reserved_micro", int64(0), ns(p.CreditReservedMicro, p.CreditReservedMicro.Int64)},
		{"key_reserved_micro", int64(0), ns(p.KeyReservedMicro, p.KeyReservedMicro.Int64)},
		{"actual_micro", nil, ns(p.ActualMicro, p.ActualMicro.Int64)},
		{"hold_usage_type", g.HoldUsageType, ns(p.HoldUsageType, p.HoldUsageType.StringVal)},
		{"settled_usage_type", nil, ns(p.SettledUsageType, p.SettledUsageType.StringVal)},
		{"authorization_id", g.AuthorizationID, ns(p.AuthorizationID, p.AuthorizationID.StringVal)},
		{"settled", false, p.Settled},
		{"idempotency_scope", g.IdempotencyScope, ns(p.IdempotencyScope, p.IdempotencyScope.StringVal)},
		{"idempotency_fingerprint", g.IdempotencyFingerprint, ns(p.IdempotencyFingerprint, p.IdempotencyFingerprint.StringVal)},
		{"created_at", true, p.CreatedAt.Valid},
		{"terminal_at", nil, ns(p.TerminalAt, p.TerminalAt.Time)},
	}
	for _, c := range cols {
		if moved && !settleStable[c.name] {
			continue
		}
		if c.go_ != c.py_ {
			out = append(out, "row.reservation."+c.name)
		}
	}
	if !p.ExpiresAt.Valid || p.ExpiresAt.Time.Before(g.ExpiresAt) ||
		p.ExpiresAt.Time.After(g.ExpiresAt.Add(latency+rowSkew)) {
		out = append(out, "row.reservation.expires_at")
	}
	return out
}

// record counts a comparison, in its snapshot's and policy's tally and the
// running totals, and logs a critical one at once.
func (sh *fullShadow) record(digest string, r fullResult) {
	sh.mu.Lock()
	defer sh.mu.Unlock()
	t := sh.tallies[digest]
	if t == nil {
		t = newFullTally()
		sh.tallies[digest] = t
	}
	for _, tally := range []*fullTally{t, sh.totals} {
		tally.classes[r.class]++
		for _, f := range r.fields {
			boundedTo(tally.fields, f, maxShadowReasons*4)
		}
		if r.rowsOf != "" {
			tally.rows[r.rowsOf]++
		}
		if r.decided {
			gate := r.gate
			if gate == "" {
				gate = "on"
			}
			tally.decide[gate]++
			boundedTo(tally.cohort, r.class, maxShadowReasons*4)
		}
		if r.readBack {
			tally.readBack++
		}
		for _, w := range r.windows {
			tally.windows[w]++
		}
		if r.stream != "" {
			tally.streams[r.stream]++
		}
		if a := r.async; a.pop != "" {
			tally.async[asyncSeen][a.pop]++
			if a.shape != "" {
				tally.async[asyncShape][a.pop+"."+a.shape]++
			}
			if a.compared != "" {
				tally.async[asyncCompared][a.pop+"."+a.compared]++
			}
			for _, f := range a.fields {
				tally.async[asyncFields][a.pop+"."+f]++
			}
			if a.pythonAdmitted {
				tally.async[asyncPythonAdmitted][a.pop]++
			}
		}
	}
	if sh.races.count(r, time.Now()) && sh.raceLogs < maxDifferLogs {
		sh.raceLogs++
		c := r.race
		sh.logf("fastpath edge decide shadow window race: class=%s key=%s window=%s go=%d reread=%d python=%d gap=%d %s",
			r.class, c.keyHashPrefix, c.window, c.first, c.reread, c.python, c.first-c.python, digest)
	}
	pages := strings.HasPrefix(r.class, ClassWouldAdmit+".") || strings.HasPrefix(r.class, ClassWindowRaceUnconfirmed+".")
	if pages && sh.critical < maxCriticalLogs {
		sh.critical++
		if sh.cfg.Alert != nil {
			sh.cfg.Alert(AlertWouldAdmit, digest, "the edge would have answered an authorize Python refused: "+r.class+
				windowNote(r.windows))
		} else {
			sh.logf("fastpath edge decide shadow CRITICAL: class=%s%s %s", r.class, windowNote(r.windows), digest)
		}
	}
	if len(r.fields) > 0 && sh.differs < maxDifferLogs {
		sh.differs++
		sh.logf("fastpath edge decide shadow differs: class=%s fields=%s rows=%s %s", r.class, strings.Join(r.fields, ","),
			orNone(r.rowsOf), digest)
	}
}

// run logs the counts each period and refreshes the decide copy, until ctx
// ends. It never waits for the requests followed: handlers may still begin
// following when ctx ends (a server shutting down serves its in-flight
// requests); WaitDecideShadow, once they have returned, waits and logs the
// last counts.
func (sh *fullShadow) run(ctx context.Context) {
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		sh.decides.run(ctx)
	}()
	ticker := time.NewTicker(sh.every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			wg.Wait()
			return
		case <-ticker.C:
			sh.flush()
		}
	}
}

// flush logs and resets the period's counts: one INFO line a snapshot and
// policy, and one for the requests not followed.
func (sh *fullShadow) flush() {
	sh.mu.Lock()
	defer sh.mu.Unlock()
	digests := make([]string, 0, len(sh.tallies))
	for d := range sh.tallies {
		digests = append(digests, d)
	}
	sort.Strings(digests)
	for _, d := range digests {
		t := sh.tallies[d]
		would := 0
		for c, n := range t.classes {
			if strings.HasPrefix(c, ClassWouldAdmit+".") {
				would += n
			}
		}
		line := "fastpath edge decide shadow: %s agree_full=%d differ=%d would_admit_python_refused=%d read_back=%d " +
			"classes=%s fields=%s rows=%s decide=%s cohort=%s windows=%s"
		args := []any{d, t.classes[ClassAgreeFull], t.classes[ClassDifferAny], would, t.readBack, jsonOf(t.classes),
			jsonOf(t.fields), jsonOf(t.rows), jsonOf(t.decide), jsonOf(t.cohort), jsonOf(t.windows)}
		for _, name := range asyncCounts {
			line += " " + name + "=%s"
			args = append(args, jsonOf(t.async[name]))
		}
		line += " streams=%s"
		args = append(args, jsonOf(t.streams))
		sh.logf(line, args...)
	}
	// Every count of the line is the period's, the cache's too: its own
	// are totals, so the line logs what they grew by since the last.
	line := "fastpath edge decide shadow: dropped=%d evictions=%d"
	args := []any{sh.dropped.Swap(0), sh.periodEvictions.Swap(0)}
	if c, ok := sh.cfg.Cache.(cacheCounters); ok {
		n, was := c.Counters(), sh.cacheAt
		sh.cacheAt = n
		line += " cache_unrefreshed=%d cache_dropped=%d cache_read_failures=%d"
		args = append(args, n.Unrefreshed-was.Unrefreshed, n.Dropped-was.Dropped, n.ReadFailures-was.ReadFailures)
	}
	sh.logf(line, args...)
	sh.raceAlerts(time.Now())
	sh.tallies, sh.critical, sh.differs, sh.raceLogs = map[string]*fullTally{}, 0, 0, 0
}

// DecideShadowCounts are the full-response shadow's counts since the edge
// started: by class, by field, by row read-back outcome, by the decide
// row's gate and, of the requests Go would decide, by class (Cohort), and
// the answers Python made from a read-back, the requests not followed
// (dropped) and the cache evictions Python's refusals made. Async are the
// async-v1 counts by the line's map name (async_seen, async_compared,
// async_fields, async_shape, async_python_admitted), Windows the window
// checks' (compareWindowed).
type DecideShadowCounts struct {
	Classes, Fields, Rows, Decide, Cohort, Windows map[string]int
	// Streams are the requests passed as body.stream by the Stage D reason
	// the edge reads for them (Decided.StreamStageD).
	Streams            map[string]int
	Async              map[string]map[string]int
	ReadBack           int
	Dropped, Evictions int64
}

// DecideShadowCounts are the counts now; zero with the shadow off.
func (e *Edge) DecideShadowCounts() DecideShadowCounts {
	if e.full == nil {
		return DecideShadowCounts{}
	}
	sh := e.full
	sh.mu.Lock()
	t := sh.totals.clone()
	sh.mu.Unlock()
	return DecideShadowCounts{Classes: t.classes, Fields: t.fields, Rows: t.rows, Decide: t.decide, Cohort: t.cohort,
		Windows: t.windows, Streams: t.streams,
		Async: t.async, ReadBack: t.readBack, Dropped: sh.dropped.Load(), Evictions: sh.evictions.Load()}
}

// DecideShadowOn reports the full-response shadow configured.
func (e *Edge) DecideShadowOn() bool { return e.full != nil }

// WaitDecideShadow waits for every request the full-response shadow
// follows to be compared (at shutdown, after the edge's handlers have
// returned, so no read the shadow makes outlives the store's client).
//
// It is called only once no handler can begin following a request: the
// WaitGroup it waits on is the handlers' to add to. It then logs the
// period's last counts.
func (e *Edge) WaitDecideShadow() {
	if e.full != nil {
		e.full.wg.Wait()
		e.full.flush()
	}
}

// checkWindows is the shadow's window check of a decision for an enforcing
// windowed key (section 8.2 item 1): the decide path's read and evaluation,
// off the request path, within ReadsPerSecond of its own. nil when the
// decision needs none, no reader is configured, or the limiter refuses
// (the comparison is then window_unchecked, as before the read).
func (sh *fullShadow) checkWindows(d Decided, at time.Time) *windowRead {
	dec := d.ForShadow()
	if dec == nil || dec.Windows == nil || sh.cfg.Windows == nil || !sh.windowSample() {
		return nil
	}
	w := readWindows(context.Background(), sh.cfg.Windows, dec, at, time.Now().Add(WindowReadBudget))
	return &w
}

// windowSample is one window read within ReadsPerSecond (a bucket of that
// many tokens refilled at that rate), apart from the row read-backs'.
func (sh *fullShadow) windowSample() bool {
	l := &sh.windowLimiter
	l.Lock()
	defer l.Unlock()
	now := time.Now()
	if !l.at.IsZero() {
		l.tokens = min(sh.cfg.ReadsPerSecond, l.tokens+now.Sub(l.at).Seconds()*sh.cfg.ReadsPerSecond)
	}
	l.at = now
	if l.tokens < 1 {
		return false
	}
	l.tokens--
	return true
}

// compareWindowed is compareFull with the window check's verdict, when the
// shadow made one (section 8.2 item 3). Go would admit: Python's 200 is
// compared as any, and its window refusal, no longer window_unchecked, is
// window_race when Python's evidence shows a settle between the two reads
// (windowRace), else the critical would-admit. Go would pass (any
// key_window.* reason): Python's 200 is python_answered_go_passed by that
// reason (benign), its refusal passed by it. Either way the counts add the
// read, its latency, the verdict and how Python's RateLimit-* headers
// compare with Go's limit, remaining and reset (windowFields).
func compareWindowed(d Decided, a pyAnswer, w *windowRead) fullResult {
	r := compareFull(d, a)
	if w == nil {
		if dec := d.ForShadow(); dec != nil && dec.Windows != nil {
			r.windows = []string{"read.none"}
		}
		return r
	}
	v := w.verdict
	r.windows = append([]string{"read.made", windowLatency(w.took), windowOutcome(v)}, windowFields(v, a)...)
	unchecked := strings.HasPrefix(r.class, ClassWindowUnchecked+".")
	switch {
	case v.Admit() && unchecked:
		class := ClassWouldAdmit
		if r.race = windowRace(d.ForShadow(), w, a); r.race != nil {
			// Unconfirmed until the re-read settles it (confirmRace).
			class, r.race.unconfirmed = ClassWindowRaceUnconfirmed, "unchecked"
		}
		r.class = class + strings.TrimPrefix(r.class, ClassWindowUnchecked)
	case v.Admit():
	case unchecked || strings.HasPrefix(r.class, ClassWouldAdmit+"."):
		r.class = ClassGoPassed + "." + v.Pass
	case a.status == http.StatusOK:
		r = fullResult{class: ClassPythonAnsweredGoPassed + "." + v.Pass, decided: true, gate: r.gate,
			async: r.async, windows: r.windows}
	}
	return r
}

// windowRefused reads the window of Python's 429 from its message
// (spend_windows.spend_window_limit_error_message), "" for another.
var windowRefused = regexp.MustCompile(`^API key (daily|weekly|monthly) spend limit exceeded; `)

// windowFields compares Go's window decision (when it computed Python's)
// with Python's, read from its 200's or 429's RateLimit-* headers and, on a
// 429, its message's window: window.agree, or window.differ.limit,
// window.differ.remaining.<size of the difference>, window.differ.reset
// (window.reset_1s for one second, the two clocks), window.differ.window;
// window.python_none when Python's answer carries no decision. Where the
// two read the same limit and remaining, window.same_inputs.agree or
// .disagree says whether the verdicts agree (section 8.3: one disagreement
// is a parity bug).
func windowFields(v keywindow.Verdict, a pyAnswer) []string {
	if !v.Decided || (a.status != http.StatusOK && a.status != http.StatusTooManyRequests) {
		return nil
	}
	var py [3]int64
	for i, h := range a.rateLimit {
		n, err := strconv.ParseInt(h, 10, 64)
		if err != nil || len(h) > 20 {
			return []string{"window.python_none"}
		}
		py[i] = n
	}
	var out []string
	if py[0] != v.Limit {
		out = append(out, "window.differ.limit")
	}
	if d := py[1] - v.Remaining; d != 0 {
		size := "gt_1000000"
		switch {
		case -1000 <= d && d <= 1000:
			size = "le_1000"
		case -1_000_000 <= d && d <= 1_000_000:
			size = "le_1000000"
		}
		out = append(out, "window.differ.remaining."+size)
	}
	switch d := py[2] - v.ResetSeconds; {
	case d == 1 || d == -1:
		out = append(out, "window.reset_1s")
	case d != 0:
		out = append(out, "window.differ.reset")
	}
	if a.status == http.StatusTooManyRequests {
		if m := windowRefused.FindStringSubmatch(errorOf(a).message); m == nil || m[1] != string(v.Window) {
			out = append(out, "window.differ.window")
		}
	}
	if py[0] == v.Limit && py[1] == v.Remaining {
		if (a.status == http.StatusOK) == v.Allowed {
			out = append(out, "window.same_inputs.agree")
		} else {
			out = append(out, "window.same_inputs.disagree")
		}
	}
	if len(out) == 0 || (len(out) == 1 && out[0] == "window.same_inputs.agree") {
		out = append(out, "window.agree")
	}
	return out
}

// windowNote is the window counts a critical line adds, "" for none.
func windowNote(windows []string) string {
	if len(windows) == 0 {
		return ""
	}
	return " windows=" + strings.Join(windows, ",")
}

// raceCandidate is Python's window refusal of a request Go read and would
// admit, whose own evidence says Python's read saw more spent than Go's
// (windowRace): the window, Go's remaining there from its first read,
// Python's (RateLimit-Remaining), Go's from the re-read that confirms or
// refutes it (confirmRace; -1 before), why it stayed unconfirmed, and the
// key hash's first characters, for the line that traces it.
type raceCandidate struct {
	window          keywindow.Window
	first, python   int64
	reread          int64
	unconfirmed     string
	keyHashPrefix   string
	refuted, landed bool
}

// keyHashPrefixLen is how much of a key's hash a race's line carries: enough
// to tell one key's repeats, not the hash.
const keyHashPrefixLen = 8

// windowRace is Python's window refusal of a request Go read and would admit
// as a race candidate, or nil: the 429's message names a window Go holds a
// limit for, its RateLimit-Limit is that limit, and its RateLimit-Remaining
// is below the remaining Go's rows leave in that window at the request's
// instant. That alone proves nothing (a refusal of what Go admits always has
// Python's remaining below Go's, a Go defect too), so the candidate pages
// until a re-read confirms it (confirmRace).
func windowRace(dec *Decision, w *windowRead, a pyAnswer) *raceCandidate {
	m := windowRefused.FindStringSubmatch(errorOf(a).message)
	if m == nil || dec == nil || dec.Windows == nil {
		return nil
	}
	window := keywindow.Window(m[1])
	limit := dec.Windows.Limits.Get(window)
	if limit == nil {
		return nil
	}
	pyLimit, err1 := strconv.ParseInt(a.rateLimit[0], 10, 64)
	pyRemaining, err2 := strconv.ParseInt(a.rateLimit[1], 10, 64)
	if err1 != nil || err2 != nil || pyLimit != *limit {
		return nil
	}
	ours, ok := windowRemaining(dec, window, w.rows, w.at)
	if !ok || pyRemaining >= ours {
		return nil
	}
	prefix := dec.KeyHash
	if len(prefix) > keyHashPrefixLen {
		prefix = prefix[:keyHashPrefixLen]
	}
	return &raceCandidate{window: window, first: ours, python: pyRemaining, reread: -1, keyHashPrefix: prefix}
}

// windowRemaining is what rows leave in one window of the decision's key at
// at: Evaluate over that window's limit alone (the other windows' mirrors
// make it a drift pass, which changes no figure). ok is false when the rows
// do not give it (not exactly the key's shards, an overflow).
func windowRemaining(dec *Decision, window keywindow.Window, rows []keywindow.Row, at time.Time) (int64, bool) {
	limit := dec.Windows.Limits.Get(window)
	if limit == nil {
		return 0, false
	}
	var one keywindow.Limits
	switch window {
	case keywindow.Daily:
		one.Daily = limit
	case keywindow.Weekly:
		one.Weekly = limit
	default:
		one.Monthly = limit
	}
	v := keywindow.Evaluate(one, dec.Windows.Shards, rows, dec.Estimate, at)
	return v.Remaining, v.Decided && v.Window == window
}

// confirmRace settles a race candidate, after Python's answer, with one more
// strong read of the key's rows (within the window limiter, at most
// WindowReadBudget), evaluated in the candidate's window at the request's
// instant. Usage only grows inside a window, so a settle that landed
// between the two first reads shows in the re-read: Go's fresh remaining at
// or below Python's confirms the race (ClassWindowRace, counted, never
// paged). Still above Python's, Go's read missed what Python saw: a defect,
// the critical would-admit. No re-read (the limiter, a failed or late read,
// rows that do not give the window) or a window that has reset since (its
// usage is another window's now) leaves it unconfirmed
// (ClassWindowRaceUnconfirmed), which pages as a would-admit does: it
// cannot be told from a defect.
func (sh *fullShadow) confirmRace(dec *Decision, w *windowRead, r *fullResult) {
	c := r.race
	if c == nil {
		return
	}
	suffix := strings.TrimPrefix(r.class, ClassWindowRaceUnconfirmed)
	switch {
	case sh.cfg.Windows == nil || !sh.windowSample():
		c.unconfirmed = "limiter"
	default:
		again := readWindows(context.Background(), sh.cfg.Windows, dec, w.at, time.Now().Add(WindowReadBudget))
		rem, ok := windowRemaining(dec, c.window, again.rows, w.at)
		switch {
		case again.verdict.Pass == keywindow.PassUnread:
			c.unconfirmed = "unread"
		case !sh.clock().Before(keywindow.ResetsAt(c.window, w.at)):
			c.unconfirmed = "reset"
		case !ok:
			c.unconfirmed = "rows"
		default:
			c.reread = rem
			c.landed = rem <= c.python
			c.refuted = !c.landed
		}
	}
	switch {
	case c.landed:
		r.class = ClassWindowRace + suffix
		r.windows = append(r.windows, "race.confirmed")
	case c.refuted:
		r.class = ClassWouldAdmit + suffix
		r.windows = append(r.windows, "race.refuted")
	default:
		r.windows = append(r.windows, "race.unconfirmed."+c.unconfirmed)
	}
}

// AlertWindowRace is the alert kind of window races past what a race
// explains (section 8.3): over the last hour, confirmed races
// (ClassWindowRace) above RaceRateShare of the window reads the shadow made
// (at least RaceRateMin of them), or one key raced more than RaceKeyRepeats
// times. Not the critical page each would-admit is: each race was confirmed
// by a re-read, so this is a rate to explain (a hot key, a settle path
// slower than usual), not a refusal Go would have missed.
const AlertWindowRace = "edge_window_race"

// The rolling hour of AlertWindowRace.
const (
	RaceRateShare  = 0.0005
	RaceRateMin    = 3
	RaceKeyRepeats = 3
	raceWindow     = time.Hour
	// maxRaceKeys bounds the races per key the hour remembers.
	maxRaceKeys = 10_000
)

// raceWatch is the rolling hour AlertWindowRace reads: each past period's
// window reads and confirmed races, this period's, and each confirmed race's
// key-hash prefix and time. Held under the shadow's mu.
type raceWatch struct {
	periods      []racePeriod
	reads, races int
	keys         []raceKey
}

type racePeriod struct {
	at           time.Time
	reads, races int
}

type raceKey struct {
	at  time.Time
	key string
}

// count adds a comparison at at: a window read made, a confirmed race. It reports
// whether the comparison was a race candidate at all (confirmed, refuted or
// unconfirmed), which the race line traces.
func (w *raceWatch) count(r fullResult, at time.Time) bool {
	if slices.Contains(r.windows, "read.made") {
		w.reads++
	}
	if r.race == nil {
		return false
	}
	if strings.HasPrefix(r.class, ClassWindowRace+".") {
		w.races++
		if len(w.keys) < maxRaceKeys {
			w.keys = append(w.keys, raceKey{at, r.race.keyHashPrefix})
		}
	}
	return true
}

// raceAlerts closes the period at now and raises AlertWindowRace for the hour
// to now: its race rate, and each key past its repeats.
func (sh *fullShadow) raceAlerts(now time.Time) {
	w := &sh.races
	w.periods = append(w.periods, racePeriod{now, w.reads, w.races})
	w.reads, w.races = 0, 0
	since := now.Add(-raceWindow)
	for len(w.periods) > 0 && !w.periods[0].at.After(since) {
		w.periods = w.periods[1:]
	}
	for len(w.keys) > 0 && !w.keys[0].at.After(since) {
		w.keys = w.keys[1:]
	}
	reads, races := 0, 0
	for _, p := range w.periods {
		reads, races = reads+p.reads, races+p.races
	}
	var what []string
	if races >= RaceRateMin && float64(races) > RaceRateShare*float64(reads) {
		what = append(what, fmt.Sprintf("%d window races of %d window reads in the last hour, above %.2f%%", races,
			reads, RaceRateShare*100))
	}
	perKey := map[string]int{}
	for _, k := range w.keys {
		perKey[k.key]++
	}
	for _, key := range slices.Sorted(maps.Keys(perKey)) {
		if perKey[key] > RaceKeyRepeats {
			what = append(what, fmt.Sprintf("key %s raced %d times in the last hour", key, perKey[key]))
		}
	}
	if len(what) == 0 {
		return
	}
	message := "the edge's window checks raced Python's past what a race explains: " + strings.Join(what, "; ")
	if sh.cfg.Alert != nil {
		sh.cfg.Alert(AlertWindowRace, "edge decide shadow", message)
		return
	}
	sh.logf("fastpath edge decide shadow ALERT %s: %s", AlertWindowRace, message)
}
