package leaselifecycle

import (
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// small is an instance small enough for TLC to write its whole state graph:
// two hold slots, one hold's worth of lease, a restart. TLC reaches 21,653
// distinct states and explores 141,710 steps in it.
var small = Config{
	MaxHolds: 2, LeaseSize: 1, Window: 2, Skew: 1, MaxLife: 1, Grace: 1,
	CacheAge: 0, LastRenew: 1, MaxRestarts: 1,
}

var (
	smallGraph     *tlc.Graph
	smallGraphErr  error
	smallGraphOnce sync.Once
)

func smallTLCGraph(t *testing.T) *tlc.Graph {
	t.Helper()
	smallGraphOnce.Do(func() {
		smallGraph, smallGraphErr = tlc.Dump("LeaseLifecycle", cfgText(small))
	})
	if smallGraphErr != nil {
		t.Fatal(smallGraphErr)
	}
	return smallGraph
}

func cfgText(c Config) string {
	return fmt.Sprintf(`SPECIFICATION Spec
CONSTANTS
    MaxHolds = %d
    LeaseSize = %d
    Window = %d
    Skew = %d
    MaxLife = %d
    Grace = %d
    CacheAge = %d
    LastRenew = %d
    MaxRestarts = %d
INVARIANTS
    TypeOK
`, c.MaxHolds, c.LeaseSize, c.Window, c.Skew, c.MaxLife, c.Grace, c.CacheAge, c.LastRenew, c.MaxRestarts)
}

type step struct {
	from   State
	action string
	to     State
}

// explore visits every state reachable from Init by next, checking every
// invariant on each state and every step property on each step. With keep it
// also returns the steps.
func explore(t *testing.T, c Config, next func(State) []Transition, keep bool) (map[State]struct{}, map[step]struct{}) {
	t.Helper()
	invariants, properties := c.Invariants(), StepProperties()
	seen := map[State]struct{}{}
	steps := map[step]struct{}{}
	queue := []State{c.Init()}
	seen[queue[0]] = struct{}{}
	failures := 0
	fail := func(format string, args ...any) {
		if failures < 5 {
			t.Errorf(format, args...)
		}
		failures++
	}
	for len(queue) > 0 {
		s := queue[0]
		queue = queue[1:]
		for _, inv := range invariants {
			if !inv.Holds(s) {
				fail("%s does not hold in %+v", inv.Name, s)
			}
		}
		for _, tr := range next(s) {
			for _, p := range properties {
				if !p.Holds(s, tr.To) {
					fail("%s does not hold on %s from %+v", p.Name, tr.Action, s)
				}
			}
			if keep {
				steps[step{s, tr.Action, tr.To}] = struct{}{}
			}
			if _, ok := seen[tr.To]; !ok {
				seen[tr.To] = struct{}{}
				queue = append(queue, tr.To)
			}
		}
	}
	return seen, steps
}

// fromTLC reads a state TLC printed into a State.
func fromTLC(c Config, r tlc.Record) (State, error) {
	var s State
	var err error
	num := func(v tlc.Value) int8 {
		n, ok := v.(int64)
		if !ok && err == nil {
			err = fmt.Errorf("not an integer: %v", v)
		}
		return int8(n)
	}
	flag := func(v tlc.Value) bool {
		b, ok := v.(bool)
		if !ok && err == nil {
			err = fmt.Errorf("not a boolean: %v", v)
		}
		return b
	}
	rec := func(v tlc.Value) tlc.Record {
		x, ok := v.(tlc.Record)
		if !ok && err == nil {
			err = fmt.Errorf("not a record: %v", v)
		}
		return x
	}
	if len(r) != 15 {
		return s, fmt.Errorf("%d variables, not 15", len(r))
	}
	s.Now = num(r["now"])
	lease := rec(r["lease"])
	switch lease["state"] {
	case "open":
		s.Lease.State = Open
	case "draining":
		s.Lease.State = Draining
	case "closed":
		s.Lease.State = Closed
	default:
		return s, fmt.Errorf("lease state %v", lease["state"])
	}
	s.Lease.Epoch, s.Lease.Expiry, s.Lease.Revoked = num(lease["epoch"]), num(lease["expiry"]), flag(lease["revoked"])
	s.Epoch, s.Has, s.Known = num(r["epoch"]), flag(r["has"]), num(r["known"])
	s.Stopped, s.Answer = flag(r["stopped"]), num(r["answer"])
	holds, ok := r["holds"].(tlc.Seq)
	if !ok || len(holds) != c.MaxHolds {
		return s, fmt.Errorf("holds is not a sequence of %d: %v", c.MaxHolds, r["holds"])
	}
	for i := range s.Holds {
		s.Holds[i] = NoHold
	}
	for i, v := range holds {
		h := rec(v)
		s.Holds[i] = Hold{
			Open: flag(h["open"]), Epoch: num(h["epoch"]), Life: num(h["life"]),
			UnderOpen: flag(h["underOpen"]), InPauseAge: flag(h["inPauseAge"]),
			InRevokeWindow: flag(h["inRevokeWindow"]), Late: flag(h["late"]),
		}
	}
	switch r["handoff"] {
	case "none":
		s.Handoff = NoHandoff
	case "partial":
		s.Handoff = Partial
	case "complete":
		s.Handoff = Complete
	default:
		return s, fmt.Errorf("handoff %v", r["handoff"])
	}
	listed, ok := r["listed"].(tlc.Set)
	if !ok {
		return s, fmt.Errorf("listed is not a set: %v", r["listed"])
	}
	for _, v := range listed {
		s.Listed |= 1 << (num(v) - 1)
	}
	s.AudRead, s.Paused, s.PausedFor = num(r["audRead"]), flag(r["paused"]), num(r["pausedFor"])
	view := rec(r["view"])
	s.View = View{Paused: flag(view["paused"]), Age: num(view["age"])}
	s.RevokedFor = num(r["revokedFor"])
	return s, err
}

// compare holds the shadow's steps, from Init by next, against TLC's graph.
// It returns what differs: a state or a step one has and the other does not.
func compare(t *testing.T, c Config, g *tlc.Graph, next func(State) []Transition) []string {
	t.Helper()
	states := map[string]State{}
	for fp, v := range g.States {
		s, err := fromTLC(c, v.(tlc.Record))
		if err != nil {
			t.Fatalf("state %s: %v", fp, err)
		}
		states[fp] = s
	}
	theirs := map[step]struct{}{}
	for _, e := range g.Edges {
		theirs[step{states[e.From], e.Action, states[e.To]}] = struct{}{}
	}
	theirStates := map[State]struct{}{}
	for _, s := range states {
		theirStates[s] = struct{}{}
	}
	var diffs []string
	if len(g.Init) != 1 || states[g.Init[0]] != c.Init() {
		diffs = append(diffs, fmt.Sprintf("TLC's initial states %v are not the shadow's Init", g.Init))
	}
	ours, steps := explore(t, c, next, true)
	for s := range ours {
		if _, ok := theirStates[s]; !ok {
			diffs = append(diffs, fmt.Sprintf("the shadow reaches a state TLC does not: %+v", s))
		}
	}
	for s := range theirStates {
		if _, ok := ours[s]; !ok {
			diffs = append(diffs, fmt.Sprintf("TLC reaches a state the shadow does not: %+v", s))
		}
	}
	for st := range steps {
		if _, ok := theirs[st]; !ok {
			diffs = append(diffs, fmt.Sprintf("the shadow takes %s where TLC does not, from %+v", st.action, st.from))
		}
	}
	for st := range theirs {
		if _, ok := steps[st]; !ok {
			diffs = append(diffs, fmt.Sprintf("TLC takes %s where the shadow does not, from %+v", st.action, st.from))
		}
	}
	sort.Strings(diffs)
	return diffs
}

// TestTransitionsMatchTLC holds every step of the shadow against TLC's state
// graph of a small instance: from every state, the same actions lead to the
// same states.
func TestTransitionsMatchTLC(t *testing.T) {
	g := smallTLCGraph(t)
	if diffs := compare(t, small, g, small.Next); len(diffs) > 0 {
		t.Fatalf("%d differences from TLC, the first: %v", len(diffs), diffs[:min(5, len(diffs))])
	}
	t.Logf("%d states and %d steps, as TLC has them", len(g.States), len(g.Edges))
}

// TestComparisonSeesADifference shows the comparison above is not vacuous: a
// shadow with one step too many, or one too few, fails it.
func TestComparisonSeesADifference(t *testing.T) {
	g := smallTLCGraph(t)
	cases := map[string]func(State) []Transition{
		"a step dropped": func(s State) []Transition {
			var out []Transition
			for _, tr := range small.Next(s) {
				if tr.Action != "AnswerLost" {
					out = append(out, tr)
				}
			}
			return out
		},
		"a step between reached states added": func(s State) []Transition {
			out := small.Next(s)
			if s.Paused {
				out = append(out, Transition{"Tick", s})
			}
			return out
		},
		"a step under another action's name": func(s State) []Transition {
			out := small.Next(s)
			for i := range out {
				if out[i].Action == "Revoke" {
					out[i].Action = "Pause"
				}
			}
			return out
		},
	}
	for name, next := range cases {
		if diffs := compare(t, small, g, next); len(diffs) == 0 {
			t.Errorf("%s: the comparison found no difference", name)
		}
	}
}

// TestStateCountMatchesTLC explores the instance proofs/LeaseLifecycle.cfg
// checks, and must reach as many distinct states as TLC does there, as its
// guard table records. Every invariant and step property is checked on the
// way.
func TestStateCountMatchesTLC(t *testing.T) {
	proofs, err := tlc.ProofsDir()
	if err != nil {
		t.Fatal(err)
	}
	text, err := os.ReadFile(filepath.Join(proofs, "LeaseLifecycle.cfg"))
	if err != nil {
		t.Fatal(err)
	}
	k, err := tlc.Constants(string(text))
	if err != nil {
		t.Fatal(err)
	}
	c := Config{
		MaxHolds: k["MaxHolds"], LeaseSize: k["LeaseSize"], Window: k["Window"], Skew: k["Skew"],
		MaxLife: k["MaxLife"], Grace: k["Grace"], CacheAge: k["CacheAge"], LastRenew: k["LastRenew"],
		MaxRestarts: k["MaxRestarts"],
	}
	if len(k) != 9 {
		t.Fatalf("LeaseLifecycle.cfg sets %d constants, not the 9 Config has: %v", len(k), k)
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	counts, err := tlc.GuardTableStates("LeaseLifecycle")
	if err != nil {
		t.Fatal(err)
	}
	want, ok := counts["LeaseLifecycle.cfg"]
	if !ok {
		t.Fatalf("the guard table gives no count for LeaseLifecycle.cfg: %v", counts)
	}
	seen, _ := explore(t, c, c.Next, false)
	if len(seen) != want {
		t.Fatalf("the shadow reaches %d distinct states, TLC %d", len(seen), want)
	}
}

// TestRandomWalksKeepTheInvariants walks an instance too large to explore, with
// more holds, a longer life and more restarts than the spec's .cfg, and checks
// every invariant and step property along each walk.
func TestRandomWalksKeepTheInvariants(t *testing.T) {
	c := Config{
		MaxHolds: 4, LeaseSize: 3, Window: 4, Skew: 2, MaxLife: 3, Grace: 3,
		CacheAge: 2, LastRenew: 8, MaxRestarts: 3,
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewPCG(1, 2))
	invariants, properties := c.Invariants(), StepProperties()
	closed := 0
	for walk := range 3000 {
		s := c.Init()
		for range 400 {
			for _, inv := range invariants {
				if !inv.Holds(s) {
					t.Fatalf("walk %d: %s does not hold in %+v", walk, inv.Name, s)
				}
			}
			next := c.Next(s)
			if len(next) == 0 {
				break
			}
			tr := next[rng.IntN(len(next))]
			for _, p := range properties {
				if !p.Holds(s, tr.To) {
					t.Fatalf("walk %d: %s does not hold on %s from %+v", walk, p.Name, tr.Action, s)
				}
			}
			s = tr.To
		}
		if s.Lease.State == Closed {
			closed++
		}
	}
	if closed == 0 {
		t.Fatal("no walk closed its lease: the walks are too short to say much")
	}
}

// TestValidateRefusesWhatTheSpecAssumesAway checks the ASSUME the shadow
// enforces.
func TestValidateRefusesWhatTheSpecAssumesAway(t *testing.T) {
	bad := small
	bad.Grace = bad.Skew - 1
	if bad.Validate() == nil {
		t.Error("a grace shorter than the skew is accepted")
	}
	bad = small
	bad.MaxHolds = MaxSlots + 1
	if bad.Validate() == nil {
		t.Error("more holds than slots are accepted")
	}
	if err := small.Validate(); err != nil {
		t.Errorf("the small instance is refused: %v", err)
	}
}
