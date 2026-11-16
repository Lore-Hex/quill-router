package edge

import (
	"bytes"
	"context"
	"errors"
	"maps"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The real cache tells the shadow its maximum age: were the method renamed,
// the triage would fall back to the default without a word.
var _ cacheAge = (*gwstate.Cache)(nil)

// The debt candidate's suffix, and its five classes.
const (
	creditSuffix        = ".402." + creditRefusal
	debtMiss            = ClassWouldAdmit + creditSuffix
	debtUnmarked        = ClassDebtUnmarked + creditSuffix
	debtLag             = ClassDebtLag + creditSuffix
	debtUnmarkedEarlier = ClassDebtUnmarkedEarlier + creditSuffix
	debtUnconfirmed     = ClassDebtUnconfirmed + creditSuffix
)

// fakeDebts is the store's read of a workspace's debt mark at an instant:
// read answers each call, in order (its index from 0), and the calls are
// kept with what was left of each one's deadline.
type fakeDebts struct {
	mu    sync.Mutex
	read  func(call int, at time.Time) (store.DebtMark, error)
	calls []debtCall
}

type debtCall struct {
	workspace string
	at        time.Time
	budget    time.Duration
}

func (f *fakeDebts) ReadDebtMarkAt(ctx context.Context, workspace string, at time.Time) (store.DebtMark, error) {
	deadline, bounded := ctx.Deadline()
	budget := time.Duration(-1)
	if bounded {
		budget = time.Until(deadline)
	}
	f.mu.Lock()
	n := len(f.calls)
	f.calls = append(f.calls, debtCall{workspace: workspace, at: at, budget: budget})
	read := f.read
	f.mu.Unlock()
	return read(n, at)
}

func (f *fakeDebts) seen() []debtCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.calls)
}

// marksOf answers the triage's two reads, the earlier then the later, with
// the marks given, each of a workspace of 16 credit rows.
func marksOf(earlier, later bool) func(int, time.Time) (store.DebtMark, error) {
	return func(call int, _ time.Time) (store.DebtMark, error) {
		return store.DebtMark{Rows: 16, Marked: []bool{earlier, later}[call]}, nil
	}
}

// debtShadow is a bare shadow for the triage: debts reads the marks, alerts
// are taken.
func debtShadow(t *testing.T, debts DebtMarkReaderAt) (*fullShadow, *alerts, *logLines) {
	t.Helper()
	a, logs := &alerts{}, &logLines{}
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{},
		Rows: &fakeRows{by: map[string]store.AuthorizationForCompare{}}, Debts: debts, ReadsPerSecond: 1e9,
		Alert: a.raise}, logs.logf, time.Now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return sh, a, logs
}

// TestTheDebtMarksClassTheCandidate: the triage's verdict from its two
// reads, every pairing of what each can be: not made (no reader, the bucket
// empty), failed, of no credit rows, unmarked, marked. The earlier read
// alone decides the page. Only credit rows with no mark there clear the
// candidate, and then nothing the later read says or fails to say pages:
// unmarked at both is debt_unmarked, marked at the later only debt_lag, and
// a later read that failed or found no rows debt_unmarked_earlier, which
// claims no more than the earlier read proved. An earlier read that was not
// made, failed or found no rows pages whatever the later one would say (an
// empty read is not an unmarked workspace), and so does a mark there: the
// would-admit when the later read finds it too, debt_unconfirmed when it
// finds none, no rows, or fails.
func TestTheDebtMarksClassTheCandidate(t *testing.T) {
	read := func(n int, marked bool) debtRead { return debtRead{mark: store.DebtMark{Rows: n, Marked: marked}} }
	not := func(why string) debtRead { return debtRead{why: why} }
	laters := map[string]debtRead{"unread": not("unread"), "no rows": read(0, false), "unmarked": read(16, false),
		"marked": read(16, true)}
	// The earlier reads that cannot clear the candidate: the same class,
	// paging, whatever the later read is.
	for name, x := range map[string]struct {
		earlier debtRead
		count   string
	}{
		"no reader":        {not("none"), "debt.unconfirmed.none"},
		"the bucket empty": {not("limiter"), "debt.unconfirmed.limiter"},
		"a failed read":    {not("unread"), "debt.unconfirmed.unread"},
		"no credit rows":   {read(0, false), "debt.unconfirmed.rows"},
		// Marked with no rows is not a thing the store reads; were it, it
		// would page too.
		"no credit rows, marked": {read(0, true), "debt.unconfirmed.rows"},
	} {
		for laterName, later := range laters {
			class, count := debtMarks{earlier: x.earlier, later: later}.class()
			if class != ClassDebtUnconfirmed || count != x.count || !pages(class+creditSuffix) {
				t.Errorf("the earlier read %s, the later %s: %s %s", name, laterName, class, count)
			}
		}
	}
	for _, x := range []struct {
		name           string
		earlier, later debtRead
		class          string
		count          string
		pages          bool
	}{
		{"unmarked at both", read(16, false), read(16, false), ClassDebtUnmarked, "debt.unmarked", false},
		{"one row, unmarked at both", read(1, false), read(1, false), ClassDebtUnmarked, "debt.unmarked", false},
		{"the shards grew between the reads", read(1, false), read(16, false), ClassDebtUnmarked, "debt.unmarked",
			false},
		{"marked at the later only", read(16, false), read(16, true), ClassDebtLag, "debt.lag", false},
		{"unmarked at the earlier, the later read failed", read(16, false), not("unread"), ClassDebtUnmarkedEarlier,
			"debt.unmarked_earlier.unread", false},
		{"unmarked at the earlier, no rows at the later", read(16, false), read(0, false), ClassDebtUnmarkedEarlier,
			"debt.unmarked_earlier.rows", false},
		{"marked at both", read(16, true), read(16, true), ClassWouldAdmit, "debt.marked", true},
		{"marked at the earlier only", read(16, true), read(16, false), ClassDebtUnconfirmed,
			"debt.unconfirmed.cleared", true},
		{"marked at the earlier, the later read failed", read(16, true), not("unread"), ClassDebtUnconfirmed,
			"debt.unconfirmed.unread", true},
		{"marked at the earlier, no rows at the later", read(16, true), read(0, false), ClassDebtUnconfirmed,
			"debt.unconfirmed.rows", true},
	} {
		class, count := debtMarks{earlier: x.earlier, later: x.later}.class()
		if class != x.class || count != x.count || pages(class+creditSuffix) != x.pages {
			t.Errorf("%s: %s %s (pages %v), want %s %s (pages %v)", x.name, class, count, pages(class+creditSuffix),
				x.class, x.count, x.pages)
		}
	}
	// The rule over every pairing: it pages unless the earlier read was made
	// and found credit rows with no mark.
	earliers := map[string]debtRead{"none": not("none"), "limiter": not("limiter"), "unread": not("unread"),
		"no rows": read(0, false), "unmarked": read(16, false), "marked": read(16, true)}
	for earlierName, earlier := range earliers {
		for laterName, later := range laters {
			class, count := debtMarks{earlier: earlier, later: later}.class()
			if got, want := pages(class+creditSuffix), earlierName != "unmarked"; got != want {
				t.Errorf("the earlier read %s, the later %s: %s %s pages %v, want %v", earlierName, laterName, class,
					count, got, want)
			}
		}
	}
}

