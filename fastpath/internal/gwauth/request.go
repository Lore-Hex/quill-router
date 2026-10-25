package gwauth

// The authorize request as Python's route reads it
// (docs/design/fast-admission-e2-decide.md, §3, §7 and §8.2): the body
// parsed strictly as schemas.GatewayAuthorizeRequest, and the headers the
// route reads. Whatever Go cannot hold exactly as Python would (a float, a
// duplicate key, a value pydantic would coerce, a string whose whitespace
// Python strips differently) is a Pass with its §3 reason, never a guess.

import (
	"encoding/json"
	"errors"
	"math/big"
	"regexp"
	"strings"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// Headers are the headers authorize reads, every value as received.
type Headers struct {
	// ContentType is every Content-Type value (body.content_type).
	ContentType           []string
	IdempotencyKey        []string
	SettlementMode        []string // X-TR-Settlement-Mode
	VideoAllowedProviders bool     // X-Quill-Video-Allowed-Providers present
}

// Request is a GatewayAuthorizeRequest as model_dump(exclude_none=True)
// holds it, for a request the request half of the cohort (§3) admits.
type Request struct {
	// Dump is §7 step 1: the declared fields present and not null, with
	// their defaults, strings stripped, then the extras whose value is not
	// null; body order, defaults last. cache_affinity_key,
	// cache_affinity_explicit and idempotency_key_source are excluded, as
	// the schema excludes them.
	Dump    pyjson.Object
	Routing routingsnapshot.Request
	// Model is body.model stripped (identity under
	// canonical_router_model_id); Models is nil when absent.
	Model              string
	Models             []string
	APIKeyLookupHash   string
	BodyIdempotencyKey *string // stripped
	InvocationNonce    *string // stripped
	// Tags are body.tags in body order, validated (§6's rules, ASCII);
	// HasTags is whether body.tags is present and not null, even {}.
	Tags                 []Tag
	HasTags              bool
	EstimatedInputTokens int64 // 1 when omitted
	OutputEstimate       int64 // max_tokens, max_completion_tokens, max_output_tokens, else 512
	RequestedRegion      *string
	// CacheAffinity is whether the body names a cache_affinity_key
	// (body.cache_affinity, the decide pipeline's gate after the compose:
	// Python's order may be a remembered session's, which Go cannot read).
	CacheAffinity bool

	// attribution is validate_request_attribution's result (§7 step 4).
	attribution attribution
	// additionalCost and inferenceReceipt are the two priced fields
	// _gateway_authorize_body drops at their defaults.
	additionalCost   bool
	inferenceReceipt bool
}

// Pass is why a request goes to Python; "" is none.
type Pass struct{ Reason string }

// Passed reports whether the request goes to Python.
func (p Pass) Passed() bool { return p.Reason != "" }

func passFor(reason string) Pass { return Pass{Reason: reason} }

// maxBodyBytes is Python's max_request_body_bytes (config.py), past which
// it answers 413. A body of exactly that size passes too.
const maxBodyBytes = 4 << 20

// maxDepth and maxIntDigits are §3's bounds: past 128 levels Python's
// decoder or validation recursion refuses, and an integer literal of more
// than 4,000 digits is near CPython's 4,300.
const (
	maxDepth     = 128
	maxIntDigits = 4000
)

// jsonContentType is the one Content-Type Go accepts: application/json,
// optionally with charset=utf-8, in any case (Starlette compares the media
// type case-insensitively).
var jsonContentType = regexp.MustCompile(`(?i)^application/json(;[ \t]*charset=utf-8)?$`)

// ParseRequest parses an authorize body strictly and applies the request
// half of §3's cohort predicate. A Pass names the first §3 reason the
// request trips; the Request is meaningful only without one.
func ParseRequest(body []byte, h Headers) (Request, Pass) {
	r, object, p := parseBody(body, h)
	if p.Passed() {
		return Request{}, p
	}
	if p := r.cohort(object, h); p.Passed() {
		return Request{}, p
	}
	return r, Pass{}
}

// parseBody is ParseRequest up to body.refused: the content type, the
// decode and the schema.
func parseBody(body []byte, h Headers) (Request, pyjson.Object, Pass) {
	if len(h.ContentType) != 1 || !jsonContentType.MatchString(strings.Trim(h.ContentType[0], " \t")) {
		return Request{}, nil, passFor(ReasonContentType)
	}
	// A UTF-8 byte order mark, which json.loads of bytes strips, is not
	// JSON to Decode: unparseable too.
	if len(body) >= maxBodyBytes || !shallowAndShort(body) {
		return Request{}, nil, passFor(ReasonUnparseable)
	}
	tree, err := pyjson.Decode(body)
	switch {
	case errors.Is(err, pyjson.ErrDuplicateKey):
		return Request{}, nil, passFor(ReasonDuplicateKey)
	case errors.Is(err, pyjson.ErrNonFinite):
		return Request{}, nil, passFor(ReasonFloat)
	case err != nil:
		return Request{}, nil, passFor(ReasonUnparseable)
	}
	object, ok := tree.(pyjson.Object)
	if !ok {
		return Request{}, nil, passFor(ReasonUnparseable)
	}
	if hasFloat(object) {
		return Request{}, nil, passFor(ReasonFloat)
	}
	r, p := parseSchema(object)
	return r, object, p
}

// shallowAndShort scans the text outside strings: no nesting deeper than
// maxDepth and no run of more than maxIntDigits digits (a float's digits
// count too; a float passes anyway).
func shallowAndShort(body []byte) bool {
	depth, digits := 0, 0
	inString, escaped := false, false
	for _, c := range body {
		if inString {
			switch {
			case escaped:
				escaped = false
			case c == '\\':
				escaped = true
			case c == '"':
				inString = false
			}
			continue
		}
		if c >= '0' && c <= '9' {
			if digits++; digits > maxIntDigits {
				return false
			}
			continue
		}
		digits = 0
		switch c {
		case '"':
			inString = true
		case '[', '{':
			if depth++; depth > maxDepth {
				return false
			}
		case ']', '}':
			depth--
		}
	}
	return true
}

// hasFloat reports a number with a fraction or an exponent anywhere: Python
// reads it as a float, whose repr Go does not hold to the fingerprint.
func hasFloat(v any) bool {
	switch v := v.(type) {
	case json.Number:
		return strings.ContainsAny(string(v), ".eE")
	case pyjson.Object:
		for _, m := range v {
			if hasFloat(m.Value) {
				return true
			}
		}
	case []any:
		for _, item := range v {
			if hasFloat(item) {
				return true
			}
		}
	}
	return false
}

// kind is a declared field's schema type.
type kind int

const (
	kindStr      kind = iota
	kindInt           // int, from a JSON integer
	kindBool          // bool, from true or false
	kindStrList       // list[str], each stripped
	kindLiterals      // list[Literal["text", "image", "audio", "video"]], not stripped
	kindDict          // dict[str, Any]: top-level keys stripped, values kept
	kindAnything      // idempotency_key_source: any value, bounded, excluded
)

// field is one declared field of GatewayAuthorizeRequest (schemas.py).
type field struct {
	kind     kind
	required bool  // model: no default
	optional bool  // `X | None`: a JSON null is the default
	dflt     any   // the default model_dump writes when absent (non-nil only)
	exclude  bool  // Field(exclude=True)
	min, max int64 // length bounds of a str (chars; max 0 = none) or value bounds of an int
	bounded  bool  // the int has bounds min..max (max -1 = none)
	strict   bool  // Field(strict=True): no lax coercion
	pattern  *regexp.Regexp
}

var hex64 = regexp.MustCompile(`^[0-9a-f]{64}$`)

// declared is every field of schemas.GatewayAuthorizeRequest.
var declared = map[string]field{
	"speech_input_characters":   {kind: kindInt, optional: true, bounded: true, min: 1, max: 60_000},
	"transcription_duration_ms": {kind: kindInt, optional: true, bounded: true, min: 1, max: 600_000, strict: true},
	"video_resolution":          {kind: kindStr, optional: true},
	"cache_affinity_key":        {kind: kindStr, optional: true, exclude: true, pattern: hex64},
	"cache_affinity_explicit":   {kind: kindBool, exclude: true},
	"api_key_hash":              {kind: kindStr, optional: true, min: 1},
	"api_key_lookup_hash":       {kind: kindStr, optional: true, min: 1},
	"idempotency_key":           {kind: kindStr, optional: true, min: 1, max: 256},
	"idempotency_key_source":    {kind: kindAnything, optional: true, exclude: true},
	"request_fingerprint":       {kind: kindStr, optional: true, pattern: hex64},
	"model":                     {kind: kindStr, required: true, min: 1},
	"models":                    {kind: kindStrList, optional: true},
	"provider":                  {kind: kindDict, optional: true},
	"requested_parameters":      {kind: kindStrList, optional: true},
	"input_modalities":          {kind: kindLiterals, optional: true},
	"estimated_input_tokens":    {kind: kindInt, dflt: json.Number("1"), bounded: true, min: 0, max: -1},
	"max_output_tokens":         {kind: kindInt, optional: true, bounded: true, min: 1, max: -1},
	"max_completion_tokens":     {kind: kindInt, optional: true, bounded: true, min: 1, max: -1},
	"max_tokens":                {kind: kindInt, optional: true, bounded: true, min: 1, max: -1},
	"service_tier":              {kind: kindStr, optional: true, min: 1, max: 20},
	"stream":                    {kind: kindBool, optional: true},
	"region":                    {kind: kindStr, optional: true},
	"user":                      {kind: kindStr, optional: true, max: maxUserChars},
	"session_id":                {kind: kindStr, optional: true, max: maxSessionChars},
	"trace":                     {kind: kindDict, optional: true},
	"metadata":                  {kind: kindDict, optional: true},
	"tags":                      {kind: kindDict, optional: true},
	"app":                       {kind: kindStr, optional: true, max: maxAppChars},
	"http_referer":              {kind: kindStr, optional: true, max: maxRefererChars},
	"app_categories":            {kind: kindStrList, optional: true},
	"route_fallbacks":           {kind: kindInt, optional: true, bounded: true, min: 0, max: -1},
	"route_failures":            {kind: kindStrList, optional: true},
	"route_type":                {kind: kindStr, optional: true},
	"inference_receipt":         {kind: kindBool, dflt: false},
	"additional_cost_reservation_microdollars": {kind: kindInt, dflt: json.Number("0"), bounded: true, min: 0,
		max: 100_000_000},
	"invocation_nonce": {kind: kindStr, optional: true, max: 64},
}

// declaredOrder is the schema's declaration order, in which model_dump
// writes the defaults of absent fields (only the order of the defaults
// matters here; the fingerprint sorts).
var declaredOrder = []string{"estimated_input_tokens", "inference_receipt",
	"additional_cost_reservation_microdollars"}

// verdict is what one field's value is to the schema.
type verdict int

const (
	exact   verdict = iota // the schema's exact JSON type
	lax                    // pydantic's lax mode would coerce it
	refused                // a validation error
)

// typeOf is the schema's reading of a field's value, before stripping.
func typeOf(f field, v any) verdict {
	if v == nil {
		if f.optional {
			return exact
		}
		return refused
	}
	switch f.kind {
	case kindAnything:
		return exact
	case kindStr:
		if _, ok := v.(string); ok {
			return exact
		}
		return refused
	case kindInt:
		switch v := v.(type) {
		case json.Number:
			return exact // hasFloat already passed fractions and exponents
		case bool:
			return laxUnlessStrict(f)
		case string:
			if laxInt.MatchString(v) {
				return laxUnlessStrict(f)
			}
		}
		return refused
	case kindBool:
		switch v := v.(type) {
		case bool:
			return exact
		case json.Number:
			if v == "0" || v == "1" {
				return lax
			}
		case string:
			if laxBools[strings.ToLower(strings.TrimSpace(v))] {
				return lax
			}
		}
		return refused
	case kindStrList, kindLiterals:
		items, ok := v.([]any)
		if !ok {
			return refused
		}
		for _, item := range items {
			if _, ok := item.(string); !ok {
				return refused
			}
		}
		return exact
	case kindDict:
		if _, ok := v.(pyjson.Object); ok {
			return exact
		}
		return refused
	}
	return refused
}

func laxUnlessStrict(f field) verdict {
	if f.strict {
		return refused
	}
	return lax
}

var laxInt = regexp.MustCompile(`^\s*[+-]?[0-9]+\s*$`)

// laxBools are the strings pydantic's lax bool accepts.
var laxBools = map[string]bool{"0": true, "1": true, "f": true, "n": true, "t": true, "y": true, "no": true,
	"on": true, "off": true, "yes": true, "true": true, "false": true}

// printableASCII is the one alphabet in which Go's strip of spaces is
// Python's str.strip(): Python also strips other ASCII and Unicode
// whitespace.
func printableASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] > 0x7e {
			return false
		}
	}
	return true
}

