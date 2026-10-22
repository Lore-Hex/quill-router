package routingsnapshot

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"slices"
	"strings"
	"time"
)

// Request is what authorize's candidate pipeline reads for one request in
// the E2 cohort: the authorize body's routing fields exactly as the enclave
// sent them, before schemas.GatewayAuthorizeRequest strips or canonicalizes
// anything (Evaluate applies that schema's whitespace stripping itself), the
// workspace's BYOK presence, the token counts and the instant.
//
// Evaluate assumes the inputs authorize_candidates takes besides these are
// absent: a custom or user-provided model, a partner billing mode, an
// additional cost reservation, a video resolution, and the process-local
// health re-rank. The caller's cohort predicate must pass any request that
// has one to Python.
type Request struct {
	// Model and Models are the body's model strings as sent; Models is nil
	// when the body has no models array.
	Model  string
	Models []string
	// Provider is the body's provider object as sent (nil or null when
	// absent).
	Provider json.RawMessage
	// AllowFallbacks is the body's top-level allow_fallbacks as sent (nil or
	// null when absent): an extra field the schema keeps unvalidated, which
	// provider_route_preferences reads beside provider.allow_fallbacks.
	AllowFallbacks json.RawMessage
	// RouteType, Region and ServiceTier are nil when the body omits them.
	RouteType   *string
	Region      *string
	ServiceTier *string
	// RequestedParameters and InputModalities are nil when absent.
	RequestedParameters []string
	InputModalities     []string
	// Stream is the body's stream flag (nil when absent); only the Stage D
	// reason reads it.
	Stream *bool
	// BYOKProviders are the provider slugs whose BYOK credentials the
	// workspace holds.
	BYOKProviders []string
	// EstimatedInputTokens is the body's estimated_input_tokens, nil when the
	// body omits it; Evaluate then applies the schema's default, 1
	// (schemas.py GatewayAuthorizeRequest). It is the enclave's prompt
	// estimate, a number Go receives, not one it derives from the messages.
	// It prices the estimate and is the prompt the context window must hold.
	EstimatedInputTokens *int64
	// MaxTokens, MaxCompletionTokens and MaxOutputTokens are the body's
	// fields (nil when absent). The window must also hold max_tokens, else
	// max_completion_tokens (max_output_tokens does not count); the estimate
	// prices the first of the three present, else 512 (output_estimate).
	MaxTokens           *int64
	MaxCompletionTokens *int64
	MaxOutputTokens     *int64
	// At is the request's instant: it chooses availability, the price epoch
	// and whether the rank evidence is current.
	At time.Time
}

// Outcome says which of a Result's fields hold the answer.
type Outcome int

const (
	// PassToPython: the request is outside what Go reproduces; Python
	// decides it. Never a guess: Reason says why.
	PassToPython Outcome = iota
	// Answered: Candidates, ModelEstimate and ServiceTier are Python's.
	Answered
	// Refused: Refusal is Python's error, status, type and message.
	Refused
)

// Refusal is the HTTP error authorize raises.
type Refusal struct {
	Status  int
	Type    string
	Message string
	// Param is the error's param field, where Python's carries one.
	Param string
}

// Result is the evaluator's answer for one request.
type Result struct {
	Outcome Outcome
	// Candidates are the endpoint ids in order; the first is selected.
	Candidates []string
	// ModelEstimate is the largest candidate's price at At, in microdollars.
	ModelEstimate int64
	// ServiceTier is the normalized tier, "" when the request names none.
	ServiceTier string
	Refusal     *Refusal
	// Reason says why a request went to Python.
	Reason string
}

func pass(format string, args ...any) Result {
	return Result{Outcome: PassToPython, Reason: fmt.Sprintf(format, args...)}
}

func refused(status int, kind, message string) Result {
	return Result{Outcome: Refused, Refusal: &Refusal{Status: status, Type: kind, Message: message}}
}

const noRouteMessage = "No route candidates match the requested provider filters"

func noRoute() Result { return refused(400, "model_not_supported", noRouteMessage) }

// Route types whose candidates come from the chat resolver for a chat
// model. images, videos, audio, fetch and orchestration sub-routes have
// resolvers or parameter rules the snapshot does not carry.
var chatArmRouteTypes = map[string]bool{
	"chat.completions": true, "responses": true, "messages": true,
	"embeddings": true, "decide": true,
}