// TestOnlyThePagingClassesPage: of every class the shadow counts, the alert
// is raised for a would-admit (the debt gate's miss among them), a window
// race no re-read settled and a debt candidate its earlier read did not
// clear, and for no other: not the three debt classes of a workspace
// unmarked at the earlier instant, nor a class whose name a paging class's
// begins or ends.
func TestOnlyThePagingClassesPage(t *testing.T) {
	for class, want := range map[string]bool{
		debtMiss:                           true,
		debtUnconfirmed:                    true,
		ClassWouldAdmit + ".403.forbidden": true,
		ClassWouldAdmit + ".402.unread":    true,
		ClassWouldAdmit + ".402.credits_not_on_this_plane":   true,
		ClassWouldAdmit + ".429." + windowRefusal:            true,
		ClassWindowRaceUnconfirmed + ".429." + windowRefusal: true,
		debtUnmarked:        false,
		debtLag:             false,
		debtUnmarkedEarlier: false,
		ClassWindowRace + ".429." + windowRefusal:       false,
		ClassWindowUnchecked + ".429." + windowRefusal:  false,
		ClassInFlightUnchecked + ".owner.off":           false,
		ClassGoPassed + "." + PassWorkspaceInDebt:       false,
		ClassPythonAnsweredGoPassed + ".owner.busy":     false,
		ClassStale + ".workspace":                       false,
		ClassPythonError + ".500":                       false,
		ClassUnread + ".wait":                           false,
		ClassAgreeFull:                                  false,
		ClassDifferAny:                                  false,
		ClassPythonUnavailable:                          false,
		ClassWouldAdmit:                                 false,
		ClassDebtUnconfirmed:                            false,
		"debt_unconfirmed_not.402.insufficient_credits": false,
	} {
		if got := pages(class); got != want {
			t.Errorf("%s: pages %v, want %v", class, got, want)
		}
		sh, a, _ := debtShadow(t, nil)
		sh.record("snapshot=s policy=p", fullResult{class: class, decided: true})
		if raised := len(a.taken()[AlertWouldAdmit]) == 1; raised != want {
			t.Errorf("%s: alert raised %v, want %v", class, raised, want)
		}
	}
}

// TestTheDebtInstantsAreInsideTheSpanOfUsableFacts: the earlier read is at
// the request's instant less the facts' age, moved later by the clocks'
// allowance (a second of the default 15, so 14 s before the request), and
// never later than halfway to the request for an age under two allowances;
// the later is the end of Python's answer, however long after.
func TestTheDebtInstantsAreInsideTheSpanOfUsableFacts(t *testing.T) {
	at := time.Date(2026, 11, 16, 0, 42, 10, 0, time.UTC)
	answered := at.Add(700 * time.Millisecond)
	for _, x := range []struct {
		age     time.Duration
		earlier time.Duration
	}{
		{gwstate.DefaultMaxAge, -14 * time.Second},
		{40 * time.Second, -39 * time.Second},
		{2 * time.Second, -time.Second},
		{time.Second, -500 * time.Millisecond},
		{100 * time.Millisecond, -50 * time.Millisecond},
	} {
		earlier, later := debtInstants(at, answered, x.age)
		if !earlier.Equal(at.Add(x.earlier)) || !later.Equal(answered) {
			t.Errorf("age %s: %s and %s, want %s before the request and the answer", x.age, earlier.Sub(at),
				later.Sub(at), -x.earlier)
		}
		if earlier.Before(at.Add(-x.age)) || !earlier.Before(at) {
			t.Errorf("age %s: the earlier read at %s is outside the span of usable facts", x.age, earlier.Sub(at))
		}
	}
	if gwstate.DefaultMaxAge != 15*time.Second || rowSkew != time.Second {
		t.Fatalf("the facts' age %s and the clocks' allowance %s: the runbook says 15 s and 1 s",
			gwstate.DefaultMaxAge, rowSkew)
	}
	// A slow answer moves the later read alone.
	if earlier, later := debtInstants(at, at.Add(29*time.Second), gwstate.DefaultMaxAge); !earlier.Equal(
		at.Add(-14*time.Second)) || !later.Equal(at.Add(29*time.Second)) {
		t.Errorf("a slow answer: %s and %s", earlier.Sub(at), later.Sub(at))
	}
}

