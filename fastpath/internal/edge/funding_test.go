package edge

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fakeCredit is a ShadowCreditReader: what its strong read answers, what
// its read at a timestamp answers (standingAt, errAt), the workspaces it was
// asked for, and the timestamps it was asked at.
type fakeCredit struct {
	mu         sync.Mutex
	standing   store.CreditStanding
	err        error
	standingAt store.CreditStanding
	errAt      error
	asked      []string
	askedAt    []time.Time
	// onRead, when set, runs at each strong read.
	onRead func()
}

func (f *fakeCredit) ReadCreditStanding(ctx context.Context, workspace string) (store.CreditStanding, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, workspace)
	if f.onRead != nil {
		f.onRead()
	}
	if _, bounded := ctx.Deadline(); !bounded {
		return store.CreditStanding{}, errors.New("the credit read has no deadline")
	}
	return f.standing, f.err
}

func (f *fakeCredit) ReadCreditStandingAt(ctx context.Context, workspace string, at time.Time) (store.CreditStanding,
	error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, workspace)
	f.askedAt = append(f.askedAt, at)
	if _, bounded := ctx.Deadline(); !bounded {
		return store.CreditStanding{}, errors.New("the credit read has no deadline")
	}
	return f.standingAt, f.errAt
}

func (f *fakeCredit) reads() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.asked...)
}

func (f *fakeCredit) readsAt() []time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]time.Time(nil), f.askedAt...)
}

// TestCreditCoversIsPythonsReadingOfUnmarkedRows: the rows can take a hold
// when one row's headroom, or their sum, is at or above it, as Python's
// authorize reads unmarked rows; the sum and the largest row come with it.
// A sum past int64, either way, and no rows at all are taken as covering,
// so the shadow never calls such rows short.
func TestCreditCoversIsPythonsReadingOfUnmarkedRows(t *testing.T) {
	for _, x := range []struct {
		name      string
		rows      []int64
		hold      int64
		covers    bool
		sum, most int64
	}{
		{"one row below the hold", []int64{99}, 100, false, 99, 99},
		{"one row at the hold", []int64{100}, 100, true, 100, 100},
		{"no row at the hold, their sum one below", []int64{40, 0, 59}, 100, false, 99, 59},
		{"no row at the hold, their sum at it", []int64{40, 0, 60}, 100, true, 100, 60},
		{"a row at the hold beside one that owes", []int64{100, -70}, 100, true, 30, 100},
		{"rows that owe", []int64{-5, -7}, 1, false, -12, -5},
		{"empty rows against a hold of nothing", []int64{0, 0}, 0, true, 0, 0},
		{"a sum past the top of int64", []int64{math.MaxInt64, 1}, math.MaxInt64, true, 0, math.MaxInt64},
		{"a sum past the bottom of int64", []int64{math.MinInt64, -1}, 100, true, 0, -1},
		{"no rows", nil, 100, true, 0, 0},
	} {
		covers, sum, most := creditCovers(x.rows, x.hold)
		if covers != x.covers || sum != x.sum || most != x.most {
			t.Errorf("%s: covers %v sum %d most %d; want %v %d %d", x.name, covers, sum, most, x.covers, x.sum, x.most)
		}
	}
}

// fundingShadow is a bare shadow whose credit reader is credit (nil for
// none), with its alerts and log lines taken.
func fundingShadow(t *testing.T, credit ShadowCreditReader, readsPerSecond float64) (*fullShadow, *alerts, *logLines) {
	t.Helper()
	a, logs := &alerts{}, &logLines{}
	cfg := DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{},
		Rows: &fakeRows{by: map[string]store.AuthorizationForCompare{}}, ReadsPerSecond: readsPerSecond, Alert: a.raise}
	if credit != nil {
		cfg.Credit = credit
	}
	sh, err := newFullShadow(cfg, logs.logf, time.Now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return sh, a, logs
}

