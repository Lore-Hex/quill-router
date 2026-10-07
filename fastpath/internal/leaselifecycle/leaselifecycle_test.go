package leaselifecycle

import (
	"fmt"
	"math"
	"math/rand/v2"
	"sort"
	"strings"
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

// fromTLC reads a state TLC printed into a State. It refuses anything a State
// would not hold exactly: a missing or extra variable or record field, an
// integer outside int8, a value of the wrong kind. Otherwise two states TLC
// tells apart could become one State, and the comparison would not see it.
func fromTLC(c Config, r tlc.Record) (State, error) {
	var s State
	var err error
	fail := func(format string, args ...any) {
		if err == nil {
			err = fmt.Errorf(format, args...)
		}
	}
	num := func(v tlc.Value) int8 {
		n, ok := v.(int64)
		if !ok || n < -128 || n > 127 {
			fail("not an int8: %v", v)
		}
		return int8(n)
	}
	flag := func(v tlc.Value) bool {
		b, ok := v.(bool)
		if !ok {
			fail("not a boolean: %v", v)
		}
		return b
	}
	rec := func(v tlc.Value, fields ...string) tlc.Record {
		x, ok := v.(tlc.Record)
		if !ok || len(x) != len(fields) {
			fail("not a record of %v: %v", fields, v)
			return tlc.Record{}
		}
		for _, f := range fields {
			if _, ok := x[f]; !ok {
				fail("a record without %s: %v", f, v)
			}
		}
		return x
	}
	r = rec(r, "now", "lease", "epoch", "has", "known", "stopped", "answer", "holds", "handoff", "listed",
		"audRead", "paused", "pausedFor", "view", "revokedFor")
	s.Now = num(r["now"])
	lease := rec(r["lease"], "state", "epoch", "expiry", "revoked")
	switch lease["state"] {
	case "open":
		s.Lease.State = Open
	case "draining":
		s.Lease.State = Draining
	case "closed":
		s.Lease.State = Closed
	default:
		fail("lease state %v", lease["state"])
	}
	s.Lease.Epoch, s.Lease.Expiry, s.Lease.Revoked = num(lease["epoch"]), num(lease["expiry"]), flag(lease["revoked"])
	s.Epoch, s.Has, s.Known = num(r["epoch"]), flag(r["has"]), num(r["known"])
	s.Stopped, s.Answer = flag(r["stopped"]), num(r["answer"])
	holds, ok := r["holds"].(tlc.Seq)
	if !ok || len(holds) != c.MaxHolds {
		fail("holds is not a sequence of %d: %v", c.MaxHolds, r["holds"])
	}
	for i := range s.Holds {
		s.Holds[i] = NoHold
	}
	for i, v := range holds {
		if i >= MaxSlots {
			break
		}
		h := rec(v, "open", "epoch", "life", "underOpen", "inPauseAge", "inRevokeWindow", "late")
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
		fail("handoff %v", r["handoff"])
	}
	listed, ok := r["listed"].(tlc.Set)
	if !ok {
		fail("listed is not a set: %v", r["listed"])
	}
	for _, v := range listed {
		h := num(v)
		if h < 1 || int(h) > c.MaxHolds {
			fail("listed names no hold: %v", v)
			continue
		}
		s.Listed |= 1 << (h - 1)
	}
	s.AudRead, s.Paused, s.PausedFor = num(r["audRead"]), flag(r["paused"]), num(r["pausedFor"])
	view := rec(r["view"], "paused", "age")
	s.View = View{Paused: flag(view["paused"]), Age: num(view["age"])}
	s.RevokedFor = num(r["revokedFor"])
	return s, err
}

// mapStates reads every state of TLC's graph into a State, one to one.
func mapStates(c Config, g *tlc.Graph) (map[string]State, error) {
	states := map[string]State{}
	byState := map[State]string{}
	for fp, v := range g.States {
		r, ok := v.(tlc.Record)
		if !ok {
			return nil, fmt.Errorf("state %s is not a record of variables", fp)
		}
		s, err := fromTLC(c, r)
		if err != nil {
			return nil, fmt.Errorf("state %s: %w", fp, err)
		}
		if other, dup := byState[s]; dup {
			return nil, fmt.Errorf("TLC's states %s and %s are one State here: the mapping loses something", other, fp)
		}
		states[fp], byState[s] = s, fp
	}
	return states, nil
}

// compare holds the shadow's steps, from Init by next, against TLC's graph.
// It returns what differs: a state or a step one has and the other does not.
func compare(t *testing.T, c Config, g *tlc.Graph, next func(State) []Transition) []string {
	t.Helper()
	states, err := mapStates(c, g)
	if err != nil {
		t.Fatal(err)
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

// TestMappingRefusesAStateItCannotHold runs TLC on a copy of the spec whose
// lease row has a field more, which OwnerRenew turns over. The shadow knows
// nothing of it, so reading TLC's states must fail, rather than fold the
// states the field tells apart into one and compare as if nothing differed.
func TestMappingRefusesAStateItCannotHold(t *testing.T) {
	spec, err := tlc.SpecText("LeaseLifecycle")
	if err != nil {
		t.Fatal(err)
	}
	for _, edit := range [][2]string{
		{`/\ lease = [state |-> "open", epoch |-> 0, expiry |-> Window,
                revoked |-> FALSE]`,
			`/\ lease = [state |-> "open", epoch |-> 0, expiry |-> Window,
                revoked |-> FALSE, turn |-> 0]`},
		{`/\ lease' = [lease EXCEPT !.expiry = now + Window]
    /\ answer' = now + Window`,
			`/\ lease' = [lease EXCEPT !.expiry = now + Window, !.turn = 1 - @]
    /\ answer' = now + Window`},
	} {
		if strings.Count(spec, edit[0]) != 1 {
			t.Fatalf("the spec no longer has %q once", edit[0])
		}
		spec = strings.Replace(spec, edit[0], edit[1], 1)
	}
	g, err := tlc.DumpText("LeaseLifecycle", spec, cfgText(small))
	if err != nil {
		t.Fatal(err)
	}
	if len(g.States) <= len(smallTLCGraph(t).States) {
		t.Fatalf("the changed spec reaches %d states, no more than the spec's %d: the change did nothing",
			len(g.States), len(smallTLCGraph(t).States))
	}
	if _, err := mapStates(small, g); err == nil {
		t.Fatal("states with a field the shadow does not hold are read as the shadow's")
	}
}

// TestMappingHasTwoChecks holds each of mapStates' checks to a case only it
// catches: a field the shadow does not hold whose value never changes, which
// folds no two states together, and two of TLC's states that read as one.
func TestMappingHasTwoChecks(t *testing.T) {
	g := smallTLCGraph(t)
	var fp string
	var init tlc.Record
	for _, f := range g.Init {
		fp, init = f, g.States[f].(tlc.Record)
	}
	withExtra := tlc.Record{}
	for k, v := range init {
		withExtra[k] = v
	}
	lease := tlc.Record{"constant": int64(0)}
	for k, v := range init["lease"].(tlc.Record) {
		lease[k] = v
	}
	withExtra["lease"] = lease
	if _, err := fromTLC(small, withExtra); err == nil {
		t.Error("a lease row with a field the shadow does not hold is read")
	}
	twice := &tlc.Graph{States: map[string]tlc.Value{fp: init, "twin": init}, Init: []string{fp}}
	if _, err := mapStates(small, twice); err == nil {
		t.Error("two of TLC's states that read as one State are accepted")
	}
}

// TestTypeOKRefusesWhatTheSpecsTypesDoNot checks the bounds TypeOK has that
// the Go types do not: the lease's state and the hand-off are enumerations.
func TestTypeOKRefusesWhatTheSpecsTypesDoNot(t *testing.T) {
	s := small.Init()
	if !small.TypeOK(s) {
		t.Fatal("Init is refused")
	}
	for name, bad := range map[string]State{
		"a lease state below open":   func() State { b := s; b.Lease.State = Open - 1; return b }(),
		"a lease state past closed":  func() State { b := s; b.Lease.State = Closed + 1; return b }(),
		"a hand-off below none":      func() State { b := s; b.Handoff = NoHandoff - 1; return b }(),
		"a hand-off past complete":   func() State { b := s; b.Handoff = Complete + 1; return b }(),
		"a listed hold that is shut": func() State { b := s; b.Listed = 1; return b }(),
	} {
		if small.TypeOK(bad) {
			t.Errorf("%s passes TypeOK", name)
		}
	}
}

// cfgInstance is the instance proofs/LeaseLifecycle.cfg checks, its constants
// written out here rather than read from the file. TestStateCountMatchesTLC
// binds the file to it (bind), and fails until a change to the file is made
// here too.
var cfgInstance = Config{
	MaxHolds: 3, LeaseSize: 2, Window: 3, Skew: 2, MaxLife: 2, Grace: 2, CacheAge: 1, LastRenew: 3, MaxRestarts: 2,
}

// TestStateCountMatchesTLC explores the instance proofs/LeaseLifecycle.cfg
// checks, and must reach as many distinct states as TLC does there, as its
// guard table records. Every invariant and step property is checked on the
// way.
func TestStateCountMatchesTLC(t *testing.T) {
	if err := cfgInstance.Validate(); err != nil {
		t.Fatal(err)
	}
	if err := bind("LeaseLifecycle.cfg", cfgInstance); err != nil {
		t.Fatal(err)
	}
	counts, err := tlc.GuardTableStates("LeaseLifecycle")
	if err != nil {
		t.Fatal(err)
	}
	want, ok := counts["LeaseLifecycle.cfg"]
	if !ok || len(counts) != 1 {
		t.Fatalf("the guard table counts %v, not LeaseLifecycle.cfg alone", counts)
	}
	seen, _ := explore(t, cfgInstance, cfgInstance.Next, false)
	if len(seen) != want {
		t.Fatalf("the shadow reaches %d distinct states, TLC %d", len(seen), want)
	}
}

// specConstants are the constants LeaseLifecycle declares: a configuration of
// it assigns them, and nothing else.
var specConstants = []string{
	"MaxHolds", "LeaseSize", "Window", "Skew", "MaxLife", "Grace", "CacheAge", "LastRenew", "MaxRestarts",
}

// bind has TLC check that proofs/<file> sets up c's model: TLC's parser finds
// the file assigns the spec's constants and sets nothing else that changes
// the graph TLC explores, and TLC finds the constants are c's (declares).
func bind(file string, c Config) error {
	proofs, err := tlc.ProofsDir()
	if err != nil {
		return err
	}
	return tlc.BindConfiguration(proofs, "LeaseLifecycle", file, specConstants, declares(c))
}

// declares is the formula that a configuration has c's constants.
func declares(c Config) string {
	return fmt.Sprintf("MaxHolds = %d /\\ LeaseSize = %d /\\ Window = %d /\\ Skew = %d /\\ MaxLife = %d /\\ "+
		"Grace = %d /\\ CacheAge = %d /\\ LastRenew = %d /\\ MaxRestarts = %d",
		c.MaxHolds, c.LeaseSize, c.Window, c.Skew, c.MaxLife, c.Grace, c.CacheAge, c.LastRenew, c.MaxRestarts)
}

// TestDeclarationsAreBoundToTheirFiles: a declaration that differs from its
// .cfg in one constant is refused, even where the two reach as many states.
// With MaxHolds 2 the model reaches the same 522,054 states as the file's 3,
// since LeaseSize 2 bounds the open holds anyway, so no count would see it;
// TLC reading the file does.
func TestDeclarationsAreBoundToTheirFiles(t *testing.T) {
	wrong := cfgInstance
	wrong.MaxHolds = 2
	if err := bind("LeaseLifecycle.cfg", wrong); err == nil {
		t.Fatal("a declaration with MaxHolds 2 is taken for LeaseLifecycle.cfg's")
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
	bad = small
	bad.MaxRestarts = 128
	if bad.Validate() == nil {
		t.Error("more restarts than an epoch holds are accepted")
	}
	for name, set := range map[string]func(*Config){
		"a window whose sums overflow":    func(c *Config) { c.Window = math.MaxInt },
		"a cache age whose sum overflows": func(c *Config) { c.CacheAge = math.MaxInt },
		"a life whose sums overflow":      func(c *Config) { c.MaxLife = math.MaxInt },
	} {
		bad = small
		set(&bad)
		if bad.Validate() == nil {
			t.Errorf("%s is accepted", name)
		}
	}
	if err := small.Validate(); err != nil {
		t.Errorf("the small instance is refused: %v", err)
	}
}
