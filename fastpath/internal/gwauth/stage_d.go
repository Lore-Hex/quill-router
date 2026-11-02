package gwauth

// Stage D for a stream the edge composes (docs/design/fast-admission-streams.md
// sections 5 and 8): the reason Python's _stage_d_eligibility_reason gives
// (gateway.py), and, for a stream it lets in ("ok"), the frozen pricing
// document Python writes into the row (stage_d.endpoint_pricing_document,
// canonical_pricing_snapshot) and answers in the 200
// (_gateway_stage_d_payload).

import (
	"slices"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// StageDOK is the reason of a stream Stage D lets in: the enclave
// heartbeats it, and the row carries its pricing snapshot.
const StageDOK = "ok"

// The pricing document's constants (stage_d.py).
const (
	pricingDocumentVersion = int64(1)
	priceHistoryVersion    = int64(1)
	pricingRounding        = "half_up_per_million"
)

// StageDRequest is what _stage_d_eligibility_reason reads of the request,
// its candidates and the boot, past the policy's settings.
type StageDRequest struct {
	WorkspaceID           string
	BootAccepted          bool // boot_context["boot_verified"]
	BootDeclaresHeartbeat bool // the boot's declares_usage_heartbeat
	Stream                bool // body.stream is true
	// RouteType is body.route_type, nil when absent (Python's None, which
	// is not a streaming route).
	RouteType *string
	// CreditsOnly: every candidate's usage type is Credits.
	CreditsOnly bool
	// StandardPricing: no custom or user model, no partner mode, no
	// additional cost and not native batch.
	StandardPricing bool
	ServiceTier     string // normalized, "" for none
	// TypedSettlement: a typed billing store writing typed records, which
	// the edge policy's async_settle.typed_records says.
	TypedSettlement bool
}

// StageDReasonOf is _stage_d_eligibility_reason (gateway.py), in its order,
// with the policy's settings (§4's stage_d object) and the request's facts.
// The edge reaches it only with an accepted boot and, for a stream past the
// request cohort, with credits-only candidates, standard pricing and no
// service tier, so those reasons are Python's here for completeness, and a
// request that is not a stream stops at not_streaming.
func StageDReasonOf(s StageDSettings, q StageDRequest) string {
	switch {
	case !s.EligibilityEnabled:
		return "stage_d_disabled"
	case len(s.PilotWorkspaces) > 0 && !slices.Contains(s.PilotWorkspaces, q.WorkspaceID):
		return "workspace_not_pilot"
	case !s.HeartbeatEnabled:
		return "heartbeats_disabled"
	case !q.BootAccepted:
		return "boot_not_accepted"
	case s.RequireHeartbeatDeclaration && !q.BootDeclaresHeartbeat:
		return "heartbeat_undeclared"
	case !q.Stream:
		return "not_streaming"
	case q.RouteType == nil || (strip(*q.RouteType) != "chat.completions" && strip(*q.RouteType) != "responses"):
		return "route"
	case !q.CreditsOnly:
		return "mixed_usage_type"
	case !q.StandardPricing:
		return "pricing_kind"
	case q.ServiceTier == "priority" || q.ServiceTier == "auto":
		return "service_tier"
	case !q.TypedSettlement:
		return "settlement_backend"
	default:
		return StageDOK
	}
}

// pricingSnapshot is a stream's frozen pricing for its candidates at the
// instant's unix seconds (the price epoch the estimate was priced at): the
// canonical text Python writes into the row's pricing_snapshot, and the
// candidates the 200's candidate_prices answers, which Python reads back
// from that text, so in its sorted key order. Every candidate must hold
// billing_pricing_snapshot's promise (one entry an endpoint, integers in
// int64 and not negative, version 1, half-up rounding), which the 200's
// candidate_cost_reporting then says; an error passes the request.
func pricingSnapshot(candidates []Candidate, at int64) (string, []any, error) {
	out := make([]any, len(candidates))
	seen := make(map[string]bool, len(candidates))
	for i, c := range candidates {
		epoch, ok := epochAt(c.Epochs, at)
		if !ok {
			return "", nil, composeErr("candidate %q has no price epoch at %d", c.EndpointID, at)
		}
		p := epoch.Price
		if p.EndpointID != c.EndpointID || seen[p.EndpointID] {
			return "", nil, composeErr("candidate %q's price names %q, or names it twice", c.EndpointID, p.EndpointID)
		}
		seen[p.EndpointID] = true
		if !pricingCandidateValid(p) {
			return "", nil, composeErr("candidate %q's price breaks the snapshot's promise", c.EndpointID)
		}
		out[i] = pricingCandidate(p)
	}
	document := pyjson.Object{
		{Key: "candidates", Value: out},
		{Key: "kind", Value: "endpoint"},
		{Key: "v", Value: pricingDocumentVersion},
	}
	canonical, err := pyjson.Canonical(document)
	if err != nil {
		return "", nil, composeErr("the pricing snapshot: %v", err)
	}
	return string(canonical), out, nil
}

// pricingCandidateValid is billing_pricing_snapshot's check of one
// candidate (stage_d.py): version and rounding, and every bound, fee and
// rate an integer from 0 to 2^63-1 (Go's int64 holds no more).
func pricingCandidateValid(p routingsnapshot.Price) bool {
	if p.PriceHistoryVersion != priceHistoryVersion || p.Rounding != pricingRounding || p.RequestFeeMicro < 0 ||
		!ratesValid(p.Rates) {
		return false
	}
	for _, t := range p.Tiers {
		if (t.MaxPromptTokens != nil && *t.MaxPromptTokens < 0) || !ratesValid(t.Rates) {
			return false
		}
	}
	return true
}

// pricingCandidate is endpoint_pricing_candidate's dict as
// parse_pricing_snapshot reads it back from the canonical text: its keys
// sorted.
func pricingCandidate(p routingsnapshot.Price) pyjson.Object {
	tiers := make([]any, len(p.Tiers))
	for i, t := range p.Tiers {
		var bound any
		if t.MaxPromptTokens != nil {
			bound = *t.MaxPromptTokens
		}
		tiers[i] = pyjson.Object{{Key: "max_prompt_tokens", Value: bound}, {Key: "rates", Value: sortedRates(t.Rates)}}
	}
	return pyjson.Object{
		{Key: "endpoint_id", Value: p.EndpointID},
		{Key: "price_history_version", Value: p.PriceHistoryVersion},
		{Key: "rates", Value: sortedRates(p.Rates)},
		{Key: "request_fee_micro", Value: p.RequestFeeMicro},
		{Key: "rounding", Value: p.Rounding},
		{Key: "tiers", Value: tiers},
	}
}

// sortedRates is Rates with its keys sorted.
func sortedRates(r routingsnapshot.Rates) pyjson.Object {
	return pyjson.Object{
		{Key: "cache_creation_micro_per_million", Value: r.CacheCreationMicroPerMillion},
		{Key: "cached_input_micro_per_million", Value: r.CachedInputMicroPerMillion},
		{Key: "input_micro_per_million", Value: r.InputMicroPerMillion},
		{Key: "output_micro_per_million", Value: r.OutputMicroPerMillion},
	}
}
