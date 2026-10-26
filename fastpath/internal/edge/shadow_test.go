package edge

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// routingCase is a case of fastpath/testdata/routing_cases_v1.json: the
// request, and Python's answer to it, which serves here as the answer a
// fake Python gives.
type routingCase struct {
	Name          string          `json:"name"`
	At            string          `json:"at"`
	BYOKProviders []string        `json:"byok_providers"`
	Request       json.RawMessage `json:"request"`
	Expect        struct {
		Candidates    []string `json:"candidates"`
		ModelEstimate *int64   `json:"model_estimate"`
		StageDReason  string   `json:"stage_d_reason"`
		Error         *struct {
			Status  int    `json:"status"`
			Type    string `json:"type"`
			Message string `json:"message"`
			Param   string `json:"param"`
		} `json:"error"`
	} `json:"expect"`
}

func routingCases(t *testing.T) map[string]routingCase {
	t.Helper()
	var file struct {
		Cases []routingCase `json:"cases"`
	}
	if err := json.Unmarshal(testdata(t, "routing_cases_v1.json"), &file); err != nil {
		t.Fatal(err)
	}
	out := map[string]routingCase{}
	for _, c := range file.Cases {
		out[c.Name] = c
	}
	if len(out) < 190 {
		t.Fatalf("%d routing cases", len(out))
	}
	return out
}

