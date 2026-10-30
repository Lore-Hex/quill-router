package routingsnapshot

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"slices"
)

// The edge policy's literals (src/trusted_router/edge_policy.py and
// routing_snapshot_signing.py, EDGE_POLICY_TYP; docs/design/
// fast-admission-e2-decide.md section 4). The routing snapshot's key signs it
// under its own typ, so each verifier refuses the other's manifest.
const (
	PolicyTyp             = "tr-edge-policy-v1"
	PolicyKind            = "tr-edge-policy"
	PolicyRefreshSeconds  = 15
	PolicyLifetimeSeconds = 60
	policyVersion         = 1
	maxPolicyJSONDepth    = 8
)

var (
	imageDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	workspaceID = regexp.MustCompile(`^[A-Za-z0-9_./:@+\-]{1,64}$`)
	regionID    = regexp.MustCompile(`^[a-z0-9\-]{1,32}$`)
	policyName  = regexp.MustCompile(`^[A-Za-z0-9_.:\-]{0,64}$`)
)

// EdgePolicy is a parsed v1 edge policy body: the live control-plane facts
// the edge gates on before it would answer an authorize itself. Every
// exported field is a wire field; ParseEdgePolicy refuses any other field, a
// missing one (async_settle, fastpath_pilot_key_windows and
// fastpath_edge_streams alone may be absent), a float, a duplicate key, null anywhere but monitor_lookup_hash,
// and a body that is not its own canonical encoding.
type EdgePolicy struct {
	V                                 int64         `json:"v"`
	Kind                              string        `json:"kind"`
	Plane                             Plane         `json:"plane"`
	IssuedAt                          int64         `json:"issued_at"`
	RefreshAt                         int64         `json:"refresh_at"`
	NotAfter                          int64         `json:"not_after"`
	AcceptedImageDigests              []string      `json:"accepted_image_digests"`
	StageD                            StageDSetting `json:"stage_d"`
	RecordsDurable                    bool          `json:"records_durable"`
	FastpathPilotEnabled              bool          `json:"fastpath_pilot_enabled"`
	FastpathPilotWorkspaces           []string      `json:"fastpath_pilot_workspaces"`
	FastpathPilotRegions              []string      `json:"fastpath_pilot_regions"`
	GatewayAuthorizeMaxInFlightPerKey int64         `json:"gateway_authorize_max_in_flight_per_key"`
	MonitorLookupHash                 *string       `json:"monitor_lookup_hash"`
	AsyncSettleShadowPolicy           string        `json:"async_settle_shadow_policy"`
	AsyncSettleShadowWorkspaces       []string      `json:"async_settle_shadow_workspaces"`
	ConfiguredRegions                 []string      `json:"configured_regions"`
	PrimaryRegion                     string        `json:"primary_region"`
	MultiRegionEnabled                bool          `json:"multi_region_enabled"`
	InternalModels                    []string      `json:"internal_models"`
	// AsyncSettle is optional: nil when the publisher does not carry it
	// (unknown, docs/design/fast-admission-e2-g15-async.md section 6.1).
	AsyncSettle *AsyncSettle `json:"async_settle,omitempty"`
	// FastpathPilotKeyWindows is optional: settings.fastpath_pilot_key_windows,
	// which keys with spend windows the cohort covers (KeyWindowsMode;
	// docs/design/fast-admission-e2-key-windows.md section 5): "alert" or
	// "enforce". Absent is off, the only form of it: the publisher leaves the
	// member out rather than write "off", and parse_edge_policy refuses "off"
	// as this does.
	FastpathPilotKeyWindows *string `json:"fastpath_pilot_key_windows,omitempty"`
	// FastpathEdgeStreams is optional: true when the control plane's
	// fastpath_edge_streams, fastpath_pilot_streams and
	// reap_snapshot_booking_enabled are all on, the edge's own switch for
	// streams (docs/design/fast-admission-streams.md section 5.4). Absent is
	// off, the only form of off: the publisher leaves the member out rather
	// than write false, and false is refused. Go ships first; the edge reads
	// it (EdgeStreams) and still passes every stream until the increment
	// that decides them.
	FastpathEdgeStreams *bool `json:"fastpath_edge_streams,omitempty"`
}

// EdgeStreams is the policy's fastpath_edge_streams, false when the
// publisher does not carry it.
func (p *EdgePolicy) EdgeStreams() bool {
	return p.FastpathEdgeStreams != nil && *p.FastpathEdgeStreams
}

// The modes of fastpath_pilot_key_windows (KeyWindowsMode): off, the member
// absent, covers no windowed key; alert the alert-only ones (whose windows
// Python's authorize never checks); enforce every one. Only alert and
// enforce are written.
const (
	KeyWindowsOff     = "off"
	KeyWindowsAlert   = "alert"
	KeyWindowsEnforce = "enforce"
)

