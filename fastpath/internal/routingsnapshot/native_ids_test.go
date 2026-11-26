package routingsnapshot

import (
	"strings"
	"testing"
)

// TestAListedNativeIDPassesToPython: Python resolves a provider's own model
// id ("claude-opus-5-5") through routing.native_model_id_targets and lists
// each such id among the snapshot's alias targets. The evaluator does not
// follow the listing; it hands the request to Python, in the primary model,
// under a variant suffix and in the fallback list. Unlisted, the same id is
// one Go refuses, so a listing is what keeps the two from disagreeing.
func TestAListedNativeIDPassesToPython(t *testing.T) {
	const native, target = "claude-fixture-9-5", "deepseek/deepseek-v4-flash"
	requests := map[string]func(*Request){
		"primary":  func(r *Request) { r.Model = native },
		"variant":  func(r *Request) { r.Model = native + ":nitro" },
		"fallback": func(r *Request) { r.Models = []string{native} },
	}
	for name, change := range requests {
		unlisted, listed := goldenSnapshot(t), goldenSnapshot(t)
		listed.Aliases.AliasTargets[native] = target
		req := baseRequest()
		change(&req)
		got := unlisted.Evaluate(req)
		if got.Outcome != Refused || got.Refusal == nil || got.Refusal.Status != 400 ||
			got.Refusal.Type != "model_not_supported" {
			t.Errorf("%s, unlisted: want the 400 for an unknown model, got %+v %+v", name, got, got.Refusal)
		}
		got = listed.Evaluate(req)
		if got.Outcome != PassToPython || !strings.Contains(got.Reason, "resolves through a rule or alias outside the cohort") {
			t.Errorf("%s, listed: want a pass to Python, got %+v %+v", name, got, got.Refusal)
		}
		// The target's own id is still answered by Go.
		if got := listed.Evaluate(baseRequest()); got.Outcome != Answered {
			t.Errorf("%s: the target model left the cohort: %+v", name, got)
		}
	}
}
