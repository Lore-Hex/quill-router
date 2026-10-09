package terminalorder

import (
	"fmt"
	"math"
	"math/rand/v2"
	"strings"
	"sync"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// Instances for TLC to write whole state graphs of. One has a single
// authorization that is a declared stream, with front-door appends:
// heartbeats, the answer, the release, adoption and reaps. Another has two
// that do not stream: the per-authorization functions, and Close's look at
// every hold. The third gives the drain log rows of both, so that a hold's
// first row can come after another's: adoption, ApplyDrain and the
// acknowledged rows across authorizations, for the negative control;
// TerminalOrder.rows.cfg is it with the first hold listable. TLC reaches
// 13,322, 69,768 and 692,648 distinct states in them; the third is read as
// it streams (tlc.Compare).
var (
	oneStream = Config{
		Auths: []string{"a1"}, Stream: []bool{true}, Declared: []bool{true}, Listable: []bool{false}, MaxAppends: 2,
	}
	twoPlain = Config{
		Auths: []string{"a1", "a2"}, Stream: []bool{false, false}, Declared: []bool{false, false},
		Listable: []bool{false, false},
	}
	twoAppends = Config{
		Auths: []string{"a1", "a2"}, Stream: []bool{false, false}, Declared: []bool{false, false},
		Listable: []bool{false, false}, MaxAppends: 2,
	}
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
    Listable = %s
    MaxAppends = %d
INVARIANTS
    TypeOK
`, set(c.Auths, nil), set(c.Auths, c.Stream), set(c.Auths, c.Declared), set(c.Auths, c.Listable), c.MaxAppends)
}

// cfgInstances are the instances proofs/TerminalOrder*.cfg check, their
// constants written out here rather than read from the files.
// TestStateCountMatchesTLC binds each file to its instance (bind), and fails
// until a change to a file is made here too.
var cfgInstances = map[string]Config{
	"TerminalOrder.cfg": {
		Auths: []string{"a1", "a2"}, Stream: []bool{true, false}, Declared: []bool{true, false},
		Listable: []bool{false, false}, MaxAppends: 1,
	},
	"TerminalOrder.undeclared.cfg": {
		Auths: []string{"a1"}, Stream: []bool{true}, Declared: []bool{false}, Listable: []bool{false}, MaxAppends: 1,
	},
	"TerminalOrder.list.cfg": {
		Auths: []string{"a1"}, Stream: []bool{false}, Declared: []bool{false}, Listable: []bool{true}, MaxAppends: 1,
	},
	"TerminalOrder.appends.cfg": {
		Auths: []string{"a1"}, Stream: []bool{true}, Declared: []bool{true}, Listable: []bool{false}, MaxAppends: 2,
	},
	"TerminalOrder.rows.cfg": {
		Auths: []string{"a1", "a2"}, Stream: []bool{false, false}, Declared: []bool{false, false},
		Listable: []bool{true, false}, MaxAppends: 2,
	},
	"TerminalOrder.reaps.cfg": {
		Auths: []string{"a1", "a2"}, Stream: []bool{false, false}, Declared: []bool{false, false},
		Listable: []bool{true, true}, MaxAppends: 0,
	},
}

// specConstants are the constants TerminalOrder declares: a configuration of
// it assigns them, and nothing else.
var specConstants = []string{"Auths", "Streams", "Declared", "Listable", "MaxAppends"}

// bind has TLC check that proofs/<file> sets up c's model: TLC's parser finds
// the file assigns the spec's constants and sets nothing else that changes
// the graph TLC explores, and TLC finds the constants are c's (declares).
func bind(file string, c Config) error {
	proofs, err := tlc.ProofsDir()
	if err != nil {
		return err
	}
	return tlc.BindConfiguration(proofs, "TerminalOrder", file, specConstants, declares(c))
}

// declares is the formula that a configuration has c's constants, up to the
// names of its model values, which the spec treats alike: how many there are
// of each kind, a listable one's kind included.
func declares(c Config) string {
	streams, declared, listable, listedStreams, listedDeclared := 0, 0, 0, 0, 0
	for a := range c.Auths {
		if c.Stream[a] {
			streams++
		}
		if c.Declared[a] {
			declared++
		}
		if c.Listable[a] {
			listable++
			if c.Stream[a] {
				listedStreams++
			}
			if c.Declared[a] {
				listedDeclared++
			}
		}
	}
	return fmt.Sprintf("Cardinality(Auths) = %d /\\ Cardinality(Streams) = %d /\\ Cardinality(Declared) = %d /\\ "+
		"Declared \\subseteq Streams /\\ Streams \\subseteq Auths /\\ Cardinality(Listable) = %d /\\ "+
		"Cardinality(Listable \\cap Streams) = %d /\\ Cardinality(Listable \\cap Declared) = %d /\\ "+
		"Listable \\subseteq Auths /\\ MaxAppends = %d",
		len(c.Auths), streams, declared, listable, listedStreams, listedDeclared, c.MaxAppends)
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
		// TLC prints a function on no authorizations as the empty sequence.
		if seq, isSeq := v.(tlc.Seq); isSeq && len(seq) == 0 && len(c.Auths) == 0 {
			return
		}
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
		"gwAcked", "enc", "allowance", "got", "listed")
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
	got, ok := r["got"].(tlc.Set)
	if !ok {
		fail("got is not a set: %v", r["got"])
	}
	for _, v := range got {
		if a := auth(v); a >= 0 {
			if s.Got[a] {
				fail("got holds %v twice", v)
			}
			s.Got[a] = true
		}
	}
	listed, ok := r["listed"].(tlc.Set)
	if !ok {
		fail("listed is not a set: %v", r["listed"])
	}
	for _, v := range listed {
		if a := auth(v); a >= 0 {
			if s.Listed[a] {
				fail("listed holds %v twice", v)
			}
			s.Listed[a] = true
		}
	}
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

// shadowOf is the shadow tlc.Compare holds against TLC: from Init by next,
// with every invariant judged on each state.
func shadowOf(c Config, next func(State) []Transition) tlc.Shadow[State] {
	return tlc.Shadow[State]{
		Init: c.Init(),
		Next: func(s State) []tlc.Step[State] {
			out := []tlc.Step[State]{}
			for _, tr := range next(s) {
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
}

// compareWithTLC runs TLC on the instance and compares its whole state graph
// with the shadow's.
func compareWithTLC(t *testing.T, c Config, next func(State) []Transition) tlc.Comparison {
	t.Helper()
	spec, err := tlc.SpecText("TerminalOrder")
	if err != nil {
		t.Fatal(err)
	}
	var result tlc.Comparison
	err = tlc.DumpFile("TerminalOrder", spec, cfgText(c), func(path string) error {
		var err error
		result, err = tlc.Compare(path, func(r tlc.Record) (State, error) { return fromTLC(c, r) },
			shadowOf(c, next), 5)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return result
}

// TestUndefinedWhereTLCHasNoValue: where TLC would stop because an expression
// has no value, the shadow panics with Undefined rather than answer. These
// states are not reachable; the point is that a predicate is the spec's on
// them too.
func TestUndefinedWhereTLCHasNoValue(t *testing.T) {
	undefined := func(f func() bool) (ok bool) {
		defer func() {
			if r := recover(); r != nil {
				_, ok = r.(Undefined)
			}
		}()
		f()
		return false
	}
	acked := oneStream.Init()
	acked.Lease, acked.Acked = Closed, 1
	if !undefined(func() bool { return oneStream.NoStreamClosedOver(acked) }) {
		t.Error("NoStreamClosedOver reads past the end of the outbox and answers")
	}
	adopted := oneStream.Init()
	adopted.Outbox[0] = Rec{0, Settle, 1}
	adopted.OutboxLen = 1
	adopted.Winner[0] = Winner{Owner, 1}
	if !undefined(func() bool { return oneStream.AdoptionKeepsTheWinner(adopted) }) {
		t.Error("AdoptionKeepsTheWinner takes the least of an empty set of rows")
	}
	beyond := oneStream.Init()
	beyond.Outbox[0] = Rec{0, Reap, 0}
	beyond.OutboxLen, beyond.OwnerApplied = 1, 2
	beyond.Winner[0] = Winner{Owner, 1}
	if !undefined(func() bool { return oneStream.WinnerIsFirstInOrder(beyond) }) {
		t.Error("WinnerIsFirstInOrder builds OwnerTerms past the end of the outbox and answers")
	}
	if undefined(func() bool { return oneStream.TypeOK(oneStream.Init()) }) {
		t.Error("TypeOK has no value on Init")
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

// TestRandomWalksKeepTheInvariants walks an instance larger than the .cfg's:
// three authorizations, two of them streams, two of them listable, more
// appends.
func TestRandomWalksKeepTheInvariants(t *testing.T) {
	c := Config{
		Auths: []string{"a1", "a2", "a3"}, Stream: []bool{true, true, false}, Declared: []bool{true, false, false},
		Listable: []bool{false, true, true}, MaxAppends: 3,
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
	bad = twoPlain
	bad.Listable = []bool{true}
	if bad.Validate() == nil {
		t.Error("a configuration that says nothing of a2's listing is accepted")
	}
	bad = twoPlain
	bad.Auths = []string{"a1", "a1"}
	if bad.Validate() == nil {
		t.Error("one authorization named twice is accepted")
	}
	fits := oneStream
	fits.MaxAppends = MaxDrain - 1
	if err := fits.Validate(); err != nil {
		t.Errorf("one authorization and %d appends fit, and are refused: %v", fits.MaxAppends, err)
	}
	huge := oneStream
	huge.MaxAppends = math.MaxInt
	if huge.Validate() == nil {
		t.Error("appends whose sum with the authorizations overflows are accepted")
	}
	none := Config{}
	if err := none.Validate(); err != nil {
		t.Errorf("no authorizations at all is what the spec allows, and is refused: %v", err)
	}
	if err := twoPlain.Validate(); err != nil {
		t.Errorf("a small instance is refused: %v", err)
	}
}
