package loadgen

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"testing"
)

// TestAStreamPlaysPastItsHold (docs/design/fast-admission-streams.md section
// 12.4, PR 3): with CeilingPermill, each stream's authorize sends a ceiling
// of that many thousandths of its hold beside it, and its heartbeats' running
// charge runs past the hold to that ceiling: a bill of 125 against a hold of
// 100 is reported whole under a ceiling of 150, and one of 200 stops at 150.
// Without it, or at a ceiling equal to the hold, none is sent and the
// running charge stops at the hold; a generation that does not stream sends
// none either.
func TestAStreamPlaysPastItsHold(t *testing.T) {
	for _, c := range []struct {
		permill, bill int64
		stream        bool
		ceiling       int64
		running       []int64
	}{
		{0, 1250, true, 0, []int64{41, 83, 100}},
		{1000, 1250, true, 0, []int64{41, 83, 100}},
		{1500, 1250, true, 150, []int64{41, 83, 125}},
		{1500, 2000, true, 150, []int64{66, 133, 150}},
		{1500, 1250, false, 0, nil},
	} {
		name := fmt.Sprintf("a ceiling of %d permill, a bill of %d permill, stream %v", c.permill, c.bill, c.stream)
		gw := admitting(t)
		var log bytes.Buffer
		cfg := config(gw)
		cfg.CeilingPermill, cfg.Mix.BillPermill, cfg.Log = c.permill, []Weight{{c.bill, 1}}, &log
		if !c.stream {
			cfg.Mix.StreamShare = 0
		}
		if _, err := Run(context.Background(), cfg); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if len(gw.authorizes) != 1 || gw.authorizes[0].Ceiling != c.ceiling || gw.authorizes[0].Estimate != 100 {
			t.Fatalf("%s: the authorizes %+v, want a ceiling of %d", name, gw.authorizes, c.ceiling)
		}
		var running []int64
		for _, hb := range gw.heartbeats {
			running = append(running, hb.Running)
		}
		if fmt.Sprint(running) != fmt.Sprint(c.running) {
			t.Fatalf("%s: the running charges %v, want %v", name, running, c.running)
		}
		var g Generation
		if err := json.Unmarshal(bytes.TrimSpace(log.Bytes()), &g); err != nil || g.Ceiling != c.ceiling {
			t.Fatalf("%s: the generation's record %s: %v", name, log.Bytes(), err)
		}
		if want := c.bill / 10; len(gw.settles) != 1 || gw.settles[0].Charge != want {
			t.Fatalf("%s: the settles %+v, want one of %d", name, gw.settles, want)
		}
	}
}

// TestACeilingIsAtLeastTheHold: a run whose streams' ceiling is below their
// hold, or whose largest estimate's ceiling passes an int64, is refused.
func TestACeilingIsAtLeastTheHold(t *testing.T) {
	for _, c := range []struct {
		permill, estimate int64
	}{{999, 100}, {-1, 100}, {3000, math.MaxInt64 / 2}} {
		cfg := config(admitting(t))
		// A bill of the hold, so the mix itself is one a run takes.
		cfg.Mix.Estimates, cfg.Mix.BillPermill = []Weight{{c.estimate, 1}}, []Weight{{1000, 1}}
		if err := cfg.valid(); err != nil {
			t.Fatalf("a hold of %d without a ceiling: %v", c.estimate, err)
		}
		cfg.CeilingPermill = c.permill
		if _, err := Run(context.Background(), cfg); err == nil {
			t.Fatalf("a ceiling of %d permill on a hold of %d was taken", c.permill, c.estimate)
		}
	}
}
