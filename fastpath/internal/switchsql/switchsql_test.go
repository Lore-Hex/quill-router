package switchsql

import (
	"encoding/json"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath"
)

// TestTheEmbeddedFixtureLoads: the fixture Go embeds parses, with its three
// operations.
func TestTheEmbeddedFixtureLoads(t *testing.T) {
	f, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{Enable, Disable, DisableUnheld} {
		if _, err := f.Get(name); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := f.Get("disable_all"); err == nil {
		t.Fatal("an operation the fixture lacks was found")
	}
}

// TestParseRefusesWhatItCannotExecuteExactly: another version, an
// operation unknown or missing, a member unknown, a mutation other than an
// insert_or_update or with unpaired columns, and a statement without a
// count's name of its own are each refused.
func TestParseRefusesWhatItCannotExecuteExactly(t *testing.T) {
	var base map[string]any
	if err := json.Unmarshal(fastpath.SwitchStatementsV1, &base); err != nil {
		t.Fatal(err)
	}
	ops := func(m map[string]any) map[string]any { return m["operations"].(map[string]any) }
	op := func(m map[string]any, name string) map[string]any { return ops(m)[name].(map[string]any) }
	for name, change := range map[string]func(map[string]any){
		"version 2":        func(m map[string]any) { m["version"] = 2 },
		"an unknown op":    func(m map[string]any) { ops(m)["disable_all"] = op(m, Enable) },
		"a missing op":     func(m map[string]any) { delete(ops(m), DisableUnheld) },
		"an unknown field": func(m map[string]any) { m["extra"] = true },
		"a delete": func(m map[string]any) {
			op(m, Disable)["mutations"].([]any)[0].(map[string]any)["op"] = "delete"
		},
		"unpaired columns": func(m map[string]any) {
			mu := op(m, Disable)["mutations"].([]any)[0].(map[string]any)
			mu["values"] = mu["values"].([]any)[:1]
		},
		"no count's name": func(m map[string]any) {
			op(m, Enable)["statements"].([]any)[0].(map[string]any)["counts"] = ""
		},
		"a shared count's name": func(m map[string]any) {
			st := op(m, Enable)["statements"].([]any)
			op(m, Enable)["statements"] = append(st, st[0])
		},
	} {
		var m map[string]any
		b, _ := json.Marshal(base)
		_ = json.Unmarshal(b, &m)
		change(m)
		b, _ = json.Marshal(m)
		if _, err := Parse(b); err == nil {
			t.Errorf("%s: parsed", name)
		}
	}
}

// TestValuesAreLiteralsParametersOrTheCommitTimestamp: "@commit_timestamp"
// is the commit timestamp, "@name" the parameter, and a parameter missing
// is an error rather than a value.
func TestValuesAreLiteralsParametersOrTheCommitTimestamp(t *testing.T) {
	params := map[string]any{"workspace": "ws"}
	for raw, want := range map[string]any{`"@commit_timestamp"`: spanner.CommitTimestamp, `"@workspace"`: "ws",
		`true`: true, `"plain"`: "plain", `7`: int64(7)} {
		got, err := value(json.RawMessage(raw), params)
		if err != nil || got != want {
			t.Errorf("%s: %v %v, want %v", raw, got, err, want)
		}
	}
	if _, err := value(json.RawMessage(`"@reason"`), params); err == nil || !strings.Contains(err.Error(), "reason") {
		t.Errorf("a missing parameter: %v", err)
	}
	if _, err := value(json.RawMessage(`[1]`), params); err == nil {
		t.Error("an array was a value")
	}
}
