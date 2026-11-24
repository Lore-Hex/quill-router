package edge

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// strictKeyLimitOf is a golden case's key_limit_row as the store reads it
// (store.StrictKeyLimit): null is a missing row, an object its four
// mirrored limits.
func strictKeyLimitOf(raw json.RawMessage) store.StrictKeyLimit {
	if string(raw) == "null" {
		return store.StrictKeyLimit{Read: true}
	}
	var row map[string]*int64
	if json.Unmarshal(raw, &row) != nil || len(row) != 4 {
		return store.StrictKeyLimit{}
	}
	out := store.StrictKeyLimit{Read: true, Row: true, Limitless: true}
	for _, limit := range row {
		if limit != nil {
			out.Limitless = false
		}
	}
	return out
}

// withStrictMode is the case with the edge policy's
// fastpath_pilot_strict_keys at mode, "" for absent.
func withStrictMode(c authorizeCase, mode string) authorizeCase {
	patch := map[string]json.RawMessage{}
	for k, v := range c.PolicyPatch {
		patch[k] = v
	}
	delete(patch, "fastpath_pilot_strict_keys")
	if mode != "" {
		patch["fastpath_pilot_strict_keys"] = json.RawMessage(`"` + mode + `"`)
	}
	c.PolicyPatch = patch
	return c
}

// TestTheStrictGoldensUnderEachMode
// (docs/design/fast-admission-e2-strict-keys.md section 7): the golden cases
// Python's own route answered for strict keys, under each mode of the edge
// policy's fastpath_pilot_strict_keys. The limitless key passes as
// key.budget_strict with the member absent; under shadow and enforce Go
// composes it, and its 200, staged under Python's ids, is Python's strict
// path's 200 byte for byte (Stage D disabled, the same estimate,
// reservation, fingerprint and scope), while Python's reservation row holds
// the key's estimate on the key (its strict hold) and Go's holds nothing on
// it. The key Python 402s for a missing row or a mirrored limit passes under
// every mode, as do the key with two usage shards Python answers 500 and a
// strict key with a lifetime limit.
func TestTheStrictGoldensUnderEachMode(t *testing.T) {
	f := authorizeGoldens(t)
	shadow, enforce := routingsnapshot.StrictKeysShadow, routingsnapshot.StrictKeysEnforce
	const limitless, noRow, mirrored, capped, sharded = "pass:key.budget_strict", "pass:key.budget_strict:no_row",
		"pass:key.budget_strict:mirror_limit", "pass:key.key_cap:strict", "pass:key.budget_strict:two_shards"
	for _, v := range []struct {
		name, mode, pass string
		decided          bool
	}{
		{limitless, "", PassKeyBudgetStrict, false},
		{limitless, shadow, PassKeyBudgetStrict, true},
		{limitless, enforce, "", true},
		{noRow, "", PassKeyBudgetStrict, false},
		{noRow, shadow, PassKeyBudgetStrict, false},
		{noRow, enforce, PassKeyBudgetStrictRow + gwstate.StrictRowMissing, false},
		{mirrored, "", PassKeyBudgetStrict, false},
		{mirrored, shadow, PassKeyBudgetStrict, false},
		{mirrored, enforce, PassKeyBudgetStrictRow + gwstate.StrictRowLimited, false},
		{sharded, "", PassKeyBudgetStrict, false},
		{sharded, shadow, PassKeyBudgetStrict, false},
		{sharded, enforce, PassKeyBudgetStrictShards, false},
		{capped, "", PassKeyCap, false},
		{capped, shadow, PassKeyCap, false},
		{capped, enforce, PassKeyCap, false},
	} {
		c := withStrictMode(authorizeCaseNamed(t, v.name), v.mode)
		label := v.name + "@" + v.mode
		if c.KeyLimitRow == nil {
			t.Fatalf("%s: the golden records no key_limit_row", label)
		}
		in, _ := caseInputs(t, f, c)
		d := Decide(in)
		if d.Pass != v.pass || (d.ForShadow() != nil) != v.decided {
			t.Errorf("%s: passed %q decision %v, want %q %v", label, d.Pass, d.ForShadow() != nil, v.pass, v.decided)
			continue
		}
		if !v.decided {
			continue
		}
		s := c.Expect.Shadow
		if s == nil || c.Expect.Python.Leased || c.Expect.Python.Status != http.StatusOK {
			t.Fatalf("%s: the golden holds no unleased 200 of Python's", label)
		}
		dec := d.ForShadow()
		if !dec.StrictLimitless || dec.Input.StageDReason != "stage_d_disabled" {
			t.Errorf("%s: strict %v, Stage D %q", label, dec.StrictLimitless, dec.Input.StageDReason)
		}
		if dec.Estimate != s.Estimate || dec.Reservation != s.Reservation || dec.Identity.Fingerprint != s.Fingerprint ||
			dec.Identity.Scope != s.IdempotencyScope {
			t.Errorf("%s: estimate %d reservation %d fingerprint %s scope %s, Python's %d %d %s %s", label,
				dec.Estimate, dec.Reservation, dec.Identity.Fingerprint, dec.Identity.Scope, s.Estimate, s.Reservation,
				s.Fingerprint, s.IdempotencyScope)
		}
		authorization, _ := s.AuthorizationRow["authorization_id"].(string)
		reservation, _ := s.ReservationRow["reservation_id"].(string)
		staged, err := dec.Restage(authorization, reservation, dec.Input.IdempotencyKey, dec.Input.At)
		if err != nil {
			t.Fatalf("%s: %v", label, err)
		}
		composed, err := staged.Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
		if err != nil {
			t.Fatalf("%s: %v", label, err)
		}
		if got := composed.Render(s.timing(), false); string(got) != s.Response {
			t.Errorf("%s: the 200 differs from Python's\n got %s\nwant %s", label, got, s.Response)
		}
		// The one money difference, by design: Python's strict path holds
		// the estimate on the key; the edge holds nothing on it.
		if held, _ := s.ReservationRow["key_reserved_micro"].(float64); int64(held) != s.Estimate {
			t.Errorf("%s: Python's key hold %v, want its estimate %d", label, held, s.Estimate)
		}
		if composed.Reservation.KeyHash != dec.KeyHash {
			t.Errorf("%s: the reservation names key %q", label, composed.Reservation.KeyHash)
		}
	}
}

