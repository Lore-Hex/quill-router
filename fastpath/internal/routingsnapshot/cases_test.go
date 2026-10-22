package routingsnapshot

import (
	"encoding/json"
	"maps"
	"slices"
	"testing"
	"time"
)

type routingCase struct {
	Name          string   `json:"name"`
	At            string   `json:"at"`
	BYOKProviders []string `json:"byok_providers"`
	Request       struct {
		Model                string          `json:"model"`
		Models               []string        `json:"models"`
		Provider             json.RawMessage `json:"provider"`
		RouteType            *string         `json:"route_type"`
		Region               *string         `json:"region"`
		ServiceTier          *string         `json:"service_tier"`
		RequestedParameters  []string        `json:"requested_parameters"`
		InputModalities      []string        `json:"input_modalities"`
		Stream               *bool           `json:"stream"`
		EstimatedInputTokens int64           `json:"estimated_input_tokens"`
		OutputTokens         int64           `json:"output_tokens"`
	} `json:"request"`
	Expect struct {
		Candidates    []string `json:"candidates"`
		ModelEstimate *int64   `json:"model_estimate"`
		ServiceTier   *string  `json:"service_tier"`
		StageDReason  string   `json:"stage_d_reason"`
		Error         *struct {
			Status  int    `json:"status"`
			Type    string `json:"type"`
			Message string `json:"message"`
		} `json:"error"`
	} `json:"expect"`
}

type routingCases struct {
	Rerank   json.RawMessage `json:"rerank"`
	Settings struct {
		PrimaryRegion string   `json:"primary_region"`
		Regions       []string `json:"regions"`
	} `json:"settings"`
	StageDInputs struct {
		BootAccepted                bool `json:"boot_accepted"`
		EligibilityEnabled          bool `json:"eligibility_enabled"`
		HeartbeatDeclared           bool `json:"heartbeat_declared"`
		HeartbeatEnabled            bool `json:"heartbeat_enabled"`
		Pilot                       bool `json:"pilot"`
		RequireHeartbeatDeclaration bool `json:"require_heartbeat_declaration"`
		SettlementBackend           bool `json:"settlement_backend"`
		StandardEndpointPricing     bool `json:"standard_endpoint_pricing"`
	} `json:"stage_d_inputs"`
	Cases []routingCase `json:"cases"`
}

// outsideTheCohort are the cases Go passes to Python, each with the reason
// it must give (docs/design/routing-snapshot-v1.md, "What Go evaluates").
// The test fails if Go answers one of them, or passes any other case.
var outsideTheCohort = map[string]string{}

func init() {
	for _, model := range []string{
		"deepseek/deepseek-v4-flash", "google/gemini-3.1-pro-preview",
		"z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash",
	} {
		// Request parsing the snapshot does not carry the tables of yet.
		outsideTheCohort[model+"+confidential"] = "provider.min_privacy"
		outsideTheCohort[model+"+max_price"] = "provider.max_price"
		outsideTheCohort[model+"+perf"] = "provider.preferred_max_latency"
		// The fixture snapshot lists 13 providers, but the cases ran against
		// the process's whole provider table: "google" names google-vertex,
		// which the fixture omits, and "nope-provider" is unknown to both.
		// A slug missing from the snapshot is not proof Python refuses it.
		outsideTheCohort[model+"+group_alias"] = `provider "google-vertex" is not in the snapshot`
		outsideTheCohort[model+"+unknown_provider"] = `provider "nope-provider" is not in the snapshot`
	}
	outsideTheCohort["route:images"] = `route type "images"`
	outsideTheCohort["required_parameters"] = "provider.require_parameters"
	outsideTheCohort["missing_parameters"] = "provider.require_parameters"
}

func loadCases(t *testing.T) routingCases {
	t.Helper()
	var file routingCases
	if err := json.Unmarshal(testdata(t, "routing_cases_v1.json"), &file); err != nil {
		t.Fatal(err)
	}
	return file
}

func (c routingCase) request(t *testing.T) Request {
	t.Helper()
	at, err := time.Parse(time.RFC3339, c.At)
	if err != nil {
		t.Fatal(err)
	}
	r := c.Request
	return Request{
		Model: r.Model, Models: r.Models, Provider: r.Provider, RouteType: r.RouteType,
		Region: r.Region, ServiceTier: r.ServiceTier, RequestedParameters: r.RequestedParameters,
		InputModalities: r.InputModalities, BYOKProviders: c.BYOKProviders,
		EstimatedInputTokens: r.EstimatedInputTokens, OutputTokens: r.OutputTokens, At: at,
	}
}