// routing._ROUTE_TYPES_DESCRIBED_BY_REQUESTED_PARAMETERS, less None (a
// missing route type), which describeParameters handles.
var described = map[string]bool{"chat.completions": true, "responses": true}

// The privacy requirement provider.zdr adds (catalog_data.PRIVACY_TIER_ZERO_RETENTION;
// the schema doc: meets contains 2 exactly when an endpoint is ZDR).
const zeroRetention = 2

var openAIServiceTiers = map[string]bool{"default": true, "auto": true, "priority": true}

// printable reports whether s is printable ASCII. Go passes anything else to
// Python: Python's str.strip and str.lower and its regex \d act on Unicode
// (and strip ASCII separators 0x1c-0x1f) where Go's would not.
func printable(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] > 0x7e {
			return false
		}
	}
	return true
}

// prefs is routing.RoutePreferences for the fields Go evaluates.
type prefs struct {
	order          []string
	only           map[string]bool
	ignore         map[string]bool
	allowFallbacks bool
	deny           bool // data_collection == "deny"
	sort           string
	partitionNone  bool
	usage          string // "", "Credits" or "BYOK"
	usOnly         bool
	privacy        []int64
	modalities     []string
}

// Evaluate answers one request as authorize_candidates' plan_route,
// resolve_candidates and finish_candidates would with rerank absent, or
// passes it to Python.
func (s *Snapshot) Evaluate(req Request) Result {
	at := req.At.Unix()
	if at < s.CompiledAt || at >= s.NotAfter {
		return pass("the instant is outside the snapshot's window")
	}
	// The authorize schema: estimated_input_tokens >= 0 (default 1), each
	// max_* >= 1.
	prompt := int64(1)
	if req.EstimatedInputTokens != nil {
		prompt = *req.EstimatedInputTokens
	}
	if prompt < 0 {
		return pass("negative estimated_input_tokens")
	}
	for _, v := range []*int64{req.MaxTokens, req.MaxCompletionTokens, req.MaxOutputTokens} {
		if v != nil && *v < 1 {
			return pass("a max_* token count below 1")
		}
	}
	outputTokens := int64(512)
	for _, v := range []*int64{req.MaxOutputTokens, req.MaxCompletionTokens, req.MaxTokens} {
		if v != nil {
			outputTokens = *v
		}
	}
	completionTokens := int64(0)
	for _, v := range []*int64{req.MaxCompletionTokens, req.MaxTokens} {
		if v != nil {
			completionTokens = *v
		}
	}
	if prompt > math.MaxInt64-completionTokens {
		return pass("prompt and completion tokens beyond int64")
	}
	// The authorize schema (str_strip_whitespace) strips every string field
	// and every string in a list of strings; provider values and extra fields
	// it keeps as sent. Go strips only the space: anything else Python or
	// pydantic might strip is not printable ASCII, which goes to Python.
	req, ok := schemaStrip(req)
	if !ok {
		return pass("a string field that is not printable ASCII, or one the authorize schema refuses")
	}
	if req.RouteType != nil && !chatArmRouteTypes[*req.RouteType] {
		return pass("route type %q", *req.RouteType)
	}
	p, result, ok := s.parseProvider(req.Provider, req.AllowFallbacks)
	if !ok {
		return result
	}
	if req.RouteType == nil || described[*req.RouteType] {
		for _, name := range req.RequestedParameters {
			if !printable(name) || strings.Trim(name, " ") != "" {
				return pass("requested_parameters: the capability parameter table is not in the snapshot")
			}
		}
	}
	for _, m := range req.InputModalities {
		if !slices.Contains([]string{"text", "image", "audio", "video"}, m) {
			return pass("input_modalities %q", m)
		}
	}
	p.modalities = req.InputModalities
	tier := ""
	if req.ServiceTier != nil {
		tier = strings.ToLower(*req.ServiceTier)
	}

	ids, result, ok := s.requestedModelIDs(req, &p)
	if !ok {
		return result
	}
	// _routing_for_body: without fallbacks, provider.order is also a ceiling.
	if !p.allowFallbacks && len(p.order) > 0 {
		ordered := map[string]bool{}
		for _, slug := range p.order {
			if len(p.only) == 0 || p.only[slug] {
				ordered[slug] = true
			}
		}
		if len(ordered) == 0 {
			return noRoute()
		}
		p.only = ordered
	}

	// chat_route_endpoint_candidates.
	var all []int
	seen := map[int]bool{}
	for _, id := range ids {
		m := s.models[id]
		if m == nil || !m.SupportsChat {
			return refused(400, "model_not_supported", "Model does not support chat completions: "+id)
		}
		for _, i := range s.endpointsByModel[id] {
			if !seen[i] && s.available(&s.Endpoints[i], at) {
				seen[i] = true
				all = append(all, i)
			}
		}
	}
	filtered := s.filter(all, p)
	if len(filtered) == 0 && p.deny && len(p.privacy) == 0 {
		relaxed := p
		relaxed.deny = false
		filtered = s.filter(all, relaxed)
	}
	if len(filtered) == 0 {
		return noRoute()
	}
	candidates, ok := s.sortCandidates(filtered, p, at, req.At.UnixMicro())
	if !ok {
		return pass("headline prices whose sum exceeds int64")
	}

	// candidates_fitting_context: the models whose window holds the prompt
	// and the completion; an unknown window (0) holds anything. When none
	// does, OpenAI's context-length error names the largest window.
	needed := prompt + completionTokens
	fitting := candidates[:0:0]
	window := int64(0)
	for _, i := range candidates {
		length := s.models[s.Endpoints[i].ModelID].ContextLength
		window = max(window, length)
		if length <= 0 || needed <= length {
			fitting = append(fitting, i)
		}
	}
	if len(fitting) == 0 {
		return contextLengthError(window, prompt, completionTokens)
	}
	candidates = fitting

	// resolve_candidates: reachable from the request's gateway region.
	regional := candidates[:0:0]
	for _, i := range candidates {
		e := &s.Endpoints[i]
		region := s.Settings.PrimaryRegion
		if req.Region != nil && *req.Region != "" { // stripped by schemaStrip
			region = *req.Region
		} else if e.GatewayRegions.RequiresExplicitOrigin {
			region = ""
		}
		g := e.GatewayRegions
		if (g.Only == nil || slices.Contains(*g.Only, region)) && !slices.Contains(g.Deny, region) {
			regional = append(regional, i)
		}
	}

	// finish_candidates: BYOK presence, then the service tier.
	admitted := regional[:0:0]
	for _, i := range regional {
		e := &s.Endpoints[i]
		if e.UsageType != usageBYOK || slices.Contains(req.BYOKProviders, e.Provider) {
			admitted = append(admitted, i)
		}
	}
	if req.ServiceTier != nil {
		if !openAIServiceTiers[tier] {
			return refused(400, "bad_request", "service_tier must be default, auto, or priority")
		}
		openai := admitted[:0:0]
		for _, i := range admitted {
			if s.Endpoints[i].Provider == "openai" {
				openai = append(openai, i)
			}
		}
		if tier != "default" && len(openai) > 0 {
			return pass("OpenAI %s processing: its prices are not in the snapshot", tier)
		}
		if tier == "priority" && prompt > 272_000 {
			// openai_service_tiers.OPENAI_PRIORITY_MAX_PROMPT_TOKENS is not
			// in the snapshot.
			return pass("OpenAI priority prompt bound")
		}
		if len(openai) == 0 {
			return refused(400, "model_not_supported",
				fmt.Sprintf("OpenAI %s processing is unavailable for the requested model", tier))
		}
		admitted = openai
	}
	if !p.allowFallbacks && len(admitted) > 1 {
		admitted = admitted[:1]
	}
	if len(admitted) == 0 {
		return refused(400, "provider_not_supported", "No authorized route candidates are available for this workspace")
	}

	usage := Usage{InputTokens: prompt, OutputTokens: outputTokens}
	estimate := new(big.Int)
	out := Result{Outcome: Answered, ServiceTier: tier}
	for _, i := range admitted {
		e := &s.Endpoints[i]
		out.Candidates = append(out.Candidates, e.ID)
		if cost := Cost(e.priceAt(at), usage); cost.Cmp(estimate) > 0 {
			estimate = cost
		}
	}
	if !estimate.IsInt64() {
		return pass("the estimate exceeds int64")
	}
	out.ModelEstimate = estimate.Int64()
	return out
}

