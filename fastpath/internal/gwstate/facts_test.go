package gwstate

import (
	"encoding/json"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

type pythonKey struct {
	Error  *string     `json:"error"`
	Reason *string     `json:"reason"`
	Tags   [][2]string `json:"tags"`
	// The ids Python's key carries when it is eligible.
	Hash        *string `json:"hash"`
	WorkspaceID *string `json:"workspace_id"`
	LookupHash  *string `json:"lookup_hash"`
}

type pythonWorkspace struct {
	Error  *string `json:"error"`
	Reason *string `json:"reason"`
	// PauseCauses is raw: a malformed body's causes need not be strings.
	PauseCauses json.RawMessage `json:"pause_causes"`
	// BilledWorkspaceID is the workspace id Python bills: the body's.
	BilledWorkspaceID *string `json:"billed_workspace_id"`
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
		ReasonKeyApp, ReasonKeyLimit, ReasonKeyWindowLimit, ReasonKeyBudgetStrict}
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
		ok, reason := facts.At(at)
		python := reasonOf(c.Python.Reason)
		if ok {
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
	want := []string{ReasonWorkspaceGone, ReasonWorkspacePaused, ReasonWorkspaceFederal}
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
// decide pipeline's (G10), and the service, which will make the cache for
// the decide shadow (G10's second part), may import it too.
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