// goldenHeld is the golden snapshot as the edge holds one. The golden
// manifest is the public test key's, which only a test verifies.
func goldenHeld(t *testing.T) *held {
	t.Helper()
	var m struct {
		Kid       string `json:"kid"`
		Purpose   string `json:"purpose"`
		Iss       string `json:"iss"`
		Aud       string `json:"aud"`
		PublicKey string `json:"public_key_b64url"`
		JWS       string `json:"jws"`
	}
	if err := json.Unmarshal(testdata(t, "routing_snapshot_v3.manifest.json"), &m); err != nil {
		t.Fatal(err)
	}
	public, err := base64.RawURLEncoding.DecodeString(m.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	g := loadGoldenBody(t)
	s, claims, err := routingsnapshot.Verify(m.JWS,
		[]routingsnapshot.TrustedKey{{Kid: m.Kid, Purpose: m.Purpose, PublicKey: public, Iss: m.Iss, Aud: m.Aud}}, g.raw,
		routingsnapshot.VerifyOptions{Now: g.compiledAt, ExpectedPlane: g.plane, ExpectedAud: m.Aud, AllowTestKey: true})
	if err != nil {
		t.Fatal(err)
	}
	return hold(s, claims)
}

// python is a fake Python answer: the authorize response shape
// (gateway.py, _gateway_authorize_response) with the fields the comparator
// reads, and others it does not, or the error envelope.
type python struct {
	status     int
	candidates []string
	estimate   int64
	stageD     string
	replay     bool
	errType    string
	message    string
	param      string
}

func pythonOf(c routingCase) python {
	if e := c.Expect.Error; e != nil {
		return python{status: e.Status, errType: e.Type, message: e.Message, param: e.Param}
	}
	return python{status: 200, candidates: c.Expect.Candidates, estimate: *c.Expect.ModelEstimate, stageD: c.Expect.StageDReason}
}

func (p python) body() []byte {
	if p.status != 200 {
		envelope := map[string]any{"code": p.status, "message": p.message, "type": p.errType, "source": "router"}
		if p.param != "" {
			envelope["param"] = p.param
		}
		out, _ := json.Marshal(map[string]any{"error": envelope})
		return out
	}
	route := []map[string]any{}
	for _, id := range p.candidates {
		route = append(route, map[string]any{"endpoint_id": id, "usage_type": "Credits", "byok_secret_ref": nil})
	}
	out, _ := json.Marshal(map[string]any{"data": map[string]any{
		"authorization_id": "auth-1", "workspace_id": "ws-1", "endpoint_id": p.candidates[0],
		"estimated_cost": float64(p.estimate) / 1e6, "estimated_cost_microdollars": p.estimate,
		"stage_d":           map[string]any{"eligible": p.stageD == "ok", "reason": p.stageD},
		"idempotent_replay": p.replay, "route_candidates": route, "tags": map[string]any{},
	}})
	return out
}

func caseAt(t *testing.T, c routingCase) time.Time {
	t.Helper()
	at, err := time.Parse(time.RFC3339, c.At)
	if err != nil {
		t.Fatal(err)
	}
	return at
}

// compareCase compares a request with Python's answer, both given, at the
// case's instant, as the edge's comparator would once Python answered.
func compareCase(t *testing.T, h *held, c routingCase, verdict Verdict, request []byte, py python) result {
	t.Helper()
	at := caseAt(t, c)
	call := &shadowCall{held: h, at: at, verdict: verdict, body: request}
	sh := newShadow(func(string, ...any) {}, time.Now, time.Minute)
	return sh.compare(comparison{call: call, status: py.status, response: py.body(), complete: true, done: at})
}

// withFields is a case's request with fields set (a nil value deletes one).
func withFields(t *testing.T, request json.RawMessage, fields map[string]any) []byte {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(request, &m); err != nil {
		t.Fatal(err)
	}
	for k, v := range fields {
		if v == nil {
			delete(m, k)
		} else {
			m[k] = v
		}
	}
	out, _ := json.Marshal(m)
	return out
}

// TestEveryRoutingCaseAgreesOrPasses: each golden case, with Python's answer
// as the fake Python's, agrees (the candidates and their order, the
// estimate, the Stage D reason, or the refusal's status, type, message and
// param) or is passed to Python for a reason with no request value in it;
// none differs. The counts are the evaluator's (routing-snapshot-v1.md,
// "What Go evaluates"): 175 answered and 71 refused alike, 19 passed.
func TestEveryRoutingCaseAgreesOrPasses(t *testing.T) {
	h := goldenHeld(t)
	classes := map[string]int{}
	byok := map[string]int{}
	for _, c := range routingCases(t) {
		r := compareCase(t, h, c, Verified, c.Request, pythonOf(c))
		classes[r.class]++
		byok[r.byok]++
		switch r.class {
		case ClassAgree:
		case ClassPassed:
			if strings.Contains(r.reason, `"`) {
				t.Errorf("%s: a reason with a request value in it: %s", c.Name, r.reason)
			}
		default:
			t.Errorf("%s: %+v", c.Name, r)
		}
	}
	if classes[ClassAgree] != 246 || classes[ClassPassed] != 19 {
		t.Fatalf("classes %v, want 246 agree and 19 passed", classes)
	}
	// BYOK presence decides some cases (Gemini's BYOK endpoint) and not others.
	if byok["determined"] == 0 || byok["consistent"] == 0 {
		t.Fatalf("BYOK presence %v", byok)
	}
}

// TestTheIdempotencyKeySourceChangesNoComparison: the enclave's
// idempotency_key_source (E4) is not a routing input, so a request carrying
// it, with any value, is compared exactly as the same request without it.
func TestTheIdempotencyKeySourceChangesNoComparison(t *testing.T) {
	h := goldenHeld(t)
	for _, c := range routingCases(t) {
		without := withFields(t, c.Request, map[string]any{})
		want := fmt.Sprintf("%+v", compareCase(t, h, c, Verified, without, pythonOf(c)))
		for _, source := range []any{"gateway", "client", "a-future-value", strings.Repeat("x", 99), 7} {
			with := withFields(t, c.Request, map[string]any{"idempotency_key_source": source})
			if got := fmt.Sprintf("%+v", compareCase(t, h, c, Verified, with, pythonOf(c))); got != want {
				t.Errorf("%s with idempotency_key_source %v: got %s, want %s", c.Name, source, got, want)
			}
		}
	}
}

// TestADifferenceIsCountedByItsField: one field changed in Python's answer
// differs in that field alone.
func TestADifferenceIsCountedByItsField(t *testing.T) {
	h := goldenHeld(t)
	cases := routingCases(t)
	glm := cases["z-ai/glm-5.3+none"]
	over := cases["context_one_over"]
	for _, c := range []struct {
		name   string
		base   routingCase
		change func(*python)
		fields []string
	}{
		{"the order", glm, func(p *python) { p.candidates = []string{p.candidates[1], p.candidates[0], p.candidates[2]} },
			[]string{"candidates"}},
		{"a candidate fewer", glm, func(p *python) { p.candidates = p.candidates[:2] }, []string{"candidates"}},
		// Lower than Go's: no app markup explains it.
		{"a lower estimate", glm, func(p *python) { p.estimate-- }, []string{"estimate_low"}},
		// More than doubled: past any markup of 10,000 basis points.
		{"a higher estimate", glm, func(p *python) { p.estimate = 2*p.estimate + 1 }, []string{"estimate_high"}},
		// pricing_kind needs pricing the edge passes to Python.
		{"the Stage D reason", glm, func(p *python) { p.stageD = "pricing_kind" }, []string{"stage_d_reason"}},
		{"Python refused", glm, func(p *python) {
			*p = python{status: 400, errType: "model_not_supported", message: "No route candidates match the requested provider filters"}
		}, []string{"outcome"}},
		{"Python answered", over, func(p *python) {
			*p = python{status: 200, candidates: []string{"deepseek/deepseek-v4-flash@deepseek/prepaid"}, estimate: 1, stageD: "route"}
		}, []string{"outcome"}},
		{"the refusal's message", over, func(p *python) { p.message += "!" }, []string{"refusal.message"}},
		{"the refusal's param", over, func(p *python) { p.param = "" }, []string{"refusal.param"}},
		{"the refusal's type", over, func(p *python) { p.errType = "bad_request" }, []string{"refusal.type"}},
	} {
		py := pythonOf(c.base)
		if r := compareCase(t, h, c.base, Verified, c.base.Request, py); r.class != ClassAgree {
			t.Fatalf("%s: the unchanged answer does not agree: %+v", c.name, r)
		}
		c.change(&py)
		r := compareCase(t, h, c.base, Verified, c.base.Request, py)
		if r.class != ClassDiffer || !slices.Equal(r.fields, c.fields) {
			t.Errorf("%s: got %+v, want a difference in %v", c.name, r, c.fields)
		}
	}
}

// TestWhatGoesToPythonSaysWhy: each input the evaluator assumes absent, each
// field not of the schema's type, Python's own refusals and replays, and an
// answer that changes while Python decides are passed with their reason.
func TestWhatGoesToPythonSaysWhy(t *testing.T) {
	h := goldenHeld(t)
	cases := routingCases(t)
	peak := cases["deepseek/deepseek-v4-flash@peak"]
	for _, c := range []struct {
		name    string
		fields  map[string]any
		python  func(*python)
		call    func(*shadowCall)
		done    time.Duration
		reason  string
		control bool
	}{
		{name: "an explicit null estimate", fields: map[string]any{"estimated_input_tokens": json.RawMessage("null")},
			reason: "an explicit null estimated_input_tokens"},
		{name: "an additional cost", fields: map[string]any{"additional_cost_reservation_microdollars": 5},
			reason: "an additional cost reservation"},
		{name: "a zero additional cost is none", fields: map[string]any{"additional_cost_reservation_microdollars": 0},
			control: true},
		{name: "a receipt", fields: map[string]any{"inference_receipt": true}, reason: "an inference receipt's fee"},
		{name: "a video resolution", fields: map[string]any{"video_resolution": "720p"},
			reason: "a video resolution or allowed providers"},
		{name: "video providers", call: func(c *shadowCall) { c.videoProviders = true },
			reason: "a video resolution or allowed providers"},
		{name: "speech usage", fields: map[string]any{"speech_input_characters": 10}, reason: "speech or transcription usage"},
		{name: "a custom model", fields: map[string]any{"model": "TR-Custom-Model/abc"}, reason: "a custom or user-provided model"},
		{name: "a user model in models", fields: map[string]any{"models": []string{" tr-user-model/x"}},
			reason: "a custom or user-provided model"},
		{name: "the partner's model", fields: map[string]any{"model": "parasail/liberty-2.0"}, reason: "a partner billing mode"},
		{name: "the partner's key", fields: map[string]any{"idempotency_key": partnerIdempotency + "1"},
			reason: "a partner billing mode"},
		{name: "the partner's key header", call: func(c *shadowCall) { c.partnerKey = true }, reason: "a partner billing mode"},
		{name: "a string count", fields: map[string]any{"max_tokens": "5"}, reason: "a field that is not of the authorize schema's type"},
		{name: "a fractional count", fields: map[string]any{"estimated_input_tokens": 1000.5},
			reason: "a field that is not of the authorize schema's type"},
		{name: "a null in models", fields: map[string]any{"models": []any{nil}},
			reason: "a field that is not of the authorize schema's type"},
		{name: "no model", fields: map[string]any{"model": nil}, reason: "model is missing or not a string"},
		{name: "a meta model", fields: map[string]any{"model": "trustedrouter/auto"},
			reason: "requested model <value> (meta model, pool or alias)"},
		{name: "an unknown provider field", fields: map[string]any{"provider": map[string]any{"nope-field": 1}},
			reason: "provider.<field>"},
		{name: "Python's 402", python: func(p *python) {
			*p = python{status: 402, errType: "insufficient_credits", message: "Insufficient credits"}
		}, reason: "python refused outside routing: 402 insufficient_credits"},
		{name: "Python's other 400", python: func(p *python) {
			*p = python{status: 400, errType: "invalid_tags", message: "tags"}
		}, reason: "python refused outside routing: 400 invalid_tags"},
		{name: "a replay", python: func(p *python) { p.replay = true }, reason: "an idempotent replay"},
		// 02:00 is DeepSeek's peak; by 04:30 the off-peak price is back.
		{name: "a price epoch while Python decided", done: 150 * time.Minute,
			reason: "the snapshot's answer changed while python decided"},
		{name: "the same epoch", done: time.Second, control: true},
	} {
		request := peak.Request
		if c.fields != nil {
			request = withFields(t, peak.Request, c.fields)
		}
		py := pythonOf(peak)
		if c.python != nil {
			c.python(&py)
		}
		at := caseAt(t, peak)
		call := &shadowCall{held: h, at: at, verdict: Verified, body: request}
		if c.call != nil {
			c.call(call)
		}
		sh := newShadow(func(string, ...any) {}, time.Now, time.Minute)
		r := sh.compare(comparison{call: call, status: py.status, response: py.body(), complete: true, done: at.Add(c.done)})
		switch {
		case c.control && r.class != ClassAgree:
			t.Errorf("%s: the control does not agree: %+v", c.name, r)
		case !c.control && (r.class != ClassPassed || r.reason != c.reason):
			t.Errorf("%s: got %+v, want passed for %q", c.name, r, c.reason)
		}
	}
}

// TestPythonsAffinityPromotesOneEndpointWithinItsModel: with a cache
// affinity key, Python's warm endpoint first among its model's is the
// affinity class; the same order without the key, or any other order,
// differs.
func TestPythonsAffinityPromotesOneEndpointWithinItsModel(t *testing.T) {
	h := goldenHeld(t)
	cases := routingCases(t)
	key := map[string]any{"cache_affinity_key": strings.Repeat("ab", 32)}
	for _, c := range []struct {
		name    string
		base    string
		order   func([]string) []string
		key     bool
		class   string
		control bool
	}{
		// GLM 5.3's five hosts; fallback_list is V4 Flash, GLM 5.3's five
		// hosts, then Gemini.
		{name: "the last to the front", base: "z-ai/glm-5.3+none", key: true, class: ClassAffinity,
			order: func(c []string) []string { return []string{c[4], c[0], c[1], c[2], c[3]} }},
		{name: "within the second model", base: "fallback_list", key: true, class: ClassAffinity,
			order: func(c []string) []string { return []string{c[0], c[5], c[1], c[2], c[3], c[4], c[6]} }},
		{name: "without the key", base: "z-ai/glm-5.3+none", class: ClassDiffer,
			order: func(c []string) []string { return []string{c[4], c[0], c[1], c[2], c[3]} }},
		{name: "two moved", base: "z-ai/glm-5.3+none", key: true, class: ClassDiffer,
			order: func(c []string) []string { return []string{c[1], c[2], c[0], c[3], c[4]} }},
		{name: "into another model's run", base: "fallback_list", key: true, class: ClassDiffer,
			order: func(c []string) []string { return []string{c[0], c[6], c[1], c[2], c[3], c[4], c[5]} }},
		{name: "unchanged", base: "z-ai/glm-5.3+none", key: true, class: ClassAgree,
			order: func(c []string) []string { return c }},
	} {
		base := cases[c.base]
		request := []byte(base.Request)
		if c.key {
			request = withFields(t, base.Request, key)
		}
		py := pythonOf(base)
		py.candidates = c.order(slices.Clone(py.candidates))
		r := compareCase(t, h, base, Verified, request, py)
		if r.class != c.class {
			t.Errorf("%s: got %+v, want %s", c.name, r, c.class)
		}
	}
}

// TestPythonsWarmEndpointWithFallbacksOff: with fallbacks off Python
// re-ranks and keeps one candidate, so its one endpoint is the warm one.
// Under a cache affinity key, an endpoint of Go's model that Go admits with
// fallbacks on is its own class, affinity_warm, never agree; its estimate is
// checked where Go pinned to its provider answers exactly that endpoint, and
// a mismatch is counted within the class. Without the key, another model's
// endpoint (also one the request's models list names), under
// provider.order, or an endpoint Go does not admit, it differs.
func TestPythonsWarmEndpointWithFallbacksOff(t *testing.T) {
	h := goldenHeld(t)
	cases := routingCases(t)
	glm, list := cases["z-ai/glm-5.3+none"], cases["fallback_list"]
	key := strings.Repeat("ab", 32)
	off := map[string]any{"allow_fallbacks": false}
	ordered := map[string]any{"allow_fallbacks": false, "order": []string{"wafer", "fireworks"}}
	ignored := map[string]any{"allow_fallbacks": false, "ignore": []string{"fireworks"}}
	// The warm endpoints' prices, as Go prices one provider's endpoint.
	price := func(base routingCase, provider string) int64 {
		req, _, why := requestOf(withFields(t, base.Request, map[string]any{"provider": map[string]any{"only": []string{provider},
			"allow_fallbacks": false}}), &shadowCall{})
		if why != "" {
			t.Fatal(why)
		}
		req.At = caseAt(t, base)
		r := h.snapshot.Evaluate(req)
		if r.Outcome != routingsnapshot.Answered || len(r.Candidates) != 1 {
			t.Fatalf("pricing %s: %+v", provider, r)
		}
		return r.ModelEstimate
	}
	fireworks, together, wafer := price(glm, "fireworks"), price(glm, "together"), price(glm, "wafer")
	if fireworks == wafer && together == wafer {
		t.Fatal("the warm endpoints' prices do not tell them from Go's first")
	}
	for _, c := range []struct {
		name     string
		base     routingCase
		fields   map[string]any
		endpoint string
		estimate int64
		class    string
		want     []string
		checked  string
	}{
		{"the warm endpoint", glm, map[string]any{"cache_affinity_key": key, "provider": off},
			"z-ai/glm-5.3@fireworks/prepaid", fireworks, ClassAffinityWarm, nil, "checked"},
		{"the warm endpoint, top-level", glm, map[string]any{"cache_affinity_key": key, "allow_fallbacks": false},
			"z-ai/glm-5.3@together/prepaid", together, ClassAffinityWarm, nil, "checked"},
		{"the warm endpoint overcharged", glm, map[string]any{"cache_affinity_key": key, "provider": off},
			"z-ai/glm-5.3@fireworks/prepaid", fireworks + 1, ClassAffinityWarm, []string{"affinity_warm.estimate_high"}, "checked"},
		{"the warm endpoint undercharged", glm, map[string]any{"cache_affinity_key": key, "provider": off},
			"z-ai/glm-5.3@fireworks/prepaid", fireworks - 1, ClassAffinityWarm, []string{"affinity_warm.estimate_low"}, "checked"},
		{"Go's own", glm, map[string]any{"cache_affinity_key": key, "provider": off}, "z-ai/glm-5.3@wafer/prepaid", wafer,
			ClassAgree, nil, ""},
		{"no key", glm, map[string]any{"provider": off}, "z-ai/glm-5.3@fireworks/prepaid", 1, ClassDiffer,
			[]string{"candidates", "estimate_low"}, ""},
		{"another model", glm, map[string]any{"cache_affinity_key": key, "provider": off},
			"deepseek/deepseek-v4-flash@deepseek/prepaid", 1, ClassDiffer, []string{"candidates", "estimate_low"}, ""},
		// Go admits glm's endpoints for this request with fallbacks on (its
		// models list), but Python with fallbacks off reads model alone.
		{"a model from the models list", list, map[string]any{"cache_affinity_key": key, "provider": off},
			"z-ai/glm-5.3@wafer/prepaid", 1, ClassDiffer, []string{"candidates", "estimate_low"}, ""},
		{"under an order", glm, map[string]any{"cache_affinity_key": key, "provider": ordered}, "z-ai/glm-5.3@fireworks/prepaid",
			1, ClassDiffer, []string{"candidates", "estimate_low"}, ""},
		{"an endpoint Go does not admit", glm, map[string]any{"cache_affinity_key": key, "provider": ignored},
			"z-ai/glm-5.3@fireworks/prepaid", 1, ClassDiffer, []string{"candidates", "estimate_low"}, ""},
	} {
		request := withFields(t, c.base.Request, c.fields)
		py := pythonOf(c.base)
		py.candidates, py.estimate = []string{c.endpoint}, c.estimate
		r := compareCase(t, h, c.base, Verified, request, py)
		if r.class != c.class || !slices.Equal(r.fields, c.want) || r.warmEstimate != c.checked {
			t.Errorf("%s: got %+v, want %s %v %q", c.name, r, c.class, c.want, c.checked)
		}
	}
}

// TestAWarmEndpointsStageDIsReadOverPythonsCandidate: Python's warm
// endpoint is Gemini's BYOK one where Go's first is its prepaid one, so
// Stage D is mixed_usage_type over Python's candidate, a reason Go's could
// not give; ok, which no BYOK candidate gives, differs within the class.
// Pinned to the provider Go answers the prepaid endpoint, so the estimate
// is not checked.
func TestAWarmEndpointsStageDIsReadOverPythonsCandidate(t *testing.T) {
	h := goldenHeld(t)
	gemini := routingCases(t)["google/gemini-3.1-pro-preview@off_peak"]
	request := withFields(t, gemini.Request, map[string]any{"cache_affinity_key": strings.Repeat("cd", 32),
		"allow_fallbacks": false})
	py := pythonOf(gemini)
	py.candidates, py.stageD = []string{"google/gemini-3.1-pro-preview@google-ai-studio/byok"}, "mixed_usage_type"
	r := compareCase(t, h, gemini, Verified, request, py)
	if r.class != ClassAffinityWarm || len(r.fields) != 0 || r.stageD != "consistent" || r.warmEstimate != "unchecked" {
		t.Fatalf("the BYOK warm endpoint: %+v", r)
	}
	py.stageD = "ok"
	r = compareCase(t, h, gemini, Verified, request, py)
	if r.class != ClassAffinityWarm || !slices.Equal(r.fields, []string{"affinity_warm.stage_d_reason"}) {
		t.Fatalf("ok over the BYOK warm endpoint: %+v", r)
	}
}

// TestAnEstimateMismatchIsAlwaysADifference: a higher estimate is
// estimate_high with the smallest app markup that explains it, or none, for
// the histogram to read; a lower one is estimate_low. None of it is agree:
// below 10,000 microdollars every higher estimate up to four times Go's has
// a markup that explains it, a 1 microdollar overcharge included.
func TestAnEstimateMismatchIsAlwaysADifference(t *testing.T) {
	h := goldenHeld(t)
	glm := routingCases(t)["z-ai/glm-5.3+none"]
	base := *glm.Expect.ModelEstimate
	for _, c := range []struct {
		name     string
		estimate int64
		field    string
		markup   string
	}{
		{"a 1 microdollar overcharge", base + 1, "estimate_high", markupBPS(base, base+1)},
		{"a 5% markup", base + base*500/10_000, "estimate_high", "500"},
		{"a 300% markup", 4 * base, "estimate_high", "30000"},
		{"past any markup", 4*base + 1, "estimate_high", "none"},
		{"a lower estimate", base - 1, "estimate_low", ""},
	} {
		py := pythonOf(glm)
		py.estimate = c.estimate
		r := compareCase(t, h, glm, Verified, glm.Request, py)
		if r.class != ClassDiffer || !slices.Equal(r.fields, []string{c.field}) || r.markup != c.markup {
			t.Errorf("%s: got %+v, want %s with markup %q", c.name, r, c.field, c.markup)
		}
	}
	if got := markupBPS(base, base+1); got == "none" || got == "" {
		t.Fatalf("the 1 microdollar overcharge has no markup recorded: %q", got)
	}
	for _, c := range []struct {
		goEstimate, py int64
		want           string
	}{{2640, 2772, "500"}, {2640, 2641, "4"}, {1, 2, "10000"}, {1, 4, "30000"}, {1, 5, "none"}, {0, 5, "none"},
		{30_000, 30_003, "1"}, {30_000, 30_001, "none"}, {30_000, 120_000, "30000"}, {30_000, 120_001, "none"},
		{2640, 2640, "none"}, {math.MaxInt64 / 20_000, math.MaxInt64/20_000 + 1, "none"},
		// The overflow guard is for 30,000 basis points: the largest Go
		// estimate it takes, and the next.
		{math.MaxInt64 / 30_000, 4 * (math.MaxInt64 / 30_000), "30000"},
		// Past it nothing is computed, even a markup of one basis point
		// that would not overflow.
		{math.MaxInt64/30_000 + 1, math.MaxInt64/30_000 + 1 + (math.MaxInt64/30_000+1)/10_000, "none"}} {
		if got := markupBPS(c.goEstimate, c.py); got != c.want {
			t.Errorf("markupBPS(%d, %d) = %q, want %q", c.goEstimate, c.py, got, c.want)
		}
	}
	// The histogram: a period's estimate_high counts by markup, in the line.
	sh := newShadow(func(string, ...any) {}, time.Now, time.Minute)
	var lines []string
	sh.logf = func(f string, a ...any) { lines = append(lines, fmt.Sprintf(f, a...)) }
	for _, bps := range []string{"500", "500", "none"} {
		sh.record("d", result{class: ClassDiffer, fields: []string{"estimate_high"}, markup: bps})
	}
	sh.flush()
	if text := strings.Join(lines, "\n"); !strings.Contains(text, `estimate_high_markup_bps={"500":2,"none":1}`) ||
		!strings.Contains(text, `"estimate_high":3`) || !strings.Contains(text, "agree=0 differ=3") {
		t.Fatalf("the histogram is not logged:\n%s", text)
	}
}

// TestBYOKPresenceIsWhatPythonsAnswerImplies: the workspace's BYOK presence
// is not in the body. When it cannot change Go's answer it is determined;
// when it can, Go answers with the presence Python's candidates imply, and
// an order no presence explains differs.
func TestBYOKPresenceIsWhatPythonsAnswerImplies(t *testing.T) {
	h := goldenHeld(t)
	cases := routingCases(t)
	deepseek := cases["deepseek/deepseek-v4-flash@peak"]
	if r := compareCase(t, h, deepseek, Verified, deepseek.Request, pythonOf(deepseek)); r.class != ClassAgree ||
		r.byok != "determined" {
		t.Fatalf("a model with no BYOK endpoint: %+v", r)
	}
	present := cases["google/gemini-3.1-pro-preview+byok:google-ai-studio+null"]
	if r := compareCase(t, h, present, Verified, present.Request, pythonOf(present)); r.class != ClassAgree ||
		r.byok != "consistent" {
		t.Fatalf("Gemini with its BYOK key present: %+v", r)
	}
	absent := cases["google/gemini-3.1-pro-preview+byok:deepseek,wafer+null"]
	if r := compareCase(t, h, absent, Verified, absent.Request, pythonOf(absent)); r.class != ClassAgree ||
		r.byok != "consistent" {
		t.Fatalf("Gemini with its BYOK key absent: %+v", r)
	}
	swapped := pythonOf(present)
	swapped.candidates = []string{swapped.candidates[1], swapped.candidates[0]}
	if r := compareCase(t, h, present, Verified, present.Request, swapped); r.class != ClassDiffer ||
		!slices.Equal(r.fields, []string{"candidates"}) {
		t.Fatalf("an order no BYOK presence explains: %+v", r)
	}
}

// TestStageDIsComparedOverWhatTheEdgeCannotKnow: the reason is determined
// when no unknown input changes it, consistent when Python's is one an
// unknown input gives, and differs when none gives it. A request with no
// boot signature is one Python cannot accept the boot of.
func TestStageDIsComparedOverWhatTheEdgeCannotKnow(t *testing.T) {
	h := goldenHeld(t)
	cases := routingCases(t)
	ok := cases["deepseek/deepseek-v4-flash@peak"]
	if r := compareCase(t, h, ok, Verified, ok.Request, pythonOf(ok)); r.stageD != "consistent" || r.class != ClassAgree {
		t.Fatalf("ok, with the key, the workspace and the boot unknown: %+v", r)
	}
	unsigned := pythonOf(ok)
	if r := compareCase(t, h, ok, Absent, ok.Request, unsigned); r.class != ClassDiffer ||
		!slices.Equal(r.fields, []string{"stage_d_reason"}) {
		t.Fatalf("ok for a request with no boot signature: %+v", r)
	}
	unsigned.stageD = "boot_not_accepted"
	if r := compareCase(t, h, ok, Absent, ok.Request, unsigned); r.class != ClassAgree || r.stageD != "consistent" {
		t.Fatalf("boot_not_accepted for a request with no boot signature: %+v", r)
	}
	// Eligibility off in the snapshot's settings decides the reason alone.
	off := *h
	snapshot := *h.snapshot
	snapshot.Settings.StageD.EligibilityEnabled = false
	off.snapshot = &snapshot
	disabled := pythonOf(ok)
	disabled.stageD = "stage_d_disabled"
	if r := compareCase(t, &off, ok, Verified, ok.Request, disabled); r.class != ClassAgree || r.stageD != "determined" {
		t.Fatalf("stage_d_disabled with eligibility off: %+v", r)
	}
	if r := compareCase(t, &off, ok, Verified, ok.Request, pythonOf(ok)); r.class != ClassDiffer || r.stageD != "differ" {
		t.Fatalf("ok with eligibility off: %+v", r)
	}
	// The abliterate estimated-usage reason is outside v1: not compared.
	abliterate := pythonOf(ok)
	abliterate.stageD = "abliterate_estimated_usage"
	if r := compareCase(t, h, ok, Verified, ok.Request, abliterate); r.class != ClassAgree || r.stageD != "" {
		t.Fatalf("the estimated-usage reason: %+v", r)
	}
}

// shadowEdge is a snapshot edge with the golden snapshot installed through
// its poll and Python answering authorize as the test says.
func shadowEdge(t *testing.T) *snapshotEdge {
	t.Helper()
	h := newSnapshotEdge(t, true, nil)
	h.serveGolden(t, h.clock.now().Unix()+60)
	h.poll(t)
	if h.edge.poller.snapshot() == nil {
		t.Fatal("no snapshot installed")
	}
	return h
}

// drain compares what the edge queued, n comparisons, as its Run would.
func (h *snapshotEdge) drain(t *testing.T, n int) {
	t.Helper()
	sh := h.edge.shadow
	for i := 0; i < n; i++ {
		select {
		case c := <-sh.queue:
			sh.process(c)
		case <-time.After(5 * time.Second):
			t.Fatalf("%d of %d comparisons queued", i, n)
		}
	}
}

func (h *snapshotEdge) tally(t *testing.T) *tally {
	t.Helper()
	sh := h.edge.shadow
	sh.mu.Lock()
	defer sh.mu.Unlock()
	return sh.tallies[h.edge.poller.snapshot().claims.SHA256]
}

// TestTheEnclaveGetsPythonsBytesWithTheComparatorOn: Python's status,
// headers and bytes, here a gzip answer the comparator must decode, reach
// the enclave as Python sent them, and Python gets the request's bytes as
// the enclave sent them, signed and unsigned; the comparator saw both.
func TestTheEnclaveGetsPythonsBytesWithTheComparatorOn(t *testing.T) {
	h := shadowEdge(t)
	c := routingCases(t)["deepseek/deepseek-v4-flash@off_peak"]
	h.clock.set(caseAt(t, c))
	// Unsigned, or signed and not verified: Python does not accept the boot.
	py := pythonOf(c)
	py.stageD = "boot_not_accepted"
	var zipped bytes.Buffer
	zw := gzip.NewWriter(&zipped)
	_, _ = zw.Write(py.body())
	_ = zw.Close()
	answer := zipped.Bytes()
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Set("X-Answer", "python")
		w.Header().Set("Content-Length", strconv.Itoa(len(answer)))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(answer)
	})
	request := string(c.Request)
	for _, headers := range []map[string]string{nil, {BootHeader: "kid=boot-1,sig=" + strings.Repeat("A", 86)}} {
		resp := h.post(t, request, headers)
		got, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != http.StatusOK || !bytes.Equal(got, answer) || resp.Header.Get("Content-Encoding") != "gzip" ||
			resp.Header.Get("X-Answer") != "python" || resp.Header.Get("Content-Length") != strconv.Itoa(len(answer)) {
			t.Fatalf("the enclave got %d %v %q, not Python's answer", resp.StatusCode, resp.Header, got)
		}
		h.python.mu.Lock()
		sent := h.python.bodies[len(h.python.bodies)-1]
		h.python.mu.Unlock()
		if string(sent) != request {
			t.Fatalf("Python got %q, not the enclave's request", sent)
		}
	}
	h.drain(t, 2)
	if got := h.tally(t); got == nil || got.classes[ClassAgree] != 2 {
		t.Fatalf("the comparator did not see both answers agree: %+v", got)
	}
}

