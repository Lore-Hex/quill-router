package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"maps"
	"strings"
	"testing"
	"time"
)

// edgeEntries are a span's journal entries as the edge writes its count
// lines, the logger's date and time first: two nodes, two periods each,
// one entry handed over twice, and lines that are not counts.
var edgeEntries = []logEntry{
	{"1", "tr-fastpath-5", `2026/10/24 12:00:00 fastpath edge decide shadow: snapshot=s1 policy=p1 agree_full=990 ` +
		`differ=4 would_admit_python_refused=1 read_back=3 classes={"agree_full":990,"differ":4,"stale.key":5,` +
		`"would_admit_python_refused.403.forbidden":1} fields={"differ.estimate_float":1,"differ.timing":3,` +
		`"row.idempotency_fingerprint":2} rows={"agree":40,"differ":2} decide={"decide.off":995}`},
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
		`fields={} rows={} decide={"decide.off":5}`},
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
// entry once, with the I2 figures: agree_full over the requests Go would
// answer, the differences by field, the estimate's and the fingerprint's,
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
	if g.CohortEligible != 1000 || g.AgreeFull != 995 || g.AgreeFullRatio == nil || *g.AgreeFullRatio != 0.995 ||
		g.Differ != 4 || g.DifferEstimate != 1 || g.RowIdempotencyFingerprint != 2 || g.WouldAdmitPythonRefused != 1 ||
		!maps.Equal(g.WouldAdmitByClass, map[string]int64{"403.forbidden": 1}) || g.Stale != 6 ||
		!maps.Equal(g.StaleByCache, map[string]int64{"key": 6}) || g.Followed != 1008 || g.StaleRate == nil ||
		*g.StaleRate != 6.0/1008 || g.Unavailable != 3 ||
		!maps.Equal(g.UnavailableByCode, map[string]int64{"failed_precondition": 2, "deadline_exceeded": 1}) ||
		!maps.Equal(g.DifferByField, map[string]int64{"differ.estimate_float": 1, "differ.timing": 3,
			"row.idempotency_fingerprint": 2}) {
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