// available is catalog_is_current(at) and not provider_model_retired(at).
func (s *Snapshot) available(e *Endpoint, at int64) bool {
	return (e.CatalogValidUntil == nil || at < *e.CatalogValidUntil) &&
		(e.RetiredAt == nil || at < *e.RetiredAt)
}

func (e *Endpoint) epochAt(at int64) *Epoch {
	current := &e.Epochs[0]
	for i := range e.Epochs {
		if e.Epochs[i].StartsAt <= at {
			current = &e.Epochs[i]
		}
	}
	return current
}

func (e *Endpoint) priceAt(at int64) Price { return e.epochAt(at).Price }

// routerCanonical is model_aliases.canonical_router_model_id over the
// snapshot's rewrite tables. ok is false for a spelling the snapshot does
// not carry the rewrite of.
func (s *Snapshot) routerCanonical(id string) (string, bool) {
	if publisher, _, found := strings.Cut(id, "/"); found &&
		(publisher == "system1models" || publisher == "system1models-eu") {
		// canonical_router_model_id rewrites these to decisionmodelsio;
		// snapshot v1's router_prefix_rewrites does not carry that rule.
		return "", false
	}
	for _, r := range s.Aliases.RouterPrefixRewrites {
		if strings.HasPrefix(id, r.Prefix) {
			id = r.Replacement + id[len(r.Prefix):]
		}
	}
	base, variant, found := strings.Cut(id, ":")
	if rewritten, ok := s.Aliases.RouterBaseRewrites[base]; ok {
		if found {
			return rewritten + ":" + variant, true
		}
		return rewritten, true
	}
	return id, true
}

