package auditorcommit

import (
	"fmt"
	"math/rand/v2"
	"strings"
	"sync"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/tlc"
)

// Small instances for TLC to write whole state graphs of, each bringing in
// what the others leave out: two members, a move of the lease between them,
// a record stored late and a front door's append; a record stored ahead of an
// earlier one, a raise and a lying owner; a record stored twice and a crash;
// two authorizations. TLC reaches 12,358, 11,760, 5,687 and 26,331 distinct
// states in them.
var (
	moved = Config{
		Auths: []string{"a1"}, Members: []string{"m1", "m2"}, MaxSeq: 1, MaxSnap: 1, MaxLate: 1,
		MaxAssign: 1, MaxAppend: 1, Grant: 4,
	}
	ahead = Config{
		Auths: []string{"a1"}, Members: []string{"m1"}, MaxSeq: 2, MaxSnap: 1, MaxAhead: 1, MaxRaise: 1,
		Lying: true, Grant: 4,
	}
	again = Config{
		Auths: []string{"a1"}, Members: []string{"m1"}, MaxSeq: 1, MaxSnap: 1, MaxDup: 1, MaxCrash: 1,
		Lying: true, Grant: 4,
	}
	twoAuths = Config{
		Auths: []string{"a1", "a2"}, Members: []string{"m1"}, MaxSeq: 2, MaxSnap: 1, MaxAppend: 1, Grant: 4,
	}
	// The lease moved twice, so it comes back to a member that held it
	// (13,770 states); a gap the log really has, with a crash and a lying
	// owner (13,936); two authorizations under two members (6,034).
	backAgain = Config{
		Auths: []string{"a1"}, Members: []string{"m1", "m2"}, MaxSeq: 1, MaxSnap: 1, MaxAssign: 2, Grant: 4,
	}
	gapCrash = Config{
		Auths: []string{"a1"}, Members: []string{"m1"}, MaxSeq: 2, MaxSnap: 1, MaxAhead: 1, MaxCrash: 1,
		Lying: true, Grant: 4,
	}
	twoByTwo = Config{
		Auths: []string{"a1", "a2"}, Members: []string{"m1", "m2"}, MaxSeq: 1, MaxSnap: 1, MaxAssign: 1, Grant: 4,
	}
)

// cfgInstances are the instances proofs/AuditorCommit*.cfg check, their
// constants written out here rather than read from the files.
// TestStateCountMatchesTLC has TLC check that each file's constants are these
// (declares), and fails until a change to a file is made here too. In each
// file m1 is the member named first, so it holds the lease at the start.
var cfgInstances = map[string]Config{
	"AuditorCommit.cfg": {
		Auths: []string{"a1"}, Members: []string{"m1", "m2"}, MaxSeq: 2, MaxSnap: 2, MaxLate: 1, MaxAssign: 1,
		MaxAppend: 1, Grant: 4,
	},
	"AuditorCommit.again.cfg": {
		Auths: []string{"a1"}, Members: []string{"m1", "m2"}, MaxSeq: 2, MaxSnap: 2, MaxLate: 1, MaxAssign: 2,
		Lying: true, Grant: 4,
	},
	"AuditorCommit.ahead.cfg": {
		Auths: []string{"a1"}, Members: []string{"m1"}, MaxSeq: 3, MaxSnap: 2, MaxAhead: 1, MaxRaise: 1,
		MaxCrash: 1, MaxAppend: 1, Lying: true, Grant: 4,
	},
	"AuditorCommit.lying.cfg": {
		Auths: []string{"a1"}, Members: []string{"m1"}, MaxSeq: 2, MaxSnap: 1, MaxDup: 1, MaxLate: 1,
		MaxCrash: 1, Lying: true, Grant: 4,
	},
	"AuditorCommit.two.cfg": {
		Auths: []string{"a1", "a2"}, Members: []string{"m1"}, MaxSeq: 3, MaxSnap: 1, MaxLate: 1, Grant: 4,
	},
}

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
		graphs[cfg], graphErr[cfg] = tlc.Dump("AuditorCommit", cfg)
	}
	if err := graphErr[cfg]; err != nil {
		t.Fatal(err)
	}
	return graphs[cfg]
}