// KeyWindowsMode is the policy's fastpath_pilot_key_windows, KeyWindowsOff
// when the publisher does not carry it.
func (p *EdgePolicy) KeyWindowsMode() string {
	if p.FastpathPilotKeyWindows == nil {
		return KeyWindowsOff
	}
	return *p.FastpathPilotKeyWindows
}

// AsyncSettle is the policy's async_settle object, four booleans the
// publishing Python process reads from its settings and its async-settle
// runtime when it signs (docs/design/fast-admission-e2-g15-async.md section
// 3.4). Each is required inside the object; no other member is allowed.
// The edge's decide pipeline reads them (edge/decide.go, G15c).
type AsyncSettle struct {
	// AdmissionEnabled is settings.async_settle_admission_enabled.
	AdmissionEnabled bool `json:"admission_enabled"`
	// TicketAuthority is whether the publisher holds a ticket signer, an
	// authority epoch of at least 1 and a non-empty journal region.
	TicketAuthority bool `json:"ticket_authority"`
	// TypedRecords is whether the publisher writes typed records, the
	// expression gateway.py's async-v1 branch gates on.
	TypedRecords bool `json:"typed_records"`
	// EdgeDecide is fastpath_edge_decide_async: whether the edge may answer
	// an async-v1 authorize itself.
	EdgeDecide bool `json:"edge_decide"`
}

// PolicyClaims are an edge-policy manifest's claims; the canonical encoding
// of this struct (its fields are in lexical order) is the signed payload.
type PolicyClaims struct {
	Aud      string `json:"aud"`
	Iss      string `json:"iss"`
	IssuedAt int64  `json:"issued_at"`
	NotAfter int64  `json:"not_after"`
	Plane    Plane  `json:"plane"`
	SHA256   string `json:"sha256"`
	Size     int64  `json:"size"`
	V        int64  `json:"v"`
}

// PolicyVerifyOptions are VerifyOptions and the clock skew the edge allows
// on issued_at: the publisher stamps its own clock, floored to whole
// seconds, and the two hosts' clocks differ by a little, so a body issued
// up to Skew seconds after Now is accepted. not_after is not stretched.
type PolicyVerifyOptions struct {
	VerifyOptions
	Skew int64
}

// ParseEdgePolicy decodes an edge policy v1 body strictly, as
// parse_edge_policy and the canonical check of edge_policy_manifest_claims
// do: printable ASCII with no escapes, integers only, no duplicate keys,
// exactly the v1 fields, every value in its pattern, sets sorted and unique,
// the window issued_at + 15 and + 60, and the bytes their own canonical
// encoding (sorted keys, compact).
func ParseEdgePolicy(raw []byte) (*EdgePolicy, error) {
	if err := wireCheck(raw, maxPolicyJSONDepth, false); err != nil {
		return nil, fmt.Errorf("edge policy body: %w", err)
	}
	var generic any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&generic); err != nil {
		return nil, fmt.Errorf("edge policy body: %w", err)
	}
	if err := shape(generic, reflect.TypeFor[EdgePolicy](), "$"); err != nil {
		return nil, fmt.Errorf("edge policy body: %w", err)
	}
	// encoding/json writes a map's keys sorted and a json.Number as it
	// came, so this is the canonical encoding of what was received.
	if !bytes.Equal(raw, canonical(generic)) {
		return nil, errors.New("edge policy body: not its canonical encoding")
	}
	var p EdgePolicy
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("edge policy body: %w", err)
	}
	if err := p.validate(); err != nil {
		return nil, fmt.Errorf("edge policy body: %w", err)
	}
	return &p, nil
}

