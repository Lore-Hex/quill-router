package edge

import (
	"encoding/json"
	"testing"
)

func TestRerankAndAbsentModelsPassToPython(t *testing.T) {
	raw, err := readTestdata("authorize_rerank_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var f authorizeFile
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatal(err)
	}
	if len(f.Cases) != 3 {
		t.Fatal("missing rerank vectors")
	}
	for _, c := range f.Cases {
		t.Run(c.Name, func(t *testing.T) {
			in, _ := caseInputs(t, f, c)
			out := Decide(in)
			if c.Expect.Outcome != "pass" || out.Pass != c.Expect.Reason || out.ForShadow() != nil {
				t.Fatalf("pass=%s want=%s; shadow=%v", out.Pass, c.Expect.Reason, out.ForShadow())
			}
		})
	}
}
