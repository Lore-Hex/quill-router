package tlc

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseStateReadsWhatTLCPrints(t *testing.T) {
	text := "/\\ now = 0\n/\\ listed = {2, 1}\n/\\ lease = [epoch |-> 0, state |-> \"open\", revoked |-> FALSE]\n" +
		"/\\ holds = << [ open |-> TRUE,\n     life |-> -1 ],\n   [ open |-> FALSE, life |-> 0 ] >>"
	s, err := ParseState(text)
	if err != nil {
		t.Fatal(err)
	}
	want := Record{
		"now":    int64(0),
		"listed": Set{int64(1), int64(2)},
		"lease":  Record{"epoch": int64(0), "state": "open", "revoked": false},
		"holds": Seq{
			Record{"open": true, "life": int64(-1)},
			Record{"open": false, "life": int64(0)},
		},
	}
	if !Equal(s, want) {
		t.Fatalf("got %s\nwant %s", Key(s), Key(want))
	}
}

func TestEqualTellsValuesApart(t *testing.T) {
	cases := []struct {
		a, b Value
		same bool
	}{
		{Set{int64(1), int64(2)}, Set{int64(2), int64(1)}, true},
		{Seq{int64(1), int64(2)}, Seq{int64(2), int64(1)}, false},
		{"1", int64(1), false},
		{Record{"a": true}, Record{"a": false}, false},
		{Set{}, Seq{}, false},
	}
	for _, c := range cases {
		if Equal(c.a, c.b) != c.same {
			t.Errorf("Equal(%s, %s) is %v", Key(c.a), Key(c.b), !c.same)
		}
	}
}

func TestParseValueReadsFunctionsAndModelValues(t *testing.T) {
	v, err := ParseValue(`(a1 :> [src |-> "none", idx |-> 0] @@ a2 :> "open")`)
	if err != nil {
		t.Fatal(err)
	}
	f, ok := v.(Func)
	if !ok || len(f) != 2 {
		t.Fatalf("read %s", Key(v))
	}
	if got, ok := f.At(ModelValue("a2")); !ok || got != "open" {
		t.Fatalf("f[a2] is %v", got)
	}
	if _, ok := f.At("a2"); ok {
		t.Fatal("the string \"a2\" is taken for the model value a2")
	}
	reordered, err := ParseValue(`(a2 :> "open" @@ a1 :> [idx |-> 0, src |-> "none"])`)
	if err != nil || !Equal(v, reordered) {
		t.Fatalf("one function printed in another order reads as another: %v", err)
	}
	if Equal(ModelValue("a1"), "a1") {
		t.Fatal("a model value equals the string of its name")
	}
}

func TestConstantValuesReadsSetsOfModelValues(t *testing.T) {
	k, err := ConstantValues("CONSTANTS\n    Auths = {a1, a2}\n    Streams = {}\n    MaxAppends = 2\nINVARIANTS\n    TypeOK\n")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]Value{
		"Auths": Set{ModelValue("a1"), ModelValue("a2")}, "Streams": Set{}, "MaxAppends": int64(2),
	}
	if len(k) != len(want) {
		t.Fatalf("read %v", k)
	}
	for name, v := range want {
		if !Equal(k[name], v) {
			t.Errorf("%s is %s, not %s", name, Key(k[name]), Key(v))
		}
	}
	if _, err := ConstantValues("CONSTANTS\n    A <- B\n"); err == nil {
		t.Fatal("a replacement is read as a constant")
	}
}

// TestConstantValuesReadsWhatTLCDoes: a string keeps its spaces, a set its
// members once, a comment is no value, a model value may start with a digit,
// and what TLC's configuration grammar refuses is refused.
func TestConstantValuesReadsWhatTLCDoes(t *testing.T) {
	k, err := ConstantValues("CONSTANTS \\* names\n  X = \"a  b\" (* a (* nested *) comment *)\n" +
		"  S = {a1, a1, 1a}\n  N = -2\nINVARIANT TypeOK\nCONSTANT\n  L = TRUE\n")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]Value{
		"X": "a  b", "S": Set{ModelValue("a1"), ModelValue("1a")}, "N": int64(-2), "L": true,
	}
	if len(k) != len(want) {
		t.Fatalf("read %v", k)
	}
	for name, v := range want {
		if !Equal(k[name], v) || (name == "S" && len(k[name].(Set)) != 2) {
			t.Errorf("%s is %s, not %s", name, Key(k[name]), Key(v))
		}
	}
	for _, bad := range []string{
		"CONSTANTS\n  Bad Name = 1\n",
		"CONSTANTS\n  X = (a1 :> 1)\n",
		"CONSTANTS\n  X = [a |-> 1]\n",
		"CONSTANTS\n  X = << 1 >>\n",
		"CONSTANTS\n  X = 1\n  X = 2\n",
		"CONSTANTS\n  X = \"never closed\n",
		"CONSTANTS\n  X = 1 (* never closed\n",
	} {
		if _, err := ConstantValues(bad); err == nil {
			t.Errorf("%q is read", bad)
		}
	}
	if v, err := ParseValue("1a"); err != nil || !Equal(v, ModelValue("1a")) {
		t.Errorf("the model value 1a reads as %v, %v", v, err)
	}
}

