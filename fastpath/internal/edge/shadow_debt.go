package edge

import (
	"context"
	"strings"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The debt triage of the decide shadow (docs/runbooks/fastpath-alerts.md,
// the edge_would_admit_python_refused row; docs/design/
// fast-admission-e2-decide.md section 11).
//
// Python answers two refusals with the same 402 insufficient_credits. One is
// a workspace marked in debt (section 4.7 of the fast-admission design: every
// marked credit row refuses a reservation). The other is a workspace whose
// available credit does not cover the request's upfront hold. Go's decision
// gates on the first alone: workspace.in_debt, read from the credit fact the
// cache holds, which is at most the cache's maximum age old at the request's
// instant (decision 17). It makes no balance check. Under the decide path
// the hold is taken by the front door's admit under a lease, and a lease
// holds only credit reserved for it on the credit rows, on a row with the
// headroom and no mark (store.reserve); with no room left the owner is Busy
// and the request passes to Python, which answers the same 402. So the
// pipeline's would-admit against that 402 is a defect only when the refusal
// was the mark, and the 402's body does not say which it was. The shadow
// reads the mark itself, at two instants, and names the comparison by what
// the rows say.
//
// What a missed debt stop is. Let at be the request's one clock reading,
// which the cache judges a fact's age by, and age the cache's maximum age.
// Every fact the gate may decide on was read at or after at - age. A
// workspace marked from before that instant, and still marked, is one every
// such fact shows marked: admitting it is a miss, of the gate or of the
// cache's age. A mark that began later may be newer than the fact the gate
// read, which is the lag decision 17 allows.
//
// The reads (store.Store.ReadDebtMarkAt, each a read of the workspace's
// credit rows by their primary-key prefix at an exact timestamp, at low
// priority, within WindowReadBudget, never asked again):
//
//   - the earlier, at at - age + rowSkew: the earliest instant a usable fact
//     can have been read, moved later by the allowance for the two clocks;
//   - the later, at the end of Python's answer as the edge saw it.
//
// They are bounded by a bucket of the triage's own (takeDebtReads), which
// gives both reads or neither, so a burst of 402s takes nothing from the
// window checks and no read is spent on a triage that cannot be finished.
// Python's refusal has already evicted the key and its workspace when the
// first read begins (follow): the decide path is not left the old fact
// while the triage reads.
//
// A mark is any credit row's (store.DebtMark), the edge's own definition and
// Python's. Whether the comparison pages is the earlier read's alone to say:
// a stop the gate owes stands at the earlier instant, so a workspace with
// credit rows and no mark there was owed none, whatever the later read
// says or fails to say. The later read names the class. The classes, each
// with the would-admit's ".402.insufficient_credits" after it:
//
//   - unmarked at both: ClassDebtUnmarked. No stop stood for the gate to
//     honour, so the 402 was the balance, which is the reservation's to
//     refuse and not the decision's. Counted, never paged.
//   - marked at the later only: ClassDebtLag. The mark landed after the
//     earliest usable fact, the lag decision 17 allows. Counted, never paged.
//   - unmarked at the earlier, the later read not had (it failed or ran
//     past its budget, unread, or found no credit rows, rows):
//     ClassDebtUnmarkedEarlier. No stop was owed; which of the two classes
//     above it was is not known. Counted, never paged.
//   - marked at both: ClassWouldAdmit, as before the triage. The mark is
//     older than any fact the gate may use, or newer than the oldest of
//     them by the clocks' allowance at most: it pages.
//   - anything else: ClassDebtUnconfirmed, which pages as a would-admit does,
//     since it cannot be told from a miss. That is an earlier read not made
//     (no reader or no workspace to read, none; the bucket empty, limiter),
//     one that failed or ran past its budget (unread) or found no credit
//     rows (rows: an empty read must not pass for an unmarked workspace),
//     and a mark at the earlier instant that the later read did not find
//     (cleared), found no rows for (rows) or was not had for (unread).
//
// What the two reads prove, and what they do not:
//
//   - A mark set and cleared between them reads as unmarked at both. It is
//     not a missed stop: a stop the gate owes began before the earlier
//     instant and would show there. One that lived only between the reads
//     began after the earliest usable fact, so it is lag at most, counted
//     under the other class that does not page.
//   - A mark at the earlier instant cleared by the later (a repayment while
//     the request was with Python) cannot be told from one cleared before
//     the gate's fact was read. It pages.
//   - A mark cleared and set again between the reads is read as standing
//     throughout. It pages.
//   - A mark set after the later instant is not what Python refused for,
//     unless the edge's clock is behind Spanner's; then it is still newer
//     than any fact the gate may use. No page is owed either way. Python's
//     402 evicts the key and its workspace, so the next request reads the
//     rows again.
//   - A gate that misreads a mark younger than the facts' age cannot be
//     told from lag by any read of the rows. It shows as the paging class at
//     the first refusal after the mark is older than the age, which is when
//     the gate owes the stop.
//   - The instants are this node's clock's, and the marks carry Spanner's
//     commit timestamps. The earlier read stays inside the span of usable
//     facts while the node's clock is at most rowSkew behind Spanner's. A
//     clock further behind is not bounded here, and nothing watches the
//     offset: the earlier read would then fall before the span by the
//     excess, and a stop set in that band would count as lag. A clock ahead
//     moves the earlier read later, which pages more, and can put the later
//     read's timestamp in Spanner's future; Spanner then waits for it, to
//     the read's budget at most. A later read that runs out so pages only
//     when the earlier one found a mark.
//   - A mark younger than age - rowSkew at the request's instant is always
//     lag here, even when the gate's own fact was read after the mark was
//     set. The fact's own read time (Decided.Read.CreditReadAt) as the
//     earlier instant would tell the two apart; it is not used yet.

// creditRefusal is the error type of Python's 402 for credit (gateway.py
// _insufficient_credits_error, ErrorType.INSUFFICIENT_CREDITS).
const creditRefusal = "insufficient_credits"

// debtCandidate is the comparison the triage takes: Go would have answered,
// and Python refused with the 402 a debt mark and a balance shortfall share.
const debtCandidate = ClassWouldAdmit + ".402." + creditRefusal

// DebtMarkReaderAt reads a workspace's debt mark as its credit rows stood at
// exactly at (store.Store.ReadDebtMarkAt).
type DebtMarkReaderAt interface {
	ReadDebtMarkAt(ctx context.Context, workspace string, at time.Time) (store.DebtMark, error)
}

// cacheAge is the cache's maximum age of a fact, when it tells it
// (gwstate.Cache).
type cacheAge interface{ MaxAge() time.Duration }

// factsAge is the age bound the workspace.in_debt gate decides within: the
// cache's own maximum age, or gwstate's default for a cache that names none.
func (sh *fullShadow) factsAge() time.Duration {
	if c, ok := sh.cfg.Cache.(cacheAge); ok {
		if age := c.MaxAge(); age > 0 {
			return age
		}
	}
	return gwstate.DefaultMaxAge
}

// debtReads is how many reads one triage makes, and how many tokens of its
// bucket it takes.
const debtReads = 2

// debtRead is one of the triage's reads: the workspace's mark at its
// instant, or why there is none (why): none, limiter or unread.
type debtRead struct {
	mark store.DebtMark
	why  string
}

// debtMarks are the triage's two reads. The later is made only after an
// earlier one that found credit rows.
type debtMarks struct {
	earlier, later debtRead
}

// class is what the reads make of the candidate: the class that takes
// ClassWouldAdmit's place (itself, for a mark at both instants) and the
// count that says why, among the shadow's window counts under debt. The
// earlier read alone decides whether it pages: only credit rows with no
// mark there clear it, and then nothing the later read says, or its
// failure, pages.
func (m debtMarks) class() (class, count string) {
	e, l := m.earlier, m.later
	switch {
	case e.why != "":
		return ClassDebtUnconfirmed, "debt.unconfirmed." + e.why
	case e.mark.Rows == 0:
		return ClassDebtUnconfirmed, "debt.unconfirmed.rows"
	case !e.mark.Marked && l.why != "":
		return ClassDebtUnmarkedEarlier, "debt.unmarked_earlier." + l.why
	case !e.mark.Marked && l.mark.Rows == 0:
		return ClassDebtUnmarkedEarlier, "debt.unmarked_earlier.rows"
	case !e.mark.Marked && l.mark.Marked:
		return ClassDebtLag, "debt.lag"
	case !e.mark.Marked:
		return ClassDebtUnmarked, "debt.unmarked"
	case l.why != "":
		return ClassDebtUnconfirmed, "debt.unconfirmed." + l.why
	case l.mark.Rows == 0:
		return ClassDebtUnconfirmed, "debt.unconfirmed.rows"
	case !l.mark.Marked:
		return ClassDebtUnconfirmed, "debt.unconfirmed.cleared"
	}
	return ClassWouldAdmit, "debt.marked"
}

// debtInstants are the triage's two read timestamps for a request decided at
// at whose answer from Python ended at answered: at less the facts' age plus
// the clocks' allowance (rowSkew, or half the age when the age is under two
// of them, so the instant stays inside the span of usable facts), and
// answered.
func debtInstants(at, answered time.Time, age time.Duration) (earlier, later time.Time) {
	return at.Add(min(rowSkew, age/2) - age), answered
}

// triageDebt settles a debt candidate (r.class is debtCandidate) with the
// two reads above, off the request path and after Python's answer: workspace
// is the key's as the cache said it, at the request's one clock reading and
// answered the end of Python's answer. It gives the comparison its class and
// adds the count; the alert is record's to raise, for the classes that page.
func (sh *fullShadow) triageDebt(workspace string, at, answered time.Time, r *fullResult) {
	earlier, later := debtInstants(at, answered, sh.factsAge())
	class, count := sh.readDebtMarks(workspace, earlier, later).class()
	r.class = class + strings.TrimPrefix(r.class, ClassWouldAdmit)
	r.windows = append(r.windows, count)
}

// readDebtMarks makes the triage's reads, the earlier first, once its
// bucket has given both. The later is made only after an earlier read that
// found credit rows: after one that failed or found none, nothing it could
// say changes the class.
func (sh *fullShadow) readDebtMarks(workspace string, earlier, later time.Time) debtMarks {
	var m debtMarks
	switch {
	case sh.cfg.Debts == nil || workspace == "":
		m.earlier.why = "none"
	case !sh.takeDebtReads():
		m.earlier.why = "limiter"
	default:
		m.earlier = sh.readDebtMark(workspace, earlier)
		m.later.why = "unread"
		if m.earlier.why == "" && m.earlier.mark.Rows > 0 {
			m.later = sh.readDebtMark(workspace, later)
		}
	}
	return m
}

// readDebtMark is one read, within WindowReadBudget; one that fails or runs
// past it is unread.
func (sh *fullShadow) readDebtMark(workspace string, at time.Time) debtRead {
	ctx, cancel := context.WithTimeout(context.Background(), WindowReadBudget)
	defer cancel()
	mark, err := sh.cfg.Debts.ReadDebtMarkAt(ctx, workspace, at)
	if err != nil {
		return debtRead{why: "unread"}
	}
	return debtRead{mark: mark}
}

// takeDebtReads takes one triage's reads, both or neither, from the
// triage's own bucket: ReadsPerSecond tokens refilled at that rate, as the
// window reads' is, and never fewer than one triage's. Apart from that
// bucket, so that a burst of 402s cannot turn a window check into
// window_unchecked or a race's re-read into a page; and both at once, so
// that no read is made for a triage whose other read the bucket would
// refuse.
func (sh *fullShadow) takeDebtReads() bool {
	l := &sh.debtLimiter
	l.Lock()
	defer l.Unlock()
	now := time.Now()
	if !l.at.IsZero() {
		l.tokens = min(max(sh.cfg.ReadsPerSecond, debtReads), l.tokens+now.Sub(l.at).Seconds()*sh.cfg.ReadsPerSecond)
	}
	l.at = now
	if l.tokens < debtReads {
		return false
	}
	l.tokens -= debtReads
	return true
}

// pages reports a class that raises AlertWouldAdmit: a would-admit, a window
// race no re-read settled, or a debt candidate its earlier read did not
// clear. Every other class is counted alone.
func pages(class string) bool {
	for _, paged := range []string{ClassWouldAdmit, ClassWindowRaceUnconfirmed, ClassDebtUnconfirmed} {
		if strings.HasPrefix(class, paged+".") {
			return true
		}
	}
	return false
}
