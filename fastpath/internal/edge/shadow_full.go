package edge

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"net/http"
	"reflect"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
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
)

const (
	// DefaultDecideShadowJobs bounds the requests the shadow follows at
	// once, each from its arrival to Python's answer; past it a request is
	// not followed (dropped).
	DefaultDecideShadowJobs = 512
	// DefaultRowSampleEvery and DefaultRowReadsPerSecond bound the row
	// read-backs (§11): one comparable request in 20, at most 5 a second.
	DefaultRowSampleEvery    = 20
	DefaultRowReadsPerSecond = 5
	// rowReadTimeout bounds one read-back.
	rowReadTimeout = 2 * time.Second
	// defaultShadowWait is how long a followed request waits for Python's
	// answer: past the enclave's 25 s per attempt.
	defaultShadowWait = 30 * time.Second
	// maxCriticalLogs bounds the critical lines a period logs.
	maxCriticalLogs = 20
)

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
}

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

	limiter struct {
		sync.Mutex
		tokens float64
		at     time.Time
	}

	mu       sync.Mutex
	tallies  map[string]*fullTally
	totals   *fullTally
	critical int
	differs  int
}

// fullTally is one snapshot's and policy's counts: the classes, the
// fields that differ (differ.<field>, row.<column>), the row read-backs by
// outcome, the decide row's gate for the requests Go would decide, and the
// answers Python made from a read-back.
type fullTally struct {
	classes, fields, rows, decide map[string]int
	readBack                      int
}

func newFullTally() *fullTally {
	return &fullTally{classes: map[string]int{}, fields: map[string]int{}, rows: map[string]int{}, decide: map[string]int{}}
}