// TestNoSnapshotIsCountedAndPassesUntouched: with the shadow on and no
// snapshot held, the authorize is counted as no_snapshot and passes.
func TestNoSnapshotIsCountedAndPassesUntouched(t *testing.T) {
	h := newSnapshotEdge(t, true, nil)
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, `{"data":{}}`) })
	resp := h.post(t, `{"model":"deepseek/deepseek-v4-flash"}`, nil)
	got, _ := io.ReadAll(resp.Body)
	if string(got) != `{"data":{}}` {
		t.Fatalf("the enclave got %q", got)
	}
	h.edge.shadow.flush()
	if !strings.Contains(h.logs.text(), "fastpath edge shadow: no_snapshot=1 dropped=0") {
		t.Fatalf("no_snapshot is not counted:\n%s", h.logs.text())
	}
	// Only authorize is compared: another path is not counted.
	h.edge.shadow.flush()
	before := h.logs.count()
	req, _ := http.NewRequest(http.MethodPost, h.server.URL+"/internal/gateway/settle", strings.NewReader("{}"))
	req.Header.Set(TokenHeader, token)
	if resp, err := plainClient.Do(req); err == nil {
		resp.Body.Close()
	}
	h.edge.shadow.flush()
	if h.logs.count() != before {
		t.Fatalf("settle was counted:\n%s", h.logs.text())
	}
}

