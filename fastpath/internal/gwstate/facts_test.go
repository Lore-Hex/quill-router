package gwstate

import (
	"encoding/json"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

type pythonKey struct {
	Error  *string     `json:"error"`
	Reason *string     `json:"reason"`
	Tags   [][2]string `json:"tags"`
	// The ids Python's key carries when it is eligible.
	Hash          *string `json:"hash"`
	WorkspaceID   *string `json:"workspace_id"`
	LookupHash    *string `json:"lookup_hash"`
	CreatorUserID *string `json:"creator_user_id"`
}

type pythonWorkspace struct {
	Error  *string `json:"error"`
	Reason *string `json:"reason"`
	// PauseCauses is raw: a malformed body's causes need not be strings.
	PauseCauses json.RawMessage `json:"pause_causes"`
	// BilledWorkspaceID is the workspace id Python bills: the body's.
	BilledWorkspaceID *string `json:"billed_workspace_id"`
	OwnerUserID       *string `json:"owner_user_id"`
}

type vectors struct {
	FactOrder       []string            `json:"fact_order"`
	WorkspaceOrder  []string            `json:"workspace_order"`
	ConsultedFields map[string][]string `json:"consulted_fields"`
	KeyCases        []struct {
		Name   string    `json:"name"`
		Body   string    `json:"body"`
		At     string    `json:"at"`
		Exact  bool      `json:"exact"`
		Python pythonKey `json:"python"`
	} `json:"key_cases"`
	WorkspaceCases []struct {
		Name           string          `json:"name"`
		Body           *string         `json:"body"`
		KeyWorkspaceID string          `json:"key_workspace_id"`
		Exact          bool            `json:"exact"`
		Python         pythonWorkspace `json:"python"`
	} `json:"workspace_cases"`
}

func readVectors(t *testing.T) vectors {
	t.Helper()
	text, err := os.ReadFile(filepath.Join("..", "..", "testdata", "gateway_key_vectors_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(text, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func reasonOf(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func pairsOf(tags [][2]string) []Pair {
	out := []Pair{}
	for _, t := range tags {
		out = append(out, Pair{t[0], t[1]})
	}
	return out
}

// TestTheKeyFactsAgreeWithPythonOnEveryVector: on a well-formed body (exact),
// the reason Go gives at the case's instant is the first Python's predicates
// give, and an eligible key's tags are Python's merged defaults in Python's
// order. On any body, Go never admits a key Python would not: when Go is
// eligible Python is, with the same tags, and a body Python cannot read is
// never eligible.
func TestTheKeyFactsAgreeWithPythonOnEveryVector(t *testing.T) {
	v := readVectors(t)
	want := []string{ReasonKeyDisabled, ReasonKeyExpired, ReasonKeyScope, ReasonKeyFederated, ReasonKeyTags,
		ReasonKeyApp, ReasonKeyLimit, ReasonKeyWindowLimit, ReasonKeyWindowAlert, ReasonKeyBudgetStrict}
	if !slices.Equal(v.FactOrder, want) {
		t.Fatalf("the vectors' fact order %q is not the facts' %q", v.FactOrder, want)
	}
	exact, passed, reasons := 0, 0, map[string]int{}
	for _, c := range v.KeyCases {
		at, err := time.Parse(time.RFC3339Nano, c.At)
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		facts := ParseKey(c.Body)
		// The vectors hold the pilot's rule, windows an exclusion (PilotAt).
		ok, reason := facts.PilotAt(at)
		python := reasonOf(c.Python.Reason)
		if ok {
			if facts.CreatorUserID != reasonOf(c.Python.CreatorUserID) {
				t.Errorf("%s: Go and Python disagree on the key creator", c.Name)
			}
			if python != "" || !slices.Equal(facts.Tags, pairsOf(c.Python.Tags)) {
				t.Errorf("%s: Go admits (tags %q); Python: %q, tags %q", c.Name, facts.Tags, python, c.Python.Tags)
			} else if facts.Hash != reasonOf(c.Python.Hash) || facts.WorkspaceID != reasonOf(c.Python.WorkspaceID) ||
				facts.LookupHash != reasonOf(c.Python.LookupHash) {
				t.Errorf("%s: Go reads key %s of workspace %s (lookup %s); Python %v %v %v", c.Name, facts.Hash,
					facts.WorkspaceID, facts.LookupHash, reasonOf(c.Python.Hash), reasonOf(c.Python.WorkspaceID),
					reasonOf(c.Python.LookupHash))
			}
		} else if reason == "" {
			t.Errorf("%s: not eligible without a reason", c.Name)
		}
		if c.Exact {
			exact++
			reasons[python]++
			if reason != python {
				t.Errorf("%s: Go %q, Python %q", c.Name, reason, python)
			}
		} else if !ok {
			passed++
		}
	}
	// Positive control: the file holds what it is meant to, and each reason
	// is exercised on well-formed bodies.
	if exact < 400 || passed < 50 {
		t.Fatalf("%d exact cases and %d passed to Python", exact, passed)
	}
	for _, r := range append([]string{""}, want...) {
		if reasons[r] < 5 {
			t.Errorf("only %d exact cases with reason %q", reasons[r], r)
		}
	}
	t.Logf("%d key cases: %d exact, %d others passed to Python", len(v.KeyCases), exact, passed)
}

// TestTheWorkspaceFactsAgreeWithPythonOnEveryVector, as the keys': the same
// reason and pause causes on well-formed bodies, and never eligible where
// Python is not.
func TestTheWorkspaceFactsAgreeWithPythonOnEveryVector(t *testing.T) {
	v := readVectors(t)
	want := []string{ReasonWorkspaceGone, ReasonWorkspacePaused, ReasonWorkspaceFederal, ReasonWorkspaceOrigin}
	if !slices.Equal(v.WorkspaceOrder, want) {
		t.Fatalf("the vectors' workspace order %q is not the facts' %q", v.WorkspaceOrder, want)
	}
	exact := 0
	for _, c := range v.WorkspaceCases {
		body, found := "", c.Body != nil
		if found {
			body = *c.Body
		}
		facts := ParseWorkspace(body, found, c.KeyWorkspaceID)
		python := reasonOf(c.Python.Reason)
		if facts.Eligible && python != "" {
			t.Errorf("%s: Go admits; Python %q", c.Name, python)
		}
		if facts.Eligible && facts.OwnerUserID != reasonOf(c.Python.OwnerUserID) {
			t.Errorf("%s: Go and Python disagree on the workspace owner", c.Name)
		}
		// Go bills the key's workspace: it answers only where Python bills
		// the same one.
		if facts.Eligible && reasonOf(c.Python.BilledWorkspaceID) != c.KeyWorkspaceID {
			t.Errorf("%s: Go admits for %s; Python bills %v", c.Name, c.KeyWorkspaceID,
				reasonOf(c.Python.BilledWorkspaceID))
		}
		if c.Exact {
			exact++
			if facts.Reason != python {
				t.Errorf("%s: Go %q, Python %q", c.Name, facts.Reason, python)
			}
			var causes []string
			if err := json.Unmarshal(c.Python.PauseCauses, &causes); err != nil {
				t.Fatalf("%s: %v", c.Name, err)
			}
			if causes != nil && !slices.Equal(facts.PauseCauses, causes) {
				t.Errorf("%s: causes Go %q, Python %q", c.Name, facts.PauseCauses, causes)
			}
		}
	}
	if exact < 15 {
		t.Fatalf("%d exact workspace cases", exact)
	}
}

// TestEveryFieldPythonConsultsIsAccountedFor: the generator records each
// ApiKey and Workspace field the route's checks read; each is in the facts'
// lists, so a field Python starts to consult fails here until Go reads it or
// says why it need not.
func TestEveryFieldPythonConsultsIsAccountedFor(t *testing.T) {
	v := readVectors(t)
	for kind, listed := range map[string]map[string]string{"api_key": consultedKeyFields,
		"workspace": consultedWorkspaceFields} {
		consulted := v.ConsultedFields[kind]
		if len(consulted) < 5 {
			t.Fatalf("%s: only %d consulted fields recorded", kind, len(consulted))
		}
		for _, field := range consulted {
			if _, ok := listed[field]; !ok {
				t.Errorf("%s.%s: Python consults it and the facts do not account for it", kind, field)
			}
		}
	}
}

func TestBYOKProvidersAreMatchedOnCompleteIds(t *testing.T) {
	key := `{"hash":"kh","salt":"","secret_hash":"","lookup_hash":"lh","name":"","label":"","workspace_id":"ws",` +
		`"creator_user_id":null}`
	got := FromContext(store.KeyContext{KeyBody: key, RangeWorkspaceID: "ws",
		WorkspaceBody: `{"id":"ws","name":"","owner_user_id":""}`, WorkspaceFound: true, BYOKIDs: []string{"ws#anthropic", "ws#openai", "ws#x#mistral", "ws#", "wsz#google"},
		BroadcastDestination: true})
	if !slices.Equal(got.BYOKProviders, []string{"anthropic", "openai"}) || !got.BroadcastDestination ||
		!got.Key.Eligible || !got.Workspace.Eligible {
		t.Fatalf("%+v", got)
	}
	if got := FromContext(store.KeyContext{KeyBody: "[]", BYOKIDs: []string{"#openai"}}); got.BYOKProviders != nil ||
		got.Key.Eligible || got.Workspace.Reason != ReasonWorkspaceGone {
		t.Fatalf("a body that is not a key: %+v", got)
	}
	// Spanner's JSON_VALUE took another workspace id than Go's reading of
	// the key body (a repeated workspace_id): the workspace, BYOK and
	// broadcast reads are not the key's workspace's.
	got = FromContext(store.KeyContext{KeyBody: key, RangeWorkspaceID: "ws-first",
		WorkspaceBody: `{"id":"ws","name":"","owner_user_id":""}`, WorkspaceFound: true})
	if got.Workspace.Eligible || got.Workspace.Reason != ReasonWorkspaceShape+":id_mismatch" {
		t.Fatalf("a range taken for another workspace: %+v", got.Workspace)
	}
}

// TestOnlyTheEdgeAndTheServiceImportTheFacts: the package is the edge's
// decide pipeline's (G10), and the service makes the cache only with
// -edge-decide-shadow on.
func TestOnlyTheEdgeAndTheServiceImportTheFacts(t *testing.T) {
	const self = "github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	root := filepath.Join("..", "..")
	allowed := map[string]bool{filepath.Join(root, "internal", "edge"): true,
		filepath.Join(root, "internal", "service"): true}
	files, imports := 0, 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
			return err
		}
		parsed, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		files++
		for _, spec := range parsed.Imports {
			if imported, _ := strconv.Unquote(spec.Path.Value); imported == self {
				if allowed[filepath.Dir(path)] {
					imports++
					continue
				}
				t.Errorf("%s imports gwstate", path)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if files < 50 || imports == 0 {
		t.Fatalf("scanned %d files, %d allowed imports", files, imports)
	}
}

// windowFactsCase is a facts case of key_window_vectors_v1.json
// (docs/design/fast-admission-e2-key-windows.md section 7): an api_key body,
// Python's reading of its windows, and what ParseKey must read (go): its
// KeyWindows, "none", or key_shape:<field>.
type windowFactsCase struct {
	Name   string   `json:"name"`
	Body   string   `json:"body"`
	Labels []string `json:"labels"`
	Exact  bool     `json:"exact"`
	Python struct {
		Error *string `json:"error"`
		// Configured and Enforced may hold integers past int64.
		Configured map[string]json.Number `json:"configured"`
		Enforced   map[string]json.Number `json:"enforced"`
		Shards     *int                   `json:"shards"`
	} `json:"python"`
	Go json.RawMessage `json:"go"`
}

type goWindowFacts struct {
	Limits    map[string]*int64 `json:"limits"`
	AlertOnly bool              `json:"alert_only"`
	Shards    int               `json:"shards"`
}

// TestTheKeyWindowFactsAreTheVectors: on every facts case ParseKey reads
// what the vectors say Go reads (the edge's rule), and on an exact case that
// is Python's reading: the configured limits, the enforced ones (none for an
// alert-only key) and the shard count. Go's windows never differ from
// Python's where it reads them; elsewhere it passes (key_shape).
func TestTheKeyWindowFactsAreTheVectors(t *testing.T) {
	text, err := os.ReadFile(filepath.Join("..", "..", "testdata", "key_window_vectors_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Facts []windowFactsCase `json:"facts"`
	}
	if err := json.Unmarshal(text, &v); err != nil {
		t.Fatal(err)
	}
	limitsOf := func(l keywindow.Limits) map[string]*int64 {
		return map[string]*int64{"daily": l.Daily, "weekly": l.Weekly, "monthly": l.Monthly}
	}
	numbers := func(l keywindow.Limits) map[string]json.Number {
		out := map[string]json.Number{}
		for w, n := range limitsOf(l) {
			if n != nil {
				out[w] = json.Number(strconv.FormatInt(*n, 10))
			}
		}
		return out
	}
	read, shapes, none := 0, 0, 0
	for _, c := range v.Facts {
		k := ParseKey(c.Body)
		var want string
		if json.Unmarshal(c.Go, &want) == nil {
			switch {
			case want == "none":
				none++
				if !k.Eligible || k.Windows != nil {
					t.Errorf("%s: Go reads %+v (%q), want no windows", c.Name, k.Windows, k.Reason)
				}
			case k.Eligible || k.Reason != want:
				t.Errorf("%s: Go %q (eligible %v), want %q", c.Name, k.Reason, k.Eligible, want)
			default:
				shapes++
			}
			continue
		}
		var w goWindowFacts
		if err := json.Unmarshal(c.Go, &w); err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		if !k.Eligible || k.Windows == nil {
			t.Errorf("%s: Go %q (windows %v), want windows %+v", c.Name, k.Reason, k.Windows, w)
			continue
		}
		got := k.Windows
		if !reflect.DeepEqual(limitsOf(got.Limits), w.Limits) || got.AlertOnly != w.AlertOnly || got.Shards != w.Shards {
			t.Errorf("%s: Go %+v, want %+v", c.Name, *got, w)
			continue
		}
		if !c.Exact || c.Python.Error != nil || c.Python.Shards == nil {
			t.Errorf("%s: Go reads windows the vectors do not hold exact", c.Name)
			continue
		}
		if !reflect.DeepEqual(numbers(got.Limits), c.Python.Configured) ||
			!reflect.DeepEqual(numbers(got.Enforced()), c.Python.Enforced) || got.Shards != *c.Python.Shards {
			t.Errorf("%s: Go %+v; Python configured %v, enforced %v, shards %d", c.Name, *got, c.Python.Configured,
				c.Python.Enforced, *c.Python.Shards)
			continue
		}
		read++
	}
	// Positive control: the file holds each outcome.
	if read < 10 || shapes < 10 || none < 3 {
		t.Fatalf("%d facts cases read, %d passed, %d without windows", read, shapes, none)
	}
	t.Logf("%d facts cases: %d read as Python reads them, %d passed (key_shape), %d without windows", len(v.Facts),
		read, shapes, none)
}

// TestAWindowedKeyIsInTheCohortWithItsWindows (section 4.7): a key whose one
// exclusion was its windows is eligible with Windows; a lifetime cap and a
// strict key still exclude it (key_limit first, the windows read before
// key_budget_strict); PilotAt gives the pilot's reasons for each. A strict
// key with no limit at all is eligible as StrictLimitless
// (fast-admission-e2-strict-keys.md section 4.1), its pilot reason still
// key_budget_strict.
func TestAWindowedKeyIsInTheCohortWithItsWindows(t *testing.T) {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)
	body := func(extra string) string {
		return `{"hash":"kh_synthetic","salt":"s","secret_hash":"x","lookup_hash":"lh_synthetic","name":"n",` +
			`"label":"l","workspace_id":"ws_synthetic","creator_user_id":"u"` + extra + `}`
	}
	daily := int64(5)
	for _, v := range []struct {
		name, extra   string
		eligible      bool
		reason, pilot string
		windows       *KeyWindows
	}{
		{"enforcing", `,"limit_daily_microdollars":5`, true, "", ReasonKeyWindowLimit,
			&KeyWindows{Limits: keywindow.Limits{Daily: &daily}, Shards: 1}},
		{"alert only, four shards", `,"limit_daily_microdollars":5,"budget_alert_only":true,"usage_shard_count":4`,
			true, "", ReasonKeyWindowAlert,
			&KeyWindows{Limits: keywindow.Limits{Daily: &daily}, AlertOnly: true, Shards: 4}},
		{"strict", `,"limit_daily_microdollars":5,"budget_strict":true`, false, ReasonKeyBudgetStrict,
			ReasonKeyWindowLimit, &KeyWindows{Limits: keywindow.Limits{Daily: &daily}, Shards: 1}},
		{"lifetime cap", `,"limit_daily_microdollars":5.5,"limit_microdollars":9`, false, ReasonKeyLimit,
			ReasonKeyLimit, nil},
		{"disabled", `,"limit_daily_microdollars":5,"disabled":true`, false, ReasonKeyDisabled, ReasonKeyDisabled,
			nil},
		{"no window", `,"budget_alert_only":"yes"`, true, "", "", nil},
		{"strict, no window", `,"budget_strict":true`, true, "", ReasonKeyBudgetStrict, nil},
		{"float window", `,"limit_weekly_microdollars":1.0`, false, ReasonKeyShape + ":limit_weekly_microdollars",
			ReasonKeyShape + ":limit_weekly_microdollars", nil},
	} {
		k := ParseKey(body(v.extra))
		if k.Eligible != v.eligible || k.Reason != v.reason || !reflect.DeepEqual(k.Windows, v.windows) {
			t.Errorf("%s: eligible %v %q windows %+v, want %v %q %+v", v.name, k.Eligible, k.Reason, k.Windows,
				v.eligible, v.reason, v.windows)
		}
		if want := v.name == "strict, no window"; k.StrictLimitless != want {
			t.Errorf("%s: StrictLimitless %v, want %v", v.name, k.StrictLimitless, want)
		}
		if ok, reason := k.PilotAt(now); ok != (v.pilot == "") || reason != v.pilot {
			t.Errorf("%s: PilotAt %v %q, want %q", v.name, ok, reason, v.pilot)
		}
	}
}

// TestOnlyAStrictKeyWithNoLimitIsStrictLimitless
// (fast-admission-e2-strict-keys.md section 4.1): a budget_strict key with a
// lifetime limit is key_limit, with any window, enforcing or alert-only,
// key_budget_strict, and neither is StrictLimitless; with no limit at all it
// is eligible and StrictLimitless, its pilot reason key_budget_strict, an
// expiry or a disabled flag still first. A key that is not strict, or whose
// budget_strict is not a JSON bool, never is.
func TestOnlyAStrictKeyWithNoLimitIsStrictLimitless(t *testing.T) {
	now := time.Date(2026, 11, 24, 12, 0, 0, 0, time.UTC)
	body := func(extra string) string {
		return `{"hash":"kh_synthetic","salt":"s","secret_hash":"x","lookup_hash":"lh_synthetic","name":"n",` +
			`"label":"l","workspace_id":"ws_synthetic","creator_user_id":"u"` + extra + `}`
	}
	for _, v := range []struct {
		name, extra    string
		eligible       bool
		limitless      bool
		reason, pilot  string
		windowsPresent bool
	}{
		{"limitless", `,"budget_strict":true`, true, true, "", ReasonKeyBudgetStrict, false},
		{"limitless, nulls", `,"budget_strict":true,"limit_microdollars":null,"limit_daily_microdollars":null,` +
			`"limit_weekly_microdollars":null,"limit_monthly_microdollars":null`, true, true, "",
			ReasonKeyBudgetStrict, false},
		{"lifetime limit", `,"budget_strict":true,"limit_microdollars":5`, false, false, ReasonKeyLimit,
			ReasonKeyLimit, false},
		{"zero lifetime limit", `,"budget_strict":true,"limit_microdollars":0`, false, false, ReasonKeyLimit,
			ReasonKeyLimit, false},
		{"daily", `,"budget_strict":true,"limit_daily_microdollars":5`, false, false, ReasonKeyBudgetStrict,
			ReasonKeyWindowLimit, true},
		{"weekly", `,"budget_strict":true,"limit_weekly_microdollars":5`, false, false, ReasonKeyBudgetStrict,
			ReasonKeyWindowLimit, true},
		{"monthly", `,"budget_strict":true,"limit_monthly_microdollars":5`, false, false, ReasonKeyBudgetStrict,
			ReasonKeyWindowLimit, true},
		{"alert-only window", `,"budget_strict":true,"limit_monthly_microdollars":5,"budget_alert_only":true`,
			false, false, ReasonKeyBudgetStrict, ReasonKeyWindowAlert, true},
		{"zero window", `,"budget_strict":true,"limit_daily_microdollars":0`, false, false, ReasonKeyBudgetStrict,
			ReasonKeyWindowLimit, true},
		{"expired", `,"budget_strict":true,"expires_at":"2026-01-01T00:00:00Z"`, true, true, "", ReasonKeyExpired,
			false},
		{"disabled", `,"budget_strict":true,"disabled":true`, false, false, ReasonKeyDisabled, ReasonKeyDisabled,
			false},
		{"not strict", `,"budget_strict":false`, true, false, "", "", false},
		{"strict as a string", `,"budget_strict":"true"`, false, false, ReasonKeyShape + ":budget_strict",
			ReasonKeyShape + ":budget_strict", false},
		{"strict as an integer", `,"budget_strict":1`, false, false, ReasonKeyShape + ":budget_strict",
			ReasonKeyShape + ":budget_strict", false},
	} {
		k := ParseKey(body(v.extra))
		if k.Eligible != v.eligible || k.StrictLimitless != v.limitless || k.Reason != v.reason ||
			(k.Windows != nil) != v.windowsPresent {
			t.Errorf("%s: eligible %v limitless %v %q windows %v, want %v %v %q %v", v.name, k.Eligible,
				k.StrictLimitless, k.Reason, k.Windows != nil, v.eligible, v.limitless, v.reason, v.windowsPresent)
		}
		if ok, reason := k.PilotAt(now); ok != (v.pilot == "") || reason != v.pilot {
			t.Errorf("%s: PilotAt %v %q, want %q", v.name, ok, reason, v.pilot)
		}
	}
}

// TestAStrictKeysRowIsReadWithItAndGatesIt: FromContext keeps the row the
// key-context read found (store.StrictKeyLimit), and StrictRowReason names
// what Python's strict reserve would not match: no row read, no row (its
// 402), a mirrored limit; "" only for a row present and limitless.
func TestAStrictKeysRowIsReadWithItAndGatesIt(t *testing.T) {
	for _, v := range []struct {
		row  store.StrictKeyLimit
		want string
	}{
		{store.StrictKeyLimit{}, StrictRowUnread},
		{store.StrictKeyLimit{Row: true, Limitless: true}, StrictRowUnread},
		{store.StrictKeyLimit{Read: true}, StrictRowMissing},
		{store.StrictKeyLimit{Read: true, Limitless: true}, StrictRowMissing},
		{store.StrictKeyLimit{Read: true, Row: true}, StrictRowLimited},
		{store.StrictKeyLimit{Read: true, Row: true, Limitless: true}, ""},
	} {
		f := FromContext(store.KeyContext{
			KeyBody: `{"hash":"kh","salt":"s","secret_hash":"x","lookup_hash":"lh","name":"n","label":"l",` +
				`"workspace_id":"ws","creator_user_id":null,"budget_strict":true}`,
			RangeWorkspaceID: "ws", StrictLimit: v.row,
		})
		if f.StrictRow != v.row || !f.Key.StrictLimitless {
			t.Fatalf("%+v: the facts keep %+v, limitless %v", v.row, f.StrictRow, f.Key.StrictLimitless)
		}
		if got := f.StrictRowReason(); got != v.want {
			t.Errorf("%+v: %q, want %q", v.row, got, v.want)
		}
	}
}