// TestTheStrictGoldenThroughTheShadow: under shadow, the full shadow follows
// the limitless strict key the edge passes, and Python's 200 agrees with
// Go's composition, its rows today's path's (python_not_leased), the class
// counted in the strict map; the key Python 402s for a missing row Go
// passed too, a refusal both made, which no strict count holds.
func TestTheStrictGoldenThroughTheShadow(t *testing.T) {
	f := authorizeGoldens(t)
	py := newFakePython(t)
	for _, v := range []struct {
		name, class string
		strict      map[string]int
	}{
		{"pass:key.budget_strict", ClassAgreeFull, map[string]int{ClassAgreeFull: 1}},
		{"pass:key.budget_strict:no_row", ClassGoPassed + "." + PassKeyBudgetStrict, map[string]int{}},
	} {
		c := withStrictMode(authorizeCaseNamed(t, v.name), routingsnapshot.StrictKeysShadow)
		h := newFullEdge(t, f, c, py, nil)
		if s := c.Expect.Shadow; s != nil {
			answered := c
			answered.Expect = *s
			id, _ := s.AuthorizationRow["authorization_id"].(string)
			h.rows.by[id] = storedRows(t, answered)
			h.answer(answerWith(http.StatusOK, []byte(s.Response)))
		} else {
			h.answer(answerWith(c.Expect.Python.Status, errorBody(c.Expect.Python.Status,
				"key_limit_exceeded", "API key spend limit exceeded")))
		}
		h.send(t, c)
		got := h.edge.DecideShadowCounts()
		if got.Classes[v.class] != 1 || len(got.Classes) != 1 {
			t.Errorf("%s: classes %v (fields %v), want %s", v.name, got.Classes, got.Fields, v.class)
		}
		if len(got.Strict) != len(v.strict) || got.Strict[ClassAgreeFull] != v.strict[ClassAgreeFull] {
			t.Errorf("%s: strict %v, want %v", v.name, got.Strict, v.strict)
		}
		if v.class == ClassAgreeFull && got.Rows["python_not_leased"] != 1 {
			t.Errorf("%s: rows %v", v.name, got.Rows)
		}
	}
}