// TestTheTriageReadsTheWorkspaceAtItsTwoInstants: the triage reads the
// request's workspace at its two instants, the earlier first, each within
// WindowReadBudget, for two tokens of its own bucket taken together, and
// gives the candidate its class by what the reads say, with the count
// beside it. With fewer than two tokens it makes no read at all, takes
// none, and pages: no read is spent on a triage the bucket would not let
// finish. No reader or no workspace makes none either, and takes no token.
// A failed earlier read, or one of no credit rows, makes no later one and
// pages. After an unmarked earlier read, a later read that fails or finds
// no rows does not page: debt_unmarked_earlier. After a marked one it does.
func TestTheTriageReadsTheWorkspaceAtItsTwoInstants(t *testing.T) {
	at := time.Date(2026, 11, 16, 0, 42, 10, 0, time.UTC)
	answered := at.Add(700 * time.Millisecond)
	failed := errors.New("synthetic read failure")
	// reads answers the earlier and the later read: a mark of 16 rows, of no
	// rows ("none"), or a failure ("fail").
	reads := func(earlier, later string) func(int, time.Time) (store.DebtMark, error) {
		return func(call int, _ time.Time) (store.DebtMark, error) {
			switch what := []string{earlier, later}[call]; what {
			case "fail":
				return store.DebtMark{}, failed
			case "none":
				return store.DebtMark{}, nil
			default:
				return store.DebtMark{Rows: 16, Marked: what == "marked"}, nil
			}
		}
	}
	for _, x := range []struct {
		name      string
		read      func(int, time.Time) (store.DebtMark, error)
		workspace string
		tokens    float64
		noReader  bool
		class     string
		count     string
		reads     int
		taken     float64
	}{
		{"unmarked at both", reads("unmarked", "unmarked"), "ws-1", 2, false, debtUnmarked, "debt.unmarked", 2, 2},
		{"marked at the later only", reads("unmarked", "marked"), "ws-1", 2, false, debtLag, "debt.lag", 2, 2},
		{"marked at both", reads("marked", "marked"), "ws-1", 2, false, debtMiss, "debt.marked", 2, 2},
		{"marked at the earlier only", reads("marked", "unmarked"), "ws-1", 2, false, debtUnconfirmed,
			"debt.unconfirmed.cleared", 2, 2},
		{"unmarked at the earlier, the later read failed", reads("unmarked", "fail"), "ws-1", 2, false,
			debtUnmarkedEarlier, "debt.unmarked_earlier.unread", 2, 2},
		{"unmarked at the earlier, no rows at the later", reads("unmarked", "none"), "ws-1", 2, false,
			debtUnmarkedEarlier, "debt.unmarked_earlier.rows", 2, 2},
		{"marked at the earlier, the later read failed", reads("marked", "fail"), "ws-1", 2, false, debtUnconfirmed,
			"debt.unconfirmed.unread", 2, 2},
		{"marked at the earlier, no rows at the later", reads("marked", "none"), "ws-1", 2, false, debtUnconfirmed,
			"debt.unconfirmed.rows", 2, 2},
		{"the earlier read failed", reads("fail", "marked"), "ws-1", 2, false, debtUnconfirmed,
			"debt.unconfirmed.unread", 1, 2},
		{"no rows at the earlier instant", reads("none", "unmarked"), "ws-1", 2, false, debtUnconfirmed,
			"debt.unconfirmed.rows", 1, 2},
		{"the bucket empty", reads("unmarked", "unmarked"), "ws-1", 0, false, debtUnconfirmed,
			"debt.unconfirmed.limiter", 0, 0},
		{"one token in the bucket", reads("unmarked", "unmarked"), "ws-1", 1, false, debtUnconfirmed,
			"debt.unconfirmed.limiter", 0, 0},
		{"just under two tokens in the bucket", reads("unmarked", "unmarked"), "ws-1", 1.999, false,
			debtUnconfirmed, "debt.unconfirmed.limiter", 0, 0},
		{"no workspace", reads("unmarked", "unmarked"), "", 2, false, debtUnconfirmed, "debt.unconfirmed.none", 0, 0},
		{"no reader", reads("unmarked", "unmarked"), "ws-1", 2, true, debtUnconfirmed, "debt.unconfirmed.none", 0, 0},
	} {
		debts := &fakeDebts{read: x.read}
		sh, _, _ := debtShadow(t, debts)
		if x.noReader {
			sh.cfg.Debts = nil
		}
		// Buckets that do not refill: the triage's with the tokens given,
		// the window reads' and the row read-backs' with three each.
		sh.cfg.ReadsPerSecond = 1e-9
		sh.debtLimiter.tokens, sh.windowLimiter.tokens, sh.limiter.tokens = x.tokens, 3, 3
		r := fullResult{class: debtCandidate, decided: true, windows: []string{"read.made"}}
		sh.triageDebt(x.workspace, at, answered, &r)
		if r.class != x.class || !slices.Equal(r.windows, []string{"read.made", x.count}) || !r.decided {
			t.Errorf("%s: %s %v, want %s and %s", x.name, r.class, r.windows, x.class, x.count)
		}
		calls := debts.seen()
		if len(calls) != x.reads {
			t.Fatalf("%s: %d reads, want %d: %+v", x.name, len(calls), x.reads, calls)
		}
		for i, call := range calls {
			want := []time.Time{at.Add(-14 * time.Second), answered}[i]
			if call.workspace != "ws-1" || !call.at.Equal(want) || call.budget <= 0 || call.budget > WindowReadBudget {
				t.Errorf("%s: read %d of %q at %s within %s, want ws-1 at %s within %s", x.name, i, call.workspace,
					call.at, call.budget, want, WindowReadBudget)
			}
		}
		// Both tokens or neither, of the triage's bucket alone.
		if left := sh.debtLimiter.tokens; left < x.tokens-x.taken-1e-6 || left > x.tokens-x.taken+1e-6 {
			t.Errorf("%s: the triage's bucket holds %v of %v, want %v taken", x.name, left, x.tokens, x.taken)
		}
		if sh.windowLimiter.tokens != 3 || sh.limiter.tokens != 3 {
			t.Errorf("%s: the triage took from another bucket: window %v, rows %v", x.name, sh.windowLimiter.tokens,
				sh.limiter.tokens)
		}
	}
}

