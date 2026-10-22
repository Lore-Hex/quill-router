package routingsnapshot

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"reflect"
	"regexp"
	"slices"
	"strings"
)

// The literals of the v1 schema (src/trusted_router/routing_snapshot.py).
const (
	snapshotVersion  = 1
	snapshotKind     = "tr-routing-snapshot"
	epochSeconds     = 3600
	maxHorizon       = 24 * 3600
	pricingRounding  = "half_up_per_million"
	receiptRounding  = "ceil_ratio"
	usageCredits     = "Credits"
	usageBYOK        = "BYOK"
	maxBodyJSONDepth = 64
)

// datedSuffixPattern is the only dated_suffix_pattern Go evaluates. Python's
// re and Go's RE2 agree on it for the printable-ASCII strings Go accepts
// (Python's \d also matches non-ASCII digits, which Go passes to Python);
// a snapshot carrying any other pattern is refused rather than guessed at.
const datedSuffixPattern = `-\d{4}-\d{2}-\d{2}$`

var datedSuffix = regexp.MustCompile(datedSuffixPattern)

// Snapshot is a parsed v1 routing snapshot body. Every exported field is a
// field of the wire schema; Parse refuses a body with any other field, a
// missing field, a float, a duplicate key or a lone surrogate.
type Snapshot struct {
	V               int64               `json:"v"`
	Kind            string              `json:"kind"`
	CompiledAt      int64               `json:"compiled_at"`
	NotAfter        int64               `json:"not_after"`
	EpochSeconds    int64               `json:"epoch_seconds"`
	SourceRevision  string              `json:"source_revision"`
	Plane           Plane               `json:"plane"`
	Settings        Settings            `json:"settings"`
	Providers       []Provider          `json:"providers"`
	Models          []Model             `json:"models"`
	Endpoints       []Endpoint          `json:"endpoints"`
	Aliases         Aliases             `json:"aliases"`
	RequestedModels []RequestedModel    `json:"requested_models"`
	DecisionChains  map[string][]string `json:"decision_chains"`
	Ranking         Ranking             `json:"ranking"`
	Fees            Fees                `json:"fees"`

	providers        map[string]*Provider
	models           map[string]*Model
	endpointsByModel map[string][]int
	endpointIndex    map[string]int
	requested        map[string]bool
}

// Plane is the cloud and region a snapshot was compiled for.
type Plane struct {
	Cloud  string `json:"cloud"`
	Region string `json:"region"`
}

type Settings struct {
	Regions                []string      `json:"regions"`
	PrimaryRegion          string        `json:"primary_region"`
	RegionPayload          []RegionEntry `json:"region_payload"`
	StageD                 StageDSetting `json:"stage_d"`
	RequestRecordWriteMode string        `json:"request_record_write_mode"`
}

type RegionEntry struct {
	ID              string  `json:"id"`
	Name            string  `json:"name"`
	Primary         bool    `json:"primary"`
	Enabled         bool    `json:"enabled"`
	APIBaseURL      string  `json:"api_base_url"`
	ControlPlaneURL *string `json:"control_plane_url"`
}

type StageDSetting struct {
	EligibilityEnabled          bool     `json:"eligibility_enabled"`
	HeartbeatEnabled            bool     `json:"heartbeat_enabled"`
	RequireHeartbeatDeclaration bool     `json:"require_heartbeat_declaration"`
	PilotWorkspaces             []string `json:"pilot_workspaces"`
}

type Provider struct {
	Slug                string   `json:"slug"`
	Name                string   `json:"name"`
	HeadquartersCountry *string  `json:"headquarters_country"`
	BYOKStorageSlugs    []string `json:"byok_storage_slugs"`
}

type Model struct {
	ID                 string `json:"id"`
	Provider           string `json:"provider"`
	SupportsChat       bool   `json:"supports_chat"`
	SupportsEmbeddings bool   `json:"supports_embeddings"`
	SupportsDecide     bool   `json:"supports_decide"`
}

type Endpoint struct {
	ID                  string           `json:"id"`
	ModelID             string           `json:"model_id"`
	Provider            string           `json:"provider"`
	UsageType           string           `json:"usage_type"`
	UpstreamModel       string           `json:"upstream_model"`
	SupportedParameters []string         `json:"supported_parameters"`
	InputModalities     []string         `json:"input_modalities"`
	Privacy             Privacy          `json:"privacy"`
	GatewayRegions      GatewayRegions   `json:"gateway_regions"`
	CatalogValidUntil   *int64           `json:"catalog_valid_until"`
	RetiredAt           *int64           `json:"retired_at"`
	Epochs              []Epoch          `json:"epochs"`
	Response            EndpointResponse `json:"response"`
}

