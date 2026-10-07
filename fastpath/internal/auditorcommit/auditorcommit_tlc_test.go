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
	noAuths := moved
	noAuths.Auths = nil
	secondHolder := moved
	secondHolder.Holder = 1
	for name, c := range map[string]Config{
		"moved": moved, "ahead": ahead, "again": again, "two authorizations": twoAuths,
		// No authorizations, which the spec allows: TLC prints the functions
		// on them as the empty sequence.
		"no authorizations": noAuths,
		// The configuration names m2 first, so CHOOSE starts the lease there.
		"m2 named first": secondHolder,
		"back again":     backAgain,
		"gap and crash":  gapCrash,
		"two by two":     twoByTwo,
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
	// What a review of this package found the first instances could not see:
	// a step taken only once the lease has come back to a member, or after a
	// gap and a crash. The instances added for it have such states.
	for name, inst := range map[string]Config{"back again": backAgain, "gap and crash": gapCrash} {
		inst := inst
		spurious := func(s State) []Transition {
			out := inst.Next(s)
			if s.St == Draining && (s.Assigns >= 2 || s.Gap && s.Crashes > 0) {
				out = append(out, Transition{"MarkDraining", s})
			}
			return out
		}
		if got := compareWithTLC(t, inst, spurious); len(got.Diffs) == 0 {
			t.Errorf("%s: a step taken only there is not seen", name)
		}
	}
}

// TestWholeConfigurationsMatchTLC compares the whole state graphs of all five
// configurations proofs/ checks, up to 822,229 states, with the shadow's, step
// for step, read as they stream. TLC runs each from its declaration in
// cfgInstances, which TestStateCountMatchesTLC binds to its file, checking
// TypeOK alone. So on every configuration TLC checks, the shadow is the spec:
// a spurious step in a state only those configurations reach would show.
func TestWholeConfigurationsMatchTLC(t *testing.T) {
	files := make([]string, 0, len(cfgInstances))
	for file := range cfgInstances {
		files = append(files, file)
	}
	sort.Strings(files)
	for _, file := range files {
		c := cfgInstances[file]
		got := compareWithTLC(t, c, c.Next)
		if len(got.Diffs) > 0 {
			t.Errorf("%s: differences from TLC, the first: %v", file, got.Diffs)
		}
		t.Logf("%s: %d states and %d steps, as TLC has them", file, got.States, got.Steps)
	}
	// What a review of this package found the small instances could not
	// see: a checkpoint issued while the lease drains, only where an owner
	// may issue three records.
	c := cfgInstances["AuditorCommit.two.cfg"]
	spurious := func(s State) []Transition {
		out := c.Next(s)
		if c.MaxSeq >= 3 && s.St == Draining && int(s.NextSeq) <= c.MaxSeq {
			to := s
			to.appendOut(Rec{KCkpt, NoAuth, s.OwnerSum, s.NextSeq, 0})
			to.NextSeq++
			out = append(out, Transition{"IssueCheckpoint", to})
		}
		return out
	}
	if got := compareWithTLC(t, c, spurious); len(got.Diffs) == 0 {
		t.Error("a checkpoint issued while the lease drains is not seen")
	}
}

// TestStateCountMatchesTLC explores each configuration proofs/ checks and
// must reach as many distinct states as TLC does there, as the guard table
// records. Every invariant and the step property are checked on the way.
func TestStateCountMatchesTLC(t *testing.T) {
	counts, err := tlc.GuardTableStates("AuditorCommit")
	if err != nil {
		t.Fatal(err)
	}
	if len(counts) != len(cfgInstances) {
		t.Errorf("the guard table counts %v, and this test declares %d configurations", counts, len(cfgInstances))
	}
	files := make([]string, 0, len(cfgInstances))
	for file := range cfgInstances {
		files = append(files, file)
	}
	sort.Strings(files)
	for _, file := range files {
		c := cfgInstances[file]
		if err := c.Validate(); err != nil {
			t.Fatalf("%s: %v", file, err)
		}
		if err := tlc.CheckAssumption("AuditorCommit", file, declares(c)); err != nil {
			t.Fatal(err)
		}
		want, ok := counts[file]
		if !ok {
			t.Fatalf("the guard table gives no count for %s: %v", file, counts)
		}
		seen, _ := explore(t, c, c.Next, false)
		if len(seen) != want {
			t.Errorf("%s: the shadow reaches %d distinct states, TLC %d", file, len(seen), want)
		}
	}
}

// TestDeclarationsAreBoundToTheirFiles: a declaration that differs from its
// .cfg is refused by TLC reading the file.
func TestDeclarationsAreBoundToTheirFiles(t *testing.T) {
	wrong := cfgInstances["AuditorCommit.again.cfg"]
	wrong.Lying = false
	if err := tlc.CheckAssumption("AuditorCommit", "AuditorCommit.again.cfg", declares(wrong)); err == nil {
		t.Fatal("a declaration with an honest owner is taken for AuditorCommit.again.cfg's")
	}
}