// TestABurstOfTriagesLeavesTheWindowChecksTheirReads: the triage's bucket is
// its own. A burst of debt candidates drains it, after which each is
// refused with no read and pages, while the window checks' bucket holds
// every token it had: the shadow's window check and its race re-read still
// read. The other way, with the window checks' bucket empty the triage
// still reads. The bucket holds ReadsPerSecond tokens and refills at that
// rate, as the window reads' does, and never fewer than one triage's two.
func TestABurstOfTriagesLeavesTheWindowChecksTheirReads(t *testing.T) {
	at := time.Date(2026, 11, 16, 0, 42, 10, 0, time.UTC)
	triage := func(sh *fullShadow) string {
		r := fullResult{class: debtCandidate, decided: true}
		sh.triageDebt("ws-1", at, at.Add(time.Second), &r)
		return r.windows[0]
	}
	// Every read finds 16 rows and no mark.
	debts := &fakeDebts{read: func(int, time.Time) (store.DebtMark, error) { return store.DebtMark{Rows: 16}, nil }}
	sh, _, _ := debtShadow(t, debts)
	// No bucket refills; the triage's holds two triages' reads.
	sh.cfg.ReadsPerSecond = 1e-9
	sh.debtLimiter.tokens, sh.windowLimiter.tokens = 4, 3
	var counts []string
	for range 6 {
		counts = append(counts, triage(sh))
	}
	if want := []string{"debt.unmarked", "debt.unmarked", "debt.unconfirmed.limiter", "debt.unconfirmed.limiter",
		"debt.unconfirmed.limiter", "debt.unconfirmed.limiter"}; !slices.Equal(counts, want) {
		t.Fatalf("six triages on a bucket of four tokens: %v", counts)
	}
	if n := len(debts.seen()); n != 4 {
		t.Fatalf("%d reads for two triages read and four refused", n)
	}
	if sh.windowLimiter.tokens != 3 || !sh.windowSample() || sh.windowLimiter.tokens != 2 {
		t.Fatalf("the triages took from the window checks' three tokens, or their read was refused: %v left",
			sh.windowLimiter.tokens)
	}
	// The window checks' bucket empty: the triage reads all the same.
	sh.windowLimiter.tokens, sh.debtLimiter.tokens = 0, 2
	if got := triage(sh); got != "debt.unmarked" || len(debts.seen()) != 6 || sh.windowSample() {
		t.Fatalf("a triage with the window checks' bucket empty: %s after %d reads", got, len(debts.seen()))
	}
	// At the default rate, with the triage's bucket spent (and its clock
	// set ahead, so that it stays spent however slowly this test runs): the
	// triage is refused with no read, and the window checks' bucket still
	// gives every read it holds.
	busy, _, _ := debtShadow(t, debts)
	busy.cfg.ReadsPerSecond = DefaultRowReadsPerSecond
	busy.windowLimiter.tokens = DefaultRowReadsPerSecond
	busy.debtLimiter.tokens, busy.debtLimiter.at = 0, time.Now().Add(time.Hour)
	for range 3 {
		if got := triage(busy); got != "debt.unconfirmed.limiter" || len(debts.seen()) != 6 {
			t.Fatalf("a triage on a spent bucket: %s after %d reads", got, len(debts.seen()))
		}
	}
	for i := range DefaultRowReadsPerSecond {
		if !busy.windowSample() {
			t.Fatalf("the window checks' read %d of %d was refused while the triage's bucket was spent", i+1,
				DefaultRowReadsPerSecond)
		}
	}
	// The bucket's size and refill: ReadsPerSecond tokens, at least a
	// triage's two; at the default five a second, two triages at once and
	// the third refused until a token more has come back.
	for _, x := range []struct {
		perSecond float64
		holds     float64
	}{{DefaultRowReadsPerSecond, 5}, {1, 2}, {0.5, 2}, {8, 8}} {
		made, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{},
			Rows: &fakeRows{by: map[string]store.AuthorizationForCompare{}}, Debts: debts,
			ReadsPerSecond: x.perSecond}, (&logLines{}).logf, time.Now, time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		if made.debtLimiter.tokens != x.holds || made.windowLimiter.tokens != x.perSecond {
			t.Errorf("%v reads a second: the triage's bucket holds %v, the window checks' %v", x.perSecond,
				made.debtLimiter.tokens, made.windowLimiter.tokens)
		}
		// Long idle, the bucket is full and no fuller.
		made.debtLimiter.at = time.Now().Add(-time.Hour)
		made.debtLimiter.tokens = 0
		if !made.takeDebtReads() {
			t.Errorf("%v reads a second: an idle hour did not refill the bucket", x.perSecond)
		}
		if left := made.debtLimiter.tokens; left != x.holds-debtReads {
			t.Errorf("%v reads a second: the refilled bucket held %v, want %v", x.perSecond, left+debtReads, x.holds)
		}
	}
	// The refill is by the clock: emptied a second ago at five a second, the
	// bucket has its five back, two triages' worth and one token over.
	sh.cfg.ReadsPerSecond = DefaultRowReadsPerSecond
	sh.debtLimiter.tokens, sh.debtLimiter.at = 0, time.Now().Add(-time.Second)
	if !sh.takeDebtReads() || !sh.takeDebtReads() || sh.takeDebtReads() {
		t.Fatalf("a second's refill at five a second did not give two triages and refuse a third: %v left",
			sh.debtLimiter.tokens)
	}
}

