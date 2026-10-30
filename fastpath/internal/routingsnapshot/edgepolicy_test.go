package routingsnapshot

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"testing"
)

// The edge policy golden files (scripts/fastpath/edge_policy_vectors.py,
// E2 P2): issued 2026-10-19T00:00Z, signed by the routing snapshot's TEST
// key under the edge-policy typ.
const (
	policyIssuedAt = 1792368000
	policyNotAfter = policyIssuedAt + PolicyLifetimeSeconds
)

func policyGolden(t *testing.T) (manifestFile, []byte) {
	t.Helper()
	var m manifestFile
	decoder := json.NewDecoder(bytes.NewReader(testdata(t, "edge_policy_v1.manifest.json")))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&m); err != nil {
		t.Fatal(err)
	}
	return m, testdata(t, "edge_policy_v1.body.json")
}

func policyOptions(m manifestFile, now int64) PolicyVerifyOptions {
	return PolicyVerifyOptions{VerifyOptions: goldenOptions(m, now), Skew: 1}
}

// testKeySigner signs as the golden files' TEST key, derived from the public
// seed in scripts/fastpath/routing_snapshot_vectors.py, so a golden case can
// be any body the publisher could have signed with it.
func testKeySigner(t *testing.T, m manifestFile) signer {
	t.Helper()
	seed := sha256.Sum256([]byte("trustedrouter routing snapshot v1 golden-file TEST key; not a secret"))
	private := ed25519.NewKeyFromSeed(seed[:])
	s := signer{private, goldenKey(t, m)}
	if !bytes.Equal(private.Public().(ed25519.PublicKey), s.key.PublicKey) {
		t.Fatal("the seed does not derive the golden files' TEST key")
	}
	return s
}

func (s signer) policyClaimsFor(t *testing.T, body []byte) PolicyClaims {
	t.Helper()
	p, err := ParseEdgePolicy(body)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	return PolicyClaims{Aud: s.key.Aud, Iss: s.key.Iss, IssuedAt: p.IssuedAt, NotAfter: p.NotAfter, Plane: p.Plane,
		SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body)), V: 1}
}

func (s signer) policyHeader(typ string) []byte {
	return canonical(jwsHeader{Alg: "EdDSA", Kid: s.key.Kid, Typ: typ})
}

func (s signer) signPolicy(t *testing.T, body []byte) string {
	t.Helper()
	return s.sign(s.policyHeader(PolicyTyp), canonical(s.policyClaimsFor(t, body)))
}

// policyIssued is the golden body issued at another instant, its window
// moved with it: what another Cloud Run instance's publisher signs.
func policyIssued(t *testing.T, body []byte, issuedAt int64) []byte {
	t.Helper()
	out := body
	for _, field := range []struct {
		name   string
		offset int64
	}{{"issued_at", 0}, {"refresh_at", PolicyRefreshSeconds}, {"not_after", PolicyLifetimeSeconds}} {
		from := fmt.Sprintf(`"%s":%d`, field.name, policyIssuedAt+field.offset)
		to := fmt.Sprintf(`"%s":%d`, field.name, issuedAt+field.offset)
		if bytes.Count(out, []byte(from)) != 1 {
			t.Fatalf("the golden body has no single %s", from)
		}
		out = bytes.Replace(out, []byte(from), []byte(to), 1)
	}
	return out
}