func TestParseValueRefusesWhatItCannotRead(t *testing.T) {
	for _, text := range []string{"(1 :> 2 @@ 1 :> 3)", "(1 :> 2", "(1 2)", "[a |-> 1", "{1, 2", "<< 1 >> 2", "%"} {
		if _, err := ParseValue(text); err == nil {
			t.Errorf("%q is read", text)
		}
	}
}

func TestReadDotReadsStatesStepsAndTheInitialState(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "g.dot")
	dot := strings.Join([]string{
		"strict digraph DiskGraph {",
		"node [shape=box,style=rounded]",
		"subgraph cluster_graph {",
		`-11 [label="/\\ x = 0\n/\\ s = {}",style = filled]`,
		`-11 -> 22 [label="Step",color="black",fontcolor="black"];`,
		`22 [label="/\\ x = 1\n/\\ s = {\"a\"}",tooltip="/\\ x = 1\n/\\ s = {\"a\"}"];`,
		`22 -> 22 [label="Stay(1)",color="black",fontcolor="black"];`,
		"{rank = same; -11;}",
		"}",
		"}",
	}, "\n")
	if err := os.WriteFile(path, []byte(dot), 0o644); err != nil {
		t.Fatal(err)
	}
	g, err := ReadDot(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(g.States) != 2 || len(g.Edges) != 2 || len(g.Init) != 1 || g.Init[0] != "-11" {
		t.Fatalf("states %d, steps %d, initial %v", len(g.States), len(g.Edges), g.Init)
	}
	if !Equal(g.States["22"], Record{"x": int64(1), "s": Set{"a"}}) || g.Edges[1].Action != "Stay(1)" {
		t.Fatalf("read %s and %+v", Key(g.States["22"]), g.Edges[1])
	}
	// A state line with an attribute TLC does not write is refused, not
	// skipped: a skipped state would leave its steps pointing nowhere.
	bad := strings.Replace(dot, `,tooltip=`, `,color="red",tooltip=`, 1)
	if err := os.WriteFile(path, []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := ReadDot(path); err == nil {
		t.Fatal("a state line with an unknown attribute is read")
	}
	for name, line := range map[string]string{
		"an indented step":            `  22 -> 22 [label="Extra",color="black",fontcolor="black"];`,
		"a step drawn in a color":     `22 -> -11 [label="Extra",color="red",fontcolor="red"];`,
		"a statement TLC never wrote": `edge [style=dashed]`,
	} {
		extra := strings.Replace(dot, "{rank = same; -11;}", line+"\n{rank = same; -11;}", 1)
		if err := os.WriteFile(path, []byte(extra), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := ReadDot(path); err == nil {
			t.Errorf("%s is skipped, not refused", name)
		}
	}
}

func TestConstantsReadsACfg(t *testing.T) {
	cfg := "\\* a comment\nSPECIFICATION Spec\nCONSTANTS\n    A = 3   \\* three\n    B = 0\nINVARIANTS\n    TypeOK\n"
	k, err := Constants(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(k) != 2 || k["A"] != 3 || k["B"] != 0 {
		t.Fatalf("read %v", k)
	}
	if _, err := Constants("CONSTANTS\n    A <- B\n"); err == nil {
		t.Fatal("a replacement is read as a constant")
	}
}

func TestGuardTableStatesReadsTheRepositorysTables(t *testing.T) {
	states, err := GuardTableStates("LeaseLifecycle")
	if err != nil {
		t.Fatal(err)
	}
	if n, ok := states["LeaseLifecycle.cfg"]; !ok || n <= 0 {
		t.Fatalf("read %v", states)
	}
	if _, err := GuardTableStates("NoSuchSpec"); err == nil {
		t.Fatal("a missing table is read")
	}
}
