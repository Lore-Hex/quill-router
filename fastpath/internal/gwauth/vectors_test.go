package gwauth

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// The P1 vectors (docs/design/fast-admission-e2-decide.md, §10.1 and
// §10.2): Python's real outcome for every body, written by
// scripts/fastpath/authorize_vectors.py.
const (
	fingerprintFile = "../../testdata/authorize_fingerprint_v1.json"
	casesFile       = "../../testdata/authorize_cases_v1.json"
	snapshotFile    = "../../testdata/routing_snapshot_v1.body.json"
)

// passRequired are §10.2's labels on which Go must pass.
var passRequired = map[string]bool{
	"float_present": true, "lax_coercion": true, "duplicate_key": true, "strip_ambiguous": true,
	"dict_key_strip": true, "non_ascii_tag": true, "referer_not_strict": true, "canonical_model_rewrite": true,
	"native_batch_key": true, "partner_key": true, "idempotency_key_too_long": true,
	"idempotency_header_repeated": true, "idempotency_header_non_ascii": true, "content_type": true,
	"deep_nesting": true, "int_over_4000_digits": true, "bom": true, "lone_surrogate": true,
	"estimate_overflow": true,
}

type fingerprintFileT struct {
	Policy struct {
		InternalModels []string `json:"internal_models"`
	} `json:"policy"`
	Cases []struct {
		Name        string     `json:"name"`
		Labels      []string   `json:"labels"`
		WorkspaceID string     `json:"workspace_id"`
		KeyHash     string     `json:"key_hash"`
		Headers     [][]string `json:"headers"`
		Body        string     `json:"body"`
		Expect      struct {
			Outcome          string  `json:"outcome"`
			Fingerprint      string  `json:"fingerprint"`
			IdempotencyKey   *string `json:"idempotency_key"`
			IdempotencyScope *string `json:"idempotency_scope"`
			Status           int     `json:"status"`
			Type             string  `json:"type"`
		} `json:"expect"`
	} `json:"cases"`
}

func readJSON(t testing.TB, path string, into any) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, into); err != nil {
		t.Fatal(err)
	}
}