// TestTheTriageReadsWithinTheCachesOwnAge: the earlier read is placed by the
// maximum age of the cache the gate reads, when the cache tells it (the real
// one does), and by the default for a cache that does not.
func TestTheTriageReadsWithinTheCachesOwnAge(t *testing.T) {
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	at := c.at()
	for _, x := range []struct {
		name  string
		cache func(*testing.T) KeyCache
		age   time.Duration
	}{
		{"a cache of the default age", func(t *testing.T) KeyCache { return warmCache(t, &fakeWorld{c: c}, "", at) },
			gwstate.DefaultMaxAge},
		{"a cache of another age", func(t *testing.T) KeyCache {
			cache, err := gwstate.NewCache(gwstate.CacheConfig{Reader: &fakeWorld{c: c}, MaxAge: 40 * time.Second})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(cache.Close)
			return cache
		}, 40 * time.Second},
		{"a cache that names no age", func(*testing.T) KeyCache { return &recordingCache{} }, gwstate.DefaultMaxAge},
	} {
		debts := &fakeDebts{read: marksOf(false, false)}
		sh, _, _ := debtShadow(t, debts)
		sh.cfg.Cache = x.cache(t)
		if got := sh.factsAge(); got != x.age {
			t.Errorf("%s: the facts' age %s, want %s", x.name, got, x.age)
		}
		r := fullResult{class: debtCandidate}
		sh.triageDebt("ws-1", at, at.Add(time.Second), &r)
		calls := debts.seen()
		if len(calls) != 2 || !calls[0].at.Equal(at.Add(time.Second-x.age)) || !calls[1].at.Equal(at.Add(time.Second)) {
			t.Errorf("%s: reads %+v, want %s before the request and a second after", x.name, calls, x.age-time.Second)
		}
	}
}

// credit402 is Python's 402 for credit, its type and a message in its form.
func credit402() func(http.ResponseWriter) {
	return answerWith(http.StatusPaymentRequired, errorBody(http.StatusPaymentRequired, creditRefusal,
		"Insufficient credits for this request's upfront reservation of $0.816068."))
}

// follow sends the case's request through the edge and waits for the shadow
// to compare it, without ending the period (send does).
func (h *fullEdge) follow(t *testing.T, c authorizeCase) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, c.Request.Path, bytes.NewReader(c.body()))
	req.Header = c.header()
	h.edge.ServeHTTP(httptest.NewRecorder(), req)
	h.edge.full.wg.Wait()
}

// rewarm waits for the cache to hold the case's key and its workspace's
// credit rows again, after an eviction.
func (h *fullEdge) rewarm(t *testing.T, c authorizeCase) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		r := h.cache.KeyCache.Lookup(c.Facts.Key.LookupHash, c.at())
		if r.Key == gwstate.Fresh && r.Credit == gwstate.Fresh {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the cache did not fill again: %+v", r)
		}
		time.Sleep(time.Millisecond)
	}
}

