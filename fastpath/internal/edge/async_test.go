package edge

import (
	"encoding/json"
	"maps"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// G15c (docs/design/fast-admission-e2-g15-async.md §4.4, §6.1, §6.2): P6's
// async goldens (fastpath/testdata/authorize_async_cases_v1.json, Python's
// authorize with X-TR-Settlement-Mode, admission off, a test runtime)
// through the decide pipeline, the decide path and the shadow. Each case's
// world is built as the P1 goldens' are (decide_test.go), over the file's own
// routing snapshot.

var loadAsyncAuthorize = sync.OnceValues(func() (authorizeFile, error) {
	var f authorizeFile
	raw, err := readTestdata("authorize_async_cases_v1.json")
	if err != nil {
		return f, err
	}
	err = json.Unmarshal(raw, &f)
	return f, err
})

func asyncAuthorizeGoldens(t *testing.T) authorizeFile {
	t.Helper()
	f, err := loadAsyncAuthorize()
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Cases) < 45 || f.SnapshotBody == "" {
		t.Fatalf("%d cases: not the goldens this test was written against", len(f.Cases))
	}
	return f
}

func asyncCaseNamed(t *testing.T, name string) authorizeCase {
	t.Helper()
	for _, c := range asyncAuthorizeGoldens(t).Cases {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no case %q", name)
	return authorizeCase{}
}

// withEdgeDecide is the case with its policy's async_settle.edge_decide
// set to on.
func withEdgeDecide(t *testing.T, f authorizeFile, c authorizeCase, on bool) authorizeCase {
	t.Helper()
	return withAsyncSettle(t, f, c, func(a *routingsnapshot.AsyncSettle) { a.EdgeDecide = on })
}

// withAsyncSettle is the case with its policy's async_settle changed.
func withAsyncSettle(t *testing.T, f authorizeFile, c authorizeCase, change func(*routingsnapshot.AsyncSettle)) authorizeCase {
	t.Helper()
	a := policyOf(t, f, c).AsyncSettle
	if a == nil {
		t.Fatalf("%s: no async_settle", c.Name)
	}
	changed := *a
	change(&changed)
	raw, err := json.Marshal(changed)
	if err != nil {
		t.Fatal(err)
	}
	c.PolicyPatch = maps.Clone(c.PolicyPatch)
	if c.PolicyPatch == nil {
		c.PolicyPatch = map[string]json.RawMessage{}
	}
	c.PolicyPatch["async_settle"] = raw
	return c
}

// asyncPopOf is the population the shadow counts a P6 case under: none for
// a header list other than exactly ["async-v1"], affinity for a body naming
// a cache_affinity_key, else plain (every P6 body parses whole).
func asyncPopOf(c authorizeCase) string {
	switch {
	case strings.Contains(c.Name, "async.none"):
		return ""
	case strings.Contains(c.Request.Body, `"cache_affinity_key"`):
		return AsyncAffinity
	}
	return AsyncPlain
}

// TestEveryAsyncGoldenThroughThePipeline: with the policy's edge_decide
// true (P6's), every answered case decides and, finished, is Python's 200
// byte for byte with its rows, its async members among them (an affinity
// case in the cold order, Python's warm bytes a differ.affinity_warm whose
// async members are the warm candidates'); every pass case passes with its
// reason, async.off and the reasons after the admit with a decision for
// the shadow; and each request names its async population.
func TestEveryAsyncGoldenThroughThePipeline(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	answered, passes, shapes := 0, 0, map[string]int{}
	for _, c := range f.Cases {
		in, _ := caseInputs(t, f, c)
		d := Decide(in)
		if d.AsyncPop != asyncPopOf(c) {
			t.Errorf("%s: population %q, want %q", c.Name, d.AsyncPop, asyncPopOf(c))
		}
		switch c.Expect.Outcome {
		case "answer":
			if d.Pass != "" || d.ForShadow() == nil {
				t.Errorf("%s: passed %q, want a decision", c.Name, d.Pass)
				continue
			}
			if !checkAnswered(t, c, d.ForShadow()) {
				continue
			}
			if c.Expect.Affinity != nil && !checkAffinity(t, c, d) {
				continue
			}
			shapes[c.Expect.AsyncShape]++
			answered++
		case "pass":
			reason := c.Expect.Reason
			if postAdmission(reason) || reason == PassAsyncOff {
				// A reason after the admit is the decide path's; async.off is
				// the decision gate's, its decision the shadow's.
				want := ""
				if reason == PassAsyncOff {
					want = PassAsyncOff
				}
				if d.Pass != want || d.ForShadow() == nil {
					t.Errorf("%s: passed %q with decision %v; Python's reason is %s", c.Name, d.Pass,
						d.ForShadow() != nil, reason)
					continue
				}
			} else if d.Pass != reason || d.ForShadow() != nil {
				t.Errorf("%s: passed %q (decision %v), want %q", c.Name, d.Pass, d.ForShadow() != nil, reason)
				continue
			}
			passes++
		default:
			t.Fatalf("%s: outcome %q", c.Name, c.Expect.Outcome)
		}
	}
	t.Logf("%d answered cases Python's bytes (by shape %v), %d pass cases with their reason", answered, shapes, passes)
	if answered != 32 || shapes["A"] != 16 || shapes["B"] != 15 || shapes["none"] != 1 || passes != 19 {
		t.Fatalf("answered %d (%v), passes %d: not the goldens' counts", answered, shapes, passes)
	}
}

// TestTheWarmAffinityAnswersAsyncMembersAreTheWarmCandidates (§4.4): the
// affinity case whose cold choice is shape B and whose remembered endpoint
// is one Candidate refuses: Python's warm 200 (shape A) is a
// differ.affinity_warm whose async members agree, rebuilt from the warm
// candidates; against the cold ones they would differ in shape.
func TestTheWarmAffinityAnswersAsyncMembersAreTheWarmCandidates(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := asyncCaseNamed(t, "answer:cache_affinity:async.warm_A.cold_B:openai/gpt-5-mini")
	in, _ := caseInputs(t, f, c)
	d := Decide(in)
	a := c.Expect.Affinity
	r := compareFull(d, pyAnswer{status: 200, body: []byte(a.Response)})
	if !warmDiffer(r) || r.async.compared != "agree" || len(r.async.fields) != 0 || r.async.pop != AsyncAffinity ||
		r.async.shape != gwauth.AsyncSnapshot {
		t.Fatalf("Python's warm bytes: %+v, async %+v", r, r.async)
	}
	py := pythonAsyncMembers([]byte(a.Response))
	if cold := compareAsync(d.ForShadow().Staged.Async(), py); cold.compared != "differ" ||
		!slices.Equal(cold.fields, []string{FieldAsyncShape}) {
		t.Fatalf("against the cold candidates: %+v", cold)
	}
}

// TestEdgeDecideFalseNeverAdmitsAnAsyncRequest is G15c's dark landing
// (§6.2): through the decide path, with the policy's edge_decide false (the
// production value until I4) no async-v1 request is answered by the edge,
// whatever its shape, even with its workspace's decide row on: each reaches
// Python, counted async.off, the owner never asked and nothing inserted.
// The positive control: edge_decide true, the edge answers each with
// Python's exact 200.
func TestEdgeDecideFalseNeverAdmitsAnAsyncRequest(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	py := newFakePython(t)
	dark, answered := 0, 0
	for _, golden := range f.Cases {
		if asyncPopOf(golden) == "" || (golden.Expect.Outcome != "answer" && golden.Expect.Reason != PassAsyncOff) {
			continue
		}
		for _, on := range []bool{false, true} {
			c := withEdgeDecide(t, f, golden, on)
			h := newDecideEdge(t, f, c, py)
			hits := py.hits.Load()
			rec := h.send(t, c)
			reached := py.hits.Load() - hits
			asked, _, _ := h.admitter.seen()
			counts := h.edge.DecideCounts()
			if !on {
				if rec.Code != http.StatusTeapot || reached != 1 || len(asked) != 0 || len(h.rows.inserted()) != 0 ||
					counts.Answered != 0 || !maps.Equal(counts.Passed, map[string]int64{PassAsyncOff: 1}) {
					t.Errorf("%s with edge_decide false: %d, Python reached %d, owner asked %d, counts %+v", c.Name,
						rec.Code, reached, len(asked), counts)
					continue
				}
				dark++
				continue
			}
			want := c.Expect.Response
			if c.Expect.Outcome != "answer" {
				want = c.Expect.Python.Response // Python's own 200 to the async.off case
			}
			if rec.Code != http.StatusOK || withGoldenTiming(rec.Body.Bytes()) != want || reached != 0 ||
				counts.Answered != 1 {
				t.Errorf("%s with edge_decide true: %d %s, Python reached %d\nwant %s", c.Name, rec.Code,
					rec.Body.Bytes(), reached, want)
				continue
			}
			answered++
		}
	}
	t.Logf("%d async-v1 requests passed with edge_decide false, %d answered with it true", dark, answered)
	if dark != 32 || answered != dark {
		t.Fatalf("dark %d, answered %d: not the goldens' counts", dark, answered)
	}
}

// TestTheUnpublishedGateIsKeyless: an async-v1 request passes before its
// key is looked up when the policy carries no async_settle object or the
// snapshot no billing_valid (async.unpublished); without the header, the
// same request decides. Python's admission (policy.async_admission) is
// gated once the key's workspace is known (TestTheAdmissionGateReadsTheKeysWorkspace).
func TestTheUnpublishedGateIsKeyless(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := asyncCaseNamed(t, "answer:async.B:single:openai/gpt-4.1")
	for _, tc := range []struct {
		name   string
		change func(*DecideInputs)
		want   string
	}{
		{"no async_settle", func(in *DecideInputs) {
			p := *in.Policy
			p.AsyncSettle = nil
			in.Policy = &p
		}, PassAsyncUnpublished},
		{"no billing_valid", func(in *DecideInputs) {
			raw := strings.ReplaceAll(f.SnapshotBody, `"billing_valid":true,`, "")
			s, err := routingsnapshot.Parse([]byte(raw))
			if err != nil || s.BillingValidPublished() {
				t.Fatalf("the stripped snapshot: %v", err)
			}
			in.Snapshot = regionsFor(s, in.Policy)
		}, PassAsyncUnpublished},
	} {
		in, _ := caseInputs(t, f, c)
		tc.change(&in)
		looked := 0
		lookup := in.Lookup
		in.Lookup = func(h string, now time.Time) gwstate.Read {
			looked++
			return lookup(h, now)
		}
		if d := Decide(in); d.Pass != tc.want || d.ForShadow() != nil || looked != 0 {
			t.Errorf("%s: passed %q, decision %v, %d lookups", tc.name, d.Pass, d.ForShadow() != nil, looked)
		}
		in.Request.Header = in.Request.Header.Clone()
		in.Request.Header.Del("X-TR-Settlement-Mode")
		if d := Decide(in); d.Pass != "" || d.AsyncPop != "" {
			t.Errorf("%s without the header: passed %q, population %q", tc.name, d.Pass, d.AsyncPop)
		}
	}
	// The P1 goldens' policy predates async_settle: their exact async-v1
	// request passes async.unpublished, its sync one header.settlement_async.
	p1 := authorizeGoldens(t)
	sync := authorizeCaseNamed(t, "pass:header.settlement_async")
	in, _ := caseInputs(t, p1, sync)
	if d := Decide(in); d.Pass != gwauth.ReasonSettlementAsync || d.AsyncPop != "" {
		t.Errorf("sync: %q, population %q", d.Pass, d.AsyncPop)
	}
	in.Request.Header = in.Request.Header.Clone()
	in.Request.Header.Set("X-TR-Settlement-Mode", "async-v1")
	if d := Decide(in); d.Pass != PassAsyncUnpublished || d.AsyncPop != AsyncPlain {
		t.Errorf("async-v1 under P1's policy: %q, population %q", d.Pass, d.AsyncPop)
	}
}

// admissionOn is the async_settle change for Python's admission on, its
// admission_workspaces the given set (nil: the member absent).
func admissionOn(workspaces ...string) func(*routingsnapshot.AsyncSettle) {
	return func(a *routingsnapshot.AsyncSettle) {
		a.AdmissionEnabled, a.AdmissionWorkspaces = true, nil
		if workspaces != nil {
			w := slices.Clone(workspaces)
			a.AdmissionWorkspaces = &w
		}
	}
}

// TestTheAdmissionGateReadsTheKeysWorkspace (G15 §4.1, per workspace): with
// Python's admission on, every answered golden's async-v1 request passes
// policy.async_admission, with no decision, when admission_workspaces is
// absent (every workspace, as before the member) or names the key's
// workspace; when the member leaves that workspace out, the request is
// decided exactly as with admission off, Python's admission-off 200 byte
// for byte (shape A or B: no ticket, no status URL). A request without the
// header decides whatever the member says.
func TestTheAdmissionGateReadsTheKeysWorkspace(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	covered, composed := 0, 0
	for _, golden := range f.Cases {
		if golden.Expect.Outcome != "answer" || golden.Expect.Affinity != nil {
			continue
		}
		ws := golden.Facts.Key.WorkspaceID
		if ws == "" {
			t.Fatalf("%s: no workspace", golden.Name)
		}
		for _, v := range []struct {
			name   string
			change func(*routingsnapshot.AsyncSettle)
			admits bool
		}{
			{"absent", admissionOn(), true},
			{"naming the workspace", admissionOn("a_"+ws, ws, "z_"+ws), true},
			{"naming it alone", admissionOn(ws), true},
			{"leaving it out", admissionOn("a_"+ws, "z_"+ws), false},
		} {
			c := withAsyncSettle(t, f, golden, v.change)
			in, _ := caseInputs(t, f, c)
			d := Decide(in)
			if v.admits && asyncPopOf(golden) != "" {
				if d.Pass != PassAsyncAdmission || d.ForShadow() != nil || d.Workspace != ws {
					t.Errorf("%s, admission_workspaces %s: passed %q (decision %v, workspace %q), want %q", golden.Name,
						v.name, d.Pass, d.ForShadow() != nil, d.Workspace, PassAsyncAdmission)
					continue
				}
				covered++
				continue
			}
			if d.Pass != "" || d.ForShadow() == nil {
				t.Errorf("%s, admission_workspaces %s: passed %q, want a decision", golden.Name, v.name, d.Pass)
				continue
			}
			if !checkAnswered(t, c, d.ForShadow()) {
				continue
			}
			if a := d.ForShadow().Staged.Async(); asyncPopOf(golden) != "" && a.Shape == gwauth.AsyncNone {
				t.Errorf("%s, admission_workspaces %s: no async members", golden.Name, v.name)
			}
			composed++
		}
	}
	t.Logf("%d requests covered by admission, %d composed as with admission off", covered, composed)
	if covered != 3*29 || composed != 29+4 {
		t.Fatalf("covered %d, composed %d: not the goldens' counts", covered, composed)
	}
}

// TestTheAdmissionGateIsAfterTheLookup: Python's admission covering the
// workspace, an async-v1 request whose key the cache cannot answer for
// passes with the cache's reason (the workspace is not known), the key
// looked up once; its key found, it passes policy.async_admission before
// any of the key's own facts, as the gate stood before every key gate.
func TestTheAdmissionGateIsAfterTheLookup(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	golden := asyncCaseNamed(t, "answer:async.B:single:openai/gpt-4.1")
	federated := asyncCaseNamed(t, "pass:key.federated:async.A")
	for _, v := range []struct {
		name   string
		c      authorizeCase
		lookup func(gwstate.Read) gwstate.Read
		want   string
	}{
		{"key missing", golden, func(r gwstate.Read) gwstate.Read { r.Found = false; return r }, PassKeyMissing},
		{"key stale", golden, func(r gwstate.Read) gwstate.Read { r.Key = gwstate.Stale; return r }, PassCacheKey},
		{"key found", golden, nil, PassAsyncAdmission},
		{"federated key", federated, nil, PassAsyncAdmission},
	} {
		c := withAsyncSettle(t, f, v.c, admissionOn(v.c.Facts.Key.WorkspaceID))
		in, _ := caseInputs(t, f, c)
		looked := 0
		lookup := in.Lookup
		in.Lookup = func(h string, now time.Time) gwstate.Read {
			looked++
			r := lookup(h, now)
			if v.lookup != nil {
				r = v.lookup(r)
			}
			return r
		}
		if d := Decide(in); d.Pass != v.want || d.ForShadow() != nil || looked != 1 {
			t.Errorf("%s: passed %q (decision %v), %d lookups; want %q after one", v.name, d.Pass,
				d.ForShadow() != nil, looked, v.want)
		}
	}
	// The control: the federated key outside the member passes with its
	// own reason, as with admission off.
	c := withAsyncSettle(t, f, federated, admissionOn("a_"+federated.Facts.Key.WorkspaceID))
	in, _ := caseInputs(t, f, c)
	if d := Decide(in); d.Pass != federated.Expect.Reason {
		t.Errorf("federated key outside the member: %q, want %q", d.Pass, federated.Expect.Reason)
	}
}

// asyncCountsOf are the shadow's async-v1 counts, every map present.
func asyncCountsOf(seen, compared, fields, shape, admitted map[string]int) map[string]map[string]int {
	out := map[string]map[string]int{asyncSeen: seen, asyncCompared: compared, asyncFields: fields, asyncShape: shape,
		asyncPythonAdmitted: admitted}
	for k, m := range out {
		if m == nil {
			out[k] = map[string]int{}
		}
	}
	return out
}

func asyncCountsEqual(a, b map[string]map[string]int) bool {
	return maps.EqualFunc(a, b, func(x, y map[string]int) bool { return maps.Equal(x, y) })
}

// TestTheShadowComparesTheAsyncMembers (§4.4): with edge_decide false, an
// async-v1 request is composed, passed (async.off, the decide row on) and
// compared. Python's own 200 is agree_full, its async members agreeing;
// each async member changed is a differ with its field, in the whole 200's
// fields and in async_fields, and async_compared differ; a difference
// elsewhere leaves the members' agreement standing; Python's shape C is
// async_python_admitted and its members not compared. Each count reaches
// the period's line, which ParseCountsLine reads.
func TestTheShadowComparesTheAsyncMembers(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	c := asyncCaseNamed(t, "pass:async.off:async.B")
	raw := c.Expect.Python.Response
	if c.Expect.Python.AsyncShape != "B" || raw == "" {
		t.Fatalf("the async.off case: shape %q", c.Expect.Python.AsyncShape)
	}
	snapshot := func(change func(pyjson.Object) pyjson.Object) []byte {
		return mutated(t, raw, func(d pyjson.Object) pyjson.Object {
			return setMember(d, "billing_snapshot", change(memberOf(d, "billing_snapshot").(pyjson.Object)))
		})
	}
	without := func(keys ...string) func(pyjson.Object) pyjson.Object {
		return func(d pyjson.Object) pyjson.Object {
			return slices.DeleteFunc(d, func(m pyjson.Member) bool { return slices.Contains(keys, m.Key) })
		}
	}
	plain := func(k string) map[string]int { return map[string]int{AsyncPlain + "." + k: 1} }
	// One character of the snapshot escaped: the same decoded value, other
	// bytes (§4.4 compares the snapshot's bytes).
	// The underscore as JSON's six-character escape (backslash, u, 005f).
	const kind, escaped = `"kind":"credits_endpoint"`, `"kind":"credits` + "\\" + `u005fendpoint"`
	if strings.Count(raw, kind) != 1 || len(escaped) != len(kind)+5 {
		t.Fatalf("the snapshot's kind is not once in Python's 200, or the escape is not one")
	}
	seen, shape := map[string]int{AsyncPlain: 1}, plain(gwauth.AsyncSnapshot)
	py := newFakePython(t)
	for _, v := range []struct {
		name     string
		body     []byte
		class    string
		fields   []string
		compared map[string]int
		admitted map[string]int
	}{
		{"Python's", []byte(raw), ClassAgreeFull, nil, plain("agree"), nil},
		{"snapshot", snapshot(func(s pyjson.Object) pyjson.Object { return setMember(s, "minimum_charge", "none") }),
			ClassDifferAny, []string{FieldBillingSnapshot}, plain("differ"), nil},
		{"snapshot escaped", []byte(strings.Replace(raw, kind, escaped, 1)), ClassDifferAny,
			[]string{FieldBillingSnapshot}, plain("differ"), nil},
		{"hash", mutated(t, raw, func(d pyjson.Object) pyjson.Object {
			return setMember(d, "billing_snapshot_hash", strings.Repeat("0", 64))
		}), ClassDifferAny, []string{FieldBillingSnapshotHash}, plain("differ"), nil},
		{"eligible", mutated(t, raw, func(d pyjson.Object) pyjson.Object { return setMember(d, "async_eligible", true) }),
			ClassDifferAny, []string{FieldAsyncEligible}, plain("differ"), nil},
		{"shape A", mutated(t, raw, without("billing_snapshot", "billing_snapshot_hash")), ClassDifferAny,
			[]string{FieldAsyncShape}, plain("differ"), nil},
		{"elsewhere", mutated(t, raw, func(d pyjson.Object) pyjson.Object { return setMember(d, "provider_name", "X") }),
			ClassDifferAny, []string{"differ.provider_name"}, plain("agree"), nil},
		{"shape C", ticketed(t, raw), ClassDifferAny, []string{FieldAsyncShape}, nil,
			map[string]int{AsyncPlain: 1}},
	} {
		h := newFullEdge(t, f, withEdgeDecide(t, f, c, false), py, nil)
		h.answer(answerWith(200, v.body))
		h.send(t, c)
		got := h.edge.DecideShadowCounts()
		wantFields := map[string]int{}
		asyncFieldCounts := map[string]int{}
		for _, field := range v.fields {
			wantFields[field] = 1
			if strings.HasPrefix(field, "differ.async") || strings.HasPrefix(field, "differ.billing") {
				asyncFieldCounts[AsyncPlain+"."+field] = 1
			}
		}
		if v.admitted != nil {
			asyncFieldCounts = nil // shape C's members are not compared
		}
		want := asyncCountsOf(seen, v.compared, asyncFieldCounts, shape, v.admitted)
		if !maps.Equal(got.Classes, map[string]int{v.class: 1}) || !maps.Equal(got.Fields, wantFields) ||
			!asyncCountsEqual(got.Async, want) || !maps.Equal(got.Decide, map[string]int{PassAsyncOff: 1}) {
			t.Errorf("%s: classes %v, fields %v, decide %v, async %v\nwant async %v", v.name, got.Classes, got.Fields,
				got.Decide, got.Async, want)
		}
		if v.name != "snapshot" {
			continue
		}
		// The period's line carries each map, and the watch's reader reads it.
		var line CountsLine
		for _, l := range strings.Split(h.logs.all(), "\n") {
			if parsed, ok, err := ParseCountsLine(l); ok && err == nil && parsed.Digest != "" {
				line = parsed
			}
		}
		for name, m := range want {
			if len(m) == 0 {
				continue
			}
			for k, n := range m {
				if line.ByName[name][k] != int64(n) {
					t.Errorf("the line's %s: %v, want %v", name, line.ByName[name], m)
				}
			}
		}
	}
}

// TestTheShadowComparesAWorkspaceAdmissionLeavesOut (§4.4): with Python's
// admission on and admission_workspaces leaving the workspace out, an
// async-v1 request is composed, passed with async.off and compared as with
// admission off: Python's admission-off 200 agrees in full; a Python that
// still tickets it (one before the per-workspace answer) is
// async_python_admitted, its members not compared. A workspace the member
// names passes policy.async_admission and is not composed.
func TestTheShadowComparesAWorkspaceAdmissionLeavesOut(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	off := asyncCaseNamed(t, "pass:async.off:async.B")
	raw, ws := off.Expect.Python.Response, off.Facts.Key.WorkspaceID
	if off.Expect.Python.AsyncShape != "B" || raw == "" || ws == "" {
		t.Fatalf("the async.off case: shape %q", off.Expect.Python.AsyncShape)
	}
	left := withAsyncSettle(t, f, off, admissionOn("a_"+ws))
	named := withAsyncSettle(t, f, off, admissionOn("a_"+ws, ws))
	plain := map[string]int{AsyncPlain: 1}
	shape := map[string]int{AsyncPlain + "." + gwauth.AsyncSnapshot: 1}
	py := newFakePython(t)
	for _, v := range []struct {
		name   string
		c      authorizeCase
		body   []byte
		class  string
		decide map[string]int
		want   map[string]map[string]int
	}{
		{"left out, Python's admission-off 200", left, []byte(raw), ClassAgreeFull, map[string]int{PassAsyncOff: 1},
			asyncCountsOf(plain, map[string]int{AsyncPlain + ".agree": 1}, nil, shape, nil)},
		{"left out, Python tickets it", left, ticketed(t, raw), ClassDifferAny, map[string]int{PassAsyncOff: 1},
			asyncCountsOf(plain, nil, nil, shape, plain)},
		{"named", named, ticketed(t, raw), ClassPythonAnsweredGoPassed + "." + PassAsyncAdmission, map[string]int{},
			asyncCountsOf(plain, nil, nil, nil, plain)},
	} {
		h := newFullEdge(t, f, v.c, py, nil)
		h.answer(answerWith(200, v.body))
		h.send(t, v.c)
		got := h.edge.DecideShadowCounts()
		if !maps.Equal(got.Classes, map[string]int{v.class: 1}) || !asyncCountsEqual(got.Async, v.want) ||
			!maps.Equal(got.Decide, v.decide) {
			t.Errorf("%s: classes %v (fields %v), decide %v, async %v\nwant %v", v.name, got.Classes, got.Fields,
				got.Decide, got.Async, v.want)
		}
	}
}

// TestTheShadowCountsTheAsyncPopulations (§4.4): async_seen counts every
// followed request whose header is exactly async-v1, whatever it passes
// with (unparsed for one whose body the edge did not parse, a boot
// failure), and nothing else: a cohort reason after the parse counts its
// population and no shape, and any other header list counts nothing. An
// affinity request is compared like any other: Python's warm 200 a
// differ.affinity_warm whose async members agree, rebuilt from the warm
// candidates.
func TestTheShadowCountsTheAsyncPopulations(t *testing.T) {
	f := asyncAuthorizeGoldens(t)
	py := newFakePython(t)
	noBoot := asyncCaseNamed(t, "pass:async.off:async.B")
	noBoot.Request.Headers = slices.DeleteFunc(slices.Clone(noBoot.Request.Headers),
		func(h [2]string) bool { return strings.EqualFold(h[0], BootHeader) })
	warm := asyncCaseNamed(t, "answer:cache_affinity:async.warm_A.cold_B:openai/gpt-5-mini")
	service := asyncCaseNamed(t, "pass:body.service_tier:async.B:default")
	sync := asyncCaseNamed(t, "pass:header.settlement_async:async.none:sync")
	for _, v := range []struct {
		name  string
		c     authorizeCase
		body  []byte
		class string
		want  map[string]map[string]int
	}{
		{"no boot", noBoot, []byte(noBoot.Expect.Python.Response), ClassPythonAnsweredGoPassed + "." + PassBootSignature,
			asyncCountsOf(map[string]int{AsyncUnparsed: 1}, nil, nil, nil, nil)},
		{"service tier", service, []byte(service.Expect.Python.Response),
			ClassPythonAnsweredGoPassed + "." + gwauth.ReasonServiceTier,
			asyncCountsOf(map[string]int{AsyncPlain: 1}, nil, nil, nil, nil)},
		{"sync", sync, []byte(sync.Expect.Python.Response),
			ClassPythonAnsweredGoPassed + "." + gwauth.ReasonSettlementAsync, asyncCountsOf(nil, nil, nil, nil, nil)},
		{"affinity warm", withEdgeDecide(t, f, warm, false), []byte(warm.Expect.Affinity.Response), ClassDifferAny,
			asyncCountsOf(map[string]int{AsyncAffinity: 1}, map[string]int{AsyncAffinity + ".agree": 1}, nil,
				map[string]int{AsyncAffinity + "." + gwauth.AsyncSnapshot: 1}, nil)},
	} {
		h := newFullEdge(t, f, v.c, py, nil)
		h.answer(answerWith(200, v.body))
		h.send(t, v.c)
		got := h.edge.DecideShadowCounts()
		if !maps.Equal(got.Classes, map[string]int{v.class: 1}) || !asyncCountsEqual(got.Async, v.want) {
			t.Errorf("%s: classes %v (fields %v), async %v\nwant %v", v.name, got.Classes, got.Fields, got.Async, v.want)
		}
	}
}

// ticketed is a whole Python 200 of shape B made shape C, as Python
// answers with its admission on (§2.2): the ticket and the status URL after
// the snapshot's hash, async_eligible kept (Python signs a ticket for a
// false answer too).
func ticketed(t *testing.T, raw string) []byte {
	t.Helper()
	return mutated(t, raw, func(d pyjson.Object) pyjson.Object {
		at := slices.IndexFunc(d, func(m pyjson.Member) bool { return m.Key == "billing_snapshot_hash" })
		id, _ := memberOf(d, "authorization_id").(string)
		if at < 0 || id == "" {
			t.Fatal("not a shape B 200")
		}
		return slices.Insert(d, at+1,
			pyjson.Member{Key: "settlement_ticket", Value: "eyJhbGciOiJFZERTQSJ9.eyJpc3MiOiJ0ZXN0In0.c2lnbmF0dXJl"},
			pyjson.Member{Key: "settlement_status_url", Value: "/v1/settlements/" + id + ".settle"})
	})
}

// TestATicketIsCountedWhateverGoDid (§3.6, §4.4): Python's shape C to a
// followed async-v1 request is async_python_admitted whatever Go did with
// it, also when Go passed it before the compose (Python's admission on; a
// cohort reason after the parse; a body the parse refuses, unparsed), and
// never compared. The control: Python's shape B to the same request counts
// no ticket.
func TestATicketIsCountedWhateverGoDid(t *testing.T) {
	f, p1 := asyncAuthorizeGoldens(t), authorizeGoldens(t)
	py := newFakePython(t)
	shapeB := asyncCaseNamed(t, "answer:async.B:invocation_nonce").Expect.Response
	service := asyncCaseNamed(t, "pass:body.service_tier:async.B:default")
	lax := authorizeCaseNamed(t, "pass:body.lax_type")
	lax.Request.Headers = append(slices.Clone(lax.Request.Headers), [2]string{"X-TR-Settlement-Mode", "async-v1"})
	for _, v := range []struct {
		name  string
		f     authorizeFile
		c     authorizeCase
		body  string // Python's shape B 200 to the request
		class string
		pop   string
	}{
		{"admission on", f, asyncCaseNamed(t, "pass:policy.async_admission:async.C"), shapeB, PassAsyncAdmission,
			AsyncPlain},
		{"service tier", f, service, service.Expect.Python.Response, gwauth.ReasonServiceTier, AsyncPlain},
		{"lax type", p1, lax, shapeB, gwauth.ReasonLaxType, AsyncUnparsed},
	} {
		for _, ticket := range []bool{false, true} {
			body, admitted := []byte(v.body), map[string]int(nil)
			if ticket {
				body, admitted = ticketed(t, v.body), map[string]int{v.pop: 1}
			}
			h := newFullEdge(t, v.f, v.c, py, nil)
			h.answer(answerWith(200, body))
			h.send(t, v.c)
			got := h.edge.DecideShadowCounts()
			want := asyncCountsOf(map[string]int{v.pop: 1}, nil, nil, nil, admitted)
			if !maps.Equal(got.Classes, map[string]int{ClassPythonAnsweredGoPassed + "." + v.class: 1}) ||
				!asyncCountsEqual(got.Async, want) {
				t.Errorf("%s, ticket %v: classes %v, async %v\nwant %v", v.name, ticket, got.Classes, got.Async, want)
			}
		}
	}
}
