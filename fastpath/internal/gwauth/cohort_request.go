package gwauth

// The request half of the cohort (docs/design/fast-admission-e2-decide.md,
// §3 "Request"): what the body and headers alone send to Python, with no
// I/O. Each reason is §3's name, counted by the shadow and the watch.

import (
	"slices"
	"strings"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// §3's request reasons, in its table's order.
const (
	ReasonContentType     = "body.content_type"
	ReasonUnparseable     = "body.unparseable"
	ReasonDuplicateKey    = "body.duplicate_key"
	ReasonFloat           = "body.float"
	ReasonLaxType         = "body.lax_type"
	ReasonStripAmbiguous  = "body.strip_ambiguous"
	ReasonDictKeyStrip    = "body.dict_key_strip"
	ReasonRefused         = "body.refused"
	ReasonAPIKeyHash      = "body.api_key_hash"
	ReasonStream          = "body.stream"
	ReasonServiceTier     = "body.service_tier"
	ReasonPricedExtra     = "body.priced_extra"
	ReasonCanonicalModel  = "body.canonical_model"
	ReasonCustomModel     = "body.custom_model"
	ReasonInternalModel   = "body.internal_model"
	ReasonTags            = "body.tags"
	ReasonReferer         = "body.referer"
	ReasonEvaluatorInput  = "body.evaluator_input"
	ReasonIdempotencyKey  = "header.idempotency_key"
	ReasonSettlementAsync = "header.settlement_async"
	ReasonVideoProviders  = "header.video_providers"

	// ReasonTagsMergedInvalid is §3's facts reason for the merged tag map
	// (MergeTags), which needs the key's stored tags.
	ReasonTagsMergedInvalid = "tags.merged_invalid"
)

// InternalModels is Go's hardcoded set of internal models (decision 19):
// MONITOR_MODEL_ID and every internal_only catalog model. A request naming
// one passes; the edge policy's internal_models adds any a newer Python
// knows (NamesInternalModel). A test holds this equal to P1's header.
var InternalModels = []string{"trustedrouter/fortanix-test", "trustedrouter/monitor"}

// Idempotency-key prefixes whose requests Python fingerprints or bills
// another way: native batch (gateway.py, _is_native_batch_idempotency_key)
// and partner billing (partner_billing.py).
const (
	nativeBatchKeyPrefix = "tr-native-batch:"
	partnerKeyPrefix     = "partner:"
	maxIdempotencyChars  = 256
)

// chatArmRouteTypes are the route types whose fingerprint is the plain one
// and whose candidates the evaluator answers (routingsnapshot's chat arm);
// any other (videos, images, audio, fetch, native batch, ...) passes.
var chatArmRouteTypes = map[string]bool{"chat.completions": true, "responses": true, "messages": true,
	"embeddings": true, "decide": true}

// cohort applies §3's request reasons after body.refused, in order. object
// is the decoded body; r is its parse. With streams a stream does not pass
// as body.stream (ParseRequestStreams).
func (r *Request) cohort(object pyjson.Object, h Headers, streams bool) Pass {
	values := make(map[string]any, len(object))
	for _, m := range object {
		values[m.Key] = m.Value
	}
	present := func(name string) bool { return values[name] != nil }
	switch {
	case present("api_key_hash"):
		return passFor(ReasonAPIKeyHash)
	case values["stream"] == true && !streams:
		return passFor(ReasonStream)
	case present("service_tier"):
		return passFor(ReasonServiceTier)
	case r.additionalCost || r.inferenceReceipt || present("video_resolution") ||
		present("speech_input_characters") || present("transcription_duration_ms"):
		return passFor(ReasonPricedExtra)
	}
	requested := append([]string{r.Model}, r.Models...)
	for _, m := range requested {
		if canonicalRouterModelID(m) != m {
			return passFor(ReasonCanonicalModel)
		}
	}
	for _, m := range requested {
		lower := strings.ToLower(m)
		// is_creator_model_id, and the partner model, whose billing mode
		// the evaluator does not reproduce.
		if strings.HasPrefix(lower, "tr-custom-model/") || strings.HasPrefix(lower, "tr-user-model/") ||
			strings.Contains(lower, "parasail/liberty-2.0") {
			return passFor(ReasonCustomModel)
		}
	}
	if r.NamesInternalModel(nil) {
		return passFor(ReasonInternalModel)
	}
	// body.tags and body.referer were decided while parsing.
	if rt := r.Routing.RouteType; rt != nil && !chatArmRouteTypes[strip(*rt)] {
		return passFor(ReasonEvaluatorInput)
	}
	if p := r.idempotencyHeaders(h); p.Passed() {
		return p
	}
	if len(h.SettlementMode) > 0 && !exactlyAsyncV1(h.SettlementMode) {
		// Python takes the async path only for exactly ["async-v1"], which
		// the edge composes (async.go, G15c); any other present value passes
		// rather than be told apart from it.
		return passFor(ReasonSettlementAsync)
	}
	if h.VideoAllowedProviders {
		return passFor(ReasonVideoProviders)
	}
	return Pass{}
}

// NamesInternalModel reports a model or models entry in InternalModels or
// in policy (the edge policy's internal_models). The ids are stripped and
// identity under canonical_router_model_id already.
func (r Request) NamesInternalModel(policy []string) bool {
	for _, m := range append([]string{r.Model}, r.Models...) {
		if slices.Contains(InternalModels, m) || slices.Contains(policy, m) {
			return true
		}
	}
	return false
}

// idempotencyHeaders is header.idempotency_key: the body's key (stripped
// and bounded by the schema already) or else the first Idempotency-Key
// header (gateway.py, _gateway_idempotency_key). Go reads the header only
// when it is sent once with every byte printable ASCII (Starlette decodes
// latin-1, and str.strip strips Unicode whitespace); either presented key
// over 256 characters, or with a native-batch or partner prefix, passes.
func (r Request) idempotencyHeaders(h Headers) Pass {
	if len(h.IdempotencyKey) > 1 {
		return passFor(ReasonIdempotencyKey)
	}
	var candidates []string
	if r.BodyIdempotencyKey != nil {
		candidates = append(candidates, *r.BodyIdempotencyKey)
	}
	if len(h.IdempotencyKey) == 1 && !printableASCII(h.IdempotencyKey[0]) {
		return passFor(ReasonIdempotencyKey)
	}
	if key := headerKey(h); key != nil {
		candidates = append(candidates, *key)
	}
	for _, key := range candidates {
		if len(key) > maxIdempotencyChars || strings.HasPrefix(key, nativeBatchKeyPrefix) ||
			strings.HasPrefix(key, partnerKeyPrefix) {
			return passFor(ReasonIdempotencyKey)
		}
	}
	return Pass{}
}

// headerKey is the one Idempotency-Key header stripped, nil when none was
// sent or it is empty once stripped. Only a header idempotencyHeaders
// accepted is read.
func headerKey(h Headers) *string {
	if len(h.IdempotencyKey) != 1 {
		return nil
	}
	if key := strip(h.IdempotencyKey[0]); key != "" {
		return &key
	}
	return nil
}

// canonicalRouterModelID is model_aliases.canonical_router_model_id. A
// requested id it changes passes (body.canonical_model); Go never answers
// under a rewritten id.
func canonicalRouterModelID(id string) string {
	if alias, ok := routerModelIDAliases[id]; ok {
		id = alias
	}
	publisher, nativeID, separator := strings.Cut(id, "/")
	if separator && (publisher == "system1models" || publisher == "system1models-eu") {
		return strings.Replace(publisher, "system1models", "decisionmodelsio", 1) + "/" + nativeID
	}
	if strings.HasPrefix(id, "nyte/") {
		id = "trustedrouter/" + strings.TrimPrefix(id, "nyte/")
	}
	base, variant, hasVariant := strings.Cut(id, ":")
	if base == "trustedrouter/auto-routing" {
		if hasVariant {
			return "trustedrouter/auto:" + variant
		}
		return "trustedrouter/auto"
	}
	return id
}

// routerModelIDAliases is model_aliases._ROUTER_MODEL_ID_ALIASES.
var routerModelIDAliases = map[string]string{
	"minimax/minimax-m3-1-flash": "minimax/minimax-m3.1-flash",
}
