package edge

import (
	"context"
	"errors"
	"fmt"
	"math"
	"regexp"
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
// The read is strong and comes after Python's refusal, so what changes
// between the two is read as it stands afterwards:
//
//   - credit freed or added (a hold released, a top-up) reads as covering,
//     which the read Python's refusal names settles as a race or leaves
//     paging (below), and a mark set since reads as marked, which pages;
//   - a mark cleared since, with what is left still short of the hold,
//     reads as the balance and is not paged. Python itself clears a stale
//     mark (rows marked whose signed sum is no longer negative) inside the
//     refusing request, before it answers, so that workspace is read
//     unmarked every time; its refusal is then for the balance. A mark
//     that stands (a negative sum) is still there at the read and pages,
//     on this refusal and the workspace's next.
//
// Python's first refusal of a workspace with several rows proves the rows
// spent by their signed sum alone, so it can refuse while one row still
// covers the hold beside one that owes: that reads as covering and pages.
//
// Rows that cover are most often a hold released, by the workspace's other
// requests' settles, between Python's read and the shadow's: a workspace
// near the end of its credit and busy swings by tens of thousands of
// microdollars a second. So Python's 402 names the read its refusal stands
// on (CreditRefusalReadHeader), and rows that cover are settled by it
// (confirmFundingRace): Python's own read short of the hold and, when it was
// a snapshot, the same rows re-read at its exact read timestamp short and
// unmarked too, is ClassFundingRace, counted and never paged; anything else
// still pages, the reason beside it.

// ShadowCreditReader reads a workspace's credit rows
// (store.Store.ReadCreditStanding) for the shadow's settling of a credit
// refusal, and the same rows as they stood at Python's read timestamp
// (store.Store.ReadCreditStandingAt) for its confirmation of a race.
type ShadowCreditReader interface {
	ReadCreditStanding(ctx context.Context, workspace string) (store.CreditStanding, error)
	ReadCreditStandingAt(ctx context.Context, workspace string, at time.Time) (store.CreditStanding, error)
}

// ClassFundingUnchecked: Python refused the request for credit (402
// insufficient_credits), Go would have answered, and the workspace's rows,
// read after the refusal, carry no debt mark and cannot take the request's
// hold: the balance ran out, which no gate of Go's checks (the lease does).
// Neither an agreement nor a would-admit: counted, never paged.
const ClassFundingUnchecked = "funding_unchecked"

// ClassFundingRace: Python refused the request for credit, Go would have
// answered, the workspace's rows read after the refusal carry no debt mark
// and could take the hold, and the read Python's refusal stands on
// (CreditRefusalReadHeader) could not: Python held it to the same hold, and
// neither a row nor the sum reached it; for a snapshot, the rows re-read at
// its exact read timestamp, unmarked, did not reach it either. A hold was
// released, or credit added, between Python's read and the shadow's.
// Neither an agreement nor a would-admit: counted, never paged.
const ClassFundingRace = "funding_race"

// creditRefusal is the error type of Python's 402 for credit (gateway.py,
// ErrorType.INSUFFICIENT_CREDITS).
const creditRefusal = "insufficient_credits"

// CreditRefusalReadHeader is the header of Python's internal authorize 402
// insufficient_credits that names the read of the workspace's credit rows
// its refusal stands on (trusted_router/credit_refusal_read.py): which read,
// its read timestamp when it was a snapshot (RFC 3339 UTC, nine fractional
// digits, as WindowReadTimestampHeader), the hold it was held to, and the
// rows' count, signed sum and largest row. Python sends it on no other
// answer, and the enclave keeps only an error's status, body and
// Retry-After, so it reaches no customer.
const CreditRefusalReadHeader = "X-TR-Credit-Refusal-Read"

// readAtLayout is the form Python writes a read timestamp in (readAtForm):
// UTC, exactly nine fractional digits, a Z.
const readAtLayout = "2006-01-02T15:04:05.000000000Z07:00"

// creditReadForm is the one form Python writes CreditRefusalReadHeader in:
// its fields in a fixed order, read_at only for a snapshot.
var creditReadForm = regexp.MustCompile(`^read=([a-z_]{1,32})(?: read_at=(\S+))? hold=(-?\d{1,19}) ` +
	`rows=(\d{1,9}) sum=(-?\d{1,19}) most=(-?\d{1,19})$`)

// creditRefusalRead is CreditRefusalReadHeader read: which read, its read
// timestamp (zero for a read inside a read-write transaction, which has
// none), the hold Python held it to, and the rows' count, signed sum and
// largest row.
type creditRefusalRead struct {
	read            string
	at              time.Time
	hold, sum, most int64
	rows            int
}

// parseCreditRefusalRead reads Python's CreditRefusalReadHeader: the read,
// or why there is none (raceNone when absent, raceMalformed for anything but
// exactly the form Python writes, a figure past int64 or no rows).
func parseCreditRefusalRead(h string) (*creditRefusalRead, string) {
	if h == "" {
		return nil, raceNone
	}
	m := creditReadForm.FindStringSubmatch(h)
	if m == nil {
		return nil, raceMalformed
	}
	c := &creditRefusalRead{read: m[1]}
	if m[2] != "" {
		at, why := pythonReadAt(m[2])
		if why != "" {
			return nil, raceMalformed
		}
		c.at = at
	}
	var err [4]error
	var rows int64
	c.hold, err[0] = strconv.ParseInt(m[3], 10, 64)
	rows, err[1] = strconv.ParseInt(m[4], 10, 64)
	c.sum, err[2] = strconv.ParseInt(m[5], 10, 64)
	c.most, err[3] = strconv.ParseInt(m[6], 10, 64)
	if errors.Join(err[:]...) != nil || rows < 1 {
		return nil, raceMalformed
	}
	c.rows = int(rows)
	return c, ""
}

// Why a credit refusal whose rows covered the hold is or is not a race
// (fundingRead.race): confirmed, or the reason it still pages.
const (
	raceConfirmed = "confirmed"
	// raceNone: Python's 402 named no read (a Python without the header, or
	// a refusal no read followed); raceMalformed: one not in Python's form.
	raceNone      = "none"
	raceMalformed = "malformed"
	// raceHold: Python held its read to another hold than Go's decision.
	raceHold = "hold"
	// racePythonCovers: Python's own read reached the hold, a row or the
	// sum, and it refused all the same.
	racePythonCovers = "python_covers"
	// raceLimiter, raceUnread: no re-read at Python's read timestamp (the
	// funding limiter, a failed or late read, a timestamp past the version
	// retention).
	raceLimiter = "limiter"
	raceUnread  = "unread"
	// raceRereadMarked, raceRereadCovers: the rows re-read at Python's read
	// timestamp carried a debt mark, or reached the hold: not the rows
	// Python's figures say it read.
	raceRereadMarked = "reread_marked"
	raceRereadCovers = "reread_covers"
)

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
// largest row (zero with no read, or a sum past int64). For rows that
// covered the hold, race is why the refusal is or is not a race
// (confirmFundingRace), python the read Python's refusal named, and reread
// the rows re-read at its read timestamp (nil for none).
type fundingRead struct {
	outcome         string
	hold, sum, most int64
	rows            int
	race            string
	python          *creditRefusalRead
	reread          *creditTotals
}

// creditTotals are rows read: their count, sum and largest row.
type creditTotals struct {
	rows      int
	sum, most int64
}

// note is what a critical line adds for the read, "" for none.
func (f *fundingRead) note() string {
	if f == nil {
		return ""
	}
	note := " funding=" + f.outcome
	if f.race != "" {
		note += " race=" + f.race
	}
	return note
}

// line is the read's trace line, after its class: amounts, a read's name
// and an instant, never an identifier.
func (f *fundingRead) line() string {
	line := fmt.Sprintf("funding=%s hold=%d rows=%d sum=%d most=%d", f.outcome, f.hold, f.rows, f.sum, f.most)
	if f.race == "" {
		return line
	}
	line += " race=" + f.race
	if p := f.python; p != nil {
		line += " python=" + p.read
		if !p.at.IsZero() {
			line += " python_at=" + p.at.Format(readAtLayout)
		}
		line += fmt.Sprintf(" python_hold=%d python_rows=%d python_sum=%d python_most=%d", p.hold, p.rows, p.sum,
			p.most)
	}
	if g := f.reread; g != nil {
		line += fmt.Sprintf(" reread_rows=%d reread_sum=%d reread_most=%d", g.rows, g.sum, g.most)
	}
	return line
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
// Python refused) is ClassFundingUnchecked; rows unmarked that cover the
// hold are settled by the read Python's refusal names (readHeader, its
// CreditRefusalReadHeader; confirmFundingRace); anything else, no reader and
// no read among them, leaves the would-admit, which pages.
func (sh *fullShadow) confirmFunding(dec *Decision, readHeader string, r *fullResult) {
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
		if sh.confirmFundingRace(dec, readHeader, f) {
			r.class = ClassFundingRace + "." + strconv.Itoa(402) + "." + creditRefusal
		}
	default:
		f.outcome = fundingShort
		r.class = ClassFundingUnchecked + "." + strconv.Itoa(402) + "." + creditRefusal
	}
}

// confirmFundingRace settles a credit refusal whose rows, read after it,
// were unmarked and covered the hold (f), by the read Python's refusal names
// (header): the race is confirmed when Python held that read to Go's hold
// and neither a row nor the sum Python read reached it, and, when the read
// was a snapshot, the same rows re-read at its exact read timestamp (within
// the funding limiter and rowReadTimeout) are unmarked and short of the hold
// too. A read with no timestamp (the rebalance's locked read, inside a
// read-write transaction) is confirmed on Python's figures alone; it does
// not read the debt mark, which the shadow's own read found unset. Anything
// else leaves the would-admit, with the reason in f.race.
func (sh *fullShadow) confirmFundingRace(dec *Decision, header string, f *fundingRead) bool {
	py, why := parseCreditRefusalRead(header)
	f.python = py
	switch {
	case why != "":
		f.race = why
	case py.hold != f.hold:
		f.race = raceHold
	case py.most >= f.hold || py.sum >= f.hold:
		f.race = racePythonCovers
	case py.at.IsZero():
		f.race = raceConfirmed
	case !sh.fundingSample():
		f.race = raceLimiter
	default:
		ctx, cancel := context.WithTimeout(context.Background(), rowReadTimeout)
		standing, err := sh.cfg.Credit.ReadCreditStandingAt(ctx, dec.Workspace, py.at)
		cancel()
		if err != nil {
			f.race = raceUnread
			break
		}
		covers, sum, most := creditCovers(standing.Headroom, f.hold)
		f.reread = &creditTotals{rows: len(standing.Headroom), sum: sum, most: most}
		switch {
		case standing.Marked:
			f.race = raceRereadMarked
		case covers:
			f.race = raceRereadCovers
		default:
			f.race = raceConfirmed
		}
	}
	return f.race == raceConfirmed
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
