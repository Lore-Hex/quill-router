package edge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The P1 goldens (docs/design/fast-admission-e2-decide.md §10.1) through the
// decide pipeline: each case's world (the key, workspace and credit rows,
// BYOK and broadcast ids, the boot, the policy patch) as the edge would hold
// it, read through the real gwstate cache from a fake store, and the
// request as the edge received it. The pipeline derives the region, the
// fingerprint and the scope itself; only the ids, the clock and the
// envelope are the case's.

type authorizeFile struct {
	Policy               map[string]json.RawMessage `json:"policy"`
	InternalModels       []string                   `json:"internal_models"`
	EstimatedUsageModels []string                   `json:"estimated_usage_models"`
	InternalToken        string                     `json:"internal_token"`
	BootTestKey          struct {
		Kid string `json:"kid"`
		JWK struct {
			X string `json:"x"`
		} `json:"jwk"`
	} `json:"boot_test_key"`
	Cases []authorizeCase `json:"cases"`
	// SnapshotBody is the routing snapshot the cases compose over when the
	// file carries its own (P6's async file); empty, the golden v3 body.
	SnapshotBody string `json:"snapshot_body"`
}

type creditRowCase struct {
	Shard              int64    `json:"shard"`
	InDebt             bool     `json:"in_debt"`
	TrustTier          int64    `json:"trust_tier"`
	TrustLatched       bool     `json:"trust_latched"`
	BillingPauseCauses []string `json:"billing_pause_causes"`
}

type authorizeCase struct {
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
		Path       string      `json:"path"`
		Headers    [][2]string `json:"headers"`
		Body       string      `json:"body"`
		BodyExpand *struct {
			Marker string `json:"marker"`
			Repeat string `json:"repeat"`
			Count  int    `json:"count"`
		} `json:"body_expand"`
	} `json:"request"`
	Boot *struct {
		Kid                    string `json:"kid"`
		Verified               bool   `json:"verified"`
		ImageDigest            string `json:"image_digest"`
		DeclaresUsageHeartbeat bool   `json:"declares_usage_heartbeat"`
	} `json:"boot"`
	Key struct {
		LookupBody *string `json:"lookup_body"`
		KeyBody    *string `json:"key_body"`
	} `json:"key"`
	Workspace struct {
		Body       *string         `json:"body"`
		CreditRows []creditRowCase `json:"credit_rows"`
	} `json:"workspace"`
	BYOKEntityIDs       []string                   `json:"byok_entity_ids"`
	BroadcastPointerIDs []string                   `json:"broadcast_pointer_ids"`
	PolicyPatch         map[string]json.RawMessage `json:"policy_patch"`
	Facts               struct {
		Key struct {
			LookupHash  string `json:"lookup_hash"`
			Hash        string `json:"hash"`
			WorkspaceID string `json:"workspace_id"`
		} `json:"key"`
	} `json:"facts"`
	Expect authorizeExpect `json:"expect"`
}

type authorizeExpect struct {
	Outcome             string           `json:"outcome"`
	Status              int              `json:"status"`
	Reason              string           `json:"reason"`
	Response            string           `json:"response"`
	GenerationID        string           `json:"generation_id"`
	Estimate            int64            `json:"estimate_microdollars"`
	Reservation         int64            `json:"reservation_microdollars"`
	DecideRegion        string           `json:"decide_region"`
	Fingerprint         string           `json:"fingerprint"`
	IdempotencyScope    string           `json:"idempotency_scope"`
	FingerprintMaterial string           `json:"fingerprint_material"`
	Timing              map[string]int64 `json:"timing"`
	AuthorizationRow    map[string]any   `json:"authorization_row"`
	ReservationRow      map[string]any   `json:"reservation_row"`
	Python              struct {
		Status int     `json:"status"`
		Type   *string `json:"type"`
		Leased bool    `json:"leased"`
		// Reservation is the credit reservation Python's typed store wrote
		// for a request it answered without a lease (§10.1): nil when it
		// wrote none.
		Reservation *int64 `json:"reservation_microdollars"`
		// Response and AsyncShape are P6's for a passed async-v1 case:
		// Python's 200 (but for shape C) and its async members' shape.
		Response   string `json:"response"`
		AsyncShape string `json:"async_shape"`
	} `json:"python"`
	// AsyncShape is P6's shape of an answered case's async members (none,
	// A or B).
	AsyncShape string `json:"async_shape"`
	// Affinity is a cache affinity case's record (§1 item 5, §10.1): the
	// case's answer is Python's with no session (the cold order, which Go
	// decides), and this is Python's answer with its sessions remembered,
	// classed by the generator's rule (affinity_cold, the same bytes, or
	// affinity_warm, a re-rank).
	Affinity *affinityExpect `json:"affinity"`
}

type affinityExpect struct {
	Class          string           `json:"class"`
	Sessions       [][]any          `json:"sessions"`
	Response       string           `json:"response"`
	Timing         map[string]int64 `json:"timing"`
	Candidates     []string         `json:"candidates"`
	ColdCandidates []string         `json:"cold_candidates"`
	Estimate       int64            `json:"estimate_microdollars"`
	ColdEstimate   int64            `json:"cold_estimate_microdollars"`
}

// The generator's classes of Python's answer with sessions remembered
// (scripts/fastpath/affinity_rule.py).
const (
	goldenAffinityCold = "affinity_cold"
	goldenAffinityWarm = "affinity_warm"
)

func timingOfMap(t map[string]int64) gwauth.Timing {
	return gwauth.Timing{TotalMS: t["total_ms"], SpannerRPCs: t["spanner_rpcs"], KeyLookupMS: t["key_lookup_ms"],
		RoutingMS: t["routing_ms"], StoreMS: t["store_ms"], PostCommitMS: t["post_commit_ms"]}
}

func (e authorizeExpect) timing() gwauth.Timing { return timingOfMap(e.Timing) }

func (c authorizeCase) at() time.Time { return time.UnixMicro(c.AtUS) }

func (c authorizeCase) body() []byte {
	b := c.Request.Body
	if x := c.Request.BodyExpand; x != nil {
		b = strings.Replace(b, x.Marker, strings.Repeat(x.Repeat, x.Count), 1)
	}
	return []byte(b)
}

func (c authorizeCase) header() http.Header {
	h := http.Header{}
	for _, kv := range c.Request.Headers {
		h.Add(kv[0], kv[1])
	}
	return h
}

func hasLabel(labels []string, label string) bool { return slices.Contains(labels, label) }

var loadAuthorize = sync.OnceValues(func() (authorizeFile, error) {
	var f authorizeFile
	raw, err := readTestdata("authorize_cases_v1.json")
	if err != nil {
		return f, err
	}
	err = json.Unmarshal(raw, &f)
	return f, err
})

func authorizeGoldens(t *testing.T) authorizeFile {
	t.Helper()
	f, err := loadAuthorize()
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Cases) < 280 {
		t.Fatalf("%d cases: not the goldens this test was written against", len(f.Cases))
	}
	return f
}

