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
	var names []string
	for _, s := range statements {
		m := created.FindStringSubmatch(s)
		if m == nil {
			t.Fatalf("a statement that creates nothing: %q", s)
		}
		names = append(names, m[2])
	}
	want := "tr_credit_balance tr_reservation tr_reservation_by_expiry tr_reservation_by_idemp " +
		"tr_reservation_by_authorization tr_gateway_authorization " +
		"tr_entities tr_lease tr_lease_by_state tr_lease_by_owner tr_lease_by_id tr_lease_donor tr_lease_hold tr_lease_handoff tr_lease_winners " +
		"tr_lease_winners_by_work tr_lease_drain tr_lease_drain_by_commit tr_lease_record tr_lease_staged " +
		"tr_lease_staged_by_lease tr_fastpath_workspace tr_fastpath_member tr_fastpath_edge_decide"
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

// TestReservationIsProductions holds fastpath.sql's tr_reservation and its
// index by expiry to production's (scripts/deploy/migrate_typed_counters.sh),
// up to whitespace, and to every column the script adds to the table since.
func TestReservationIsProductions(t *testing.T) {
	const table, index, applied = "CREATE TABLE tr_reservation ", "CREATE INDEX tr_reservation_by_expiry ", `apply_ddl "`
	script, err := os.ReadFile(filepath.Join("..", "..", "scripts", "deploy", "migrate_typed_counters.sh"))
	if err != nil {
		t.Fatal(err)
	}
	theirs := map[string][][]byte{
		table: regexp.MustCompile(`(?s)` + applied + `(` + regexp.QuoteMeta(table) + `\(.*?\) PRIMARY KEY \(reservation_id\))"`).
			FindSubmatch(script),
		index: regexp.MustCompile(applied + `(` + regexp.QuoteMeta(index) + `ON tr_reservation \(settled, expires_at\))"`).
			FindSubmatch(script),
	}
	statements, err := Statements()
	if err != nil {
		t.Fatal(err)
	}
	ours := map[string]string{}
	for _, s := range statements {
		for name := range theirs {
			if strings.HasPrefix(s, name) {
				ours[name] = s
			}
		}
	}
	spaces := regexp.MustCompile(`\s+`)
	for name, m := range theirs {
		if m == nil {
			t.Fatalf("the script no longer matches %q", name)
		}
		if got, want := spaces.ReplaceAllString(ours[name], " "), spaces.ReplaceAllString(string(m[1]), " "); got != want {
			t.Fatalf("fastpath.sql's %s:\n%s\nproduction's:\n%s", name, got, want)
		}
	}
	added := regexp.MustCompile(`(?m)^ensure_column tr_reservation (\w+) `).FindAllSubmatch(script, -1)
	if len(added) == 0 {
		t.Fatal("the script adds no column to tr_reservation: the pattern no longer matches it")
	}
	for _, m := range added {
		if !regexp.MustCompile(`\b` + string(m[1]) + ` `).MatchString(ours[table]) {
			t.Errorf("production adds %s to tr_reservation, and fastpath.sql lacks it", m[1])
		}
	}
}

// deployScript is a script of scripts/deploy, production's migrations.
func deployScript(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "scripts", "deploy", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// ourStatement is fastpath.sql's statement that begins with prefix, its
// whitespace made single spaces.
func ourStatement(t *testing.T, prefix string) string {
	t.Helper()
	statements, err := Statements()
	if err != nil {
		t.Fatal(err)
	}
	var found []string
	for _, s := range statements {
		if w := words(s); strings.HasPrefix(w, prefix) {
			found = append(found, w)
		}
	}
	if len(found) != 1 {
		t.Fatalf("fastpath.sql has %d statements beginning %q", len(found), prefix)
	}
	return found[0]
}

// TestReservationIndexesAreProductions holds fastpath.sql's
// tr_reservation_by_idemp, the unique index that arbitrates a retried
// authorization, and tr_reservation_by_authorization, by which an
// authorization's reservation is read back, to the indexes production makes
// (scripts/deploy/migrate_typed_counters.sh), up to whitespace.
func TestReservationIndexesAreProductions(t *testing.T) {
	script := deployScript(t, "migrate_typed_counters.sh")
	for index, ours := range map[string]string{
		"tr_reservation_by_idemp":         "CREATE UNIQUE NULL_FILTERED INDEX tr_reservation_by_idemp ",
		"tr_reservation_by_authorization": "CREATE NULL_FILTERED INDEX tr_reservation_by_authorization ",
	} {
		made := regexp.MustCompile(`(?s)"(CREATE [A-Z_ ]*INDEX `+index+`\s.*?)"`).FindAllSubmatch(script, -1)
		if len(made) != 1 {
			t.Fatalf("the script makes %s %d times", index, len(made))
		}
		want := words(string(made[0][1]))
		if got := ourStatement(t, ours); got != want {
			t.Errorf("fastpath.sql's %s:\n%s\nproduction's:\n%s", index, got, want)
		}
	}
}

// TestGatewayAuthorizationIsProductions holds fastpath.sql's
// tr_gateway_authorization to the table production has: the table
// scripts/deploy/migrate_request_retention.sh makes, up to whitespace, with
// each column scripts/deploy/migrate_spend_lease.sh has added to it since
// appended, in that script's order and with its type; and no other script
// of scripts/deploy adds a column to it.
func TestGatewayAuthorizationIsProductions(t *testing.T) {
	const adder = "migrate_spend_lease.sh"
	made := regexp.MustCompile(`(?s)"(CREATE TABLE tr_gateway_authorization \(.*?)\s*(\) PRIMARY KEY \(authorization_id\))"`).
		FindAllSubmatch(deployScript(t, "migrate_request_retention.sh"), -1)
	if len(made) != 1 {
		t.Fatalf("the script makes tr_gateway_authorization %d times", len(made))
	}
	added := regexp.MustCompile(`(?m)^ensure_column tr_gateway_authorization (\w+) \\\n\s*"([^"\n]+)"$`).
		FindAllSubmatch(deployScript(t, adder), -1)
	if len(added) == 0 {
		t.Fatal("the script adds no column to tr_gateway_authorization: the pattern no longer matches it")
	}
	want := words(string(made[0][1]))
	for _, m := range added {
		want += ", " + string(m[1]) + " " + words(string(m[2]))
	}
	want += " " + words(string(made[0][2]))
	if got := ourStatement(t, "CREATE TABLE tr_gateway_authorization "); got != want {
		t.Fatalf("fastpath.sql's tr_gateway_authorization:\n%s\nproduction's:\n%s", got, want)
	}
	// Every line of any script that adds a column to the table is one of
	// those read above, so a column added another way, or by another
	// script, is not missed.
	scripts, err := filepath.Glob(filepath.Join("..", "..", "scripts", "deploy", "*.sh"))
	if err != nil || len(scripts) == 0 {
		t.Fatalf("the scripts: %v %v", scripts, err)
	}
	adds := regexp.MustCompile(`(?im)^.*(ensure_column|add\s+column).*\btr_gateway_authorization\b.*$|` +
		`^.*\btr_gateway_authorization\b.*(ensure_column|add\s+column).*$`)
	for _, path := range scripts {
		found := adds.FindAll(deployScript(t, filepath.Base(path)), -1)
		if filepath.Base(path) == adder && len(found) == len(added) {
			continue
		}
		if len(found) > 0 {
			t.Errorf("%s adds a column to tr_gateway_authorization the test does not read: %q", path, found)
		}
	}
}