// TestAPython402ForCreditIsTriagedByTheDebtMark, over the edge: Python
// refuses a request Go would answer with its 402 insufficient_credits, and
// the shadow reads the workspace's debt mark at the request's instant less
// 14 s and at the end of Python's answer. A workspace unmarked at the
// earlier instant is counted under its own class, in the cohort too, and
// raises no alert and no CRITICAL line, whatever the later read says:
// unmarked (debt_unmarked), marked (debt_lag), or failed
// (debt_unmarked_earlier). Marked at both is the would-admit, as before,
// and a candidate the earlier read does not clear is debt_unconfirmed: both
// raise the alert, each one its own. Only the would-admit counts in the
// line's would_admit_python_refused. Whatever the class, Python's 402 still
// evicts the key and its workspace, and no line carries the workspace or a
// key.
func TestAPython402ForCreditIsTriagedByTheDebtMark(t *testing.T) {
	f := authorizeGoldens(t)
	// The golden whose workspace holds no credit at all and whose request
	// Go's decision answers all the same: the decision has no funding
	// verdict, and the owner's lease admits it.
	c := authorizeCaseNamed(t, "answer:credits_short_owner_admits")
	py := newFakePython(t)
	failed := errors.New("synthetic read failure")
	// laterFails answers the earlier read with its mark and fails the later.
	laterFails := func(earlier bool) func(int, time.Time) (store.DebtMark, error) {
		return func(call int, _ time.Time) (store.DebtMark, error) {
			if call == 1 {
				return store.DebtMark{}, failed
			}
			return store.DebtMark{Rows: 16, Marked: earlier}, nil
		}
	}
	for _, x := range []struct {
		name     string
		read     func(int, time.Time) (store.DebtMark, error)
		noReader bool
		// tokens are the triage's bucket's, which does not refill when
		// fewer than a triage's two.
		tokens float64
		class  string
		count  string
		reads  int
		paged  bool
	}{
		{"unmarked at both", marksOf(false, false), false, 2, debtUnmarked, "debt.unmarked", 2, false},
		{"marked at the later only", marksOf(false, true), false, 2, debtLag, "debt.lag", 2, false},
		{"unmarked at the earlier, the later read fails", laterFails(false), false, 2, debtUnmarkedEarlier,
			"debt.unmarked_earlier.unread", 2, false},
		{"marked at both", marksOf(true, true), false, 2, debtMiss, "debt.marked", 2, true},
		{"marked at the earlier only", marksOf(true, false), false, 2, debtUnconfirmed,
			"debt.unconfirmed.cleared", 2, true},
		{"marked at the earlier, the later read fails", laterFails(true), false, 2, debtUnconfirmed,
			"debt.unconfirmed.unread", 2, true},
		{"the earlier read fails", func(int, time.Time) (store.DebtMark, error) { return store.DebtMark{}, failed },
			false, 2, debtUnconfirmed, "debt.unconfirmed.unread", 1, true},
		{"the bucket is empty", marksOf(false, false), false, 0, debtUnconfirmed, "debt.unconfirmed.limiter", 0,
			true},
		{"the bucket holds one token", marksOf(false, false), false, 1, debtUnconfirmed,
			"debt.unconfirmed.limiter", 0, true},
		{"no reader", marksOf(false, false), true, 2, debtUnconfirmed, "debt.unconfirmed.none", 0, true},
		{"no credit rows", func(int, time.Time) (store.DebtMark, error) { return store.DebtMark{}, nil }, false,
			2, debtUnconfirmed, "debt.unconfirmed.rows", 1, true},
	} {
		debts := &fakeDebts{read: x.read}
		a := &alerts{}
		h := newFullEdge(t, f, c, py, func(cfg *DecideShadowConfig) {
			cfg.Alert = a.raise
			if !x.noReader {
				cfg.Debts = debts
			}
		})
		if x.tokens < debtReads {
			h.edge.full.cfg.ReadsPerSecond, h.edge.full.debtLimiter.tokens = 1e-9, x.tokens
		}
		h.answer(credit402())
		began := time.Now()
		h.send(t, c)
		ended := time.Now()
		got := h.edge.DecideShadowCounts()
		if !maps.Equal(got.Classes, map[string]int{x.class: 1}) || !maps.Equal(got.Cohort, map[string]int{x.class: 1}) ||
			!maps.Equal(got.Windows, map[string]int{x.count: 1}) {
			t.Errorf("%s: classes %v, cohort %v, windows %v; want %s and %s", x.name, got.Classes, got.Cohort,
				got.Windows, x.class, x.count)
		}
		raised := a.taken()
		want := []string{"snapshot=golden-snapshot policy=golden-policy: the edge would have answered an authorize " +
			"Python refused: " + x.class + " windows=" + x.count}
		if !x.paged {
			want = nil
		}
		if !slices.Equal(raised[AlertWouldAdmit], want) || len(raised) > 1 {
			t.Errorf("%s: alerts %q, want %q", x.name, raised, want)
		}
		if strings.Contains(h.logs.all(), "CRITICAL") {
			t.Errorf("%s: a CRITICAL line beside the alert path: %s", x.name, h.logs.all())
		}
		// The period's line: only the would-admit is one in its plain count.
		would := 0
		if x.class == debtMiss {
			would = 1
		}
		lines := parsedLines(t, h.logs)
		var digest []CountsLine
		for _, l := range lines {
			if l.Digest != "" {
				digest = append(digest, l)
			}
		}
		if len(digest) != 1 || digest[0].Counts["would_admit_python_refused"] != int64(would) ||
			!maps.Equal(digest[0].ByName["classes"], map[string]int64{x.class: 1}) ||
			!maps.Equal(digest[0].ByName["cohort"], map[string]int64{x.class: 1}) ||
			!maps.Equal(digest[0].ByName["windows"], map[string]int64{x.count: 1}) {
			t.Errorf("%s: the period's line %+v", x.name, digest)
		}
		calls := debts.seen()
		if len(calls) != x.reads {
			t.Fatalf("%s: %d reads, want %d", x.name, len(calls), x.reads)
		}
		for i, call := range calls {
			if call.workspace != c.Facts.Key.WorkspaceID || call.budget <= 0 || call.budget > WindowReadBudget {
				t.Errorf("%s: read %d of %q within %s", x.name, i, call.workspace, call.budget)
			}
			// The earlier read by the request's one clock reading (the
			// edge's clock, the case's instant), the later by the end of
			// Python's answer as this test's own clock brackets it.
			if i == 0 && !call.at.Equal(c.at().Add(-14*time.Second)) {
				t.Errorf("%s: the earlier read at %s, want 14 s before the request's %s", x.name, call.at, c.at())
			}
			if i == 1 && (call.at.Before(began) || call.at.After(ended)) {
				t.Errorf("%s: the later read at %s, outside the request's %s to %s", x.name, call.at, began, ended)
			}
		}
		h.cache.mu.Lock()
		evicted, workspaces := slices.Clone(h.cache.evicted), slices.Clone(h.cache.workspaces)
		h.cache.mu.Unlock()
		if !slices.Equal(evicted, []string{c.Facts.Key.LookupHash}) ||
			!slices.Equal(workspaces, []string{c.Facts.Key.WorkspaceID}) {
			t.Errorf("%s: evicted %v and %v", x.name, evicted, workspaces)
		}
		for _, secret := range []string{c.Facts.Key.WorkspaceID, c.Facts.Key.LookupHash, c.Facts.Key.Hash,
			"upfront reservation"} {
			if strings.Contains(h.logs.all(), secret) || strings.Contains(strings.Join(raised[AlertWouldAdmit], "\n"), secret) {
				t.Errorf("%s: a line carries %q", x.name, secret)
			}
		}
	}
}

