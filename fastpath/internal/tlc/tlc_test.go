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

func TestCompareRefusesALimitBelowOne(t *testing.T) {
	path := filepath.Join(t.TempDir(), "g.dot")
	dot := "strict digraph DiskGraph {\n-1 [label=\"/\\\\ x = 0\",style = filled]\n}\n"
	if err := os.WriteFile(path, []byte(dot), 0o644); err != nil {
		t.Fatal(err)
	}
	read := func(r Record) (int64, error) { return r["x"].(int64), nil }
	shadow := Shadow[int64]{Init: 0, Next: func(int64) []Step[int64] { return nil }}
	if _, err := Compare(path, read, shadow, 0); err == nil {
		t.Fatal("a limit of 0, which would report no difference, is accepted")
	}
	got, err := Compare(path, read, shadow, 1)
	if err != nil || got.States != 1 || len(got.Diffs) != 0 {
		t.Fatalf("the one-state graph compares as %+v, %v", got, err)
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

// TestCheckAssumptionTakesOnlyWhatTLCFindsTrue: TLC judges the assumption
// against the files as they are, whatever follows the spec's module, and
// anything it does not find true is refused.
func TestCheckAssumptionTakesOnlyWhatTLCFindsTrue(t *testing.T) {
	proofs, err := ProofsDir()
	if err != nil {
		t.Fatal(err)
	}
	// A copy of LeaseLifecycle with a second ==== line after its module's,
	// past which TLC does not read.
	dir := t.TempDir()
	for name, more := range map[string]string{"LeaseLifecycle.tla": "\n====\n", "LeaseLifecycle.cfg": ""} {
		text, err := os.ReadFile(filepath.Join(proofs, name))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), append(text, more...), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	check := func(assumption string) error {
		return checkAssumption(proofs, dir, "LeaseLifecycle", "LeaseLifecycle.cfg", assumption)
	}
	for _, assumption := range []string{"MaxHolds = 3", `MaxHolds = 3 \* with a comment`} {
		if err := check(assumption); err != nil {
			t.Errorf("%q: %v", assumption, err)
		}
	}
	for _, assumption := range []string{"MaxHolds = 2", "MaxHolds", "MaxHolds =", "NoSuchName = 1"} {
		if err := check(assumption); err == nil {
			t.Errorf("%q is taken", assumption)
		}
	}
}

// leaseLifecycleConstants are the constants proofs/LeaseLifecycle.cfg assigns.
var leaseLifecycleConstants = []string{
	"MaxHolds", "LeaseSize", "Window", "Skew", "MaxLife", "Grace", "CacheAge", "LastRenew", "MaxRestarts",
}

// TestBindConfigurationRefusesWhatChangesTheModel: a copy of LeaseLifecycle's
// files is bound as it is, and refused once anything that changes the graph
// TLC explores is appended to its .cfg, an override of a definition among
// them, or once the constants the test names differ from the file's.
func TestBindConfigurationRefusesWhatChangesTheModel(t *testing.T) {
	proofs, err := ProofsDir()
	if err != nil {
		t.Fatal(err)
	}
	copyWith := func(more string) string {
		dir := t.TempDir()
		for name, extra := range map[string]string{"LeaseLifecycle.tla": "", "LeaseLifecycle.cfg": more} {
			text, err := os.ReadFile(filepath.Join(proofs, name))
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, name), append(text, extra...), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		return dir
	}
	bind := func(dir string, constants []string) error {
		return BindConfiguration(dir, "LeaseLifecycle", "LeaseLifecycle.cfg", constants, "MaxHolds = 3")
	}
	if err := bind(copyWith(""), leaseLifecycleConstants); err != nil {
		t.Fatal(err)
	}
	// Each refusal must name what it refuses, or a copy refused for another
	// reason, such as one TLC cannot parse, would pass here too.
	refused := func(dir string, constants []string, want string) {
		t.Helper()
		err := bind(dir, constants)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("want a refusal naming %q, got %v", want, err)
		}
	}
	for more, want := range map[string]string{
		"\nCONSTANT HoldIds = {1}\n":          "HoldIds is assigned, and is no constant the test declares",
		"\nCONSTANTS MaxHolds <- LeaseSize\n": "override MaxHolds LeaseSize",
		"\nCONSTRAINT TypeOK\n":               "constraint TypeOK",
		"\nACTION_CONSTRAINT TypeOK\n":        "actionconstraint TypeOK",
		"\nSYMMETRY TypeOK\n":                 "symmetry TypeOK",
		"\nVIEW TypeOK\n":                     "view TypeOK",
		"\nINIT Init\n":                       "init Init",
	} {
		refused(copyWith(more), leaseLifecycleConstants, want)
	}
	dir := copyWith("")
	refused(dir, leaseLifecycleConstants[1:], "MaxHolds is assigned, and is no constant the test declares")
	refused(dir, append([]string{"MaxOwners"}, leaseLifecycleConstants...), "MaxOwners is not assigned")
}
