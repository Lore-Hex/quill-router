package routingsnapshot

import (
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"
)

var decidePasses = map[string]string{
	"route:chat.completions": "hosted decide requires route_type decide",
	"route:embeddings":       "hosted decide requires route_type decide",
	"route:None":             "hosted decide requires route_type decide",
	"route_absent":           "hosted decide requires route_type decide",
	"variant":                "hosted decide requires an exact catalog id",
	"dated":                  "hosted decide requires an exact catalog id",
	"spaced_id":              "hosted decide requires an exact catalog id",
	"non_ascii":              "a string field that is not printable ASCII, or one the authorize schema refuses",
	"models_decision":        "hosted decide with a nonempty models array",
	"models_chat":            "hosted decide with a nonempty models array",
	"models_no_fallbacks":    "hosted decide with a nonempty models array",
	"performance_latency":    "provider.preferred_max_latency",
	"performance_throughput": "provider.preferred_min_throughput",
	"unknown_option":         "provider.fixture_option",
	"unknown_provider":       `provider "fixture-provider" is not in the snapshot`,
	"priority_large":         "OpenAI priority prompt bound",
	"embeddings_only":        `model "fixture.invalid/embedding-only" is served by the embeddings or decide resolver`,
}

// chatArmWording are the cases where a chat model's request names a
// decision-only model as an alternative on route decide. Python refuses with
// its decision wording; the chat arm refuses with its own, with the flag on
// or off, as it did before the flag existed. The edge passes every refusal
// to Python, so the wording reaches no caller.
var chatArmWording = map[string]string{
	"chat_then_decision": "Model does not support chat completions: fixture.invalid/hosted-decision-multi",
}

func checkChatArmWording(t *testing.T, name string, got Result) bool {
	t.Helper()
	message, listed := chatArmWording[name]
	if !listed {
		return false
	}
	if want := refused(400, "model_not_supported", message); !reflect.DeepEqual(got, want) {
		t.Fatalf("want %+v (%+v), got %+v (%+v)", want, want.Refusal, got, got.Refusal)
	}
	return true
}

func decideFixture(t *testing.T) (*Snapshot, routingCases) {
	t.Helper()
	var manifest manifestFile
	if err := json.Unmarshal(testdata(t, "routing_snapshot_decide_v3.manifest.json"), &manifest); err != nil {
		t.Fatal(err)
	}
	s, _, err := Verify(manifest.JWS, []TrustedKey{goldenKey(t, manifest)},
		testdata(t, "routing_snapshot_decide_v3.body.json"), goldenOptions(manifest, 1792368000))
	if err != nil {
		t.Fatal(err)
	}
	var file routingCases
	if err := json.Unmarshal(testdata(t, "routing_cases_decide_v1.json"), &file); err != nil {
		t.Fatal(err)
	}
	return s, file
}

func checkPythonCase(t *testing.T, s *Snapshot, file routingCases, c routingCase, req Request, got Result) {
	t.Helper()
	if e := c.Expect.Error; e != nil {
		want := &Refusal{Status: e.Status, Type: e.Type, Message: e.Message, Param: e.Param}
		if got.Outcome != Refused || !reflect.DeepEqual(got.Refusal, want) {
			t.Fatalf("want refusal %+v, got %+v (%+v)", want, got, got.Refusal)
		}
		return
	}
	tier := ""
	if c.Expect.ServiceTier != nil {
		tier = *c.Expect.ServiceTier
	}
	if got.Outcome != Answered || !slices.Equal(got.Candidates, c.Expect.Candidates) ||
		got.ModelEstimate != *c.Expect.ModelEstimate || got.ServiceTier != tier {
		t.Fatalf("want %v %d %q, got %+v", c.Expect.Candidates, *c.Expect.ModelEstimate, tier, got)
	}
	if c.Expect.ModelReservation != nil && got.Reservation != *c.Expect.ModelReservation {
		t.Fatalf("reservation %d, want %d", got.Reservation, *c.Expect.ModelReservation)
	}
	in := file.StageDInputs
	stageD := StageDInputs{
		EligibilityEnabled: in.EligibilityEnabled, Pilot: in.Pilot, HeartbeatEnabled: in.HeartbeatEnabled,
		BootAccepted: in.BootAccepted, HeartbeatDeclared: in.HeartbeatDeclared,
		RequireHeartbeatDeclaration: in.RequireHeartbeatDeclaration,
		StandardEndpointPricing:     in.StandardEndpointPricing, SettlementBackend: in.SettlementBackend,
	}
	if reason, ok := s.StageDReason(stageD, req, got); !ok || reason != c.Expect.StageDReason {
		t.Fatalf("Stage D reason %q (known %v), want %q", reason, ok, c.Expect.StageDReason)
	}
}

