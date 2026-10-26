package gwauth

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// The P1 goldens (docs/design/fast-admission-e2-decide.md §10.1): every
// answered case's response, payload and rows, Python's own, with every id,
// clock and envelope pinned. Compose is given each case's pinned ids,
// clock, envelope and evaluator result (the candidates' endpoint ids and
// the estimate); everything else it composes from the routing snapshot,
// the request body and the key's facts.

type goldenFile struct {
	Policy struct {
		StageD struct {
			EligibilityEnabled          bool     `json:"eligibility_enabled"`
			HeartbeatEnabled            bool     `json:"heartbeat_enabled"`
			RequireHeartbeatDeclaration bool     `json:"require_heartbeat_declaration"`
			PilotWorkspaces             []string `json:"pilot_workspaces"`
		} `json:"stage_d"`
	} `json:"policy"`
	Volatile []string     `json:"volatile"`
	Cases    []goldenCase `json:"cases"`
}

type goldenCase struct {
	Name   string   `json:"name"`
	Labels []string `json:"labels"`
	AtUS   int64    `json:"at_us"`
	IDs    struct {
		AuthorizationID      string `json:"authorization_id"`
		ReservationID        string `json:"reservation_id"`
		MintedIdempotencyKey string `json:"minted_idempotency_key"`
	} `json:"ids"`
	Admitted struct {
		Envelope    string `json:"envelope"`
		EndOfLifeUS int64  `json:"end_of_life_us"`
		EndOfLife   string `json:"end_of_life"`
	} `json:"admitted"`
	Insert  string `json:"insert"`
	Request struct {
		Headers [][2]string `json:"headers"`
		Body    string      `json:"body"`
	} `json:"request"`
	Boot struct {
		Kid                    string `json:"kid"`
		DeclaresUsageHeartbeat bool   `json:"declares_usage_heartbeat"`
	} `json:"boot"`
	Key struct {
		KeyBody string `json:"key_body"`
	} `json:"key"`
	PolicyPatch map[string]json.RawMessage `json:"policy_patch"`
	Facts       struct {
		Key struct {
			Hash        string      `json:"hash"`
			WorkspaceID string      `json:"workspace_id"`
			Tags        [][2]string `json:"tags"`
		} `json:"key"`
	} `json:"facts"`
	Expect goldenExpect `json:"expect"`
}

type goldenExpect struct {
	Outcome          string      `json:"outcome"`
	Status           int         `json:"status"`
	Headers          [][2]string `json:"headers"`
	Response         string      `json:"response"`
	GenerationID     string      `json:"generation_id"`
	Estimate         int64       `json:"estimate_microdollars"`
	DecideRegion     string      `json:"decide_region"`
	Fingerprint      string      `json:"fingerprint"`
	IdempotencyScope string      `json:"idempotency_scope"`
	Timing           struct {
		TotalMS      int64 `json:"total_ms"`
		SpannerRPCs  int64 `json:"spanner_rpcs"`
		KeyLookupMS  int64 `json:"key_lookup_ms"`
		RoutingMS    int64 `json:"routing_ms"`
		StoreMS      int64 `json:"store_ms"`
		PostCommitMS int64 `json:"post_commit_ms"`
	} `json:"timing"`
	AuthorizationRow map[string]any `json:"authorization_row"`
	ReservationRow   map[string]any `json:"reservation_row"`
}

func (e goldenExpect) timing() Timing {
	t := e.Timing
	return Timing{t.TotalMS, t.SpannerRPCs, t.KeyLookupMS, t.RoutingMS, t.StoreMS, t.PostCommitMS}
}

var testdata = filepath.Join("..", "..", "testdata")

func loadGoldens(t *testing.T) goldenFile {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(testdata, "authorize_cases_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var f goldenFile
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&f); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(f.Volatile, []string{"data.timing"}) {
		t.Fatalf("volatile is %q; this test compares data.timing by shape only", f.Volatile)
	}
	return f
}

