package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/api/option"
)

// edgeEntries are a span's journal entries as the edge writes its count
// lines, the logger's date and time first: two nodes, two periods each,
// one entry handed over twice, and lines that are not counts.
var edgeEntries = []logEntry{
	{"1", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide shadow: snapshot=s1 policy=p1 agree_full=990 ` +
		`differ=6 would_admit_python_refused=1 read_back=3 classes={"agree_full":990,"differ":6,"stale.key":5,` +
		`"would_admit_python_refused.403.forbidden":1} fields={"differ.affinity_warm":2,"differ.estimate_float":1,` +
		`"differ.timing":3,"other":3,"row.idempotency_fingerprint":2} rows={"agree":40,"differ":2} ` +
		`decide={"decide.off":997} cohort={"agree_full":990,"differ":6,"would_admit_python_refused.403.forbidden":1}`},
	{"2", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide shadow: dropped=0 evictions=1 cache_unrefreshed=0 ` +
		`cache_dropped=0 cache_read_failures=0`},
	{"3", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide: answered=7 read_back=1 unavailable=2 refunded=1 ` +
		`refund_failed=0 unavailable_by_code={"failed_precondition":2} passed={"owner.busy":3} slow={}`},
	{"3", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide: answered=7 read_back=1 unavailable=2 refunded=1 ` +
		`refund_failed=0 unavailable_by_code={"failed_precondition":2} passed={"owner.busy":3} slow={}`},
	{"4", "tr-fastpath-5", `2026/10/24 12:00:30 fastpath edge decide shadow differs: class=differ fields=differ.timing ` +
		`rows=none snapshot=s1 policy=p1`},
	{"5", "tr-fastpath-5", `2026/10/24 12:00:30 fastpath edge decide shadow CRITICAL: class=` +
		`would_admit_python_refused.403.forbidden snapshot=s1 policy=p1`},
	{"6", "tr-fastpath-6", `2026/10/24 12:01:00 fastpath edge decide shadow: snapshot=s2 policy=p1 agree_full=5 ` +
		`differ=0 would_admit_python_refused=0 read_back=0 classes={"agree_full":5,"passed.owner.off":2,"stale.key":1} ` +
		`fields={} rows={} decide={"decide.off":5} cohort={"agree_full":5}`},
	{"7", "tr-fastpath-6", `2026/10/24 12:01:00 fastpath edge decide shadow: dropped=2 evictions=0 cache_unrefreshed=0 ` +
		`cache_dropped=0 cache_read_failures=0`},
	{"8", "tr-fastpath-6", `2026/10/24 12:01:00 fastpath edge decide: answered=0 read_back=0 unavailable=1 refunded=0 ` +
		`refund_failed=0 unavailable_by_code={"deadline_exceeded":1} passed={} slow={"at_edge":1}`},
	{"9", "tr-fastpath-6", `2026/10/24 12:01:00 fastpath edge snapshot: poll status=304 outcome=unchanged`},
}

func edgeDeps(entries []logEntry, failWith error) (deps, *[]string) {
	var asked []string
	d := deps{clock: func() time.Time { return time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC) },
		entries: func(_ context.Context, project string, since, until time.Time, each func(logEntry) error) error {
			asked = append(asked, edgeFilter(project, since, until))
			for _, e := range entries {
				if err := each(e); err != nil {
					return err
				}
			}
			return failWith
		}}
	return d, &asked
}