func goldenSnapshot(t testing.TB) *routingsnapshot.Snapshot {
	t.Helper()
	raw, err := os.ReadFile(snapshotFile)
	if err != nil {
		t.Fatal(err)
	}
	s, err := routingsnapshot.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// headersOf reads a vector's header pairs as the edge would hand them over.
func headersOf(pairs [][]string) Headers {
	var h Headers
	for _, p := range pairs {
		switch strings.ToLower(p[0]) {
		case "content-type":
			h.ContentType = append(h.ContentType, p[1])
		case "idempotency-key":
			h.IdempotencyKey = append(h.IdempotencyKey, p[1])
		case "x-tr-settlement-mode":
			h.SettlementMode = append(h.SettlementMode, p[1])
		case "x-quill-video-allowed-providers":
			h.VideoAllowedProviders = true
		}
	}
	return h
}

// peak is the instant P1 sent every fingerprint body at (the routing
// cases' "peak", 2026-10-19T02:00:00Z).
var peak = time.Unix(1792375200, 0).UTC()

// fingerprintKeyTags are the stored tags of P1's fingerprint key.
var fingerprintKeyTags = []Tag{{"team", "e2"}}

const minted = "00000000-0000-4000-8000-000000000000"

// decision is what Go's request side, the merged tags and the evaluator
// make of one request: answered with an identity, or passed with a reason.
type decision struct {
	answered bool
	reason   string
	identity Identity
}

func decide(t testing.TB, s *routingsnapshot.Snapshot, body []byte, h Headers, ws, keyHash string, keyTags []Tag,
	at time.Time) decision {
	r, p := ParseRequest(body, h)
	if p.Passed() {
		return decision{reason: p.Reason}
	}
	if _, p := MergeTags(keyTags, true, r); p.Passed() {
		return decision{reason: p.Reason}
	}
	id, p := IdentityOf(r, h, ws, keyHash, func() string { return minted })
	if p.Passed() {
		return decision{reason: p.Reason}
	}
	req := r.Routing
	req.At = at
	if res := s.Evaluate(req); res.Outcome != routingsnapshot.Answered {
		return decision{reason: "evaluate", identity: id}
	}
	return decision{answered: true, identity: id}
}

// TestEveryFingerprintVector holds Go to Python over every P1 body: a body
// Go answers has exactly Python's fingerprint and scope; Go never answers a
// body with a pass-required label or one Python refused. It also checks
// the fingerprint of every body Go parses whose pass is a cohort reason
// that leaves the material alone, so parity is shown over more than the
// answered set.
func TestEveryFingerprintVector(t *testing.T) {
	var file fingerprintFileT
	readJSON(t, fingerprintFile, &file)
	s := goldenSnapshot(t)
	if len(file.Cases) < 14_000 {
		t.Fatalf("%d cases", len(file.Cases))
	}
	var answered, agreed, refusedPassed, labelledPassed, unlabelledPassed, extraAgreed int
	reasons := map[string]int{}
	for _, c := range file.Cases {
		h := headersOf(c.Headers)
		d := decide(t, s, []byte(c.Body), h, c.WorkspaceID, c.KeyHash, fingerprintKeyTags, peak)
		labelled := slices.ContainsFunc(c.Labels, func(l string) bool { return passRequired[l] })
		if !d.answered {
			reasons[d.reason]++
			switch {
			case c.Expect.Outcome == "refused":
				refusedPassed++
			case labelled:
				labelledPassed++
			default:
				unlabelledPassed++
			}
		}
		if d.answered {
			answered++
			if c.Expect.Outcome != "fingerprint" {
				t.Errorf("%s: Go answers a body Python refused (%d %s)", c.Name, c.Expect.Status, c.Expect.Type)
				continue
			}
			if labelled {
				t.Errorf("%s: Go answers a body labelled %v", c.Name, c.Labels)
				continue
			}
			if !agrees(t, c.Name, d.identity, c.Expect.Fingerprint, c.Expect.IdempotencyKey,
				c.Expect.IdempotencyScope) {
				continue
			}
			agreed++
			continue
		}
		// Parity beyond the answered set: a body Python answered that Go
		// parses and passes only for a cohort reason that leaves the
		// material alone still has Python's fingerprint.
		if c.Expect.Outcome != "fingerprint" || labelled {
			continue
		}
		r, _, p := parseBody([]byte(c.Body), h)
		if p.Passed() || r.idempotencyHeaders(h).Passed() {
			continue
		}
		id, p := IdentityOf(r, h, c.WorkspaceID, c.KeyHash, func() string { return minted })
		if p.Passed() {
			t.Errorf("%s: no identity for a parsed body: %s", c.Name, p.Reason)
			continue
		}
		if agrees(t, c.Name, id, c.Expect.Fingerprint, c.Expect.IdempotencyKey, c.Expect.IdempotencyScope) {
			extraAgreed++
		}
	}
	names := make([]string, 0, len(reasons))
	for r := range reasons {
		names = append(names, r)
	}
	sort.Strings(names)
	for _, r := range names {
		t.Logf("pass %-26s %5d", r, reasons[r])
	}
	t.Logf("%d bodies: Go answered %d (fingerprint and scope equal: %d); passed %d Python refused, %d labelled "+
		"pass-required, %d others; fingerprint equal on %d further parsed bodies", len(file.Cases), answered, agreed,
		refusedPassed, labelledPassed, unlabelledPassed, extraAgreed)
	// Positive control: the corpus exercises the answered path, the
	// refusals and the labels, and parity holds over thousands of bodies.
	if answered < 1000 || refusedPassed < 5000 || labelledPassed < 2000 || agreed != answered {
		t.Fatalf("coverage: answered %d agreed %d refused-passed %d labelled-passed %d", answered, agreed,
			refusedPassed, labelledPassed)
	}
}

// agrees compares Go's identity with Python's: the fingerprint always; the
// presented key and its scope when Python had one; a minted key otherwise.
func agrees(t testing.TB, name string, id Identity, fingerprint string, key, scope *string) bool {
	t.Helper()
	ok := true
	if id.Fingerprint != fingerprint {
		t.Errorf("%s: fingerprint %s over %s, Python's %s", name, id.Fingerprint, id.Material, fingerprint)
		ok = false
	}
	switch {
	case key == nil:
		if id.Presented || id.IdempotencyKey != minted {
			t.Errorf("%s: Go presented %q where Python minted", name, id.IdempotencyKey)
			ok = false
		}
	case !id.Presented || id.IdempotencyKey != *key || scope == nil || id.Scope != *scope:
		t.Errorf("%s: key %q scope %q, Python's %q %v", name, id.IdempotencyKey, id.Scope, *key, scope)
		ok = false
	}
	return ok
}

type casesFileT struct {
	InternalModels       []string `json:"internal_models"`
	EstimatedUsageModels []string `json:"estimated_usage_models"`
	Policy               struct {
		InternalModels []string `json:"internal_models"`
	} `json:"policy"`
	Cases []struct {
		Name    string `json:"name"`
		AtUS    int64  `json:"at_us"`
		Insert  string `json:"insert"`
		Request struct {
			Headers    [][]string `json:"headers"`
			Body       string     `json:"body"`
			BodyExpand *struct {
				Marker string `json:"marker"`
				Repeat string `json:"repeat"`
				Count  int    `json:"count"`
			} `json:"body_expand"`
		} `json:"request"`
		IDs struct {
			Minted string `json:"minted_idempotency_key"`
		} `json:"ids"`
		Key struct {
			KeyBody *string `json:"key_body"`
		} `json:"key"`
		Facts struct {
			Key struct {
				Hash        string `json:"hash"`
				WorkspaceID string `json:"workspace_id"`
			} `json:"key"`
		} `json:"facts"`
		Expect struct {
			Outcome          string `json:"outcome"`
			Reason           string `json:"reason"`
			Response         string `json:"response"`
			Fingerprint      string `json:"fingerprint"`
			Material         string `json:"fingerprint_material"`
			IdempotencyScope string `json:"idempotency_scope"`
		} `json:"expect"`
	} `json:"cases"`
}

// storedTags reads a key body's tags as merge_tags does: a non-object is
// none; an object with a value that is not a string is unusable.
func storedTags(t *testing.T, keyBody *string) ([]Tag, bool) {
	t.Helper()
	if keyBody == nil {
		return nil, true
	}
	tree, err := pyjson.Decode([]byte(*keyBody))
	if err != nil {
		t.Fatal(err)
	}
	var raw any
	for _, m := range tree.(pyjson.Object) {
		if m.Key == "tags" {
			raw = m.Value
		}
	}
	object, ok := raw.(pyjson.Object)
	if !ok {
		return nil, true
	}
	tags := make([]Tag, 0, len(object))
	for _, m := range object {
		v, ok := m.Value.(string)
		if !ok {
			return nil, false
		}
		tags = append(tags, Tag{m.Key, v})
	}
	return tags, true
}

// TestEveryAuthorizeCase holds the request side to P1's response cases:
// a pass case whose reason is a request reason (or tags.merged_invalid)
// passes with exactly that reason; every other case passes no request
// reason; an answer has Python's exact fingerprint material, fingerprint
// and scope, and its merged tags are the response's, in order.
func TestEveryAuthorizeCase(t *testing.T) {
	var file casesFileT
	readJSON(t, casesFile, &file)
	requestReasons, answers := 0, 0
	for _, c := range file.Cases {
		body := c.Request.Body
		if e := c.Request.BodyExpand; e != nil {
			if strings.Count(body, e.Marker) != 1 {
				t.Fatalf("%s: the marker is not once in the body", c.Name)
			}
			body = strings.Replace(body, e.Marker, strings.Repeat(e.Repeat, e.Count), 1)
		}
		h := headersOf(c.Request.Headers)
		keyTags, usable := storedTags(t, c.Key.KeyBody)
		r, p := ParseRequest([]byte(body), h)
		if !p.Passed() {
			_, p = MergeTags(keyTags, usable, r)
		}
		want := ""
		if c.Expect.Outcome == "pass" && (strings.HasPrefix(c.Expect.Reason, "body.") ||
			strings.HasPrefix(c.Expect.Reason, "header.") || c.Expect.Reason == ReasonTagsMergedInvalid) {
			want = c.Expect.Reason
			requestReasons++
		}
		if p.Reason != want {
			t.Errorf("%s: request side passes %q, want %q", c.Name, p.Reason, want)
			continue
		}
		if c.Expect.Outcome != "answer" || p.Passed() {
			continue
		}
		answers++
		id, p := IdentityOf(r, h, c.Facts.Key.WorkspaceID, c.Facts.Key.Hash, func() string { return c.IDs.Minted })
		if p.Passed() {
			t.Errorf("%s: %s", c.Name, p.Reason)
			continue
		}
		if string(id.Material) != c.Expect.Material || id.Fingerprint != c.Expect.Fingerprint ||
			id.Scope != c.Expect.IdempotencyScope {
			t.Errorf("%s: material %s fingerprint %s scope %s\nwant %s %s %s", c.Name, id.Material, id.Fingerprint,
				id.Scope, c.Expect.Material, c.Expect.Fingerprint, c.Expect.IdempotencyScope)
		}
		if c.Insert != "inserted" {
			continue // after a read-back Python sorts the tags (§6)
		}
		merged, _ := MergeTags(keyTags, usable, r)
		var response struct {
			Data struct {
				Tags json.RawMessage `json:"tags"`
			} `json:"data"`
		}
		if err := json.Unmarshal([]byte(c.Expect.Response), &response); err != nil {
			t.Fatal(err)
		}
		object := make(pyjson.Object, len(merged))
		for i, tag := range merged {
			object[i] = pyjson.Member{Key: tag.Key, Value: tag.Value}
		}
		got, _ := pyjson.Dumps(object, pyjson.Options{})
		if string(got) != string(response.Data.Tags) {
			t.Errorf("%s: merged tags %s, the response's %s", c.Name, got, response.Data.Tags)
		}
	}
	// Positive control: every request reason has a case, and so does the
	// answered path.
	if requestReasons < 50 || answers < 100 {
		t.Fatalf("%d request-reason cases, %d answers", requestReasons, answers)
	}
}

// TestTheInternalModelsArePythons: Go's hardcoded set is P1's header set
// (MONITOR_MODEL_ID and every internal_only model) and the policy's.
func TestTheInternalModelsArePythons(t *testing.T) {
	var cases casesFileT
	readJSON(t, casesFile, &cases)
	var fingerprints fingerprintFileT
	readJSON(t, fingerprintFile, &fingerprints)
	for name, set := range map[string][]string{"cases header": cases.InternalModels,
		"cases policy": cases.Policy.InternalModels, "fingerprint policy": fingerprints.Policy.InternalModels} {
		if !slices.Equal(set, InternalModels) {
			t.Errorf("%s: Python's internal models %v, Go's %v", name, set, InternalModels)
		}
	}
	if len(InternalModels) == 0 {
		t.Fatal("no internal models")
	}
}

// TestTheModelAliasesArePythons holds routerModelIDAliases to
// model_aliases._ROUTER_MODEL_ID_ALIASES as written: the dict literal's
// "key": "value" lines, which the Python source keeps one a line.
func TestTheModelAliasesArePythons(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "src", "trusted_router", "model_aliases.py"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	start := strings.Index(text, "_ROUTER_MODEL_ID_ALIASES = {\n")
	if start < 0 {
		t.Fatal("_ROUTER_MODEL_ID_ALIASES is not a dict literal at the top level")
	}
	block := text[start+len("_ROUTER_MODEL_ID_ALIASES = {\n"):]
	block = block[:strings.Index(block, "\n}\n")]
	got := map[string]string{}
	for _, line := range strings.Split(block, "\n") {
		var key, value string
		line = strings.TrimSuffix(strings.TrimSpace(line), ",")
		k, v, ok := strings.Cut(line, ": ")
		if !ok || json.Unmarshal([]byte(k), &key) != nil || json.Unmarshal([]byte(v), &value) != nil {
			t.Fatalf("an alias line Go does not read: %q", line)
		}
		got[key] = value
	}
	if len(got) == 0 || len(got) != len(routerModelIDAliases) {
		t.Fatalf("Python's aliases %v, Go's %v", got, routerModelIDAliases)
	}
	for k, v := range got {
		if routerModelIDAliases[k] != v {
			t.Errorf("alias %q: Python %q, Go %q", k, v, routerModelIDAliases[k])
		}
	}
}
