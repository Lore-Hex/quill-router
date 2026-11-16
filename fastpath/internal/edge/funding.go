package edge

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The decide shadow's reading of Python's credit refusal (its 402
// insufficient_credits) to a request Go would have answered.
//
// Go's decision carries no verdict on the workspace's balance: what the edge
// answers is held by the front door's reservation under a lease (decision 14
// of docs/design/fast-admission-e2-decide.md), and with no lease room the
// owner is Busy and the request passes to Python, which answers this 402. So
// the refusal of a workspace whose credit no longer covers the request's hold
// is not one Go would have missed. Python answers a workspace marked in debt
// with the same 402, though, and that one Go's workspace.in_debt gate should
// have stopped within the facts' age (decision 17): a would-admit. The body
// cannot tell the two apart, so the shadow reads the workspace's credit rows
// once more, after Python's answer, and settles it as an operator would by
// hand (docs/runbooks/fastpath-alerts.md):
//
//   - no row marked, and neither a row's headroom nor their sum at or above
//     the hold: the balance ran out, funding_unchecked, counted and never
//     paged;
//   - a row marked, a row or the sum covering the hold, or no read: it stays
//     the would-admit and pages, with the reason beside it.
//
// The read is strong and comes after Python's refusal, so credit freed or
// added between the two (a hold released, a top-up) reads as covering and
// pages, as it did before the read existed; and a mark repaid between the
// two, with what is left still short of the hold, reads as the balance. A
// gate that misses marked workspaces shows on their next refusal.

// ShadowCreditReader reads a workspace's credit rows
// (store.Store.ReadCreditStanding) for the shadow's settling of a credit
// refusal.
type ShadowCreditReader interface {
	ReadCreditStanding(ctx context.Context, workspace string) (store.CreditStanding, error)
}

// ClassFundingUnchecked: Python refused the request for credit (402
// insufficient_credits), Go would have answered, and the workspace's rows,
// read after the refusal, carry no debt mark and cannot take the request's
// hold: the balance ran out, which no gate of Go's checks (the lease does).
// Neither an agreement nor a would-admit: counted, never paged.
const ClassFundingUnchecked = "funding_unchecked"

// creditRefusal is the error type of Python's 402 for credit (gateway.py,
// ErrorType.INSUFFICIENT_CREDITS).
const creditRefusal = "insufficient_credits"

// fundingCandidate is the class confirmFunding settles: Python's credit
// refusal, read through, of a request Go would have answered.
var fundingCandidate = ClassWouldAdmit + "." + strconv.Itoa(402) + "." + creditRefusal

// What the credit read found, a fundingRead's outcome. Only fundingShort
// settles the refusal; each other one leaves it the would-admit.
const (
	fundingShort    = "short"
	fundingMarked   = "marked"
	fundingCovered  = "covered"
	fundingUnread   = "unread"
	fundingLimiter  = "limiter"
	fundingNoReader = "no_reader"
)

// fundingRead is one credit refusal's read: its outcome, the hold Go's
// decision takes, and of the rows read their count, their sum and the
// largest row (zero with no read, or a sum past int64).
type fundingRead struct {
	outcome         string
	hold, sum, most int64
	rows            int
}

// note is what a critical line adds for the read, "" for none.
func (f *fundingRead) note() string {
	if f == nil {
		return ""
	}
	return " funding=" + f.outcome
}

// line is the read's trace line, after its class.
func (f *fundingRead) line() string {
	return fmt.Sprintf("funding=%s hold=%d rows=%d sum=%d most=%d", f.outcome, f.hold, f.rows, f.sum, f.most)
}

// creditCovers is whether unmarked rows could take the hold as Python's
// authorize reads them (storage_gcp_authorize.py: one row's headroom at or
// above it, or their sum), with the sum and the largest row. A sum past
// int64 either way is taken as covering, with a zero sum: the rows are then
// not ones the shadow can call short.
func creditCovers(headroom []int64, hold int64) (covers bool, sum, most int64) {
	if len(headroom) == 0 {
		return true, 0, 0
	}
	most = headroom[0]
	for _, h := range headroom {
		most = max(most, h)
		if (h > 0 && sum > math.MaxInt64-h) || (h < 0 && sum < math.MinInt64-h) {
			return true, 0, most
		}
		sum += h
	}
	return most >= hold || sum >= hold, sum, most
}

// confirmFunding settles Python's credit refusal of a request Go would have
// answered (fundingCandidate) with one strong read of the workspace's credit
// rows, within the funding limiter and rowReadTimeout: no row marked and the
// rows short of the hold Go's decision takes (Decision.Reservation, the hold
// Python refused) is ClassFundingUnchecked; anything else, no reader and no
// read among them, leaves the would-admit, which pages.
func (sh *fullShadow) confirmFunding(dec *Decision, r *fullResult) {
	if r.class != fundingCandidate || dec == nil {
		return
	}
	f := &fundingRead{hold: dec.Reservation}
	r.funding = f
	switch {
	case sh.cfg.Credit == nil || dec.Workspace == "":
		f.outcome = fundingNoReader
		return
	case !sh.fundingSample():
		f.outcome = fundingLimiter
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), rowReadTimeout)
	standing, err := sh.cfg.Credit.ReadCreditStanding(ctx, dec.Workspace)
	cancel()
	if err != nil {
		f.outcome = fundingUnread
		return
	}
	var covers bool
	covers, f.sum, f.most = creditCovers(standing.Headroom, f.hold)
	f.rows = len(standing.Headroom)
	switch {
	case standing.Marked:
		f.outcome = fundingMarked
	case covers:
		f.outcome = fundingCovered
	default:
		f.outcome = fundingShort
		r.class = ClassFundingUnchecked + "." + strconv.Itoa(402) + "." + creditRefusal
	}
}

// fundingSample is one credit read within ReadsPerSecond (a bucket of that
// many tokens refilled at that rate), apart from the row read-backs' and
// the window reads'.
func (sh *fullShadow) fundingSample() bool {
	l := &sh.fundingLimiter
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
