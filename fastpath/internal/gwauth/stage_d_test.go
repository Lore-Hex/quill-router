package gwauth

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// TestStageDReasonIsPythonsOrder: _stage_d_eligibility_reason's reasons in
// its order, each the first failing condition, and ok when none fails.
func TestStageDReasonIsPythonsOrder(t *testing.T) {
	chat, messages := "chat.completions", "messages"
	open := StageDSettings{EligibilityEnabled: true, HeartbeatEnabled: true}
	stream := StageDRequest{WorkspaceID: "ws-a", BootAccepted: true, BootDeclaresHeartbeat: true, Stream: true,
		RouteType: &chat, CreditsOnly: true, StandardPricing: true, TypedSettlement: true}
	for _, c := range []struct {
		name   string
		s      func(*StageDSettings)
		q      func(*StageDRequest)
		reason string
	}{
		{"every condition holds", nil, nil, StageDOK},
		{"responses", nil, func(q *StageDRequest) { r := " responses "; q.RouteType = &r }, StageDOK},
		{"switch off, and nothing else holds", func(s *StageDSettings) {
			*s = StageDSettings{PilotWorkspaces: []string{"ws-b"}}
		}, func(q *StageDRequest) { *q = StageDRequest{} }, "stage_d_disabled"},
		{"not a pilot", func(s *StageDSettings) { s.PilotWorkspaces = []string{"ws-b"} },
			func(q *StageDRequest) { q.BootAccepted = false }, "workspace_not_pilot"},
		{"a pilot", func(s *StageDSettings) { s.PilotWorkspaces = []string{"ws-b", "ws-a"} }, nil, StageDOK},
		{"heartbeats off", func(s *StageDSettings) { s.HeartbeatEnabled = false },
			func(q *StageDRequest) { q.BootAccepted = false }, "heartbeats_disabled"},
		{"boot not accepted", nil, func(q *StageDRequest) {
			q.BootAccepted, q.BootDeclaresHeartbeat = false, false
		}, "boot_not_accepted"},
		{"undeclared, required", func(s *StageDSettings) { s.RequireHeartbeatDeclaration = true },
			func(q *StageDRequest) { q.BootDeclaresHeartbeat, q.Stream = false, false }, "heartbeat_undeclared"},
		{"undeclared, not required", nil, func(q *StageDRequest) { q.BootDeclaresHeartbeat = false }, StageDOK},
		{"not a stream", nil, func(q *StageDRequest) { q.Stream, q.RouteType = false, nil }, "not_streaming"},
		{"messages", nil, func(q *StageDRequest) { q.RouteType, q.CreditsOnly = &messages, false }, "route"},
		{"no route type", nil, func(q *StageDRequest) { q.RouteType = nil }, "route"},
		{"a BYOK candidate", nil, func(q *StageDRequest) { q.CreditsOnly, q.StandardPricing = false, false },
			"mixed_usage_type"},
		{"not standard pricing", nil, func(q *StageDRequest) { q.StandardPricing, q.ServiceTier = false, "auto" },
			"pricing_kind"},
		{"the priority tier", nil, func(q *StageDRequest) { q.ServiceTier, q.TypedSettlement = "priority", false },
			"service_tier"},
		{"the auto tier", nil, func(q *StageDRequest) { q.ServiceTier = "auto" }, "service_tier"},
		{"the flex tier", nil, func(q *StageDRequest) { q.ServiceTier = "flex" }, StageDOK},
		{"legacy records", nil, func(q *StageDRequest) { q.TypedSettlement = false }, "settlement_backend"},
	} {
		s, q := open, stream
		if c.s != nil {
			c.s(&s)
		}
		if c.q != nil {
			c.q(&q)
		}
		if got := StageDReasonOf(s, q); got != c.reason {
			t.Errorf("%s: %q, want %q", c.name, got, c.reason)
		}
	}
}

func priced(id string, starts int64, p routingsnapshot.Price) Candidate {
	p.EndpointID = id
	return Candidate{EndpointID: id, Epochs: []routingsnapshot.Epoch{{StartsAt: starts, Price: p}}}
}

func goodPrice() routingsnapshot.Price {
	bound := int64(200_000)
	return routingsnapshot.Price{
		PriceHistoryVersion: 1, Rounding: "half_up_per_million",
		Rates: routingsnapshot.Rates{InputMicroPerMillion: 1, CachedInputMicroPerMillion: 2,
			CacheCreationMicroPerMillion: 3, OutputMicroPerMillion: 4},
		Tiers: []routingsnapshot.Tier{
			{MaxPromptTokens: &bound, Rates: routingsnapshot.Rates{InputMicroPerMillion: 5}},
			{Rates: routingsnapshot.Rates{OutputMicroPerMillion: 6}},
		},
	}
}