// TestPythons402EvictsBeforeTheTriageReads: Python's 402 evicts the key and
// its workspace from the cache before the triage's first read begins, not
// after its last. The decide path decides on the same cache, and the two
// reads may take up to WindowReadBudget each: while they run, a lookup of
// the key finds it cold, so a request of it passes to Python and is not
// decided on the fact Python's refusal says is behind. So for every class
// the triage can give, the reads' failure included.
func TestPythons402EvictsBeforeTheTriageReads(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:credits_short_owner_admits")
	py := newFakePython(t)
	failed := errors.New("synthetic read failure")
	for name, read := range map[string]func(int, time.Time) (store.DebtMark, error){
		"unmarked at both":       marksOf(false, false),
		"marked at both":         marksOf(true, true),
		"the earlier read fails": func(int, time.Time) (store.DebtMark, error) { return store.DebtMark{}, failed },
	} {
		var h *fullEdge
		// What the cache had been told, and what it held for the key, as
		// each read began.
		type seen struct {
			evicted, workspaces []string
			key                 gwstate.Status
		}
		var mu sync.Mutex
		var atRead []seen
		debts := &fakeDebts{read: func(call int, at time.Time) (store.DebtMark, error) {
			h.cache.mu.Lock()
			now := seen{evicted: slices.Clone(h.cache.evicted), workspaces: slices.Clone(h.cache.workspaces)}
			h.cache.mu.Unlock()
			// As the decide path would look the key up (the lookup starts
			// the read that fills it again, which this test does not wait
			// for).
			if call == 0 {
				now.key = h.cache.KeyCache.Lookup(c.Facts.Key.LookupHash, c.at()).Key
			}
			mu.Lock()
			atRead = append(atRead, now)
			mu.Unlock()
			return read(call, at)
		}}
		h = newFullEdge(t, f, c, py, func(cfg *DecideShadowConfig) { cfg.Debts = debts })
		if before := h.cache.KeyCache.Lookup(c.Facts.Key.LookupHash, c.at()); before.Key != gwstate.Fresh ||
			before.Credit != gwstate.Fresh {
			t.Fatalf("%s: the cache did not hold the key before the request: %+v", name, before)
		}
		h.answer(credit402())
		h.send(t, c)
		mu.Lock()
		reads := slices.Clone(atRead)
		mu.Unlock()
		if len(reads) == 0 {
			t.Fatalf("%s: the triage made no read", name)
		}
		for i, r := range reads {
			if !slices.Equal(r.evicted, []string{c.Facts.Key.LookupHash}) ||
				!slices.Equal(r.workspaces, []string{c.Facts.Key.WorkspaceID}) {
				t.Errorf("%s: as read %d began the cache had been told to evict %v and %v, not the key and its "+
					"workspace", name, i, r.evicted, r.workspaces)
			}
		}
		if reads[0].key != gwstate.Cold {
			t.Errorf("%s: as the first read began the cache still held the key: %s", name, reads[0].key)
		}
	}
}

// TestOnlyThe402ForCreditIsTriaged: every other refusal of a request Go
// would answer stays the would-admit it was and pages, with no read of the
// debt mark: a 402 of another type, a 402 whose body was not read through
// (its type is not known), and a 403. Nor is the 402 for credit read for
// when Go would have passed the request, or when Python answered 200.
func TestOnlyThe402ForCreditIsTriaged(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	py := newFakePython(t)
	long := bytes.Repeat([]byte("x"), maxShadowResponse+1)
	for _, x := range []struct {
		name   string
		answer func(http.ResponseWriter)
		class  string
	}{
		{"a 402 of another type", answerWith(402, errorBody(402, "credits_not_on_this_plane", "No spendable credits")),
			ClassWouldAdmit + ".402.credits_not_on_this_plane"},
		{"a 402 of the tests' older type", answerWith(402, errorBody(402, "payment_required", "Insufficient credits")),
			ClassWouldAdmit + ".402.payment_required"},
		{"a 402 past the bound", answerWith(402, long), ClassWouldAdmit + ".402.unread"},
		{"a 403", answerWith(403, errorBody(403, "forbidden", "no")), ClassWouldAdmit + ".403.forbidden"},
		{"a 403 of the credit type", answerWith(403, errorBody(403, creditRefusal, "no")),
			ClassWouldAdmit + ".403." + creditRefusal},
	} {
		debts := &fakeDebts{read: marksOf(false, false)}
		a := &alerts{}
		h := newFullEdge(t, f, c, py, func(cfg *DecideShadowConfig) { cfg.Debts, cfg.Alert = debts, a.raise })
		h.answer(x.answer)
		h.send(t, c)
		if got := h.edge.DecideShadowCounts(); !maps.Equal(got.Classes, map[string]int{x.class: 1}) ||
			len(got.Windows) != 0 {
			t.Errorf("%s: classes %v, windows %v; want %s", x.name, got.Classes, got.Windows, x.class)
		}
		if raised := a.taken()[AlertWouldAdmit]; len(raised) != 1 || !strings.HasSuffix(raised[0], ": "+x.class) {
			t.Errorf("%s: alerts %q", x.name, raised)
		}
		if calls := debts.seen(); len(calls) != 0 {
			t.Errorf("%s: read the debt mark: %+v", x.name, calls)
		}
	}
	// Go would pass: the golden workspace marked in debt, which the gate
	// passes at workspace.in_debt. Python's 402 for credit is then
	// passed.workspace.in_debt, with no read and no alert, as it is with the
	// triage: the gate did its work.
	inDebt := authorizeCaseNamed(t, "pass:workspace.in_debt")
	for _, x := range []struct {
		name   string
		c      authorizeCase
		answer func(http.ResponseWriter)
		class  string
	}{
		{"Go would pass a workspace in debt", inDebt, credit402(), ClassGoPassed + "." + PassWorkspaceInDebt},
		{"Python answered", c, answerWith(200, []byte(c.Expect.Response)), ClassAgreeFull},
	} {
		debts := &fakeDebts{read: marksOf(true, true)}
		a := &alerts{}
		h := newFullEdge(t, f, x.c, py, func(cfg *DecideShadowConfig) { cfg.Debts, cfg.Alert = debts, a.raise })
		h.answer(x.answer)
		h.send(t, x.c)
		if got := h.edge.DecideShadowCounts(); !maps.Equal(got.Classes, map[string]int{x.class: 1}) {
			t.Errorf("%s: classes %v, want %s", x.name, got.Classes, x.class)
		}
		if raised, calls := a.taken(), debts.seen(); len(raised) != 0 || len(calls) != 0 {
			t.Errorf("%s: alerts %q, reads %+v", x.name, raised, calls)
		}
	}
}