// snapshotFacts is what the routing snapshot says of each endpoint and of
// the regions, read from the published body (sorted keys) as the edge
// holds it.
type snapshotFacts struct {
	endpoints map[string]Candidate
	regions   []Region
}

func member(o pyjson.Object, key string) any {
	for _, m := range o {
		if m.Key == key {
			return m.Value
		}
	}
	return nil
}

// loadSnapshot reads the published body as the edge holds it, through the
// production mapping (routingsnapshot.Snapshot.Endpoint, CandidateOf,
// RegionsOf) the decide pipeline composes from.
func loadSnapshot(t *testing.T) snapshotFacts {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(testdata, "routing_snapshot_v2.body.json"))
	if err != nil {
		t.Fatal(err)
	}
	snap, err := routingsnapshot.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	s := snapshotFacts{endpoints: map[string]Candidate{}, regions: RegionsOf(snap.Settings.RegionPayload)}
	for _, e := range snap.Endpoints {
		facts, ok := snap.Endpoint(e.ID)
		if !ok {
			t.Fatalf("endpoint %q is not in its own snapshot", e.ID)
		}
		s.endpoints[e.ID] = CandidateOf(facts)
	}
	return s
}

func intOf(t *testing.T, v any) int64 {
	t.Helper()
	n, err := strconv.ParseInt(string(v.(json.Number)), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// stageDSettings is the header's policy with the case's patch applied.
func stageDSettings(t *testing.T, f goldenFile, c goldenCase) StageDSettings {
	t.Helper()
	sd := f.Policy.StageD
	// Unmarshal appends into a slice it is given: give it its own.
	sd.PilotWorkspaces = slices.Clone(sd.PilotWorkspaces)
	if raw, ok := c.PolicyPatch["stage_d"]; ok {
		if err := json.Unmarshal(raw, &sd); err != nil {
			t.Fatal(err)
		}
	}
	return StageDSettings{sd.EligibilityEnabled, sd.HeartbeatEnabled, sd.RequireHeartbeatDeclaration, sd.PilotWorkspaces}
}

// inputOf is the case as the decide pipeline would hand it to Stage. The
// request side is read from the body here as G5's Request and Identity
// will carry it (the fingerprint and scope are G5's, so they come from the
// case); the candidates' order and the estimate are the evaluator's, so
// they come from the case too, and every fact of each candidate from the
// snapshot.
func inputOf(t *testing.T, f goldenFile, snap snapshotFacts, c goldenCase) ComposeInput {
	t.Helper()
	tree, err := pyjson.Decode([]byte(c.Request.Body))
	if err != nil {
		t.Fatalf("%s: %v", c.Name, err)
	}
	body := tree.(pyjson.Object)
	// The key's tags as the edge reads them: the key's stored body, in
	// stored order. (The case's facts.key lists some keys' tags in the
	// order the generator built them, not the order stored, which is the
	// order Python merged.)
	var keyTags []ComposeTag
	keyTree, err := pyjson.Decode([]byte(c.Key.KeyBody))
	if err != nil {
		t.Fatalf("%s: the key body: %v", c.Name, err)
	}
	key := keyTree.(pyjson.Object)
	if member(key, "hash") != c.Facts.Key.Hash || member(key, "workspace_id") != c.Facts.Key.WorkspaceID {
		t.Fatalf("%s: the key body is not the facts' key", c.Name)
	}
	if stored, ok := member(key, "tags").(pyjson.Object); ok {
		for _, m := range stored {
			keyTags = append(keyTags, ComposeTag{Key: m.Key, Value: m.Value.(string)})
		}
	}
	if len(keyTags) != len(c.Facts.Key.Tags) {
		t.Fatalf("%s: the key body has %d tags, the facts %d", c.Name, len(keyTags), len(c.Facts.Key.Tags))
	}
	in := ComposeInput{
		At:                     time.UnixMicro(c.AtUS),
		AuthorizationID:        c.IDs.AuthorizationID,
		ReservationID:          c.IDs.ReservationID,
		IdempotencyKey:         c.IDs.MintedIdempotencyKey,
		IdempotencyFingerprint: c.Expect.Fingerprint,
		IdempotencyScope:       c.Expect.IdempotencyScope,
		WorkspaceID:            c.Facts.Key.WorkspaceID,
		KeyHash:                c.Facts.Key.Hash,
		KeyTags:                keyTags,
		RequestedModel:         strings.TrimSpace(member(body, "model").(string)),
		EstimatedInputTokens:   1,
		OutputEstimate:         512,
		Region:                 c.Expect.DecideRegion,
		Regions:                regionsOf(t, snap, c),
		Estimate:               c.Expect.Estimate,
		StageDReason:           StageDReasonOf(stageDSettings(t, f, c), c.Facts.Key.WorkspaceID, c.Boot.DeclaresUsageHeartbeat),
		BootKid:                c.Boot.Kid,
	}
	for _, h := range c.Request.Headers {
		if h[0] == "idempotency-key" && strings.TrimSpace(h[1]) != "" {
			in.IdempotencyKey = strings.TrimSpace(h[1])
		}
	}
	if k, ok := member(body, "idempotency_key").(string); ok {
		in.IdempotencyKey = strings.TrimSpace(k)
	}
	if n, ok := member(body, "invocation_nonce").(string); ok {
		n = strings.TrimSpace(n)
		in.InvocationNonce = &n
	}
	if v := member(body, "estimated_input_tokens"); v != nil {
		in.EstimatedInputTokens = intOf(t, v)
	}
	for _, field := range []string{"max_tokens", "max_completion_tokens", "max_output_tokens"} {
		if v := member(body, field); v != nil {
			in.OutputEstimate = intOf(t, v)
			break
		}
	}
	if tags, ok := member(body, "tags").(pyjson.Object); ok {
		for _, m := range tags {
			in.RequestTags = append(in.RequestTags, ComposeTag{Key: m.Key, Value: m.Value.(string)})
		}
	}
	// The evaluator's candidates, in its order: the endpoint ids Python
	// recorded; each endpoint's facts are the snapshot's.
	var payload struct {
		Endpoints []string `json:"candidate_endpoint_ids"`
	}
	if err := json.Unmarshal([]byte(c.Expect.AuthorizationRow["payload"].(string)), &payload); err != nil {
		t.Fatalf("%s: %v", c.Name, err)
	}
	for _, id := range payload.Endpoints {
		e, ok := snap.endpoints[id]
		if !ok {
			t.Fatalf("%s: endpoint %q is not in the snapshot", c.Name, id)
		}
		in.Candidates = append(in.Candidates, e)
	}
	return in
}

// regionsOf is the region payload of the snapshot the edge would hold for
// the case. A case whose policy patch turns multi_region_enabled off
// answers Python's region_payload for that setting (every region but the
// primary disabled, regions.py region_payload); the edge decides it only
// with a snapshot whose settings agree (else policy.regions_mismatch
// passes, §4), so the snapshot it holds says the same.
func regionsOf(t *testing.T, snap snapshotFacts, c goldenCase) []Region {
	t.Helper()
	raw, ok := c.PolicyPatch["multi_region_enabled"]
	if !ok {
		return snap.regions
	}
	var multi bool
	if err := json.Unmarshal(raw, &multi); err != nil {
		t.Fatal(err)
	}
	regions := slices.Clone(snap.regions)
	for i := range regions {
		regions[i].Enabled = multi || regions[i].Primary
	}
	return regions
}

func endOfLife(t *testing.T, c goldenCase) time.Time {
	t.Helper()
	eol, err := time.Parse(time.RFC3339Nano, c.Admitted.EndOfLife)
	if err != nil {
		t.Fatalf("%s: %v", c.Name, err)
	}
	if eol.UnixMicro() != c.Admitted.EndOfLifeUS {
		t.Fatalf("%s: end_of_life %q is not end_of_life_us %d", c.Name, c.Admitted.EndOfLife, c.Admitted.EndOfLifeUS)
	}
	return eol
}

func compose(t *testing.T, f goldenFile, snap snapshotFacts, c goldenCase) (ComposeInput, Composed) {
	t.Helper()
	in := inputOf(t, f, snap, c)
	staged, err := Stage(in)
	if err != nil {
		t.Fatalf("%s: Stage: %v", c.Name, err)
	}
	composed, err := staged.Finish(c.Admitted.Envelope, endOfLife(t, c))
	if err != nil {
		t.Fatalf("%s: Finish: %v", c.Name, err)
	}
	return in, composed
}

func answered(f goldenFile) []goldenCase {
	var out []goldenCase
	for _, c := range f.Cases {
		if c.Expect.Outcome == "answer" {
			out = append(out, c)
		}
	}
	return out
}

func hasLabel(c goldenCase, label string) bool { return slices.Contains(c.Labels, label) }

// TestEveryAnsweredCaseIsPythonsBytes: the 200 body byte for byte, with
// the case's timing (P1 pins perf_counter, so its values are the ones in
// the body), and after a read-back for an unknown_landed case.
func TestEveryAnsweredCaseIsPythonsBytes(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	cases := answered(f)
	equal := 0
	for _, c := range cases {
		_, composed := compose(t, f, snap, c)
		readBack := c.Insert == "unknown_landed"
		if !readBack && c.Insert != "inserted" {
			t.Fatalf("%s: an answer after insert %q", c.Name, c.Insert)
		}
		got := composed.Render(c.Expect.timing(), readBack)
		if string(got) != c.Expect.Response {
			t.Errorf("%s: the 200 differs\n got %s\nwant %s", c.Name, got, c.Expect.Response)
			continue
		}
		if composed.GenerationID != c.Expect.GenerationID {
			t.Errorf("%s: generation id %s, want %s", c.Name, composed.GenerationID, c.Expect.GenerationID)
			continue
		}
		equal++
	}
	t.Logf("%d of %d answered cases byte-equal", equal, len(cases))
	if len(cases) < 150 {
		t.Fatalf("only %d answered cases: the goldens are not the ones this test was written against", len(cases))
	}
}

// TestEveryPayloadAndRowIsPythons: the payload byte for byte, and every
// column of both rows field for field (the columns Go does not write are
// the NULLs and constants the case records).
func TestEveryPayloadAndRowIsPythons(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	cases := answered(f)
	payloads, rows := 0, 0
	for _, c := range cases {
		_, composed := compose(t, f, snap, c)
		a, r := composed.Authorization, composed.Reservation
		wantA, wantR := c.Expect.AuthorizationRow, c.Expect.ReservationRow
		if a.Payload == wantA["payload"] {
			payloads++
		} else {
			t.Errorf("%s: payload differs\n got %s\nwant %s", c.Name, a.Payload, wantA["payload"])
		}
		var nonce any
		if a.InvocationNonce != nil {
			nonce = *a.InvocationNonce
		}
		gotA := map[string]any{
			"authorization_id": a.AuthorizationID, "workspace_id": a.WorkspaceID, "key_hash": a.KeyHash,
			"reservation_id": a.ReservationID, "model_id": a.ModelID, "provider": a.Provider,
			"usage_type": a.UsageType, "estimated_microdollars": json.Number(strconv.FormatInt(a.EstimatedMicrodollars, 10)),
			"settled": false, "created_at_us": json.Number(strconv.FormatInt(a.CreatedAt.UnixMicro(), 10)),
			"payload": a.Payload, "idempotency_fingerprint": a.IdempotencyFingerprint,
			"stage_d_boot_kid": a.StageDBootKid, "invocation_nonce": nonce,
		}
		gotR := map[string]any{
			"reservation_id": r.ReservationID, "workspace_id": r.WorkspaceID, "key_hash": r.KeyHash,
			"ws_shard": json.Number("0"), "credit_shard": json.Number("0"), "key_shard": json.Number("0"),
			"credit_reserved_micro": json.Number("0"), "key_reserved_micro": json.Number("0"),
			"hold_usage_type": r.HoldUsageType, "authorization_id": r.AuthorizationID,
			"idempotency_scope": r.IdempotencyScope, "idempotency_fingerprint": r.IdempotencyFingerprint,
			"expires_at_us": json.Number(strconv.FormatInt(r.ExpiresAt.UnixMicro(), 10)),
			// The commit's timestamp, as the store writes it (§5).
			"created_at": "commit_timestamp",
		}
		ok := true
		for column, want := range wantA {
			got, written := gotA[column]
			if !written {
				// A column Go leaves NULL (§5): the case must say NULL.
				if want != nil {
					t.Errorf("%s: authorization column %s is %v in Python's row; Go writes NULL", c.Name, column, want)
					ok = false
				}
				continue
			}
			if got != want {
				t.Errorf("%s: authorization column %s = %v, want %v", c.Name, column, got, want)
				ok = false
			}
		}
		if len(wantR) != len(gotR) {
			t.Errorf("%s: reservation has %d columns, Go %d", c.Name, len(wantR), len(gotR))
			ok = false
		}
		for column, want := range wantR {
			if got := gotR[column]; got != want {
				t.Errorf("%s: reservation column %s = %v, want %v", c.Name, column, got, want)
				ok = false
			}
		}
		// The row timestamps are UTC microseconds (Spanner keeps
		// nanoseconds, Python's datetime microseconds).
		for _, ts := range []time.Time{a.CreatedAt, r.ExpiresAt} {
			if ts.Location() != time.UTC || ts.Nanosecond()%1000 != 0 {
				t.Errorf("%s: row instant %v is not UTC microseconds", c.Name, ts)
				ok = false
			}
		}
		if ok {
			rows++
		}
	}
	t.Logf("%d of %d payloads byte-equal; %d of %d row pairs field-equal", payloads, len(cases), rows, len(cases))
}

// TestTheReadBackSortsTags: P1's read_back.sorted_tags case answers with
// the payload's sorted tags; the inserted rendering of the same case
// (merge order) is not Python's bytes for it.
func TestTheReadBackSortsTags(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	found := 0
	for _, c := range answered(f) {
		if !hasLabel(c, "read_back.sorted_tags") {
			continue
		}
		found++
		in, composed := compose(t, f, snap, c)
		if len(in.KeyTags) == 0 || len(in.RequestTags) == 0 {
			t.Fatalf("%s: not a merge of key and request tags", c.Name)
		}
		if got := composed.Render(c.Expect.timing(), true); string(got) != c.Expect.Response {
			t.Errorf("%s: read-back differs\n got %s\nwant %s", c.Name, got, c.Expect.Response)
		}
		// Positive control: merge order is not the read-back's.
		if got := composed.Render(c.Expect.timing(), false); string(got) == c.Expect.Response {
			t.Errorf("%s: the inserted rendering equals the read-back's; the case does not test the sort", c.Name)
		}
		if !strings.Contains(c.Expect.Response, `"tags":{"alpha":"2","beta":"k","mid":"k","zeta":"1"}`) {
			t.Errorf("%s: the golden's tags are not the sorted merge", c.Name)
		}
	}
	if found != 1 {
		t.Fatalf("%d read_back.sorted_tags cases, want 1", found)
	}
}

// TestTheUnavailableBodyIsPythons: decision 1's 503, byte for byte with
// the case's timing, and its one header, for each insert whose outcome is
// not known and not landed (a lost answer, and the refusals that prove
// nothing: §8.4).
func TestTheUnavailableBodyIsPythons(t *testing.T) {
	f := loadGoldens(t)
	found := 0
	unknown := map[string]bool{"unknown_not_landed": true, "failed_precondition": true, "aborted": true}
	for _, c := range f.Cases {
		if c.Expect.Outcome != "unavailable" {
			continue
		}
		found++
		if !unknown[c.Insert] || c.Expect.Status != UnavailableStatus {
			t.Errorf("%s: insert %q, status %d", c.Name, c.Insert, c.Expect.Status)
		}
		if got := Unavailable(c.Expect.timing()); string(got) != c.Expect.Response {
			t.Errorf("%s: the 503 differs\n got %s\nwant %s", c.Name, got, c.Expect.Response)
		}
		if !slices.Equal(c.Expect.Headers, [][2]string{{"retry-after", UnavailableRetryAfter}}) {
			t.Errorf("%s: headers %q", c.Name, c.Expect.Headers)
		}
	}
	if found != len(unknown) {
		t.Fatalf("%d unavailable cases, want %d", found, len(unknown))
	}
}

// timingSplit cuts a body at data.timing: what comes before it, the
// timing object, and what follows.
func timingSplit(t *testing.T, body []byte) (head, timing, tail []byte) {
	t.Helper()
	i := bytes.LastIndex(body, []byte(`"timing":{`))
	if i < 0 {
		t.Fatalf("no timing in %s", body)
	}
	start := i + len(`"timing":`)
	end := start + bytes.IndexByte(body[start:], '}') + 1
	return body[:start], body[start:end], body[end:]
}

// TestTimingIsComparedByShape: §10.1 holds data.timing volatile. With
// other values Go's body differs from the golden only inside the timing
// object, whose keys and their order stay §6's.
func TestTimingIsComparedByShape(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	other := Timing{TotalMS: 12, SpannerRPCs: 2, KeyLookupMS: 0, RoutingMS: 3, StoreMS: 7, PostCommitMS: 1}
	wantKeys := []string{"total_ms", "spanner_rpcs", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms"}
	check := func(name string, got, want []byte) {
		gh, gt, gtail := timingSplit(t, got)
		wh, wt, wtail := timingSplit(t, want)
		if !bytes.Equal(gh, wh) || !bytes.Equal(gtail, wtail) {
			t.Errorf("%s: differs outside timing", name)
		}
		if bytes.Equal(gt, wt) {
			t.Errorf("%s: other timing values wrote the same timing %s", name, gt)
		}
		for _, timing := range [][]byte{gt, wt} {
			tree, err := pyjson.Decode(timing)
			if err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			var keys []string
			for _, m := range tree.(pyjson.Object) {
				keys = append(keys, m.Key)
			}
			if !slices.Equal(keys, wantKeys) {
				t.Errorf("%s: timing keys %q", name, keys)
			}
		}
	}
	for _, c := range f.Cases {
		switch c.Expect.Outcome {
		case "answer":
			_, composed := compose(t, f, snap, c)
			check(c.Name, composed.Render(other, c.Insert == "unknown_landed"), []byte(c.Expect.Response))
		case "unavailable":
			check(c.Name, Unavailable(other), []byte(c.Expect.Response))
		}
	}
}

// TestStageDReasonIsTheLabel: every answered case's stage_d.<reason>
// label is the reason StageDReasonOf gives from the policy and the boot,
// and every cohort reason is covered.
func TestStageDReasonIsTheLabel(t *testing.T) {
	f := loadGoldens(t)
	seen := map[string]bool{}
	for _, c := range answered(f) {
		got := StageDReasonOf(stageDSettings(t, f, c), c.Facts.Key.WorkspaceID, c.Boot.DeclaresUsageHeartbeat)
		if !hasLabel(c, "stage_d."+got) {
			t.Errorf("%s: reason %s, labels %q", c.Name, got, c.Labels)
		}
		seen[got] = true
	}
	for _, r := range []string{"stage_d_disabled", "workspace_not_pilot", "heartbeats_disabled", "heartbeat_undeclared", "not_streaming"} {
		if !seen[r] {
			t.Errorf("no answered case has stage_d reason %s", r)
		}
	}
}

// TestEndOfLifeKeepsItsOffset: the payload's fastpath_end_of_life for the
// offsets and fractions P1 pins, and the forms Python writes beyond them.
func TestEndOfLifeKeepsItsOffset(t *testing.T) {
	f := loadGoldens(t)
	offsets := 0
	for _, c := range answered(f) {
		var payload struct {
			EndOfLife string `json:"fastpath_end_of_life"`
		}
		if err := json.Unmarshal([]byte(c.Expect.AuthorizationRow["payload"].(string)), &payload); err != nil {
			t.Fatal(err)
		}
		if got := EndOfLifeText(endOfLife(t, c)); got != payload.EndOfLife {
			t.Errorf("%s: %q, want %q", c.Name, got, payload.EndOfLife)
		}
		if hasLabel(c, "end_of_life.offset") {
			offsets++
		}
	}
	if offsets < 2 {
		t.Errorf("%d end_of_life.offset cases", offsets)
	}
	base := time.Date(2026, 10, 19, 5, 0, 0, 0, time.UTC)
	for _, v := range []struct {
		at   time.Time
		want string
	}{
		{base, "2026-10-19T05:00:00Z"},
		{base.Add(999), "2026-10-19T05:00:00Z"},
		{base.Add(1000), "2026-10-19T05:00:00.000001Z"},
		{base.In(time.FixedZone("", 0)), "2026-10-19T05:00:00Z"},
		{base.In(time.FixedZone("", -(5*3600 + 30*60))), "2026-10-18T23:30:00-05:30"},
	} {
		if got := EndOfLifeText(v.at); got != v.want {
			t.Errorf("EndOfLifeText(%v) = %q, want %q", v.at, got, v.want)
		}
	}
}

// TestFinishRefusesAnOffsetWithSeconds: Go's JSON writes an offset to the
// minute, so Python would parse another instant than the owner meant, and
// no text Go writes is Python's. Finish refuses (the caller refunds and
// passes); a whole-minute offset is the positive control.
func TestFinishRefusesAnOffsetWithSeconds(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	c := answered(f)[0]
	staged, err := Stage(inputOf(t, f, snap, c))
	if err != nil {
		t.Fatal(err)
	}
	base := time.Date(2026, 10, 19, 5, 0, 0, 0, time.UTC)
	if _, err := staged.Finish(c.Admitted.Envelope, base.In(time.FixedZone("", 3600+30))); err == nil {
		t.Fatal("Finish wrote an end of life whose offset has seconds")
	}
	if _, err := staged.Finish(c.Admitted.Envelope, base.In(time.FixedZone("", -(5*3600+30*60)))); err != nil {
		t.Fatalf("a whole-minute offset: %v", err)
	}
}

// TestStageRefusesAnAuthorizationIDThatIsNotAUUID4: the id's 32 digits are
// a uuid4's, version and variant, as the reservation id's are; any other is
// not one this edge or Python minted.
func TestStageRefusesAnAuthorizationIDThatIsNotAUUID4(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	in := inputOf(t, f, snap, answered(f)[0])
	if _, err := Stage(in); err != nil {
		t.Fatalf("the golden's own id: %v", err)
	}
	for _, id := range []string{"gwa-da235f8103b551818b0a9a656b52460a", "gwa-da235f8103b541817b0a9a656b52460a",
		"gwa-DA235F8103B541818B0A9A656B52460A"} {
		in.AuthorizationID = id
		if _, err := Stage(in); err == nil {
			t.Errorf("Stage took authorization id %q", id)
		}
	}
}