// TestTheGoldenPolicyVerifies holds Go's edge-policy verifier to the files
// tests/test_edge_policy_publish.py holds Python's to, field by field.
func TestTheGoldenPolicyVerifies(t *testing.T) {
	m, raw := policyGolden(t)
	if !m.TestKey || m.Kid != TestKID || m.PublicKey != TestPublicKey || m.Purpose != Purpose ||
		m.Generator != "scripts/fastpath/edge_policy_vectors.py" {
		t.Fatalf("the golden policy must be signed by the named test key, not %+v", m)
	}
	p, c, err := VerifyEdgePolicy(m.JWS, []TrustedKey{goldenKey(t, m)}, raw, policyOptions(m, policyIssuedAt))
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	want := PolicyClaims{Aud: m.Aud, Iss: m.Iss, IssuedAt: policyIssuedAt, NotAfter: policyNotAfter, Plane: goldenPlane,
		SHA256: hex.EncodeToString(sum[:]), Size: int64(len(raw)), V: 1}
	if c != want {
		t.Fatalf("claims %+v, want %+v", c, want)
	}
	digest := func(label string) string {
		s := sha256.Sum256([]byte(label))
		return "sha256:" + hex.EncodeToString(s[:])
	}
	monitor := "e4b55020e0693f129b6580f704d47899e84dfbca5169d6b0f92a4b0159bed9b9"
	wantPolicy := EdgePolicy{
		V: 1, Kind: PolicyKind, Plane: goldenPlane,
		IssuedAt: policyIssuedAt, RefreshAt: policyIssuedAt + 15, NotAfter: policyNotAfter,
		AcceptedImageDigests: slices.Sorted(slices.Values([]string{digest("synthetic enclave image 1"),
			digest("synthetic enclave image 2"), digest("synthetic enclave image 3")})),
		StageD: StageDSetting{EligibilityEnabled: true, HeartbeatEnabled: true, RequireHeartbeatDeclaration: false,
			PilotWorkspaces: []string{"ws-synthetic-stage-d-a", "ws-synthetic-stage-d-b"}},
		RecordsDurable: true, FastpathPilotEnabled: true,
		FastpathPilotWorkspaces:           []string{"ws-synthetic-pilot-1", "ws-synthetic-pilot-2"},
		FastpathPilotRegions:              []string{"us-central1"},
		GatewayAuthorizeMaxInFlightPerKey: 4, MonitorLookupHash: &monitor,
		AsyncSettleShadowPolicy: "shadow-v2", AsyncSettleShadowWorkspaces: []string{"ws-synthetic-async-shadow"},
		ConfiguredRegions: []string{"us-central1", "europe-west4", "us-east4"}, PrimaryRegion: "us-central1",
		MultiRegionEnabled: true, InternalModels: []string{"trustedrouter/fortanix-test", "trustedrouter/monitor"},
		AsyncSettle: &AsyncSettle{TicketAuthority: true, TypedRecords: true},
	}
	got, _ := json.Marshal(p)
	wanted, _ := json.Marshal(wantPolicy)
	if !bytes.Equal(got, wanted) {
		t.Fatalf("policy\n%s\nwant\n%s", got, wanted)
	}
	if raw[len(raw)-1] == '\n' {
		t.Fatal("the body has no trailing newline")
	}
}

// TestEachVerifierRefusesTheOthersManifest: decision 9. The snapshot
// verifier refuses the policy's golden manifest on its typ, the policy
// verifier the snapshot's, and a policy body re-signed under the snapshot's
// typ (with the TEST key, the golden "wrong typ" case) is refused too.
func TestEachVerifierRefusesTheOthersManifest(t *testing.T) {
	pm, policyRaw := policyGolden(t)
	sm, snapshotRaw := golden(t)
	keys := []TrustedKey{goldenKey(t, pm)}
	if _, _, err := Verify(pm.JWS, keys, policyRaw, goldenOptions(pm, policyIssuedAt)); reason(err) != "type" {
		t.Errorf("the snapshot verifier given the policy manifest: %q", reason(err))
	}
	if _, _, err := VerifyEdgePolicy(sm.JWS, keys, snapshotRaw, policyOptions(sm, policyIssuedAt)); reason(err) != "type" {
		t.Errorf("the policy verifier given the snapshot manifest: %q", reason(err))
	}
	s := testKeySigner(t, pm)
	claims := canonical(s.policyClaimsFor(t, policyRaw))
	if _, _, err := VerifyEdgePolicy(s.sign(s.policyHeader(PolicyTyp), claims), keys, policyRaw, policyOptions(pm, policyIssuedAt)); err != nil {
		t.Fatalf("the positive control, the same claims under the policy typ: %v", err)
	}
	for _, typ := range []string{Typ, "tr-edge-policy-v2", "tr-edge-policy"} {
		token := s.sign(s.policyHeader(typ), claims)
		if _, _, err := VerifyEdgePolicy(token, keys, policyRaw, policyOptions(pm, policyIssuedAt)); reason(err) != "type" {
			t.Errorf("typ %q: %q", typ, reason(err))
		}
	}
}