func strip(s string) string { return strings.Trim(s, " ") }

// parseSchema validates the decoded object as GatewayAuthorizeRequest and
// _gateway_authorize_body's checks, in §3's order: lax_type,
// strip_ambiguous, dict_key_strip, then refused.
func parseSchema(object pyjson.Object) (Request, Pass) {
	values := make(map[string]any, len(object))
	for _, m := range object {
		values[m.Key] = m.Value
	}
	// Each reason is checked over the whole body before the next, so a
	// body tripping several is counted under the first in §3's order.
	anyLax, anyRefused := false, false
	for name, f := range declared {
		v, present := values[name]
		switch {
		case !present:
			anyRefused = anyRefused || f.required
		case typeOf(f, v) == lax:
			anyLax = true
		case typeOf(f, v) == refused:
			anyRefused = true
		}
	}
	if anyLax {
		return Request{}, passFor(ReasonLaxType)
	}
	// Every declared string and list-of-strings entry must strip as Go
	// strips; input_modalities is a Literal list and is not stripped.
	for name, f := range declared {
		if !stripped(f.kind) {
			continue
		}
		for _, s := range stringsOf(values[name]) {
			if !printableASCII(s) {
				return Request{}, passFor(ReasonStripAmbiguous)
			}
		}
	}
	for _, name := range []string{"provider", "trace", "metadata", "tags"} {
		if o, ok := values[name].(pyjson.Object); ok {
			for _, m := range o {
				if !printableASCII(m.Key) || strip(m.Key) != m.Key {
					return Request{}, passFor(ReasonDictKeyStrip)
				}
			}
		}
	}
	if anyRefused {
		return Request{}, passFor(ReasonRefused)
	}
	return build(object, values)
}