// TestAFullComparatorDropsAndNeverWaits: with the comparator's queue full,
// authorizes pass at once and the comparisons are counted dropped.
func TestAFullComparatorDropsAndNeverWaits(t *testing.T) {
	h := shadowEdge(t)
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = io.WriteString(w, `{"error":{"code":402,"message":"m","type":"insufficient_credits"}}`)
	})
	started := time.Now()
	for i := 0; i < shadowQueue+3; i++ {
		resp := h.post(t, `{"model":"deepseek/deepseek-v4-flash"}`, nil)
		// Read through and closed, so the client reuses its connection.
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusPaymentRequired {
			t.Fatalf("authorize %d: %d", i, resp.StatusCode)
		}
	}
	if elapsed := time.Since(started); elapsed > 10*time.Second {
		t.Fatalf("authorizes waited on the comparator: %v", elapsed)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		dropped := h.edge.shadow.dropped.Load()
		if dropped == 3 && len(h.edge.shadow.queue) == shadowQueue {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("dropped %d with %d queued", dropped, len(h.edge.shadow.queue))
		}
		time.Sleep(time.Millisecond)
	}
	// The dropped comparisons gave their copies back: what the budget holds
	// is the queued copies', and nothing once they are compared.
	sh := h.edge.shadow
	var queued []comparison
	var held int64
	for len(sh.queue) > 0 {
		c := <-sh.queue
		queued = append(queued, c)
		held += heldOf(c)
	}
	if got := sh.bytes.Load(); held == 0 || got != held {
		t.Fatalf("the budget holds %d, the queued copies %d", got, held)
	}
	for _, c := range queued {
		sh.process(c)
	}
	if got := sh.bytes.Load(); got != 0 {
		t.Fatalf("%d bytes of the budget still held after draining", got)
	}
}