func (p *EdgePolicy) validate() error {
	if p.V != policyVersion || p.Kind != PolicyKind {
		return errors.New("unsupported version or kind")
	}
	if !identity.MatchString(p.Plane.Cloud) || !identity.MatchString(p.Plane.Region) {
		return errors.New("plane")
	}
	if p.IssuedAt < 0 || p.RefreshAt != p.IssuedAt+PolicyRefreshSeconds || p.NotAfter != p.IssuedAt+PolicyLifetimeSeconds {
		return errors.New("window")
	}
	for _, set := range []struct {
		name    string
		values  []string
		pattern *regexp.Regexp
	}{
		{"accepted_image_digests", p.AcceptedImageDigests, imageDigest},
		{"stage_d.pilot_workspaces", p.StageD.PilotWorkspaces, workspaceID},
		{"fastpath_pilot_workspaces", p.FastpathPilotWorkspaces, workspaceID},
		{"fastpath_pilot_regions", p.FastpathPilotRegions, regionID},
		{"async_settle_shadow_workspaces", p.AsyncSettleShadowWorkspaces, workspaceID},
		{"internal_models", p.InternalModels, identity},
	} {
		if !sortedUnique(set.values) {
			return fmt.Errorf("%s must be sorted and unique", set.name)
		}
		for _, value := range set.values {
			if !set.pattern.MatchString(value) {
				return fmt.Errorf("%s: %q", set.name, value)
			}
		}
	}
	if p.GatewayAuthorizeMaxInFlightPerKey < 1 {
		return errors.New("gateway_authorize_max_in_flight_per_key must be positive")
	}
	if p.MonitorLookupHash != nil && !digest.MatchString(*p.MonitorLookupHash) {
		return errors.New("monitor_lookup_hash")
	}
	if !policyName.MatchString(p.AsyncSettleShadowPolicy) {
		return errors.New("async_settle_shadow_policy")
	}
	if m := p.FastpathPilotKeyWindows; m != nil && *m != KeyWindowsAlert && *m != KeyWindowsEnforce {
		return errors.New("fastpath_pilot_key_windows")
	}
	if s := p.FastpathEdgeStreams; s != nil && !*s {
		return errors.New("fastpath_edge_streams is absent when off, never false")
	}
	regions := p.ConfiguredRegions
	if len(regions) == 0 || len(regions) != len(slices.Compact(slices.Sorted(slices.Values(regions)))) ||
		!slices.Contains(regions, p.PrimaryRegion) {
		return errors.New("configured_regions must be unique and include primary_region")
	}
	for _, region := range regions {
		if !identity.MatchString(region) {
			return fmt.Errorf("configured_regions: %q", region)
		}
	}
	return nil
}

func validPolicyClaims(c PolicyClaims) bool {
	for _, value := range []string{c.Plane.Cloud, c.Plane.Region, c.Iss, c.Aud} {
		if !identity.MatchString(value) {
			return false
		}
	}
	return c.V == 1 && digest.MatchString(c.SHA256) && c.Size >= 0 && c.IssuedAt >= 0 && c.NotAfter >= 0
}

// VerifyEdgePolicy checks an edge-policy manifest against the body bytes
// received and returns the parsed policy, as verify_edge_policy_manifest
// does, in Verify's order:
//
//   - the compact JWS, its header typ tr-edge-policy-v1 (a routing snapshot
//     manifest is refused on "type", as Verify refuses this one), the one
//     key of that kid with the routing-snapshot purpose, its signature;
//   - the public test key refused unless AllowTestKey is set;
//   - the claims: exactly {aud, iss, issued_at, not_after, plane, sha256,
//     size, v}, strict integers, the signed payload their canonical
//     encoding, iss and aud the key's;
//   - aud and plane the verifier's own;
//   - the binding: sha256 and size of the bytes received (never
//     re-encoded), the body a strict v1 policy whose issued_at, not_after
//     and plane are the claims';
//   - freshness: issued_at <= now + Skew and now < not_after.
func VerifyEdgePolicy(token string, keys []TrustedKey, body []byte, opts PolicyVerifyOptions) (*EdgePolicy, PolicyClaims, error) {
	var claims PolicyClaims
	payload, key, err := verifyJWS(token, keys, PolicyTyp, opts.AllowTestKey)
	if err != nil {
		return nil, claims, err
	}
	if err := exact(payload, &claims); err != nil || !validPolicyClaims(claims) {
		return nil, claims, refuse("claims")
	}
	if !bytes.Equal(payload, canonical(claims)) || claims.Iss != key.Iss || claims.Aud != key.Aud {
		return nil, claims, refuse("manifest issuer or encoding")
	}
	if claims.Aud != opts.ExpectedAud {
		return nil, claims, refuse("manifest audience")
	}
	if claims.Plane != opts.ExpectedPlane {
		return nil, claims, refuse("manifest plane")
	}
	sum := sha256.Sum256(body)
	if claims.SHA256 != hex.EncodeToString(sum[:]) || claims.Size != int64(len(body)) {
		return nil, claims, refuse("manifest binding")
	}
	policy, err := ParseEdgePolicy(body)
	if err != nil {
		return nil, claims, refuse("manifest binding: " + err.Error())
	}
	if policy.IssuedAt != claims.IssuedAt || policy.NotAfter != claims.NotAfter || policy.Plane != claims.Plane {
		return nil, claims, refuse("manifest binding")
	}
	if opts.Skew < 0 || !(claims.IssuedAt <= opts.Now+opts.Skew && opts.Now < claims.NotAfter) {
		return nil, claims, refuse("edge policy freshness")
	}
	return policy, claims, nil
}