// TestACreditRefusalIsSettledByTheWorkspacesRows: Python's 402
// insufficient_credits to a request Go would have answered is the
// would-admit until the workspace's credit rows, read once after the
// refusal, show no debt mark and neither a row nor their sum at the hold
// Go's decision takes: then it is funding_unchecked, counted and not paged,
// since Go checks no balance (the lease does). A marked row is the refusal
// Go's in-debt gate should have made, so it pages whatever the amounts;
// rows that cover the hold, a failed read, the limiter and no reader leave
// it the would-admit too, each named in the alert. The read is of the
// decision's workspace, against the decision's own hold, with a deadline.
func TestACreditRefusalIsSettledByTheWorkspacesRows(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	dec := d.ForShadow()
	if dec == nil || dec.Reservation < 2 || dec.Workspace != c.Facts.Key.WorkspaceID {
		t.Fatalf("the decision: %q %+v", d.Pass, dec)
	}
	hold := dec.Reservation
	refusal := pyAnswer{status: 402, body: errorBody(402, creditRefusal, "Insufficient credits")}
	wouldAdmit := ClassWouldAdmit + ".402." + creditRefusal
	settled := ClassFundingUnchecked + ".402." + creditRefusal
	failed := errors.New("synthetic credit read failure")
	for _, x := range []struct {
		name     string
		rows     []int64
		marked   bool
		err      error
		noReader bool
		noToken  bool
		class    string
		outcome  string
		reads    int
	}{
		{"unmarked, one row one below the hold", []int64{hold - 1}, false, nil, false, false, settled, fundingShort, 1},
		{"unmarked, no credit at all", []int64{0, 0, 0}, false, nil, false, false, settled, fundingShort, 1},
		{"unmarked, the sum one below the hold", []int64{hold - 2, 0, 1}, false, nil, false, false, settled,
			fundingShort, 1},
		{"unmarked, one row at the hold", []int64{0, hold}, false, nil, false, false, wouldAdmit, fundingCovered, 1},
		{"unmarked, the sum at the hold", []int64{hold - 1, 1}, false, nil, false, false, wouldAdmit, fundingCovered, 1},
		{"marked, the rows owing", []int64{-5, -9}, true, nil, false, false, wouldAdmit, fundingMarked, 1},
		{"marked, the rows short of the hold", []int64{hold - 1}, true, nil, false, false, wouldAdmit, fundingMarked, 1},
		{"marked, the rows covering the hold", []int64{hold}, true, nil, false, false, wouldAdmit, fundingMarked, 1},
		{"the read failed", nil, false, failed, false, false, wouldAdmit, fundingUnread, 1},
		{"the limiter refused the read", []int64{0}, false, nil, false, true, wouldAdmit, fundingLimiter, 0},
		{"no reader", []int64{0}, false, nil, true, false, wouldAdmit, fundingNoReader, 0},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: x.rows, Marked: x.marked}, err: x.err}
		var reader ShadowCreditReader
		if !x.noReader {
			reader = credit
		}
		rate := 1e9
		if x.noToken {
			rate = 1e-9
		}
		sh, alerts, logs := fundingShadow(t, reader, rate)
		r := compareFull(d, refusal)
		if r.class != wouldAdmit || r.funding != nil {
			t.Fatalf("%s: before the read: %+v", x.name, r)
		}
		sh.confirmFunding(dec, "", &r)
		reads := credit.reads()
		if r.class != x.class || r.funding == nil || r.funding.outcome != x.outcome || r.funding.hold != hold ||
			len(reads) != x.reads || (x.reads == 1 && reads[0] != dec.Workspace) {
			t.Errorf("%s: class %s funding %+v reads %q; want %s %s and %d read(s) of the workspace", x.name, r.class,
				r.funding, reads, x.class, x.outcome, x.reads)
			continue
		}
		sh.record("digest", r)
		paged := alerts.taken()[AlertWouldAdmit]
		want := "digest: the edge would have answered an authorize Python refused: " + wouldAdmit + " funding=" + x.outcome
		if x.outcome == fundingCovered {
			// Rows that cover are settled by the read Python's refusal
			// names, and this one names none.
			want += " race=" + raceNone
		}
		if x.class == settled && len(paged) != 0 {
			t.Errorf("%s: a settled refusal paged: %q", x.name, paged)
		}
		if x.class == wouldAdmit && (len(paged) != 1 || paged[0] != want) {
			t.Errorf("%s: alerts %q, want %q", x.name, paged, want)
		}
		if !logs.has("fastpath edge decide shadow funding: class=" + x.class + " funding=" + x.outcome + " hold=") {
			t.Errorf("%s: no funding line: %s", x.name, logs.all())
		}
		if n := sh.totals.classes[x.class]; n != 1 {
			t.Errorf("%s: classes %v", x.name, sh.totals.classes)
		}
	}
}

// TestTheHoldComparedIsTheReservation: the rows are held to the credit hold
// Python refused, the decision's reservation, not to its estimate, which is
// the larger when the output budget passes the hold's bound. Rows one below
// the reservation are short; rows at the reservation, far below the
// estimate, cover it.
func TestTheHoldComparedIsTheReservation(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:estimate:hold_cap:deepseek/deepseek-v4-flash:1000+131072")
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	dec := d.ForShadow()
	if dec == nil || dec.Reservation < 2 || dec.Estimate < dec.Reservation+2 {
		t.Fatalf("the decision: %q %+v", d.Pass, dec)
	}
	refusal := pyAnswer{status: 402, body: errorBody(402, creditRefusal, "Insufficient credits")}
	for _, x := range []struct {
		name    string
		row     int64
		outcome string
	}{
		{"one below the reservation", dec.Reservation - 1, fundingShort},
		{"at the reservation", dec.Reservation, fundingCovered},
		{"between the reservation and the estimate", dec.Reservation + 1, fundingCovered},
		{"at the estimate", dec.Estimate, fundingCovered},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: []int64{x.row}}}
		sh, _, _ := fundingShadow(t, credit, 1e9)
		r := compareFull(d, refusal)
		sh.confirmFunding(dec, "", &r)
		if r.funding == nil || r.funding.outcome != x.outcome || r.funding.hold != dec.Reservation {
			t.Errorf("%s: %+v, want %s against a hold of %d", x.name, r.funding, x.outcome, dec.Reservation)
		}
	}
}