// strictDecided is a limitless strict key's decision under mode, its row
// one Python's reserve matches as limitless.
func strictDecided(t *testing.T, mode string) (Decided, DecideInputs) {
	t.Helper()
	in := strictInputs(t, map[string]any{"budget_strict": true}, mode, "", strictRowOK)
	d := Decide(in)
	if d.ForShadow() == nil || !d.ForShadow().StrictLimitless {
		t.Fatalf("%s: no strict decision: %q", mode, d.Pass)
	}
	return d, in
}

// goAnswer is the decision's own 200, zero timing: what Python's answer is
// when the two agree.
func goAnswer(t *testing.T, d Decided, in DecideInputs) []byte {
	t.Helper()
	composed, err := d.ForShadow().Finish("envelope", in.Request.At.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	return composed.Render(gwauth.Timing{}, false)
}

// TestTheShadowCountsAStrictKeysComparisonsApart
// (docs/design/fast-admission-e2-strict-keys.md section 6): the classes of a
// limitless strict key's decisions go to the strict map as well; Python's
// refusal of one Go would admit is the would-admit, paged; Python's
// strict-budget-busy 503 is strict.python_busy, never paged, and only for a
// strict decision (the same 503 to another is python_error.503, and an
// unread one stays python_error.503.unread); Python's 200 that is Go's
// answer agrees.
func TestTheShadowCountsAStrictKeysComparisonsApart(t *testing.T) {
	var lines []string
	var alerts []string
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{}, Rows: &fakeRows{},
		Alert: func(kind, _, what string) { alerts = append(alerts, kind+": "+what) }},
		func(format string, args ...any) {
			if strings.HasPrefix(format, "fastpath edge decide shadow: %s") {
				lines = append(lines, format)
			}
		}, time.Now, 0)
	if err != nil {
		t.Fatal(err)
	}
	shadow, shadowIn := strictDecided(t, routingsnapshot.StrictKeysShadow)
	enforce, _ := strictDecided(t, routingsnapshot.StrictKeysEnforce)
	plainIn := strictInputs(t, map[string]any{"budget_strict": false}, routingsnapshot.StrictKeysEnforce, "",
		strictRowUnread)
	plain := Decide(plainIn)
	if plain.ForShadow() == nil || plain.ForShadow().StrictLimitless {
		t.Fatalf("the control is no uncapped decision: %q", plain.Pass)
	}
	busy := pyAnswer{status: http.StatusServiceUnavailable,
		body: errorBody(http.StatusServiceUnavailable, "service_unavailable", strictBusyMessage)}
	missing := pyAnswer{status: http.StatusPaymentRequired,
		body: errorBody(http.StatusPaymentRequired, "key_limit_exceeded", "API key spend limit exceeded")}
	for _, v := range []struct {
		name   string
		d      Decided
		a      pyAnswer
		class  string
		strict bool
	}{
		{"busy, shadow", shadow, busy, ClassStrictPythonBusy, true},
		{"busy, enforce", enforce, busy, ClassStrictPythonBusy, true},
		{"busy, not strict", plain, busy, ClassPythonError + ".503", false},
		{"busy, unread", shadow, pyAnswer{status: http.StatusServiceUnavailable, why: "bound"},
			ClassPythonError + ".503.unread", true},
		{"another 503", shadow, pyAnswer{status: http.StatusServiceUnavailable,
			body: errorBody(http.StatusServiceUnavailable, "service_unavailable", "Persistent storage is temporarily unavailable; retry.")},
			ClassPythonError + ".503", true},
		{"no row, 402", shadow, missing, ClassWouldAdmit + ".402.key_limit_exceeded", true},
		{"a window 429", enforce, pyAnswer{status: http.StatusTooManyRequests,
			body: errorBody(http.StatusTooManyRequests, "key_window_limit_exceeded", "limit")},
			ClassWouldAdmit + ".429.key_window_limit_exceeded", true},
		{"agree", shadow, pyAnswer{status: http.StatusOK, body: goAnswer(t, shadow, shadowIn)}, ClassAgreeFull, true},
	} {
		r := sh.safeCompare(v.d, v.a, nil, "")
		if r.class != v.class || r.strict != v.strict {
			t.Errorf("%s: class %q strict %v (fields %v), want %q %v", v.name, r.class, r.strict, r.fields, v.class,
				v.strict)
		}
		sh.record("d", r)
	}
	sh.mu.Lock()
	strict := clonedCounts(sh.totals.strict)
	sh.mu.Unlock()
	want := map[string]int{ClassStrictPythonBusy: 2, ClassPythonError + ".503.unread": 1,
		ClassPythonError + ".503": 1, ClassWouldAdmit + ".402.key_limit_exceeded": 1,
		ClassWouldAdmit + ".429.key_window_limit_exceeded": 1, ClassAgreeFull: 1}
	if len(strict) != len(want) {
		t.Fatalf("the strict counts %v, want %v", strict, want)
	}
	for class, n := range want {
		if strict[class] != n {
			t.Errorf("strict %s: %d, want %d (%v)", class, strict[class], n, strict)
		}
	}
	// The would-admits page; strict.python_busy never does.
	if len(alerts) != 2 {
		t.Fatalf("alerts %q, want the two would-admits", alerts)
	}
	for _, a := range alerts {
		if !strings.HasPrefix(a, AlertWouldAdmit+": ") || strings.Contains(a, ClassStrictPythonBusy) {
			t.Errorf("alert %q", a)
		}
	}
	sh.flush()
	if len(lines) != 1 || !strings.HasSuffix(lines[0], " strict=%s") {
		t.Fatalf("the period's line: %q", lines)
	}
	if got := (&Edge{full: sh}).DecideShadowCounts(); got.Strict == nil {
		t.Fatal("DecideShadowCounts has no strict map")
	}
}

