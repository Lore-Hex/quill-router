package gwauth

import (
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// Staged is everything but the front door's envelope, built before the
// admit so that nothing after Admitted but the payload's encoding can
// refuse.
type Staged struct {
	in           ComposeInput
	generationID string
	payload      pyjson.Object // without the two front-door keys
	inserted     []byte
	readBack     []byte
}

// Stage builds the rows' fields and encodes the 200 body but its timing.
// An error passes the request to Python (it happens before the admit, so
// nothing needs refunding).
func Stage(in ComposeInput) (Staged, error) {
	if !authorizationIDForm.MatchString(in.AuthorizationID) {
		return Staged{}, composeErr("authorization id %q is not gwa- and a uuid4's 32 lowercase hex digits", in.AuthorizationID)
	}
	if !reservationIDForm.MatchString(in.ReservationID) {
		return Staged{}, composeErr("reservation id %q is not a hyphenated uuid4", in.ReservationID)
	}
	if len(in.Candidates) == 0 {
		return Staged{}, composeErr("no route candidates")
	}
	if in.At.IsZero() {
		return Staged{}, composeErr("no clock reading")
	}
	// One clock reading (decision 15), as a Python datetime holds it: UTC,
	// to the microsecond. The authorization's created_at, the payload's
	// created_at and expires_at all derive from it; the reservation's
	// created_at is the commit's timestamp (ReservationRow).
	in.At = in.At.UTC().Truncate(time.Microsecond)
	in.Candidates = slices.Clone(in.Candidates)
	in.Regions = slices.Clone(in.Regions)

	generationID := GenerationID(in.AuthorizationID)
	tags := mergeTags(in.KeyTags, in.RequestTags)
	data, err := responseData(in, generationID, tags)
	if err != nil {
		return Staged{}, err
	}
	inserted, err := renderHead(data)
	if err != nil {
		return Staged{}, err
	}
	// After a read-back Python's tags are the payload's, which json_body
	// sorted; everything else is the same.
	sorted := slices.Clone(tags)
	slices.SortFunc(sorted, func(a, b ComposeTag) int { return strings.Compare(a.Key, b.Key) })
	for i, m := range data {
		if m.Key == "tags" {
			data = slices.Clone(data)
			data[i].Value = tagsObject(sorted)
			break
		}
	}
	readBack, err := renderHead(data)
	if err != nil {
		return Staged{}, err
	}
	return Staged{
		in:           in,
		generationID: generationID,
		payload:      payloadMembers(in, tags),
		inserted:     inserted,
		readBack:     readBack,
	}, nil
}

// Input is what Stage was given, its instant as Python holds it.
func (s Staged) Input() ComposeInput { return s.in }

// Finish adds the front door's envelope and end of life to the payload,
// encodes it as json_body does (sorted, ASCII, compact) and returns the two
// rows and the body. An error, after Admitted, refunds and passes.
func (s Staged) Finish(envelope string, endOfLife time.Time) (Composed, error) {
	if s.payload == nil {
		return Composed{}, composeErr("Finish on a Staged that Stage did not return")
	}
	if !utf8.ValidString(envelope) {
		return Composed{}, composeErr("the envelope is not UTF-8")
	}
	if endOfLife.IsZero() {
		return Composed{}, composeErr("no end of life")
	}
	// An offset with seconds is one Python never writes back as Go does:
	// Go's JSON drops the offset's seconds (RFC 3339 has none), so the text
	// Python parses names another instant. The front door answers in UTC;
	// anything else passes rather than write a payload Python would not.
	if _, offset := endOfLife.Zone(); offset%60 != 0 {
		return Composed{}, composeErr("the end of life's UTC offset has seconds")
	}
	payload := append(slices.Clone(s.payload),
		pyjson.Member{Key: "fastpath_envelope", Value: envelope},
		pyjson.Member{Key: "fastpath_end_of_life", Value: EndOfLifeText(endOfLife)},
	)
	encoded, err := pyjson.Canonical(payload)
	if err != nil {
		return Composed{}, composeErr("the payload: %v", err)
	}
	in := s.in
	return Composed{
		GenerationID: s.generationID,
		Authorization: AuthorizationRow{
			AuthorizationID:        in.AuthorizationID,
			WorkspaceID:            in.WorkspaceID,
			KeyHash:                in.KeyHash,
			ReservationID:          in.ReservationID,
			ModelID:                in.Candidates[0].Model,
			Provider:               in.Candidates[0].Provider,
			UsageType:              usageCredits,
			EstimatedMicrodollars:  in.Estimate,
			CreatedAt:              in.At,
			Payload:                string(encoded),
			IdempotencyFingerprint: in.IdempotencyFingerprint,
			StageDBootKid:          in.BootKid,
			InvocationNonce:        cloneString(in.InvocationNonce),
		},
		Reservation: ReservationRow{
			ReservationID:          in.ReservationID,
			WorkspaceID:            in.WorkspaceID,
			KeyHash:                in.KeyHash,
			HoldUsageType:          usageCredits,
			AuthorizationID:        in.AuthorizationID,
			IdempotencyScope:       in.IdempotencyScope,
			IdempotencyFingerprint: in.IdempotencyFingerprint,
			ExpiresAt:              in.At.Add(reservationTTL),
		},
		inserted: s.inserted,
		readBack: s.readBack,
	}, nil
}

func cloneString(s *string) *string {
	if s == nil {
		return nil
	}
	v := *s
	return &v
}

// StageDSettings are the edge policy's live Stage D settings (§4's
// stage_d object).
type StageDSettings struct {
	EligibilityEnabled          bool
	HeartbeatEnabled            bool
	RequireHeartbeatDeclaration bool
	PilotWorkspaces             []string
}

// StageDReasonOf is _stage_d_eligibility_reason (gateway.py) for a request
// in the cohort, with the policy's settings and the boot's
// declares_usage_heartbeat (§6). The cohort has an accepted boot and never
// streams, so the reason is one of the five below; boot_not_accepted
// cannot occur, and nothing after not_streaming is reached.
func StageDReasonOf(s StageDSettings, workspaceID string, bootDeclaresHeartbeat bool) string {
	switch {
	case !s.EligibilityEnabled:
		return "stage_d_disabled"
	case len(s.PilotWorkspaces) > 0 && !slices.Contains(s.PilotWorkspaces, workspaceID):
		return "workspace_not_pilot"
	case !s.HeartbeatEnabled:
		return "heartbeats_disabled"
	case s.RequireHeartbeatDeclaration && !bootDeclaresHeartbeat:
		return "heartbeat_undeclared"
	default:
		return "not_streaming"
	}
}