// TestTheFundingLinesAreBoundedByThePeriod: a period logs at most
// maxDifferLogs funding lines however many credit refusals it settles, each
// still counted, and the next period logs again.
func TestTheFundingLinesAreBoundedByThePeriod(t *testing.T) {
	sh, _, logs := fundingShadow(t, &fakeCredit{}, 1e9)
	settled := fullResult{class: ClassFundingUnchecked + ".402." + creditRefusal,
		funding: &fundingRead{outcome: fundingShort, hold: 5}}
	lines := func() int { return strings.Count(logs.all(), "fastpath edge decide shadow funding: ") }
	for i := 0; i < maxDifferLogs+7; i++ {
		sh.record("digest", settled)
	}
	if lines() != maxDifferLogs || sh.totals.classes[settled.class] != maxDifferLogs+7 {
		t.Fatalf("%d funding lines for %d refusals", lines(), sh.totals.classes[settled.class])
	}
	sh.flush()
	sh.record("digest", settled)
	if lines() != maxDifferLogs+1 {
		t.Fatalf("%d funding lines after the period's line", lines())
	}
}

// TestOnlyACreditRefusalReadThroughIsSettled: the credit read is made for
// Python's 402 insufficient_credits alone, read through, of a request Go
// would have answered. A 402 of another type, a 402 whose body was not
// read, any other refusal, Python's 200 and a request Go passed each keep
// their class with no read, whatever the rows would have said.
func TestOnlyACreditRefusalReadThroughIsSettled(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	if d.ForShadow() == nil {
		t.Fatalf("the decision: %q", d.Pass)
	}
	passed := Decided{Pass: "decide.off"}
	for _, x := range []struct {
		name string
		d    Decided
		a    pyAnswer
		want string
	}{
		{"a 402 of another type", d, pyAnswer{status: 402, body: errorBody(402, "credits_not_on_this_plane", "no")},
			ClassWouldAdmit + ".402.credits_not_on_this_plane"},
		{"a 402 not read through", d, pyAnswer{status: 402, why: "bound"}, ClassWouldAdmit + ".402.unread"},
		{"a 403", d, pyAnswer{status: 403, body: errorBody(403, "forbidden", "billing_paused")},
			ClassWouldAdmit + ".403.forbidden"},
		{"a 429 named as the credit refusal", d, pyAnswer{status: 429, body: errorBody(429, creditRefusal, "no")},
			ClassWouldAdmit + ".429." + creditRefusal},
		{"a credit refusal of a request Go passed", passed,
			pyAnswer{status: 402, body: errorBody(402, creditRefusal, "no")}, ClassGoPassed + ".decide.off"},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: []int64{0}}}
		sh, _, logs := fundingShadow(t, credit, 1e9)
		r := compareFull(x.d, x.a)
		sh.confirmFunding(x.d.ForShadow(), "", &r)
		if r.class != x.want || r.funding != nil || len(credit.reads()) != 0 {
			t.Errorf("%s: class %s funding %+v reads %q; want %s and no read", x.name, r.class, r.funding,
				credit.reads(), x.want)
		}
		sh.record("digest", r)
		if strings.Contains(logs.all(), "shadow funding:") {
			t.Errorf("%s: a funding line: %s", x.name, logs.all())
		}
	}
}

