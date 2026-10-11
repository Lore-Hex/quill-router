package schema

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestSplitKeepsQuotesAndDropsComments(t *testing.T) {
	got, err := Split("-- a comment; not a statement\nCREATE TABLE a (s STRING(8) DEFAULT ('x;y--z'));\n" +
		"/* a block; comment */ CREATE INDEX b ON a (s); # another\n\"q;\" ;;\n")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"CREATE TABLE a (s STRING(8) DEFAULT ('x;y--z'))", "CREATE INDEX b ON a (s)", `"q;"`}
	if len(got) != len(want) {
		t.Fatalf("got %q", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("statement %d: got %q, want %q", i, got[i], want[i])
		}
	}
}

// TestSplitEndsALineCommentAtACarriageReturn: a statement after a comment
// that a carriage return ends is kept, as GoogleSQL's lexer keeps it.
func TestSplitEndsALineCommentAtACarriageReturn(t *testing.T) {
	got, err := Split("CREATE TABLE a (x INT64) PRIMARY KEY (x);-- one\rCREATE TABLE b (x INT64) PRIMARY KEY (x);# two\rSELECT 1")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 || got[1] != "CREATE TABLE b (x INT64) PRIMARY KEY (x)" || got[2] != "SELECT 1" {
		t.Fatalf("got %q", got)
	}
}

func TestSplitRefusesWhatItCannotClose(t *testing.T) {
	for _, sql := range []string{"SELECT 'a", "SELECT \"a\\\"", "SELECT '''a''", "SELECT 1 /* a", "SELECT `a"} {
		if _, err := Split(sql); err == nil {
			t.Errorf("%q is split", sql)
		}
	}
}

func TestStatementsAreTheFastPathsTablesAndIndexes(t *testing.T) {
	statements, err := Statements()
	if err != nil {
		t.Fatal(err)
	}
	created := regexp.MustCompile(`^CREATE (TABLE|INDEX|UNIQUE INDEX) (\w+)`)
	var names []string
	for _, s := range statements {
		m := created.FindStringSubmatch(s)
		if m == nil {
			t.Fatalf("a statement that creates nothing: %q", s)
		}
		names = append(names, m[2])
	}
	want := "tr_credit_balance tr_lease tr_lease_by_state tr_lease_by_owner tr_lease_by_id tr_lease_donor tr_lease_hold tr_lease_handoff tr_lease_winners " +
		"tr_lease_winners_by_work tr_lease_drain tr_lease_drain_by_commit tr_lease_record tr_lease_staged " +
		"tr_lease_staged_by_lease tr_fastpath_workspace tr_fastpath_member"
	if got := strings.Join(names, " "); got != want {
		t.Fatalf("fastpath.sql creates %s", got)
	}
}

// TestCreditBalanceIsProductions holds fastpath.sql's tr_credit_balance to the
// table production creates (scripts/deploy/migrate_typed_counters.sh), up to
// whitespace, and to every column the script adds to it since.
func TestCreditBalanceIsProductions(t *testing.T) {
	script, err := os.ReadFile(filepath.Join("..", "..", "scripts", "deploy", "migrate_typed_counters.sh"))
	if err != nil {
		t.Fatal(err)
	}
	created := regexp.MustCompile(`(?s)apply_ddl "(CREATE TABLE tr_credit_balance \(.*?\) PRIMARY KEY \(workspace_id, shard\))"`).
		FindAllSubmatch(script, -1)
	if len(created) != 1 {
		t.Fatalf("the script creates tr_credit_balance %d times", len(created))
	}
	statements, err := Statements()
	if err != nil {
		t.Fatal(err)
	}
	var ours string
	for _, s := range statements {
		if strings.HasPrefix(s, "CREATE TABLE tr_credit_balance ") {
			ours = s
		}
	}
	spaces := regexp.MustCompile(`\s+`)
	theirs := spaces.ReplaceAllString(string(created[0][1]), " ")
	if got := spaces.ReplaceAllString(ours, " "); got != theirs {
		t.Fatalf("fastpath.sql's tr_credit_balance:\n%s\nproduction's:\n%s", got, theirs)
	}
	added := regexp.MustCompile(`(?m)^ensure_column tr_credit_balance (\w+) `).FindAllSubmatch(script, -1)
	if len(added) == 0 {
		t.Fatal("the script adds no column to tr_credit_balance: the pattern no longer matches it")
	}
	for _, m := range added {
		if !regexp.MustCompile(`\b` + string(m[1]) + ` `).MatchString(ours) {
			t.Errorf("production adds %s to tr_credit_balance, and fastpath.sql lacks it", m[1])
		}
	}
}
