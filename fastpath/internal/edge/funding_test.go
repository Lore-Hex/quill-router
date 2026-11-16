package edge

import (
	"context"
	"errors"
	"math"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// fakeCredit is a ShadowCreditReader: what it answers, and the workspaces
// it was asked for.
type fakeCredit struct {
	mu       sync.Mutex
	standing store.CreditStanding
	err      error
	asked    []string
}

func (f *fakeCredit) ReadCreditStanding(ctx context.Context, workspace string) (store.CreditStanding, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, workspace)
	if _, bounded := ctx.Deadline(); !bounded {
		return store.CreditStanding{}, errors.New("the credit read has no deadline")
	}
	return f.standing, f.err
}

func (f *fakeCredit) reads() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.asked...)
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
		sh.confirmFunding(dec, &r)
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
		sh.confirmFunding(x.d.ForShadow(), &r)
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
	}
}