// TestABenignDebtClassNeverKeepsAPagingOneFromItsAlert: the alert is raised
// one a paging comparison, as it is counted, so a period that holds
// comparisons that do not page and one that does still pages, in either
// order, and its one line counts both. However many that do not page come
// first, more than the period's bound on alerts, the paging one after them
// is raised; the bound counts only the comparisons that page.
func TestABenignDebtClassNeverKeepsAPagingOneFromItsAlert(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	py := newFakePython(t)
	for _, order := range [][2]bool{{false, true}, {true, false}} {
		// The marks of each request's two reads, by the request: unmarked at
		// both, or marked at both.
		var mu sync.Mutex
		request := 0
		debts := &fakeDebts{read: func(call int, _ time.Time) (store.DebtMark, error) {
			mu.Lock()
			defer mu.Unlock()
			return store.DebtMark{Rows: 16, Marked: order[request]}, nil
		}}
		a := &alerts{}
		h := newFullEdge(t, f, c, py, func(cfg *DecideShadowConfig) { cfg.Debts, cfg.Alert = debts, a.raise })
		h.answer(credit402())
		for i := range order {
			mu.Lock()
			request = i
			mu.Unlock()
			h.follow(t, c)
			// Python's 402 evicted the key and its workspace: the next
			// request is decided once the cache has read them again.
			h.rewarm(t, c)
		}
		// One period: no line yet, and the alert already raised.
		if lines := parsedLines(t, h.logs); len(lines) != 0 {
			t.Fatalf("%v: the period ended early: %+v", order, lines)
		}
		raised := a.taken()[AlertWouldAdmit]
		if len(raised) != 1 || !strings.Contains(raised[0], ": "+debtMiss+" windows=debt.marked") {
			t.Errorf("%v: alerts %q, want the would-admit's alone", order, raised)
		}
		h.edge.WaitDecideShadow()
		var digest []CountsLine
		for _, l := range parsedLines(t, h.logs) {
			if l.Digest != "" {
				digest = append(digest, l)
			}
		}
		if len(digest) != 1 || digest[0].Counts["would_admit_python_refused"] != 1 ||
			!maps.Equal(digest[0].ByName["classes"], map[string]int64{debtUnmarked: 1, debtMiss: 1}) ||
			!maps.Equal(digest[0].ByName["windows"], map[string]int64{"debt.unmarked": 1, "debt.marked": 1}) {
			t.Errorf("%v: the period's line %+v", order, digest)
		}
	}
	sh, a, logs := debtShadow(t, nil)
	benign := []string{debtUnmarked, debtLag, debtUnmarkedEarlier}
	for i := range 3 * maxCriticalLogs {
		sh.record("snapshot=s policy=p", fullResult{class: benign[i%len(benign)], decided: true})
	}
	if raised := a.taken(); len(raised) != 0 || sh.critical != 0 {
		t.Fatalf("comparisons that do not page raised %q and took %d of the bound", raised, sh.critical)
	}
	for _, class := range []string{debtUnconfirmed, debtMiss} {
		sh.record("snapshot=s policy=p", fullResult{class: class, decided: true, windows: []string{"debt.marked"}})
	}
	if raised := a.taken()[AlertWouldAdmit]; len(raised) != 2 || !strings.Contains(raised[0], debtUnconfirmed) ||
		!strings.Contains(raised[1], debtMiss) {
		t.Fatalf("the paging comparisons after them raised %q", raised)
	}
	// The bound is the paging comparisons' own, as before.
	for range 2 * maxCriticalLogs {
		sh.record("snapshot=s policy=p", fullResult{class: debtUnconfirmed, decided: true})
	}
	if raised := a.taken()[AlertWouldAdmit]; len(raised) != maxCriticalLogs-2 {
		t.Fatalf("%d alerts past the first two in a period bounded to %d", len(raised), maxCriticalLogs)
	}
	sh.flush()
	sh.record("snapshot=s policy=p", fullResult{class: debtMiss, decided: true})
	if raised := a.taken()[AlertWouldAdmit]; len(raised) != 1 {
		t.Fatalf("the next period's would-admit raised %q", raised)
	}
	if logs.has("fastpath edge decide shadow CRITICAL") {
		t.Fatalf("a CRITICAL line beside the alert path: %s", logs.all())
	}
}

// TestWithNoAlertPathOnlyThePagingClassesLogCritical: without the service's
// alert path the shadow logs a CRITICAL line for a paging class and none
// for the three debt classes that do not page.
func TestWithNoAlertPathOnlyThePagingClassesLogCritical(t *testing.T) {
	for class, want := range map[string]bool{debtMiss: true, debtUnconfirmed: true, debtUnmarked: false, debtLag: false,
		debtUnmarkedEarlier: false} {
		sh, _, logs := debtShadow(t, nil)
		sh.cfg.Alert = nil
		sh.record("snapshot=s policy=p", fullResult{class: class, decided: true, windows: []string{"debt.x"}})
		if got := logs.has("fastpath edge decide shadow CRITICAL: class=" + class + " windows=debt.x snapshot=s"); got != want {
			t.Errorf("%s: CRITICAL logged %v, want %v: %s", class, got, want, logs.all())
		}
	}
}