type Privacy struct {
	StoresContent bool    `json:"stores_content"`
	Meets         []int64 `json:"meets"`
}

type GatewayRegions struct {
	Only                   *[]string `json:"only"`
	Deny                   []string  `json:"deny"`
	RequiresExplicitOrigin bool      `json:"requires_explicit_origin"`
}

type Epoch struct {
	StartsAt int64    `json:"starts_at"`
	Headline Headline `json:"headline"`
	Price    Price    `json:"price"`
}

// Headline is an endpoint's effective headline rates, which sort=price
// compares.
type Headline struct {
	Prompt     int64 `json:"prompt"`
	Completion int64 `json:"completion"`
}

// Price is exactly stage_d.endpoint_pricing_candidate: the frozen Stage D
// candidate the estimate is computed from.
type Price struct {
	EndpointID          string `json:"endpoint_id"`
	PriceHistoryVersion int64  `json:"price_history_version"`
	Rates               Rates  `json:"rates"`
	Tiers               []Tier `json:"tiers"`
	RequestFeeMicro     int64  `json:"request_fee_micro"`
	Rounding            string `json:"rounding"`
}

type Rates struct {
	InputMicroPerMillion         int64 `json:"input_micro_per_million"`
	CachedInputMicroPerMillion   int64 `json:"cached_input_micro_per_million"`
	CacheCreationMicroPerMillion int64 `json:"cache_creation_micro_per_million"`
	OutputMicroPerMillion        int64 `json:"output_micro_per_million"`
}

type Tier struct {
	MaxPromptTokens *int64 `json:"max_prompt_tokens"`
	Rates           Rates  `json:"rates"`
}

type EndpointResponse struct {
	InferenceLocation InferenceLocation `json:"inference_location"`
	WaferZDRRequired  bool              `json:"wafer_zdr_required"`
}

type InferenceLocation struct {
	AdvertisedRegions             []string `json:"advertised_regions"`
	AdvertisedRegionScope         string   `json:"advertised_region_scope"`
	CatalogUpdatedAt              *string  `json:"catalog_updated_at"`
	ProviderDeclaredLocations     []string `json:"provider_declared_locations"`
	ProviderDeclarationReviewedOn *string  `json:"provider_declaration_reviewed_on"`
	ServingRegion                 *string  `json:"serving_region"`
	ServingRegionStatus           string   `json:"serving_region_status"`
	RegionPinningEnforced         bool     `json:"region_pinning_enforced"`
	DocumentationURL              *string  `json:"documentation_url"`
}

type Aliases struct {
	RouterPrefixRewrites []PrefixRewrite     `json:"router_prefix_rewrites"`
	RouterBaseRewrites   map[string]string   `json:"router_base_rewrites"`
	AliasTargets         map[string]string   `json:"alias_targets"`
	DatedSuffixPattern   string              `json:"dated_suffix_pattern"`
	BareVendorPrefix     string              `json:"bare_vendor_prefix"`
	VariantSuffixes      []VariantSuffix     `json:"variant_suffixes"`
	ProviderAliases      map[string]string   `json:"provider_aliases"`
	ProviderGroupAliases map[string][]string `json:"provider_group_aliases"`
	RouterProviderSlugs  []string            `json:"router_provider_slugs"`
}

type PrefixRewrite struct {
	Prefix      string `json:"prefix"`
	Replacement string `json:"replacement"`
}

type VariantSuffix struct {
	Suffix     string `json:"suffix"`
	Preference string `json:"preference"`
	Value      string `json:"value"`
}

type RequestedModel struct {
	ID            string             `json:"id"`
	ExpandsTo     []string           `json:"expands_to"`
	RefusedStatus *int64             `json:"refused_status"`
	PrivateProxy  bool               `json:"private_proxy"`
	Overrides     RequestedOverrides `json:"overrides"`
}

type RequestedOverrides struct {
	Usage                *string   `json:"usage"`
	RenewableOnly        *[]string `json:"renewable_only"`
	ProviderJurisdiction *string   `json:"provider_jurisdiction"`
	PrivacyRequirements  []int64   `json:"privacy_requirements"`
	Order                *[]string `json:"order"`
	Only                 *[]string `json:"only"`
}