func authorizeCaseNamed(t *testing.T, name string) authorizeCase {
	t.Helper()
	for _, c := range authorizeGoldens(t).Cases {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no case %q", name)
	return authorizeCase{}
}

// policyOf is the header's policy with the case's patch applied.
func policyOf(t *testing.T, f authorizeFile, c authorizeCase) *routingsnapshot.EdgePolicy {
	t.Helper()
	merged := map[string]json.RawMessage{}
	for k, v := range f.Policy {
		merged[k] = v
	}
	for k, v := range c.PolicyPatch {
		merged[k] = v
	}
	raw, err := json.Marshal(merged)
	if err != nil {
		t.Fatal(err)
	}
	var p routingsnapshot.EdgePolicy
	if err := json.Unmarshal(raw, &p); err != nil {
		t.Fatal(err)
	}
	return &p
}

var parsedSnapshot = sync.OnceValues(func() (*routingsnapshot.Snapshot, error) {
	raw, err := readTestdata("routing_snapshot_v3.body.json")
	if err != nil {
		return nil, err
	}
	return routingsnapshot.Parse(raw)
})

// snapshotFor is the golden snapshot as the edge would hold it for the
// case: one compiled under the case's multi_region_enabled, whose
// region_payload says so (regions.region_payload), else the policy's
// regions would disagree with it and pass (policy.regions_mismatch).
func snapshotFor(t *testing.T, p *routingsnapshot.EdgePolicy) *routingsnapshot.Snapshot {
	t.Helper()
	s, err := parsedSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	return regionsFor(s, p)
}

// snapshotOver is snapshotFor over the file's own snapshot when it carries
// one (P6's async file), else the golden v3 body.
func snapshotOver(t *testing.T, f authorizeFile, p *routingsnapshot.EdgePolicy) *routingsnapshot.Snapshot {
	t.Helper()
	if f.SnapshotBody == "" {
		return snapshotFor(t, p)
	}
	s, err := routingsnapshot.Parse([]byte(f.SnapshotBody))
	if err != nil {
		t.Fatal(err)
	}
	return regionsFor(s, p)
}

func regionsFor(s *routingsnapshot.Snapshot, p *routingsnapshot.EdgePolicy) *routingsnapshot.Snapshot {
	if p.MultiRegionEnabled {
		return s
	}
	copied := *s
	copied.Settings.RegionPayload = slices.Clone(s.Settings.RegionPayload)
	for i := range copied.Settings.RegionPayload {
		r := &copied.Settings.RegionPayload[i]
		r.Enabled = r.Primary
	}
	return &copied
}

// fakeWorld is the store as one case's world holds it: what gwstate's cache
// reads (store.ReadKeyContext and store.ReadCreditState).
type fakeWorld struct {
	c     authorizeCase
	reads atomic.Int64
	// slow delays every read (the slow-shadow test).
	slow time.Duration
}

func (w *fakeWorld) ReadKeyContext(ctx context.Context, lookupHash string) (store.KeyContext, bool, error) {
	w.reads.Add(1)
	if err := w.wait(ctx); err != nil {
		return store.KeyContext{}, false, err
	}
	c := w.c
	if lookupHash != c.Facts.Key.LookupHash || c.Key.LookupBody == nil || c.Key.KeyBody == nil {
		return store.KeyContext{}, false, nil
	}
	var key struct {
		WorkspaceID string `json:"workspace_id"`
	}
	_ = json.Unmarshal([]byte(*c.Key.KeyBody), &key)
	kc := store.KeyContext{KeyBody: *c.Key.KeyBody, RangeWorkspaceID: key.WorkspaceID,
		BYOKIDs: slices.Clone(c.BYOKEntityIDs), BroadcastDestination: len(c.BroadcastPointerIDs) > 0}
	if c.Workspace.Body != nil {
		kc.WorkspaceBody, kc.WorkspaceFound = *c.Workspace.Body, true
	}
	return kc, true, nil
}

// requiredTier is the service's default (service.Defaults, -required-tier).
const requiredTier = 1

func (w *fakeWorld) ReadCreditState(ctx context.Context, workspace string) (store.CreditState, error) {
	w.reads.Add(1)
	if err := w.wait(ctx); err != nil {
		return store.CreditState{}, err
	}
	rows := w.c.Workspace.CreditRows
	if len(rows) == 0 || workspace != w.c.Facts.Key.WorkspaceID {
		return store.CreditState{}, store.ErrCreditRowsIncomplete
	}
	// As store.ReadCreditState and rowsRefuse read them.
	out := store.CreditState{Shards: len(rows), Tier: 3}
	for i, r := range rows {
		if r.Shard != int64(i) {
			return store.CreditState{}, store.ErrCreditRowsIncomplete
		}
		out.InDebt = out.InDebt || r.InDebt
		out.Paused = out.Paused || len(r.BillingPauseCauses) > 0
		out.Latched = out.Latched || r.TrustLatched
		tier := r.TrustTier
		if r.TrustLatched {
			tier = 0
		}
		out.Tier = min(out.Tier, tier)
	}
	switch {
	case out.InDebt:
		out.Refused = store.RefusedInDebt
	case out.Paused:
		out.Refused = store.RefusedPaused
	case out.Tier < requiredTier:
		out.Refused = store.RefusedTier
	}
	return out, nil
}

func (w *fakeWorld) wait(ctx context.Context) error {
	if w.slow <= 0 {
		return nil
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(w.slow):
		return nil
	}
}

// warmCache is a cache over the world, filled for the request's lookup hash
// at the case's instant: the request then finds what a warm edge holds.
func warmCache(t *testing.T, w *fakeWorld, lookupHash string, at time.Time) *gwstate.Cache {
	t.Helper()
	cache, err := gwstate.NewCache(gwstate.CacheConfig{Reader: w, Clock: func() time.Time { return at }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cache.Close)
	if lookupHash == "" {
		return cache
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		r := cache.Lookup(lookupHash, at)
		if r.Key != gwstate.Cold && r.Credit != gwstate.Cold {
			return cache
		}
		if time.Now().After(deadline) {
			t.Fatalf("the cache did not fill: %+v", r)
		}
		time.Sleep(time.Millisecond)
	}
}

func lookupHashOf(body []byte) string {
	var b struct {
		Lookup any `json:"api_key_lookup_hash"`
	}
	if json.Unmarshal(body, &b) != nil {
		return ""
	}
	s, _ := b.Lookup.(string)
	return strings.TrimSpace(s)
}

// bootsOf is the case's boot as the store holds it.
func bootsOf(t *testing.T, f authorizeFile, c authorizeCase) *boots {
	t.Helper()
	b := &boots{by: map[string]Boot{}}
	if c.Boot != nil {
		public, err := base64.RawURLEncoding.DecodeString(f.BootTestKey.JWK.X)
		if err != nil {
			t.Fatal(err)
		}
		b.by[c.Boot.Kid] = Boot{Kid: c.Boot.Kid, PublicKey: public, Verified: c.Boot.Verified,
			ImageDigest: c.Boot.ImageDigest, DeclaresUsageHeartbeat: c.Boot.DeclaresUsageHeartbeat}
	}
	return b
}

// bootSeenOf is E1's verdict for the case's request, from an edge over the
// case's boot.
func bootSeenOf(t *testing.T, f authorizeFile, c authorizeCase) BootSeen {
	t.Helper()
	upstream, _ := url.Parse("http://python.invalid")
	e, err := New(Config{Token: f.InternalToken, Upstream: upstream, Boots: bootsOf(t, f, c)})
	if err != nil {
		t.Fatal(err)
	}
	headers := c.header().Values(BootHeader)
	if len(headers) == 0 {
		return BootSeen{Verdict: Absent}
	}
	return e.verdictOf(context.Background(), headers, http.MethodPost, c.Request.Path, c.body())
}

// decideAll is a decide copy with every configured region on for the
// pilot workspace, read at the request's instant.
func decideAll(p *routingsnapshot.EdgePolicy, workspace string, at time.Time) DecideView {
	on := map[DecideKey]bool{}
	for _, r := range p.ConfiguredRegions {
		on[DecideKey{workspace, p.Plane.Cloud, r}] = true
	}
	return DecideView{On: on, ReadAt: at, MaxAge: decideStaleness * DecideEvery}
}

func pinnedMint(c authorizeCase) Mint {
	return Mint{
		AuthorizationID: func() string { return c.IDs.AuthorizationID },
		ReservationID:   func() string { return c.IDs.ReservationID },
		IdempotencyKey:  func() string { return c.IDs.MintedIdempotencyKey },
	}
}

// caseInputs are the pipeline's inputs for one case, its cache warm.
func caseInputs(t *testing.T, f authorizeFile, c authorizeCase) (DecideInputs, *fakeWorld) {
	t.Helper()
	p := policyOf(t, f, c)
	w := &fakeWorld{c: c}
	cache := warmCache(t, w, lookupHashOf(c.body()), c.at())
	return DecideInputs{
		Request: DecideRequest{At: c.at(), Method: http.MethodPost, Path: c.Request.Path, EscapedPath: c.Request.Path,
			Header: c.header(), Body: c.body(), Boot: bootSeenOf(t, f, c)},
		Snapshot: snapshotOver(t, f, p),
		Policy:   p,
		Lookup:   cache.Lookup,
		Decide:   decideAll(p, c.Facts.Key.WorkspaceID, c.at()),
		Mint:     pinnedMint(c),
	}, w
}

// postAdmission are the reasons that arise after the front door is asked
// (§3 "Admission and insert"): the pipeline reaches a Decision for them,
// and the decide path (G11) passes at the admit or the insert.
func postAdmission(reason string) bool {
	return strings.HasPrefix(reason, "owner.") || strings.HasPrefix(reason, "insert.") ||
		reason == "inflight.key" || reason == "budget.spent"
}

func endOfLifeOf(t *testing.T, c authorizeCase) time.Time {
	t.Helper()
	eol, err := time.Parse(time.RFC3339Nano, c.Admitted.EndOfLife)
	if err != nil || eol.UnixMicro() != c.Admitted.EndOfLifeUS {
		t.Fatalf("%s: end of life %q: %v", c.Name, c.Admitted.EndOfLife, err)
	}
	return eol
}

// TestEveryGoldenCaseThroughThePipeline: every answered case decides with
// Python's region, fingerprint, scope, material and estimate, and once
// finished its 200 and rows are Python's bytes; the unavailable case
// decides (its 503 is the insert's); every pass case passes with its §3
// reason, or, for a reason after the admit, reaches a Decision.
func TestEveryGoldenCaseThroughThePipeline(t *testing.T) {
	f := authorizeGoldens(t)
	answered, passes, admitted, evaluatorPassed := 0, 0, 0, 0
	affinity := map[string]int{}
	seen := map[string]bool{}
	for _, c := range f.Cases {
		in, _ := caseInputs(t, f, c)
		d := Decide(in)
		switch c.Expect.Outcome {
		case "answer", "unavailable":
			if why, ok := evaluatorPasses[c.Name]; ok {
				if d.Pass != PassEvaluate || d.ForShadow() != nil {
					t.Errorf("%s: %q, want the evaluator's pass (%s)", c.Name, d.Pass, why)
				}
				evaluatorPassed++
				continue
			}
			if d.Pass != "" || d.ForShadow() == nil {
				t.Errorf("%s: passed %q, want a decision", c.Name, d.Pass)
				continue
			}
			if c.Expect.Outcome == "unavailable" {
				admitted++
				continue
			}
			if !checkAnswered(t, c, d.ForShadow()) {
				continue
			}
			if c.Expect.Affinity != nil {
				if !checkAffinity(t, c, d) {
					continue
				}
				affinity[c.Expect.Affinity.Class]++
			}
			answered++
		case "pass":
			seen[c.Expect.Reason] = true
			if postAdmission(c.Expect.Reason) {
				if d.Pass != "" || d.ForShadow() == nil {
					t.Errorf("%s: passed %q before the admit; Python's reason is %s", c.Name, d.Pass, c.Expect.Reason)
					continue
				}
				admitted++
				continue
			}
			if d.Pass != c.Expect.Reason && !(c.Expect.Reason == PassEvaluateRefused && d.Pass == PassEvaluate) {
				// A refusal Go's evaluator does not reproduce it passes
				// (evaluate.pass); either way Python decides it.
				t.Errorf("%s: passed %q, want %q", c.Name, d.Pass, c.Expect.Reason)
				continue
			}
			if d.ForShadow() != nil {
				t.Errorf("%s: a decision for a request passed before the decide row", c.Name)
				continue
			}
			passes++
		default:
			t.Fatalf("%s: outcome %q", c.Name, c.Expect.Outcome)
		}
	}
	t.Logf("%d answered cases Python's bytes, %d pass cases with their reason, %d reach the admit, %d answered by "+
		"Python that the evaluator passes", answered, passes, admitted, evaluatorPassed)
	if evaluatorPassed != len(evaluatorPasses) {
		t.Errorf("%d of the %d evaluator passes seen", evaluatorPassed, len(evaluatorPasses))
	}
	if answered < 160 || passes < 90 || admitted < 10 {
		t.Fatalf("answered %d, passes %d, admitted %d: fewer than the goldens hold", answered, passes, admitted)
	}
	if affinity[goldenAffinityCold] < 6 || affinity[goldenAffinityWarm] < 10 {
		t.Fatalf("affinity %v: fewer than the goldens hold", affinity)
	}
}

// checkAffinity holds an answered decision whose body names a
// cache_affinity_key (§1 item 5) to the case's record of Python's answer
// with its sessions remembered: the decision is Go's cold order (Python's
// answer with no session, which checkAnswered holds byte for byte), and
// the shadow compares Python's warm bytes as any cohort answer, rows read
// back: agree_full when the session changed nothing (affinity_cold), and
// otherwise a differ whose one field is differ.affinity_warm, its rows
// read against Go's decision in Python's order (affinity_warm).
func checkAffinity(t *testing.T, c authorizeCase, d Decided) bool {
	t.Helper()
	a, dec := c.Expect.Affinity, d.ForShadow()
	if !dec.Affinity || !slices.Equal(dec.Candidates, a.ColdCandidates) || dec.Estimate != a.ColdEstimate {
		t.Errorf("%s: affinity %v, candidates %v at %d, want the cold %v at %d", c.Name, dec.Affinity, dec.Candidates,
			dec.Estimate, a.ColdCandidates, a.ColdEstimate)
		return false
	}
	r := compareFull(d, pyAnswer{status: 200, body: []byte(a.Response)})
	if !r.decided || !r.rows {
		t.Errorf("%s: Python's warm bytes compare as %+v, not as the cohort's with rows read", c.Name, r)
		return false
	}
	switch a.Class {
	case goldenAffinityCold:
		if r.class != ClassAgreeFull || len(r.fields) != 0 || r.warm != nil || a.Response != c.Expect.Response {
			t.Errorf("%s: Python's bytes with sessions compare as %+v, want agree_full", c.Name, r)
			return false
		}
	case goldenAffinityWarm:
		if r.class != ClassDifferAny || !slices.Equal(r.fields, []string{FieldAffinityWarm}) || r.warm == nil ||
			!slices.Equal(r.warm.Candidates, a.Candidates) || r.warm.Estimate != a.Estimate ||
			slices.Equal(a.Candidates, a.ColdCandidates) {
			t.Errorf("%s: Python's warm bytes compare as %+v, want differ with %s alone", c.Name, r, FieldAffinityWarm)
			return false
		}
	default:
		t.Errorf("%s: class %q", c.Name, a.Class)
		return false
	}
	return true
}

// evaluatorPasses are the answered goldens whose provider preferences the
// routing snapshot evaluator does not reproduce (docs/design/
// routing-snapshot-v1.md, "What Go evaluates"): Go passes them, which is
// always safe; the shadow counts them python_answered_go_passed.evaluate.pass.
var evaluatorPasses = map[string]string{
	"answer:provider:max_price": "provider.max_price",
}

// checkAnswered holds a decision to an answered case: what the pipeline
// derived (region, fingerprint, scope, material, estimate), the 200 once
// finished with the case's envelope and timing, and both rows.
func checkAnswered(t *testing.T, c authorizeCase, d *Decision) bool {
	t.Helper()
	e := c.Expect
	ok := true
	check := func(what string, got, want any) {
		if got != want {
			t.Errorf("%s: %s %v, want %v", c.Name, what, got, want)
			ok = false
		}
	}
	check("region", d.Region, e.DecideRegion)
	check("fingerprint", d.Identity.Fingerprint, e.Fingerprint)
	check("scope", d.Identity.Scope, e.IdempotencyScope)
	check("material", string(d.Identity.Material), e.FingerprintMaterial)
	check("estimate", d.Estimate, e.Estimate)
	check("reservation", d.Reservation, e.Reservation)
	check("workspace", d.Workspace, c.Facts.Key.WorkspaceID)
	if !ok {
		return false
	}
	composed, err := d.Finish(c.Admitted.Envelope, endOfLifeOf(t, c))
	if err != nil {
		t.Errorf("%s: Finish: %v", c.Name, err)
		return false
	}
	if got := composed.Render(e.timing(), c.Insert == "unknown_landed"); string(got) != e.Response {
		t.Errorf("%s: the 200 differs\n got %s\nwant %s", c.Name, got, e.Response)
		return false
	}
	return rowsAreGoldens(t, c, composed)
}

// rowsAreGoldens: every column of both rows as the case records Python's.
func rowsAreGoldens(t *testing.T, c authorizeCase, composed gwauth.Composed) bool {
	t.Helper()
	a, r := composed.Authorization, composed.Reservation
	var nonce any
	if a.InvocationNonce != nil {
		nonce = *a.InvocationNonce
	}
	num := func(n int64) any { return float64(n) }
	gotA := map[string]any{
		"authorization_id": a.AuthorizationID, "workspace_id": a.WorkspaceID, "key_hash": a.KeyHash,
		"reservation_id": a.ReservationID, "model_id": a.ModelID, "provider": a.Provider, "usage_type": a.UsageType,
		"estimated_microdollars": num(a.EstimatedMicrodollars), "settled": false,
		"created_at_us": num(a.CreatedAt.UnixMicro()), "payload": a.Payload,
		"idempotency_fingerprint": a.IdempotencyFingerprint, "stage_d_boot_kid": a.StageDBootKid,
		"invocation_nonce": nonce,
	}
	gotR := map[string]any{
		"reservation_id": r.ReservationID, "workspace_id": r.WorkspaceID, "key_hash": r.KeyHash,
		"ws_shard": num(0), "credit_shard": num(0), "key_shard": num(0), "credit_reserved_micro": num(0),
		"key_reserved_micro": num(0), "hold_usage_type": r.HoldUsageType, "authorization_id": r.AuthorizationID,
		"idempotency_scope": r.IdempotencyScope, "idempotency_fingerprint": r.IdempotencyFingerprint,
		"expires_at_us": num(r.ExpiresAt.UnixMicro()),
		// The commit's timestamp, as the store writes it (§5).
		"created_at": "commit_timestamp",
	}
	ok := true
	// Every column of the table (store.StoredAuthorization's 25, the
	// timestamps as _us): a typed column is caught only by row parity.
	if len(c.Expect.AuthorizationRow) != 25 {
		t.Errorf("%s: the authorization row has %d columns, the table 25", c.Name, len(c.Expect.AuthorizationRow))
		ok = false
	}
	for column := range gotA {
		if _, recorded := c.Expect.AuthorizationRow[column]; !recorded {
			t.Errorf("%s: authorization column %s is not in Python's row", c.Name, column)
			ok = false
		}
	}
	for column, want := range c.Expect.AuthorizationRow {
		if got := gotA[column]; got != want {
			t.Errorf("%s: authorization %s = %v, want %v", c.Name, column, got, want)
			ok = false
		}
	}
	if len(c.Expect.ReservationRow) != len(gotR) {
		t.Errorf("%s: reservation has %d columns, Go %d", c.Name, len(c.Expect.ReservationRow), len(gotR))
		ok = false
	}
	for column, want := range c.Expect.ReservationRow {
		if got := gotR[column]; got != want {
			t.Errorf("%s: reservation %s = %v, want %v", c.Name, column, got, want)
			ok = false
		}
	}
	return ok
}

// TestEveryGoldenReasonIsDecideOrAfterIt: the vocabulary the goldens use
// is the pipeline's, so a reason the pipeline cannot name is not silently
// waved through.
func TestEveryGoldenReasonIsNamed(t *testing.T) {
	named := map[string]bool{}
	for _, r := range []string{PassBootSignature, PassBootUnverified, PassBootMissing, PassKeyMissing, PassKeyDisabled,
		PassKeyExpired, PassKeyScope, PassKeyFederated, PassKeyApp, PassKeyCap, PassKeyWindow, PassKeyWindowAlert,
		PassKeyBudgetStrict, PassWorkspaceMissing, PassWorkspacePaused, PassWorkspaceInDebt, PassWorkspaceTier, PassWorkspaceFederated,
		PassWorkspaceDeleted, PassWorkspaceBroadcast, PassPilotOff, PassNotPilotWorkspace, PassPilotRegion,
		PassRecordsNotDurable, PassAsyncSettleShadow, PassMonitorKey, PassDigestNotAccepted, PassEvaluateRefused,
		PassEstimateOverflow, PassCandidatesBYOK, PassCandidatesEstimated, PassCandidatesPrivate,
		gwauth.ReasonTagsMergedInvalid} {
		named[r] = true
	}
	for _, c := range authorizeGoldens(t).Cases {
		r := c.Expect.Reason
		if c.Expect.Outcome == "pass" && !named[r] && !postAdmission(r) && !strings.HasPrefix(r, "body.") &&
			!strings.HasPrefix(r, "header.") {
			t.Errorf("%s: reason %q is not one the pipeline names", c.Name, r)
		}
	}
}

// TestGosSetsAreTheGoldensHeader: decision 7's estimated-usage models and
// decision 19's internal models, as P1's header records Python's.
func TestGosSetsAreTheGoldensHeader(t *testing.T) {
	f := authorizeGoldens(t)
	if !slices.Equal(EstimatedUsageModels, f.EstimatedUsageModels) {
		t.Errorf("estimated usage models %q, Python's %q", EstimatedUsageModels, f.EstimatedUsageModels)
	}
	if !slices.Equal(gwauth.InternalModels, f.InternalModels) {
		t.Errorf("internal models %q, Python's %q", gwauth.InternalModels, f.InternalModels)
	}
}

// baseInputs are the inputs of a plain answered case, for the tests that
// change one fact of it.
func baseInputs(t *testing.T) (DecideInputs, authorizeCase) {
	t.Helper()
	f := authorizeGoldens(t)
	c := authorizeCaseNamed(t, "answer:app_categories_empty")
	in, _ := caseInputs(t, f, c)
	if d := Decide(in); d.Pass != "" || d.ForShadow() == nil {
		t.Fatalf("the base case passes: %q", d.Pass)
	}
	return in, c
}

// withRead is in with the cache answering read, as changed by change, for
// every lookup.
func withRead(t *testing.T, in DecideInputs, change func(*gwstate.Read)) DecideInputs {
	t.Helper()
	base := in.Lookup(lookupHashOf(in.Request.Body), in.Request.At)
	change(&base)
	in.Lookup = func(string, time.Time) gwstate.Read { return base }
	return in
}

// TestTheGateReadsTheCachesWord (G7's notes): a stale credit entry, or one
// whose rows are incomplete, passes even with every fact eligible; a credit
// row's pause, latch or debt passes though the entity is clean; and Usable
// is the last word. Each change's positive control is the base case, which
// decides.
func TestTheGateReadsTheCachesWord(t *testing.T) {
	in, _ := baseInputs(t)
	for _, v := range []struct {
		name   string
		change func(*gwstate.Read)
		want   string
	}{
		{"credit stale", func(r *gwstate.Read) { r.Credit = gwstate.Stale }, PassCacheWorkspace},
		{"credit cold", func(r *gwstate.Read) { r.Credit = gwstate.Cold }, PassCacheWorkspace},
		{"credit unneeded", func(r *gwstate.Read) { r.Credit = gwstate.Unneeded }, PassCacheWorkspace},
		{"key stale", func(r *gwstate.Read) { r.Key = gwstate.Stale }, PassCacheKey},
		{"rows incomplete", func(r *gwstate.Read) { r.CreditRows = false }, PassWorkspaceCreditRows},
		{"row paused", func(r *gwstate.Read) { r.CreditState.Paused = true }, PassWorkspacePaused},
		{"row refuses paused", func(r *gwstate.Read) { r.CreditState.Refused = store.RefusedPaused }, PassWorkspacePaused},
		{"row in debt", func(r *gwstate.Read) { r.CreditState.InDebt = true }, PassWorkspaceInDebt},
		{"row latched", func(r *gwstate.Read) { r.CreditState.Latched = true }, PassWorkspaceTier},
		{"row below the tier", func(r *gwstate.Read) { r.CreditState.Refused = store.RefusedTier }, PassWorkspaceTier},
		{"row refuses otherwise", func(r *gwstate.Read) { r.CreditState.Refused = store.RefusedAllowance },
			PassWorkspaceRefused},
		{"entity paused", func(r *gwstate.Read) {
			r.Facts.Workspace.Eligible, r.Facts.Workspace.Reason = false, gwstate.ReasonWorkspacePaused
		}, PassWorkspacePaused},
		{"broadcast", func(r *gwstate.Read) { r.Facts.BroadcastDestination = true }, PassWorkspaceBroadcast},
		{"not found", func(r *gwstate.Read) { r.Found = false }, PassKeyMissing},
	} {
		if got := Decide(withRead(t, in, v.change)); got.Pass != v.want || got.ForShadow() != nil {
			t.Errorf("%s: passed %q (decision %v), want %q", v.name, got.Pass, got.ForShadow() != nil, v.want)
		}
	}
	if got := Decide(withRead(t, in, func(*gwstate.Read) {})); got.Pass != "" {
		t.Fatalf("positive control: the unchanged read passes %q", got.Pass)
	}
}

// TestTheKeysExpiryIsAtTheRequestsInstant: KeyFacts.At, the request's one
// clock reading, not the read's: a key fresh in the cache that has expired
// since passes; one that expires later decides.
func TestTheKeysExpiryIsAtTheRequestsInstant(t *testing.T) {
	in, _ := baseInputs(t)
	at := in.Request.At
	for _, v := range []struct {
		expires time.Time
		want    string
	}{
		{at, PassKeyExpired},
		{at.Add(-time.Microsecond), PassKeyExpired},
		{at.Add(time.Second), ""},
	} {
		got := Decide(withRead(t, in, func(r *gwstate.Read) { r.Facts.Key.ExpiresAt = v.expires }))
		if got.Pass != v.want {
			t.Errorf("expires %v at %v: passed %q, want %q", v.expires, at, got.Pass, v.want)
		}
	}
}

// keyBodyWith is the base case's stored key with fields replaced.
func keyBodyWith(t *testing.T, c authorizeCase, fields map[string]any) string {
	t.Helper()
	var key map[string]any
	if err := json.Unmarshal([]byte(*c.Key.KeyBody), &key); err != nil {
		t.Fatal(err)
	}
	for k, v := range fields {
		key[k] = v
	}
	out, err := json.Marshal(key)
	if err != nil {
		t.Fatal(err)
	}
	return string(out)
}

// TestTheRouteWalkerGapIsGated (G3's #2001 note): authorize_gateway_typed's
// checks before the leased branch (the window check, the shard count's
// re-validation, the lifetime cap's precheck) and its later arguments
// (app_id, budget_alert_only) are each a key the cohort excludes, read from
// the key's stored body through the real cache.
func TestTheRouteWalkerGapIsGated(t *testing.T) {
	f := authorizeGoldens(t)
	base := authorizeCaseNamed(t, "answer:app_categories_empty")
	for _, v := range []struct {
		name   string
		fields map[string]any
		want   string
	}{
		{"daily window", map[string]any{"limit_daily_microdollars": 5}, PassKeyWindow},
		{"weekly window", map[string]any{"limit_weekly_microdollars": 5}, PassKeyWindow},
		{"monthly window alert only", map[string]any{"limit_monthly_microdollars": 5, "budget_alert_only": true},
			PassKeyWindowAlert},
		{"daily window alert only false", map[string]any{"limit_daily_microdollars": 5, "budget_alert_only": false},
			PassKeyWindow},
		{"alert only and a cap", map[string]any{"limit_microdollars": 5, "limit_daily_microdollars": 5,
			"budget_alert_only": true}, PassKeyCap},
		{"alert only and strict", map[string]any{"limit_weekly_microdollars": 5, "budget_alert_only": true,
			"budget_strict": true}, PassKeyWindowAlert},
		{"window with alert only not a bool", map[string]any{"limit_daily_microdollars": 5,
			"budget_alert_only": "false"}, PassKeyShape},
		{"lifetime cap", map[string]any{"limit_microdollars": 5}, PassKeyCap},
		{"shard count zero", map[string]any{"usage_shard_count": 0}, PassKeyShape},
		{"shard count past 64", map[string]any{"usage_shard_count": 65}, PassKeyShape},
		{"shard count not an integer", map[string]any{"usage_shard_count": "2"}, PassKeyShape},
		{"app", map[string]any{"app_id": "app_x"}, PassKeyApp},
		{"budget strict", map[string]any{"budget_strict": true}, PassKeyBudgetStrict},
		// Positive controls: what the cohort's keys carry decides.
		{"alert only alone", map[string]any{"budget_alert_only": true}, ""},
		{"alert only not a bool, no window", map[string]any{"budget_alert_only": "yes"}, ""},
		{"two shards", map[string]any{"usage_shard_count": 2}, ""},
	} {
		c := base
		body := keyBodyWith(t, c, v.fields)
		c.Key.KeyBody = &body
		in, _ := caseInputs(t, f, c)
		if got := Decide(in); got.Pass != v.want {
			t.Errorf("%s: passed %q, want %q", v.name, got.Pass, v.want)
		}
	}
}

// TestAnAbsentOrStalePolicyPassesEverything: no policy, one past its
// not_after, or one issued more than a second ahead of the request.
func TestAnAbsentOrStalePolicyPassesEverything(t *testing.T) {
	in, _ := baseInputs(t)
	at := in.Request.At.Unix()
	for _, v := range []struct {
		name   string
		policy func(*routingsnapshot.EdgePolicy) *routingsnapshot.EdgePolicy
		want   string
	}{
		{"absent", func(*routingsnapshot.EdgePolicy) *routingsnapshot.EdgePolicy { return nil }, PassCachePolicy},
		{"past not_after", func(p *routingsnapshot.EdgePolicy) *routingsnapshot.EdgePolicy {
			p.NotAfter = at
			return p
		}, PassCachePolicy},
		{"issued ahead", func(p *routingsnapshot.EdgePolicy) *routingsnapshot.EdgePolicy {
			p.IssuedAt = at + 2
			return p
		}, PassCachePolicy},
		{"issued a second ahead", func(p *routingsnapshot.EdgePolicy) *routingsnapshot.EdgePolicy {
			p.IssuedAt = at + 1
			return p
		}, ""},
	} {
		changed := *in.Policy
		in := in
		in.Policy = v.policy(&changed)
		if got := Decide(in); got.Pass != v.want {
			t.Errorf("%s: passed %q, want %q", v.name, got.Pass, v.want)
		}
	}
	in.Snapshot = nil
	if got := Decide(in); got.Pass != PassCacheSnapshot {
		t.Errorf("no snapshot: passed %q", got.Pass)
	}
}

// TestThePilotGates: decision 11 (Python's pilot set, region and records)
// and §4's Stage D pilot list, whose empty list is everyone where the
// fast-path pilot's empty list is no one.
func TestThePilotGates(t *testing.T) {
	in, c := baseInputs(t)
	ws := c.Facts.Key.WorkspaceID
	for _, v := range []struct {
		name   string
		change func(*routingsnapshot.EdgePolicy)
		want   string
		stageD string
	}{
		{"pilot workspaces empty", func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotWorkspaces = nil },
			PassNotPilotWorkspace, ""},
		{"pilot regions empty", func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotRegions = nil }, PassPilotRegion, ""},
		{"records not durable", func(p *routingsnapshot.EdgePolicy) { p.RecordsDurable = false }, PassRecordsNotDurable, ""},
		{"stage d pilot empty is everyone", func(p *routingsnapshot.EdgePolicy) { p.StageD.PilotWorkspaces = nil }, "",
			"not_streaming"},
		{"stage d pilot names another", func(p *routingsnapshot.EdgePolicy) {
			p.StageD.PilotWorkspaces = []string{"ws_other"}
		}, "", "workspace_not_pilot"},
		{"stage d pilot names it", func(p *routingsnapshot.EdgePolicy) { p.StageD.PilotWorkspaces = []string{ws} }, "",
			"not_streaming"},
		{"monitor key", func(p *routingsnapshot.EdgePolicy) {
			h := lookupHashOf(in.Request.Body)
			p.MonitorLookupHash = &h
		}, PassMonitorKey, ""},
		{"an internal model the policy names", func(p *routingsnapshot.EdgePolicy) {
			p.InternalModels = append(slices.Clone(p.InternalModels), "z-ai/glm-5.3")
		}, gwauth.ReasonInternalModel, ""},
		{"regions disagree", func(p *routingsnapshot.EdgePolicy) { p.MultiRegionEnabled = false }, PassRegionsMismatch, ""},
		{"primary disagrees", func(p *routingsnapshot.EdgePolicy) { p.PrimaryRegion = "us-east4" }, PassRegionsMismatch, ""},
	} {
		changed := *in.Policy
		v.change(&changed)
		in := in
		in.Policy = &changed
		got := Decide(in)
		if got.Pass != v.want {
			t.Errorf("%s: passed %q, want %q", v.name, got.Pass, v.want)
			continue
		}
		if v.stageD != "" && got.ForShadow().Input.StageDReason != v.stageD {
			t.Errorf("%s: stage d %q, want %q", v.name, got.ForShadow().Input.StageDReason, v.stageD)
		}
	}
}