// resolve is routing.resolve_model_alias. ok is false where the result is
// not a plain model Go may evaluate (an alias target is a routing pool).
func (s *Snapshot) resolve(id string) (string, bool) {
	id, ok := s.routerCanonical(id)
	if !ok {
		return "", false
	}
	if _, ok := s.Aliases.AliasTargets[id]; ok {
		return "", false
	}
	if s.models[id] != nil {
		return id, true
	}
	for _, candidate := range []string{id, datedSuffix.ReplaceAllString(id, "")} {
		if s.models[candidate] != nil {
			return candidate, true
		}
		if s.models[s.Aliases.BareVendorPrefix+candidate] != nil {
			return s.Aliases.BareVendorPrefix + candidate, true
		}
	}
	return id, true
}

// requestedModelIDs is routing._requested_model_ids for requests that name
// no requested model (meta model, pool, privacy alias, decision model): the
// canonical ids in order, and the variant suffix's sort.
func (s *Snapshot) requestedModelIDs(req Request, p *prefs) ([]string, Result, bool) {
	var ids []string
	sortOverride := ""
	take := func(raw string) (Result, bool) {
		if !printable(raw) {
			return pass("a model string that is not printable ASCII"), false
		}
		raw = strings.Trim(raw, " ")
		if raw == "" {
			return pass("an empty model string"), false
		}
		stripped := raw
		for _, v := range s.Aliases.VariantSuffixes {
			if strings.HasSuffix(raw, v.Suffix) {
				stripped = raw[:len(raw)-len(v.Suffix)]
				sortOverride = v.Value
				break
			}
		}
		id, ok := s.resolve(stripped)
		if !ok {
			return pass("model %q resolves through a rule or alias outside the cohort", raw), false
		}
		if s.requested[id] {
			return pass("requested model %q (meta model, pool or alias)", id), false
		}
		if _, chain := s.DecisionChains[id]; chain {
			return pass("decision chain %q", id), false
		}
		if strings.HasPrefix(id, "trustedrouter/") && s.models[id] == nil {
			// The router's own namespace holds its pools and meta models; a
			// snapshot that does not list one is not evidence it is unknown.
			return pass("router model %q", id), false
		}
		ids = append(ids, id)
		return Result{}, true
	}

	if !printable(req.Model) {
		return nil, pass("a model string that is not printable ASCII"), false
	}
	route, ok := s.routerCanonical(req.Model)
	if !ok {
		return nil, pass("model %q needs a rewrite the snapshot does not carry", req.Model), false
	}
	// plan_route dispatches on the requested model before any suffix or
	// alias: an embeddings-only or decision-only model takes another arm.
	if m := s.models[route]; m != nil && !m.SupportsChat && (m.SupportsEmbeddings || m.SupportsDecide) {
		return nil, pass("model %q is served by the embeddings or decide resolver", route), false
	}
	if _, chain := s.DecisionChains[route]; chain {
		return nil, pass("decision chain %q", route), false
	}
	if r, ok := take(route); !ok {
		return nil, r, false
	}
	// With fallbacks off and a model named, models is ignored altogether.
	if p.allowFallbacks {
		for _, raw := range req.Models {
			if !printable(raw) {
				return nil, pass("a model string that is not printable ASCII"), false
			}
			canonical, ok := s.routerCanonical(raw)
			if !ok {
				return nil, pass("model %q needs a rewrite the snapshot does not carry", raw), false
			}
			if r, ok := take(canonical); !ok {
				return nil, r, false
			}
		}
	}
	if sortOverride != "" {
		p.sort = sortOverride
	}
	return ids, Result{}, true
}