type Ranking struct {
	MeasuredFromUS          int64                       `json:"measured_from_us"`
	MeasuredThroughUS       int64                       `json:"measured_through_us"`
	UnknownRank             int64                       `json:"unknown_rank"`
	Default                 map[string]int64            `json:"default"`
	Latency                 map[string]int64            `json:"latency"`
	Throughput              map[string]int64            `json:"throughput"`
	PrepaidMinRank          map[string]int64            `json:"prepaid_min_rank"`
	ModelProviderPreference map[string]map[string]int64 `json:"model_provider_preference"`
}

type Fees struct {
	StandardMarkupBps        int64  `json:"standard_markup_bps"`
	SignedReceiptTotalFeeBps int64  `json:"signed_receipt_total_fee_bps"`
	ReceiptRounding          string `json:"receipt_rounding"`
	TokenRounding            string `json:"token_rounding"`
}

// Parse decodes a snapshot body without trusting it to be well formed: the
// strict wire rules first (printable ASCII, no duplicate keys, no floats, no
// lone surrogates), then the exact field set of every object, then the
// literals and cross-references the evaluator relies on. It does not check a
// signature; Verify does, and calls Parse on the bytes it has bound.
func Parse(raw []byte) (*Snapshot, error) {
	if err := wireCheck(raw, maxBodyJSONDepth, true); err != nil {
		return nil, fmt.Errorf("routing snapshot body: %w", err)
	}
	var generic any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&generic); err != nil {
		return nil, fmt.Errorf("routing snapshot body: %w", err)
	}
	if err := shape(generic, reflect.TypeFor[Snapshot](), "$"); err != nil {
		return nil, fmt.Errorf("routing snapshot body: %w", err)
	}
	var s Snapshot
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, fmt.Errorf("routing snapshot body: %w", err)
	}
	if err := s.validate(); err != nil {
		return nil, fmt.Errorf("routing snapshot body: %w", err)
	}
	return &s, nil
}

// wireCheck applies the strict JSON rules that encoding/json does not:
// printable ASCII bytes only (the canonical encoding is ensure_ascii, with
// no newline), bounded nesting, integer numbers only, no duplicate object
// keys, and no lone surrogate escapes. backslashes says whether string
// escapes may appear at all (the JWS header and claims allow none).
func wireCheck(raw []byte, maxDepth int, backslashes bool) error {
	for _, b := range raw {
		if b < 0x20 || b > 0x7e || (!backslashes && b == '\\') {
			return errors.New("json: bytes outside printable ASCII")
		}
	}
	if err := surrogates(raw); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	type frame struct {
		object bool
		keys   map[string]bool
		expect bool // the next string token in this object is a key
	}
	var stack []*frame
	values := 0
	for {
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return fmt.Errorf("json: %w", err)
		}
		if len(stack) == 0 {
			if values++; values > 1 {
				return errors.New("json: trailing data")
			}
		}
		var top *frame
		if len(stack) > 0 {
			top = stack[len(stack)-1]
		}
		isKey := top != nil && top.object && top.expect
		switch value := token.(type) {
		case json.Delim:
			switch value {
			case '{', '[':
				if top != nil && top.object {
					top.expect = true
				}
				stack = append(stack, &frame{object: value == '{', keys: map[string]bool{}, expect: value == '{'})
				if len(stack) > maxDepth {
					return errors.New("json: nested too deeply")
				}
			case '}', ']':
				stack = stack[:len(stack)-1]
				if len(stack) > 0 && stack[len(stack)-1].object {
					stack[len(stack)-1].expect = true
				}
			}
			continue
		case string:
			if isKey {
				if top.keys[value] {
					return fmt.Errorf("json: duplicate key %q", value)
				}
				top.keys[value] = true
				top.expect = false
				continue
			}
		case json.Number:
			if strings.ContainsAny(string(value), ".eE") {
				return fmt.Errorf("json: %s is not an integer", value)
			}
		}
		if top != nil && top.object {
			top.expect = true
		}
	}
	if values != 1 || len(stack) != 0 {
		return errors.New("json: not one value")
	}
	return nil
}

