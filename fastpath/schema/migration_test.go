package schema

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// migration is production's migration of the service's tables
// (docs/design/fast-admission-production-rollout.md, W4).
var migration = filepath.Join("..", "..", "scripts", "deploy", "migrate_fastpath.sh")

// guarded is one statement of the migration with the check that guards it:
// `if <check> <name>; then ... else apply_ddl "<statement>" ... fi`, and the
// read-write wait an index has after it.
var guarded = regexp.MustCompile(`(?s)\nif (table_exists|index_exists) (\w+); then\n` +
	`  log "\w+: already present"\nelse\n  apply_ddl "([^"]*)"\n  log "\w+: created"\nfi\n` +
	`(wait_index_read_write (\w+)\n)?`)

// words is a statement with its whitespace made single spaces.
func words(s string) string { return strings.Join(strings.Fields(s), " ") }

// TestTheMigrationIsTheSchema: the migration creates every table and index
// fastpath.sql does but production's tr_credit_balance, statement for
// statement and in its order, each behind a check for the very object it
// creates, and waits for each index to be read-write; it applies nothing
// else.
func TestTheMigrationIsTheSchema(t *testing.T) {
	script, err := os.ReadFile(migration)
	if err != nil {
		t.Fatal(err)
	}
	statements, err := Statements()
	if err != nil {
		t.Fatal(err)
	}
	created := regexp.MustCompile(`^CREATE (TABLE|INDEX|UNIQUE INDEX) (\w+)`)
	var want []string
	for _, s := range statements {
		if created.FindStringSubmatch(s)[2] != "tr_credit_balance" {
			want = append(want, words(s))
		}
	}
	steps := guarded.FindAllStringSubmatch(string(script), -1)
	if got := strings.Count(string(script), "apply_ddl \""); got != len(steps) {
		t.Fatalf("%d statements applied, %d of them guarded as the test reads them", got, len(steps))
	}
	if len(steps) != len(want) {
		t.Fatalf("the migration applies %d statements, and fastpath.sql has %d of the service's own", len(steps),
			len(want))
	}
	for i, step := range steps {
		check, name, statement, waits, waited := step[1], step[2], words(step[3]), step[4] != "", step[5]
		m := created.FindStringSubmatch(statement)
		switch {
		case statement != want[i]:
			t.Errorf("statement %d:\n%s\nfastpath.sql's:\n%s", i, statement, want[i])
		case m == nil || m[2] != name:
			t.Errorf("statement %d creates %v, and its check is for %s", i, m, name)
		case (m[1] == "TABLE") != (check == "table_exists"):
			t.Errorf("%s, a %s, is checked with %s", name, m[1], check)
		case (m[1] != "TABLE") != waits || (waits && waited != name):
			t.Errorf("%s's wait for read-write: %q", name, step[4])
		}
	}
}