// heldOf is what a comparison's copies hold of the budget.
func heldOf(c comparison) int64 {
	held := func(t *tap) int64 {
		if t == nil {
			return 0
		}
		t.mu.Lock()
		defer t.mu.Unlock()
		return t.held
	}
	return c.call.bodyHeld + held(c.call.tap) + held(c.answer)
}

// TestACopyCutShortByTheBudgetGivesItBack: a request streamed in two chunks,
// the budget room for the first and not the second, is copied until the
// second, then not at all: Python gets both, the request is counted as
// passed, and the first chunk's bytes go back to the budget.
func TestACopyCutShortByTheBudgetGivesItBack(t *testing.T) {
	h := shadowEdge(t)
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusPaymentRequired) })
	first, second := `{"model":"deepseek/deepseek-v4-flash",`, `"estimated_input_tokens":10}`
	sh := h.edge.shadow
	sh.budget = int64(len(first)) + 4
	body, writer := io.Pipe()
	req, err := http.NewRequest(http.MethodPost, h.server.URL+"/internal/gateway/authorize", body)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(TokenHeader, token)
	answered := make(chan *http.Response, 1)
	go func() {
		resp, err := plainClient.Do(req)
		if err != nil {
			answered <- nil
			return
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		answered <- resp
	}()
	_, _ = io.WriteString(writer, first)
	deadline := time.Now().Add(5 * time.Second)
	for sh.bytes.Load() != int64(len(first)) {
		if time.Now().After(deadline) {
			t.Fatalf("the first chunk was not copied: %d held", sh.bytes.Load())
		}
		time.Sleep(time.Millisecond)
	}
	_, _ = io.WriteString(writer, second)
	_ = writer.Close()
	if resp := <-answered; resp == nil || resp.StatusCode != http.StatusPaymentRequired {
		t.Fatalf("the answer: %+v", resp)
	}
	h.drain(t, 1)
	h.python.mu.Lock()
	sent := string(h.python.bodies[len(h.python.bodies)-1])
	h.python.mu.Unlock()
	if sent != first+second {
		t.Fatalf("Python got %q", sent)
	}
	if got := h.tally(t); got == nil || got.reasons["the comparator's byte budget is spent"] != 1 {
		t.Fatalf("the counts: %+v", got)
	}
	if got := sh.bytes.Load(); got != 0 {
		t.Fatalf("%d bytes of the budget still held", got)
	}
}

