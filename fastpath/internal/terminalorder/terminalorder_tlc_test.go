//go:build !race

// The whole-graph comparisons with TLC, and the full instances' counts. They
// are single-threaded and some ten times slower under the race detector, so
// CI runs them in a step of their own without it (.github/workflows/ci.yml).

package terminalorder

import (
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// TestTransitionsMatchTLC holds every step of the shadow against TLC's state
// graphs of the two small instances.
func TestTransitionsMatchTLC(t *testing.T) {
	instances := map[string]Config{
		"one stream": oneStream, "two plain": twoPlain, "two appends": twoAppends,
		// No authorizations, which the spec allows: TLC prints the functions
		// on them as the empty sequence.
		"none": {},
	}
	for name, c := range instances {
		got := compareWithTLC(t, c, c.Next)
		if len(got.Diffs) > 0 {
			t.Errorf("%s: differences from TLC, the first: %v", name, got.Diffs)
		}
		t.Logf("%s: %d states and %d steps, as TLC has them", name, got.States, got.Steps)
	}
}

// TestComparisonSeesADifference shows the comparison is not vacuous.
func TestComparisonSeesADifference(t *testing.T) {
	c := oneStream
	cases := map[string]func(State) []Transition{
		"a step dropped": func(s State) []Transition {
			var out []Transition
			for _, tr := range c.Next(s) {
				if tr.Action != "Deliver" {
					out = append(out, tr)
				}
			}
			return out
		},
		"a step between reached states added": func(s State) []Transition {
			out := c.Next(s)
			if s.Lease == Draining {
				out = append(out, Transition{"MarkDraining", s})
			}
			return out
		},
		"a step under another action's name": func(s State) []Transition {
			out := c.Next(s)
			for i := range out {
				if out[i].Action == "OwnerReap(a1)" {
					out[i].Action = "OwnerSettle(a1)"
				}
			}
			return out
		},
	}
	for name, next := range cases {
		if got := compareWithTLC(t, c, next); len(got.Diffs) == 0 {
			t.Errorf("%s: the comparison found no difference", name)
		}
	}
	// What a review of this package found the two smaller instances could not
	// see: a step under OwnerAdopt for a hold whose first drain row comes
	// after another hold's. The third instance has such states.
	spurious := func(s State) []Transition {
		out := twoAppends.Next(s)
		if s.OwnerActive() && s.OwnerWinner[1] == 0 && s.firstDrainRow(1) > 1 {
			out = append(out, Transition{"OwnerAdopt(a2)", s})
		}
		return out
	}
	if got := compareWithTLC(t, twoAppends, spurious); len(got.Diffs) == 0 {
		t.Error("a spurious adoption of a later drain row is not seen")
	}
}

// TestStateCountMatchesTLC explores each configuration in proofs/ and must
// reach as many distinct states as TLC does there, as the guard table
// records. Every invariant is checked on the way.
func TestStateCountMatchesTLC(t *testing.T) {
	counts, err := tlc.GuardTableStates("TerminalOrder")
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range []string{"TerminalOrder.cfg", "TerminalOrder.undeclared.cfg"} {
		want, ok := counts[file]
		if !ok {
			t.Fatalf("the guard table gives no count for %s: %v", file, counts)
		}
		c := configOf(t, file)
		seen, _ := explore(t, c, c.Next, false)
		if len(seen) != want {
			t.Errorf("%s: the shadow reaches %d distinct states, TLC %d", file, len(seen), want)
		}
	}
	if len(counts) != 2 {
		t.Errorf("the guard table counts %d configurations, and this test explores 2: %v", len(counts), counts)
	}
}
