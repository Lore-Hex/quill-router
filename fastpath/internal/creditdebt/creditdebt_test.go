package creditdebt

import (
	"encoding/json"
	"math"
	"math/rand"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

type vectors struct {
	Cases []struct {
		Headroom []int64         `json:"headroom"`
		Cover    json.RawMessage `json:"cover"`
		Square   struct {
			Headroom []int64 `json:"headroom"`
			Marked   bool    `json:"marked"`
		} `json:"square"`
		Deltas  []int64 `json:"deltas"`
		Inflows []struct {
			Amount int64 `json:"amount"`
			Repay  struct {
				Headroom []int64 `json:"headroom"`
				Left     int64   `json:"left"`
			} `json:"repay"`
			TakeInflow struct {
				Headroom []int64 `json:"headroom"`
				Marked   bool    `json:"marked"`
				Left     int64   `json:"left"`
			} `json:"take_inflow"`
		} `json:"inflows"`
	} `json:"cases"`
	Refused []struct {
		Function string  `json:"function"`
		Headroom []int64 `json:"headroom"`
		After    []int64 `json:"after"`
		Amount   int64   `json:"amount"`
	} `json:"refused"`
}

// TestThePortComputesWhatPythonDoes holds each function to
// src/trusted_router/credit_debt.py's results on the golden vectors, which
// tests/test_fastpath_credit_debt_vectors.py holds to that module.
func TestThePortComputesWhatPythonDoes(t *testing.T) {
	text, err := os.ReadFile(filepath.Join("..", "..", "testdata", "credit_debt_vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(text, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Cases) < 200 || len(v.Refused) == 0 {
		t.Fatalf("the vectors hold %d cases and %d refusals", len(v.Cases), len(v.Refused))
	}
	for _, c := range v.Cases {
		covered, err := Cover(c.Headroom)
		var want []int64
		if json.Unmarshal(c.Cover, &want) == nil {
			if err != nil || !slices.Equal(covered, want) {
				t.Errorf("Cover(%v) = %v, %v; Python %v", c.Headroom, covered, err, want)
			}
		} else if err == nil {
			t.Errorf("Cover(%v) = %v; Python refuses it", c.Headroom, covered)
		}
		squared, err := Square(c.Headroom)
		if err != nil || squared.Marked != c.Square.Marked || !slices.Equal(squared.Headroom, c.Square.Headroom) {
			t.Errorf("Square(%v) = %+v, %v; Python %+v", c.Headroom, squared, err, c.Square)
		}
		deltas, err := CreditDeltas(c.Headroom, squared.Headroom)
		if err != nil || !slices.Equal(deltas, c.Deltas) {
			t.Errorf("CreditDeltas for %v = %v, %v; Python %v", c.Headroom, deltas, err, c.Deltas)
		}
		for _, in := range c.Inflows {
			repaid, left, err := Repay(c.Headroom, in.Amount)
			if err != nil || left != in.Repay.Left || !slices.Equal(repaid, in.Repay.Headroom) {
				t.Errorf("Repay(%v, %d) = %v, %d, %v; Python %+v", c.Headroom, in.Amount, repaid, left, err, in.Repay)
			}
			inflow, err := TakeInflow(c.Headroom, in.Amount)
			if err != nil || inflow.Marked != in.TakeInflow.Marked || inflow.Left != in.TakeInflow.Left ||
				!slices.Equal(inflow.Headroom, in.TakeInflow.Headroom) {
				t.Errorf("TakeInflow(%v, %d) = %+v, %v; Python %+v", c.Headroom, in.Amount, inflow, err, in.TakeInflow)
			}
		}
	}
	for _, r := range v.Refused {
		var err error
		switch r.Function {
		case "square":
			_, err = Square(r.Headroom)
		case "cover":
			_, err = Cover(r.Headroom)
		case "repay":
			_, _, err = Repay(r.Headroom, r.Amount)
		case "take_inflow":
			_, err = TakeInflow(r.Headroom, r.Amount)
		case "credit_deltas":
			_, err = CreditDeltas(r.Headroom, r.After)
		default:
			t.Fatalf("a refusal of no function %q", r.Function)
		}
		if err == nil {
			t.Errorf("%s takes %+v, which Python refuses", r.Function, r)
		}
	}
}

// TestTheRulesHoldOnRandomRows checks the section's claims directly on rows
// the vectors do not hold: no row ends negative unless every row is marked;
// marked exactly when the signed sum is negative; only credit moves between
// rows, so the sum is kept; money coming in repays before it is left over.
func TestTheRulesHoldOnRandomRows(t *testing.T) {
	rng := rand.New(rand.NewSource(1586))
	sum := func(r []int64) (s int64) {
		for _, v := range r {
			s += v
		}
		return s
	}
	for range 20000 {
		r := make([]int64, 1+rng.Intn(16))
		for i := range r {
			r[i] = rng.Int63n(2001) - 1000
		}
		sq, err := Square(r)
		if err != nil {
			t.Fatal(err)
		}
		if sq.Marked != (sum(r) < 0) || sum(sq.Headroom) != sum(r) {
			t.Fatalf("Square(%v) = %+v", r, sq)
		}
		if !sq.Marked && slices.Min(sq.Headroom) < 0 {
			t.Fatalf("Square(%v) leaves a row negative unmarked: %v", r, sq.Headroom)
		}
		amount := rng.Int63n(3000)
		in, err := TakeInflow(r, amount)
		if err != nil {
			t.Fatal(err)
		}
		if sum(in.Headroom)+in.Left != sum(r)+amount || in.Marked != (sum(r)+amount-in.Left < 0) {
			t.Fatalf("TakeInflow(%v, %d) = %+v", r, amount, in)
		}
		if in.Left > 0 && slices.Min(in.Headroom) < 0 {
			t.Fatalf("TakeInflow(%v, %d) leaves money over with a row negative: %+v", r, amount, in)
		}
	}
}

func TestRowsInt64CannotHoldAreRefused(t *testing.T) {
	for _, r := range [][]int64{{math.MinInt64}, {math.MinInt64, math.MaxInt64, 1}} {
		if _, err := Square(r); err == nil {
			t.Errorf("Square(%v) is computed", r)
		}
	}
	// A sum past int64's bounds has a sign all the same, and the rows stay
	// in range: Python's results, which the vectors hold too.
	if got, err := Square([]int64{math.MaxInt64, 1}); err != nil || got.Marked {
		t.Errorf("Square past the top: %+v %v", got, err)
	}
	if got, err := Square([]int64{math.MinInt64 + 1, -2}); err != nil || !got.Marked {
		t.Errorf("Square past the bottom: %+v %v", got, err)
	}
	if _, err := CreditDeltas([]int64{math.MinInt64 + 1}, []int64{math.MaxInt64}); err == nil {
		t.Error("a change past int64's range is computed")
	}
}