// parseProvider is routing.provider_route_preferences for the provider
// fields and spellings the routing cases cover. Any other field or spelling
// passes to Python, which owns request parsing until the snapshot carries
// its tables. The one refusal it makes is the router-name refusal, in
// Python's field and item order.
func (s *Snapshot) parseProvider(raw, topLevelAllowFallbacks json.RawMessage) (prefs, Result, bool) {
	p := prefs{allowFallbacks: true}
	fields := map[string]json.RawMessage{}
	if !isJSONNull(raw) {
		if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
			return p, pass("provider is not an object"), false
		}
	}
	isNull := func(name string) bool {
		v, present := fields[name]
		return !present || isJSONNull(v)
	}
	str := func(name string) (string, bool) {
		var v string
		if err := json.Unmarshal(fields[name], &v); err != nil || !printable(v) {
			return "", false
		}
		return v, true
	}
	handled := map[string]bool{
		"order": true, "only": true, "ignore": true, "allow_fallbacks": true,
		"data_collection": true, "sort": true, "usage": true, "jurisdiction": true,
		"zdr": true, "require_parameters": true, "min_privacy": true, "max_price": true,
		"preferred_max_latency": true, "preferred_min_throughput": true,
	}
	for name := range fields {
		if !handled[name] {
			return p, pass("provider.%s", name), false
		}
	}
	// Present and not null, these need tables the snapshot does not carry.
	for _, name := range []string{"min_privacy", "max_price", "preferred_max_latency", "preferred_min_throughput"} {
		if !isNull(name) {
			return p, pass("provider.%s", name), false
		}
	}
	if v, present := fields["require_parameters"]; present && !bytes.Equal(bytes.TrimSpace(v), []byte("false")) {
		return p, pass("provider.require_parameters"), false
	}

	var routerRefusal *Result
	lists := map[string][]string{}
	for _, field := range []string{"order", "only", "ignore"} {
		if routerRefusal != nil {
			break
		}
		if isNull(field) {
			continue
		}
		// routing._string_list: the whole list is validated (every item a
		// non-blank string) before any item is looked up, so a bad item
		// after a router name is refused as a bad item. Go passes it.
		var items []string
		if text, ok := str(field); ok {
			for _, item := range strings.Split(text, ",") {
				if item = strings.Trim(item, " "); item != "" {
					items = append(items, item)
				}
			}
		} else {
			var raws []json.RawMessage
			if err := json.Unmarshal(fields[field], &raws); err != nil || raws == nil {
				return p, pass("provider.%s is not a list or a string", field), false
			}
			for _, rawItem := range raws {
				var item string
				if isJSONNull(rawItem) || json.Unmarshal(rawItem, &item) != nil ||
					!printable(item) || strings.Trim(item, " ") == "" {
					return p, pass("provider.%s has an item that is not a provider name", field), false
				}
				items = append(items, item)
			}
		}
		var out []string
		for _, item := range items {
			rawSlug := strings.NewReplacer("_", "-", " ", "-").Replace(strings.ToLower(strings.Trim(item, " ")))
			slugs, group := s.Aliases.ProviderGroupAliases[rawSlug]
			if !group {
				slug := rawSlug
				if alias, ok := s.Aliases.ProviderAliases[slug]; ok {
					slug = alias
				}
				slugs = []string{slug}
			}
			if slices.ContainsFunc(slugs, func(slug string) bool { return slices.Contains(s.Aliases.RouterProviderSlugs, slug) }) {
				r := refused(400, "bad_request", fmt.Sprintf(
					"Routing filters cannot contain router name '%s'. Use model='trustedrouter/zdr' "+
						"or another TrustedRouter alias, and omit the router from provider filters.", item))
				routerRefusal = &r
				break
			}
			for _, slug := range slugs {
				if s.providers[slug] == nil {
					// Python refuses a slug its process does not know; a slug
					// the snapshot does not list is not proof of that.
					return p, pass("provider %q is not in the snapshot", slug), false
				}
				if !slices.Contains(out, slug) {
					out = append(out, slug)
				}
			}
		}
		lists[field] = out
	}
	// The top-level and nested allow_fallbacks: each a boolean when present,
	// and equal when both are. These refusals follow the lists' in Python.
	var top, nested *bool
	var fallbackRefusal *Result
	for _, f := range []struct {
		raw    json.RawMessage
		into   **bool
		reason string
	}{
		{topLevelAllowFallbacks, &top, "allow_fallbacks must be a boolean"},
		{fields["allow_fallbacks"], &nested, "provider.allow_fallbacks must be a boolean"},
	} {
		if fallbackRefusal != nil || isJSONNull(f.raw) {
			continue
		}
		var v bool
		if err := json.Unmarshal(f.raw, &v); err != nil {
			r := refused(400, "bad_request", f.reason)
			fallbackRefusal = &r
			continue
		}
		*f.into = &v
	}
	if fallbackRefusal == nil && top != nil && nested != nil && *top != *nested {
		r := refused(400, "bad_request", "allow_fallbacks conflicts with provider.allow_fallbacks")
		fallbackRefusal = &r
	}
	if top != nil {
		p.allowFallbacks = *top
	} else if nested != nil {
		p.allowFallbacks = *nested
	}
	if !isNull("data_collection") {
		v, ok := str("data_collection")
		switch v = strings.ToLower(strings.Trim(v, " ")); {
		case ok && v == "deny":
			p.deny = true
		case ok && v == "allow":
		default:
			return p, pass("provider.data_collection spelling"), false
		}
	}
	if !isNull("sort") {
		by, partition := "", "model"
		if v, ok := str("sort"); ok {
			by = v
		} else {
			var object map[string]*string
			if err := json.Unmarshal(fields["sort"], &object); err != nil || object == nil {
				return p, pass("provider.sort shape"), false
			}
			for _, key := range []string{"sort", "strategy", "by"} {
				if v := object[key]; v != nil && *v != "" {
					by = *v
					break
				}
			}
			if v := object["partition"]; v != nil {
				// Python: any non-null partition must be "model" or "none".
				partition = strings.ToLower(strings.Trim(*v, " "))
			}
			for _, v := range object {
				if v != nil && !printable(*v) {
					return p, pass("provider.sort is not printable ASCII"), false
				}
			}
		}
		by = strings.ToLower(strings.Trim(by, " "))
		if !slices.Contains([]string{"", "price", "latency", "throughput"}, by) ||
			!slices.Contains([]string{"model", "none"}, partition) {
			return p, pass("provider.sort spelling"), false
		}
		p.sort, p.partitionNone = by, partition == "none"
	}
	if !isNull("usage") {
		v, ok := str("usage")
		switch v = strings.ToLower(strings.Trim(v, " ")); {
		case ok && v == "byok":
			p.usage = usageBYOK
		case ok && v == "credits":
			p.usage = usageCredits
		default:
			return p, pass("provider.usage spelling"), false
		}
	}
	if !isNull("jurisdiction") {
		v, ok := str("jurisdiction")
		if !ok || strings.ToLower(strings.Trim(v, " ")) != "us" {
			return p, pass("provider.jurisdiction spelling"), false
		}
		p.usOnly = true
	}
	if !isNull("zdr") {
		var zdr bool
		if err := json.Unmarshal(fields["zdr"], &zdr); err != nil {
			return p, pass("provider.zdr is not a boolean"), false
		}
		if zdr {
			p.privacy = []int64{zeroRetention}
		}
	}
	if routerRefusal != nil {
		return p, *routerRefusal, false
	}
	if fallbackRefusal != nil {
		return p, *fallbackRefusal, false
	}
	p.order = lists["order"]
	p.only = set(lists["only"])
	p.ignore = set(lists["ignore"])
	return p, Result{}, true
}