// TestThePricingSnapshotIsPythons: the canonical text is
// canonical_pricing_snapshot's (sorted keys, compact, ASCII) of
// endpoint_pricing_document, the candidates in order, each at the epoch
// the instant falls in; candidate_prices is that text read back.
func TestThePricingSnapshotIsPythons(t *testing.T) {
	later := goodPrice()
	later.Rates.OutputMicroPerMillion = 40
	a := priced("m/a@p/prepaid", 100, goodPrice())
	a.Epochs = append(a.Epochs, routingsnapshot.Epoch{StartsAt: 200, Price: func() routingsnapshot.Price {
		later.EndpointID = a.EndpointID
		return later
	}()})
	b := priced("m/b@q/prepaid", 0, goodPrice())
	text, candidates, err := pricingSnapshot([]Candidate{a, b}, 199)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"candidates":[` +
		`{"endpoint_id":"m/a@p/prepaid","price_history_version":1,"rates":{"cache_creation_micro_per_million":3,` +
		`"cached_input_micro_per_million":2,"input_micro_per_million":1,"output_micro_per_million":4},` +
		`"request_fee_micro":0,"rounding":"half_up_per_million","tiers":[{"max_prompt_tokens":200000,"rates":` +
		`{"cache_creation_micro_per_million":0,"cached_input_micro_per_million":0,"input_micro_per_million":5,` +
		`"output_micro_per_million":0}},{"max_prompt_tokens":null,"rates":{"cache_creation_micro_per_million":0,` +
		`"cached_input_micro_per_million":0,"input_micro_per_million":0,"output_micro_per_million":6}}]},` +
		`{"endpoint_id":"m/b@q/prepaid","price_history_version":1,"rates":{"cache_creation_micro_per_million":3,` +
		`"cached_input_micro_per_million":2,"input_micro_per_million":1,"output_micro_per_million":4},` +
		`"request_fee_micro":0,"rounding":"half_up_per_million","tiers":[{"max_prompt_tokens":200000,"rates":` +
		`{"cache_creation_micro_per_million":0,"cached_input_micro_per_million":0,"input_micro_per_million":5,` +
		`"output_micro_per_million":0}},{"max_prompt_tokens":null,"rates":{"cache_creation_micro_per_million":0,` +
		`"cached_input_micro_per_million":0,"input_micro_per_million":0,"output_micro_per_million":6}}]}],` +
		`"kind":"endpoint","v":1}`
	if text != want {
		t.Fatalf("the snapshot\n got %s\nwant %s", text, want)
	}
	// candidate_prices encodes, in its own order, as the text's candidates.
	encoded, err := pyjson.Dumps(candidates, pyjson.Options{})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(text, `{"candidates":`+string(encoded)+`,"kind"`) {
		t.Fatalf("candidate_prices %s is not the text's", encoded)
	}
	// The next epoch from its first second.
	at200, _, err := pricingSnapshot([]Candidate{a}, 200)
	if err != nil || !strings.Contains(at200, `"output_micro_per_million":40`) {
		t.Fatalf("the epoch at 200: %v %s", err, at200)
	}
	var doc map[string]any
	if err := json.Unmarshal([]byte(text), &doc); err != nil || !reflect.DeepEqual(doc["v"], float64(1)) {
		t.Fatalf("not a v1 document: %v", err)
	}
}

// TestAPriceBreakingThePromisePasses: every way a candidate's price breaks
// billing_pricing_snapshot's promise (which would make Python's
// candidate_cost_reporting false) is an error, so the request passes.
func TestAPriceBreakingThePromisePasses(t *testing.T) {
	for _, c := range []struct {
		name   string
		change func(*Candidate)
	}{
		{"no epoch yet", func(c *Candidate) { c.Epochs[0].StartsAt = 1_000 }},
		{"another endpoint's price", func(c *Candidate) { c.Epochs[0].Price.EndpointID = "m/z@p/prepaid" }},
		{"version 2", func(c *Candidate) { c.Epochs[0].Price.PriceHistoryVersion = 2 }},
		{"another rounding", func(c *Candidate) { c.Epochs[0].Price.Rounding = "banker" }},
		{"a negative fee", func(c *Candidate) { c.Epochs[0].Price.RequestFeeMicro = -1 }},
		{"a negative rate", func(c *Candidate) { c.Epochs[0].Price.Rates.CachedInputMicroPerMillion = -1 }},
		{"a negative tier rate", func(c *Candidate) { c.Epochs[0].Price.Tiers[1].Rates.OutputMicroPerMillion = -1 }},
		{"a negative bound", func(c *Candidate) {
			bound := int64(-1)
			c.Epochs[0].Price.Tiers[0].MaxPromptTokens = &bound
		}},
	} {
		good := priced("m/a@p/prepaid", 0, goodPrice())
		if _, _, err := pricingSnapshot([]Candidate{good}, 10); err != nil {
			t.Fatalf("the positive control: %v", err)
		}
		bad := priced("m/a@p/prepaid", 0, goodPrice())
		bad.Epochs[0].Price.Tiers = append([]routingsnapshot.Tier(nil), bad.Epochs[0].Price.Tiers...)
		c.change(&bad)
		if _, _, err := pricingSnapshot([]Candidate{bad}, 10); err == nil {
			t.Errorf("%s: no error", c.name)
		}
	}
	twice := priced("m/a@p/prepaid", 0, goodPrice())
	if _, _, err := pricingSnapshot([]Candidate{twice, twice}, 10); err == nil {
		t.Error("an endpoint twice: no error")
	}
}
