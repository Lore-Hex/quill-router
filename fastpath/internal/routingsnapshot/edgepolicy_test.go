package routingsnapshot

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
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