func isJSONNull(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null"))
}

// schemaStrip applies schemas.GatewayAuthorizeRequest to the routing fields:
// str_strip_whitespace on every str and list[str] field, min_length=1 on
// model and service_tier, max_length=20 on service_tier. ok is false for a
// string that is not printable ASCII (where Python's or pydantic's notion of
// whitespace could differ from Go's) or that the schema refuses.
func schemaStrip(req Request) (Request, bool) {
	strip := func(v string) (string, bool) {
		return strings.Trim(v, " "), printable(v)
	}
	ok := true
	field := func(v *string) *string {
		if v == nil {
			return nil
		}
		out, good := strip(*v)
		ok = ok && good
		return &out
	}
	list := func(values []string) []string {
		if values == nil {
			return nil
		}
		out := make([]string, len(values))
		for i, v := range values {
			var good bool
			out[i], good = strip(v)
			ok = ok && good
		}
		return out
	}
	var good bool
	req.Model, good = strip(req.Model)
	ok = ok && good && req.Model != ""
	req.Models = list(req.Models)
	req.RouteType = field(req.RouteType)
	req.Region = field(req.Region)
	req.ServiceTier = field(req.ServiceTier)
	req.RequestedParameters = list(req.RequestedParameters)
	if req.ServiceTier != nil && (*req.ServiceTier == "" || len(*req.ServiceTier) > 20) {
		ok = false
	}
	return req, ok
}