// surrogates refuses a \u escape of a lone UTF-16 surrogate: Python's strict
// decoder rejects the non-scalar string it would produce, and encoding/json
// would silently replace it with U+FFFD.
func surrogates(raw []byte) error {
	inString := false
	pendingHigh := false
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if !inString {
			if c == '"' {
				inString = true
			}
			continue
		}
		switch c {
		case '"':
			if pendingHigh {
				return errors.New("json: lone surrogate")
			}
			inString = false
		case '\\':
			if i+1 >= len(raw) {
				return errors.New("json: truncated escape")
			}
			if raw[i+1] != 'u' {
				if pendingHigh {
					return errors.New("json: lone surrogate")
				}
				i++
				continue
			}
			if i+5 >= len(raw) {
				return errors.New("json: truncated escape")
			}
			var code int
			if _, err := fmt.Sscanf(string(raw[i+2:i+6]), "%04x", &code); err != nil {
				return errors.New("json: bad escape")
			}
			i += 5
			switch {
			case code >= 0xd800 && code <= 0xdbff:
				if pendingHigh {
					return errors.New("json: lone surrogate")
				}
				pendingHigh = true
			case code >= 0xdc00 && code <= 0xdfff:
				if !pendingHigh {
					return errors.New("json: lone surrogate")
				}
				pendingHigh = false
			default:
				if pendingHigh {
					return errors.New("json: lone surrogate")
				}
			}
		default:
			if pendingHigh {
				return errors.New("json: lone surrogate")
			}
		}
	}
	return nil
}

// shape holds a generically decoded value to a Go type: every struct is an
// object with exactly its exported fields' JSON names, null only where the
// field is a pointer, and every scalar of its declared kind.
func shape(value any, t reflect.Type, path string) error {
	if t.Kind() == reflect.Pointer {
		if value == nil {
			return nil
		}
		return shape(value, t.Elem(), path)
	}
	if value == nil {
		return fmt.Errorf("%s: null where a value is required", path)
	}
	switch t.Kind() {
	case reflect.Struct:
		object, ok := value.(map[string]any)
		if !ok {
			return fmt.Errorf("%s: not an object", path)
		}
		names := map[string]bool{}
		for i := range t.NumField() {
			field := t.Field(i)
			if !field.IsExported() {
				continue
			}
			name := strings.Split(field.Tag.Get("json"), ",")[0]
			names[name] = true
			item, present := object[name]
			if !present {
				return fmt.Errorf("%s.%s: missing", path, name)
			}
			if err := shape(item, field.Type, path+"."+name); err != nil {
				return err
			}
		}
		for _, name := range slices.Sorted(maps.Keys(object)) {
			if !names[name] {
				return fmt.Errorf("%s.%s: unknown field", path, name)
			}
		}
	case reflect.Slice:
		list, ok := value.([]any)
		if !ok {
			return fmt.Errorf("%s: not an array", path)
		}
		for i, item := range list {
			if err := shape(item, t.Elem(), fmt.Sprintf("%s[%d]", path, i)); err != nil {
				return err
			}
		}
	case reflect.Map:
		object, ok := value.(map[string]any)
		if !ok {
			return fmt.Errorf("%s: not an object", path)
		}
		for key, item := range object {
			if err := shape(item, t.Elem(), path+"."+key); err != nil {
				return err
			}
		}
	case reflect.String:
		if _, ok := value.(string); !ok {
			return fmt.Errorf("%s: not a string", path)
		}
	case reflect.Bool:
		if _, ok := value.(bool); !ok {
			return fmt.Errorf("%s: not a boolean", path)
		}
	case reflect.Int64:
		number, ok := value.(json.Number)
		if !ok {
			return fmt.Errorf("%s: not an integer", path)
		}
		if _, err := number.Int64(); err != nil {
			return fmt.Errorf("%s: %s is not a 64-bit integer", path, number)
		}
	default:
		return fmt.Errorf("%s: unsupported kind %s", path, t.Kind())
	}
	return nil
}

func sortedUnique(values []string) bool {
	for i := 1; i < len(values); i++ {
		if values[i-1] >= values[i] {
			return false
		}
	}
	return true
}

func nonNegativeRates(r Rates) bool {
	return r.InputMicroPerMillion >= 0 && r.OutputMicroPerMillion >= 0 &&
		r.CachedInputMicroPerMillion >= 0 && r.CacheCreationMicroPerMillion >= 0
}