// TestPolicyVerifyRefusals: each refusal beside its positive control.
func TestPolicyVerifyRefusals(t *testing.T) {
	m, raw := policyGolden(t)
	keys := []TrustedKey{goldenKey(t, m)}
	check := func(name, want string, token string, body []byte, o PolicyVerifyOptions) {
		t.Helper()
		_, _, err := VerifyEdgePolicy(token, keys, body, o)
		if got := reason(err); (want == "" && err != nil) || (want != "" && got != want) {
			t.Errorf("%s: want %q, got %q", name, want, got)
		}
	}
	opts := policyOptions(m, policyIssuedAt)

	// Freshness: issued_at <= now + 1 s of skew, now < not_after. A stale
	// body (the golden at its not_after) and one from too far ahead.
	check("issued now", "", m.JWS, raw, opts)
	check("one second of skew", "", m.JWS, raw, policyOptions(m, policyIssuedAt-1))
	check("last second", "", m.JWS, raw, policyOptions(m, policyNotAfter-1))
	check("stale", "edge policy freshness", m.JWS, raw, policyOptions(m, policyNotAfter))
	check("long stale", "edge policy freshness", m.JWS, raw, policyOptions(m, policyNotAfter+3600))
	check("from the future", "edge policy freshness", m.JWS, raw, policyOptions(m, policyIssuedAt-2))
	noSkew := policyOptions(m, policyIssuedAt-1)
	noSkew.Skew = 0
	check("skew off", "edge policy freshness", m.JWS, raw, noSkew)
	// A negative skew is refused outright, even where now - 1 would pass.
	negative := policyOptions(m, policyIssuedAt+5)
	negative.Skew = -1
	check("negative skew", "edge policy freshness", m.JWS, raw, negative)
	negative.Skew = 0
	check("negative skew's control", "", m.JWS, raw, negative)

	production := opts
	production.AllowTestKey = false
	check("test key", "test key outside test mode", m.JWS, raw, production)
	otherPlane := opts
	otherPlane.ExpectedPlane = Plane{Cloud: "gcp", Region: "europe-west4"}
	check("plane", "manifest plane", m.JWS, raw, otherPlane)
	otherAud := opts
	otherAud.ExpectedAud = "tr-some-other-consumer"
	check("audience", "manifest audience", m.JWS, raw, otherAud)

	// The binding: the bytes, their size, and the body's window and plane.
	altered := bytes.Replace(raw, []byte(`"ws-synthetic-pilot-1"`), []byte(`"ws-synthetic-pilot-0"`), 1)
	if bytes.Equal(altered, raw) {
		t.Fatal("the alteration must change the body")
	}
	check("sha256", "manifest binding", m.JWS, altered, opts)
	check("size", "manifest binding", m.JWS, append(bytes.Clone(raw), ' '), opts)

	s := testKeySigner(t, m)
	claims := s.policyClaimsFor(t, raw)
	header := s.policyHeader(PolicyTyp)
	check("re-signed", "", s.sign(header, canonical(claims)), raw, opts)
	window := claims
	window.NotAfter++
	check("claims window", "manifest binding", s.sign(header, canonical(window)), raw, opts)
	plane := claims
	plane.Plane.Region = "europe-west4"
	check("claims plane", "manifest plane", s.sign(header, canonical(plane)), raw, opts)
	// The body names another plane than the claims (which name the
	// verifier's own, and bind these exact bytes).
	otherBody := bytes.Replace(raw, []byte(`"plane":{"cloud":"gcp","region":"us-central1"}`),
		[]byte(`"plane":{"cloud":"gcp","region":"europe-west4"}`), 1)
	if bytes.Equal(otherBody, raw) {
		t.Fatal("the plane mutation does not apply")
	}
	bodyPlane := s.policyClaimsFor(t, otherBody)
	bodyPlane.Plane = goldenPlane
	check("body plane", "manifest binding", s.sign(header, canonical(bodyPlane)), otherBody, opts)
	bodyPlaneControl := s.policyClaimsFor(t, otherBody)
	check("body plane's control", "", s.sign(header, canonical(bodyPlaneControl)), otherBody, PolicyVerifyOptions{
		VerifyOptions: VerifyOptions{Now: policyIssuedAt, ExpectedPlane: bodyPlaneControl.Plane, ExpectedAud: m.Aud, AllowTestKey: true},
		Skew:          1})
	issuer := claims
	issuer.Iss = "tr-someone-else"
	check("issuer", "manifest issuer or encoding", s.sign(header, canonical(issuer)), raw, opts)
	snapshotClaims := strings.Replace(string(canonical(claims)), `"issued_at"`, `"compiled_at"`, 1)
	check("snapshot claims", "claims", s.sign(header, []byte(snapshotClaims)), raw, opts)
	extra := strings.Replace(string(canonical(claims)), `"v":1}`, `"v":1,"x":1}`, 1)
	check("claims field", "claims", s.sign(header, []byte(extra)), raw, opts)
	float := strings.Replace(string(canonical(claims)), `"v":1}`, `"v":1.0}`, 1)
	check("float claim", "json", s.sign(header, []byte(float)), raw, opts)
	duplicate := strings.Replace(string(canonical(claims)), `"v":1}`, `"v":1,"v":1}`, 1)
	check("duplicate claim", "json", s.sign(header, []byte(duplicate)), raw, opts)

	// A body the claims bind exactly but that is not a v1 policy.
	notPolicy := bytes.Replace(raw, []byte(`"v":1}`), []byte(`"v":1,"x":1}`), 1)
	bound := claims
	sum := sha256.Sum256(notPolicy)
	bound.SHA256, bound.Size = hex.EncodeToString(sum[:]), int64(len(notPolicy))
	if _, _, err := VerifyEdgePolicy(s.sign(header, canonical(bound)), keys, notPolicy, opts); !strings.HasPrefix(reason(err), "manifest binding: ") {
		t.Errorf("a bound non-policy body verified: %q", reason(err))
	}

	// A later body (another instance's) verifies at its own instant: which
	// copy to keep is the poller's rule, not the verifier's.
	later := policyIssued(t, raw, policyIssuedAt+30)
	check("later body", "", s.signPolicy(t, later), later, policyOptions(m, policyIssuedAt+30))
	check("later body, early", "edge policy freshness", s.signPolicy(t, later), later, opts)
}

