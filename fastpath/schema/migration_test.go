package schema

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// migration is production's migration of the service's tables
// (docs/design/fast-admission-production-rollout.md, W4).
var migration = filepath.Join("..", "..", "scripts", "deploy", "migrate_fastpath.sh")

// created names what a statement creates.
var created = regexp.MustCompile(`^CREATE (TABLE|INDEX|UNIQUE INDEX) (\w+)`)

// words is a statement with its whitespace made single spaces.
func words(s string) string { return strings.Join(strings.Fields(s), " ") }

// ownStatements are fastpath.sql's statements but production's
// tr_credit_balance, which the migration leaves alone, and what each
// creates, and whether it is an index.
func ownStatements(t *testing.T) (statements, names []string, index []bool) {
	t.Helper()
	all, err := Statements()
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range all {
		m := created.FindStringSubmatch(s)
		if m == nil {
			t.Fatalf("a statement that creates nothing: %q", s)
		}
		if m[2] != "tr_credit_balance" {
			statements, names, index = append(statements, words(s)), append(names, m[2]), append(index, m[1] != "TABLE")
		}
	}
	return statements, names, index
}

// database is the stand-in database testdata/gcloud answers from.
type database struct{ dir string }

func newDatabase(t *testing.T, existing ...string) database {
	t.Helper()
	d := database{t.TempDir()}
	for _, sub := range []string{"objects", "waits"} {
		if err := os.Mkdir(filepath.Join(d.dir, sub), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range existing {
		d.set(t, filepath.Join("objects", name), "")
	}
	return d
}

func (d database) set(t *testing.T, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(d.dir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func (d database) read(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(d.dir, name))
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// applied is every statement the migration applied, in order.
func (d database) applied(t *testing.T) []string {
	t.Helper()
	var out []string
	for _, s := range strings.Split(d.read(t, "applied"), "\n;\n") {
		if strings.TrimSpace(s) != "" {
			out = append(out, words(s))
		}
	}
	return out
}

// readinessChecked are the indexes whose state the migration read.
func (d database) readinessChecked(t *testing.T) map[string]int {
	t.Helper()
	out := map[string]int{}
	state := regexp.MustCompile(`INDEX_STATE .* index_name='(\w+)'`)
	for _, q := range strings.Split(d.read(t, "queries"), "\n") {
		if m := state.FindStringSubmatch(q); m != nil {
			out[m[1]]++
		}
	}
	return out
}

// migrate runs the migration against d, with testdata's gcloud and sleep
// first on the path, and reports whether it succeeded.
func migrate(t *testing.T, d database) (bool, string) {
	t.Helper()
	testdata, err := filepath.Abs("testdata")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("bash", migration)
	cmd.Env = []string{"PATH=" + testdata + string(os.PathListSeparator) + os.Getenv("PATH"),
		"GCP_PROJECT_ID=project", "SPANNER_INSTANCE_ID=instance", "SPANNER_DATABASE_ID=database",
		"FAKE_SPANNER=" + d.dir}
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	err = cmd.Run()
	if err != nil && cmd.ProcessState == nil {
		t.Fatal(err)
	}
	return err == nil, out.String()
}

// TestTheMigrationAppliesTheSchema: on a database with none of the
// service's tables, the migration applies fastpath.sql's statements but
// tr_credit_balance's, each once and in its order, nothing else, and finds
// each index read-write; run again, it applies nothing and checks each index
// again.
func TestTheMigrationAppliesTheSchema(t *testing.T) {
	want, names, index := ownStatements(t)
	d := newDatabase(t)
	if ok, out := migrate(t, d); !ok {
		t.Fatalf("the first run failed:\n%s", out)
	}
	if got := d.applied(t); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("the first run applied\n%s\nand fastpath.sql has\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	for run := 1; run <= 2; run++ {
		checked := d.readinessChecked(t)
		for i, name := range names {
			if index[i] != (checked[name] == run) {
				t.Errorf("run %d: %s's state read %d times", run, name, checked[name])
			}
		}
		if run == 2 {
			break
		}
		if ok, out := migrate(t, d); !ok {
			t.Fatalf("the second run failed:\n%s", out)
		}
		if got := d.applied(t); len(got) != len(want) {
			t.Fatalf("the second run applied %d statements more", len(got)-len(want))
		}
	}
}

// TestARunStoppedPartwayIsFinished: a database with the first k of the
// service's objects, as a run stopped after k statements leaves it, gets
// exactly the rest, for every k: so each statement is behind a check of
// the very object it creates.
func TestARunStoppedPartwayIsFinished(t *testing.T) {
	want, names, _ := ownStatements(t)
	for k := range want {
		d := newDatabase(t, names[:k]...)
		if ok, out := migrate(t, d); !ok {
			t.Fatalf("with %d objects present the run failed:\n%s", k, out)
		}
		if got := d.applied(t); strings.Join(got, "\n") != strings.Join(want[k:], "\n") {
			t.Errorf("with the first %d objects present, the run applied %d statements: %q", k, len(got), got)
		}
	}
}

// TestAMigrationThatCannotReadOrWriteFails: a schema query that fails stops
// the run with nothing applied, rather than reading as an object absent; a
// statement that fails stops it after that statement; and either run exits
// non-zero, so the deploy stops.
func TestAMigrationThatCannotReadOrWriteFails(t *testing.T) {
	d := newDatabase(t)
	d.set(t, "fail-queries", "")
	if ok, _ := migrate(t, d); ok || len(d.applied(t)) != 0 {
		t.Fatalf("a run whose queries fail: succeeded %v, applied %d", ok, len(d.applied(t)))
	}
	d = newDatabase(t)
	d.set(t, "fail-statements", "")
	if ok, _ := migrate(t, d); ok || len(d.applied(t)) != 0 {
		t.Fatalf("a run whose statements fail: succeeded %v, applied %d", ok, len(d.applied(t)))
	}
}

// TestTheMigrationWaitsForEachIndex: an index reported not yet read-write is
// read again until it is, and the run then passes; one that never is fails
// the run.
func TestTheMigrationWaitsForEachIndex(t *testing.T) {
	_, names, index := ownStatements(t)
	var last string
	for i, name := range names {
		if index[i] {
			last = name
		}
	}
	d := newDatabase(t)
	d.set(t, filepath.Join("waits", last), "3")
	if ok, out := migrate(t, d); !ok {
		t.Fatalf("a run whose index came read-write late failed:\n%s", out)
	}
	if got := d.readinessChecked(t)[last]; got != 4 {
		t.Fatalf("%s's state was read %d times, want 4", last, got)
	}
	d = newDatabase(t)
	d.set(t, filepath.Join("waits", last), "always")
	if ok, _ := migrate(t, d); ok {
		t.Fatalf("a run whose index never came read-write passed")
	}
}