func TestHostedDecideCases(t *testing.T) {
	s, file := decideFixture(t)
	names := map[string]bool{}
	for _, c := range file.Cases {
		if names[c.Name] {
			t.Fatalf("duplicate case %s", c.Name)
		}
		names[c.Name] = true
		t.Run(c.Name, func(t *testing.T) {
			req := c.request(t)
			req.HostedDecide = true
			got := s.Evaluate(req)
			if reason, outside := decidePasses[c.Name]; outside {
				if got.Outcome != PassToPython || !strings.Contains(got.Reason, reason) {
					t.Fatalf("must pass with %q, got %+v", reason, got)
				}
				return
			}
			if checkChatArmWording(t, c.Name, got) {
				return
			}
			checkPythonCase(t, s, file, c, req, got)
		})
	}
	for name := range decidePasses {
		if !names[name] {
			t.Errorf("expected pass %s has no case", name)
		}
	}
}

// These are the evaluator's results before HostedDecide existed. Most exact
// hosted ids stop at the old resolver pass; earlier provider parsing and the
// old chat refusal for variant/dated ids retain their original precedence.
func TestHostedDecideDisabled(t *testing.T) {
	s, file := decideFixture(t)
	for _, c := range file.Cases {
		t.Run(c.Name, func(t *testing.T) {
			req := c.request(t)
			req.HostedDecide = false
			got := s.Evaluate(req)
			if checkChatArmWording(t, c.Name, got) {
				return
			}
			switch c.Name {
			case "native_chat_decide", "invalid_privacy", "invalid_parameters",
				"chat_and_decision", "chat_and_decision_fits", "chat_and_decision_chat_route",
				"chat_then_decision_chat_route":
				checkPythonCase(t, s, file, c, req, got)
				return
			case "variant", "dated":
				want := refused(400, "model_not_supported", "Model does not support chat completions: fixture.invalid/hosted-decision-multi")
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("want %+v (%+v), got %+v (%+v)", want, want.Refusal, got, got.Refusal)
				}
				return
			}
			reason := fmt.Sprintf("model %q is served by the embeddings or decide resolver", strings.TrimSpace(req.Model))
			switch c.Name {
			case "performance_latency", "performance_throughput", "unknown_option", "unknown_provider", "non_ascii":
				reason = decidePasses[c.Name]
			}
			if want := pass("%s", reason); !reflect.DeepEqual(got, want) {
				t.Fatalf("want %+v, got %+v (%+v)", want, got, got.Refusal)
			}
		})
	}
	// Every existing case still runs with the zero-value flag, against its
	// original Python expectation and explicit original pass table.
	t.Run("existing", TestTheEvaluatorReproducesEveryRoutingCase)
}

// The flag selects an arm only for a decision-only model. The existing
// fixture has none, so with the flag set every existing case must give the
// result it gives without it.
func TestHostedDecideFlagLeavesEveryExistingCaseAlone(t *testing.T) {
	checkEveryRoutingCase(t, true)
}

func TestHostedDecideAliasPasses(t *testing.T) {
	s, file := decideFixture(t)
	req := file.Cases[0].request(t)
	alias := "fixture.invalid/decision-alias"
	s.Aliases.RouterBaseRewrites[alias] = req.Model
	req.Model = alias
	req.HostedDecide = true
	if got := s.Evaluate(req); got.Outcome != PassToPython || got.Reason != "hosted decide requires an exact catalog id" {
		t.Fatalf("rewritten decision id must pass, got %+v", got)
	}
}

func TestHostedDecideUnclassifiedCatalogSpellingPasses(t *testing.T) {
	s, file := decideFixture(t)
	req := file.Cases[0].request(t)
	// A catalog id can itself look like a routing variant. The arm selector
	// does not admit this spelling; the request flag alone must not admit it
	// through requestedModelIDs either.
	model := *s.models[req.Model]
	model.ID = "fixture.invalid/variant-only:nitro"
	s.models[model.ID] = &model
	req.Model = model.ID
	req.HostedDecide = true
	want := fmt.Sprintf("model %q is served by the embeddings or decide resolver", model.ID)
	if got := s.Evaluate(req); got.Outcome != PassToPython || got.Reason != want {
		t.Fatalf("unclassified catalog spelling must pass with %q, got %+v", want, got)
	}
}
