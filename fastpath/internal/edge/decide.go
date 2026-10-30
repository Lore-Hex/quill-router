package edge

import (
	"context"
	"net/http"
	"slices"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
	"github.com/Lore-Hex/quill-router/fastpath/internal/keywindow"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The decide pipeline (docs/design/fast-admission-e2-decide.md §1 steps 2
// to 6, §3): everything the edge would check and compose before it asks
// the front door to admit an authorize, as one pure function of what the
// edge holds when the request arrives. Decide answers either a Decision
// (the request is in the cohort; staged rows and a 200 ready for the
// envelope) or the §3 reason it goes to Python. Nothing here admits,
// writes or answers: the full-response shadow (shadow_full.go) compares a
// decision (Decided.ForShadow) with what Python did, off the request path,
// and the decide path (decide_path.go) admits, inserts and answers one
// (Decided.Admit).
//
// Decide is only for an authorize the edge has authenticated (the
// internal token) and read: build its request with RequestOf, which records
// the method and the path as both E1 and Python read them; any other
// passes (request.path).

// The §3 pass reasons Decide names beyond gwauth's request reasons (the
// body.* and header.* reasons, and tags.merged_invalid).
const (
	PassBootSignature  = "boot.signature"
	PassBootUnverified = "boot.unverified"
	PassBootMissing    = "boot.missing"
	// PassBootUnread: the boot could not be read within BootWait (E1's
	// Unread verdict); Python decides as it would alone.
	PassBootUnread = "boot.unread"
	// PassRequestPath: not a POST to exactly an authorize path, as E1
	// (r.URL.Path, decoded) and Python (request.url.path) both read it: an
	// escaped path, whose decoding the two may read apart, passes.
	PassRequestPath = "request.path"

	PassCachePolicy    = "cache.policy"
	PassCacheSnapshot  = "cache.snapshot"
	PassCacheKey       = "cache.key"
	PassCacheWorkspace = "cache.workspace"
	PassCacheDecide    = "cache.decide"

	PassKeyMissing   = "key.missing"
	PassKeyDisabled  = "key.disabled"
	PassKeyExpired   = "key.expired"
	PassKeyScope     = "key.scope"
	PassKeyFederated = "key.federated"
	PassKeyApp       = "key.app"
	PassKeyCap       = "key.key_cap"
	// PassKeyWindow and PassKeyWindowAlert are the edge policy's
	// fastpath_pilot_key_windows as a decision gate
	// (docs/design/fast-admission-e2-key-windows.md section 4.7): a key whose
	// spend windows Python's authorize enforces, under mode off or alert; an
	// alert-only one, under mode off.
	PassKeyWindow       = "key.key_window"
	PassKeyWindowAlert  = "key.key_window_alert"
	PassKeyBudgetStrict = "key.budget_strict"
	// PassKeyShape: the key's stored body is not one Go reads exactly
	// (gwstate's key_shape:<field>), such as a usage_shard_count Python's
	// re-validation would refuse.
	PassKeyShape = "key.shape"

	PassWorkspaceMissing   = "workspace.missing"
	PassWorkspacePaused    = "workspace.paused"
	PassWorkspaceInDebt    = "workspace.in_debt"
	PassWorkspaceTier      = "workspace.tier"
	PassWorkspaceFederated = "workspace.federated"
	PassWorkspaceDeleted   = "workspace.deleted"
	PassWorkspaceBroadcast = "workspace.broadcast"
	// PassWorkspaceShape: the workspace's body is not one Go reads exactly,
	// or names another workspace than the key's.
	PassWorkspaceShape = "workspace.shape"
	// PassWorkspaceCreditRows: the credit rows are not shards 0 to n-1
	// (store.ErrCreditRowsIncomplete), which a grant refuses.
	PassWorkspaceCreditRows = "workspace.credit_rows"
	// PassWorkspaceRefused: the rows' refusal is one the reasons above do
	// not name (none today; store.rowsRefuse names only those).
	PassWorkspaceRefused = "workspace.refused"

	PassPilotOff           = "policy.pilot_off"
	PassNotPilotWorkspace  = "policy.not_pilot_workspace"
	PassPilotRegion        = "policy.pilot_region"
	PassRecordsNotDurable  = "policy.records_not_durable"
	PassAsyncSettleShadow  = "policy.async_settle_shadow"
	PassMonitorKey         = "policy.monitor_key"
	PassLookupHashMismatch = "policy.lookup_hash_mismatch"
	PassDigestNotAccepted  = "policy.digest_not_accepted"
	PassRegionsMismatch    = "policy.regions_mismatch"
	PassDecideOff          = "decide.off"

	// The async-v1 gates (docs/design/fast-admission-e2-g15-async.md §4.1,
	// §6.1), for a request whose X-TR-Settlement-Mode is exactly
	// ["async-v1"]. PassAsyncUnpublished is a keyless policy gate: the
	// policy carries no async_settle object or the snapshot no
	// billing_valid, so Go cannot build Python's answer. PassAsyncAdmission
	// is the first gate once the key's workspace is known: Python's
	// admission covers that workspace (async_settle.Admits), so the ticket is
	// Python's; a workspace its admission_workspaces leaves out is composed
	// as with admission off, Python's own answer to it. PassAsyncOff is a
	// decision gate after the compose, beside the decide row's: the policy's
	// edge_decide is false, so the request is composed (and compared by the
	// shadow) but never answered.
	PassAsyncUnpublished = "async.unpublished"
	PassAsyncAdmission   = "policy.async_admission"
	PassAsyncOff         = "async.off"
	// PassSettlementHeader is gwauth's request reason for any other present
	// X-TR-Settlement-Mode, named here for the watch's settlement-header
	// pass share (only the edge uses gwauth's request side).
	PassSettlementHeader = gwauth.ReasonSettlementAsync

	// PassEvaluate is any evaluator pass not named below (the request's
	// shape or a snapshot input Go does not reproduce); PassEvaluateRefused
	// any refusal, which Python makes itself.
	PassEvaluate              = "evaluate.pass"
	PassEvaluateRefused       = "evaluate.refused"
	PassEstimateOverflow      = "evaluate.estimate_overflow"
	PassCandidatesBYOK        = "candidates.byok"
	PassCandidatesEstimated   = "candidates.estimated_usage"
	PassCandidatesPrivate     = "candidates.private_proxy"
	PassClockWindow           = "clock.window"
	PassCompose               = "compose.refused"
	evaluatorWindowReason     = "the instant is outside the snapshot's window"
	evaluatorEstimateOverflow = "the estimate exceeds int64"
)

// EstimatedUsageModels is ESTIMATED_USAGE_SNAPSHOT_MODELS (stage_d.py,
// decision 7): a candidate of one of these models passes. A test holds it
// equal to P1's header.
var EstimatedUsageModels = []string{
	"abliterate/abliterate-0.3-balanced",
	"abliterate/abliterate-0.3-clever",
	"abliterate/abliterate-0.3-fast",
}

// BootSeen is E1's verdict on a request's boot signature and the boot the
// verdict read: Kid is the header's when it parsed, Found whether a boot
// row has that kid, and Boot that row.
type BootSeen struct {
	Verdict Verdict
	Kid     string
	Found   bool
	Boot    Boot
}

// DecideRequest is the request as the edge received it, at its one clock
// reading (decision 15; the monotonic reading kept, as gwstate's Lookup
// compares it). Body is nil when the edge did not read it (no boot header,
// or one past its bound): the boot passes it first.
type DecideRequest struct {
	At     time.Time
	Method string
	// Path is r.URL.Path (decoded, as E1 verifies the boot signature over
	// it) and EscapedPath r.URL.EscapedPath(): they differ for a path the
	// client escaped.
	Path, EscapedPath string
	Header            http.Header
	Body              []byte
	Boot              BootSeen
}

// RequestOf is the DecideRequest of an authenticated request the edge
// read: at its one clock reading, the body the edge read (nil for none) and
// E1's verdict. The decide path (G11) and the shadow build it only this
// way.
func RequestOf(r *http.Request, at time.Time, body []byte, boot BootSeen) DecideRequest {
	return DecideRequest{At: at, Method: r.Method, Path: r.URL.Path, EscapedPath: r.URL.EscapedPath(),
		Header: r.Header.Clone(), Body: body, Boot: boot}
}

// Mint makes a decision's ids; a nil func is gwauth's random one. A test
// pins them.
type Mint struct {
	AuthorizationID, ReservationID, IdempotencyKey func() string
}

func (m Mint) authorization() string {
	if m.AuthorizationID != nil {
		return m.AuthorizationID()
	}
	return gwauth.NewAuthorizationID()
}

func (m Mint) reservation() string {
	if m.ReservationID != nil {
		return m.ReservationID()
	}
	return gwauth.NewReservationID()
}

func (m Mint) idempotencyKey() func() string {
	if m.IdempotencyKey != nil {
		return m.IdempotencyKey
	}
	return gwauth.NewIdempotencyKey
}

// DecideInputs are everything Decide reads, each held once for the request:
// the routing snapshot and the edge policy held when it arrived (nil for
// none), the key and workspace cache's lookup, the decide copy, and the
// ids to mint.
type DecideInputs struct {
	Request  DecideRequest
	Snapshot *routingsnapshot.Snapshot
	Policy   *routingsnapshot.EdgePolicy
	// Lookup is the cache's (gwstate.Cache.Lookup); nil is a cold cache.
	Lookup func(lookupHash string, now time.Time) gwstate.Read
	Decide DecideView
	Mint   Mint
}

// Decision is a request in the cohort: what the edge would admit and the
// rows and the 200 it would write once admitted (Finish, then Render).
// Input is what Staged was built from, kept so the shadow can stage the
// same decision under Python's ids (Restage).
type Decision struct {
	Staged   gwauth.Staged
	Input    gwauth.ComposeInput
	Identity gwauth.Identity
	// Estimate is the spend ceiling (Result.ModelEstimate: the rows and the
	// 200 carry it); Reservation is the credit hold the owner's lease takes
	// (Result.Reservation, at most HoldOutputTokens of the output budget).
	Estimate    int64
	Reservation int64
	Workspace   string
	KeyHash     string
	LookupHash  string
	Kid         string
	Region      string
	Candidates  []string
	// Affinity is whether the body names a cache_affinity_key: the
	// candidates are Go's cold order, decided like any other (§1 item 5).
	// Routing is the evaluator's request and Snapshot the snapshot it
	// answered from, for the shadow to name a Python answer that is that
	// order re-ranked for a remembered session (affinity.go).
	Affinity bool
	Routing  routingsnapshot.Request
	Snapshot *routingsnapshot.Snapshot
	// Windows is the window check a key whose spend windows Python's
	// authorize enforces needs before the edge may admit (key-windows
	// section 4.2): nil for any other key, an alert-only one included.
	Windows *WindowCheck
}

// WindowCheck is what the edge checks an enforcing windowed key against: the
// cached limits and the key's tr_key_limit shard count. The estimate checked
// is the Decision's (Python checks the model estimate, never the
// reservation).
type WindowCheck struct {
	Limits keywindow.Limits
	Shards int
}

// Finish is the decision admitted: the front door's envelope and end of
// life added, the two rows and the 200 (Composed.Render). An error refunds
// and passes (G11).
func (d *Decision) Finish(envelope string, endOfLife time.Time) (gwauth.Composed, error) {
	return d.Staged.Finish(envelope, endOfLife)
}

// Restage is the decision staged again under other ids and instant (the
// shadow's substitution of Python's per-request values, §10.1): the same
// candidates, estimate, request and facts.
func (d *Decision) Restage(authorizationID, reservationID, idempotencyKey string, at time.Time) (gwauth.Staged, error) {
	in := d.Input
	in.AuthorizationID, in.ReservationID, in.At = authorizationID, reservationID, at
	if idempotencyKey != in.IdempotencyKey {
		in.IdempotencyKey = idempotencyKey
		in.IdempotencyScope = gwauth.ScopeOf(in.WorkspaceID, in.KeyHash, idempotencyKey)
	}
	return gwauth.Stage(in)
}

// Decided is Decide's answer. Pass is "" when the edge would answer the
// request itself, else the first §3 reason it goes to Python. ForShadow is
// the decision when every gate but the decision gates passed, so the shadow
// compares what the edge would answer for a workspace it does not yet
// decide; DecideGate is then the decision gate's reason ("" for on; for a
// windowed key the policy's mode does not cover, PassKeyWindow or
// PassKeyWindowAlert; else the decide row's PassDecideOff or
// PassCacheDecide; else, for an async-v1 request while the policy's
// edge_decide is false, PassAsyncOff), and Pass is it. A windowed key the
// mode does not cover passes with its window reason whatever gate after
// the key's facts passes it, as it did when its windows kept it out of the
// cohort. Only Admit hands out a decision to admit, and only when Pass is
// "". LookupHash and Workspace are what the request named and what the
// cache said of its key, "" when not known: the shadow and the decide path
// evict them when Python refuses the key.
type Decided struct {
	Pass string
	// shadowOnly is the decision for comparison: read through ForShadow,
	// admitted only through Admit. Outside this file nothing names it, and
	// only the shadow calls ForShadow (TestOnlyAdmitHandsOutADecision).
	shadowOnly *Decision
	DecideGate string
	LookupHash string
	Workspace  string
	// Read is what the cache held for the lookup hash (zero when not
	// looked up).
	Read gwstate.Read
	// AsyncPop is the shadow's async-v1 population (§4.4) of a request
	// whose X-TR-Settlement-Mode is exactly ["async-v1"], whatever it passes
	// with: AsyncPlain or AsyncAffinity (whether the body names a
	// cache_affinity_key) once the strict parse has read the body whole,
	// else AsyncUnparsed; "" for any other request.
	AsyncPop string
	// StreamStageD is the shadow's split of a request passed as body.stream
	// (docs/design/fast-admission-streams.md section 8.1): the Stage D
	// reason the stream would get, as far as the edge reads it before the
	// key's facts (streamStageD); "" for any other request. It changes no
	// decision: every stream still passes.
	StreamStageD string
}

// The async-v1 populations (Decided.AsyncPop).
const (
	AsyncPlain    = "plain"
	AsyncAffinity = "affinity"
	AsyncUnparsed = "unparsed"
)

// Admit is the decision the edge may admit: one, and true, only when every
// gate passed, the decide row's included.
func (d Decided) Admit() (*Decision, bool) {
	if d.Pass != "" || d.shadowOnly == nil {
		return nil, false
	}
	return d.shadowOnly, true
}

// ForShadow is the decision to compare with Python's answer, the decide
// row's gate aside: nil when a gate before it passed. It is the shadow's
// alone, and never admitted (Admit is).
func (d Decided) ForShadow() *Decision { return d.shadowOnly }

// Decide runs the pipeline: the boot, the request, the caches, the key's
// and workspace's facts, the policy, the evaluator, the identity and the
// compose, in §3's order (the boot first: the edge reads nothing else of a
// request whose boot fails), and the decision gates last.
func Decide(in DecideInputs) Decided {
	var out Decided
	// windowGate is the policy mode's reason for a windowed key it does not
	// cover, once the key's facts are read: from there on it is the reason
	// any gate passes with, as the windows' place in the key's fact order
	// made it before they joined the cohort.
	var windowGate string
	pass := func(reason string) Decided {
		if windowGate != "" {
			reason = windowGate
		}
		out.Pass = reason
		return out
	}
	h := headersOf(in.Request.Header)
	if v := h.SettlementMode; len(v) == 1 && v[0] == gwauth.SettlementModeAsyncV1 {
		out.AsyncPop = AsyncUnparsed
	}
	if q := in.Request; q.Method != http.MethodPost || !authorizeRoutes[q.Path] || q.EscapedPath != q.Path {
		return pass(PassRequestPath)
	}
	if reason := bootPass(in.Request.Boot); reason != "" {
		return pass(reason)
	}
	r, p := gwauth.ParseRequest(in.Request.Body, h)
	if r.Async.V1 {
		// The body parsed whole (a cohort reason may still pass it).
		out.AsyncPop = AsyncPlain
		if r.CacheAffinity {
			out.AsyncPop = AsyncAffinity
		}
	}
	if p.Passed() {
		if p.Reason == gwauth.ReasonStream {
			out.StreamStageD = streamStageD(in.Policy, in.Request.Boot, r)
		}
		return pass(p.Reason)
	}
	at := in.Request.At
	out.LookupHash = r.APIKeyLookupHash
	policy, snap := in.Policy, in.Snapshot
	if policy == nil || at.Unix()+policySkew < policy.IssuedAt || at.Unix() >= policy.NotAfter {
		return pass(PassCachePolicy)
	}
	if snap == nil {
		return pass(PassCacheSnapshot)
	}
	// The policy's gates a key is not needed for, before the cache is read:
	// with the pilot off, or this region or no workspace in it, the key is
	// never looked up (and never filled and refreshed). The workspace's own
	// membership needs the key (below, policyPass).
	switch {
	case !policy.FastpathPilotEnabled:
		return pass(PassPilotOff)
	case len(policy.FastpathPilotWorkspaces) == 0:
		return pass(PassNotPilotWorkspace)
	case !slices.Contains(policy.FastpathPilotRegions, policy.Plane.Region):
		return pass(PassPilotRegion)
	}
	if reason := asyncPolicyPass(r, policy, snap); reason != "" {
		return pass(reason)
	}
	if in.Lookup == nil || r.APIKeyLookupHash == "" {
		return pass(PassCacheKey)
	}
	read := in.Lookup(r.APIKeyLookupHash, at)
	out.Read = read
	if read.Key != gwstate.Fresh {
		return pass(PassCacheKey)
	}
	if !read.Found {
		return pass(PassKeyMissing)
	}
	key, ws := read.Facts.Key, read.Facts.Workspace
	out.Workspace = key.WorkspaceID
	if reason := asyncAdmissionPass(r, policy, key.WorkspaceID); reason != "" {
		return pass(reason)
	}
	eligible, reason := key.At(at)
	if w := key.Windows; w != nil && (eligible || reason == gwstate.ReasonKeyBudgetStrict) {
		windowGate = keyWindowsGate(policy.KeyWindowsMode(), *w)
	}
	if !eligible {
		return pass(keyPass(reason))
	}
	if !ws.Eligible {
		return pass(workspacePass(ws))
	}
	if read.Credit != gwstate.Fresh {
		return pass(PassCacheWorkspace)
	}
	if reason := creditPass(read); reason != "" {
		return pass(reason)
	}
	if read.Facts.BroadcastDestination {
		return pass(PassWorkspaceBroadcast)
	}
	// Every fact above is what Usable reads; it is the cache's own word that
	// the entry is fit to decide on, so a gate above that drifts from it
	// cannot let a request through.
	if !read.Usable() {
		return pass(PassCacheKey)
	}
	keyTags := make([]gwauth.Tag, len(key.Tags))
	for i, t := range key.Tags {
		keyTags[i] = gwauth.Tag(t)
	}
	// The merged map validated whole (§6); Stage writes the same merge.
	if _, p := gwauth.MergeTags(keyTags, true, r); p.Passed() {
		return pass(p.Reason)
	}

	if reason := policyPass(policy, snap, key, r, in.Request.Boot.Boot); reason != "" {
		return pass(reason)
	}
	region := gwauth.ChooseRegion(policy.ConfiguredRegions, policy.PrimaryRegion, r.RequestedRegion)

	// The requested models first: the evaluator passes a private proxy's
	// decision chain and refuses a model its catalog lacks, where Python
	// answers either, so these are named before it answers (and the
	// candidates' models are checked again after).
	requested := append([]string{r.Model}, r.Models...)
	if snap.PrivateProxy(requested...) {
		return pass(PassCandidatesPrivate)
	}
	for _, m := range requested {
		if slices.Contains(EstimatedUsageModels, m) {
			return pass(PassCandidatesEstimated)
		}
	}

	req := r.Routing
	req.At = at
	req.BYOKProviders = snap.BYOKProviders(read.Facts.BYOKProviders)
	res := snap.Evaluate(req)
	switch res.Outcome {
	case routingsnapshot.Refused:
		return pass(PassEvaluateRefused)
	case routingsnapshot.PassToPython:
		switch res.Reason {
		case evaluatorWindowReason:
			return pass(PassClockWindow)
		case evaluatorEstimateOverflow:
			return pass(PassEstimateOverflow)
		}
		return pass(PassEvaluate)
	}
	if !snap.CreditsOnly(res.Candidates) {
		return pass(PassCandidatesBYOK)
	}
	candidates := make([]gwauth.Candidate, len(res.Candidates))
	for i, id := range res.Candidates {
		e, ok := snap.Endpoint(id)
		if !ok {
			return pass(PassEvaluate)
		}
		if slices.Contains(EstimatedUsageModels, e.ModelID) {
			return pass(PassCandidatesEstimated)
		}
		candidates[i] = gwauth.CandidateOf(e)
	}
	if res.ServiceTier != "" {
		return pass(gwauth.ReasonServiceTier)
	}

	identity, p := gwauth.IdentityOf(r, h, key.WorkspaceID, key.Hash, in.Mint.idempotencyKey())
	if p.Passed() {
		return pass(p.Reason)
	}
	sd := policy.StageD
	compose := gwauth.ComposeInput{
		At:                     at,
		AuthorizationID:        in.Mint.authorization(),
		ReservationID:          in.Mint.reservation(),
		IdempotencyKey:         identity.IdempotencyKey,
		IdempotencyFingerprint: identity.Fingerprint,
		IdempotencyScope:       identity.Scope,
		WorkspaceID:            key.WorkspaceID,
		KeyHash:                key.Hash,
		KeyTags:                composeTags(keyTags),
		RequestedModel:         r.Model,
		RequestTags:            composeTags(r.Tags),
		InvocationNonce:        r.InvocationNonce,
		EstimatedInputTokens:   r.EstimatedInputTokens,
		OutputEstimate:         r.OutputEstimate,
		Region:                 region,
		Regions:                gwauth.RegionsOf(snap.Settings.RegionPayload),
		Candidates:             candidates,
		Estimate:               res.ModelEstimate,
		StageDReason: gwauth.StageDReasonOf(gwauth.StageDSettings{
			// Python ANDs the switch with not budget_strict (gateway.py,
			// _stage_d_eligibility_reason's caller); a budget-strict key
			// passed above (key.budget_strict), so the switch is the whole
			// of it.
			EligibilityEnabled:          sd.EligibilityEnabled,
			HeartbeatEnabled:            sd.HeartbeatEnabled,
			RequireHeartbeatDeclaration: sd.RequireHeartbeatDeclaration, PilotWorkspaces: sd.PilotWorkspaces,
		}, key.WorkspaceID, in.Request.Boot.Boot.DeclaresUsageHeartbeat),
		BootKid: in.Request.Boot.Kid,
	}
	if r.Async.V1 {
		// The additions Python splices in (shape A or B); the policy's
		// object is present (asyncPolicyPass) and its admission does not
		// cover the workspace (asyncAdmissionPass), so Python answers it as
		// with admission off (asyncComposeInput).
		compose.Async = asyncComposeInput(r.Async, *policy.AsyncSettle, key.WorkspaceID)
	}
	staged, err := gwauth.Stage(compose)
	if err != nil {
		return pass(PassCompose)
	}
	decision := &Decision{
		Staged: staged, Input: compose, Identity: identity, Estimate: res.ModelEstimate, Reservation: res.Reservation,
		Workspace: key.WorkspaceID, KeyHash: key.Hash, LookupHash: r.APIKeyLookupHash, Kid: in.Request.Boot.Kid,
		Region: region, Candidates: slices.Clone(res.Candidates),
		Affinity: r.CacheAffinity, Routing: req, Snapshot: snap,
	}
	if w := key.Windows; w != nil && !w.AlertOnly {
		// An alert-only key is decided as an uncapped one: Python's
		// authorize never checks its windows.
		decision.Windows = &WindowCheck{Limits: w.Limits, Shards: w.Shards}
	}
	out.shadowOnly = decision
	// The decision gates, in order: the policy's mode for a windowed key,
	// the decide row, and async-v1's edge_decide.
	out.DecideGate = windowGate
	if out.DecideGate == "" {
		out.DecideGate = in.Decide.gate(key.WorkspaceID, policy.Plane.Cloud, region, at)
	}
	if out.DecideGate == "" && r.Async.V1 && !policy.AsyncSettle.EdgeDecide {
		// Composed for the shadow, answered by Python, even for a workspace
		// whose decide row is on (§6.2, the dark sequence).
		out.DecideGate = PassAsyncOff
	}
	out.Pass = out.DecideGate
	return out
}

// asyncPolicyPass is an async-v1 request's keyless policy gate: "" when the
// policy and the snapshot carry what the edge reads to compose Python's
// answer, else PassAsyncUnpublished.
func asyncPolicyPass(r gwauth.Request, p *routingsnapshot.EdgePolicy, s *routingsnapshot.Snapshot) string {
	if r.Async.V1 && (p.AsyncSettle == nil || !s.BillingValidPublished()) {
		return PassAsyncUnpublished
	}
	return ""
}

// asyncAdmissionPass is an async-v1 request's admission gate, once the key's
// workspace is known (asyncPolicyPass passed, so the policy's object is
// present): PassAsyncAdmission when Python's admission covers the workspace
// (a ticket is Python's), else "": admission off, or admission_workspaces
// leaves the workspace out, and Python answers it as with admission off
// (docs/design/fast-admission-e2-g15-async.md section 4.1).
func asyncAdmissionPass(r gwauth.Request, p *routingsnapshot.EdgePolicy, workspace string) string {
	if r.Async.V1 && p.AsyncSettle.Admits(workspace) {
		return PassAsyncAdmission
	}
	return ""
}

// asyncComposeInput is the async input Stage composes for the workspace: the
// policy's object as that workspace sees it, admission on only when it covers
// the workspace (Admits), not a literal false, so that were the admission
// gate skipped, Stage refuses a covered workspace (a ticket is Python's).
func asyncComposeInput(r gwauth.AsyncRequest, p routingsnapshot.AsyncSettle, workspace string) *gwauth.AsyncInput {
	p.AdmissionEnabled = p.Admits(workspace)
	return &gwauth.AsyncInput{Request: r, Policy: p}
}

func composeTags(tags []gwauth.Tag) []gwauth.ComposeTag {
	out := make([]gwauth.ComposeTag, len(tags))
	for i, t := range tags {
		out[i] = gwauth.ComposeTag(t)
	}
	return out
}

// The shadow's Stage D reasons for a stream (Decided.StreamStageD), in
// _stage_d_eligibility_reason's order (gateway.py), as far as the edge
// reads them before the key's facts. A stream reaches them with a verified
// boot (bootPass), so boot_not_accepted cannot occur. StreamPilotSet is a
// stream whose policy names a Stage D pilot set, whose membership needs the
// key's workspace; StreamUnread is one every reason read here lets
// through, whose candidates' usage types and pricing the edge has not read.
const (
	StreamPolicyUnknown       = "policy_unknown"
	StreamStageDDisabled      = "stage_d_disabled"
	StreamPilotSet            = "pilot_set"
	StreamHeartbeatsDisabled  = "heartbeats_disabled"
	StreamHeartbeatUndeclared = "heartbeat_undeclared"
	StreamRoute               = "route"
	StreamServiceTier         = "service_tier"
	StreamUnread              = "unread"
)

// streamStageD is a stream's Stage D reason for the shadow's split
// (docs/design/fast-admission-streams.md section 8.1). A stream joins the
// lease cohort only with a boot that declared heartbeats, whatever
// require_heartbeat_declaration says (section 5.1), so an undeclared boot
// is counted apart either way.
func streamStageD(p *routingsnapshot.EdgePolicy, boot BootSeen, r gwauth.Request) string {
	if p == nil {
		return StreamPolicyUnknown
	}
	sd := p.StageD
	switch {
	case !sd.EligibilityEnabled:
		return StreamStageDDisabled
	case len(sd.PilotWorkspaces) > 0:
		return StreamPilotSet
	case !sd.HeartbeatEnabled:
		return StreamHeartbeatsDisabled
	case !boot.Boot.DeclaresUsageHeartbeat:
		return StreamHeartbeatUndeclared
	}
	if rt := r.Routing.RouteType; rt == nil || (*rt != "chat.completions" && *rt != "responses") {
		return StreamRoute
	}
	if t := r.Routing.ServiceTier; t != nil && (*t == "priority" || *t == "auto") {
		return StreamServiceTier
	}
	return StreamUnread
}

// bootPass is the boot's reason (§3 "Boot"): E1's verdict, an Invalid one
// told apart by the boot it read.
func bootPass(b BootSeen) string {
	switch b.Verdict {
	case Verified:
		if !b.Found || !b.Boot.Verified {
			return PassBootMissing
		}
		return ""
	case Absent:
		return PassBootSignature
	case Unread:
		return PassBootUnread
	case Unchecked:
		// A body past the edge's bound (past Python's 4 MiB too), or a
		// path outside printable ASCII, which request.path passes first.
		return gwauth.ReasonUnparseable
	}
	switch {
	case b.Kid == "":
		return PassBootSignature
	case !b.Found:
		return PassBootMissing
	case !b.Boot.Verified:
		return PassBootUnverified
	}
	return PassBootSignature
}

// headersOf are the headers authorize reads, as Python will see them: a
// header the request names as hop-by-hop is dropped by the proxy.
func headersOf(h http.Header) gwauth.Headers {
	values := func(name string) []string {
		if listedAsHopByHop(h, name) {
			return nil
		}
		return h.Values(name)
	}
	return gwauth.Headers{
		ContentType:           values("Content-Type"),
		IdempotencyKey:        values("Idempotency-Key"),
		SettlementMode:        values("X-TR-Settlement-Mode"),
		VideoAllowedProviders: len(values("X-Quill-Video-Allowed-Providers")) > 0,
	}
}

// keyPass is a key's §3 reason from gwstate's (§8.1 as merged in G3).
func keyPass(reason string) string {
	switch reason {
	case gwstate.ReasonKeyDisabled:
		return PassKeyDisabled
	case gwstate.ReasonKeyExpiresAt, gwstate.ReasonKeyExpired:
		// An expiry Go cannot parse is expired, as is_api_key_expired
		// treats one.
		return PassKeyExpired
	case gwstate.ReasonKeyScope:
		return PassKeyScope
	case gwstate.ReasonKeyFederated:
		return PassKeyFederated
	case gwstate.ReasonKeyTags, gwstate.ReasonKeyTagsNonASCII:
		// Stored tags Go cannot read, or that validate_tags refuses (§6).
		return gwauth.ReasonTagsMergedInvalid
	case gwstate.ReasonKeyApp:
		return PassKeyApp
	case gwstate.ReasonKeyLimit:
		return PassKeyCap
	case gwstate.ReasonKeyWindowLimit:
		// Spend windows Python's authorize enforces (keyWindowsGate).
		return PassKeyWindow
	case gwstate.ReasonKeyWindowAlert:
		// Alert-only windows, which it never checks; counted apart
		// (fast-admission-e2-key-windows.md §8.1).
		return PassKeyWindowAlert
	case gwstate.ReasonKeyBudgetStrict:
		return PassKeyBudgetStrict
	}
	return PassKeyShape
}

// keyWindowsGate is the edge policy's fastpath_pilot_key_windows as a
// decision gate for a key with spend windows (key-windows section 5): "" when
// the mode covers the key (alert covers an alert-only key, enforce every
// one), else its window reason.
func keyWindowsGate(mode string, w gwstate.KeyWindows) string {
	switch {
	case mode == routingsnapshot.KeyWindowsEnforce,
		mode == routingsnapshot.KeyWindowsAlert && w.AlertOnly:
		return ""
	}
	return keyPass(w.Reason())
}

// workspacePass is an ineligible workspace's §3 reason.
func workspacePass(w gwstate.WorkspaceFacts) string {
	switch {
	case w.Reason == gwstate.ReasonWorkspaceGone && w.Deleted:
		return PassWorkspaceDeleted
	case w.Reason == gwstate.ReasonWorkspaceGone:
		return PassWorkspaceMissing
	case w.Reason == gwstate.ReasonWorkspacePaused:
		return PassWorkspacePaused
	case w.Reason == gwstate.ReasonWorkspaceFederal:
		return PassWorkspaceFederated
	}
	return PassWorkspaceShape
}

// creditPass is what the workspace's credit rows say (store.CreditState):
// a pause cause on any row (the entity's are WorkspaceFacts'), debt, a
// latch or a tier below the grant's, or any refusal the grant would make.
func creditPass(r gwstate.Read) string {
	c := r.CreditState
	switch {
	case !r.CreditRows:
		return PassWorkspaceCreditRows
	case c.Paused || c.Refused == store.RefusedPaused:
		return PassWorkspacePaused
	case c.InDebt || c.Refused == store.RefusedInDebt:
		return PassWorkspaceInDebt
	case c.Latched || c.Refused == store.RefusedTier:
		return PassWorkspaceTier
	case c.Refused != "":
		return PassWorkspaceRefused
	}
	return ""
}

// policyPass is §3's policy reasons and decision 8's boot acceptance, with
// decision 19's internal models from the policy.
func policyPass(p *routingsnapshot.EdgePolicy, s *routingsnapshot.Snapshot, key gwstate.KeyFacts, r gwauth.Request,
	boot Boot) string {
	switch {
	case !p.FastpathPilotEnabled:
		return PassPilotOff
	case !slices.Contains(p.FastpathPilotWorkspaces, key.WorkspaceID):
		// Empty is nobody (Python's pilot set), unlike stage_d's.
		return PassNotPilotWorkspace
	case !slices.Contains(p.FastpathPilotRegions, p.Plane.Region):
		return PassPilotRegion
	case !p.RecordsDurable:
		return PassRecordsNotDurable
	case slices.Contains(p.AsyncSettleShadowWorkspaces, key.WorkspaceID):
		return PassAsyncSettleShadow
	case p.MonitorLookupHash != nil && *p.MonitorLookupHash == r.APIKeyLookupHash:
		return PassMonitorKey
	case key.LookupHash != r.APIKeyLookupHash:
		return PassLookupHashMismatch
	case !slices.Contains(p.AcceptedImageDigests, boot.ImageDigest):
		return PassDigestNotAccepted
	case !regionsAgree(p, s.Settings):
		return PassRegionsMismatch
	case r.NamesInternalModel(p.InternalModels):
		return gwauth.ReasonInternalModel
	}
	return ""
}

// regionsAgree: the policy's configured regions, primary and multi-region
// flag imply the snapshot's settings, as regions.region_payload derives
// them (§4): the regions in order, each primary exactly when it is
// choose_region's, enabled when multi-region is on or it is the primary.
// The response writes the snapshot's region_payload, which must be what
// Python would write live.
func regionsAgree(p *routingsnapshot.EdgePolicy, s routingsnapshot.Settings) bool {
	if len(p.ConfiguredRegions) == 0 || !slices.Equal(p.ConfiguredRegions, s.Regions) ||
		p.PrimaryRegion != s.PrimaryRegion || len(s.RegionPayload) != len(s.Regions) {
		return false
	}
	primary := gwauth.ChooseRegion(p.ConfiguredRegions, p.PrimaryRegion, nil)
	for i, r := range s.RegionPayload {
		if r.ID != s.Regions[i] || r.Name != r.ID || r.Primary != (r.ID == primary) ||
			r.Enabled != (p.MultiRegionEnabled || r.ID == primary) {
			return false
		}
	}
	return true
}

// DecideView is a node's copy of tr_fastpath_edge_decide (§9, decision
// 10): the rows on, by workspace, cloud and region, and when the read that
// made it began. Its zero value is a copy never read: stale, so nobody
// decides.
type DecideView struct {
	On     map[DecideKey]bool
	ReadAt time.Time
	// MaxAge is how old the copy may be (three refresh intervals).
	MaxAge time.Duration
}

// DecideKey is a decide row's key.
type DecideKey struct{ Workspace, Cloud, Region string }

// gate is the row's reason for a request at now: "" when the row is on,
// PassCacheDecide when the copy is older than MaxAge (or was never read),
// else PassDecideOff.
func (v DecideView) gate(workspace, cloud, region string, now time.Time) string {
	if v.ReadAt.IsZero() || now.Sub(v.ReadAt) > v.MaxAge || now.Before(v.ReadAt.Add(-time.Second)) {
		return PassCacheDecide
	}
	if !v.On[DecideKey{workspace, cloud, region}] {
		return PassDecideOff
	}
	return ""
}

// The decide copy's numbers (decision 10): read every 5 s, all off when
// the copy is older than three intervals.
const (
	DecideEvery     = 5 * time.Second
	decideStaleness = 3
)

// DecideReader reads the decide table (store.Store.ReadDecide).
type DecideReader interface {
	ReadDecide(ctx context.Context) ([]store.DecideRow, time.Time, error)
}

// decideCopy keeps the node's copy of the decide table, read every
// interval; a read that fails keeps the last copy, which ages out.
type decideCopy struct {
	reader  DecideReader
	every   time.Duration
	clock   func() time.Time
	logf    func(string, ...any)
	current atomic.Pointer[DecideView]
	reads   atomic.Int64
	failed  atomic.Int64
}

func newDecideCopy(reader DecideReader, every time.Duration, clock func() time.Time,
	logf func(string, ...any)) *decideCopy {
	if every <= 0 {
		every = DecideEvery
	}
	return &decideCopy{reader: reader, every: every, clock: clock, logf: logf}
}

// view is the copy now; zero (stale) before the first read.
func (d *decideCopy) view() DecideView {
	if v := d.current.Load(); v != nil {
		return *v
	}
	return DecideView{MaxAge: decideStaleness * d.every}
}

// run reads the table at once and then every interval, until ctx ends.
func (d *decideCopy) run(ctx context.Context) {
	tick := time.NewTicker(d.every)
	defer tick.Stop()
	failing := false
	for {
		at := d.clock()
		read, cancel := context.WithTimeout(ctx, d.every)
		rows, _, err := d.reader.ReadDecide(read)
		cancel()
		d.reads.Add(1)
		switch {
		case err != nil && ctx.Err() == nil:
			d.failed.Add(1)
			if !failing {
				d.logf("fastpath edge decide copy: read failed; the copy held ages out after %v",
					decideStaleness*d.every)
			}
			failing = true
		case err == nil:
			on := map[DecideKey]bool{}
			for _, row := range rows {
				if row.Decide {
					on[DecideKey{strings.Clone(row.Workspace), row.Cloud, row.Region}] = true
				}
			}
			d.current.Store(&DecideView{On: on, ReadAt: at, MaxAge: decideStaleness * d.every})
			failing = false
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}
