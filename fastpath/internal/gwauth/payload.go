package gwauth

import (
	"fmt"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// payloadMembers is the authorization payload but its two front-door keys
// (fastpath_envelope, fastpath_end_of_life), which Finish adds: every key
// json_body(GatewayAuthorization) writes for the cohort (§5,
// storage_codec.py:25-37, storage_models.py:887-990). json_body drops a
// field that is None with a None default (invocation_nonce when the body
// sends none, and every field §5 lists as absent) and an empty list or
// dict with an empty default factory (tags when the merged map is empty);
// a field with any other default is always written. The order here is
// immaterial: Canonical sorts.
func payloadMembers(in ComposeInput, tags []ComposeTag) pyjson.Object {
	first := in.Candidates[0]
	models := make([]string, len(in.Candidates))
	endpoints := make([]string, len(in.Candidates))
	for i, c := range in.Candidates {
		models[i], endpoints[i] = c.Model, c.EndpointID
	}
	p := pyjson.Object{
		{Key: "id", Value: in.AuthorizationID},
		{Key: "workspace_id", Value: in.WorkspaceID},
		{Key: "key_hash", Value: in.KeyHash},
		{Key: "model_id", Value: first.Model},
		{Key: "provider", Value: first.Provider},
		{Key: "endpoint_id", Value: first.EndpointID},
		{Key: "usage_type", Value: usageCredits},
		{Key: "estimated_microdollars", Value: in.Estimate},
		{Key: "credit_reservation_id", Value: in.ReservationID},
		{Key: "settled", Value: false},
		{Key: "created_at", Value: pyjson.ISOFormat(in.At)},
		{Key: "requested_model_id", Value: in.RequestedModel},
		{Key: "candidate_model_ids", Value: models},
		{Key: "candidate_endpoint_ids", Value: endpoints},
		{Key: "region", Value: in.Region},
		{Key: "idempotency_key", Value: in.IdempotencyKey},
		{Key: "idempotency_fingerprint", Value: in.IdempotencyFingerprint},
		{Key: "app_id", Value: ""},
		{Key: "app_owner_user_id", Value: ""},
		{Key: "custom_model_owner_user_id", Value: ""},
		{Key: "app_markup_basis_points", Value: int64(0)},
		{Key: "receipt_fee_basis_points", Value: int64(0)},
		{Key: "custom_model_markup_basis_points", Value: int64(0)},
		{Key: "additional_cost_reservation_microdollars", Value: int64(0)},
		{Key: "native_batch_eligible", Value: false},
		{Key: "settlement", Value: "local"},
		{Key: "stage_d_reason", Value: in.StageDReason},
		{Key: "stage_d_prompt_tokens", Value: in.EstimatedInputTokens},
		{Key: "stage_d_max_output_tokens", Value: in.OutputEstimate},
		{Key: "stage_d_boot_kid", Value: in.BootKid},
	}
	if len(tags) > 0 {
		p = append(p, pyjson.Member{Key: "tags", Value: tagsObject(tags)})
	}
	if in.InvocationNonce != nil {
		p = append(p, pyjson.Member{Key: "invocation_nonce", Value: *in.InvocationNonce})
	}
	return p
}

// EndOfLifeText is the payload's fastpath_end_of_life for the owner's end
// of life (§5): what Python's _parse_time and isoformat make of the
// instant Go's JSON writes (fastpath_frontdoor.py _parse_time, gateway.py
// `admitted.end_of_life.isoformat().replace("+00:00", "Z")`). The fraction
// is cut, not rounded, to microseconds and written as six digits, or not
// at all when they are zero; the instant keeps its own UTC offset, written
// ±HH:MM (±HH:MM:SS when it has seconds), and a zero offset is "Z".
func EndOfLifeText(t time.Time) string {
	t = t.Truncate(time.Microsecond)
	out := fmt.Sprintf("%04d-%02d-%02dT%02d:%02d:%02d",
		t.Year(), int(t.Month()), t.Day(), t.Hour(), t.Minute(), t.Second())
	if micro := t.Nanosecond() / 1000; micro != 0 {
		out += fmt.Sprintf(".%06d", micro)
	}
	_, offset := t.Zone()
	if offset == 0 {
		return out + "Z"
	}
	sign := byte('+')
	if offset < 0 {
		sign, offset = '-', -offset
	}
	out += fmt.Sprintf("%c%02d:%02d", sign, offset/3600, offset/60%60)
	if s := offset % 60; s != 0 {
		out += fmt.Sprintf(":%02d", s)
	}
	return out
}