// cfgText writes the instance's configuration: the members first, the holder
// first among them, then the authorizations. TLC orders model values as its
// configuration reader meets them, and CHOOSE picks the first, so the holder
// has to be the first model value written.
func cfgText(c Config) string {
	members := append([]string{c.Members[c.Holder]}, c.Members[:c.Holder]...)
	members = append(members, c.Members[c.Holder+1:]...)
	return fmt.Sprintf(`SPECIFICATION Spec
CONSTANTS
    Members = {%s}
    Auths = {%s}
    MaxSeq = %d
    MaxSnap = %d
    MaxDup = %d
    MaxAhead = %d
    MaxLate = %d
    MaxRaise = %d
    MaxAssign = %d
    MaxCrash = %d
    MaxAppend = %d
    Lying = %s
    Grant = %d
INVARIANTS
    TypeOK
`, strings.Join(members, ", "), strings.Join(c.Auths, ", "), c.MaxSeq, c.MaxSnap, c.MaxDup, c.MaxAhead,
		c.MaxLate, c.MaxRaise, c.MaxAssign, c.MaxCrash, c.MaxAppend, strings.ToUpper(fmt.Sprint(c.Lying)), c.Grant)
}

// declares is the formula that a configuration has c's constants, up to the
// names of its model values, which the spec treats alike but for the holder
// CHOOSE picks, the same up to renaming too.
func declares(c Config) string {
	lying := "FALSE"
	if c.Lying {
		lying = "TRUE"
	}
	return fmt.Sprintf("Cardinality(Auths) = %d /\\ Cardinality(Members) = %d /\\ Auths \\cap Members = {} /\\ "+
		"MaxSeq = %d /\\ MaxSnap = %d /\\ MaxDup = %d /\\ MaxAhead = %d /\\ MaxLate = %d /\\ MaxRaise = %d /\\ "+
		"MaxAssign = %d /\\ MaxCrash = %d /\\ MaxAppend = %d /\\ Lying = %s /\\ Grant = %d",
		len(c.Auths), len(c.Members), c.MaxSeq, c.MaxSnap, c.MaxDup, c.MaxAhead, c.MaxLate, c.MaxRaise,
		c.MaxAssign, c.MaxCrash, c.MaxAppend, lying, c.Grant)
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
			if !c.AuditsEachCheckpoint(s, tr.To) {
				fail("AuditsEachCheckpoint does not hold on %s from %+v", tr.Action, s)
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

// fromTLC reads a state TLC printed into a State, refusing anything a State
// would not hold exactly.
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
	index := func(v tlc.Value, names []string, what string) int8 {
		if m, ok := v.(tlc.ModelValue); ok {
			for i, name := range names {
				if string(m) == name {
					return int8(i)
				}
			}
		}
		fail("%v is no %s", v, what)
		return 0
	}
	recOf := func(v tlc.Value) Rec {
		x := rec(v, "k", "a", "c", "seq", "idx")
		kind := int8(-1)
		for k, name := range kindNames {
			if x["k"] == name {
				kind = k
			}
		}
		if kind < 0 {
			fail("record kind %v", x["k"])
		}
		a := int8(NoAuth)
		if x["a"] != "none" {
			a = index(x["a"], c.Auths, "authorization")
		}
		return Rec{kind, a, num(x["c"]), num(x["seq"]), num(x["idx"])}
	}
	seq := func(v tlc.Value, max int, into []Rec) int8 {
		xs, ok := v.(tlc.Seq)
		if !ok || len(xs) > max {
			fail("not a sequence of at most %d: %v", max, v)
			return 0
		}
		for i, x := range xs {
			into[i] = recOf(x)
		}
		return int8(len(xs))
	}
	function := func(v tlc.Value, names []string, what string, read func(tlc.Value, int8)) {
		// TLC prints a function on an empty set as the empty sequence.
		if seq, isSeq := v.(tlc.Seq); isSeq && len(seq) == 0 && len(names) == 0 {
			return
		}
		f, ok := v.(tlc.Func)
		if !ok || len(f) != len(names) {
			fail("not a function on the %ss: %v", what, v)
			return
		}
		seen := map[int8]bool{}
		for _, p := range f {
			i := index(p.Arg, names, what)
			if seen[i] {
				fail("a function with %v twice", p.Arg)
			}
			seen[i] = true
			read(p.Val, i)
		}
	}
	r = rec(r, "out", "nextSeq", "osnap", "ownerSum", "ownerDone", "log", "dups", "aheads", "ticked", "lates",
		"st", "ver", "prog", "booked", "alloc", "holds", "win", "osum", "raised", "S", "drain", "appends",
		"holder", "acked", "assigns", "mem", "pos", "dpos", "done", "crashes", "alert", "gap")
	s.OutLen = seq(r["out"], MaxOut, s.Out[:])
	s.NextSeq = num(r["nextSeq"])
	function(r["osnap"], c.Auths, "authorization", func(v tlc.Value, a int8) { s.Osnap[a] = num(v) })
	s.OwnerSum = num(r["ownerSum"])
	done, ok := r["ownerDone"].(tlc.Set)
	if !ok {
		fail("ownerDone is not a set: %v", r["ownerDone"])
	}
	for _, v := range done {
		s.OwnerDone |= 1 << index(v, c.Auths, "authorization")
	}
	s.LogLen = seq(r["log"], MaxLog, s.Log[:])
	s.Dups, s.Aheads, s.Ticked, s.Lates = num(r["dups"]), num(r["aheads"]), flag(r["ticked"]), num(r["lates"])
	s.St = -1
	for k, name := range stateNames {
		if r["st"] == name {
			s.St = k
		}
	}
	if s.St < 0 {
		fail("lease state %v", r["st"])
	}
	s.Ver, s.Prog, s.Booked, s.Alloc = num(r["ver"]), num(r["prog"]), num(r["booked"]), num(r["alloc"])
	function(r["holds"], c.Auths, "authorization", func(v tlc.Value, a int8) { s.Holds[a] = num(v) })
	function(r["win"], c.Auths, "authorization", func(v tlc.Value, a int8) { s.Win[a] = recOf(v) })
	s.Osum, s.Raised, s.S = num(r["osum"]), num(r["raised"]), num(r["S"])
	s.DrainLen = seq(r["drain"], MaxRows, s.Drain[:])
	s.Appends = num(r["appends"])
	s.Holder = index(r["holder"], c.Members, "member")
	s.Acked, s.Assigns = num(r["acked"]), num(r["assigns"])
	function(r["mem"], c.Members, "member", func(v tlc.Value, m int8) {
		x := rec(v, "loaded", "ver", "prog", "alloc", "holds", "win", "wl", "osum", "dbooked", "dirty", "S", "fault")
		mem := Mem{
			Loaded: flag(x["loaded"]), Ver: num(x["ver"]), Prog: num(x["prog"]), Alloc: num(x["alloc"]),
			WL: flag(x["wl"]), Osum: num(x["osum"]), Dbooked: num(x["dbooked"]), Dirty: flag(x["dirty"]),
			S: num(x["S"]), Fault: flag(x["fault"]),
		}
		function(x["holds"], c.Auths, "authorization", func(v tlc.Value, a int8) { mem.Holds[a] = num(v) })
		function(x["win"], c.Auths, "authorization", func(v tlc.Value, a int8) { mem.Win[a] = recOf(v) })
		s.Mem[m] = mem
	})
	function(r["pos"], c.Members, "member", func(v tlc.Value, m int8) { s.Pos[m] = num(v) })
	function(r["dpos"], c.Members, "member", func(v tlc.Value, m int8) { s.Dpos[m] = num(v) })
	function(r["done"], c.Members, "member", func(v tlc.Value, m int8) { s.Done[m] = num(v) })
	s.Crashes, s.Alert, s.Gap = num(r["crashes"]), flag(r["alert"]), flag(r["gap"])
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
// with every invariant and the step property judged.
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
			for _, tr := range next(s) {
				if !c.AuditsEachCheckpoint(s, tr.To) {
					return fmt.Errorf("AuditsEachCheckpoint does not hold on %s", tr.Action)
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
	return compareText(t, c, cfgText(c), next)
}

// compareText is compareWithTLC with the configuration's text as given.
func compareText(t *testing.T, c Config, cfg string, next func(State) []Transition) tlc.Comparison {
	t.Helper()
	spec, err := tlc.SpecText("AuditorCommit")
	if err != nil {
		t.Fatal(err)
	}
	var result tlc.Comparison
	err = tlc.DumpFile("AuditorCommit", spec, cfg, func(path string) error {
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

// TestMappingRefusesWhatAStateCannotHold: a variable more, a member's memory
// with a field more, a record whose authorization is the string and not the
// model value, two of TLC's states that read as one.
func TestMappingRefusesWhatAStateCannotHold(t *testing.T) {
	c := moved
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
	memExtra := copyOf(init)
	var f tlc.Func
	for _, p := range init["mem"].(tlc.Func) {
		m := copyOf(p.Val.(tlc.Record))
		m["late"] = false
		f = append(f, tlc.Pair{Arg: p.Arg, Val: m})
	}
	memExtra["mem"] = f
	if _, err := fromTLC(c, memExtra); err == nil {
		t.Error("a member's memory with a field more is read")
	}
	stringly := copyOf(init)
	stringly["out"] = tlc.Seq{tlc.Record{"k": "hb", "a": "a1", "c": int64(0), "seq": int64(1), "idx": int64(0)}}
	if _, err := fromTLC(c, stringly); err == nil {
		t.Error("a record for the string \"a1\" is read as one for the authorization a1")
	}
	twice := &tlc.Graph{States: map[string]tlc.Value{g.Init[0]: init, "twin": init}, Init: g.Init}
	if _, err := mapStates(c, twice); err == nil {
		t.Error("two of TLC's states that read as one State are accepted")
	}
}

// TestRandomWalksKeepTheInvariants walks an instance larger than any .cfg's:
// two authorizations and two members, with every kind of mischief at once.
func TestRandomWalksKeepTheInvariants(t *testing.T) {
	c := Config{
		Auths: []string{"a1", "a2"}, Members: []string{"m1", "m2"}, MaxSeq: 4, MaxSnap: 2, MaxDup: 1,
		MaxAhead: 1, MaxLate: 1, MaxRaise: 1, MaxAssign: 2, MaxCrash: 1, MaxAppend: 1, Lying: true, Grant: 6,
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewPCG(5, 6))
	invariants := c.Invariants()
	closed := 0
	for walk := range 2000 {
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
			tr := next[rng.IntN(len(next))]
			if !c.AuditsEachCheckpoint(s, tr.To) {
				t.Fatalf("walk %d: AuditsEachCheckpoint does not hold on %s from %+v", walk, tr.Action, s)
			}
			s = tr.To
		}
		if s.St == Closed {
			closed++
		}
	}
	if closed == 0 {
		t.Fatal("no walk closed its lease: the walks are too short to say much")
	}
}

func TestValidateRefusesWhatAStateCannotHold(t *testing.T) {
	bad := moved
	bad.Members = []string{"m1", "m2", "m3"}
	if bad.Validate() == nil {
		t.Error("more members than a State holds are accepted")
	}
	bad = moved
	bad.MaxSeq = MaxOut + 1
	if bad.Validate() == nil {
		t.Error("more records than a State holds are accepted")
	}
	bad = moved
	bad.Grant = 1 << 40
	if bad.Validate() == nil {
		t.Error("a grant that overflows is accepted")
	}
	bad = moved
	bad.Members = []string{"m1", "m1"}
	if bad.Validate() == nil {
		t.Error("one member named twice is accepted")
	}
	bad = twoAuths
	bad.Auths = []string{"a1", "a1"}
	if bad.Validate() == nil {
		t.Error("one authorization named twice is accepted")
	}
	bad = moved
	bad.Members = nil
	if bad.Validate() == nil {
		t.Error("no members, which the spec assumes away, is accepted")
	}
	none := moved
	none.Auths = nil
	if err := none.Validate(); err != nil {
		t.Errorf("no authorizations is what the spec allows, and is refused: %v", err)
	}
}

// TestUndefinedWhereTLCHasNoValue: reading a sequence outside its records
// panics with Undefined, where TLC would stop, rather than answer.
func TestUndefinedWhereTLCHasNoValue(t *testing.T) {
	undefined := func(f func()) (ok bool) {
		defer func() {
			if r := recover(); r != nil {
				_, ok = r.(Undefined)
			}
		}()
		f()
		return false
	}
	s := moved.Init()
	for name, read := range map[string]func(){
		"log[1] of none":   func() { s.log(1) },
		"log[0]":           func() { s.log(0) },
		"drain[1] of none": func() { s.drain(1) },
	} {
		if !undefined(read) {
			t.Errorf("%s is read", name)
		}
	}
	if undefined(func() { moved.TypeOK(s) }) {
		t.Error("TypeOK has no value on Init")
	}
}
