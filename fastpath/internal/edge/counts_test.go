package edge

import (
	"context"
	"maps"
	"net/http"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// parsedLines are the count lines of logs, parsed, each a line or the test
// fails.
func parsedLines(t *testing.T, logs *logLines) []CountsLine {
	t.Helper()
	logs.mu.Lock()
	defer logs.mu.Unlock()
	var out []CountsLine
	for _, l := range logs.lines {
		// As the service's logger writes it, its date and time first.
		line, ok, err := ParseCountsLine("2026/10/24 12:00:00 " + l)
		if err != nil {
			t.Fatalf("%q: %v", l, err)
		}
		if ok {
			out = append(out, line)
		}
	}
	return out
}

func asInt64(m map[string]int) map[string]int64 {
	out := map[string]int64{}
	for k, v := range m {
		out[k] = int64(v)
	}
	return out
}

// TestTheDecideLineReadsBackAsItsCounts: one period's decide line, read by
// ParseCountsLine, is the period's counts, each named as DecideCounts names
// it.
func TestTheDecideLineReadsBackAsItsCounts(t *testing.T) {
	h, c := baseDecide(t)
	h.send(t, c)
	h.rows.insert, h.rows.code = func(context.Context) store.InsertOutcome { return store.Unknown }, codes.FailedPrecondition
	h.send(t, c)
	h.rows.insert = func(context.Context) store.InsertOutcome { return store.Failed }
	h.send(t, c)
	h.edge.decider.slowAtEdge = 0
	h.send(t, c)
	h.edge.decider.flush()
	lines := parsedLines(t, h.logs)
	if len(lines) != 1 || lines[0].Of != CountsDecide || lines[0].Digest != "" {
		t.Fatalf("the lines: %+v", lines)
	}
	got, want := lines[0], h.edge.DecideCounts()
	if !maps.Equal(got.Counts, map[string]int64{"answered": want.Answered, "read_back": want.ReadBack,
		"unavailable": want.Unavailable, "refunded": want.Refunded, "refund_failed": want.RefundFailed}) ||
		!maps.Equal(got.ByName["unavailable_by_code"], want.UnavailableByCode) ||
		!maps.Equal(got.ByName["passed"], want.Passed) || !maps.Equal(got.ByName["slow"], want.Slow) ||
		len(got.ByName) != 3 {
		t.Fatalf("read back %+v, counted %+v", got, want)
	}
	if want.Answered != 1 || want.Unavailable != 1 || want.Refunded != 2 || want.Slow["at_edge"] != 1 {
		t.Fatalf("not the period's requests: %+v", want)
	}
}

// TestTheDecideShadowLinesReadBackAsTheirCounts: the shadow's lines of four
// periods (each request's send ends one, and the last is empty), read by
// ParseCountsLine and summed, are the counts since the start: the classes,
// fields, rows and decide gates of the snapshot and policy's lines, and the
// requests not followed and the evictions of the others.
func TestTheDecideShadowLinesReadBackAsTheirCounts(t *testing.T) {
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	h := newFullEdge(t, f, c, nil, nil)
	h.rows.by[c.IDs.AuthorizationID] = storedRows(t, c)
	h.answer(answerWith(http.StatusOK, []byte(c.Expect.Response)))
	h.send(t, c)
	h.answer(answerWith(http.StatusOK, []byte(strings.Replace(c.Expect.Response, `"stage_d"`, `"stage_x"`, 1))))
	h.send(t, c)
	h.answer(answerWith(http.StatusForbidden, []byte(`{"error":{"code":403,"message":"no","type":"forbidden"}}`)))
	h.send(t, c)
	h.edge.WaitDecideShadow()
	lines := parsedLines(t, h.logs)
	sums := map[string]map[string]int64{}
	counts := map[string]int64{}
	digests := 0
	for _, l := range lines {
		if l.Of != CountsDecideShadow {
			t.Fatalf("a line of %q", l.Of)
		}
		if l.Digest != "" {
			digests++
			if l.Digest != "snapshot=golden-snapshot policy=golden-policy" {
				t.Fatalf("the digest %q", l.Digest)
			}
		}
		for k, v := range l.Counts {
			counts[k] += v
		}
		for name, m := range l.ByName {
			if sums[name] == nil {
				sums[name] = map[string]int64{}
			}
			for k, v := range m {
				sums[name][k] += v
			}
		}
	}
	want := h.edge.DecideShadowCounts()
	if digests != 3 || len(lines) != 7 {
		t.Fatalf("the lines: %+v", lines)
	}
	would := int64(0)
	for class, n := range want.Classes {
		if strings.HasPrefix(class, ClassWouldAdmit+".") {
			would += int64(n)
		}
	}
	if !maps.Equal(sums["classes"], asInt64(want.Classes)) || !maps.Equal(sums["fields"], asInt64(want.Fields)) ||
		!maps.Equal(sums["rows"], asInt64(want.Rows)) || !maps.Equal(sums["decide"], asInt64(want.Decide)) ||
		counts["read_back"] != int64(want.ReadBack) || counts["dropped"] != want.Dropped ||
		counts["evictions"] != want.Evictions || counts["agree_full"] != int64(want.Classes[ClassAgreeFull]) ||
		counts["differ"] != int64(want.Classes[ClassDifferAny]) || counts["would_admit_python_refused"] != would {
		t.Fatalf("read back %v %v, counted %+v", counts, sums, want)
	}
	if want.Classes[ClassAgreeFull] != 1 || want.Classes[ClassDifferAny] != 1 || would != 1 || want.Evictions == 0 {
		t.Fatalf("not the period's requests: %+v", want)
	}
}

// TestParseCountsLine: a message that is no count line is not one, the
// edge's other lines among them; a count line that does not read is an
// error, never skipped.
func TestParseCountsLine(t *testing.T) {
	for _, m := range []string{
		"2026/10/24 12:00:00 fastpath edge snapshot: poll status=304 outcome=unchanged",
		"2026/10/24 12:00:00 fastpath edge decide shadow differs: class=differ fields=differ.timing rows=none snapshot=s policy=p",
		"2026/10/24 12:00:00 fastpath edge decide shadow CRITICAL: class=would_admit_python_refused.403.forbidden s",
		"2026/10/24 12:00:00 fastpath edge decide ALERT edge_decide_unavailable: the edge answered 1",
		"2026/10/24 12:00:00 fastpath edge decide copy: read failed; the copy held ages out after 30s",
	} {
		if _, ok, err := ParseCountsLine(m); ok || err != nil {
			t.Errorf("%q: a count line (%v)", m, err)
		}
	}
	for _, m := range []string{
		"fastpath edge decide: answered=x",
		"fastpath edge decide: answered",
		`fastpath edge decide: passed={"a b":1}`,
		`fastpath edge decide shadow: snapshot=s policy=p classes={"agree_full":1`,
	} {
		if _, ok, err := ParseCountsLine(m); !ok || err == nil {
			t.Errorf("%q: read (%v, %v)", m, ok, err)
		}
	}
	line, ok, err := ParseCountsLine(`2026/10/24 12:00:00 fastpath edge decide shadow: snapshot=s policy=p agree_full=2 ` +
		`fields={"row.idempotency_fingerprint":1}`)
	if !ok || err != nil || line.Digest != "snapshot=s policy=p" || line.Counts["agree_full"] != 2 ||
		line.ByName["fields"]["row.idempotency_fingerprint"] != 1 {
		t.Fatalf("%+v %v %v", line, ok, err)
	}
}