func stripped(k kind) bool { return k == kindStr || k == kindStrList }

// stringsOf is a str field's value, or a list-of-strings field's entries.
func stringsOf(v any) []string {
	switch v := v.(type) {
	case string:
		return []string{v}
	case []any:
		out := make([]string, 0, len(v))
		for _, item := range v {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

var literalModalities = map[string]bool{"text": true, "image": true, "audio": true, "video": true}

// build checks the schema's constraints and _gateway_authorize_body's
// validators on values of the exact types, and holds the result.
func build(object pyjson.Object, values map[string]any) (Request, Pass) {
	refuse := func() (Request, Pass) { return Request{}, passFor(ReasonRefused) }
	var r Request
	dumped := map[string]any{}
	for _, m := range object {
		f, isDeclared := declared[m.Key]
		if !isDeclared {
			if m.Value != nil { // exclude_none drops a top-level null extra
				r.Dump = append(r.Dump, pyjson.Member{Key: m.Key, Value: m.Value})
			}
			continue
		}
		if m.Value == nil {
			continue
		}
		value := m.Value
		switch f.kind {
		case kindStr:
			s := strip(value.(string))
			if int64(len(s)) < f.min || (f.max > 0 && int64(len(s)) > f.max) ||
				(f.pattern != nil && !f.pattern.MatchString(s)) {
				return refuse()
			}
			value = s
		case kindStrList:
			items := value.([]any)
			out := make([]any, len(items))
			for i, item := range items {
				out[i] = strip(item.(string))
			}
			value = out
		case kindLiterals:
			for _, item := range value.([]any) {
				if !literalModalities[item.(string)] {
					return refuse()
				}
			}
		case kindInt:
			n, ok := new(big.Int).SetString(string(value.(json.Number)), 10)
			if !ok || (f.bounded && (n.Cmp(big.NewInt(f.min)) < 0 || (f.max >= 0 && n.Cmp(big.NewInt(f.max)) > 0))) {
				return refuse()
			}
		}
		dumped[m.Key] = value
		if !f.exclude {
			r.Dump = append(r.Dump, pyjson.Member{Key: m.Key, Value: value})
		}
	}
	for _, name := range declaredOrder {
		if _, present := dumped[name]; !present {
			dumped[name] = declared[name].dflt
			r.Dump = append(r.Dump, pyjson.Member{Key: name, Value: declared[name].dflt})
		}
	}
	str := func(name string) *string {
		if s, ok := dumped[name].(string); ok {
			return &s
		}
		return nil
	}
	// The model validator: a key identifier is required.
	keyHash, lookup := str("api_key_hash"), str("api_key_lookup_hash")
	if (keyHash == nil || *keyHash == "") && (lookup == nil || *lookup == "") {
		return refuse()
	}
	if lookup != nil {
		r.APIKeyLookupHash = *lookup
	}
	r.Model = dumped["model"].(string)
	if models, ok := dumped["models"].([]any); ok {
		r.Models = make([]string, len(models))
		for i, m := range models {
			r.Models[i] = m.(string)
		}
	}
	r.BodyIdempotencyKey, r.InvocationNonce, r.RequestedRegion = str("idempotency_key"), str("invocation_nonce"),
		str("region")
	// The schema's pattern (hex64) admits no empty key.
	r.CacheAffinity = str("cache_affinity_key") != nil
	cost, _ := new(big.Int).SetString(string(dumped["additional_cost_reservation_microdollars"].(json.Number)), 10)
	r.additionalCost = cost.Sign() != 0
	r.inferenceReceipt = dumped["inference_receipt"].(bool)

	// _gateway_authorize_body: validate_tags(body.tags), when present.
	if tags, ok := dumped["tags"].(pyjson.Object); ok {
		r.HasTags = true
		for _, m := range tags {
			v, isString := m.Value.(string)
			if !isString {
				return refuse() // "tag value must be a string"
			}
			r.Tags = append(r.Tags, Tag{Key: m.Key, Value: v})
		}
		if !tagsPortableASCII(r.Tags) {
			return Request{}, passFor(ReasonTags)
		}
		if !validTags(r.Tags) {
			return refuse()
		}
	}
	a, p := validateAttribution(dumped)
	if p.Passed() {
		return Request{}, p
	}
	r.attribution = a

	routing, p := routingOf(values, dumped)
	if p.Passed() {
		return Request{}, p
	}
	r.Routing = routing
	r.EstimatedInputTokens = 1
	if routing.EstimatedInputTokens != nil {
		r.EstimatedInputTokens = *routing.EstimatedInputTokens
	}
	r.OutputEstimate = 512
	for _, v := range []*int64{routing.MaxOutputTokens, routing.MaxCompletionTokens, routing.MaxTokens} {
		if v != nil {
			r.OutputEstimate = *v
		}
	}
	return r, Pass{}
}

// routingOf is the evaluator's input: the body's routing fields as sent
// (Evaluate strips them itself). An integer beyond int64, which Python
// holds and Go's evaluator does not, passes.
func routingOf(values, dumped map[string]any) (routingsnapshot.Request, Pass) {
	var req routingsnapshot.Request
	req.Model = values["model"].(string)
	if models, ok := values["models"].([]any); ok {
		req.Models = make([]string, len(models))
		for i, m := range models {
			req.Models[i] = m.(string)
		}
	}
	for _, f := range []struct {
		name string
		into *json.RawMessage
	}{{"provider", &req.Provider}, {"allow_fallbacks", &req.AllowFallbacks}} {
		v, present := values[f.name]
		if !present || v == nil {
			continue
		}
		raw, err := pyjson.Dumps(v, pyjson.Options{})
		if err != nil {
			return req, passFor(ReasonEvaluatorInput)
		}
		*f.into = raw
	}
	for _, f := range []struct {
		name string
		into **string
	}{{"route_type", &req.RouteType}, {"region", &req.Region}, {"service_tier", &req.ServiceTier}} {
		if s, ok := values[f.name].(string); ok {
			*f.into = &s
		}
	}
	for _, f := range []struct {
		name string
		into *[]string
	}{{"requested_parameters", &req.RequestedParameters}, {"input_modalities", &req.InputModalities}} {
		if items, ok := values[f.name].([]any); ok {
			*f.into = make([]string, len(items))
			for i, item := range items {
				(*f.into)[i] = item.(string)
			}
		}
	}
	if b, ok := values["stream"].(bool); ok {
		req.Stream = &b
	}
	for _, f := range []struct {
		name string
		into **int64
	}{{"estimated_input_tokens", &req.EstimatedInputTokens}, {"max_tokens", &req.MaxTokens},
		{"max_completion_tokens", &req.MaxCompletionTokens}, {"max_output_tokens", &req.MaxOutputTokens}} {
		n, ok := values[f.name].(json.Number)
		if !ok {
			continue
		}
		v, err := n.Int64()
		if err != nil {
			return req, passFor(ReasonEvaluatorInput)
		}
		*f.into = &v
	}
	return req, Pass{}
}