// TestTheFollowedCreditRefusalIsSettledBeforeItPages: through the edge, a
// request Python refuses for credit is followed to the credit read. Rows
// that are unmarked and short of the hold count it funding_unchecked with
// no alert and no CRITICAL line; marked rows page it as the would-admit,
// the reason in the alert. Either way the key and its workspace are
// evicted, as after any refusal of Python's.
func TestTheFollowedCreditRefusalIsSettledBeforeItPages(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	wouldAdmit := ClassWouldAdmit + ".402." + creditRefusal
	settled := ClassFundingUnchecked + ".402." + creditRefusal
	for _, x := range []struct {
		name   string
		marked bool
		class  string
		alerts int
	}{
		{"the balance ran out", false, settled, 0},
		{"the workspace is marked in debt", true, wouldAdmit, 1},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: []int64{0, 0}, Marked: x.marked}}
		taken := &alerts{}
		h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) {
			cfg.Credit = credit
			cfg.Alert = taken.raise
		})
		// The eviction does not wait on the credit read: the key and its
		// workspace are out of the cache by the time the rows are read.
		evictedAtRead := -1
		credit.onRead = func() {
			h.cache.mu.Lock()
			evictedAtRead = len(h.cache.evicted)
			h.cache.mu.Unlock()
		}
		h.answer(answerWith(402, errorBody(402, creditRefusal, "Insufficient credits")))
		h.send(t, c)
		got := h.edge.DecideShadowCounts()
		paged := taken.taken()[AlertWouldAdmit]
		reads := credit.reads()
		if got.Classes[x.class] != 1 || len(got.Classes) != 1 || len(paged) != x.alerts || len(reads) != 1 ||
			reads[0] != c.Facts.Key.WorkspaceID {
			t.Errorf("%s: classes %v alerts %q reads %q", x.name, got.Classes, paged, reads)
		}
		if x.alerts == 1 && !strings.HasSuffix(paged[0], wouldAdmit+" funding="+fundingMarked) {
			t.Errorf("%s: the alert %q", x.name, paged[0])
		}
		if strings.Contains(h.logs.all(), "CRITICAL") {
			t.Errorf("%s: a CRITICAL line beside the alert path: %s", x.name, h.logs.all())
		}
		// The period's line counts a settled refusal in its classes and
		// not among the would-admits.
		h.edge.full.flush()
		if want := "would_admit_python_refused=" + strconv.Itoa(x.alerts) + " "; !strings.Contains(h.logs.all(), want) ||
			!strings.Contains(h.logs.all(), `"`+x.class+`":1`) {
			t.Errorf("%s: the period's line: %s", x.name, h.logs.all())
		}
		h.cache.mu.Lock()
		evicted, workspaces := h.cache.evicted, h.cache.workspaces
		h.cache.mu.Unlock()
		if len(evicted) != 1 || len(workspaces) != 1 || workspaces[0] != c.Facts.Key.WorkspaceID {
			t.Errorf("%s: evicted %v and %v", x.name, evicted, workspaces)
		}
		if evictedAtRead != 1 {
			t.Errorf("%s: %d eviction(s) before the credit read", x.name, evictedAtRead)
		}
	}
}

// incidentHold is the hold of the 2026-11-18 07:42:14Z page (tr-fastpath-16):
// a workspace of 32 credit rows nearly out of credit while bursting holds of
// about $0.08, whose rows, read by the shadow after Python's refusal, covered
// the hold by their sum (87,331), a hold released since Python's read.
const incidentHold = 82_074

// incidentRows are 32 rows of headroom whose sum is sum and largest most.
func incidentRows(sum, most int64) []int64 {
	rows := make([]int64, 32)
	rows[0] = most
	for i, left := 1, sum-most; left > 0; i++ {
		rows[i] = min(most, left)
		left -= rows[i]
	}
	return rows
}

// TestTheIncidentsRaceIsCountedNotPaged: the 2026-11-18 07:42:14Z page, by
// its own figures. Python refused a hold of 82,074 to a workspace of 32
// rows; the shadow's read after it covered the hold by the sum (87,331, the
// largest row 40,896). Python's read, named on its 402 and re-read by the
// shadow at exactly its timestamp, did not (75,869, the sum the shadow read
// on a refusal two seconds before): a hold released between the two reads,
// funding_race, counted and never paged, its line carrying all three reads.
// The same refusal from a Python that names no read pages as it did.
func TestTheIncidentsRaceIsCountedNotPaged(t *testing.T) {
	pythonAt := time.Date(2026, 11, 18, 7, 42, 14, 889_123_456, time.UTC)
	named := "read=remembered read_at=2026-11-18T07:42:14.889123456Z hold=82074 rows=32 sum=75869 most=40896"
	dec := &Decision{Workspace: "ws-made-up-incident", Reservation: incidentHold}
	race := ClassFundingRace + ".402." + creditRefusal
	for _, x := range []struct {
		name, header, class, line string
		alerts, rereads           int
	}{
		{"Python names its read", named, race, "class=" + race + " funding=covered hold=82074 rows=32 sum=87331 " +
			"most=40896 race=confirmed python=remembered python_at=2026-11-18T07:42:14.889123456Z python_hold=82074 " +
			"python_rows=32 python_sum=75869 python_most=40896 reread_rows=32 reread_sum=75869 reread_most=40896 digest",
			0, 1},
		{"Python names none", "", fundingCandidate, "class=" + fundingCandidate + " funding=covered hold=82074 " +
			"rows=32 sum=87331 most=40896 race=none digest", 1, 0},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: incidentRows(87_331, 40_896)},
			standingAt: store.CreditStanding{Headroom: incidentRows(75_869, 40_896)}}
		sh, alerts, logs := fundingShadow(t, credit, 1e9)
		r := fullResult{class: fundingCandidate}
		sh.confirmFunding(dec, x.header, &r)
		sh.record("digest", r)
		paged := alerts.taken()[AlertWouldAdmit]
		rereads := credit.readsAt()
		if r.class != x.class || len(paged) != x.alerts || len(rereads) != x.rereads {
			t.Errorf("%s: class %s alerts %q re-reads %v", x.name, r.class, paged, rereads)
			continue
		}
		if x.rereads == 1 && !rereads[0].Equal(pythonAt) {
			t.Errorf("%s: re-read at %v, not Python's read timestamp", x.name, rereads)
		}
		if x.alerts == 1 && paged[0] != "digest: the edge would have answered an authorize Python refused: "+
			fundingCandidate+" funding=covered race=none" {
			t.Errorf("%s: the alert %q", x.name, paged[0])
		}
		if !logs.has("fastpath edge decide shadow funding: " + x.line) {
			t.Errorf("%s: no funding line %q: %s", x.name, x.line, logs.all())
		}
	}
}