// contextLengthError is routing.context_length_error.
func contextLengthError(window, prompt, completion int64) Result {
	message := fmt.Sprintf("This model's maximum context length is %d tokens. However, your "+
		"messages resulted in %d tokens. Please reduce the length of the messages.", window, prompt)
	if completion != 0 {
		message = fmt.Sprintf("This model's maximum context length is %d tokens. However, you "+
			"requested %d tokens (%d in the messages, %d in the completion). Please reduce the "+
			"length of the messages or completion.", window, prompt+completion, prompt, completion)
	}
	r := refused(400, "context_length_exceeded", message)
	r.Refusal.Param = "messages"
	return r
}

func set(values []string) map[string]bool {
	out := make(map[string]bool, len(values))
	for _, v := range values {
		out[v] = true
	}
	return out
}

// filter is routing._apply_endpoint_provider_filters for the preferences
// Go evaluates.
func (s *Snapshot) filter(candidates []int, p prefs) []int {
	var out []int
	for _, i := range candidates {
		e := &s.Endpoints[i]
		if len(p.only) > 0 && !p.only[e.Provider] {
			continue
		}
		if p.ignore[e.Provider] {
			continue
		}
		if hq := s.providers[e.Provider].HeadquartersCountry; p.usOnly && (hq == nil || *hq != "US") {
			continue
		}
		if p.deny && e.Privacy.StoresContent {
			continue
		}
		if slices.ContainsFunc(p.privacy, func(r int64) bool { return !slices.Contains(e.Privacy.Meets, r) }) {
			continue
		}
		if p.usage != "" && e.UsageType != p.usage {
			continue
		}
		if slices.ContainsFunc(p.modalities, func(m string) bool { return !slices.Contains(e.InputModalities, m) }) {
			continue
		}
		out = append(out, i)
	}
	return out
}

// rank is provider_ranking.measured_provider_rank at nowUS (microseconds).
func (s *Snapshot) rank(provider, sort string, nowUS int64) int64 {
	r := s.Ranking
	if nowUS < r.MeasuredFromUS || nowUS > r.MeasuredThroughUS {
		return r.UnknownRank
	}
	table := r.Default
	switch sort {
	case "latency":
		table = r.Latency
	case "throughput":
		table = r.Throughput
	}
	if v, ok := table[provider]; ok {
		return v
	}
	return r.UnknownRank
}