// TestParseEdgePolicyRefusals: the strict decoder refuses what
// parse_edge_policy and the canonical check refuse, each beside the golden
// body, which it accepts.
func TestParseEdgePolicyRefusals(t *testing.T) {
	_, raw := policyGolden(t)
	if _, err := ParseEdgePolicy(raw); err != nil {
		t.Fatal(err)
	}
	monitor := `"monitor_lookup_hash":"e4b55020e0693f129b6580f704d47899e84dfbca5169d6b0f92a4b0159bed9b9"`
	nullMonitor := strings.Replace(string(raw), monitor, `"monitor_lookup_hash":null`, 1)
	if p, err := ParseEdgePolicy([]byte(nullMonitor)); err != nil || p.MonitorLookupHash != nil {
		t.Fatalf("a null monitor_lookup_hash is the one null allowed: %v", err)
	}
	emptySets := strings.Replace(string(raw), `"fastpath_pilot_regions":["us-central1"]`, `"fastpath_pilot_regions":[]`, 1)
	if p, err := ParseEdgePolicy([]byte(emptySets)); err != nil || p.FastpathPilotRegions == nil {
		t.Fatalf("an empty set: %v", err)
	}
	cases := []struct{ name, from, to string }{
		{"unknown field", `"v":1}`, `"v":1,"x":1}`},
		{"missing field", `"records_durable":true,`, ``},
		{"float", `"gateway_authorize_max_in_flight_per_key":4`, `"gateway_authorize_max_in_flight_per_key":4.0`},
		{"exponent", `"gateway_authorize_max_in_flight_per_key":4`, `"gateway_authorize_max_in_flight_per_key":4e0`},
		{"duplicate key", `"v":1}`, `"v":1,"v":1}`},
		{"null", `"primary_region":"us-central1"`, `"primary_region":null`},
		{"null set", `"internal_models":[`, `"internal_models":null,"x":[`},
		{"string for a bool", `"records_durable":true`, `"records_durable":"true"`},
		{"int for a bool", `"records_durable":true`, `"records_durable":1`},
		{"bool for an int", `"gateway_authorize_max_in_flight_per_key":4`, `"gateway_authorize_max_in_flight_per_key":true`},
		{"spacing", `"v":1}`, `"v": 1}`},
		{"trailing newline", `"v":1}`, "\"v\":1}\n"},
		{"escape", `"primary_region":"us-central1"`, "\"primary_region\":\"us-c" + string(rune(0x5c)) + "u0065ntral1\""},
		{"version", `"v":1}`, `"v":2}`},
		{"kind", `"kind":"tr-edge-policy"`, `"kind":"tr-routing-snapshot"`},
		{"window", `"refresh_at":1792368015`, `"refresh_at":1792368016`},
		{"lifetime", `"not_after":1792368060`, `"not_after":1792368120`},
		{"unsorted set", `["ws-synthetic-pilot-1","ws-synthetic-pilot-2"]`, `["ws-synthetic-pilot-2","ws-synthetic-pilot-1"]`},
		{"repeated set", `["ws-synthetic-pilot-1","ws-synthetic-pilot-2"]`, `["ws-synthetic-pilot-1","ws-synthetic-pilot-1"]`},
		{"digest", `"sha256:5070204bd1f3`, `"sha256:X070204bd1f3`},
		{"in-flight zero", `"gateway_authorize_max_in_flight_per_key":4`, `"gateway_authorize_max_in_flight_per_key":0`},
		{"negative", `"gateway_authorize_max_in_flight_per_key":4`, `"gateway_authorize_max_in_flight_per_key":-4`},
		{"monitor hash", monitor, `"monitor_lookup_hash":"E4B5"`},
		{"primary outside", `"primary_region":"us-central1"`, `"primary_region":"us-west1"`},
		{"repeated region", `["us-central1","europe-west4","us-east4"]`, `["us-central1","europe-west4","us-central1"]`},
		{"no regions", `"configured_regions":["us-central1","europe-west4","us-east4"]`, `"configured_regions":[]`},
		{"pilot region", `"fastpath_pilot_regions":["us-central1"]`, `"fastpath_pilot_regions":["US-central1"]`},
		{"shadow policy", `"async_settle_shadow_policy":"shadow-v2"`, `"async_settle_shadow_policy":"shadow v2"`},
		{"nested unknown", `"require_heartbeat_declaration":false}`, `"require_heartbeat_declaration":false,"x":1}`},
	}
	for _, c := range cases {
		mutated := strings.Replace(string(raw), c.from, c.to, 1)
		if mutated == string(raw) {
			t.Errorf("%s: the mutation does not apply", c.name)
			continue
		}
		if _, err := ParseEdgePolicy([]byte(mutated)); err == nil {
			t.Errorf("%s: accepted", c.name)
		}
	}
	// The same fields in another order: not the canonical encoding.
	reordered := `{"v":1,` + strings.TrimPrefix(strings.Replace(string(raw), `,"v":1}`, `}`, 1), `{`)
	if _, err := ParseEdgePolicy([]byte(reordered)); err == nil || !strings.Contains(err.Error(), "canonical") {
		t.Errorf("key order: %v", err)
	}
}