// TestACoveredCreditRefusalIsSettledByPythonsRead: rows that cover the hold,
// unmarked, after Python's credit refusal are a race (funding_race, counted,
// never paged) only when the read Python's 402 names (its
// X-TR-Credit-Refusal-Read) was held to the same hold, neither a row nor the
// sum it read reached it, and, for a snapshot, the same rows re-read at its
// exact read timestamp, unmarked, did not either. A locked read (no read
// timestamp) is taken on Python's figures, with no re-read. Everything else
// pages as before, the reason in the alert: no read named, one not in
// Python's form, another hold, Python's own read reaching the hold, no
// re-read (the limiter, a failure), or re-read rows that are marked or reach
// the hold.
func TestACoveredCreditRefusalIsSettledByPythonsRead(t *testing.T) {
	const hold = 1_000
	pythonAt := time.Date(2026, 11, 18, 7, 42, 14, 7, time.UTC)
	snapshot := func(hold, rows, sum, most int64) string {
		return "read=remembered read_at=2026-11-18T07:42:14.000000007Z hold=" + strconv.FormatInt(hold, 10) +
			" rows=" + strconv.FormatInt(rows, 10) + " sum=" + strconv.FormatInt(sum, 10) + " most=" +
			strconv.FormatInt(most, 10)
	}
	locked := func(hold, rows, sum, most int64) string {
		return "read=rebalance hold=" + strconv.FormatInt(hold, 10) + " rows=" + strconv.FormatInt(rows, 10) +
			" sum=" + strconv.FormatInt(sum, 10) + " most=" + strconv.FormatInt(most, 10)
	}
	short := store.CreditStanding{Headroom: []int64{600, 300}}
	failed := errors.New("synthetic credit read failure")
	race := ClassFundingRace + ".402." + creditRefusal
	for _, x := range []struct {
		name    string
		header  string
		at      store.CreditStanding
		errAt   error
		rate    float64
		class   string
		race    string
		rereads int
	}{
		{"a snapshot short, its rows re-read short", snapshot(hold, 2, 900, 600), short, nil, 1e9, race,
			raceConfirmed, 1},
		{"a snapshot short by its sum beside a row that owes, re-read so", snapshot(hold, 2, -5, 999),
			store.CreditStanding{Headroom: []int64{999, -1004}}, nil, 1e9, race, raceConfirmed, 1},
		{"a snapshot short, its rows re-read reaching the hold by a row", snapshot(hold, 2, 900, 600),
			store.CreditStanding{Headroom: []int64{1000, -100}}, nil, 1e9, fundingCandidate, raceRereadCovers, 1},
		{"a snapshot short, its rows re-read reaching the hold by the sum", snapshot(hold, 2, 900, 600),
			store.CreditStanding{Headroom: []int64{600, 400}}, nil, 1e9, fundingCandidate, raceRereadCovers, 1},
		{"a snapshot short, its rows re-read marked", snapshot(hold, 2, 900, 600),
			store.CreditStanding{Headroom: []int64{600, 300}, Marked: true}, nil, 1e9, fundingCandidate,
			raceRereadMarked, 1},
		{"a snapshot short, its re-read failed", snapshot(hold, 2, 900, 600), short, failed, 1e9, fundingCandidate,
			raceUnread, 1},
		{"a snapshot short, no token for its re-read", snapshot(hold, 2, 900, 600), short, nil, 1, fundingCandidate,
			raceLimiter, 0},
		{"a locked read short", locked(hold, 2, 999, 999), short, nil, 1e9, race, raceConfirmed, 0},
		{"a locked read short by its sum beside a row that owes", locked(hold, 2, -5, 999), short, nil, 1e9, race,
			raceConfirmed, 0},
		{"Python's snapshot at the hold by its sum", snapshot(hold, 2, 1000, 600), short, nil, 1e9,
			fundingCandidate, racePythonCovers, 0},
		{"Python's locked read at the hold by a row", locked(hold, 2, 900, 1000), short, nil, 1e9, fundingCandidate,
			racePythonCovers, 0},
		{"Python held its read to another hold", snapshot(hold+1, 2, 900, 600), short, nil, 1e9, fundingCandidate,
			raceHold, 0},
		{"Python held its locked read to another hold", locked(hold-1, 2, 900, 600), short, nil, 1e9,
			fundingCandidate, raceHold, 0},
		{"no read named", "", short, nil, 1e9, fundingCandidate, raceNone, 0},
		{"a read timestamp of seven digits",
			"read=remembered read_at=2026-11-18T07:42:14.0000007Z hold=1000 rows=2 sum=900 most=600", short, nil,
			1e9, fundingCandidate, raceMalformed, 0},
		{"a read timestamp with an offset",
			"read=remembered read_at=2026-11-18T07:42:14.000000007+00:00 hold=1000 rows=2 sum=900 most=600", short,
			nil, 1e9, fundingCandidate, raceMalformed, 0},
		{"an empty read timestamp", "read=remembered read_at= hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"a read named in capitals", "read=Rebalance hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"the fields out of order", "hold=1000 read=rebalance rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"a field more", "read=rebalance hold=1000 rows=2 sum=900 most=600 marked=false", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"no rows", "read=rebalance hold=1000 rows=0 sum=0 most=0", short, nil, 1e9, fundingCandidate,
			raceMalformed, 0},
		{"a hold past int64", "read=rebalance hold=9223372036854775808 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"a snapshot of fewer rows than the shadow's read", snapshot(hold, 1, 900, 900), short, nil, 1e9,
			fundingCandidate, raceRows, 0},
		{"a snapshot of more rows than the shadow's read", snapshot(hold, 3, 900, 600), short, nil, 1e9,
			fundingCandidate, raceRows, 0},
		{"a locked read of fewer rows than the shadow's read", locked(hold, 1, 900, 900), short, nil, 1e9,
			fundingCandidate, raceRows, 0},
		{"a snapshot whose rows re-read at its timestamp are more", snapshot(hold, 2, 900, 600),
			store.CreditStanding{Headroom: []int64{600, 300, 0}}, nil, 1e9, fundingCandidate, raceRows, 1},
		{"a remembered read with no read timestamp", "read=remembered hold=1000 rows=2 sum=900 most=600", short, nil,
			1e9, fundingCandidate, raceReadAtNone, 0},
		{"a heal read with no read timestamp", "read=heal hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceReadAtNone, 0},
		{"a headroom read with no read timestamp", "read=headroom hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceReadAtNone, 0},
		{"a locked read with a read timestamp",
			"read=rebalance read_at=2026-11-18T07:42:14.000000007Z hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"a read of a name Python does not write", "read=lease hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
		{"a read of a name Python does not write, with a read timestamp",
			"read=lease read_at=2026-11-18T07:42:14.000000007Z hold=1000 rows=2 sum=900 most=600", short, nil, 1e9,
			fundingCandidate, raceMalformed, 0},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: []int64{1000, 500}}, standingAt: x.at,
			errAt: x.errAt}
		sh, alerts, logs := fundingShadow(t, credit, x.rate)
		r := fullResult{class: fundingCandidate}
		sh.confirmFunding(&Decision{Workspace: "ws-made-up", Reservation: hold}, x.header, &r)
		sh.record("digest", r)
		rereads := credit.readsAt()
		if r.class != x.class || r.funding == nil || r.funding.outcome != fundingCovered || r.funding.race != x.race ||
			len(rereads) != x.rereads || (x.rereads == 1 && !rereads[0].Equal(pythonAt)) {
			t.Errorf("%s: class %s funding %+v re-reads %v; want %s race=%s and %d re-read(s)", x.name, r.class,
				r.funding, rereads, x.class, x.race, x.rereads)
			continue
		}
		paged := alerts.taken()[AlertWouldAdmit]
		want := "digest: the edge would have answered an authorize Python refused: " + fundingCandidate +
			" funding=covered race=" + x.race
		if x.class == race && len(paged) != 0 {
			t.Errorf("%s: a race paged: %q", x.name, paged)
		}
		if x.class == fundingCandidate && (len(paged) != 1 || paged[0] != want) {
			t.Errorf("%s: alerts %q, want %q", x.name, paged, want)
		}
		if !logs.has("fastpath edge decide shadow funding: class=" + x.class + " funding=covered hold=1000 rows=2 " +
			"sum=1500 most=1000 race=" + x.race) {
			t.Errorf("%s: no funding line: %s", x.name, logs.all())
		}
		if n := sh.totals.classes[x.class]; n != 1 {
			t.Errorf("%s: classes %v", x.name, sh.totals.classes)
		}
	}
}

// TestOnlyCoveringRowsConsultPythonsRead: the read Python's 402 names
// settles nothing but rows that cover the hold: rows short of it are the
// balance (funding_unchecked) and marked rows page, whatever Python's read
// says, with no read at its timestamp.
func TestOnlyCoveringRowsConsultPythonsRead(t *testing.T) {
	header := "read=remembered read_at=2026-11-18T07:42:14.000000007Z hold=1000 rows=2 sum=1500 most=1000"
	for _, x := range []struct {
		name     string
		standing store.CreditStanding
		class    string
		outcome  string
	}{
		{"rows short of the hold", store.CreditStanding{Headroom: []int64{600, 300}},
			ClassFundingUnchecked + ".402." + creditRefusal, fundingShort},
		{"marked rows that cover", store.CreditStanding{Headroom: []int64{1000, 500}, Marked: true},
			fundingCandidate, fundingMarked},
	} {
		credit := &fakeCredit{standing: x.standing, standingAt: store.CreditStanding{Headroom: []int64{1}}}
		sh, _, _ := fundingShadow(t, credit, 1e9)
		r := fullResult{class: fundingCandidate}
		sh.confirmFunding(&Decision{Workspace: "ws-made-up", Reservation: 1000}, header, &r)
		if r.class != x.class || r.funding.outcome != x.outcome || r.funding.race != "" || len(credit.readsAt()) != 0 {
			t.Errorf("%s: class %s funding %+v re-reads %v", x.name, r.class, r.funding, credit.readsAt())
		}
	}
}

// TestTheFollowedRefusalCarriesPythonsReadToTheShadow: through the edge, the
// read Python's 402 names rides on its answer's header to the shadow, which
// re-reads the rows at its timestamp: rows short there, covering after, are
// counted as the race, with no alert.
func TestTheFollowedRefusalCarriesPythonsReadToTheShadow(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	dec := Decide(in).ForShadow()
	if dec == nil || dec.Reservation < 2 {
		t.Fatalf("the decision: %+v", dec)
	}
	hold := dec.Reservation
	pythonAt := time.Date(2026, 11, 18, 7, 42, 14, 889_123_456, time.UTC)
	header := "read=heal read_at=" + pythonAt.Format(readAtLayout) + " hold=" + strconv.FormatInt(hold, 10) +
		" rows=2 sum=" + strconv.FormatInt(hold-1, 10) + " most=" + strconv.FormatInt(hold-1, 10)
	credit := &fakeCredit{standing: store.CreditStanding{Headroom: []int64{hold, 0}},
		standingAt: store.CreditStanding{Headroom: []int64{hold - 1, 0}}}
	taken := &alerts{}
	h := newFullEdge(t, f, c, nil, func(cfg *DecideShadowConfig) {
		cfg.Credit = credit
		cfg.Alert = taken.raise
	})
	h.answer(answerWith(402, errorBody(402, creditRefusal, "Insufficient credits"), CreditRefusalReadHeader, header))
	h.send(t, c)
	race := ClassFundingRace + ".402." + creditRefusal
	got := h.edge.DecideShadowCounts()
	rereads := credit.readsAt()
	if got.Classes[race] != 1 || len(got.Classes) != 1 || len(taken.taken()[AlertWouldAdmit]) != 0 ||
		len(rereads) != 1 || !rereads[0].Equal(pythonAt) {
		t.Errorf("classes %v alerts %v re-reads %v", got.Classes, taken.taken(), rereads)
	}
}

// TestPythonsCreditRefusalReadVectorsParse: every header Python writes
// (fastpath/testdata/credit_refusal_read_vectors_v1.json, written from
// trusted_router.credit_refusal_read and held to it by
// tests/test_credit_refusal_read.py) reads back as the read it names: the
// header's name, the read, its timestamp to the nanosecond (none for a
// locked read), the hold and the rows' figures, int64's bounds included.
func TestPythonsCreditRefusalReadVectorsParse(t *testing.T) {
	text, err := os.ReadFile(filepath.Join("..", "..", "testdata", "credit_refusal_read_vectors_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Header string `json:"header"`
		Cases  []struct {
			Read            string `json:"read"`
			ReadTimestampNs *int64 `json:"read_timestamp_ns"`
			Hold            int64  `json:"hold"`
			Rows            int    `json:"rows"`
			Sum             int64  `json:"sum"`
			Most            int64  `json:"most"`
			Header          string `json:"header"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(text, &v); err != nil {
		t.Fatal(err)
	}
	if v.Header != CreditRefusalReadHeader || len(v.Cases) < 7 {
		t.Fatalf("the vectors name %q with %d case(s)", v.Header, len(v.Cases))
	}
	for _, c := range v.Cases {
		got, why := parseCreditRefusalRead(c.Header)
		if why != "" || got == nil {
			t.Errorf("%q: %s", c.Header, why)
			continue
		}
		wantAt := time.Time{}
		if c.ReadTimestampNs != nil {
			wantAt = time.Unix(0, *c.ReadTimestampNs).UTC()
		}
		if got.read != c.Read || !got.at.Equal(wantAt) || got.at.IsZero() != wantAt.IsZero() || got.hold != c.Hold ||
			got.rows != c.Rows || got.sum != c.Sum || got.most != c.Most {
			t.Errorf("%q read as %+v", c.Header, *got)
		}
	}
}

// TestALockedReadOfFewerRowsPages is the review's probe: Python's rebalance
// read rows 0 to 15 of a count it held while the workspace has 32 rows, and
// the 16 it read are short of the hold while the 32 the shadow read cover
// it. Python's figures are not of the rows there are, so nothing is
// confirmed: it pages race=rows. The same read of all 32 rows is the race.
func TestALockedReadOfFewerRowsPages(t *testing.T) {
	covering := make([]int64, 32)
	for i := range covering {
		covering[i] = 40
	}
	for _, x := range []struct {
		name, header, class, race string
		alerts                    int
	}{
		{"16 of 32 rows", "read=rebalance hold=1000 rows=16 sum=640 most=40", fundingCandidate, raceRows, 1},
		{"all 32 rows", "read=rebalance hold=1000 rows=32 sum=640 most=40", ClassFundingRace + ".402." + creditRefusal,
			raceConfirmed, 0},
	} {
		credit := &fakeCredit{standing: store.CreditStanding{Headroom: covering}}
		sh, alerts, _ := fundingShadow(t, credit, 1e9)
		r := fullResult{class: fundingCandidate}
		sh.confirmFunding(&Decision{Workspace: "ws-made-up", Reservation: 1000}, x.header, &r)
		sh.record("digest", r)
		if r.class != x.class || r.funding.race != x.race || len(alerts.taken()[AlertWouldAdmit]) != x.alerts {
			t.Errorf("%s: class %s race %s alerts %v", x.name, r.class, r.funding.race, alerts.taken())
		}
	}
}

// TestTheFundingRaceRateIsWatched: confirmed races past FundingRaceShare of
// the hour's credit refusals whose rows were read, and at least
// FundingRaceMin of them, raise AlertFundingRace; a share at or under it, too
// few races, or races older than the hour raise nothing. Refusals with no
// read (the limiter, no reader) are not in the share.
func TestTheFundingRaceRateIsWatched(t *testing.T) {
	race := fullResult{class: ClassFundingRace + ".402." + creditRefusal,
		funding: &fundingRead{outcome: fundingCovered, race: raceConfirmed}}
	short := fullResult{class: ClassFundingUnchecked + ".402." + creditRefusal, funding: &fundingRead{outcome: fundingShort}}
	unread := fullResult{class: fundingCandidate, funding: &fundingRead{outcome: fundingLimiter}}
	t0 := time.Date(2026, 11, 18, 8, 0, 0, 0, time.UTC)
	for _, x := range []struct {
		name                  string
		races, shorts, unread int
		alerts                int
	}{
		{"6 races of 100 reads", 6, 94, 0, 1},
		{"5 races of 100 reads, at the share", 5, 95, 0, 0},
		{"4 races of 4 reads, too few", 4, 0, 0, 0},
		{"5 races of 5 reads beside refusals with no read", 5, 0, 500, 1},
	} {
		sh, alerts, _ := fundingShadow(t, &fakeCredit{}, 1e9)
		for i := 0; i < x.races; i++ {
			sh.record("digest", race)
		}
		for i := 0; i < x.shorts; i++ {
			sh.record("digest", short)
		}
		for i := 0; i < x.unread; i++ {
			sh.record("digest", unread)
		}
		sh.mu.Lock()
		sh.fundingRaceAlerts(t0)
		sh.mu.Unlock()
		got := alerts.taken()[AlertFundingRace]
		if len(got) != x.alerts {
			t.Errorf("%s: alerts %q", x.name, got)
		}
		if x.alerts == 1 && !strings.Contains(got[0], "confirmed "+strconv.Itoa(x.races)+" funding races of "+
			strconv.Itoa(x.races+x.shorts)+" credit refusals") {
			t.Errorf("%s: the alert %q", x.name, got[0])
		}
		// An hour on, with nothing new, the races have left the window.
		sh.mu.Lock()
		sh.fundingRaceAlerts(t0.Add(raceWindow + time.Minute))
		sh.mu.Unlock()
		if again := alerts.taken()[AlertFundingRace]; len(again) != 0 {
			t.Errorf("%s: alerts once the hour passed: %q", x.name, again)
		}
	}
	// The period's flush closes the period and raises it the same way.
	sh, alerts, _ := fundingShadow(t, &fakeCredit{}, 1e9)
	for i := 0; i < FundingRaceMin; i++ {
		sh.record("digest", race)
	}
	sh.flush()
	if got := alerts.taken()[AlertFundingRace]; len(got) != 1 {
		t.Errorf("the flush raised %q", got)
	}
}