// TestTheStrictCountsReadBack: the shadow's line, strict map included, is
// one the count reader reads (counts.go), so fastpathwatch sums it.
func TestTheStrictCountsReadBack(t *testing.T) {
	var lines []string
	sh, err := newFullShadow(DecideShadowConfig{Cache: &recordingCache{}, Decides: &fakeDecides{}, Rows: &fakeRows{}},
		func(format string, args ...any) {
			if strings.HasPrefix(format, "fastpath edge decide shadow: %s") {
				lines = append(lines, fmt.Sprintf(format, args...))
			}
		}, time.Now, 0)
	if err != nil {
		t.Fatal(err)
	}
	d, in := strictDecided(t, routingsnapshot.StrictKeysShadow)
	sh.record("snapshot=s policy=p", sh.safeCompare(d, pyAnswer{status: http.StatusOK, body: goAnswer(t, d, in)}, nil, ""))
	sh.record("snapshot=s policy=p", sh.safeCompare(d, pyAnswer{status: http.StatusServiceUnavailable,
		body: errorBody(http.StatusServiceUnavailable, "service_unavailable", strictBusyMessage)}, nil, ""))
	sh.flush()
	if len(lines) != 1 {
		t.Fatalf("lines %q", lines)
	}
	l, ok, err := ParseCountsLine(lines[0])
	if err != nil || !ok {
		t.Fatalf("%v %v: %s", ok, err, lines[0])
	}
	if s := l.ByName["strict"]; s[ClassAgreeFull] != 1 || s[ClassStrictPythonBusy] != 1 || len(s) != 2 {
		t.Fatalf("the strict map read back: %v", s)
	}
}
