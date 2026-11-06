package gwauth

// The async-v1 additions (docs/design/fast-admission-e2-g15-async.md, §2):
// what Python's authorize_additions (services/async_settle.py) splices into
// the 200 between stage_d and tags when the X-TR-Settlement-Mode header
// list is exactly ["async-v1"], for the answers the edge can give with
// admission off. Shape A is {"async_eligible": false}; shape B adds the
// frozen billing snapshot (billing_snapshot.py, build_snapshot) and its
// hash. Shape C, a signed ticket, is Python's alone: Stage refuses an input
// whose policy says admission is on, and the caller passes.
//
// Every branch is §2.3's, in Python's order, so a shape A's reason is the
// first check Python's own code would fail. Nothing here reads the clock,
// the network or a remembered session: the candidates are the compose's,
// priced at the compose's instant from their snapshot epochs.

import (
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"slices"
	"strings"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// SettlementModeAsyncV1 is the one header value Python's async path takes.
const SettlementModeAsyncV1 = "async-v1"

// exactlyAsyncV1 is request.headers.getlist("X-TR-Settlement-Mode") ==
// ["async-v1"]: one value, that case, nothing around it.
func exactlyAsyncV1(values []string) bool {
	return len(values) == 1 && values[0] == SettlementModeAsyncV1
}

// AsyncRequest is what authorize_additions reads of the request (§3.1):
// the header, and the body as the schema holds it.
type AsyncRequest struct {
	// V1 is whether the header list is exactly ["async-v1"].
	V1 bool
	// Undeclared is body.model_extra non-empty: any top-level member the
	// schema does not declare, one whose value is null included.
	Undeclared bool
	// InputModalities is "input_modalities" in body.model_fields_set:
	// present, null included.
	InputModalities bool
	// RouteType and ServiceTier are the stripped strings; nil when absent
	// or null.
	RouteType, ServiceTier *string
	// RequestedParameters are the stripped entries; nil when absent or null.
	RequestedParameters []string
	Model               string // body.model, stripped
	// InferenceReceipt is body.inference_receipt; AdditionalCost is
	// additional_cost_reservation_microdollars nonzero (the receipt_fee and
	// tool_cost exclusions; the cohort passes both before the compose).
	InferenceReceipt, AdditionalCost bool
}

// AsyncInput is everything the additions read beyond the candidates: the
// request's facts and the edge policy's async_settle object.
type AsyncInput struct {
	Request AsyncRequest
	Policy  routingsnapshot.AsyncSettle
}

// The shapes, as the shadow counts them (§4.4): nothing added, shape A
// (false_only, with its reason) and shape B (snapshot).
const (
	AsyncNone      = "none"
	AsyncFalseOnly = "false_only"
	AsyncSnapshot  = "snapshot"
)

// Shape A's reasons, each the first of §2.3's checks that fails, named as
// Python's branch or exclusion is.
const (
	AsyncReasonUndeclared         = "undeclared_member"
	AsyncReasonRouteType          = "route_type"
	AsyncReasonInputModalities    = "input_modalities"
	AsyncReasonRequestedParameter = "requested_parameter"
	AsyncReasonUntyped            = "untyped"
	AsyncReasonServiceTier        = "service_tier"
	AsyncReasonReceiptFee         = "receipt_fee"
	AsyncReasonToolCost           = "tool_cost"
	AsyncReasonPartner            = "partner"
	AsyncReasonBillingInvalid     = "billing_invalid"   // billing_valid false at the instant
	AsyncReasonCandidateRefused   = "candidate_refused" // Candidate's validation
	AsyncReasonCandidateSet       = "candidate_set"     // none, more than 64, or twice
	AsyncReasonTicketAuthority    = "ticket_authority"
)

// Async is the additions Stage spliced into the 200: the shape, shape A's
// reason, and the members in Python's order.
type Async struct {
	Shape   string
	Reason  string
	Members pyjson.Object
}

// ordinaryParameters is async_settle.ORDINARY_PARAMETERS: the requested
// parameters an async-v1 request may name.
var ordinaryParameters = map[string]bool{
	"temperature": true, "top_p": true, "max_tokens": true, "max_completion_tokens": true,
	"max_output_tokens": true, "stop": true, "seed": true, "frequency_penalty": true,
	"presence_penalty": true, "logit_bias": true, "logprobs": true, "top_logprobs": true,
	"response_format": true, "reasoning_effort": true, "stream": true,
}

// identity is billing_snapshot.Identity: 1 to 512 characters of this set.
var identity = regexp.MustCompile(`^[A-Za-z0-9_./:@+\-]{1,512}$`)

// The DTO's literals and bounds (billing_snapshot.py, Candidate and
// BillingSnapshot).
const (
	maxBillingTiers      = 64
	maxBillingCandidates = 64
	billingRounding      = "half_up_per_million"
)

var asyncFalse = pyjson.Object{{Key: "async_eligible", Value: false}}

func falseOnly(reason string) Async {
	return Async{Shape: AsyncFalseOnly, Reason: reason, Members: asyncFalse}
}

// asyncAdditions is authorize_additions for a non-replay authorization the
// edge composed: settlement "local", unsettled, with a reservation (§2.3's
// authorization guards always hold), Credits, no markup, fee, custom or
// user model or native batch, priced at at (unix seconds). An error is an
// input Go cannot answer as Python would (an unpublished billing_valid,
// admission on, an instant before every epoch): Stage refuses and the
// caller passes.
func asyncAdditions(in *AsyncInput, candidates []Candidate, at int64) (Async, error) {
	if in == nil || !in.Request.V1 {
		return Async{Shape: AsyncNone}, nil
	}
	if in.Policy.AdmissionEnabled {
		return Async{}, composeErr("async admission is on: the ticket is Python's")
	}
	r := in.Request
	switch {
	case r.Undeclared:
		return falseOnly(AsyncReasonUndeclared), nil
	case r.RouteType == nil || (*r.RouteType != "chat.completions" && *r.RouteType != "responses"):
		return falseOnly(AsyncReasonRouteType), nil
	case r.InputModalities:
		return falseOnly(AsyncReasonInputModalities), nil
	}
	for _, p := range r.RequestedParameters {
		if !ordinaryParameters[p] {
			return falseOnly(AsyncReasonRequestedParameter), nil
		}
	}
	// require_eligible(Eligibility(...)): exclusion's order. usage_type,
	// authority, markups, custom and user models and native batch are the
	// cohort's constants; the route type was checked above.
	switch {
	case !in.Policy.TypedRecords:
		return falseOnly(AsyncReasonUntyped), nil
	case r.ServiceTier != nil && *r.ServiceTier != "default":
		return falseOnly(AsyncReasonServiceTier), nil
	case r.InferenceReceipt:
		return falseOnly(AsyncReasonReceiptFee), nil
	case r.AdditionalCost:
		return falseOnly(AsyncReasonToolCost), nil
	case !strings.HasPrefix(r.Model, "openai/") && !strings.HasPrefix(r.Model, "anthropic/"):
		return falseOnly(AsyncReasonPartner), nil
	}
	snapshot, reason, err := billingSnapshot(candidates, at)
	if err != nil {
		return Async{}, err
	}
	if reason != "" {
		return falseOnly(reason), nil
	}
	// snapshot_projection's runtime guard: signer, epoch >= 1 and a journal
	// region, which the policy publishes as one fact.
	if !in.Policy.TicketAuthority {
		return falseOnly(AsyncReasonTicketAuthority), nil
	}
	canonical, err := pyjson.Canonical(snapshot)
	if err != nil {
		return Async{}, composeErr("the billing snapshot: %v", err)
	}
	sum := sha256.Sum256(canonical)
	return Async{Shape: AsyncSnapshot, Members: pyjson.Object{
		{Key: "async_eligible", Value: false},
		{Key: "billing_snapshot", Value: snapshot},
		{Key: "billing_snapshot_hash", Value: hex.EncodeToString(sum[:])},
	}}, nil
}

// epochAt is the candidate's epoch at at: the last whose start is at or
// before it. ok is false when none is.
func epochAt(epochs []routingsnapshot.Epoch, at int64) (routingsnapshot.Epoch, bool) {
	var current routingsnapshot.Epoch
	ok := false
	for _, e := range epochs {
		if e.StartsAt <= at {
			current, ok = e, true
		}
	}
	return current, ok
}

// billingSnapshot is build_snapshot over the candidates' prices at at, as
// BillingSnapshot.model_dump(mode="json") holds it (declaration order).
// A non-empty reason is shape A's; an error, an input Go cannot answer.
func billingSnapshot(candidates []Candidate, at int64) (pyjson.Object, string, error) {
	type built struct {
		id     string
		object pyjson.Object
	}
	out := make([]built, 0, len(candidates))
	for _, c := range candidates {
		epoch, ok := epochAt(c.Epochs, at)
		if !ok {
			return nil, "", composeErr("candidate %q has no price epoch at %d", c.EndpointID, at)
		}
		// _require_raw_prices, as the snapshot publishes it.
		if epoch.BillingValid == nil {
			return nil, "", composeErr("candidate %q: billing_valid is unpublished", c.EndpointID)
		}
		if !*epoch.BillingValid {
			return nil, AsyncReasonBillingInvalid, nil
		}
		p := epoch.Price
		if p.EndpointID != c.EndpointID {
			return nil, "", composeErr("candidate %q is priced as %q", c.EndpointID, p.EndpointID)
		}
		if !billingCandidateValid(c, p) {
			return nil, AsyncReasonCandidateRefused, nil
		}
		out = append(out, built{p.EndpointID, billingCandidate(c, p)})
	}
	slices.SortFunc(out, func(a, b built) int { return strings.Compare(a.id, b.id) })
	if len(out) == 0 || len(out) > maxBillingCandidates {
		return nil, AsyncReasonCandidateSet, nil
	}
	list := make([]any, len(out))
	for i, b := range out {
		if i > 0 && out[i-1].id == b.id {
			return nil, AsyncReasonCandidateSet, nil
		}
		list[i] = b.object
	}
	return pyjson.Object{
		{Key: "v", Value: int64(1)},
		{Key: "kind", Value: "credits_endpoint"},
		{Key: "candidates", Value: list},
		{Key: "minimum_charge", Value: "one_micro_if_positive"},
		{Key: "charge_cap", Value: nil},
		{Key: "tier_basis", Value: "total_prompt"},
		{Key: "tier_boundary", Value: "inclusive"},
		{Key: "tier_fallback", Value: "last_tier"},
	}, "", nil
}

// billingCandidateValid is Candidate's validation (billing_snapshot.py):
// the field types, literals and bounds, then supported(). Integers are
// int64 already; each must also be non-negative (UInt).
func billingCandidateValid(c Candidate, p routingsnapshot.Price) bool {
	if !identity.MatchString(p.EndpointID) || (c.Provider != "openai" && c.Provider != "anthropic") ||
		!identity.MatchString(c.Model) || c.UsageType != usageCredits || p.PriceHistoryVersion != 1 ||
		!ratesValid(p.Rates) || len(p.Tiers) > maxBillingTiers || p.RequestFeeMicro < 0 ||
		p.Rounding != billingRounding {
		return false
	}
	for _, t := range p.Tiers {
		if (t.MaxPromptTokens != nil && *t.MaxPromptTokens < 0) || !ratesValid(t.Rates) {
			return false
		}
	}
	// supported(): no request fee, a model under its provider, tiers
	// increasing with an unbounded tier only last. The prompt convention
	// follows from the provider, so it always agrees.
	if p.RequestFeeMicro != 0 || !strings.HasPrefix(c.Model, c.Provider+"/") {
		return false
	}
	previous := int64(-1)
	for i, t := range p.Tiers {
		switch {
		case t.MaxPromptTokens == nil:
			if i != len(p.Tiers)-1 {
				return false
			}
		case *t.MaxPromptTokens <= previous:
			return false
		default:
			previous = *t.MaxPromptTokens
		}
	}
	return true
}

func ratesValid(r routingsnapshot.Rates) bool {
	return r.InputMicroPerMillion >= 0 && r.CachedInputMicroPerMillion >= 0 &&
		r.CacheCreationMicroPerMillion >= 0 && r.OutputMicroPerMillion >= 0
}

// billingCandidate is one Candidate in its declaration order.
func billingCandidate(c Candidate, p routingsnapshot.Price) pyjson.Object {
	tiers := make([]any, len(p.Tiers))
	for i, t := range p.Tiers {
		var bound any
		if t.MaxPromptTokens != nil {
			bound = *t.MaxPromptTokens
		}
		tiers[i] = pyjson.Object{{Key: "max_prompt_tokens", Value: bound}, {Key: "rates", Value: ratesObject(t.Rates)}}
	}
	convention := "includes_cache"
	if c.Provider == "anthropic" {
		convention = "excludes_cache"
	}
	return pyjson.Object{
		{Key: "endpoint_id", Value: p.EndpointID},
		{Key: "provider", Value: c.Provider},
		{Key: "model_id", Value: c.Model},
		{Key: "usage_type", Value: c.UsageType},
		{Key: "price_history_version", Value: p.PriceHistoryVersion},
		{Key: "rates", Value: ratesObject(p.Rates)},
		{Key: "tiers", Value: tiers},
		{Key: "request_fee_micro", Value: p.RequestFeeMicro},
		{Key: "rounding", Value: p.Rounding},
		{Key: "prompt_convention", Value: convention},
		{Key: "output_convention", Value: "includes_reasoning"},
	}
}

// ratesObject is Rates in its declaration order.
func ratesObject(r routingsnapshot.Rates) pyjson.Object {
	return pyjson.Object{
		{Key: "input_micro_per_million", Value: r.InputMicroPerMillion},
		{Key: "cached_input_micro_per_million", Value: r.CachedInputMicroPerMillion},
		{Key: "cache_creation_micro_per_million", Value: r.CacheCreationMicroPerMillion},
		{Key: "output_micro_per_million", Value: r.OutputMicroPerMillion},
	}
}