// TestTheLogsCarryNoRequestOrAnswerContent: the poll's and the comparator's
// lines, a difference's included, carry counts, field names, redacted
// reasons and the digest, never a value from a body, a header, or the
// token. The positive controls: the evaluator's own reason holds the
// request's model, which the line redacts, and the check finds a marker
// where one is.
func TestTheLogsCarryNoRequestOrAnswerContent(t *testing.T) {
	h := shadowEdge(t)
	cases := routingCases(t)
	glm := cases["z-ai/glm-5.3+none"]
	h.clock.set(caseAt(t, glm))
	markers := []string{"MARKERKEY", "MARKERUSER", "MARKERMODEL", "MARKERMSG", "MARKERTAG", "MARKERFIELD", token}
	leaks := func(text string) []string {
		var found []string
		for _, m := range markers {
			if strings.Contains(text, m) {
				found = append(found, m)
			}
		}
		return found
	}
	if got := leaks("x MARKERMODEL y"); len(got) != 1 {
		t.Fatalf("the check does not find a marker: %v", got)
	}
	extra := map[string]any{"api_key_lookup_hash": "MARKERKEY", "user": "MARKERUSER", "tags": map[string]any{"t": "MARKERTAG"}}
	// The requests carry no boot signature, so Python does not accept the
	// boot.
	agrees := pythonOf(glm)
	agrees.stageD = "boot_not_accepted"
	differs := agrees
	differs.candidates = []string{differs.candidates[1], differs.candidates[0], differs.candidates[2]}
	for _, c := range []struct {
		request []byte
		answer  python
	}{
		{withFields(t, glm.Request, extra), agrees},
		{withFields(t, glm.Request, extra), differs},
		{withFields(t, glm.Request, map[string]any{"model": "trustedrouter/MARKERMODEL"}), agrees},
		{withFields(t, glm.Request, map[string]any{"provider": map[string]any{"MARKERFIELD": 1}}), agrees},
		{withFields(t, glm.Request, extra), python{status: 400, errType: "bad_request", message: "MARKERMSG"}},
		{withFields(t, glm.Request, extra), python{status: 402, errType: "insufficient_credits", message: "MARKERMSG"}},
	} {
		answer := c.answer.body()
		h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(c.answer.status)
			_, _ = w.Write(answer)
		})
		h.post(t, string(c.request), map[string]string{"Idempotency-Key": "MARKERKEY"})
	}
	h.drain(t, 6)
	h.edge.shadow.flush()
	text := h.logs.text()
	if found := leaks(text); len(found) > 0 {
		t.Fatalf("the logs carry %v:\n%s", found, text)
	}
	digest := h.edge.poller.snapshot().claims.SHA256
	for _, want := range []string{"fastpath edge shadow differs: snapshot=" + digest + " class=differ fields=candidates",
		"fastpath edge shadow: snapshot=" + digest + " agree=1 differ=2 affinity=0 affinity_warm=0 passed_to_python=3",
		`"router model <value>":1`, `"provider.<field>":1`, `"outcome":1`, `"candidates":1`,
		`"python refused outside routing: 402 insufficient_credits":1`, "outcome=installed"} {
		if !strings.Contains(text, want) {
			t.Fatalf("the logs lack %q:\n%s", want, text)
		}
	}
	// The evaluator's own reason names the model; only the redacted one is
	// logged.
	var model routingsnapshot.Request
	model.Model, model.At = "trustedrouter/MARKERMODEL", caseAt(t, glm)
	if raw := h.edge.poller.snapshot().snapshot.Evaluate(model).Reason; !strings.Contains(raw, "MARKERMODEL") {
		t.Fatalf("the positive control's reason does not name the model: %q", raw)
	}
}

