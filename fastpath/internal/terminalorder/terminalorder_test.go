package terminalorder

import (
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// Two instances small enough for TLC to write their whole state graphs. One
// has a single authorization that is a declared stream, with front-door
// appends: heartbeats, the release, adoption and reaps. The other has two
// that do not stream: the per-authorization functions, and Close's look at
// every hold. TLC reaches 4,742 and 46,728 distinct states in them.
var (
	oneStream = Config{Auths: []string{"a1"}, Stream: []bool{true}, Declared: []bool{true}, MaxAppends: 2}
	twoPlain  = Config{Auths: []string{"a1", "a2"}, Stream: []bool{false, false}, Declared: []bool{false, false}}
)

var (
	graphs   = map[string]*tlc.Graph{}
	graphErr = map[string]error{}
	graphMu  sync.Mutex
)

func tlcGraph(t *testing.T, c Config) *tlc.Graph {
	t.Helper()
	cfg := cfgText(c)
	graphMu.Lock()
	defer graphMu.Unlock()
	if _, ok := graphs[cfg]; !ok && graphErr[cfg] == nil {
		graphs[cfg], graphErr[cfg] = tlc.Dump("TerminalOrder", cfg)
	}
	if err := graphErr[cfg]; err != nil {
		t.Fatal(err)
	}
	return graphs[cfg]
}

func set(names []string, member []bool) string {
	var in []string
	for i, n := range names {
		if member == nil || member[i] {
			in = append(in, n)
		}
	}
	return "{" + strings.Join(in, ", ") + "}"
}

func cfgText(c Config) string {
	return fmt.Sprintf(`SPECIFICATION Spec
CONSTANTS
    Auths = %s
    Streams = %s
    Declared = %s
    MaxAppends = %d
INVARIANTS
    TypeOK
`, set(c.Auths, nil), set(c.Auths, c.Stream), set(c.Auths, c.Declared), c.MaxAppends)
}

// configOf reads a .cfg's constants into a Config, its authorizations sorted
// by name.
func configOf(t *testing.T, cfgFile string) Config {
	t.Helper()
	proofs, err := tlc.ProofsDir()
	if err != nil {
		t.Fatal(err)
	}
	text, err := os.ReadFile(filepath.Join(proofs, cfgFile))
	if err != nil {
		t.Fatal(err)
	}
	k, err := tlc.ConstantValues(string(text))
	if err != nil {
		t.Fatal(err)
	}
	if len(k) != 4 {
		t.Fatalf("%s sets %d constants, not the spec's 4: %v", cfgFile, len(k), k)
	}
	names := func(name string) map[string]bool {
		s, ok := k[name].(tlc.Set)
		if !ok {
			t.Fatalf("%s is not a set: %v", name, k[name])
		}
		out := map[string]bool{}
		for _, v := range s {
			m, ok := v.(tlc.ModelValue)
			if !ok {
				t.Fatalf("%s holds %v, which is no model value", name, v)
			}
			out[string(m)] = true
		}
		return out
	}
	auths, streams, declared := names("Auths"), names("Streams"), names("Declared")
	var c Config
	for a := range auths {
		c.Auths = append(c.Auths, a)
	}
	sort.Strings(c.Auths)
	for _, a := range c.Auths {
		c.Stream = append(c.Stream, streams[a])
		c.Declared = append(c.Declared, declared[a])
	}
	appends, ok := k["MaxAppends"].(int64)
	if !ok {
		t.Fatalf("MaxAppends is %v", k["MaxAppends"])
	}
	c.MaxAppends = int(appends)
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	return c
}

type step struct {
	from   State
	action string
	to     State
}

func explore(t *testing.T, c Config, next func(State) []Transition, keep bool) (map[State]struct{}, map[step]struct{}) {
	t.Helper()
	invariants := c.Invariants()
	seen := map[State]struct{}{}
	steps := map[step]struct{}{}
	queue := []State{c.Init()}
	seen[queue[0]] = struct{}{}
	failures := 0
	for len(queue) > 0 {
		s := queue[0]
		queue = queue[1:]
		for _, inv := range invariants {
			if !inv.Holds(s) {
				if failures < 5 {
					t.Errorf("%s does not hold in %+v", inv.Name, s)
				}
				failures++
			}
		}
		for _, tr := range next(s) {
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

// fromTLC reads a state TLC printed into a State. Like leaselifecycle's, it
// refuses anything a State would not hold exactly.
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
	enum := func(v tlc.Value, names map[int8]string) int8 {
		for k, name := range names {
			if v == name {
				return k
			}
		}
		fail("%v is none of %v", v, names)
		return -1
	}
	auth := func(v tlc.Value) int8 {
		m, ok := v.(tlc.ModelValue)
		if ok {
			for i, name := range c.Auths {
				if string(m) == name {
					return int8(i)
				}
			}
		}
		fail("%v is no authorization", v)
		return -1
	}
	perAuth := func(v tlc.Value, read func(tlc.Value, int8)) {
		f, ok := v.(tlc.Func)
		if !ok || len(f) != len(c.Auths) {
			fail("not a function on the authorizations: %v", v)
			return
		}
		seen := map[int8]bool{}
		for _, p := range f {
			a := auth(p.Arg)
			if a < 0 || seen[a] {
				fail("a function on the authorizations with %v twice or not at all", p.Arg)
				return
			}
			seen[a] = true
			read(p.Val, a)
		}
	}
	winner := func(v tlc.Value) Winner {
		w := rec(v, "src", "idx")
		return Winner{enum(w["src"], srcNames), num(w["idx"])}
	}
	r = rec(r, "lease", "ownerUp", "ownerCutoff", "issuedAtCutoff", "deadlinePassed", "outbox", "delivered",
		"acked", "tickAt", "S", "drain", "appends", "ownerApplied", "drainApplied", "winner", "ownerWinner",
		"gwAcked", "enc", "allowance")
	s.Lease = enum(r["lease"], leaseNames)
	s.OwnerUp, s.OwnerCutoff = flag(r["ownerUp"]), flag(r["ownerCutoff"])
	s.IssuedAtCutoff, s.DeadlinePassed = num(r["issuedAtCutoff"]), flag(r["deadlinePassed"])
	outbox, ok := r["outbox"].(tlc.Seq)
	if !ok || len(outbox) > MaxOutbox {
		fail("outbox is not a sequence of at most %d: %v", MaxOutbox, r["outbox"])
		outbox = nil
	}
	for i, v := range outbox {
		x := rec(v, "auth", "kind", "row")
		s.Outbox[i] = Rec{auth(x["auth"]), enum(x["kind"], kindNames), num(x["row"])}
	}
	s.OutboxLen = int8(len(outbox))
	s.Delivered, s.Acked, s.TickAt, s.S = num(r["delivered"]), num(r["acked"]), num(r["tickAt"]), num(r["S"])
	drain, ok := r["drain"].(tlc.Seq)
	if !ok || len(drain) > MaxDrain {
		fail("drain is not a sequence of at most %d: %v", MaxDrain, r["drain"])
		drain = nil
	}
	for j, v := range drain {
		x := rec(v, "auth", "kind")
		s.Drain[j] = Row{auth(x["auth"]), enum(x["kind"], kindNames)}
	}
	s.DrainLen = int8(len(drain))
	s.Appends, s.OwnerApplied, s.DrainApplied = num(r["appends"]), num(r["ownerApplied"]), num(r["drainApplied"])
	perAuth(r["winner"], func(v tlc.Value, a int8) { s.Winner[a] = winner(v) })
	perAuth(r["ownerWinner"], func(v tlc.Value, a int8) { s.OwnerWinner[a] = num(v) })
	perAuth(r["enc"], func(v tlc.Value, a int8) { s.Enc[a] = enum(v, encNames) })
	perAuth(r["allowance"], func(v tlc.Value, a int8) { s.Allowance[a] = flag(v) })
	acked, ok := r["gwAcked"].(tlc.Set)
	if !ok {
		fail("gwAcked is not a set: %v", r["gwAcked"])
	}
	for _, v := range acked {
		w := winner(v)
		switch {
		case w.Src == Owner && w.Idx >= 1 && w.Idx <= 16:
			s.GwAckedOwner |= 1 << (w.Idx - 1)
		case w.Src == Drain && w.Idx >= 1 && w.Idx <= 16:
			s.GwAckedDrain |= 1 << (w.Idx - 1)
		default:
			fail("gwAcked holds %v, which this State cannot", v)
		}
	}
	return s, err
}

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
// graphs of the two small instances.
func TestTransitionsMatchTLC(t *testing.T) {
	for name, c := range map[string]Config{"one stream": oneStream, "two plain": twoPlain} {
		g := tlcGraph(t, c)
		if diffs := compare(t, c, g, c.Next); len(diffs) > 0 {
			t.Errorf("%s: %d differences from TLC, the first: %v", name, len(diffs), diffs[:min(5, len(diffs))])
		}
		t.Logf("%s: %d states and %d steps, as TLC has them", name, len(g.States), len(g.Edges))
	}
}

// TestComparisonSeesADifference shows the comparison is not vacuous.
func TestComparisonSeesADifference(t *testing.T) {
	c := oneStream
	g := tlcGraph(t, c)
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
		if diffs := compare(t, c, g, next); len(diffs) == 0 {
			t.Errorf("%s: the comparison found no difference", name)
		}
	}
}

// TestMappingRefusesWhatAStateCannotHold: a record with a field more, a
// function missing an authorization, two of TLC's states that read as one.
func TestMappingRefusesWhatAStateCannotHold(t *testing.T) {
	c := twoPlain
	g := tlcGraph(t, c)
	init := g.States[g.Init[0]].(tlc.Record)
	if _, err := fromTLC(c, init); err != nil {
		t.Fatalf("the initial state is refused: %v", err)
	}
	copyOf := func(r tlc.Record) tlc.Record {
		out := tlc.Record{}
		for k, v := range r {
			out[k] = v
		}
		return out
	}
	extra := copyOf(init)
	extra["turn"] = int64(0)
	if _, err := fromTLC(c, extra); err == nil {
		t.Error("a state with a variable more is read")
	}
	short := copyOf(init)
	short["enc"] = init["enc"].(tlc.Func)[:1]
	if _, err := fromTLC(c, short); err == nil {
		t.Error("a function missing an authorization is read")
	}
	stringly := copyOf(init)
	f := tlc.Func{}
	for _, p := range init["allowance"].(tlc.Func) {
		f = append(f, tlc.Pair{Arg: string(p.Arg.(tlc.ModelValue)), Val: p.Val})
	}
	stringly["allowance"] = f
	if _, err := fromTLC(c, stringly); err == nil {
		t.Error("a function on strings is read as one on the authorizations")
	}
	twice := &tlc.Graph{States: map[string]tlc.Value{g.Init[0]: init, "twin": init}, Init: g.Init}
	if _, err := mapStates(c, twice); err == nil {
		t.Error("two of TLC's states that read as one State are accepted")
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

// TestRandomWalksKeepTheInvariants walks an instance larger than the .cfg's:
// three authorizations, two of them streams, more appends.
func TestRandomWalksKeepTheInvariants(t *testing.T) {
	c := Config{
		Auths: []string{"a1", "a2", "a3"}, Stream: []bool{true, true, false}, Declared: []bool{true, false, false},
		MaxAppends: 3,
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewPCG(3, 4))
	invariants := c.Invariants()
	closed := 0
	for walk := range 3000 {
		s := c.Init()
		for range 300 {
			for _, inv := range invariants {
				if !inv.Holds(s) {
					t.Fatalf("walk %d: %s does not hold in %+v", walk, inv.Name, s)
				}
			}
			next := c.Next(s)
			if len(next) == 0 {
				break
			}
			s = next[rng.IntN(len(next))].To
		}
		if s.Lease == Closed {
			closed++
		}
	}
	if closed == 0 {
		t.Fatal("no walk closed its lease: the walks are too short to say much")
	}
}

func TestValidateRefusesWhatTheSpecAssumesAway(t *testing.T) {
	bad := oneStream
	bad.Stream = []bool{false}
	if bad.Validate() == nil {
		t.Error("a declared authorization that is no stream is accepted")
	}
	bad = oneStream
	bad.MaxAppends = MaxDrain
	if bad.Validate() == nil {
		t.Error("more appends than the drain log holds are accepted")
	}
	if err := twoPlain.Validate(); err != nil {
		t.Errorf("a small instance is refused: %v", err)
	}
}