// validate checks the literals and references the evaluator relies on, and
// builds its indexes. A snapshot it refuses is never evaluated: every
// request then goes to Python.
func (s *Snapshot) validate() error {
	if s.V != snapshotVersion || s.Kind != snapshotKind || s.EpochSeconds != epochSeconds {
		return errors.New("unsupported version, kind or epoch")
	}
	if s.CompiledAt < 0 || !(s.CompiledAt < s.NotAfter && s.NotAfter <= s.CompiledAt+maxHorizon) {
		return errors.New("not_after must follow compiled_at within the horizon")
	}
	if !slices.Contains(s.Settings.Regions, s.Settings.PrimaryRegion) {
		return errors.New("regions must include primary_region")
	}
	if s.Fees.ReceiptRounding != receiptRounding || s.Fees.TokenRounding != pricingRounding ||
		s.Fees.StandardMarkupBps < 0 || s.Fees.SignedReceiptTotalFeeBps < 0 {
		return errors.New("unsupported fees")
	}
	a := s.Aliases
	if a.DatedSuffixPattern != datedSuffixPattern {
		return fmt.Errorf("unsupported dated_suffix_pattern %q", a.DatedSuffixPattern)
	}
	for _, v := range a.VariantSuffixes {
		if v.Preference != "sort" || !slices.Contains([]string{"price", "latency", "throughput"}, v.Value) || v.Suffix == "" {
			return fmt.Errorf("unsupported variant suffix %q", v.Suffix)
		}
	}
	for _, r := range a.RouterPrefixRewrites {
		if r.Prefix == "" {
			return errors.New("empty router prefix rewrite")
		}
	}
	if !sortedUnique(a.RouterProviderSlugs) {
		return errors.New("router_provider_slugs must be sorted and unique")
	}
	s.providers = make(map[string]*Provider, len(s.Providers))
	slugs := make([]string, len(s.Providers))
	for i := range s.Providers {
		slugs[i] = s.Providers[i].Slug
		s.providers[s.Providers[i].Slug] = &s.Providers[i]
	}
	if !sortedUnique(slugs) {
		return errors.New("providers must be sorted and unique")
	}
	s.models = make(map[string]*Model, len(s.Models))
	for i := range s.Models {
		m := &s.Models[i]
		if s.models[m.ID] != nil || s.providers[m.Provider] == nil {
			return fmt.Errorf("model %q: duplicate or unknown provider", m.ID)
		}
		s.models[m.ID] = m
	}
	s.endpointsByModel = make(map[string][]int, len(s.Models))
	s.endpointIndex = make(map[string]int, len(s.Endpoints))
	for i := range s.Endpoints {
		e := &s.Endpoints[i]
		if _, dup := s.endpointIndex[e.ID]; dup || s.models[e.ModelID] == nil || s.providers[e.Provider] == nil {
			return fmt.Errorf("endpoint %q: duplicate, or unknown model or provider", e.ID)
		}
		s.endpointIndex[e.ID] = i
		if e.UsageType != usageCredits && e.UsageType != usageBYOK {
			return fmt.Errorf("endpoint %q: usage_type %q", e.ID, e.UsageType)
		}
		if len(e.InputModalities) == 0 || len(e.Epochs) == 0 || e.Epochs[0].StartsAt != s.CompiledAt ||
			e.Epochs[len(e.Epochs)-1].StartsAt >= s.NotAfter {
			return fmt.Errorf("endpoint %q: epochs must cover compiled_at and end before not_after", e.ID)
		}
		for j, epoch := range e.Epochs {
			p := epoch.Price
			if j > 0 && epoch.StartsAt <= e.Epochs[j-1].StartsAt {
				return fmt.Errorf("endpoint %q: epochs must increase", e.ID)
			}
			if p.EndpointID != e.ID || p.PriceHistoryVersion != 1 || p.Rounding != pricingRounding ||
				p.RequestFeeMicro < 0 || !nonNegativeRates(p.Rates) {
				return fmt.Errorf("endpoint %q: unsupported price", e.ID)
			}
			for _, tier := range p.Tiers {
				if (tier.MaxPromptTokens != nil && *tier.MaxPromptTokens < 0) || !nonNegativeRates(tier.Rates) {
					return fmt.Errorf("endpoint %q: unsupported tier", e.ID)
				}
			}
		}
		s.endpointsByModel[e.ModelID] = append(s.endpointsByModel[e.ModelID], i)
	}
	s.requested = make(map[string]bool, len(s.RequestedModels))
	for _, r := range s.RequestedModels {
		for _, id := range r.ExpandsTo {
			if s.models[id] == nil {
				return fmt.Errorf("requested model %q expands to an unknown model", r.ID)
			}
		}
		s.requested[r.ID] = true
	}
	for id, chain := range s.DecisionChains {
		for _, p := range chain {
			if s.providers[p] == nil {
				return fmt.Errorf("decision chain %q names an unknown provider", id)
			}
		}
	}
	return nil
}