// TestWhatTheEdgeCannotCompareIsCountedAsPassed: a body past the edge's
// bound passes on unread, and a Python not reached answers the edge's 502;
// each is counted with its reason.
func TestWhatTheEdgeCannotCompareIsCountedAsPassed(t *testing.T) {
	h := shadowEdge(t)
	h.python.onAuthorize(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(http.StatusPaymentRequired)
	})
	big := `{"model":"m","pad":"` + strings.Repeat("x", maxBody) + `"}`
	resp := h.post(t, big, map[string]string{BootHeader: "kid=boot-1,sig=" + strings.Repeat("A", 86)})
	if resp.StatusCode != http.StatusPaymentRequired {
		t.Fatalf("an oversized body: %d", resp.StatusCode)
	}
	h.drain(t, 1)
	h.python.server.Close()
	if resp := h.post(t, `{"model":"m"}`, nil); resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("Python away: %d", resp.StatusCode)
	}
	h.drain(t, 1)
	got := h.tally(t)
	if got == nil || got.reasons["the request body is over the comparator's bound"] != 1 ||
		got.reasons["python was not reached"] != 1 || got.classes[ClassPassed] != 2 {
		t.Fatalf("the counts: %+v", got)
	}
}

// TestABodyPastTheBoundIsNotKeptOnTheBootPath: the edge reads a signed
// request's body whole; past the comparator's 1 MiB it is counted as passed
// at once and not kept, Python's answer is not copied, and the request
// still reaches Python whole.
func TestABodyPastTheBoundIsNotKeptOnTheBootPath(t *testing.T) {
	h := shadowEdge(t)
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusPaymentRequired) })
	big := `{"model":"m","pad":"` + strings.Repeat("x", maxShadowRequest) + `"}`
	resp := h.post(t, big, map[string]string{BootHeader: "kid=boot-1,sig=" + strings.Repeat("A", 86)})
	h.python.mu.Lock()
	sent := h.python.bodies[len(h.python.bodies)-1]
	h.python.mu.Unlock()
	if resp.StatusCode != http.StatusPaymentRequired || string(sent) != big {
		t.Fatalf("the request: %d, Python read %d of %d", resp.StatusCode, len(sent), len(big))
	}
	// Counted before Python answered, holding none of the budget, and
	// Python's answer not copied.
	select {
	case c := <-h.edge.shadow.queue:
		if c.passed != "the request body is over the comparator's bound" || c.answer != nil || c.call.body != nil {
			t.Fatalf("the comparison: passed %q, answer copied %v, body kept %v", c.passed, c.answer != nil, c.call.body != nil)
		}
		if held := h.edge.shadow.bytes.Load(); held != 0 {
			t.Fatalf("%d bytes of the budget held for a body past the bound", held)
		}
		h.edge.shadow.process(c)
	case <-time.After(5 * time.Second):
		t.Fatal("nothing queued")
	}
	if n := len(h.edge.shadow.queue); n != 0 {
		t.Fatalf("Python's answer was copied too: %d queued", n)
	}
	if got := h.tally(t); got == nil || got.reasons["the request body is over the comparator's bound"] != 1 {
		t.Fatalf("the counts: %+v", got)
	}
	if held := h.edge.shadow.bytes.Load(); held != 0 {
		t.Fatalf("%d bytes of the budget still held", held)
	}
}