// TestTheEdgeReportSumsTheSpansLines: the span's count lines summed, each
// entry once, with the I2 figures: agree_full over the comparable requests
// (agree_full and differ, Python's warm re-ranks among the differs and
// counted by name), the requests Go would answer by class, the
// differences by field, the estimate's and the fingerprint's, the fields
// past the line's bound,
// would_admit_python_refused by class, the stale passes over every request
// followed, and the decide path's unavailable answers by code; and each
// node's period lines.
func TestTheEdgeReportSumsTheSpansLines(t *testing.T) {
	d, asked := edgeDeps(edgeEntries, nil)
	var out bytes.Buffer
	code, err := run(context.Background(), []string{"edge", "-project", "p", "-since", "2026-10-24T11:00:00Z"}, &out, d)
	if code != 0 || err != nil {
		t.Fatalf("%d %v", code, err)
	}
	if len(*asked) != 1 || (*asked)[0] != `logName="projects/p/logs/fastpath_journal" AND `+
		`timestamp>="2026-10-24T11:00:00Z" AND timestamp<"2026-10-24T13:00:00Z" AND `+
		`jsonPayload.MESSAGE:"fastpath edge decide"` {
		t.Fatalf("the filter: %q", *asked)
	}
	var r edgeReport
	if err := json.Unmarshal(out.Bytes(), &r); err != nil {
		t.Fatalf("%v: %s", err, out.Bytes())
	}
	g := r.I2
	if g.Comparable != 1001 || g.AgreeFull != 995 || g.AgreeFullRatio == nil || *g.AgreeFullRatio != 995.0/1001 ||
		g.CohortEligible != 1002 || !maps.Equal(g.CohortByClass, map[string]int64{"agree_full": 995, "differ": 6,
		"would_admit_python_refused.403.forbidden": 1}) || g.FieldsOther != 3 || g.Differ != 6 ||
		g.DifferAffinityWarm != 2 || g.DifferEstimate != 1 || g.RowIdempotencyFingerprint != 2 ||
		g.WouldAdmitPythonRefused != 1 || !maps.Equal(g.WouldAdmitByClass, map[string]int64{"403.forbidden": 1}) ||
		g.Stale != 6 || !maps.Equal(g.StaleByCache, map[string]int64{"key": 6}) || g.Followed != 1010 ||
		g.StaleRate == nil || *g.StaleRate != 6.0/1010 || g.Unavailable != 3 ||
		!maps.Equal(g.UnavailableByCode, map[string]int64{"failed_precondition": 2, "deadline_exceeded": 1}) ||
		!maps.Equal(g.DifferByField, map[string]int64{"differ.affinity_warm": 2, "differ.estimate_float": 1,
			"differ.timing": 3, "other": 3, "row.idempotency_fingerprint": 2}) {
		t.Fatalf("the I2 figures: %s", out.Bytes())
	}
	if r.Decide.Counts["answered"] != 7 || r.Decide.ByName["passed"]["owner.busy"] != 3 ||
		r.Decide.ByName["slow"]["at_edge"] != 1 || r.DecideShadow.Counts["dropped"] != 2 ||
		r.DecideShadow.Counts["evictions"] != 1 || r.DecideShadow.ByName["rows"]["agree"] != 40 {
		t.Fatalf("the sums: %s", out.Bytes())
	}
	if len(r.Periods) != 2 || !maps.Equal(r.Periods["tr-fastpath-5"], map[string]int64{"decide": 1, "decide shadow": 1}) ||
		!maps.Equal(r.Periods["tr-fastpath-6"], map[string]int64{"decide": 1, "decide shadow": 1}) {
		t.Fatalf("the periods: %v", r.Periods)
	}
	if !r.Since.Equal(time.Date(2026, 10, 24, 11, 0, 0, 0, time.UTC)) ||
		!r.Until.Equal(time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC)) {
		t.Fatalf("the span: %v to %v", r.Since, r.Until)
	}
}

