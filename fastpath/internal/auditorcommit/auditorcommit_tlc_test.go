//go:build !race

// The whole-graph comparisons with TLC, and the full instances' counts. They
// are single-threaded and some ten times slower under the race detector, so
// CI runs them in a step of their own without it (.github/workflows/ci.yml).

package auditorcommit

import (
	"sort"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// TestTransitionsMatchTLC holds every step of the shadow against TLC's state
// graphs of the small instances.
func TestTransitionsMatchTLC(t *testing.T) {
	for name, c := range map[string]Config{
		"moved": moved, "ahead": ahead, "again": again, "two authorizations": twoAuths,
	} {
		if err := c.Validate(); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		got := compareWithTLC(t, c, c.Next)
		if len(got.Diffs) > 0 {
			t.Errorf("%s: differences from TLC, the first: %v", name, got.Diffs)
		}
		t.Logf("%s: %d states and %d steps, as TLC has them", name, got.States, got.Steps)
	}
}

// TestComparisonSeesADifference shows the comparison is not vacuous.
func TestComparisonSeesADifference(t *testing.T) {
	c := moved
	g := tlcGraph(t, c)
	cases := map[string]func(State) []Transition{
		"a step dropped": func(s State) []Transition {
			var out []Transition
			for _, tr := range c.Next(s) {
				if tr.Action != "Reread(m2)" {
					out = append(out, tr)
				}
			}
			return out
		},
		"a step between reached states added": func(s State) []Transition {
			out := c.Next(s)
			if s.St == Draining {
				out = append(out, Transition{"MarkDraining", s})
			}
			return out
		},
		"a step under another action's name": func(s State) []Transition {
			out := c.Next(s)
			for i := range out {
				if out[i].Action == "Commit(m1)" {
					out[i].Action = "Commit(m2)"
				}
			}
			return out
		},
	}
	// Each case changes steps TLC takes, or it would show nothing.
	labels := map[string]int{}
	for _, e := range g.Edges {
		labels[e.Action]++
	}
	for _, action := range []string{"Reread(m2)", "Commit(m1)"} {
		if labels[action] == 0 {
			t.Fatalf("TLC never takes %s in this instance, so a case about it says nothing", action)
		}
	}
	for name, next := range cases {
		if got := compareWithTLC(t, c, next); len(got.Diffs) == 0 {
			t.Errorf("%s: the comparison found no difference", name)
		}
	}
}

// TestStateCountMatchesTLC explores each configuration in proofs/ and must
// reach as many distinct states as TLC does there, as the guard table
// records. Every invariant and the step property are checked on the way.
func TestStateCountMatchesTLC(t *testing.T) {
	counts, err := tlc.GuardTableStates("AuditorCommit")
	if err != nil {
		t.Fatal(err)
	}
	files := make([]string, 0, len(counts))
	for file := range counts {
		files = append(files, file)
	}
	sort.Strings(files)
	if len(files) != 5 {
		t.Errorf("the guard table counts %d configurations, not the spec's 5: %v", len(files), files)
	}
	for _, file := range files {
		c := configOf(t, file)
		seen, _ := explore(t, c, c.Next, false)
		if len(seen) != counts[file] {
			t.Errorf("%s: the shadow reaches %d distinct states, TLC %d", file, len(seen), counts[file])
		}
	}
}
