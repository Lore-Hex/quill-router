package gwauth

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// P6's goldens (docs/design/fast-admission-e2-g15-async.md §6.1): Python's
// authorize with X-TR-Settlement-Mode, admission off, a test runtime. G15b
// reads them through the compose only: every 200 Python answered that the
// compose can reach, byte for byte, with the async members Stage built from
// the request's recorded facts, the policy's async_settle object and the
// candidates' epoch prices in the file's own routing snapshot.

const asyncCasesFile = "authorize_async_cases_v1.json"

// asyncTarget is one 200 the compose is held to: Python's body, the
// candidates and estimate it was answered from, its timing and its shape.
type asyncTarget struct {
	response   string
	candidates []string
	estimate   int64
	region     string
	timing     Timing
	shape      string // P6's: "none", "A", "B" or "C"
	// reservation is a passed case's 200's: for a request passed before
	// the lease, P6's Python minted its own (the case's ids say another).
	reservation string
}

type asyncCase struct {
	goldenCase
	asyncSettle routingsnapshot.AsyncSettle
	reason      string // the pass reason, or ""
	target      *asyncTarget
}

type asyncGoldens struct {
	file    goldenFile
	snap    snapshotFacts
	ordered []string
	cases   []asyncCase
}

// asyncExtras are the members of P6's file goldenFile does not read.
type asyncExtras struct {
	SnapshotBody       string   `json:"snapshot_body"`
	SnapshotSHA256     string   `json:"snapshot_sha256"`
	OrdinaryParameters []string `json:"ordinary_parameters"`
	AsyncMembers       []string `json:"async_members"`
	Policy             struct {
		AsyncSettle routingsnapshot.AsyncSettle `json:"async_settle"`
	} `json:"policy"`
	Cases []struct {
		Name        string `json:"name"`
		PolicyPatch struct {
			AsyncSettle *routingsnapshot.AsyncSettle `json:"async_settle"`
		} `json:"policy_patch"`
		Expect struct {
			Reason     string `json:"reason"`
			AsyncShape string `json:"async_shape"`
			Python     *struct {
				AsyncShape string `json:"async_shape"`
				Response   string `json:"response"`
			} `json:"python"`
			Affinity *struct {
				ColdAsyncShape string   `json:"cold_async_shape"`
				ColdCandidates []string `json:"cold_candidates"`
				ColdEstimate   int64    `json:"cold_estimate_microdollars"`
				ColdResponse   string   `json:"cold_response"`
				DecideRegion   string   `json:"decide_region"`
			} `json:"affinity"`
		} `json:"expect"`
	} `json:"cases"`
}

func loadAsyncGoldens(t testing.TB) asyncGoldens {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(testdata, asyncCasesFile))
	if err != nil {
		t.Fatal(err)
	}
	var g asyncGoldens
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&g.file); err != nil {
		t.Fatal(err)
	}
	var x asyncExtras
	if err := json.Unmarshal(raw, &x); err != nil {
		t.Fatal(err)
	}
	if len(x.Cases) != len(g.file.Cases) {
		t.Fatal("the two reads of the file disagree")
	}
	sum := sha256.Sum256([]byte(x.SnapshotBody))
	if hex.EncodeToString(sum[:]) != x.SnapshotSHA256 {
		t.Fatal("snapshot_body is not snapshot_sha256's")
	}
	g.snap = snapshotFactsOf(t, []byte(x.SnapshotBody))
	g.ordered = x.OrdinaryParameters
	if !slices.Equal(x.AsyncMembers, []string{"async_eligible", "billing_snapshot", "billing_snapshot_hash",
		"settlement_ticket", "settlement_status_url"}) {
		t.Fatalf("async_members %q", x.AsyncMembers)
	}
	for i, gc := range g.file.Cases {
		xc := x.Cases[i]
		if xc.Name != gc.Name {
			t.Fatalf("case %d: %q and %q", i, xc.Name, gc.Name)
		}
		c := asyncCase{goldenCase: gc, asyncSettle: x.Policy.AsyncSettle, reason: xc.Expect.Reason}
		if p := xc.PolicyPatch.AsyncSettle; p != nil {
			c.asyncSettle = *p
		}
		e := xc.Expect
		switch {
		case gc.Expect.Outcome == "answer":
			c.target = &asyncTarget{response: gc.Expect.Response, shape: e.AsyncShape, timing: gc.Expect.timing()}
		case e.Affinity != nil:
			// The edge decides an affinity request in Go's cold order
			// (affinity option A): Python's cold answer is the target,
			// never its warm one, which a remembered endpoint chose.
			a := e.Affinity
			c.target = &asyncTarget{response: a.ColdResponse, candidates: a.ColdCandidates, estimate: a.ColdEstimate,
				region: a.DecideRegion, shape: a.ColdAsyncShape}
		case e.Python != nil && e.Python.Response != "":
			c.target = &asyncTarget{response: e.Python.Response, shape: e.Python.AsyncShape}
		case e.Python != nil:
			c.target = &asyncTarget{shape: e.Python.AsyncShape} // no 200 to hold (a ticket)
		}
		if c.target != nil && c.target.response != "" && gc.Expect.Outcome != "answer" {
			fillFromResponse(t, c.Name, c.target)
		}
		g.cases = append(g.cases, c)
	}
	return g
}