func (t *fullTally) clone() *fullTally {
	return &fullTally{classes: clonedCounts(t.classes), fields: clonedCounts(t.fields), rows: clonedCounts(t.rows),
		decide: clonedCounts(t.decide), readBack: t.readBack}
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

// pyAnswer is Python's answer as the shadow read it: its status, its
// bytes (decoded from gzip), or why it was not read.
type pyAnswer struct {
	status int
	body   []byte
	why    string
}

// fullJob is one followed request: what the pipeline reads, held at its
// arrival, and Python's answer once it is in.
type fullJob struct {
	in     DecideInputs
	digest string
	held   int64
	answer chan pyAnswer
	tap    *tap
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
	job := &fullJob{held: int64(len(body)), answer: make(chan pyAnswer, 1), in: DecideInputs{
		Request: DecideRequest{At: at, Header: r.Header.Clone(), Body: body, Boot: boot},
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
// hands it to the job once the proxy has closed it.
func (sh *fullShadow) tapFull(resp *http.Response, job *fullJob) {
	status, encoding := resp.StatusCode, resp.Header.Get("Content-Encoding")
	resp.Body = &tap{rc: resp.Body, sh: sh, limit: maxShadowResponse, length: resp.ContentLength, onClose: func(t *tap) {
		raw, complete, spent := t.bytes()
		a := pyAnswer{status: status}
		switch {
		case spent:
			a.why = "budget"
		case !complete:
			a.why = "bound"
		default:
			a.body, a.why = decoded(encoding, raw)
		}
		job.tap = t
		job.deliver(a)
	}}
}

// decoded is Python's answer's bytes as JSON, gunzipped when Python
// compressed them (decision 13: Go never does).
func decoded(encoding string, raw []byte) ([]byte, string) {
	switch strings.ToLower(strings.TrimSpace(encoding)) {
	case "", "identity":
		return raw, ""
	case "gzip":
		zr, err := gzip.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, "encoding"
		}
		out, err := io.ReadAll(io.LimitReader(zr, maxShadowResponse+1))
		if err != nil || len(out) > maxShadowResponse {
			return nil, "bound"
		}
		return out, ""
	}
	return nil, "encoding"
}

// follow decides the request, waits for Python's answer, compares, evicts
// what Python's refusal says is behind, and reads back the rows when
// sampled; then gives back its job and its bytes.
func (sh *fullShadow) follow(job *fullJob) {
	defer sh.wg.Done()
	defer func() { <-sh.jobs }()
	decided := sh.safeDecide(job.in)
	var a pyAnswer
	wait := time.NewTimer(sh.cfg.Wait)
	select {
	case a = <-job.answer:
		wait.Stop()
	case <-wait.C:
		a = pyAnswer{why: "wait"}
	}
	r := sh.safeCompare(decided, a)
	sh.evict(decided, a)
	if r.rows && sh.sample() {
		sh.readRows(decided.Decision, &r)
	}
	sh.record(job.digest, r)
	sh.release(job.held + job.tap.finish())
}

func (sh *fullShadow) safeDecide(in DecideInputs) (d Decided) {
	defer func() {
		if recover() != nil {
			d = Decided{Pass: "shadow.decide_failed"}
		}
	}()
	return Decide(in)
}

func (sh *fullShadow) safeCompare(d Decided, a pyAnswer) (r fullResult) {
	defer func() {
		if recover() != nil {
			r = fullResult{class: ClassUnread + ".compare_failed"}
		}
	}()
	return compareFull(d, a)
}

// fullResult is one comparison: its class, the fields that differ, whether
// Python answered from a read-back, the decide row's gate (when Go would
// decide), whether the rows are worth reading back, and Python's ids for
// that read.
type fullResult struct {
	class    string
	fields   []string
	readBack bool
	gate     string
	decided  bool
	rows     bool
	py       pythonIDs
	rowsOf   string
}

// pythonIDs are Python's per-request values from its 200.
type pythonIDs struct {
	authorization, reservation string
}

// compareFull is the comparison of what Go would have answered with what
// Python answered.
func compareFull(d Decided, a pyAnswer) fullResult {
	if a.why != "" {
		return fullResult{class: ClassUnread + "." + a.why}
	}
	refusal := errorOf(a)
	if d.Decision == nil {
		reason := d.Pass
		switch {
		case strings.HasPrefix(reason, "cache."):
			return fullResult{class: ClassStale + "." + strings.TrimPrefix(reason, "cache.")}
		case a.status == http.StatusOK:
			return fullResult{class: ClassPythonAnsweredGoPassed + "." + reason}
		}
		return fullResult{class: ClassGoPassed + "." + reason}
	}
	r := fullResult{decided: true, gate: d.DecideGate}
	switch {
	case a.status == http.StatusOK:
		return compareAnswer(d.Decision, a.body, r)
	case a.status == http.StatusConflict && refusal.kind == "conflict":
		// Another body under the key: Go's insert would have found the
		// scope taken and passed (insert.scope_taken).
		r.class = ClassGoPassed + ".insert.scope_taken"
	case a.status == http.StatusServiceUnavailable && refusal.message == unavailableMessage:
		r.class = ClassPythonUnavailable
	case a.status >= 500 && !(a.status == http.StatusServiceUnavailable && refusal.message == pauseMessage):
		r.class = ClassPythonError + "." + strconv.Itoa(a.status)
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
// replay is the scope taken (Go's insert would have passed).
func compareAnswer(d *Decision, raw []byte, r fullResult) fullResult {
	tree, err := pyjson.Decode(raw)
	top, ok := tree.(pyjson.Object)
	data, isData := memberOf(top, "data").(pyjson.Object)
	if err != nil || !ok || !isData || len(top) != 1 {
		r.class, r.fields = ClassDifferAny, []string{"differ.shape"}
		return r
	}
	if replay, _ := memberOf(data, "idempotent_replay").(bool); replay {
		r.class = ClassPythonAnsweredGoPassed + ".insert.scope_taken"
		return r
	}
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
	inserted, readBack := composed.Render(timing, false), composed.Render(timing, true)
	switch {
	case len(r.fields) == 0 && shaped && bytes.Equal(inserted, raw):
		r.class = ClassAgreeFull
		return r
	case len(r.fields) == 0 && shaped && bytes.Equal(readBack, raw):
		r.class, r.readBack = ClassAgreeFull, true
		return r
	}
	goTree, _ := pyjson.Decode(inserted)
	goTop, _ := goTree.(pyjson.Object)
	goData, _ := memberOf(goTop, "data").(pyjson.Object)
	r.fields = append(r.fields, differences(goData, data, shaped)...)
	if len(r.fields) == 0 {
		r.fields = []string{"differ.bytes"}
	}
	r.class = ClassDifferAny
	return r
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
// name, the timing's shape, and the members' order.
func differences(goData, pyData pyjson.Object, timingShaped bool) []string {
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
	if len(out) == 0 && !slices.Equal(goKeys, pyKeys) {
		add("key_order")
	}
	return out
}

func candidateDifferences(g, p any, add func(string)) {
	gl, _ := g.([]any)
	pl, _ := p.([]any)
	ids := func(l []any) []string {
		var out []string
		for _, c := range l {
			o, _ := c.(pyjson.Object)
			id, _ := memberOf(o, "endpoint_id").(string)
			out = append(out, id)
		}
		return out
	}
	if !slices.Equal(ids(gl), ids(pl)) {
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
// 403, or whose workspace it paused (503): the lookup hash the request
// named, read or not, and its workspace when the cache said which.
func (sh *fullShadow) evict(d Decided, a pyAnswer) {
	switch {
	case a.why != "":
		return
	case a.status == http.StatusUnauthorized, a.status == http.StatusPaymentRequired, a.status == http.StatusForbidden:
	case a.status == http.StatusServiceUnavailable && errorOf(a).message == pauseMessage:
	default:
		return
	}
	if d.LookupHash != "" {
		sh.cfg.Cache.Evict(d.LookupHash)
		sh.evictions.Add(1)
	}
	if d.Workspace != "" {
		sh.cfg.Cache.EvictWorkspace(d.Workspace)
		sh.evictions.Add(1)
	}
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
func (sh *fullShadow) readRows(d *Decision, r *fullResult) {
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
	r.rowsOf, r.fields = compareRows(d, stored, r.fields)
}

// compareRows is Python's rows against Go's: the outcome for the rows
// tally, and the fields with any row.<column> appended.
func compareRows(d *Decision, stored store.AuthorizationForCompare, fields []string) (string, []string) {
	a := stored.Authorization
	if a.Settled || a.TerminalAt.Valid || a.StartedAt.Valid || a.HeartbeatSeq.Valid || a.FinalizationOutcome.Valid {
		// Python's settle or heartbeat reached the row first: its columns
		// are no longer the authorize's.
		return "moved_on", fields
	}
	payloadTree, err := pyjson.Decode([]byte(a.Payload.StringVal))
	payload, ok := payloadTree.(pyjson.Object)
	if !a.Payload.Valid || err != nil || !ok {
		return "differ", append(fields, "row.payload")
	}
	envelope, leased := memberOf(payload, "fastpath_envelope").(string)
	if !leased {
		// Python took today's path, not the pilot's lease (its owner was
		// busy, say): its rows are not the cohort's.
		return "python_not_leased", fields
	}
	eolText, _ := memberOf(payload, "fastpath_end_of_life").(string)
	eol, err := time.Parse(time.RFC3339Nano, eolText)
	if err != nil {
		return "differ", append(fields, "row.payload.fastpath_end_of_life")
	}
	key := d.Input.IdempotencyKey
	if !d.Identity.Presented {
		key, _ = memberOf(payload, "idempotency_key").(string)
	}
	staged, err := d.Restage(a.AuthorizationID, a.ReservationID.StringVal, key, a.CreatedAt)
	if err != nil {
		return "differ", append(fields, "row.ids")
	}
	composed, err := staged.Finish(envelope, eol)
	if err != nil {
		return "differ", append(fields, "row.payload.fastpath_end_of_life")
	}
	before := len(fields)
	fields = append(fields, authorizationDifferences(composed.Authorization, a, payload)...)
	fields = append(fields, reservationDifferences(composed.Reservation, stored.Reservation)...)
	if len(fields) > before {
		return "differ", fields
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
func authorizationDifferences(g gwauth.AuthorizationRow, p store.StoredAuthorization, pyPayload pyjson.Object) []string {
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
		if c.go_ != c.py_ {
			out = append(out, "row."+c.name)
		}
	}
	if g.Payload != p.Payload.StringVal {
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

// reservationDifferences: every column of tr_reservation; expires_at is
// Python's at least Go's (Python reads its clock again for it, §5).
func reservationDifferences(g gwauth.ReservationRow, p *store.StoredReservation) []string {
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
		{"created_at", g.CreatedAt.UnixMicro(), ns(p.CreatedAt, p.CreatedAt.Time.UnixMicro())},
		{"terminal_at", nil, ns(p.TerminalAt, p.TerminalAt.Time)},
	}
	for _, c := range cols {
		if c.go_ != c.py_ {
			out = append(out, "row.reservation."+c.name)
		}
	}
	if !p.ExpiresAt.Valid || p.ExpiresAt.Time.Before(g.ExpiresAt) {
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
		}
		if r.readBack {
			tally.readBack++
		}
	}
	if strings.HasPrefix(r.class, ClassWouldAdmit+".") && sh.critical < maxCriticalLogs {
		sh.critical++
		sh.logf("fastpath edge decide shadow CRITICAL: class=%s %s", r.class, digest)
	}
	if len(r.fields) > 0 && sh.differs < maxDifferLogs {
		sh.differs++
		sh.logf("fastpath edge decide shadow differs: class=%s fields=%s rows=%s %s", r.class, strings.Join(r.fields, ","),
			orNone(r.rowsOf), digest)
	}
}

// run logs the counts each period and refreshes the decide copy, until ctx
// ends; then waits for the requests it follows.
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
			sh.flush()
			return
		case <-ticker.C:
			sh.flush()
		}
	}
}

// wait waits for every request the shadow follows to be compared.
func (sh *fullShadow) wait() { sh.wg.Wait() }

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
		sh.logf("fastpath edge decide shadow: %s agree_full=%d differ=%d would_admit_python_refused=%d read_back=%d "+
			"classes=%s fields=%s rows=%s decide=%s", d, t.classes[ClassAgreeFull], t.classes[ClassDifferAny], would,
			t.readBack, jsonOf(t.classes), jsonOf(t.fields), jsonOf(t.rows), jsonOf(t.decide))
	}
	if dropped := sh.dropped.Swap(0); dropped > 0 {
		sh.logf("fastpath edge decide shadow: dropped=%d", dropped)
	}
	sh.tallies, sh.critical, sh.differs = map[string]*fullTally{}, 0, 0
}

// DecideShadowCounts are the full-response shadow's counts since the edge
// started: by class, by field, by row read-back outcome and by the decide
// row's gate, and the answers Python made from a read-back, the requests
// not followed (dropped) and the cache evictions Python's refusals made.
type DecideShadowCounts struct {
	Classes, Fields, Rows, Decide map[string]int
	ReadBack                      int
	Dropped, Evictions            int64
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
	return DecideShadowCounts{Classes: t.classes, Fields: t.fields, Rows: t.rows, Decide: t.decide, ReadBack: t.readBack,
		Dropped: sh.dropped.Load(), Evictions: sh.evictions.Load()}
}

// DecideShadowOn reports the full-response shadow configured.
func (e *Edge) DecideShadowOn() bool { return e.full != nil }

// WaitDecideShadow waits for every request the full-response shadow
// follows to be compared (at shutdown, after the edge's handlers have
// returned, so no read the shadow makes outlives the store's client).
func (e *Edge) WaitDecideShadow() {
	if e.full != nil {
		e.full.wait()
	}
}