// withoutAsyncSettle is the golden body as a publisher before P5 wrote it:
// the async_settle member removed.
func withoutAsyncSettle(t *testing.T, raw []byte) []byte {
	t.Helper()
	member := regexp.MustCompile(`"async_settle":\{[^{}]*\},`)
	if len(member.FindAll(raw, -1)) != 1 {
		t.Fatal("the golden body has no single async_settle object")
	}
	return member.ReplaceAll(raw, nil)
}

// withAsyncSettle is the golden body with an async_settle object in its
// canonical place (just before async_settle_shadow_policy, keys sorted).
func withAsyncSettle(t *testing.T, raw []byte, object string) []byte {
	t.Helper()
	at := []byte(`"async_settle_shadow_policy":`)
	if bytes.Count(raw, at) != 1 {
		t.Fatal("the golden body has no single async_settle_shadow_policy")
	}
	return bytes.Replace(raw, at, append([]byte(`"async_settle":`+object+`,`), at...), 1)
}

// TestTheAsyncSettleObjectIsOptional: G15a (docs/design/
// fast-admission-e2-g15-async.md section 6.1). The golden body without its
// async_settle (P5 publishes it) parses with it nil (unknown); a body with
// the object parses and exposes each of the four booleans as published, in
// every combination, and verifies under a manifest signed for it; null, a
// missing, unknown or non-boolean member, and an object out of canonical
// order are refused.
func TestTheAsyncSettleObjectIsOptional(t *testing.T) {
	m, published := policyGolden(t)
	raw := withoutAsyncSettle(t, published)
	if bytes.Contains(raw, []byte(`"async_settle":`)) {
		t.Fatal("the stripped body must have no async_settle")
	}
	p, err := ParseEdgePolicy(raw)
	if err != nil || p.AsyncSettle != nil {
		t.Fatalf("absent async_settle: %v, %+v", err, p)
	}
	for bits := range 16 {
		want := AsyncSettle{AdmissionEnabled: bits&1 != 0, EdgeDecide: bits&2 != 0,
			TicketAuthority: bits&4 != 0, TypedRecords: bits&8 != 0}
		object := fmt.Sprintf(`{"admission_enabled":%t,"edge_decide":%t,"ticket_authority":%t,"typed_records":%t}`,
			want.AdmissionEnabled, want.EdgeDecide, want.TicketAuthority, want.TypedRecords)
		body := withAsyncSettle(t, raw, object)
		got, err := ParseEdgePolicy(body)
		if err != nil || got.AsyncSettle == nil || *got.AsyncSettle != want {
			t.Fatalf("%s: %v, %+v", object, err, got)
		}
		// Every other field is what the golden body says.
		got.AsyncSettle = nil
		if a, b := canonical(got), canonical(p); !bytes.Equal(a, b) {
			t.Fatalf("%s changed another field:\n%s\n%s", object, a, b)
		}
	}
	on := `{"admission_enabled":false,"edge_decide":true,"ticket_authority":true,"typed_records":true}`
	signed := withAsyncSettle(t, raw, on)
	s := testKeySigner(t, m)
	verified, _, err := VerifyEdgePolicy(s.signPolicy(t, signed), []TrustedKey{goldenKey(t, m)}, signed, policyOptions(m, policyIssuedAt))
	if err != nil || verified.AsyncSettle == nil || !verified.AsyncSettle.EdgeDecide || verified.AsyncSettle.AdmissionEnabled {
		t.Fatalf("a signed body with async_settle: %v, %+v", err, verified)
	}
	for name, object := range map[string]string{
		"null":           `null`,
		"fifth member":   `{"admission_enabled":false,"edge_decide":true,"extra":true,"ticket_authority":true,"typed_records":true}`,
		"missing member": `{"admission_enabled":false,"edge_decide":true,"ticket_authority":true}`,
		"empty object":   `{}`,
		"null member":    `{"admission_enabled":null,"edge_decide":true,"ticket_authority":true,"typed_records":true}`,
		"string member":  `{"admission_enabled":"false","edge_decide":true,"ticket_authority":true,"typed_records":true}`,
		"int member":     `{"admission_enabled":0,"edge_decide":true,"ticket_authority":true,"typed_records":true}`,
		"not an object":  `true`,
		"an array":       `[false,true,true,true]`,
		"unsorted":       `{"edge_decide":true,"admission_enabled":false,"ticket_authority":true,"typed_records":true}`,
		"duplicate":      `{"admission_enabled":false,"admission_enabled":false,"edge_decide":true,"ticket_authority":true,"typed_records":true}`,
	} {
		if got, err := ParseEdgePolicy(withAsyncSettle(t, raw, object)); err == nil {
			t.Errorf("%s: accepted as %+v", name, got.AsyncSettle)
		}
	}
	// The positive control for the refusals: the same splice accepts the
	// object they each break.
	if _, err := ParseEdgePolicy(withAsyncSettle(t, raw, on)); err != nil {
		t.Fatalf("the refusals' control: %v", err)
	}
}

