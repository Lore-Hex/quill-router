package gwauth

import (
	"bytes"
	"strconv"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// inferenceLocationKeys is inference_location_metadata's literal key order
// (provider_locations.py:419-429). The snapshot body is sorted, so the
// order is Go's to restore.
var inferenceLocationKeys = [...]string{
	"advertised_regions",
	"advertised_region_scope",
	"catalog_updated_at",
	"provider_declared_locations",
	"provider_declaration_reviewed_on",
	"serving_region",
	"serving_region_status",
	"region_pinning_enforced",
	"documentation_url",
}

// inferenceLocation re-emits the snapshot's object in Python's order. An
// object with a key missing, extra or twice is not one Python's function
// wrote, so it is refused.
func inferenceLocation(snapshot pyjson.Object) (pyjson.Object, error) {
	if len(snapshot) != len(inferenceLocationKeys) {
		return nil, composeErr("inference_location has %d keys, not %d", len(snapshot), len(inferenceLocationKeys))
	}
	out := make(pyjson.Object, 0, len(inferenceLocationKeys))
	for _, key := range inferenceLocationKeys {
		found := false
		for _, m := range snapshot {
			if m.Key == key {
				if found {
					return nil, composeErr("inference_location has %q twice", key)
				}
				found = true
				out = append(out, m)
			}
		}
		if !found {
			return nil, composeErr("inference_location has no %q", key)
		}
	}
	return out, nil
}

// byokNulls is _gateway_byok_payload with no BYOK config: the cohort has
// none (decision 6).
var byokNulls = pyjson.Object{
	{Key: "byok_secret_ref", Value: nil},
	{Key: "byok_encrypted_secret", Value: nil},
	{Key: "byok_cache_key", Value: nil},
	{Key: "byok_key_hint", Value: nil},
	{Key: "byok_provider", Value: nil},
}

// providerMembers are the members the response and each route candidate
// share after upstream_model: provider, provider_name, inference_location
// and the optional wafer_zdr_required (gateway.py _gateway_candidate_payload,
// _gateway_provider_route_payload).
func providerMembers(c Candidate, location pyjson.Object) pyjson.Object {
	o := pyjson.Object{
		{Key: "provider", Value: c.Provider},
		{Key: "provider_name", Value: c.ProviderName},
		{Key: "inference_location", Value: location},
	}
	if c.WaferZDRRequired {
		o = append(o, pyjson.Member{Key: "wafer_zdr_required", Value: true})
	}
	return o
}

// responseData is _gateway_authorize_response's data for the cohort
// (gateway.py, §6), every member in insertion order, but timing, which
// the timing decorator appends last.
func responseData(in ComposeInput, generationID string, tags []ComposeTag) (pyjson.Object, error) {
	locations := make([]pyjson.Object, len(in.Candidates))
	for i, c := range in.Candidates {
		loc, err := inferenceLocation(c.InferenceLocation)
		if err != nil {
			return nil, err
		}
		locations[i] = loc
	}
	first := in.Candidates[0]

	d := pyjson.Object{
		{Key: "authorization_id", Value: in.AuthorizationID},
		{Key: "generation_id", Value: generationID},
		{Key: "workspace_id", Value: in.WorkspaceID},
		{Key: "api_key_hash", Value: in.KeyHash},
		{Key: "model", Value: first.Model},
		{Key: "upstream_model", Value: first.UpstreamModel},
		{Key: "endpoint_id", Value: first.EndpointID},
	}
	d = append(d, providerMembers(first, locations[0])...)
	d = append(d,
		pyjson.Member{Key: "requested_model", Value: in.RequestedModel},
		pyjson.Member{Key: "response_model", Value: nil},
		pyjson.Member{Key: "hide_public_metadata", Value: false},
		pyjson.Member{Key: "usage_type", Value: usageCredits},
		pyjson.Member{Key: "limit_usage_type", Value: usageCredits},
		// money_pair: float(m) / 1_000_000, whose float the 200 writes
		// in pydantic-core's format (DumpsPydantic).
		pyjson.Member{Key: "estimated_cost", Value: float64(in.Estimate) / 1_000_000},
		pyjson.Member{Key: "estimated_cost_microdollars", Value: in.Estimate},
		pyjson.Member{Key: "credit_reservation_id", Value: in.ReservationID},
	)
	d = append(d, byokNulls...)
	regions := make([]any, len(in.Regions))
	for i, r := range in.Regions {
		var controlPlane any
		if r.ControlPlaneURL != nil {
			controlPlane = *r.ControlPlaneURL
		}
		regions[i] = pyjson.Object{
			{Key: "id", Value: r.ID},
			{Key: "name", Value: r.Name},
			{Key: "primary", Value: r.Primary},
			{Key: "enabled", Value: r.Enabled},
			{Key: "api_base_url", Value: r.APIBaseURL},
			{Key: "control_plane_url", Value: controlPlane},
		}
	}
	d = append(d,
		pyjson.Member{Key: "content_storage_enabled", Value: false},
		pyjson.Member{Key: "region", Value: in.Region},
		pyjson.Member{Key: "regions", Value: regions},
		pyjson.Member{Key: "broadcast_destinations", Value: []any{}},
		pyjson.Member{Key: "idempotent_replay", Value: false},
		pyjson.Member{Key: "additional_cost_reservation_microdollars", Value: int64(0)},
		pyjson.Member{Key: "receipt_fee_basis_points", Value: int64(0)},
		// billing_pricing_snapshot is None for a row with no pricing
		// snapshot.
		pyjson.Member{Key: "candidate_cost_reporting", Value: false},
		pyjson.Member{Key: "request_metadata_version", Value: int64(1)},
		pyjson.Member{Key: "native_batch_eligible", Value: false},
		pyjson.Member{Key: "stage_d", Value: pyjson.Object{
			{Key: "eligible", Value: false},
			{Key: "reason", Value: in.StageDReason},
		}},
		pyjson.Member{Key: "tags", Value: tagsObject(tags)},
		pyjson.Member{Key: "custom_model", Value: nil},
	)
	candidates := make([]any, len(in.Candidates))
	for i, c := range in.Candidates {
		o := pyjson.Object{
			{Key: "endpoint_id", Value: c.EndpointID},
			{Key: "model", Value: c.Model},
			{Key: "upstream_model", Value: c.UpstreamModel},
		}
		o = append(o, providerMembers(c, locations[i])...)
		o = append(o, pyjson.Member{Key: "usage_type", Value: usageCredits})
		o = append(o, byokNulls...)
		o = append(o, pyjson.Member{Key: "region", Value: in.Region})
		candidates[i] = o
	}
	d = append(d, pyjson.Member{Key: "route_candidates", Value: candidates})
	return d, nil
}

// renderHead writes {"data": data} with DumpsPydantic, the 200's encoder
// (decision 13), and cuts the closing "}}" so Render can append timing as
// data's last member. Any DumpsPydantic error is Stage's: the caller
// passes.
func renderHead(data pyjson.Object) ([]byte, error) {
	b, err := pyjson.DumpsPydantic(pyjson.Object{{Key: "data", Value: data}})
	if err != nil {
		return nil, composeErr("the 200 body: %v", err)
	}
	head, ok := bytes.CutSuffix(b, []byte("}}"))
	if !ok {
		return nil, composeErr("the 200 body does not end with data's close")
	}
	return head, nil
}

// Render is the 200's bytes: the body Stage encoded, then data.timing in
// §6's key order, appended last as the timing decorator appends it. After
// a read-back (an Unknown insert that landed, or Landed) Python answers
// from the row it read, whose tags decode from the sorted payload, so
// readBack writes tags sorted; every other byte is the same.
func (c Composed) Render(t Timing, readBack bool) []byte {
	head := c.inserted
	if readBack {
		head = c.readBack
	}
	out := make([]byte, 0, len(head)+160)
	out = append(out, head...)
	out = append(out, `,"timing":`...)
	out = appendTiming(out, t)
	return append(out, "}}"...)
}

// The 503 for an insert whose outcome is unknown and that a read-back did
// not find (decision 1): api_error's body as Starlette's JSONResponse
// writes it (json.dumps, ensure_ascii off, compact), extended by the
// timing decorator with a top-level data.timing beside error.
const (
	UnavailableStatus     = 503
	UnavailableRetryAfter = "1"
)

// unavailableError is api_error(503, "Authorization unavailable; retry",
// "service_unavailable")'s error object (gateway.py:1794-1807).
var unavailableError = pyjson.Object{
	{Key: "code", Value: int64(503)},
	{Key: "message", Value: "Authorization unavailable; retry"},
	{Key: "type", Value: "service_unavailable"},
	{Key: "source", Value: "router"},
}

// unavailableHead is the 503 body up to data.timing's value. json.dumps
// writes the error object as Dumps does, and with these values it cannot
// fail; it is encoded once, when the package loads, so a request never
// can.
var unavailableHead = func() []byte {
	b, err := pyjson.Dumps(pyjson.Object{{Key: "error", Value: unavailableError}}, pyjson.Options{})
	if err != nil {
		panic("gwauth: the 503 body: " + err.Error())
	}
	return append(b[:len(b)-1:len(b)-1], `,"data":{"timing":`...) // reopen the top-level object
}()

// Unavailable is decision 1's 503 body, timing in the top-level data.
func Unavailable(t Timing) []byte {
	out := make([]byte, 0, len(unavailableHead)+160)
	out = append(out, unavailableHead...)
	out = appendTiming(out, t)
	return append(out, "}}"...)
}

// appendTiming writes GatewayTiming.snapshot's six integers in its order
// (gateway_timing.py:54-60). Integers are the same bytes under json.dumps
// and pydantic-core.
func appendTiming(dst []byte, t Timing) []byte {
	for i, m := range [...]struct {
		key string
		v   int64
	}{
		{"total_ms", t.TotalMS},
		{"spanner_rpcs", t.SpannerRPCs},
		{"key_lookup_ms", t.KeyLookupMS},
		{"routing_ms", t.RoutingMS},
		{"store_ms", t.StoreMS},
		{"post_commit_ms", t.PostCommitMS},
	} {
		if i == 0 {
			dst = append(dst, '{')
		} else {
			dst = append(dst, ',')
		}
		dst = append(dst, '"')
		dst = append(dst, m.key...)
		dst = append(dst, `":`...)
		dst = strconv.AppendInt(dst, m.v, 10)
	}
	return append(dst, '}')
}
