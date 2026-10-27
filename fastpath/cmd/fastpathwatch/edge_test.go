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
		`differ=4 would_admit_python_refused=1 read_back=3 classes={"agree_full":990,"differ":4,"stale.key":5,` +
		`"would_admit_python_refused.403.forbidden":1} fields={"differ.estimate_float":1,"differ.timing":3,` +
		`"other":3,"row.idempotency_fingerprint":2} rows={"agree":40,"differ":2} decide={"decide.off":995} ` +
		`cohort={"agree_full":990,"differ":4,"would_admit_python_refused.403.forbidden":1} ` +
		`affinity={"affinity_cold":30,"affinity_warm":10,"differ":2}`},
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
// (agree_full and differ), the requests Go would answer by class, the
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
	if g.Comparable != 999 || g.AgreeFull != 995 || g.AgreeFullRatio == nil || *g.AgreeFullRatio != 995.0/999 ||
		g.CohortEligible != 1000 || !maps.Equal(g.CohortByClass, map[string]int64{"agree_full": 995, "differ": 4,
		"would_admit_python_refused.403.forbidden": 1}) || g.FieldsOther != 3 || g.Differ != 4 || g.DifferEstimate != 1 || g.RowIdempotencyFingerprint != 2 || g.WouldAdmitPythonRefused != 1 ||
		!maps.Equal(g.WouldAdmitByClass, map[string]int64{"403.forbidden": 1}) || g.Stale != 6 ||
		!maps.Equal(g.StaleByCache, map[string]int64{"key": 6}) || g.Followed != 1008 || g.StaleRate == nil ||
		*g.StaleRate != 6.0/1008 || g.Unavailable != 3 ||
		!maps.Equal(g.UnavailableByCode, map[string]int64{"failed_precondition": 2, "deadline_exceeded": 1}) ||
		!maps.Equal(g.DifferByField, map[string]int64{"differ.estimate_float": 1, "differ.timing": 3, "other": 3,
			"row.idempotency_fingerprint": 2}) ||
		!maps.Equal(g.AffinityByClass, map[string]int64{"affinity_cold": 30, "affinity_warm": 10, "differ": 2}) ||
		g.AffinityWarmShare == nil || *g.AffinityWarmShare != 0.25 {
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