// TestTheAdmissionWorkspacesMemberIsOptional (docs/design/
// fast-admission-e2-g15-async.md section 4.1, per workspace): Go ships
// before Python publishes the member, so an async_settle without
// admission_workspaces parses with it nil and Admits every workspace while
// admission is on (an empty pilot set admits all), none while it is off; a
// body with it parses, Admits exactly the workspaces it names, verifies
// under a manifest signed for it and encodes back to its own bytes. Present
// with admission off, empty, more than 32, unsorted, duplicated, null, a
// workspace outside the pattern, a non-string, a non-array and a member out
// of canonical order are refused.
func TestTheAdmissionWorkspacesMemberIsOptional(t *testing.T) {
	m, published := policyGolden(t)
	raw := withoutAsyncSettle(t, published)
	object := func(admission bool, workspaces string) string {
		member := ""
		if workspaces != "" {
			member = `"admission_workspaces":` + workspaces + `,`
		}
		return fmt.Sprintf(`{"admission_enabled":%t,%s"edge_decide":true,"ticket_authority":true,"typed_records":true}`,
			admission, member)
	}
	for _, admission := range []bool{false, true} {
		p, err := ParseEdgePolicy(withAsyncSettle(t, raw, object(admission, "")))
		if err != nil || p.AsyncSettle == nil || p.AsyncSettle.AdmissionWorkspaces != nil {
			t.Fatalf("absent admission_workspaces: %v, %+v", err, p)
		}
		for _, ws := range []string{"ws_a", "ws_b", "anything"} {
			if p.AsyncSettle.Admits(ws) != admission {
				t.Errorf("absent admission_workspaces, admission %v: Admits(%s) %v", admission, ws, !admission)
			}
		}
	}
	thirtyTwo := make([]string, 32)
	for i := range thirtyTwo {
		thirtyTwo[i] = fmt.Sprintf("ws_%02d", i)
	}
	for _, set := range [][]string{{"ws_b"}, {"ws_a", "ws_c"}, {"org:ws/1", "ws-2", "ws_3"}, thirtyTwo} {
		listed, _ := json.Marshal(set)
		body := withAsyncSettle(t, raw, object(true, string(listed)))
		p, err := ParseEdgePolicy(body)
		if err != nil || p.AsyncSettle == nil || p.AsyncSettle.AdmissionWorkspaces == nil ||
			!slices.Equal(*p.AsyncSettle.AdmissionWorkspaces, set) {
			t.Fatalf("%s: %v, %+v", listed, err, p)
		}
		for _, ws := range append(slices.Clone(set), "ws_a", "ws_b", "ws_c", "ws_z", "") {
			if got, want := p.AsyncSettle.Admits(ws), slices.Contains(set, ws); got != want {
				t.Errorf("%s: Admits(%q) %v, want %v", listed, ws, got, want)
			}
		}
		// The parsed policy encodes back to the bytes received.
		encoded, err := json.Marshal(p)
		var generic any
		decoder := json.NewDecoder(bytes.NewReader(encoded))
		decoder.UseNumber()
		if err != nil || decoder.Decode(&generic) != nil || !bytes.Equal(canonical(generic), body) {
			t.Fatalf("%s: does not round trip:\n%s\n%s", listed, canonical(generic), body)
		}
	}
	signed := withAsyncSettle(t, raw, object(true, `["ws_b"]`))
	s := testKeySigner(t, m)
	verified, _, err := VerifyEdgePolicy(s.signPolicy(t, signed), []TrustedKey{goldenKey(t, m)}, signed,
		policyOptions(m, policyIssuedAt))
	if err != nil || !verified.AsyncSettle.Admits("ws_b") || verified.AsyncSettle.Admits("ws_a") {
		t.Fatalf("a signed body with admission_workspaces: %v, %+v", err, verified)
	}
	thirtyThree, _ := json.Marshal(append(slices.Clone(thirtyTwo), "ws_99"))
	for name, body := range map[string][]byte{
		"admission off": withAsyncSettle(t, raw, object(false, `["ws_b"]`)),
		"empty":         withAsyncSettle(t, raw, object(true, `[]`)),
		"thirty-three":  withAsyncSettle(t, raw, object(true, string(thirtyThree))),
		"unsorted":      withAsyncSettle(t, raw, object(true, `["ws_c","ws_a"]`)),
		"duplicate":     withAsyncSettle(t, raw, object(true, `["ws_a","ws_a"]`)),
		"null":          withAsyncSettle(t, raw, object(true, `null`)),
		"null element":  withAsyncSettle(t, raw, object(true, `[null]`)),
		"space":         withAsyncSettle(t, raw, object(true, `["ws a"]`)),
		"empty string":  withAsyncSettle(t, raw, object(true, `[""]`)),
		"too long":      withAsyncSettle(t, raw, object(true, `["`+strings.Repeat("w", 65)+`"]`)),
		"an integer":    withAsyncSettle(t, raw, object(true, `[1]`)),
		"a string":      withAsyncSettle(t, raw, object(true, `"ws_b"`)),
		"an object":     withAsyncSettle(t, raw, object(true, `{"ws_b":true}`)),
		"a bool":        withAsyncSettle(t, raw, object(true, `true`)),
		"misplaced": withAsyncSettle(t, raw,
			`{"admission_enabled":true,"edge_decide":true,"admission_workspaces":["ws_b"],"ticket_authority":true,"typed_records":true}`),
		"duplicate member": withAsyncSettle(t, raw,
			`{"admission_enabled":true,"admission_workspaces":["ws_b"],"admission_workspaces":["ws_b"],"edge_decide":true,"ticket_authority":true,"typed_records":true}`),
	} {
		if got, err := ParseEdgePolicy(body); err == nil {
			t.Errorf("%s: accepted as %+v", name, got.AsyncSettle)
		}
	}
	// The positive control for the refusals: the same splice accepts a set.
	if _, err := ParseEdgePolicy(withAsyncSettle(t, raw, object(true, `["ws_a"]`))); err != nil {
		t.Fatalf("the refusals' control: %v", err)
	}
}

