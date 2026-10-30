package edge

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"maps"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"google.golang.org/grpc/codes"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The decide path (docs/design/fast-admission-e2-decide.md §1, G11;
// -edge-decide, off by default): for an authorize the edge has read, the
// decide pipeline (decide.go) over what the edge holds at its arrival, and
// for a decision Admit hands out, the in-flight limit of its lookup hash,
// the front door's admit within the decide budget, the two rows' insert and
// the answer. Go answers only a 200 whose rows are ours (inserted, or read
// back and found ours) and Python's exact 503 for an insert whose outcome
// is not known; everything else passes to Python unchanged, after a refund
// of the hold if one was made and nothing of ours can have landed.
//
// Everything after the decision runs detached from the request's context:
// an enclave that hangs up neither cancels an admit whose hold would then
// be left behind nor turns a commit into Canceled. Each step has its own
// budget instead (decision 12): the admit's ends DecideBudget after the
// request's arrival, the insert's and its one read-back are the store's
// (store.InsertDeadline, store.ReadBackDeadline), and a refund runs on its
// own RefundBudget, never delaying the pass. No log line carries a body, an
// id, a key or an envelope.

// The §3 reasons the decide path names after the pipeline's.
const (
	PassInFlightKey      = "inflight.key"
	PassOwnerOff         = "owner.off"
	PassOwnerBusy        = "owner.busy"
	PassOwnerInvalid     = "owner.invalid"
	PassOwnerFailed      = "owner.failed"
	PassOwnerUnreachable = "owner.unreachable"
	PassBudgetSpent      = "budget.spent"
	PassInsertScopeTaken = "insert.scope_taken"
	PassInsertFailed     = "insert.failed"
)

// The budgets of decision 12. With the store's, an answer comes at most
// DecideBudget + store.InsertDeadline + store.ReadBackDeadline (3.5 s) after
// the request's arrival, and a pass is forwarded no later; a test fails if
// any of them grows (TestTheBudgetsAreTheContracts).
const (
	// DecideBudget is the deadline, from the request's arrival, of the
	// context the front door's admit gets; past it the request passes.
	DecideBudget = 1500 * time.Millisecond
	// RefundBudget bounds a refund, which runs beside the pass.
	RefundBudget = time.Second
	// WindowReadBudget bounds an enforcing windowed key's window read
	// (docs/design/fast-admission-e2-key-windows.md section 4.2), which
	// also ends with the decide budget: past either the request passes
	// (key_window.unread), so the budgets above are unchanged.
	WindowReadBudget = 300 * time.Millisecond
)

// The decide path's alert kinds (the fastpath_alert label of the service's
// alert line; a policy each in infra/fastpath_alerts.tf, and a row each in
// docs/runbooks/fastpath-alerts.md). Each is raised once a period at most,
// when the period's counts are logged.
const (
	// AlertUnavailable: the period's answers included decision 1's 503, an
	// insert whose outcome is not known; the message counts them by the
	// insert's gRPC code, which tells a clock ahead of Spanner's
	// (failed_precondition, §8.4) and a Spanner incident (unavailable,
	// deadline_exceeded) from a commit whose answer was simply lost.
	AlertUnavailable = "edge_decide_unavailable"
	// AlertUnavailableSustained: unavailable answers in SustainedPeriods
	// periods with no 200 between them (a period that answered nothing
	// neither adds to the run nor ends it, so sparse traffic is seen).
	AlertUnavailableSustained = "edge_decide_unavailable_sustained"
	// AlertSlowPass: a forwarded pass spent more than SlowAtEdge at the edge
	// before it was forwarded, or Python's answer to it came more than
	// SlowInAll after its arrival (decision 12).
	AlertSlowPass = "edge_decide_pass_slow"
	// AlertWindowUnread: more than WindowUnreadShare of the period's window
	// reads (at least WindowUnreadMin of them) failed or ran past their
	// deadline, so those windowed keys passed to Python (key_window.unread).
	AlertWindowUnread = "edge_decide_window_unread"
)

const (
	// SustainedPeriods is how many periods (a minute each) with an
	// unavailable answer and no 200 between them make
	// AlertUnavailableSustained: more than one commit's lost answer, a cause
	// that persists.
	SustainedPeriods = 3
	// SlowAtEdge and SlowInAll are decision 12's: what a forwarded pass may
	// spend at the edge, and the enclave's 25 s an attempt.
	SlowAtEdge = time.Second
	SlowInAll  = 25 * time.Second
	// WindowUnreadShare and WindowUnreadMin make AlertWindowUnread: ten
	// times the rollout gate's 0.1% (key-windows section 8.3), and not for
	// one or two slow reads in a quiet minute.
	WindowUnreadShare = 0.01
	WindowUnreadMin   = 3
)

// refundMoney is a refund's money fields, Python's
// _fastpath_release_after_failure's (gateway.py).
var refundMoney = []byte(`{"charge":0,"estimate":0}`)

// Inserter writes an admitted decision's two rows and reports what the
// commit, and its one read-back, prove (store.Store.InsertLeasedAuthorization).
type Inserter interface {
	InsertLeasedAuthorization(ctx context.Context, a gwauth.AuthorizationRow,
		r gwauth.ReservationRow) (store.InsertOutcome, codes.Code)
}

// DecideConfig turns the decide path on: the key and workspace cache it
// reads (and evicts on Python's 401, 402, 403 or pause 503 to a request it
// passed), the decide table it copies, and the rows it inserts. It needs the
// routing snapshot and the edge policy configured, and answers nothing
// until the node's admitter is bound (Config.Admitter).
type DecideConfig struct {
	Cache   KeyCache
	Decides DecideReader
	Rows    Inserter
	// Windows reads an enforcing windowed key's tr_key_limit rows; nil
	// passes every such key (key_window.unread).
	Windows KeyWindowReader
	// Mint makes the ids the edge mints; nil funcs are gwauth's random
	// ones. A test pins them.
	Mint Mint
	// Alert raises an alert of a kind (the service's alert line, labelled
	// fastpath_alert); nil logs it.
	Alert func(kind, subject, what string)
}

// decider is the decide path's state: the decide copy, the in-flight
// counts, the refunds still running and the counts it logs.
type decider struct {
	cfg      DecideConfig
	logf     func(string, ...any)
	every    time.Duration
	decides  *decideCopy
	inflight inFlightKeys
	refunds  sync.WaitGroup
	// slowAtEdge and slowInAll are SlowAtEdge and SlowInAll; a test
	// shortens them.
	slowAtEdge, slowInAll time.Duration

	mu sync.Mutex
	// total is the counts since the edge started, period the period's;
	// unavailableRun counts the periods with an unavailable answer since
	// the last with a 200 and none.
	total, period  decideTally
	unavailableRun int
}

// decideTally is the decide path's counts: the requests answered (of which
// read back, the 200 after a read-back), the 503s of an outcome not known
// by the insert's gRPC code, the refunds sent and failed, the requests
// passed by §3 reason, the forwarded passes slow at the edge (at_edge) or
// in all (in_all), and the window checks (windows: reads made, unread,
// latency.<bucket>, verdict.<outcome>).
type decideTally struct {
	answered, readBack, refunded, refundFailed int64
	unavailable, passed, slow, windows         map[string]int64
}

func newDecideTally() decideTally {
	return decideTally{unavailable: map[string]int64{}, passed: map[string]int64{}, slow: map[string]int64{},
		windows: map[string]int64{}}
}

func newDecider(cfg DecideConfig, logf func(string, ...any), clock func() time.Time, every time.Duration) *decider {
	if every <= 0 {
		every = time.Minute
	}
	return &decider{cfg: cfg, logf: logf, every: every, decides: newDecideCopy(cfg.Decides, 0, clock, logf),
		slowAtEdge: SlowAtEdge, slowInAll: SlowInAll, total: newDecideTally(), period: newDecideTally()}
}

// count adds to the period's counts and the totals.
func (dp *decider) count(add func(*decideTally)) {
	dp.mu.Lock()
	defer dp.mu.Unlock()
	add(&dp.total)
	add(&dp.period)
}

// decidePassed is what the proxy carries of a request the decide path
// passed: what it named, for the eviction Python's refusal makes, and its
// arrival, for the time Python's answer took in all.
type decidePassed struct {
	lookupHash, workspace string
	arrived               time.Time
}

type decidePassedKey struct{}

// decide runs the decide path over a request the edge read. It reports
// whether it answered; when it did not, the request passes to Python and
// passed is what the proxy carries of it.
func (e *Edge) decide(w http.ResponseWriter, r *http.Request, at, arrived time.Time, body []byte,
	seen BootSeen) (answered bool, passed decidePassed) {
	dp := e.decider
	in := DecideInputs{Request: RequestOf(r, at, body, seen), Lookup: dp.cfg.Cache.Lookup, Decide: dp.decides.view(),
		Mint: dp.cfg.Mint}
	if e.poller != nil {
		if h := e.poller.snapshot(); h != nil {
			in.Snapshot = h.snapshot
		}
	}
	if e.policy != nil {
		if h := e.policy.policy(); h != nil {
			in.Policy = h.policy
		}
	}
	decided := Decide(in)
	routed := time.Now()
	passed = decidePassed{lookupHash: decided.LookupHash, workspace: decided.Workspace, arrived: arrived}
	pass := func(reason string) (bool, decidePassed) {
		dp.pass(reason)
		if time.Since(arrived) > dp.slowAtEdge {
			dp.count(func(t *decideTally) { t.slow["at_edge"]++ })
		}
		return false, passed
	}
	d, ok := decided.Admit()
	if !ok {
		return pass(decided.Pass)
	}
	if !dp.inflight.enter(d.LookupHash, in.Policy.GatewayAuthorizeMaxInFlightPerKey) {
		return pass(PassInFlightKey)
	}
	defer dp.inflight.leave(d.LookupHash)
	admitter := e.cfg.Admitter.Load()
	if admitter == nil {
		return pass(PassOwnerUnreachable)
	}
	budget := arrived.Add(DecideBudget)
	if !time.Now().Before(budget) {
		// Nothing is asked on a budget already spent.
		return pass(PassBudgetSpent)
	}
	detached := context.WithoutCancel(r.Context())
	// The Spanner calls the 200 counts: the insert, its read-back, and an
	// enforcing windowed key's window read.
	var rpcs int64
	if d.Windows != nil {
		// Admitted only on its window check (key-windows section 4.2): one
		// read of its tr_key_limit rows within the decide budget, at most
		// WindowReadBudget, evaluated at the request's instant. Anything but
		// an admit passes; nothing has been admitted, so nothing is refunded.
		v := dp.checkWindows(detached, d, at, budget)
		rpcs++
		if !v.Admit() {
			return pass(v.Pass)
		}
	}
	admitCtx, cancel := context.WithDeadline(detached, budget)
	// The lease holds the reservation, as Python's _fastpath_admission asks
	// for it; the rows and the 200 keep the estimate.
	got := admitter.Authorize(admitCtx, frontdoor.AuthorizeOf{Workspace: d.Workspace, Request: d.Input.AuthorizationID,
		Estimate: d.Reservation, Boot: []byte(d.Kid)})
	spent := admitCtx.Err() != nil
	cancel()
	switch {
	case got.Status != frontdoor.Admitted && spent:
		return pass(PassBudgetSpent)
	case got.Status != frontdoor.Admitted:
		return pass(ownerPass(got.Status))
	case got.Envelope == "":
		// Admitted with nothing to settle against, or to refund with.
		return pass(PassOwnerInvalid)
	}
	composed, err := d.Finish(got.Envelope, got.EndOfLife)
	if err != nil {
		dp.refund(admitter, got.Envelope)
		return pass(PassCompose)
	}
	outcome, code := dp.cfg.Rows.InsertLeasedAuthorization(detached, composed.Authorization, composed.Reservation)
	committed := time.Now()
	timing := func(n int64) gwauth.Timing {
		now := time.Now()
		return gwauth.Timing{TotalMS: now.Sub(arrived).Milliseconds(), SpannerRPCs: n,
			RoutingMS: routed.Sub(arrived).Milliseconds(), StoreMS: committed.Sub(routed).Milliseconds(),
			PostCommitMS: now.Sub(committed).Milliseconds()}
	}
	switch outcome {
	case store.Inserted:
		dp.count(func(t *decideTally) { t.answered++ })
		writeAnswer(w, http.StatusOK, composed.Render(timing(rpcs+1), false))
		return true, passed
	case store.Landed:
		// The commit's answer was lost or AlreadyExists, and the row read
		// back is ours: Python's bytes after a read-back (§6).
		dp.count(func(t *decideTally) { t.answered++; t.readBack++ })
		writeAnswer(w, http.StatusOK, composed.Render(timing(rpcs+2), true))
		return true, passed
	case store.Unknown:
		// The commit may have applied: never refunded. Python's 503, the
		// hold kept; the enclave's retry finds the row through Python's
		// replay, or inserts. Counted by the insert's code.
		dp.count(func(t *decideTally) { t.unavailable[codeName(code)]++ })
		writeAnswer(w, gwauth.UnavailableStatus, gwauth.Unavailable(timing(rpcs+2)))
		return true, passed
	case store.ScopeTaken:
		dp.refund(admitter, got.Envelope)
		return pass(PassInsertScopeTaken)
	}
	dp.refund(admitter, got.Envelope)
	return pass(PassInsertFailed)
}

// passedUnread counts an authorize the edge passes before reading its body
// whole (no boot header, or one past its bound) under the boot's §3 reason,
// as Decide would name it.
func (e *Edge) passedUnread(r *http.Request, seen BootSeen) {
	if e.decider != nil && r.Method == http.MethodPost && authorizeRoutes[r.URL.Path] {
		e.decider.pass(bootPass(seen))
	}
}

// ownerPass is a front door's answer but Admitted as its §3 reason.
func ownerPass(s frontdoor.Status) string {
	switch s {
	case frontdoor.Off:
		return PassOwnerOff
	case frontdoor.Busy:
		return PassOwnerBusy
	case frontdoor.Invalid:
		return PassOwnerInvalid
	}
	return PassOwnerFailed
}

// writeAnswer writes a JSON body as Python's are written: identity
// encoded, with its length, and decision 1's Retry-After on the 503.
func writeAnswer(w http.ResponseWriter, status int, body []byte) {
	h := w.Header()
	h.Set("Content-Type", "application/json")
	h.Set("Content-Length", strconv.Itoa(len(body)))
	if status == gwauth.UnavailableStatus {
		h.Set("Retry-After", gwauth.UnavailableRetryAfter)
	}
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// refund gives back a hold nothing of ours can have landed under, beside
// the pass and within RefundBudget. A refund that is not sent ends with the
// lease, as Python's does.
func (dp *decider) refund(a Admitter, envelope string) {
	dp.refunds.Add(1)
	go func() {
		defer dp.refunds.Done()
		ctx, cancel := context.WithTimeout(context.Background(), RefundBudget)
		defer cancel()
		got := a.Refund(ctx, frontdoor.RefundOf{Envelope: envelope, Money: refundMoney})
		switch got.Status {
		case frontdoor.Won, frontdoor.Recorded, frontdoor.Settled:
			dp.count(func(t *decideTally) { t.refunded++ })
		default:
			dp.count(func(t *decideTally) { t.refundFailed++ })
		}
	}()
}

// checkWindows is the decide path's window check of an enforcing windowed
// key's decision (readWindows), within the decide budget, counted.
func (dp *decider) checkWindows(ctx context.Context, d *Decision, at, budget time.Time) keywindow.Verdict {
	w := readWindows(ctx, dp.cfg.Windows, d, at, budget)
	dp.count(func(t *decideTally) {
		if w.made {
			t.windows["reads"]++
			t.windows[windowLatency(w.took)]++
		}
		if w.verdict.Pass == keywindow.PassUnread {
			t.windows["unread"]++
		}
		t.windows[windowOutcome(w.verdict)]++
	})
	return w.verdict
}

func (dp *decider) pass(reason string) {
	dp.count(func(t *decideTally) { t.passed[reason]++ })
}

// answeredUpstream counts a pass whose answer from Python, or the proxy's
// failure to reach it, came more than SlowInAll after the request arrived.
func (dp *decider) answeredUpstream(p decidePassed) {
	if time.Since(p.arrived) > dp.slowInAll {
		dp.count(func(t *decideTally) { t.slow["in_all"]++ })
	}
}

// codeName is a gRPC code as the vectors and the counts name it:
// FailedPrecondition is failed_precondition.
func codeName(c codes.Code) string {
	var b strings.Builder
	afterLower := false
	for _, r := range c.String() {
		if unicode.IsUpper(r) && afterLower {
			b.WriteByte('_')
		}
		afterLower = unicode.IsLower(r)
		b.WriteRune(unicode.ToLower(r))
	}
	return b.String()
}

// evict drops what the cache holds of a key Python refused (401, 402 or
// 403) or whose workspace it paused (its pause 503) on a request the decide
// path passed: the lookup hash the request named and its workspace. A 503
// is told a pause by its message, read from at most its first 4 KiB, which
// are then given back to the body the proxy passes on.
func (dp *decider) evict(resp *http.Response, p decidePassed) {
	switch resp.StatusCode {
	case http.StatusUnauthorized, http.StatusPaymentRequired, http.StatusForbidden:
	case http.StatusServiceUnavailable:
		if !pauseAnswer(resp) {
			return
		}
	default:
		return
	}
	if p.lookupHash != "" {
		dp.cfg.Cache.Evict(p.lookupHash)
	}
	if p.workspace != "" {
		dp.cfg.Cache.EvictWorkspace(p.workspace)
	}
}

// pauseAnswer reads whether a 503 is Python's pause, giving back what it
// read.
func pauseAnswer(resp *http.Response) bool {
	if resp.Body == nil || resp.Header.Get("Content-Encoding") != "" {
		return false
	}
	head, err := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
	resp.Body = struct {
		io.Reader
		io.Closer
	}{io.MultiReader(bytes.NewReader(head), resp.Body), resp.Body}
	if err != nil {
		return false
	}
	return errorOf(pyAnswer{status: resp.StatusCode, body: head}).message == pauseMessage
}

// DecideCounts are the decide path's counts since the edge started: the
// requests it answered (of which read back, the 200 after a read-back), the
// 503s of an outcome not known (and by the insert's gRPC code), the refunds
// sent and failed, the requests it passed by §3 reason, the forwarded
// passes slow at the edge (at_edge) or in all (in_all), and its window
// checks (decideTally's windows).
type DecideCounts struct {
	Answered, ReadBack, Unavailable, Refunded, RefundFailed int64
	UnavailableByCode, Passed, Slow, Windows                map[string]int64
}

// DecideCounts are the counts now; zero with the decide path off.
func (e *Edge) DecideCounts() DecideCounts {
	dp := e.decider
	if dp == nil {
		return DecideCounts{}
	}
	dp.mu.Lock()
	defer dp.mu.Unlock()
	t := dp.total
	return DecideCounts{Answered: t.answered, ReadBack: t.readBack, Unavailable: sum(t.unavailable),
		Refunded: t.refunded, RefundFailed: t.refundFailed, UnavailableByCode: maps.Clone(t.unavailable),
		Passed: maps.Clone(t.passed), Slow: maps.Clone(t.slow), Windows: maps.Clone(t.windows)}
}

func sum(m map[string]int64) int64 {
	var n int64
	for _, v := range m {
		n += v
	}
	return n
}

// DecideOn reports the decide path configured.
func (e *Edge) DecideOn() bool { return e.decider != nil }

// run refreshes the decide copy and logs the counts each period, until ctx
// ends.
func (dp *decider) run(ctx context.Context) {
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		dp.decides.run(ctx)
	}()
	ticker := time.NewTicker(dp.every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			wg.Wait()
			return
		case <-ticker.C:
			dp.flush()
		}
	}
}

