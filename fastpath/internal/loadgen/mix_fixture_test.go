package loadgen

import (
	"os"
	"testing"
)

// TestTheCheckedInMixesAreOnesTheGeneratorTakes: the fixtures (rollout P1)
// read and are valid. load-mix.json is the one the nodes carry, from two
// hours of production's settled reservations; load-mix-small.json is the
// same mix with production's stream share and its estimates capped at
// 1,915 micro-credits, so a minute at 1,000 a second books about 1.6 USD
// rather than 250 to 400 (the 2026-11-04 runs).
func TestTheCheckedInMixesAreOnesTheGeneratorTakes(t *testing.T) {
	for _, c := range []struct {
		path         string
		minEstimates int
		maxEstimate  int64
	}{
		{"../../testdata/load-mix.json", 10, 0},
		{"../../testdata/load-mix-small.json", 6, 1915},
	} {
		f, err := os.Open(c.path)
		if err != nil {
			t.Fatal(err)
		}
		mix, err := ReadMix(f)
		f.Close()
		if err != nil {
			t.Fatalf("%s: %v", c.path, err)
		}
		if err := mix.valid(); err != nil {
			t.Fatalf("%s: %v", c.path, err)
		}
		if mix.StreamShare <= 0 || mix.RefundShare <= 0 || len(mix.Estimates) < c.minEstimates || len(mix.BillPermill) < 10 {
			t.Fatalf("%s: the mix: %+v", c.path, mix)
		}
		if c.maxEstimate > 0 {
			for _, e := range mix.Estimates {
				if e.Value > c.maxEstimate {
					t.Fatalf("%s: estimate %d past its cap %d", c.path, e.Value, c.maxEstimate)
				}
			}
		}
	}
}