// TestTheEdgeReportPrintsTheAsyncFigures (docs/design/
// fast-admission-e2-g15-async.md §5, §6.2): the settlement-header pass
// share over followed, and each I3 figure as one number summed over the
// span's lines: async_seen and its share of followed, async_compared in all
// and by population with each population's agreement and the two daily
// rates over the span, each async field over both populations, and
// async_python_admitted.
func TestTheEdgeReportPrintsTheAsyncFigures(t *testing.T) {
	entries := []logEntry{
		{"1", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide shadow: snapshot=s1 policy=p1 agree_full=300 ` +
			`differ=4 would_admit_python_refused=0 read_back=0 classes={"agree_full":300,"differ":4,` +
			`"python_answered_go_passed.header.settlement_async":10,"passed.header.settlement_async":2,` +
			`"python_answered_go_passed.async.unpublished":6} fields={} rows={} decide={"async.off":300,"decide.off":4} ` +
			`cohort={} async_seen={"plain":40,"affinity":250,"unparsed":2} async_compared={"plain.agree":30,` +
			`"plain.differ":1,"affinity.agree":200,"affinity.differ":2} async_fields={"plain.differ.billing_snapshot":1,` +
			`"affinity.differ.async_shape":2} async_shape={"plain.snapshot":31,"affinity.snapshot":202} ` +
			`async_python_admitted={"affinity":1}`},
		{"2", "tr-fastpath-6", `2026/10/24 12:01:00 fastpath edge decide shadow: snapshot=s1 policy=p1 agree_full=9 ` +
			`differ=0 would_admit_python_refused=0 read_back=0 classes={"agree_full":9,` +
			`"python_answered_go_passed.async.off":1} fields={} rows={} decide={"async.off":9} cohort={} ` +
			`async_seen={"plain":10} async_compared={"plain.agree":9} async_fields={} async_shape={"plain.snapshot":9} ` +
			`async_python_admitted={}`},
	}
	d, _ := edgeDeps(entries, nil)
	var out bytes.Buffer
	code, err := run(context.Background(), []string{"edge", "-project", "p", "-since", "2026-10-22T13:00:00Z"}, &out, d)
	if code != 0 || err != nil {
		t.Fatalf("%d %v", code, err)
	}
	var r edgeReport
	if err := json.Unmarshal(out.Bytes(), &r); err != nil {
		t.Fatalf("%v: %s", err, out.Bytes())
	}
	is := func(p *float64, want float64) bool { return p != nil && *p == want }
	if g := r.I2; g.Followed != 332 || g.SettlementHeaderPasses != 12 || !is(g.SettlementHeaderPassShare, 12.0/332) {
		t.Errorf("the settlement header share: %s", out.Bytes())
	}
	g := r.I3
	if g.AsyncSeen != 302 || !is(g.AsyncSeenShare, 302.0/332) || g.AsyncCompared != 242 ||
		!is(g.AsyncComparedPerDay, 121) || g.AsyncComparedPlain != 40 || !is(g.AsyncComparedPlainPerDay, 20) ||
		!is(g.AsyncComparedPlainAgreeRatio, 39.0/40) || g.AsyncComparedAffinity != 202 ||
		!is(g.AsyncComparedAffinityAgreeRatio, 200.0/202) || g.AsyncBillingSnapshot != 1 || g.AsyncShape != 2 ||
		g.AsyncBillingSnapshotHash != 0 || g.AsyncEligible != 0 || g.AsyncPythonAdmitted != 1 {
		t.Errorf("the I3 figures: %s", out.Bytes())
	}
	// A release's lines before G15c carry no async maps: every I3 count is
	// zero, and no ratio of nothing is printed.
	d, _ = edgeDeps(edgeEntries, nil)
	out.Reset()
	if code, err := run(context.Background(), []string{"edge", "-project", "p", "-since", "2026-10-24T11:00:00Z"}, &out,
		d); code != 0 || err != nil {
		t.Fatalf("%d %v", code, err)
	}
	r = edgeReport{}
	if err := json.Unmarshal(out.Bytes(), &r); err != nil {
		t.Fatal(err)
	}
	if g := r.I3; g.AsyncSeen != 0 || g.AsyncCompared != 0 || g.AsyncComparedPlainAgreeRatio != nil ||
		g.AsyncComparedAffinityAgreeRatio != nil || !is(g.AsyncSeenShare, 0) {
		t.Errorf("before G15c: %+v", g)
	}
}

// TestTheEdgeReportFailsRatherThanUndercount: a count line that does not
// read, or a read that fails, is exit 1 with no report: a report short of
// a line's counts would understate a gate.
func TestTheEdgeReportFailsRatherThanUndercount(t *testing.T) {
	bad := append([]logEntry{}, edgeEntries...)
	bad = append(bad, logEntry{"10", "tr-fastpath-6", "2026/10/24 12:02:00 fastpath edge decide: answered=x"})
	for name, d := range map[string]deps{
		"a line":   func() deps { d, _ := edgeDeps(bad, nil); return d }(),
		"the read": func() deps { d, _ := edgeDeps(edgeEntries, errors.New("quota")); return d }(),
	} {
		var out bytes.Buffer
		code, err := run(context.Background(), []string{"edge", "-project", "p", "-since", "2026-10-24T11:00:00Z"}, &out, d)
		if code != 1 || err == nil || out.Len() != 0 {
			t.Errorf("%s: %d %v %q", name, code, err, out.String())
		}
	}
}

func TestTheEdgeReportsUsage(t *testing.T) {
	d, asked := edgeDeps(edgeEntries, nil)
	for _, args := range [][]string{
		{"-since", "2026-10-24T11:00:00Z"},
		{"-project", "p"},
		{"-project", "p", "-since", "yesterday"},
		{"-project", "p", "-since", "2026-10-24T11:00:00Z", "-until", "noon"},
		{"-project", "p", "-since", "2026-10-24T11:00:00Z", "-until", "2026-10-24T11:00:00Z"},
		{"-project", "p", "-since", "2026-10-24T11:00:00Z", "extra"},
	} {
		code, err := run(context.Background(), append([]string{"edge"}, args...), &bytes.Buffer{}, d)
		if code != 2 || err == nil {
			t.Errorf("%v: %d %v", args, code, err)
		}
	}
	if len(*asked) != 0 {
		t.Fatalf("read with bad usage: %v", *asked)
	}
	if code, err := run(context.Background(), []string{"edge", "-h"}, &bytes.Buffer{}, d); code != 0 || err != nil ||
		strings.Contains(strings.Join(*asked, ""), "logName") {
		t.Fatalf("-h: %d %v", code, err)
	}
}

// fakeLogging is entries.list: each request's body kept with when it came,
// and answered by the next of its answers, a status and a body.
type fakeLogging struct {
	mu      sync.Mutex
	asked   []map[string]any
	at      []time.Time
	answers []fakeAnswer
}

type fakeAnswer struct {
	status int
	body   string
}

func (f *fakeLogging) serve(t *testing.T) logReader {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var asked map[string]any
		if r.Method != http.MethodPost || r.URL.Path != "/v2/entries:list" || json.Unmarshal(body, &asked) != nil {
			t.Errorf("%s %s %s", r.Method, r.URL.Path, body)
		}
		f.mu.Lock()
		f.asked, f.at = append(f.asked, asked), append(f.at, time.Now())
		a := fakeAnswer{http.StatusInternalServerError, `{"error":{"code":500,"message":"no more answers"}}`}
		if len(f.answers) > 0 {
			a, f.answers = f.answers[0], f.answers[1:]
		}
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(a.status)
		_, _ = io.WriteString(w, a.body)
	}))
	t.Cleanup(srv.Close)
	return logReader{opts: []option.ClientOption{option.WithEndpoint(srv.URL + "/"), option.WithoutAuthentication(),
		option.WithHTTPClient(srv.Client())}, pace: 20 * time.Millisecond}
}

func entryJSON(id, node, message string) string {
	b, _ := json.Marshal(map[string]any{"insertId": id, "labels": map[string]string{
		"compute.googleapis.com/resource_name": node, "unit": "fastpath.service"},
		"jsonPayload": map[string]any{"MESSAGE": message, "PRIORITY": "6"}})
	return string(b)
}

// TestTheLogReaderPagesPacesAndBacksOff: the reader asks for the span's
// filter, oldest first, a page of 1,000 at a time, follows each page's
// token, decodes each entry's node from its label and its message from its
// payload, waits its pace between reads and twice as long again after a 429
// or a 5xx; a 4xx otherwise, or a 5xx past its attempts, is the read's error.
func TestTheLogReaderPagesPacesAndBacksOff(t *testing.T) {
	since, until := time.Date(2026, 10, 24, 11, 0, 0, 0, time.UTC), time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC)
	f := &fakeLogging{answers: []fakeAnswer{
		{http.StatusTooManyRequests, `{"error":{"code":429,"message":"quota"}}`},
		{http.StatusOK, `{"entries":[` + entryJSON("a", "tr-fastpath-5", "line one") + `,` +
			entryJSON("b", "tr-fastpath-6", "line two") + `],"nextPageToken":"page-2"}`},
		{http.StatusServiceUnavailable, `{"error":{"code":503,"message":"later"}}`},
		{http.StatusOK, `{"entries":[` + entryJSON("c", "tr-fastpath-5", "line three") + `]}`},
	}}
	lr := f.serve(t)
	var got []logEntry
	err := lr.entries(context.Background(), "p", since, until, func(e logEntry) error { got = append(got, e); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got, []logEntry{{"a", "tr-fastpath-5", "line one"}, {"b", "tr-fastpath-6", "line two"},
		{"c", "tr-fastpath-5", "line three"}}) {
		t.Fatalf("the entries: %+v", got)
	}
	if len(f.asked) != 4 {
		t.Fatalf("%d reads", len(f.asked))
	}
	for i, a := range f.asked {
		if a["filter"] != edgeFilter("p", since, until) || a["orderBy"] != "timestamp asc" || a["pageSize"] != 1000.0 ||
			!slices.Equal(a["resourceNames"].([]any), []any{"projects/p"}) {
			t.Errorf("read %d: %v", i, a)
		}
		want := ""
		if i >= 2 {
			want = "page-2"
		}
		if token, _ := a["pageToken"].(string); token != want {
			t.Errorf("read %d: the token %q, want %q", i, token, want)
		}
	}
	// After the 429, twice the pace; then the pace; after the 503, twice
	// the last wait.
	for i, least := range []time.Duration{2 * lr.pace, lr.pace, 2 * lr.pace} {
		if gap := f.at[i+1].Sub(f.at[i]); gap < least {
			t.Errorf("read %d came %v after the last, under %v", i+1, gap, least)
		}
	}
	for name, answers := range map[string][]fakeAnswer{
		"a 400":      {{http.StatusBadRequest, `{"error":{"code":400,"message":"bad filter"}}`}},
		"5xx always": nil,
	} {
		f := &fakeLogging{answers: answers}
		err := f.serve(t).entries(context.Background(), "p", since, until, func(logEntry) error { return nil })
		want := 1
		if answers == nil {
			want = readAttempts
		}
		if err == nil || len(f.asked) != want {
			t.Errorf("%s: %v after %d reads, want an error after %d", name, err, len(f.asked), want)
		}
	}
}

// TestTheEdgeReportPrintsTheWindowRaceFigures (docs/design/
// fast-admission-e2-key-windows.md section 8.3): the shadow's window reads,
// its confirmed read races and their share of the reads (the gate's 0.05%),
// and the unconfirmed candidates, summed over the span's lines; the races
// are not would-admits, the unconfirmed not races.
func TestTheEdgeReportPrintsTheWindowRaceFigures(t *testing.T) {
	entries := []logEntry{
		{"1", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide shadow: snapshot=s1 policy=p1 agree_full=1990 ` +
			`differ=0 would_admit_python_refused=1 read_back=0 classes={"agree_full":1990,` +
			`"window_race.429.key_window_limit_exceeded":2,"window_race_unconfirmed.429.key_window_limit_exceeded":1,` +
			`"would_admit_python_refused.429.key_window_limit_exceeded":1} fields={} rows={} decide={"on":1994} ` +
			`cohort={} windows={"read.made":1500,"verdict.admit":1500,"race.confirmed":2}`},
		{"2", "tr-fastpath-6", `2026/10/24 12:01:00 fastpath edge decide shadow: snapshot=s1 policy=p1 agree_full=500 ` +
			`differ=0 would_admit_python_refused=0 read_back=0 classes={"agree_full":500,` +
			`"window_race.429.key_window_limit_exceeded":1} fields={} rows={} decide={"on":501} cohort={} ` +
			`windows={"read.made":500,"race.confirmed":1}`},
	}
	d, _ := edgeDeps(entries, nil)
	var out bytes.Buffer
	if code, err := run(context.Background(), []string{"edge", "-project", "p", "-since", "2026-10-22T13:00:00Z"},
		&out, d); code != 0 || err != nil {
		t.Fatalf("%d %v", code, err)
	}
	var r edgeReport
	if err := json.Unmarshal(out.Bytes(), &r); err != nil {
		t.Fatalf("%v: %s", err, out.Bytes())
	}
	g := r.I2
	if g.WindowReads != 2000 || g.WindowRace != 3 || g.WindowRaceUnconfirmed != 1 || g.WindowRaceRatio == nil ||
		*g.WindowRaceRatio != 3.0/2000 || g.WouldAdmitPythonRefused != 1 ||
		len(g.WouldAdmitByClass) != 1 || g.WouldAdmitByClass["429.key_window_limit_exceeded"] != 1 {
		t.Errorf("the window race figures: %s", out.Bytes())
	}
}
