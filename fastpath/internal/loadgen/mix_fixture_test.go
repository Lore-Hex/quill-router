package loadgen

import (
	"os"
	"testing"
)

// TestTheCheckedInMixIsOneTheGeneratorTakes: the fixture the nodes carry
// (fastpath/testdata/load-mix.json, rollout P1) reads and is valid.
func TestTheCheckedInMixIsOneTheGeneratorTakes(t *testing.T) {
	f, err := os.Open("../../testdata/load-mix.json")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	mix, err := ReadMix(f)
	if err != nil {
		t.Fatal(err)
	}
	if err := mix.valid(); err != nil {
		t.Fatal(err)
	}
	if mix.StreamShare <= 0 || mix.RefundShare <= 0 || len(mix.Estimates) < 10 || len(mix.BillPermill) < 10 {
		t.Fatalf("the mix: %+v", mix)
	}
}