// TestTheDecideRowIsLastAndTheShadowStillDecides: a workspace with no row,
// or a copy past three intervals, passes; the Decision is made all the
// same, for the shadow to compare.
func TestTheDecideRowIsLastAndTheShadowStillDecides(t *testing.T) {
	in, c := baseInputs(t)
	at := in.Request.At
	for _, v := range []struct {
		name string
		view DecideView
		want string
	}{
		{"never read", DecideView{}, PassCacheDecide},
		{"past three intervals", DecideView{On: in.Decide.On, ReadAt: at.Add(-16 * time.Second), MaxAge: 15 * time.Second},
			PassCacheDecide},
		{"no row", DecideView{On: map[DecideKey]bool{}, ReadAt: at, MaxAge: 15 * time.Second}, PassDecideOff},
		{"another region's row", DecideView{On: map[DecideKey]bool{{c.Facts.Key.WorkspaceID, "gcp", "us-east4"}: true},
			ReadAt: at, MaxAge: 15 * time.Second}, PassDecideOff},
		{"its row", DecideView{On: map[DecideKey]bool{{c.Facts.Key.WorkspaceID, "gcp", "us-central1"}: true},
			ReadAt: at.Add(-15 * time.Second), MaxAge: 15 * time.Second}, ""},
	} {
		in := in
		in.Decide = v.view
		got := Decide(in)
		if got.Pass != v.want || got.DecideGate != v.want || got.ForShadow() == nil {
			t.Errorf("%s: pass %q gate %q decision %v, want %q", v.name, got.Pass, got.DecideGate, got.ForShadow() != nil, v.want)
		}
	}
}