// keyWindowsMember matches a fastpath_pilot_key_windows member, whatever its
// value, so the tests run on a golden body with or without one.
var keyWindowsMember = regexp.MustCompile(`"fastpath_pilot_key_windows":("[^"]*"|[^,]*),`)

// withKeyWindows is the golden body with its fastpath_pilot_key_windows
// member set to value (raw JSON) in its canonical place, just before
// fastpath_pilot_regions; "" removes it.
func withKeyWindows(t *testing.T, raw []byte, value string) []byte {
	t.Helper()
	out := keyWindowsMember.ReplaceAll(raw, nil)
	if value == "" {
		return out
	}
	at := []byte(`"fastpath_pilot_regions":`)
	if bytes.Count(out, at) != 1 {
		t.Fatal("the golden body has no single fastpath_pilot_regions")
	}
	return bytes.Replace(out, at, append([]byte(`"fastpath_pilot_key_windows":`+value+`,`), at...), 1)
}

// TestTheKeyWindowsModeIsOptional (docs/design/fast-admission-e2-key-windows.md
// section 5): Go ships before Python publishes the member, so a body
// without fastpath_pilot_key_windows parses and reads as off, the only form
// of off; a body with it parses "alert" and "enforce" as published, and
// verifies under a manifest signed for it; "off", null, another string,
// another type and a member out of canonical order are refused, as
// Python's parse_edge_policy refuses them.
func TestTheKeyWindowsModeIsOptional(t *testing.T) {
	m, published := policyGolden(t)
	raw := withKeyWindows(t, published, "")
	p, err := ParseEdgePolicy(raw)
	if err != nil || p.FastpathPilotKeyWindows != nil || p.KeyWindowsMode() != KeyWindowsOff {
		t.Fatalf("absent fastpath_pilot_key_windows: %v, %+v", err, p)
	}
	for _, mode := range []string{KeyWindowsAlert, KeyWindowsEnforce} {
		body := withKeyWindows(t, raw, `"`+mode+`"`)
		got, err := ParseEdgePolicy(body)
		if err != nil || got.FastpathPilotKeyWindows == nil || got.KeyWindowsMode() != mode {
			t.Fatalf("%s: %v, %+v", mode, err, got)
		}
		// Every other field is what the golden body says.
		got.FastpathPilotKeyWindows = nil
		if a, b := canonical(got), canonical(p); !bytes.Equal(a, b) {
			t.Fatalf("%s changed another field:\n%s\n%s", mode, a, b)
		}
	}
	signed := withKeyWindows(t, raw, `"enforce"`)
	s := testKeySigner(t, m)
	verified, _, err := VerifyEdgePolicy(s.signPolicy(t, signed), []TrustedKey{goldenKey(t, m)}, signed,
		policyOptions(m, policyIssuedAt))
	if err != nil || verified.KeyWindowsMode() != KeyWindowsEnforce {
		t.Fatalf("a signed body with fastpath_pilot_key_windows: %v, %+v", err, verified)
	}
	for name, value := range map[string]string{
		"off":         `"off"`,
		"null":        `null`,
		"empty":       `""`,
		"upper case":  `"ENFORCE"`,
		"other mode":  `"on"`,
		"padded":      `" off"`,
		"a bool":      `true`,
		"an integer":  `1`,
		"an object":   `{"mode":"off"}`,
		"an array":    `["off"]`,
		"unknown key": `"alert","fastpath_pilot_key_windows_x":"alert"`,
	} {
		if got, err := ParseEdgePolicy(withKeyWindows(t, raw, value)); err == nil {
			t.Errorf("%s: accepted as %v", name, got.KeyWindowsMode())
		}
	}
	// Out of canonical order: the member after fastpath_pilot_regions.
	at := []byte(`"fastpath_pilot_workspaces":`)
	misplaced := bytes.Replace(raw, at, append([]byte(`"fastpath_pilot_key_windows":"alert",`), at...), 1)
	if _, err := ParseEdgePolicy(misplaced); err == nil || !strings.Contains(err.Error(), "canonical") {
		t.Errorf("a misplaced member: %v", err)
	}
	// The positive control for the refusals: the same splice accepts a mode.
	if _, err := ParseEdgePolicy(withKeyWindows(t, raw, `"alert"`)); err != nil {
		t.Fatalf("the refusals' control: %v", err)
	}
}
