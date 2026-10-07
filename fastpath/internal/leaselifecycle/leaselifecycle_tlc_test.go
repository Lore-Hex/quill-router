//go:build !race

// The whole-graph comparison of the configuration proofs/ checks. It is
// single-threaded and some ten times slower under the race detector, so CI
// runs it in a step of its own without it (.github/workflows/ci.yml).

package leaselifecycle

import (
	"fmt"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// TestWholeConfigurationMatchesTLC compares the whole state graph of the
// configuration proofs/LeaseLifecycle.cfg checks, 522,054 states, with the
// shadow's, step for step, read as it streams. TLC runs it from cfgInstance,
// which TestStateCountMatchesTLC binds to the file. So on every configuration
// TLC checks, the shadow is the spec: a spurious step anywhere in it would
// show.
func TestWholeConfigurationMatchesTLC(t *testing.T) {
	spec, err := tlc.SpecText("LeaseLifecycle")
	if err != nil {
		t.Fatal(err)
	}
	c := cfgInstance
	shadow := tlc.Shadow[State]{
		Init: c.Init(),
		Next: func(s State) []tlc.Step[State] {
			out := []tlc.Step[State]{}
			for _, tr := range c.Next(s) {
				out = append(out, tlc.Step[State]{Action: tr.Action, To: tr.To})
			}
			return out
		},
		Check: func(s State) error {
			for _, inv := range c.Invariants() {
				if !inv.Holds(s) {
					return fmt.Errorf("%s does not hold", inv.Name)
				}
			}
			return nil
		},
	}
	var got tlc.Comparison
	err = tlc.DumpFile("LeaseLifecycle", spec, cfgText(c), func(path string) error {
		var err error
		got, err = tlc.Compare(path, func(r tlc.Record) (State, error) { return fromTLC(c, r) }, shadow, 5)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Diffs) > 0 {
		t.Errorf("differences from TLC, the first: %v", got.Diffs)
	}
	t.Logf("LeaseLifecycle.cfg: %d states and %d steps, as TLC has them", got.States, got.Steps)
}