// sortCandidates is routing._sort_endpoint_candidates: by (model rank unless
// the sort partition is none, provider order rank, sort rank, catalog index).
func (s *Snapshot) sortCandidates(candidates []int, p prefs, at, nowUS int64) ([]int, bool) {
	orderRank := map[string]int{}
	for i, provider := range p.order {
		if _, ok := orderRank[provider]; !ok {
			orderRank[provider] = i
		}
	}
	modelRank := map[string]int{}
	for _, i := range candidates {
		if _, ok := modelRank[s.Endpoints[i].ModelID]; !ok {
			modelRank[s.Endpoints[i].ModelID] = len(modelRank)
		}
	}
	type key struct{ model, order, sort, index int64 }
	keys := make(map[int]key, len(candidates))
	for index, i := range candidates {
		e := &s.Endpoints[i]
		k := key{order: int64(len(p.order)), index: int64(index)}
		if !p.partitionNone {
			k.model = int64(modelRank[e.ModelID])
		}
		if r, ok := orderRank[e.Provider]; ok {
			k.order = int64(r)
		}
		switch p.sort {
		case "price":
			h := e.epochAt(at).Headline
			if h.Prompt < 0 || h.Completion < 0 || h.Prompt > math.MaxInt64-h.Completion {
				return nil, false
			}
			k.sort = h.Prompt + h.Completion
		case "latency", "throughput":
			k.sort = s.rank(e.Provider, p.sort, nowUS)
		default:
			k.sort = s.rank(e.Provider, "", nowUS)
			if e.UsageType == usageCredits {
				if floor, ok := s.Ranking.PrepaidMinRank[e.Provider]; ok && floor > k.sort {
					k.sort = floor
				}
			}
			if len(modelRank) == 1 {
				if v, ok := s.Ranking.ModelProviderPreference[e.ModelID][e.Provider]; ok {
					k.sort = v
				}
			}
		}
		keys[i] = k
	}
	out := slices.Clone(candidates)
	slices.SortStableFunc(out, func(a, b int) int {
		ka, kb := keys[a], keys[b]
		for _, d := range [][2]int64{{ka.model, kb.model}, {ka.order, kb.order}, {ka.sort, kb.sort}, {ka.index, kb.index}} {
			if d[0] != d[1] {
				if d[0] < d[1] {
					return -1
				}
				return 1
			}
		}
		return 0
	})
	return out, true
}

// StageDInputs are what a request brings to the Stage D decision beyond its
// candidates (the routing cases' stage_d_inputs). Pilot is true when the
// workspace is in the pilot list or the list is empty.
type StageDInputs struct {
	EligibilityEnabled          bool
	Pilot                       bool
	HeartbeatEnabled            bool
	BootAccepted                bool
	HeartbeatDeclared           bool
	RequireHeartbeatDeclaration bool
	StandardEndpointPricing     bool
	SettlementBackend           bool
}

// StageDReason is gateway._stage_d_eligibility_reason for req and r, the
// Result Evaluate gave for it. It reads req through the same schemaStrip as
// Evaluate, so the route type it compares is the one the candidates were
// chosen for. ok is false unless r answered req.
func (s *Snapshot) StageDReason(in StageDInputs, req Request, r Result) (reason string, ok bool) {
	req, ok = schemaStrip(req)
	if !ok || r.Outcome != Answered {
		return "", false
	}
	stream, routeType := req.Stream, req.RouteType
	switch {
	case !in.EligibilityEnabled:
		return "stage_d_disabled", true
	case !in.Pilot:
		return "workspace_not_pilot", true
	case !in.HeartbeatEnabled:
		return "heartbeats_disabled", true
	case !in.BootAccepted:
		return "boot_not_accepted", true
	case in.RequireHeartbeatDeclaration && !in.HeartbeatDeclared:
		return "heartbeat_undeclared", true
	case stream == nil || !*stream:
		return "not_streaming", true
	case routeType == nil || (*routeType != "chat.completions" && *routeType != "responses"):
		return "route", true
	}
	for _, id := range r.Candidates {
		if s.Endpoints[s.endpointIndex[id]].UsageType != usageCredits {
			return "mixed_usage_type", true
		}
	}
	switch {
	case !in.StandardEndpointPricing:
		return "pricing_kind", true
	case r.ServiceTier == "priority" || r.ServiceTier == "auto":
		return "service_tier", true
	case !in.SettlementBackend:
		return "settlement_backend", true
	}
	return "ok", true
}
