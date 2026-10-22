package routingsnapshot

import (
	"bytes"
	"encoding/json"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// TestThePricingCoreComputesWhatPythonCharges holds Cost to
// stage_d.endpoint_cost_microdollars_from_candidate on the shared pricing
// vectors (tests/fixtures/usage_cost_vectors.json, which
// tests/test_usage_cost_vectors.py holds to that function). Every vector
// applies: the candidate is the snapshot's Price, and the usage carries the
// cache counts and tier basis the estimate leaves at zero.
func TestThePricingCoreComputesWhatPythonCharges(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "tests", "fixtures", "usage_cost_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors []struct {
		Name      string          `json:"name"`
		Candidate json.RawMessage `json:"candidate"`
		Usage     struct {
			InputTokens          int64 `json:"input_tokens"`
			OutputTokens         int64 `json:"output_tokens"`
			CacheReadTokens      int64 `json:"cache_read_tokens"`
			CacheCreationTokens  int64 `json:"cache_creation_tokens"`
			PriceTierInputTokens int64 `json:"price_tier_input_tokens"`
		} `json:"usage"`
		Expected json.Number `json:"expected_microdollars"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&vectors); err != nil {
		t.Fatal(err)
	}
	if len(vectors) != 29 {
		t.Fatalf("%d vectors", len(vectors))
	}
	tiered := 0
	for _, v := range vectors {
		var generic any
		d := json.NewDecoder(bytes.NewReader(v.Candidate))
		d.UseNumber()
		if err := d.Decode(&generic); err != nil {
			t.Fatal(err)
		}
		// The vectors' candidate is exactly the snapshot's price shape.
		if err := shape(generic, reflect.TypeFor[Price](), v.Name); err != nil {
			t.Fatal(err)
		}
		var price Price
		if err := json.Unmarshal(v.Candidate, &price); err != nil {
			t.Fatal(err)
		}
		tiered += min(len(price.Tiers), 1)
		want, ok := new(big.Int).SetString(v.Expected.String(), 10)
		if !ok {
			t.Fatalf("%s: expected %s", v.Name, v.Expected)
		}
		got := Cost(price, Usage(v.Usage))
		if got.Cmp(want) != 0 {
			t.Errorf("%s: Go %s, Python %s", v.Name, got, want)
		}
	}
	if tiered == 0 {
		t.Error("no vector exercises tiers")
	}
}

// TestCostIsExactBeyondInt64: Python's integers do not overflow, so neither
// does the core; the evaluator passes an estimate outside int64 to Python.
func TestCostIsExactBeyondInt64(t *testing.T) {
	const max = 1<<63 - 1
	price := Price{Rates: Rates{InputMicroPerMillion: max, OutputMicroPerMillion: max}, Rounding: pricingRounding}
	got := Cost(price, Usage{InputTokens: max, OutputTokens: max})
	product := new(big.Int).Mul(big.NewInt(max), big.NewInt(max))
	product.Add(product, big.NewInt(500_000))
	product.Quo(product, big.NewInt(1_000_000))
	want := new(big.Int).Add(product, product)
	if got.Cmp(want) != 0 || got.IsInt64() {
		t.Fatalf("got %s, want %s", got, want)
	}
	// Positive control: an ordinary usage is an ordinary int64.
	if small := Cost(price, Usage{}); small.Sign() != 0 {
		t.Fatalf("zero usage costs %s", small)
	}
}
