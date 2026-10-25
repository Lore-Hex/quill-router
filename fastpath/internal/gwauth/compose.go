package gwauth

import (
	"errors"
	"fmt"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// Compose (G6 of docs/design/fast-admission-e2-decide.md, §5, §6 and §8.3)
// turns an admission the edge decided into the two rows Python's leased
// authorize writes and the 200 body Python answers, byte for byte:
//
//	staged, err := Stage(in)                     // before the admit; err passes
//	composed, err := staged.Finish(env, eol)     // after Admitted; err refunds and passes
//	body := composed.Render(timing, readBack)    // after the insert answered
//
// Every error means the edge cannot write what Python would have written:
// the caller passes the request to Python, never answers 500 itself. Stage
// does every encoding the 200 needs, so Render cannot fail.
//
// Nothing outside tests imports this package yet: the edge's decide
// pipeline (G10) is its first caller.

// ComposeInput is everything Stage needs, as plain values.
//
// It is §8.3's ComposeInput with the request side (G5's Request and
// Identity, built in parallel) given as the plain fields Stage reads; the
// decide pipeline copies them from Request and Identity. The envelope and
// its end of life arrive with Finish, after the admit.
type ComposeInput struct {
	At              time.Time // the one clock reading (decision 15)
	AuthorizationID string    // "gwa-" + a uuid4's 32 lowercase hex digits
	ReservationID   string    // hyphenated uuid4

	// From Identity.
	IdempotencyKey         string // presented (stripped) or minted
	IdempotencyFingerprint string // §7
	IdempotencyScope       string // workspace#key_hash#sha256(idempotency key)

	WorkspaceID string
	KeyHash     string
	KeyTags     []ComposeTag // the key's stored tags, stored order

	// From Request.
	RequestedModel       string       // body.model, stripped
	RequestTags          []ComposeTag // body.tags, body order
	InvocationNonce      *string      // nil when the body sends none
	EstimatedInputTokens int64        // 1 when omitted
	OutputEstimate       int64        // max_tokens, max_completion_tokens, max_output_tokens, else 512

	Region       string // choose_region(body.region)
	Regions      []Region
	Candidates   []Candidate // in order; every one Credits
	Estimate     int64       // Result.ModelEstimate, microdollars
	StageDReason string      // StageDReasonOf
	BootKid      string
}

// ComposeTag is one tag, key and value. It is not G5's Tag (built in
// parallel) nor gwstate's Pair (nothing imports gwstate yet); both convert
// to it, ComposeTag(t), having the same fields.
type ComposeTag struct{ Key, Value string }

// Region is one entry of the routing snapshot's settings.region_payload.
type Region struct {
	ID, Name        string
	Primary         bool
	Enabled         bool
	APIBaseURL      string
	ControlPlaneURL *string
}

// Candidate is one route candidate as the snapshot describes its endpoint.
type Candidate struct {
	EndpointID, Model, UpstreamModel, Provider, ProviderName string
	InferenceLocation                                        pyjson.Object // the snapshot's (sorted); Stage re-orders it
	WaferZDRRequired                                         bool
}

// Composed is an admitted authorization's rows and its 200 body but the
// timing.
type Composed struct {
	GenerationID  string
	Authorization AuthorizationRow
	Reservation   ReservationRow

	// The 200 body up to the value of data.timing: data's members as
	// inserted, tags in merge order (inserted) and in sorted order (after
	// a read-back, §6).
	inserted, readBack []byte
}

// Timing is §6 item 15, in that key order.
type Timing struct {
	TotalMS, SpannerRPCs, KeyLookupMS, RoutingMS, StoreMS, PostCommitMS int64
}

// The §5 constants Go writes into both rows.
const (
	usageCredits = "Credits"
	// reservationTTL is GATEWAY_RESERVATION_TTL_SECONDS (user_model_rules.py).
	reservationTTL = 7200 * time.Second
)

// ErrCompose is the error every Stage and Finish refusal wraps.
var ErrCompose = errors.New("gwauth: cannot compose as Python would")

func composeErr(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrCompose, fmt.Sprintf(format, args...))
}

// mergeTags is merge_tags(key.tags, body.tags) (request_tags.py:65-82):
// the key's in stored order, then the request's in body order, a key seen
// again overwriting the value where it first stood, as a dict update does.
// Validating the merged map is the gate's (tags.merged_invalid), not
// Stage's.
func mergeTags(keyTags, requestTags []ComposeTag) []ComposeTag {
	merged := make([]ComposeTag, 0, len(keyTags)+len(requestTags))
	at := make(map[string]int, cap(merged))
	for _, list := range [][]ComposeTag{keyTags, requestTags} {
		for _, t := range list {
			if i, ok := at[t.Key]; ok {
				merged[i].Value = t.Value
				continue
			}
			at[t.Key] = len(merged)
			merged = append(merged, t)
		}
	}
	return merged
}

// tagsObject is a tag list as the JSON object Python's dict writes.
func tagsObject(tags []ComposeTag) pyjson.Object {
	o := make(pyjson.Object, 0, len(tags))
	for _, t := range tags {
		o = append(o, pyjson.Member{Key: t.Key, Value: t.Value})
	}
	return o
}