// TestTheBootReasons: an Invalid verdict told apart by the boot it read,
// and the verdicts that read no boot.
func TestTheBootReasons(t *testing.T) {
	for _, v := range []struct {
		seen BootSeen
		want string
	}{
		{BootSeen{Verdict: Absent}, PassBootSignature},
		{BootSeen{Verdict: Unread, Kid: "k"}, PassBootUnread},
		{BootSeen{Verdict: Unchecked}, gwauth.ReasonUnparseable},
		{BootSeen{Verdict: Invalid}, PassBootSignature},
		{BootSeen{Verdict: Invalid, Kid: "k"}, PassBootMissing},
		{BootSeen{Verdict: Invalid, Kid: "k", Found: true}, PassBootUnverified},
		{BootSeen{Verdict: Invalid, Kid: "k", Found: true, Boot: Boot{Verified: true}}, PassBootSignature},
		{BootSeen{Verdict: Verified, Kid: "k", Found: true, Boot: Boot{Verified: true}}, ""},
	} {
		if got := bootPass(v.seen); got != v.want {
			t.Errorf("%+v: %q, want %q", v.seen, got, v.want)
		}
	}
}

// TestTheFingerprintCorpusThroughThePipeline (§10.2): every body P1 sent
// on the cohort path, through the whole pipeline over the golden snapshot
// and a key of the corpus's workspace. A body Go decides has Python's
// fingerprint and scope and carries no label that requires a pass; a body
// Python refused is never decided.
func TestTheFingerprintCorpusThroughThePipeline(t *testing.T) {
	raw := testdata(t, "authorize_fingerprint_v1.json")
	var file struct {
		Policy json.RawMessage `json:"policy"`
		Cases  []struct {
			Name        string      `json:"name"`
			Labels      []string    `json:"labels"`
			WorkspaceID string      `json:"workspace_id"`
			KeyHash     string      `json:"key_hash"`
			Headers     [][2]string `json:"headers"`
			Body        string      `json:"body"`
			Expect      struct {
				Outcome          string  `json:"outcome"`
				Fingerprint      string  `json:"fingerprint"`
				IdempotencyKey   *string `json:"idempotency_key"`
				IdempotencyScope *string `json:"idempotency_scope"`
			} `json:"expect"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	var p routingsnapshot.EdgePolicy
	if err := json.Unmarshal(file.Policy, &p); err != nil {
		t.Fatal(err)
	}
	snap := snapshotFor(t, &p)
	f := authorizeGoldens(t)
	public, _ := base64.RawURLEncoding.DecodeString(f.BootTestKey.JWK.X)
	boot := BootSeen{Verdict: Verified, Kid: f.BootTestKey.Kid, Found: true, Boot: Boot{Kid: f.BootTestKey.Kid,
		PublicKey: public, Verified: true, ImageDigest: p.AcceptedImageDigests[0], DeclaresUsageHeartbeat: true}}
	at := time.Unix(p.IssuedAt+10, 0)
	passRequired := map[string]bool{"float_present": true, "lax_coercion": true, "duplicate_key": true,
		"strip_ambiguous": true, "dict_key_strip": true, "non_ascii_tag": true, "referer_not_strict": true,
		"canonical_model_rewrite": true, "native_batch_key": true, "partner_key": true,
		"idempotency_key_too_long": true, "idempotency_header_repeated": true, "idempotency_header_non_ascii": true,
		"content_type": true, "deep_nesting": true, "int_over_4000_digits": true, "bom": true, "lone_surrogate": true,
		"estimate_overflow": true}
	decided, passedReasons := 0, map[string]int{}
	for _, c := range file.Cases {
		lookup := func(hash string, now time.Time) gwstate.Read {
			// FINGERPRINT_KEY (authorize_vectors.py) stores the tag
			// team=e2, which Python merged into every body's tags.
			key := `{"hash":` + strconv.Quote(c.KeyHash) + `,"salt":"s","secret_hash":"h","lookup_hash":` +
				strconv.Quote(hash) + `,"name":"n","label":"l","workspace_id":` + strconv.Quote(c.WorkspaceID) +
				`,"creator_user_id":"u","disabled":false,"scopes":[],"federated_home":"","app_id":"",` +
				`"budget_strict":false,"tags":{"team":"e2"}}`
			facts := gwstate.FromContext(store.KeyContext{KeyBody: key, RangeWorkspaceID: c.WorkspaceID,
				WorkspaceBody:  `{"id":` + strconv.Quote(c.WorkspaceID) + `,"name":"n","owner_user_id":"u"}`,
				WorkspaceFound: true})
			return gwstate.Read{Key: gwstate.Fresh, Found: true, Facts: facts, KeyReadAt: now, Credit: gwstate.Fresh,
				CreditRows: true, CreditState: store.CreditState{Shards: 1, Tier: 3}, CreditReadAt: now}
		}
		h := http.Header{}
		for _, kv := range c.Headers {
			h.Add(kv[0], kv[1])
		}
		d := Decide(DecideInputs{
			Request: DecideRequest{At: at, Method: http.MethodPost, Path: authorizePath, EscapedPath: authorizePath,
				Header: h, Body: []byte(c.Body), Boot: boot},
			Snapshot: snap, Policy: &p, Lookup: lookup, Decide: decideAll(&p, c.WorkspaceID, at),
		})
		if d.ForShadow() == nil {
			passedReasons[d.Pass]++
			continue
		}
		decided++
		if c.Expect.Outcome != "fingerprint" {
			t.Errorf("%s: decided a body Python refused", c.Name)
			continue
		}
		for _, l := range c.Labels {
			if passRequired[l] {
				t.Errorf("%s: decided a body labelled %s", c.Name, l)
			}
		}
		if d.ForShadow().Identity.Fingerprint != c.Expect.Fingerprint {
			t.Errorf("%s: fingerprint %s, Python's %s", c.Name, d.ForShadow().Identity.Fingerprint, c.Expect.Fingerprint)
		}
		if c.Expect.IdempotencyScope != nil && d.ForShadow().Identity.Scope != *c.Expect.IdempotencyScope {
			t.Errorf("%s: scope %s, Python's %s", c.Name, d.ForShadow().Identity.Scope, *c.Expect.IdempotencyScope)
		}
		if c.Expect.IdempotencyKey == nil && d.ForShadow().Identity.Presented {
			t.Errorf("%s: a key presented where Python minted one", c.Name)
		}
	}
	t.Logf("%d of %d bodies decided; passes by reason %v", decided, len(file.Cases), passedReasons)
	if decided < 1000 {
		t.Fatalf("only %d bodies decided: the pipeline is not the cohort's", decided)
	}
}

// TestTheDecideCopyAgesOut: the refresher reads at once and every
// interval; a copy whose reads fail is stale (every request cache.decide)
// once three intervals have passed since the last good read.
func TestTheDecideCopyAgesOut(t *testing.T) {
	var mu sync.Mutex
	now := time.Date(2026, 10, 19, 2, 0, 0, 0, time.UTC)
	clock := func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		return now
	}
	r := &fakeDecides{rows: []store.DecideRow{{Workspace: "ws_a", Cloud: "gcp", Region: "us-central1", Decide: true},
		{Workspace: "ws_b", Cloud: "gcp", Region: "us-central1", Decide: false}}}
	d := newDecideCopy(r, 10*time.Millisecond, clock, func(string, ...any) {})
	if got := d.view().gate("ws_a", "gcp", "us-central1", now); got != PassCacheDecide {
		t.Fatalf("before the first read: %q", got)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		d.run(ctx)
	}()
	defer func() {
		cancel()
		<-done
	}()
	waitFor(t, func() bool { return d.reads.Load() >= 1 && d.current.Load() != nil })
	v := d.view()
	if v.gate("ws_a", "gcp", "us-central1", now) != "" || v.gate("ws_b", "gcp", "us-central1", now) != PassDecideOff ||
		v.gate("ws_a", "gcp", "us-east4", now) != PassDecideOff {
		t.Fatalf("the copy: %+v", v)
	}
	r.fail.Store(true)
	before := d.reads.Load()
	waitFor(t, func() bool { return d.failed.Load() >= 1 && d.reads.Load() > before })
	v = d.view()
	if got := v.gate("ws_a", "gcp", "us-central1", now.Add(30*time.Millisecond)); got != "" {
		t.Fatalf("within three intervals of the last good read: %q", got)
	}
	if got := v.gate("ws_a", "gcp", "us-central1", now.Add(31*time.Millisecond)); got != PassCacheDecide {
		t.Fatalf("past three intervals: %q", got)
	}
}

type fakeDecides struct {
	rows  []store.DecideRow
	fail  atomic.Bool
	reads atomic.Int64
}

func (f *fakeDecides) ReadDecide(ctx context.Context) ([]store.DecideRow, time.Time, error) {
	f.reads.Add(1)
	if f.fail.Load() {
		return nil, time.Time{}, errors.New("spanner away")
	}
	return slices.Clone(f.rows), time.Time{}, nil
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(time.Millisecond)
	}
}

func readTestdata(name string) ([]byte, error) {
	return os.ReadFile(filepath.Join("..", "..", "testdata", name))
}

// TestOnlyACompleteDecisionIsAdmittable (G11's footgun): a decision the
// decide row stops (decide.off, cache.decide) is the shadow's only; Admit
// hands one out only when nothing passed.
func TestOnlyACompleteDecisionIsAdmittable(t *testing.T) {
	in, _ := baseInputs(t)
	if d, ok := Decide(in).Admit(); !ok || d == nil {
		t.Fatal("positive control: the base case is not admittable")
	}
	for _, view := range []DecideView{{}, {On: map[DecideKey]bool{}, ReadAt: in.Request.At, MaxAge: 15 * time.Second}} {
		in := in
		in.Decide = view
		got := Decide(in)
		if got.Pass == "" || got.ForShadow() == nil {
			t.Fatalf("%+v: pass %q, shadow %v", view, got.Pass, got.ForShadow() != nil)
		}
		if d, ok := got.Admit(); ok || d != nil {
			t.Fatalf("%+v: admittable with %q", view, got.Pass)
		}
	}
	for _, reason := range []string{PassDecideOff, PassCacheDecide, PassKeyMissing} {
		if d, ok := (Decided{Pass: reason, shadowOnly: &Decision{}}).Admit(); ok || d != nil {
			t.Fatalf("%s: admittable", reason)
		}
	}
}

// TestOnlyAnAuthorizePostIsDecided: Decide passes anything but a POST to
// exactly an authorize path as E1 and Python both read it; a path the
// client escaped (Python's request.url.path and E1's r.URL.Path may read
// it apart) passes, as does a request RequestOf did not build.
func TestOnlyAnAuthorizePostIsDecided(t *testing.T) {
	in, _ := baseInputs(t)
	for _, v := range []struct{ method, path, escaped, want string }{
		{http.MethodPost, "/internal/gateway/authorize", "/internal/gateway/authorize", ""},
		{http.MethodPost, "/v1/internal/gateway/authorize", "/v1/internal/gateway/authorize", ""},
		{http.MethodGet, "/internal/gateway/authorize", "/internal/gateway/authorize", PassRequestPath},
		{http.MethodPost, "/internal/gateway/heartbeat", "/internal/gateway/heartbeat", PassRequestPath},
		{http.MethodPost, "/internal/gateway/authorize", "/internal/gateway/%61uthorize", PassRequestPath},
		{"", "", "", PassRequestPath},
	} {
		in := in
		in.Request.Method, in.Request.Path, in.Request.EscapedPath = v.method, v.path, v.escaped
		if got := Decide(in); got.Pass != v.want {
			t.Errorf("%s %s (%s): %q, want %q", v.method, v.path, v.escaped, got.Pass, v.want)
		}
	}
	for _, target := range []string{"/internal/gateway/authorize", "/internal/gateway/%61uthorize",
		"/internal/gateway/authorize%3Fx", "/internal/gateway/authorize?x=1"} {
		r := httptest.NewRequest(http.MethodPost, target, nil)
		q := RequestOf(r, in.Request.At, in.Request.Body, in.Request.Boot)
		q.Header = in.Request.Header
		in := in
		in.Request = q
		want := PassRequestPath
		if target == "/internal/gateway/authorize" || target == "/internal/gateway/authorize?x=1" {
			want = ""
		}
		if got := Decide(in); got.Pass != want {
			t.Errorf("%s: %q, want %q", target, got.Pass, want)
		}
	}
}

// TestTheKeylessPolicyGatesAreBeforeTheLookup: with the pilot off, no
// pilot workspace or not this region, the cache is never read.
func TestTheKeylessPolicyGatesAreBeforeTheLookup(t *testing.T) {
	in, _ := baseInputs(t)
	looked := 0
	lookup := in.Lookup
	in.Lookup = func(h string, now time.Time) gwstate.Read {
		looked++
		return lookup(h, now)
	}
	for _, v := range []struct {
		change func(*routingsnapshot.EdgePolicy)
		want   string
	}{
		{func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotEnabled = false }, PassPilotOff},
		{func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotWorkspaces = nil }, PassNotPilotWorkspace},
		{func(p *routingsnapshot.EdgePolicy) { p.FastpathPilotRegions = []string{"europe-west4"} }, PassPilotRegion},
	} {
		changed := *in.Policy
		v.change(&changed)
		in := in
		in.Policy = &changed
		if got := Decide(in); got.Pass != v.want {
			t.Errorf("passed %q, want %q", got.Pass, v.want)
		}
	}
	if looked != 0 {
		t.Fatalf("%d lookups behind a keyless gate", looked)
	}
	if Decide(in); looked != 1 {
		t.Fatalf("positive control: %d lookups", looked)
	}
}

// TestTheReservationPricesAtMostHoldOutputTokens: through the pipeline, the
// production shape (a max_tokens of 131,072) and the budgets either side of
// routingsnapshot.HoldOutputTokens decide what the regenerated goldens
// record: a reservation (the lease's hold) of 16,384 output tokens at and
// above the bound and of the budget itself below it, while the estimate
// (the rows' and the 200's ceiling) prices the whole budget.
func TestTheReservationPricesAtMostHoldOutputTokens(t *testing.T) {
	f := authorizeGoldens(t)
	figures := func(name string) (estimate, reservation int64) {
		t.Helper()
		for _, c := range f.Cases {
			if c.Name != name {
				continue
			}
			in, _ := caseInputs(t, f, c)
			d := Decide(in)
			if d.Pass != "" || d.ForShadow() == nil {
				t.Fatalf("%s: passed %q, want a decision", name, d.Pass)
			}
			got := d.ForShadow()
			if got.Estimate != c.Expect.Estimate || got.Reservation != c.Expect.Reservation {
				t.Fatalf("%s: estimate %d and reservation %d, the golden's %d and %d",
					name, got.Estimate, got.Reservation, c.Expect.Estimate, c.Expect.Reservation)
			}
			return got.Estimate, got.Reservation
		}
		t.Fatalf("%s: not a golden case", name)
		return 0, 0
	}
	flash := "answer:estimate:hold_cap:deepseek/deepseek-v4-flash:1000+"
	atBound, bound := figures(flash + "16384")
	if atBound != bound {
		t.Errorf("at the bound the estimate %d and the reservation %d differ", atBound, bound)
	}
	for _, budget := range []string{"16385", "131072"} {
		if estimate, reservation := figures(flash + budget); reservation != bound || estimate <= atBound {
			t.Errorf("a %s budget: reservation %d, want the bound's %d; estimate %d, want above %d",
				budget, reservation, bound, estimate, atBound)
		}
	}
	if estimate, reservation := figures(flash + "16383"); reservation >= bound || estimate != reservation {
		t.Errorf("a 16,383 budget: reservation %d, want below %d; estimate %d, want the reservation", reservation, bound, estimate)
	}
	if estimate, reservation := figures("answer:estimate:hold_cap:google/gemini-3.1-pro-preview:8000+max_tokens:131072"); reservation >= estimate {
		t.Errorf("max_tokens 131,072: reservation %d, want below the estimate %d", reservation, estimate)
	}
}

// TestPythonsReservationOnPassedGoldens: a golden Go passes that Python
// answered without a lease records the credit reservation Python wrote
// (python.reservation_microdollars, §10.1). Where the evaluator reads the
// body, it is the evaluator's reservation: the whole budget's price for a
// stream (for now), at most HoldOutputTokens of it otherwise.
func TestPythonsReservationOnPassedGoldens(t *testing.T) {
	f := authorizeGoldens(t)
	checked, streams := 0, 0
	for _, c := range f.Cases {
		if c.Expect.Outcome != "pass" || c.Expect.Python.Reservation == nil {
			continue
		}
		req, _, why := requestOf(c.body(), &shadowCall{})
		if why != "" {
			continue // a body the evaluator does not read (§3's body.* reasons)
		}
		in, _ := caseInputs(t, f, c)
		req.At = c.at()
		r := in.Snapshot.Evaluate(req)
		if r.Outcome != routingsnapshot.Answered {
			continue
		}
		if req.Stream != nil && *req.Stream {
			streams++
			if r.Reservation != r.ModelEstimate {
				t.Errorf("%s: a stream's reservation %d, want its estimate %d", c.Name, r.Reservation, r.ModelEstimate)
			}
		}
		if got := *c.Expect.Python.Reservation; got != r.Reservation {
			t.Errorf("%s: Python reserved %d, the evaluator %d (estimate %d)", c.Name, got, r.Reservation, r.ModelEstimate)
		}
		checked++
	}
	t.Logf("%d passed goldens' reservations checked, %d of them streams", checked, streams)
	if streams < 2 || checked < 4 {
		t.Fatalf("checked %d (%d streams): fewer than the goldens hold", checked, streams)
	}
}