// flush logs the period's counts and resets them, each a count of the
// period alone, so that the lines of a span add up to its counts
// (fastpathwatch edge); then raises the period's alerts.
func (dp *decider) flush() {
	dp.mu.Lock()
	p := dp.period
	dp.period = newDecideTally()
	unavailable := sum(p.unavailable)
	switch {
	case unavailable > 0:
		dp.unavailableRun++
	case p.answered > 0:
		// Only an answer ends the run: a period that answered nothing
		// says nothing of whether the fault is gone.
		dp.unavailableRun = 0
	}
	run := dp.unavailableRun
	dp.mu.Unlock()
	dp.logf("fastpath edge decide: answered=%d read_back=%d unavailable=%d refunded=%d refund_failed=%d "+
		"unavailable_by_code=%s passed=%s slow=%s windows=%s", p.answered, p.readBack, unavailable, p.refunded,
		p.refundFailed, jsonOf(p.unavailable), jsonOf(p.passed), jsonOf(p.slow), jsonOf(p.windows))
	if unavailable > 0 {
		dp.alert(AlertUnavailable, fmt.Sprintf("the edge answered %d authorizes unavailable, their insert's "+
			"outcome not known; by the insert's gRPC code: %s", unavailable, jsonOf(p.unavailable)))
	}
	if unavailable > 0 && run >= SustainedPeriods {
		dp.alert(AlertUnavailableSustained, fmt.Sprintf("the edge has answered authorizes unavailable in %d periods "+
			"with no 200 between them; this period's by the insert's gRPC code: %s", run, jsonOf(p.unavailable)))
	}
	if len(p.slow) > 0 {
		dp.alert(AlertSlowPass, fmt.Sprintf("forwarded passes over %v at the edge (at_edge) or over %v in all "+
			"(in_all): %s", dp.slowAtEdge, dp.slowInAll, jsonOf(p.slow)))
	}
	if unread, reads := p.windows["unread"], p.windows["reads"]; unread >= WindowUnreadMin &&
		float64(unread) > WindowUnreadShare*float64(max(reads, unread)) {
		dp.alert(AlertWindowUnread, fmt.Sprintf("%d of %d window checks passed unread (key_window.unread): their "+
			"read failed or ran past its deadline; the window counts: %s", unread, reads, jsonOf(p.windows)))
	}
}

// alert raises one of the decide path's alerts, its subject the decide
// path's: the counts name no request.
func (dp *decider) alert(kind, what string) {
	if dp.cfg.Alert != nil {
		dp.cfg.Alert(kind, "edge decide", what)
		return
	}
	dp.logf("fastpath edge decide ALERT %s: %s", kind, what)
}

// WaitDecide waits for the decide path's refunds still running (at
// shutdown, once the edge's handlers have returned, so no refund outlives
// the front door or the store's client), then logs the last counts. The
// store's own inserts are waited for apart (store.Store.WaitLeasedInserts).
func (e *Edge) WaitDecide() {
	if e.decider != nil {
		e.decider.refunds.Wait()
		e.decider.flush()
	}
}
