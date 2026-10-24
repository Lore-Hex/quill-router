package edge

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
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
)

// refundMoney is a refund's money fields, Python's
// _fastpath_release_after_failure's (gateway.py).
var refundMoney = []byte(`{"charge":0,"estimate":0}`)

// Inserter writes an admitted decision's two rows and reports what the
// commit, and its one read-back, prove (store.Store.InsertLeasedAuthorization).
type Inserter interface {
	InsertLeasedAuthorization(ctx context.Context, a gwauth.AuthorizationRow, r gwauth.ReservationRow) store.InsertOutcome
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
	// Mint makes the ids the edge mints; nil funcs are gwauth's random
	// ones. A test pins them.
	Mint Mint
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

	answered, readBack, unavailable atomic.Int64
	refunded, refundFailed          atomic.Int64

	mu     sync.Mutex
	passed map[string]int64
	period map[string]int64
}

func newDecider(cfg DecideConfig, logf func(string, ...any), clock func() time.Time, every time.Duration) *decider {
	if every <= 0 {
		every = time.Minute
	}
	return &decider{cfg: cfg, logf: logf, every: every, decides: newDecideCopy(cfg.Decides, 0, clock, logf),
		passed: map[string]int64{}, period: map[string]int64{}}
}

// decidePassed is what the proxy carries of a request the decide path
// passed: what it named, for the eviction Python's refusal makes.
type decidePassed struct{ lookupHash, workspace string }

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
	passed = decidePassed{lookupHash: decided.LookupHash, workspace: decided.Workspace}
	d, ok := decided.Admit()
	if !ok {
		dp.pass(decided.Pass)
		return false, passed
	}
	pass := func(reason string) (bool, decidePassed) {
		dp.pass(reason)
		return false, passed
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
	admitCtx, cancel := context.WithDeadline(detached, budget)
	got := admitter.Authorize(admitCtx, frontdoor.AuthorizeOf{Workspace: d.Workspace, Request: d.Input.AuthorizationID,
		Estimate: d.Estimate, Boot: []byte(d.Kid)})
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
	outcome := dp.cfg.Rows.InsertLeasedAuthorization(detached, composed.Authorization, composed.Reservation)
	committed := time.Now()
	timing := func(rpcs int64) gwauth.Timing {
		now := time.Now()
		return gwauth.Timing{TotalMS: now.Sub(arrived).Milliseconds(), SpannerRPCs: rpcs,
			RoutingMS: routed.Sub(arrived).Milliseconds(), StoreMS: committed.Sub(routed).Milliseconds(),
			PostCommitMS: now.Sub(committed).Milliseconds()}
	}
	switch outcome {
	case store.Inserted:
		dp.answered.Add(1)
		writeAnswer(w, http.StatusOK, composed.Render(timing(1), false))
		return true, passed
	case store.Landed:
		// The commit's answer was lost or AlreadyExists, and the row read
		// back is ours: Python's bytes after a read-back (§6).
		dp.answered.Add(1)
		dp.readBack.Add(1)
		writeAnswer(w, http.StatusOK, composed.Render(timing(2), true))
		return true, passed
	case store.Unknown:
		// The commit may have applied: never refunded. Python's 503, the
		// hold kept; the enclave's retry finds the row through Python's
		// replay, or inserts.
		dp.unavailable.Add(1)
		writeAnswer(w, gwauth.UnavailableStatus, gwauth.Unavailable(timing(2)))
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
			dp.refunded.Add(1)
		default:
			dp.refundFailed.Add(1)
		}
	}()
}

func (dp *decider) pass(reason string) {
	dp.mu.Lock()
	defer dp.mu.Unlock()
	dp.passed[reason]++
	dp.period[reason]++
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
// 503s of an outcome not known, the refunds sent and failed, and the
// requests it passed by §3 reason.
type DecideCounts struct {
	Answered, ReadBack, Unavailable, Refunded, RefundFailed int64
	Passed                                                  map[string]int64
}

// DecideCounts are the counts now; zero with the decide path off.
func (e *Edge) DecideCounts() DecideCounts {
	dp := e.decider
	if dp == nil {
		return DecideCounts{}
	}
	dp.mu.Lock()
	passed := make(map[string]int64, len(dp.passed))
	for k, v := range dp.passed {
		passed[k] = v
	}
	dp.mu.Unlock()
	return DecideCounts{Answered: dp.answered.Load(), ReadBack: dp.readBack.Load(), Unavailable: dp.unavailable.Load(),
		Refunded: dp.refunded.Load(), RefundFailed: dp.refundFailed.Load(), Passed: passed}
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

// flush logs the period's counts: totals of the answers and refunds, and
// the period's passes by reason.
func (dp *decider) flush() {
	dp.mu.Lock()
	reasons := make([]string, 0, len(dp.period))
	for r := range dp.period {
		reasons = append(reasons, r)
	}
	sort.Strings(reasons)
	parts := make([]string, len(reasons))
	for i, r := range reasons {
		parts[i] = r + "=" + strconv.FormatInt(dp.period[r], 10)
	}
	dp.period = map[string]int64{}
	dp.mu.Unlock()
	dp.logf("fastpath edge decide: answered=%d read_back=%d unavailable=%d refunded=%d refund_failed=%d passed={%s}",
		dp.answered.Load(), dp.readBack.Load(), dp.unavailable.Load(), dp.refunded.Load(), dp.refundFailed.Load(),
		strings.Join(parts, ","))
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