// TestTheComparatorsCopiesKeepWithinItsBudget: past the byte budget a
// request is not copied and is counted as passed, read and answered as
// ever; every copy's bytes are given back once compared, so the budget
// does not leak.
func TestTheComparatorsCopiesKeepWithinItsBudget(t *testing.T) {
	h := shadowEdge(t)
	c := routingCases(t)["deepseek/deepseek-v4-flash@off_peak"]
	h.clock.set(caseAt(t, c))
	py := pythonOf(c)
	py.stageD = "boot_not_accepted"
	answer := py.body()
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(answer) })
	signed := map[string]string{BootHeader: "kid=boot-1,sig=" + strings.Repeat("A", 86)}
	for _, headers := range []map[string]string{nil, signed} {
		resp := h.post(t, string(c.Request), headers)
		if b, _ := io.ReadAll(resp.Body); !bytes.Equal(b, answer) {
			t.Fatalf("the answer: %q", b)
		}
	}
	h.drain(t, 2)
	if held := h.edge.shadow.bytes.Load(); held != 0 {
		t.Fatalf("%d bytes of the budget still held after comparing", held)
	}
	// A budget smaller than a request: the request is not copied, on either
	// path, nor is an answer.
	h.edge.shadow.budget = int64(len(c.Request)) - 1
	for _, headers := range []map[string]string{nil, signed} {
		resp := h.post(t, string(c.Request), headers)
		if b, _ := io.ReadAll(resp.Body); !bytes.Equal(b, answer) {
			t.Fatalf("the answer under a spent budget: %q", b)
		}
	}
	h.drain(t, 2)
	got := h.tally(t)
	if got == nil || got.classes[ClassAgree] != 2 || got.reasons["the comparator's byte budget is spent"] != 2 {
		t.Fatalf("the counts: %+v %+v", got.classes, got.reasons)
	}
	if held := h.edge.shadow.bytes.Load(); held != 0 {
		t.Fatalf("%d bytes of the budget still held", held)
	}
}

// TestAStreamedAnswerIsNeverHeldBack: Python's chunked answer reaches the
// enclave as it is written, its first bytes before the rest exists, and its
// trailers after; the comparator's copy waits on nothing.
func TestAStreamedAnswerIsNeverHeldBack(t *testing.T) {
	h := shadowEdge(t)
	release := make(chan struct{})
	var once sync.Once
	t.Cleanup(func() { once.Do(func() { close(release) }) })
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Trailer", "X-Python-Trailer")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, `{"data":`)
		w.(http.Flusher).Flush()
		<-release
		_, _ = io.WriteString(w, `{}}`)
		w.Header().Set("X-Python-Trailer", "done")
	})
	resp := h.post(t, `{"model":"m"}`, nil)
	first := make([]byte, len(`{"data":`))
	read := make(chan error, 1)
	go func() {
		_, err := io.ReadFull(resp.Body, first)
		read <- err
	}()
	select {
	case err := <-read:
		if err != nil || string(first) != `{"data":` {
			t.Fatalf("the first bytes: %q %v", first, err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the first bytes were held back until the rest came")
	}
	once.Do(func() { close(release) })
	rest, err := io.ReadAll(resp.Body)
	if err != nil || string(rest) != `{}}` || resp.Trailer.Get("X-Python-Trailer") != "done" ||
		len(resp.TransferEncoding) == 0 || resp.TransferEncoding[0] != "chunked" {
		t.Fatalf("the rest %q %v, trailer %v, encoding %v", rest, err, resp.Trailer, resp.TransferEncoding)
	}
	h.drain(t, 1)
}

// TestAComparatorThatPanicsCountsItAndTheEdgeServesOn: an observation that
// fails is counted as passed, never a crash of the node.
func TestAComparatorThatPanicsCountsItAndTheEdgeServesOn(t *testing.T) {
	sh := newShadow(func(string, ...any) {}, time.Now, time.Minute)
	// A call with no snapshot held cannot be compared: compare panics.
	r := sh.safeCompare(comparison{call: &shadowCall{body: []byte(`{"model":"m"}`)}, status: 200, complete: true})
	if r.class != ClassPassed || r.reason != "the comparator failed" {
		t.Fatalf("a panicking comparison: %+v", r)
	}
}

// TestRedactKeepsNoRequestValue: quoted values, a quote left open, and a
// provider field the schema does not name are taken out; the evaluator's
// fixed sentences stay.
func TestRedactKeepsNoRequestValue(t *testing.T) {
	for in, want := range map[string]string{
		`requested model "trustedrouter/auto" (meta model, pool or alias)`: "requested model <value> (meta model, pool or alias)",
		`model "a\"b" needs a rewrite the snapshot does not carry`:         "model <value> needs a rewrite the snapshot does not carry",
		`route type "videos`:                                       "route type <value>",
		"provider.secret_field":                                    "provider.<field>",
		"provider.order is not a list or a string":                 "provider.order is not a list or a string",
		"provider.min_privacy is not printable ASCII":              "provider.min_privacy is not printable ASCII",
		"provider.min_privacy is not a string":                     "provider.min_privacy is not a string",
		"provider.min_privacy":                                     "provider.<field>",
		"provider.order is a made-up field":                        "provider.<field>",
		"OpenAI priority prompt bound":                             "OpenAI priority prompt bound",
		"a string field that is not printable ASCII":               "a string field that is not printable ASCII",
		`provider "nope-provider" is not in the snapshot`:          "provider <value> is not in the snapshot",
		`input_modalities "smell"`:                                 "input_modalities <value>",
		"the instant is outside the snapshot's window":             "the instant is outside the snapshot's window",
		`provider.x"y`:                                             "provider.<field>",
		`decision chain "trustedrouter/dev-1.0"`:                   "decision chain <value>",
		`model "x" is served by the embeddings or decide resolver`: "model <value> is served by the embeddings or decide resolver",
	} {
		if got := redact(in); got != want {
			t.Errorf("redact(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestRunComparesAndLogsUntilItsContextEnds: Run's comparator takes what
// the proxy queued and logs the counts when it stops.
func TestRunComparesAndLogsUntilItsContextEnds(t *testing.T) {
	h := shadowEdge(t)
	c := routingCases(t)["deepseek/deepseek-v4-flash@off_peak"]
	h.clock.set(caseAt(t, c))
	py := pythonOf(c)
	py.stageD = "boot_not_accepted"
	answer := py.body()
	h.python.onAuthorize(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(answer) })
	h.python.onSnapshot(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNotModified) })
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() {
		h.edge.Run(ctx)
		close(stopped)
	}()
	h.post(t, string(c.Request), nil)
	deadline := time.Now().Add(5 * time.Second)
	for {
		if got := h.tally(t); got != nil && got.classes[ClassAgree] == 1 && h.python.polls.Load() >= 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("Run did not poll and compare")
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-stopped
	if !strings.Contains(h.logs.text(), "agree=1 differ=0 affinity=0 affinity_warm=0 passed_to_python=0") {
		t.Fatalf("Run's counts:\n%s", h.logs.text())
	}
}