// fillFromResponse reads what a passed case's input needs from Python's
// 200 itself, which is all the compose renders: the route candidates in
// order, the estimate, the region and the timing.
func fillFromResponse(t testing.TB, name string, target *asyncTarget) {
	t.Helper()
	var body struct {
		Data struct {
			Reservation string `json:"credit_reservation_id"`
			Estimate    int64  `json:"estimated_cost_microdollars"`
			Region      string `json:"region"`
			Candidates  []struct {
				EndpointID string `json:"endpoint_id"`
			} `json:"route_candidates"`
			Timing struct {
				TotalMS      int64 `json:"total_ms"`
				SpannerRPCs  int64 `json:"spanner_rpcs"`
				KeyLookupMS  int64 `json:"key_lookup_ms"`
				RoutingMS    int64 `json:"routing_ms"`
				StoreMS      int64 `json:"store_ms"`
				PostCommitMS int64 `json:"post_commit_ms"`
			} `json:"timing"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(target.response), &body); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	d := body.Data
	ids := make([]string, len(d.Candidates))
	for i, c := range d.Candidates {
		ids[i] = c.EndpointID
	}
	if target.candidates == nil {
		target.candidates, target.estimate = ids, d.Estimate
	} else if !slices.Equal(target.candidates, ids) || target.estimate != d.Estimate {
		t.Fatalf("%s: the case's candidates %q are not its 200's %q", name, target.candidates, ids)
	}
	if target.region == "" {
		target.region = d.Region
	}
	target.reservation = d.Reservation
	tm := d.Timing
	target.timing = Timing{tm.TotalMS, tm.SpannerRPCs, tm.KeyLookupMS, tm.RoutingMS, tm.StoreMS, tm.PostCommitMS}
}

func pairsOf(h [][2]string) [][]string {
	out := make([][]string, len(h))
	for i, p := range h {
		out[i] = []string{p[0], p[1]}
	}
	return out
}

// asyncInputOf is the case as the decide pipeline would hand it to Stage:
// the request parsed strictly (the request half of the cohort is not this
// test's: P6's header cases pass it), its async facts recorded, the
// policy's async_settle object as the workspace sees it (admission on only
// when it covers the workspace, as edge.Decide hands it over: a workspace
// admission_workspaces leaves out is composed as with admission off), and
// the target's candidates.
func asyncInputOf(t *testing.T, g asyncGoldens, c asyncCase) (ComposeInput, Request) {
	t.Helper()
	r, _, p := parseBody([]byte(c.Request.Body), headersOf(pairsOf(c.Request.Headers)))
	if p.Passed() {
		t.Fatalf("%s: the strict parse passes %s", c.Name, p.Reason)
	}
	in := inputOf(t, g.file, g.snap, c.goldenCase)
	if c.Expect.Outcome != "answer" {
		in.Candidates = candidatesOf(t, g.snap, c.Name, c.target.candidates)
		in.Estimate, in.Region = c.target.estimate, c.target.region
		if c.target.reservation != "" {
			in.ReservationID = c.target.reservation
		}
	}
	policy := c.asyncSettle
	policy.AdmissionEnabled = policy.Admits(in.WorkspaceID)
	in.Async = &AsyncInput{Request: r.Async, Policy: policy}
	return in, r
}

// asyncShapes maps P6's shape names to Go's.
var asyncShapes = map[string]string{"none": AsyncNone, "A": AsyncFalseOnly, "B": AsyncSnapshot}

// p6Reasons maps the shape-A reason a P6 case names to Go's.
var p6Reasons = map[string]string{
	"undeclared_member":      AsyncReasonUndeclared,
	"route_type":             AsyncReasonRouteType,
	"input_modalities":       AsyncReasonInputModalities,
	"requested_parameter":    AsyncReasonRequestedParameter,
	"typed_records_false":    AsyncReasonUntyped,
	"receipt_fee":            AsyncReasonReceiptFee,
	"partner":                AsyncReasonPartner,
	"billing_valid_false":    AsyncReasonBillingInvalid,
	"candidate_refused":      AsyncReasonCandidateRefused,
	"ticket_authority_false": AsyncReasonTicketAuthority,
}

// reasonOfName is the reason P6's name gives a shape-A case: the token
// after "async.A".
func reasonOfName(name string) string {
	_, rest, ok := strings.Cut(name, "async.A:")
	if !ok {
		return ""
	}
	token, _, _ := strings.Cut(rest, ":")
	return p6Reasons[token]
}

// notComposed are the P6 cases with a 200 whose bytes the compose cannot
// write, each because the edge passes the request before the compose for
// a fact that changes the 200 outside the async members (Go never answers
// a replay, a federated key or a priced extra). Their async members alone
// are held to Python's where Go's builder reads the fact.
var notComposed = map[string]string{
	"pass:insert.scope_taken:async.A:replay": "a replay (idempotent_replay true); Go never answers one",
	"pass:key.federated:async.A":             "a federated key; the edge passes key.federated",
	"pass:body.priced_extra:async.A:receipt_fee": "receipt_fee_basis_points 1200 in the 200; the edge passes " +
		"body.priced_extra",
}

// asyncSegment is the 200's async members as written: the bytes between
// stage_d's object and tags, without the separating commas.
func asyncSegment(t *testing.T, body string) string {
	t.Helper()
	i := strings.Index(body, `"stage_d":{`)
	k := strings.Index(body, `,"tags":{`)
	if i < 0 || k < 0 {
		t.Fatalf("no stage_d or tags in %s", body)
	}
	end := strings.Index(body[i:], "},")
	if end < 0 {
		t.Fatalf("stage_d does not close in %s", body)
	}
	start := i + end + 1 // after stage_d's "}"
	if start == k {
		return ""
	}
	return body[start+1 : k]
}

func membersBytes(t *testing.T, members pyjson.Object) string {
	t.Helper()
	if len(members) == 0 {
		return ""
	}
	b, err := pyjson.DumpsPydantic(members)
	if err != nil {
		t.Fatal(err)
	}
	return string(b[1 : len(b)-1])
}

// TestEveryAsyncCaseIsPythonsBytes: every P6 200 the compose can reach,
// byte for byte, with Go's shape and reason Python's; the rest counted and
// accounted for.
func TestEveryAsyncCaseIsPythonsBytes(t *testing.T) {
	g := loadAsyncGoldens(t)
	equal, membersOnly, admission, leftOut := 0, 0, 0, 0
	shapes, reasons := map[string]int{}, map[string]int{}
	for _, c := range g.cases {
		if c.target == nil {
			t.Fatalf("%s: no Python answer to hold the compose to", c.Name)
		}
		in, _ := asyncInputOf(t, g, c)
		if c.asyncSettle.AdmissionEnabled && !in.Async.Policy.AdmissionEnabled {
			// Admission on for a pilot set leaving the workspace out:
			// composed and byte-compared below as with admission off.
			leftOut++
		}
		if in.Async.Policy.AdmissionEnabled {
			// policy.async_admission: Python's ticket (or its shape A for a
			// missing nonce). Stage refuses, and the caller passes.
			if _, err := Stage(in); err == nil {
				t.Errorf("%s: Stage composed with admission on", c.Name)
			}
			admission++
			continue
		}
		if c.target.response == "" {
			t.Fatalf("%s: shape %s without a 200", c.Name, c.target.shape)
		}
		staged, err := Stage(in)
		if err != nil {
			t.Fatalf("%s: Stage: %v", c.Name, err)
		}
		got := staged.Async()
		if why, skip := notComposed[c.Name]; skip {
			if c.target.shape != "A" {
				t.Errorf("%s: Python's shape %s", c.Name, c.target.shape)
			}
			// The members alone, where the builder reads the fact (a
			// replay or a federated key is not an input: the edge never
			// composes either).
			if c.reason == "body.priced_extra" {
				if seg := membersBytes(t, got.Members); seg != asyncSegment(t, c.target.response) ||
					got.Reason != AsyncReasonReceiptFee {
					t.Errorf("%s: members %s (%s), Python's %s", c.Name, seg, got.Reason, asyncSegment(t, c.target.response))
				}
				reasons[got.Reason]++
			}
			t.Logf("%s: not composed: %s", c.Name, why)
			membersOnly++
			continue
		}
		if want := asyncShapes[c.target.shape]; got.Shape != want {
			t.Errorf("%s: shape %s (%s), Python's %s", c.Name, got.Shape, got.Reason, c.target.shape)
			continue
		}
		if want := reasonOfName(c.Name); got.Shape == AsyncFalseOnly && got.Reason != want {
			t.Errorf("%s: reason %s, want %s", c.Name, got.Reason, want)
		}
		shapes[got.Shape]++
		reasons[got.Reason]++
		composed, err := staged.Finish(c.Admitted.Envelope, endOfLife(t, c.goldenCase))
		if err != nil {
			t.Fatalf("%s: Finish: %v", c.Name, err)
		}
		body := composed.Render(c.target.timing, false)
		if string(body) != c.target.response {
			t.Errorf("%s: the 200 differs\n got %s\nwant %s", c.Name, body, c.target.response)
			continue
		}
		if c.Expect.Outcome == "answer" {
			// §2.5: the rows are the sync ones, the async members nowhere.
			if composed.Authorization.Payload != c.Expect.AuthorizationRow["payload"] {
				t.Errorf("%s: payload differs\n got %s\nwant %s", c.Name, composed.Authorization.Payload,
					c.Expect.AuthorizationRow["payload"])
			}
			if composed.GenerationID != c.Expect.GenerationID {
				t.Errorf("%s: generation id %s", c.Name, composed.GenerationID)
			}
		}
		equal++
	}
	t.Logf("%d byte-equal; %d members only; %d admission on; shapes %v; reasons %v", equal, membersOnly, admission,
		shapes, reasons)
	if equal+membersOnly+admission != len(g.cases) {
		t.Fatalf("%d of %d cases accounted for", equal+membersOnly+admission, len(g.cases))
	}
	// Positive control: the file is the one this test was written against,
	// every shape and every reason P6 can show reached.
	if equal < 40 || shapes[AsyncNone] < 10 || shapes[AsyncSnapshot] < 13 || shapes[AsyncFalseOnly] < 15 {
		t.Fatalf("%d byte-equal, shapes %v", equal, shapes)
	}
	// The admission cases: the three covered (the member absent, or naming
	// the workspace) refused, the two the member leaves out byte-equal.
	if admission != 3 || leftOut != 2 {
		t.Fatalf("%d admission on, %d left out by admission_workspaces", admission, leftOut)
	}
	for _, r := range p6Reasons {
		if reasons[r] == 0 {
			t.Errorf("no P6 case reaches reason %s", r)
		}
	}
}

// TestTheOrdinaryParametersArePythons: Go's set is the one P6's header
// carries, Python's ORDINARY_PARAMETERS.
func TestTheOrdinaryParametersArePythons(t *testing.T) {
	g := loadAsyncGoldens(t)
	var ours []string
	for p := range ordinaryParameters {
		ours = append(ours, p)
	}
	slices.Sort(ours)
	theirs := slices.Clone(g.ordered)
	slices.Sort(theirs)
	if !slices.Equal(ours, theirs) || len(ours) != 15 {
		t.Fatalf("Go's ordinary parameters %q, P6's %q", ours, theirs)
	}
}

// TestTheHeaderIsExactlyAsyncV1: the request records Python's test, one
// value, that case, nothing around it; every P6 header case agrees with
// Python's shape, and only an exact list composes members.
func TestTheHeaderIsExactlyAsyncV1(t *testing.T) {
	g := loadAsyncGoldens(t)
	seen := map[bool]int{}
	for _, c := range g.cases {
		r, _, p := parseBody([]byte(c.Request.Body), headersOf(pairsOf(c.Request.Headers)))
		if p.Passed() {
			t.Fatalf("%s: %s", c.Name, p.Reason)
		}
		if want := c.target.shape != "none"; r.Async.V1 != want {
			t.Errorf("%s: V1 %v, Python's shape %s", c.Name, r.Async.V1, c.target.shape)
		}
		seen[r.Async.V1]++
	}
	if seen[true] < 20 || seen[false] < 10 {
		t.Fatalf("%v", seen)
	}
	for _, v := range [][]string{nil, {}, {"sync"}, {"async-v1", "async-v1"}, {" async-v1"}, {"ASYNC-V1"}, {""}} {
		if exactlyAsyncV1(v) {
			t.Errorf("%q taken as async-v1", v)
		}
	}
	if !exactlyAsyncV1([]string{"async-v1"}) {
		t.Fatal("the exact list refused")
	}
}

// TestTheRequestRecordsItsAsyncFacts: each fact read from the body as the
// schema holds it, each beside the same body without it.
func TestTheRequestRecordsItsAsyncFacts(t *testing.T) {
	h := Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"async-v1"}}
	parse := func(body []byte) AsyncRequest {
		t.Helper()
		r, _, p := parseBody(body, h)
		if p.Passed() {
			t.Fatalf("%s: %s", body, p.Reason)
		}
		return r.Async
	}
	base := parse(bodyOf(`"route_type":"chat.completions"`))
	if !base.V1 || base.Undeclared || base.InputModalities || base.RouteType == nil ||
		*base.RouteType != "chat.completions" || base.RequestedParameters != nil || base.ServiceTier != nil ||
		base.Model == "" || base.InferenceReceipt || base.AdditionalCost {
		t.Fatalf("the base body's facts: %+v", base)
	}
	for _, v := range []struct {
		field string
		check func(AsyncRequest) bool
	}{
		{`"x_extra":1`, func(a AsyncRequest) bool { return a.Undeclared }},
		{`"x_extra":null`, func(a AsyncRequest) bool { return a.Undeclared }},
		{`"input_modalities":null`, func(a AsyncRequest) bool { return a.InputModalities }},
		{`"input_modalities":["text"]`, func(a AsyncRequest) bool { return a.InputModalities }},
		{`"requested_parameters":[" tools "]`, func(a AsyncRequest) bool {
			return slices.Equal(a.RequestedParameters, []string{"tools"})
		}},
		{`"service_tier":" flex "`, func(a AsyncRequest) bool { return a.ServiceTier != nil && *a.ServiceTier == "flex" }},
		{`"inference_receipt":true`, func(a AsyncRequest) bool { return a.InferenceReceipt }},
		{`"additional_cost_reservation_microdollars":5`, func(a AsyncRequest) bool { return a.AdditionalCost }},
	} {
		if got := parse(bodyOf(`"route_type":"chat.completions"`, v.field)); !v.check(got) {
			t.Errorf("%s: %+v", v.field, got)
		}
	}
	if got := parse(bodyOf(`"route_type":" responses "`)); got.RouteType == nil || *got.RouteType != "responses" {
		t.Errorf("route_type not stripped: %+v", got)
	}
	if got := parse(bodyOf()); got.RouteType != nil {
		t.Errorf("an absent route_type: %+v", got)
	}
}

// baseAsync is P6's single gpt-4.1 shape-B case as Stage's input.
func baseAsync(t *testing.T, g asyncGoldens) (ComposeInput, asyncCase) {
	t.Helper()
	for _, c := range g.cases {
		if c.Name == "answer:async.B:single:openai/gpt-4.1" {
			in, _ := asyncInputOf(t, g, c)
			return in, c
		}
	}
	t.Fatal("no answer:async.B:single:openai/gpt-4.1 case")
	return ComposeInput{}, asyncCase{}
}

// cloneCandidate copies a candidate's epochs and their tiers, so a test
// may change them without writing the snapshot's.
func cloneCandidate(c Candidate) Candidate {
	c.Epochs = slices.Clone(c.Epochs)
	for i := range c.Epochs {
		c.Epochs[i].Price.Tiers = slices.Clone(c.Epochs[i].Price.Tiers)
		if v := c.Epochs[i].BillingValid; v != nil {
			b := *v
			c.Epochs[i].BillingValid = &b
		}
	}
	return c
}

func ptr[T any](v T) *T { return &v }

// TestEachAsyncRefusalHasItsReason: every §2.3 branch the builder checks,
// one fault at a time on a shape-B input, gives its own reason; the same
// input without the fault is shape B (the positive control, held first).
func TestEachAsyncRefusalHasItsReason(t *testing.T) {
	g := loadAsyncGoldens(t)
	base, c := baseAsync(t, g)
	run := func(in ComposeInput) Async {
		t.Helper()
		in.Candidates = slices.Clone(in.Candidates)
		s, err := Stage(in)
		if err != nil {
			t.Fatalf("Stage: %v", err)
		}
		return s.Async()
	}
	if got := run(base); got.Shape != AsyncSnapshot {
		t.Fatalf("the control is %s (%s)", got.Shape, got.Reason)
	}
	if got := membersBytes(t, run(base).Members); got != asyncSegment(t, c.target.response) {
		t.Fatalf("the control's members are not Python's")
	}
	price := func(f func(p *routingsnapshot.Price)) func(*ComposeInput) {
		return func(in *ComposeInput) {
			cand := cloneCandidate(in.Candidates[0])
			f(&cand.Epochs[0].Price)
			in.Candidates[0] = cand
		}
	}
	tiers := func(bounds ...*int64) func(*ComposeInput) {
		return price(func(p *routingsnapshot.Price) {
			p.Tiers = nil
			for _, b := range bounds {
				p.Tiers = append(p.Tiers, routingsnapshot.Tier{MaxPromptTokens: b, Rates: p.Rates})
			}
		})
	}
	req := func(f func(*AsyncRequest)) func(*ComposeInput) {
		return func(in *ComposeInput) {
			a := *in.Async
			f(&a.Request)
			in.Async = &a
		}
	}
	pol := func(f func(*routingsnapshot.AsyncSettle)) func(*ComposeInput) {
		return func(in *ComposeInput) {
			a := *in.Async
			f(&a.Policy)
			in.Async = &a
		}
	}
	cand := func(f func(*Candidate)) func(*ComposeInput) {
		return func(in *ComposeInput) {
			c := cloneCandidate(in.Candidates[0])
			f(&c)
			in.Candidates[0] = c
		}
	}
	many := func(n int) []*int64 {
		out := make([]*int64, n)
		for i := range out {
			out[i] = ptr(int64(i))
		}
		return out
	}
	for _, v := range []struct {
		name   string
		fault  func(*ComposeInput)
		shape  string
		reason string
	}{
		{"no async input", func(in *ComposeInput) { in.Async = nil }, AsyncNone, ""},
		{"header not exact", req(func(r *AsyncRequest) { r.V1 = false }), AsyncNone, ""},
		{"undeclared member", req(func(r *AsyncRequest) { r.Undeclared = true }), AsyncFalseOnly, AsyncReasonUndeclared},
		{"route type absent", req(func(r *AsyncRequest) { r.RouteType = nil }), AsyncFalseOnly, AsyncReasonRouteType},
		{"route type messages", req(func(r *AsyncRequest) { r.RouteType = ptr("messages") }), AsyncFalseOnly, AsyncReasonRouteType},
		{"input modalities", req(func(r *AsyncRequest) { r.InputModalities = true }), AsyncFalseOnly, AsyncReasonInputModalities},
		{"a non-ordinary parameter", req(func(r *AsyncRequest) { r.RequestedParameters = []string{"temperature", "tools"} }),
			AsyncFalseOnly, AsyncReasonRequestedParameter},
		{"untyped records", pol(func(p *routingsnapshot.AsyncSettle) { p.TypedRecords = false }), AsyncFalseOnly, AsyncReasonUntyped},
		{"a service tier", req(func(r *AsyncRequest) { r.ServiceTier = ptr("flex") }), AsyncFalseOnly, AsyncReasonServiceTier},
		{"an inference receipt", req(func(r *AsyncRequest) { r.InferenceReceipt = true }), AsyncFalseOnly, AsyncReasonReceiptFee},
		{"a tool cost", req(func(r *AsyncRequest) { r.AdditionalCost = true }), AsyncFalseOnly, AsyncReasonToolCost},
		{"a partner model", req(func(r *AsyncRequest) { r.Model = "z-ai/glm-5.3" }), AsyncFalseOnly, AsyncReasonPartner},
		{"a prefix that is not a path", req(func(r *AsyncRequest) { r.Model = "openai-gpt" }), AsyncFalseOnly, AsyncReasonPartner},
		{"billing_valid false", cand(func(c *Candidate) { c.Epochs[0].BillingValid = ptr(false) }), AsyncFalseOnly, AsyncReasonBillingInvalid},
		{"provider azure", cand(func(c *Candidate) { c.Provider = "azure" }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		// The provider literal alone: the model is under its provider.
		{"provider azure, model under it", cand(func(c *Candidate) { c.Provider, c.Model = "azure", "azure/gpt-4.1" }),
			AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"model not under provider", cand(func(c *Candidate) { c.Model = "anthropic/gpt-4.1" }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"model id not an identity", cand(func(c *Candidate) { c.Model = "openai/gpt 4.1" }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"usage BYOK", cand(func(c *Candidate) { c.UsageType = "BYOK" }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"a request fee", price(func(p *routingsnapshot.Price) { p.RequestFeeMicro = 1 }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"a negative request fee", price(func(p *routingsnapshot.Price) { p.RequestFeeMicro = -1 }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"a negative rate", price(func(p *routingsnapshot.Price) { p.Rates.CacheCreationMicroPerMillion = -1 }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"a negative tier rate", price(func(p *routingsnapshot.Price) { p.Tiers[0].Rates.OutputMicroPerMillion = -1 }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"price version 2", price(func(p *routingsnapshot.Price) { p.PriceHistoryVersion = 2 }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"another rounding", price(func(p *routingsnapshot.Price) { p.Rounding = "half_even_per_million" }), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"unbounded tier not last", tiers(nil, ptr(int64(10))), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"tiers out of order", tiers(ptr(int64(10)), ptr(int64(5))), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"tiers equal", tiers(ptr(int64(10)), ptr(int64(10))), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"a negative tier bound", tiers(ptr(int64(-1))), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"65 tiers", tiers(many(65)...), AsyncFalseOnly, AsyncReasonCandidateRefused},
		{"64 tiers", tiers(many(64)...), AsyncSnapshot, ""},
		{"a first bound of 0", tiers(ptr(int64(0)), nil), AsyncSnapshot, ""},
		{"no tiers", tiers(), AsyncSnapshot, ""},
		{"a duplicate candidate", func(in *ComposeInput) { in.Candidates = append(in.Candidates, in.Candidates[0]) },
			AsyncFalseOnly, AsyncReasonCandidateSet},
		{"no ticket authority", pol(func(p *routingsnapshot.AsyncSettle) { p.TicketAuthority = false }), AsyncFalseOnly,
			AsyncReasonTicketAuthority},
		{"edge_decide off is not the builder's", pol(func(p *routingsnapshot.AsyncSettle) { p.EdgeDecide = false }), AsyncSnapshot, ""},
		{"an ordinary parameter", req(func(r *AsyncRequest) { r.RequestedParameters = []string{"temperature", "stream"} }), AsyncSnapshot, ""},
		{"service tier default", req(func(r *AsyncRequest) { r.ServiceTier = ptr("default") }), AsyncSnapshot, ""},
		{"an anthropic model name", req(func(r *AsyncRequest) { r.Model = "anthropic/claude-sonnet-4.5" }), AsyncSnapshot, ""},
		// First failing check wins, in Python's order.
		{"undeclared before partner", req(func(r *AsyncRequest) { r.Undeclared, r.Model = true, "z-ai/glm-5.3" }),
			AsyncFalseOnly, AsyncReasonUndeclared},
		{"untyped before billing", func(in *ComposeInput) {
			pol(func(p *routingsnapshot.AsyncSettle) { p.TypedRecords = false })(in)
			cand(func(c *Candidate) { c.Epochs[0].BillingValid = ptr(false) })(in)
		}, AsyncFalseOnly, AsyncReasonUntyped},
		{"billing before ticket authority", func(in *ComposeInput) {
			pol(func(p *routingsnapshot.AsyncSettle) { p.TicketAuthority = false })(in)
			cand(func(c *Candidate) { c.Epochs[0].BillingValid = ptr(false) })(in)
		}, AsyncFalseOnly, AsyncReasonBillingInvalid},
	} {
		in := base
		in.Candidates = slices.Clone(base.Candidates)
		v.fault(&in)
		got := run(in)
		if got.Shape != v.shape || got.Reason != v.reason {
			t.Errorf("%s: %s (%s), want %s (%s)", v.name, got.Shape, got.Reason, v.shape, v.reason)
		}
		if v.shape == AsyncFalseOnly && membersBytes(t, got.Members) != `"async_eligible":false` {
			t.Errorf("%s: shape A's members %s", v.name, membersBytes(t, got.Members))
		}
	}
	// The candidate count: none, and 65 distinct (the builder directly:
	// Stage refuses no candidates itself).
	a := base.Async
	if got, err := asyncAdditions(a, nil, base.At.Unix()); err != nil || got.Reason != AsyncReasonCandidateSet {
		t.Errorf("no candidates: %+v %v", got, err)
	}
	var lots []Candidate
	for i := range 65 {
		c := cloneCandidate(base.Candidates[0])
		c.EndpointID = c.EndpointID + "-" + string(rune('a'+i/26)) + string(rune('a'+i%26))
		c.Epochs[0].Price.EndpointID = c.EndpointID
		lots = append(lots, c)
	}
	if got, err := asyncAdditions(a, lots, base.At.Unix()); err != nil || got.Reason != AsyncReasonCandidateSet {
		t.Errorf("65 candidates: %+v %v", got, err)
	}
	if got, err := asyncAdditions(a, lots[:64], base.At.Unix()); err != nil || got.Shape != AsyncSnapshot {
		t.Errorf("64 candidates: %+v %v", got, err)
	}
}

// TestStageRefusesWhatGoCannotAnswer: admission on (the ticket is
// Python's), an unpublished billing_valid, an instant before every epoch
// and a price that is another endpoint's each refuse; the control composes.
func TestStageRefusesWhatGoCannotAnswer(t *testing.T) {
	g := loadAsyncGoldens(t)
	base, _ := baseAsync(t, g)
	if _, err := Stage(base); err != nil {
		t.Fatalf("the control: %v", err)
	}
	for name, fault := range map[string]func(*ComposeInput){
		"admission on": func(in *ComposeInput) {
			a := *in.Async
			a.Policy.AdmissionEnabled = true
			in.Async = &a
		},
		"billing_valid unpublished": func(in *ComposeInput) {
			c := cloneCandidate(in.Candidates[0])
			c.Epochs[0].BillingValid = nil
			in.Candidates = []Candidate{c}
		},
		"before every epoch": func(in *ComposeInput) {
			in.At = time.Unix(in.Candidates[0].Epochs[0].StartsAt-1, 0)
		},
		"another endpoint's price": func(in *ComposeInput) {
			c := cloneCandidate(in.Candidates[0])
			c.Epochs[0].Price.EndpointID = "openai/gpt-4.1@openai/byok"
			in.Candidates = []Candidate{c}
		},
	} {
		in := base
		fault(&in)
		if _, err := Stage(in); err == nil {
			t.Errorf("%s: Stage composed", name)
		}
	}
	// Admission on does not matter without the header: the 200 adds nothing.
	in := base
	a := *in.Async
	a.Policy.AdmissionEnabled, a.Request.V1 = true, false
	in.Async = &a
	if s, err := Stage(in); err != nil || s.Async().Shape != AsyncNone {
		t.Errorf("admission on without the header: %v", err)
	}
}

// TestTheSnapshotIsSortedAndPricedAtTheInstant: candidates in route order
// give the snapshot sorted by endpoint id (P6's mixed-provider case, whose
// route order is openai then anthropic, and its reverse giving the same
// bytes), and a candidate with two epochs is priced by the one the instant
// falls in. (P6's route_order_sorted case routes anthropic first, already
// sorted; P6's @late case's endpoint has one epoch.)
func TestTheSnapshotIsSortedAndPricedAtTheInstant(t *testing.T) {
	g := loadAsyncGoldens(t)
	var in ComposeInput
	var c asyncCase
	for _, x := range g.cases {
		if strings.HasPrefix(x.Name, "answer:async.B:mixed_provider:") {
			in, _ = asyncInputOf(t, g, x)
			c = x
		}
	}
	if len(in.Candidates) != 2 || in.Candidates[0].EndpointID < in.Candidates[1].EndpointID {
		t.Fatalf("the route_order_sorted case is not out of order: %+v", in.Candidates)
	}
	s, err := Stage(in)
	if err != nil {
		t.Fatal(err)
	}
	want := asyncSegment(t, c.target.response)
	if got := membersBytes(t, s.Async().Members); got != want {
		t.Fatalf("members %s, Python's %s", got, want)
	}
	rev := in
	rev.Candidates = []Candidate{in.Candidates[1], in.Candidates[0]}
	s, err = Stage(rev)
	if err != nil || membersBytes(t, s.Async().Members) != want {
		t.Fatalf("route order changed the snapshot: %v", err)
	}

	base, _ := baseAsync(t, g)
	cand := cloneCandidate(base.Candidates[0])
	later := cand.Epochs[0]
	later.StartsAt += 3600
	later.Price.Rates.InputMicroPerMillion++
	cand.Epochs = append(cand.Epochs, later)
	priced := func(at time.Time) int64 {
		t.Helper()
		in := base
		in.At, in.Candidates = at, []Candidate{cand}
		s, err := Stage(in)
		if err != nil || s.Async().Shape != AsyncSnapshot {
			t.Fatalf("at %v: %v %+v", at, err, s.Async())
		}
		snapshot := s.Async().Members[1].Value.(pyjson.Object)
		first := snapshot[2].Value.([]any)[0].(pyjson.Object)
		return first[5].Value.(pyjson.Object)[0].Value.(int64)
	}
	start := time.Unix(later.StartsAt, 0)
	if got := priced(start.Add(-time.Microsecond)); got != cand.Epochs[0].Price.Rates.InputMicroPerMillion {
		t.Errorf("before the second epoch: %d", got)
	}
	if got := priced(start); got != later.Price.Rates.InputMicroPerMillion {
		t.Errorf("at the second epoch: %d", got)
	}
}

// TestTheHashIsTheCanonicalSnapshots: the hash is sha256 of the sorted,
// ASCII, compact encoding, not of the 200's declaration-order bytes, for
// every shape-B case (the control: the two encodings differ).
func TestTheHashIsTheCanonicalSnapshots(t *testing.T) {
	g := loadAsyncGoldens(t)
	checked := 0
	for _, c := range g.cases {
		if c.Expect.Outcome != "answer" || c.target.shape != "B" {
			continue
		}
		in, _ := asyncInputOf(t, g, c)
		s, err := Stage(in)
		if err != nil {
			t.Fatal(err)
		}
		m := s.Async().Members
		canonical, err := pyjson.Canonical(m[1].Value)
		if err != nil {
			t.Fatal(err)
		}
		ordered, err := pyjson.DumpsPydantic(m[1].Value)
		if err != nil {
			t.Fatal(err)
		}
		sum, other := sha256.Sum256(canonical), sha256.Sum256(ordered)
		if m[2].Value != hex.EncodeToString(sum[:]) || bytes.Equal(canonical, ordered) ||
			m[2].Value == hex.EncodeToString(other[:]) {
			t.Errorf("%s: hash %v", c.Name, m[2].Value)
		}
		checked++
	}
	if checked < 10 {
		t.Fatalf("%d shape-B cases", checked)
	}
}

// fuzzEndpoints are the snapshot's endpoint ids in a fixed order, which the
// fuzzer's candidate bytes index.
func fuzzEndpoints(snap snapshotFacts) []string {
	ids := make([]string, 0, len(snap.endpoints))
	for id := range snap.endpoints {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	return ids
}

// FuzzAsyncAdditions: the builder against P6's inputs and mutations of
// them: any body, any header list, any policy bits, any candidates of the
// snapshot in any order with their prices perturbed. It never panics, is
// deterministic, gives no members without the exact header, gives shape B
// only for a snapshot that satisfies §3.2 (sorted unique Python-valid
// candidates, the canonical hash) and is the same whatever the route
// order, and names for shape A a reason whose fact holds of the input.
func FuzzAsyncAdditions(f *testing.F) {
	g := loadAsyncGoldens(f)
	ids := fuzzEndpoints(g.snap)
	index := map[string]byte{}
	for i, id := range ids {
		index[id] = byte(i)
	}
	for _, c := range g.cases {
		if c.target == nil || c.target.candidates == nil && c.Expect.Outcome != "answer" {
			continue
		}
		var cands []byte
		if c.Expect.Outcome == "answer" {
			var payload struct {
				Endpoints []string `json:"candidate_endpoint_ids"`
			}
			_ = json.Unmarshal([]byte(c.Expect.AuthorizationRow["payload"].(string)), &payload)
			for _, id := range payload.Endpoints {
				cands = append(cands, index[id])
			}
		} else {
			for _, id := range c.target.candidates {
				cands = append(cands, index[id])
			}
		}
		var header string
		for _, h := range c.Request.Headers {
			if strings.EqualFold(h[0], "x-tr-settlement-mode") {
				header += h[1] + "\n"
			}
		}
		p := c.asyncSettle
		bits := byte(0)
		for i, b := range []bool{p.AdmissionEnabled, p.TicketAuthority, p.TypedRecords, p.EdgeDecide} {
			if b {
				bits |= 1 << i
			}
		}
		f.Add([]byte(c.Request.Body), strings.TrimSuffix(header, "\n"), bits, cands, []byte{}, c.AtUS)
	}
	f.Fuzz(func(t *testing.T, body []byte, header string, bits byte, cands, perturb []byte, atUS int64) {
		h := Headers{ContentType: []string{"application/json"}}
		if header != "" {
			h.SettlementMode = strings.Split(header, "\n")
		}
		r, _, p := parseBody(body, h)
		if p.Passed() {
			return
		}
		in := &AsyncInput{Request: r.Async, Policy: routingsnapshot.AsyncSettle{
			AdmissionEnabled: bits&1 != 0, TicketAuthority: bits&2 != 0, TypedRecords: bits&4 != 0, EdgeDecide: bits&8 != 0,
		}}
		var candidates []Candidate
		for _, b := range cands {
			candidates = append(candidates, cloneCandidate(g.snap.endpoints[ids[int(b)%len(ids)]]))
		}
		for i := 0; i+1 < len(perturb) && len(candidates) > 0; i += 2 {
			perturbCandidate(&candidates[int(perturb[i+1])%len(candidates)], perturb[i], int64(int8(perturb[i+1])))
		}
		at := atUS / 1_000_000
		got, err := asyncAdditions(in, candidates, at)
		again, err2 := asyncAdditions(in, candidates, at)
		if !reflect.DeepEqual(got, again) || (err == nil) != (err2 == nil) {
			t.Fatal("not deterministic")
		}
		if err != nil {
			if !in.Request.V1 {
				t.Fatalf("an error without the header: %v", err)
			}
			return
		}
		if (got.Shape == AsyncNone) != !in.Request.V1 || (got.Shape == AsyncNone) != (len(got.Members) == 0) {
			t.Fatalf("shape %s with V1 %v", got.Shape, in.Request.V1)
		}
		if _, err := pyjson.DumpsPydantic(pyjson.Object(got.Members)); err != nil {
			t.Fatalf("the members do not encode: %v", err)
		}
		switch got.Shape {
		case AsyncFalseOnly:
			if !reasonHolds(got.Reason, in, candidates, at) {
				t.Fatalf("reason %s does not hold of the input", got.Reason)
			}
		case AsyncSnapshot:
			checkSnapshot(t, got, in, candidates)
			rev := slices.Clone(candidates)
			slices.Reverse(rev)
			if back, err := asyncAdditions(in, rev, at); err != nil || !reflect.DeepEqual(back, got) {
				t.Fatal("the route order changed shape B")
			}
		}
	})
}

// perturbCandidate changes one price or identity fact of the candidate.
func perturbCandidate(c *Candidate, op byte, arg int64) {
	e := &c.Epochs[len(c.Epochs)-1]
	p := &e.Price
	switch op % 12 {
	case 0:
		e.BillingValid = ptr(arg%2 == 0)
	case 1:
		c.Provider = []string{"openai", "anthropic", "azure", ""}[uint64(arg)%4]
	case 2:
		p.RequestFeeMicro = arg
	case 3:
		p.Rates.InputMicroPerMillion = arg
	case 4:
		if len(p.Tiers) > 0 {
			p.Tiers[0].MaxPromptTokens = ptr(arg)
		}
	case 5:
		p.Tiers = append(p.Tiers, routingsnapshot.Tier{MaxPromptTokens: ptr(arg), Rates: p.Rates})
	case 6:
		p.Tiers = append(p.Tiers, routingsnapshot.Tier{Rates: p.Rates})
	case 7:
		p.PriceHistoryVersion = arg
	case 8:
		c.Model = c.Provider + "/m" + string(rune('a'+uint64(arg)%26))
	case 9:
		c.UsageType = []string{"Credits", "BYOK"}[uint64(arg)%2]
	case 10:
		if len(p.Tiers) > 0 {
			p.Tiers[len(p.Tiers)-1].Rates.CachedInputMicroPerMillion = arg
		}
	case 11:
		slices.Reverse(p.Tiers)
	}
}

// reasonHolds is a shape-A reason's fact, read from the input directly.
func reasonHolds(reason string, in *AsyncInput, candidates []Candidate, at int64) bool {
	r := in.Request
	anyCandidate := func(f func(Candidate, routingsnapshot.Epoch) bool) bool {
		for _, c := range candidates {
			if e, ok := epochAt(c.Epochs, at); ok && f(c, e) {
				return true
			}
		}
		return false
	}
	switch reason {
	case AsyncReasonUndeclared:
		return r.Undeclared
	case AsyncReasonRouteType:
		return r.RouteType == nil || (*r.RouteType != "chat.completions" && *r.RouteType != "responses")
	case AsyncReasonInputModalities:
		return r.InputModalities
	case AsyncReasonRequestedParameter:
		return slices.ContainsFunc(r.RequestedParameters, func(p string) bool { return !ordinaryParameters[p] })
	case AsyncReasonUntyped:
		return !in.Policy.TypedRecords
	case AsyncReasonServiceTier:
		return r.ServiceTier != nil && *r.ServiceTier != "default"
	case AsyncReasonReceiptFee:
		return r.InferenceReceipt
	case AsyncReasonToolCost:
		return r.AdditionalCost
	case AsyncReasonPartner:
		return !strings.HasPrefix(r.Model, "openai/") && !strings.HasPrefix(r.Model, "anthropic/")
	case AsyncReasonBillingInvalid:
		return anyCandidate(func(_ Candidate, e routingsnapshot.Epoch) bool { return e.BillingValid != nil && !*e.BillingValid })
	case AsyncReasonCandidateRefused:
		return anyCandidate(func(c Candidate, e routingsnapshot.Epoch) bool { return !billingCandidateValid(c, e.Price) })
	case AsyncReasonCandidateSet:
		seen := map[string]bool{}
		for _, c := range candidates {
			if seen[c.EndpointID] {
				return true
			}
			seen[c.EndpointID] = true
		}
		return len(candidates) == 0 || len(candidates) > maxBillingCandidates
	case AsyncReasonTicketAuthority:
		return !in.Policy.TicketAuthority
	}
	return false
}

// checkSnapshot holds a shape B to §3.2, reading its members back.
func checkSnapshot(t *testing.T, got Async, in *AsyncInput, candidates []Candidate) {
	t.Helper()
	r := in.Request
	if !in.Policy.TicketAuthority || !in.Policy.TypedRecords || in.Policy.AdmissionEnabled || r.Undeclared ||
		r.InputModalities || r.InferenceReceipt || r.AdditionalCost {
		t.Fatalf("shape B for %+v", in)
	}
	if len(got.Members) != 3 || got.Members[0].Value != false {
		t.Fatalf("members %v", got.Members)
	}
	encoded, err := pyjson.DumpsPydantic(got.Members[1].Value)
	if err != nil {
		t.Fatal(err)
	}
	// Decoded back, the snapshot re-encodes canonically to the hash.
	tree, err := pyjson.Decode(encoded)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := pyjson.Canonical(tree)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(canonical)
	if got.Members[2].Value != hex.EncodeToString(sum[:]) {
		t.Fatal("the hash is not the canonical snapshot's")
	}
	list := got.Members[1].Value.(pyjson.Object)[2].Value.([]any)
	if len(list) != len(candidates) || len(list) == 0 || len(list) > maxBillingCandidates {
		t.Fatalf("%d candidates for %d", len(list), len(candidates))
	}
	previous := ""
	for _, item := range list {
		o := item.(pyjson.Object)
		id, provider, model := o[0].Value.(string), o[1].Value.(string), o[2].Value.(string)
		if id <= previous || !identity.MatchString(id) || !identity.MatchString(model) ||
			(provider != "openai" && provider != "anthropic") || !strings.HasPrefix(model, provider+"/") ||
			o[3].Value != usageCredits || o[7].Value != int64(0) {
			t.Fatalf("candidate %v", o)
		}
		previous = id
	}
}