// TestTheEvaluatorReproducesEveryRoutingCase runs every case in
// routing_cases_v1.json against the golden snapshot. A case inside the
// cohort must match exactly: candidates and their order, the estimate to
// the microdollar, the service tier and the Stage D reason, or the refusal's
// status, type and message. A case outside it must be passed to Python for
// its listed reason.
func TestTheEvaluatorReproducesEveryRoutingCase(t *testing.T) {
	s := goldenSnapshot(t)
	file := loadCases(t)
	if string(file.Rerank) != "null" || file.Settings.PrimaryRegion != s.Settings.PrimaryRegion ||
		!slices.Equal(file.Settings.Regions, s.Settings.Regions) {
		t.Fatalf("the cases' settings differ from the snapshot's: %+v", file.Settings)
	}
	in := file.StageDInputs
	stageD := StageDInputs{
		EligibilityEnabled: in.EligibilityEnabled, Pilot: in.Pilot, HeartbeatEnabled: in.HeartbeatEnabled,
		BootAccepted: in.BootAccepted, HeartbeatDeclared: in.HeartbeatDeclared,
		RequireHeartbeatDeclaration: in.RequireHeartbeatDeclaration,
		StandardEndpointPricing:     in.StandardEndpointPricing, SettlementBackend: in.SettlementBackend,
	}
	var answered, refusedCount int
	passed := map[string]int{}
	names := map[string]bool{}
	for _, c := range file.Cases {
		if names[c.Name] {
			t.Fatalf("duplicate case name %q", c.Name)
		}
		names[c.Name] = true
		got := s.Evaluate(c.request(t))
		if want, outside := outsideTheCohort[c.Name]; outside {
			if got.Outcome != PassToPython || got.Reason != want {
				t.Errorf("%s: must pass to Python (%s), got %+v", c.Name, want, got)
			}
			passed[got.Reason]++
			continue
		}
		if got.Outcome == PassToPython {
			t.Errorf("%s: passed to Python (%s), but it is inside the cohort", c.Name, got.Reason)
			continue
		}
		if e := c.Expect.Error; e != nil {
			refusedCount++
			if got.Outcome != Refused || *got.Refusal != (Refusal{Status: e.Status, Type: e.Type, Message: e.Message}) {
				t.Errorf("%s: want refusal %+v, got %+v %+v", c.Name, *e, got, got.Refusal)
			}
			continue
		}
		answered++
		tier := ""
		if c.Expect.ServiceTier != nil {
			tier = *c.Expect.ServiceTier
		}
		if got.Outcome != Answered || !slices.Equal(got.Candidates, c.Expect.Candidates) ||
			got.ModelEstimate != *c.Expect.ModelEstimate || got.ServiceTier != tier {
			t.Errorf("%s: want %v %d %q, got %+v", c.Name, c.Expect.Candidates, *c.Expect.ModelEstimate, tier, got)
			continue
		}
		if reason := s.StageDReason(stageD, c.Request.Stream, c.Request.RouteType, got); reason != c.Expect.StageDReason {
			t.Errorf("%s: Stage D reason %q, want %q", c.Name, reason, c.Expect.StageDReason)
		}
	}
	for name := range outsideTheCohort {
		if !names[name] {
			t.Errorf("%s: listed outside the cohort but not a case", name)
		}
	}
	total := 0
	for _, n := range passed {
		total += n
	}
	t.Logf("%d cases: %d answered and %d refused exactly as Python; %d passed to Python:",
		len(file.Cases), answered, refusedCount, total)
	for _, reason := range slices.Sorted(maps.Keys(passed)) {
		t.Logf("  %2d  %s", passed[reason], reason)
	}
	// Positive control: the cohort answers and refuses in volume, and orders
	// several candidates.
	if answered < 90 || refusedCount < 30 || answered+refusedCount+total != len(file.Cases) {
		t.Fatalf("answered %d, refused %d, passed %d of %d", answered, refusedCount, total, len(file.Cases))
	}
}
