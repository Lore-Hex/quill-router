package routingsnapshot

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type manifestFile struct {
	Generator string `json:"generator"`
	TestKey   bool   `json:"test_key"`
	Kid       string `json:"kid"`
	Purpose   string `json:"purpose"`
	Iss       string `json:"iss"`
	Aud       string `json:"aud"`
	PublicKey string `json:"public_key_b64url"`
	JWS       string `json:"jws"`
}

func testdata(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func golden(t *testing.T) (manifestFile, []byte) {
	t.Helper()
	var m manifestFile
	decoder := json.NewDecoder(bytes.NewReader(testdata(t, "routing_snapshot_v2.manifest.json")))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&m); err != nil {
		t.Fatal(err)
	}
	return m, testdata(t, "routing_snapshot_v2.body.json")
}

// goldenKey is the manifest's key as a verifier configured with it holds it.
func goldenKey(t *testing.T, m manifestFile) TrustedKey {
	t.Helper()
	public, err := base64.RawURLEncoding.DecodeString(m.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	return TrustedKey{Kid: m.Kid, Purpose: m.Purpose, PublicKey: public, Iss: m.Iss, Aud: m.Aud}
}

var goldenPlane = Plane{Cloud: "gcp", Region: "us-central1"}

func goldenOptions(m manifestFile, now int64) VerifyOptions {
	return VerifyOptions{Now: now, ExpectedPlane: goldenPlane, ExpectedAud: m.Aud, AllowTestKey: true}
}

// goldenSnapshot is the golden body, verified as the edge verifies it.
func goldenSnapshot(t *testing.T) *Snapshot {
	t.Helper()
	m, raw := golden(t)
	s, _, err := Verify(m.JWS, []TrustedKey{goldenKey(t, m)}, raw, goldenOptions(m, 1792368000))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func reason(err error) string {
	var refusal *VerifyError
	if errors.As(err, &refusal) {
		return refusal.Reason
	}
	if err == nil {
		return "<nil>"
	}
	return "unexpected: " + err.Error()
}

// TestTheGoldenManifestSignsTheGoldenBody holds Go's verification to the
// files tests/test_fastpath_routing_snapshot_vectors.py holds to Python.
func TestTheGoldenManifestSignsTheGoldenBody(t *testing.T) {
	m, raw := golden(t)
	if !m.TestKey || m.Kid != TestKID || m.PublicKey != TestPublicKey || m.Purpose != Purpose {
		t.Fatalf("the golden manifest must be signed by the named test key, not %q", m.Kid)
	}
	s, c, err := Verify(m.JWS, []TrustedKey{goldenKey(t, m)}, raw, goldenOptions(m, 1792368000))
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if c.SHA256 != hex.EncodeToString(sum[:]) || c.Size != int64(len(raw)) || c.Iss != m.Iss {
		t.Fatalf("claims %+v", c)
	}
	if len(raw) == 0 || raw[len(raw)-1] == '\n' || bytes.IndexFunc(raw, func(r rune) bool { return r > 0x7e || r < 0x20 }) >= 0 {
		t.Fatal("the body must be printable ASCII with no trailing newline")
	}
	if s.EpochSeconds != 3600 || s.NotAfter <= s.CompiledAt || len(s.Endpoints) == 0 || len(s.Models) == 0 {
		t.Fatalf("window %d..%d, %d endpoints", s.CompiledAt, s.NotAfter, len(s.Endpoints))
	}
}

// signer signs manifests with a key generated for the test run, so the
// refusals below can be exercised on tokens the golden key never signed.
// Nothing about it is committed.
type signer struct {
	private ed25519.PrivateKey
	key     TrustedKey
}

func newSigner(t *testing.T, kid string) signer {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return signer{private, TrustedKey{Kid: kid, Purpose: Purpose, PublicKey: public, Iss: "tr-control-plane", Aud: "tr-fastpath-edge"}}
}

func (s signer) sign(header, claims []byte) string {
	material := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(claims)
	return material + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(s.private, []byte(material)))
}

func (s signer) claimsFor(body []byte) Claims {
	snapshot, err := Parse(body)
	if err != nil {
		panic(err)
	}
	sum := sha256.Sum256(body)
	return Claims{
		Aud: s.key.Aud, CompiledAt: snapshot.CompiledAt, Iss: s.key.Iss, NotAfter: snapshot.NotAfter,
		Plane: snapshot.Plane, SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body)),
		SourceRevision: snapshot.SourceRevision, V: snapshotVersion,
	}
}

func (s signer) header() []byte {
	return canonical(jwsHeader{Alg: "EdDSA", Kid: s.key.Kid, Typ: Typ})
}

// TestVerifyRefusals: each refusal beside its positive control, the same
// call with only the refused fact restored.
func TestVerifyRefusals(t *testing.T) {
	m, raw := golden(t)
	key := goldenKey(t, m)
	opts := goldenOptions(m, 1792368000)
	ok := func(name, token string, keys []TrustedKey, body []byte, o VerifyOptions) {
		t.Helper()
		if _, _, err := Verify(token, keys, body, o); err != nil {
			t.Errorf("%s: the positive control is refused: %v", name, err)
		}
	}
	refused := func(name, want, token string, keys []TrustedKey, body []byte, o VerifyOptions) {
		t.Helper()
		if _, _, err := Verify(token, keys, body, o); reason(err) != want {
			t.Errorf("%s: want %q, got %q", name, want, reason(err))
		}
	}
	keys := []TrustedKey{key}

	// A bad signature.
	parts := strings.Split(m.JWS, ".")
	signature, _ := base64.RawURLEncoding.DecodeString(parts[2])
	signature[0] ^= 1
	ok("signature", m.JWS, keys, raw, opts)
	refused("signature", "signature", parts[0]+"."+parts[1]+"."+base64.RawURLEncoding.EncodeToString(signature), keys, raw, opts)

	// The kid is not one the verifier accepts, or names a different key.
	renamed := key
	renamed.Kid = "routing-snapshot-other-key"
	refused("unknown kid", "key", m.JWS, []TrustedKey{renamed}, raw, opts)
	refused("two keys of one kid", "key", m.JWS, []TrustedKey{key, key}, raw, opts)
	impostor := key
	impostor.PublicKey = newSigner(t, m.Kid).key.PublicKey
	refused("another key under the kid", "signature", m.JWS, []TrustedKey{impostor}, raw, opts)
	wrongPurpose := key
	wrongPurpose.Purpose = "settle-ticket"
	refused("another purpose's key", "purpose", m.JWS, []TrustedKey{wrongPurpose}, raw, opts)

	// The public test key outside test mode.
	production := opts
	production.AllowTestKey = false
	refused("test key", "test key outside test mode", m.JWS, keys, raw, production)
	if !isTestKey(TrustedKey{Kid: "any", PublicKey: key.PublicKey}) || isTestKey(newSigner(t, "any").key) {
		t.Error("the test key is recognized by its public key as well as its kid")
	}

	// The body is not the one the claims bind: altered, or one byte longer.
	altered := bytes.Replace(raw, []byte(`"golden-fixture"`), []byte(`"golden-fixturf"`), 1)
	if bytes.Equal(altered, raw) || len(altered) != len(raw) {
		t.Fatal("the alteration must change the body and keep its size")
	}
	refused("sha256", "manifest binding", m.JWS, keys, altered, opts)
	refused("size", "manifest binding", m.JWS, keys, append(bytes.Clone(raw), ' '), opts)

	// Freshness: compiled_at <= now < not_after.
	for _, now := range []int64{1792368000, 1792387799} {
		ok("fresh", m.JWS, keys, raw, goldenOptions(m, now))
	}
	for _, now := range []int64{1792367999, 1792387800} {
		refused("stale or early", "snapshot freshness", m.JWS, keys, raw, goldenOptions(m, now))
	}

	// Another plane or another audience, under the trusted key.
	otherPlane := opts
	otherPlane.ExpectedPlane = Plane{Cloud: "gcp", Region: "europe-west4"}
	refused("plane", "manifest plane", m.JWS, keys, raw, otherPlane)
	otherAud := opts
	otherAud.ExpectedAud = "tr-some-other-consumer"
	refused("audience", "manifest audience", m.JWS, keys, raw, otherAud)

	// Manifests the golden key never signed: a production key verifies the
	// golden body without test mode, and every structural fault is refused.
	s := newSigner(t, "routing-snapshot-key-2")
	claims := s.claimsFor(raw)
	prod := VerifyOptions{Now: claims.CompiledAt, ExpectedPlane: goldenPlane, ExpectedAud: s.key.Aud}
	sKeys := []TrustedKey{s.key}
	ok("production key", s.sign(s.header(), canonical(claims)), sKeys, raw, prod)
	refused("typ", "type", s.sign(canonical(jwsHeader{Alg: "EdDSA", Kid: s.key.Kid, Typ: "tr-other"}), canonical(claims)), sKeys, raw, prod)
	refused("alg", "algorithm", s.sign(canonical(jwsHeader{Alg: "HS256", Kid: s.key.Kid, Typ: Typ}), canonical(claims)), sKeys, raw, prod)
	refused("header spacing", "canonical_header", s.sign([]byte(`{"alg": "EdDSA","kid":"routing-snapshot-key-2","typ":"tr-routing-snapshot-v2"}`), canonical(claims)), sKeys, raw, prod)
	refused("header field", "fields", s.sign([]byte(`{"alg":"EdDSA","kid":"routing-snapshot-key-2","typ":"tr-routing-snapshot-v2","x":"y"}`), canonical(claims)), sKeys, raw, prod)
	refused("v1 type", "type", s.sign(canonical(jwsHeader{Alg: "EdDSA", Kid: s.key.Kid, Typ: "tr-routing-snapshot-v1"}), canonical(claims)), sKeys, raw, prod)
	oldClaims := claims
	oldClaims.V = 1
	refused("v1 claims", "claims", s.sign(s.header(), canonical(oldClaims)), sKeys, raw, prod)
	extra := strings.Replace(string(canonical(claims)), `"v":2}`, `"v":2,"x":1}`, 1)
	refused("claims field", "claims", s.sign(s.header(), []byte(extra)), sKeys, raw, prod)
	float := strings.Replace(string(canonical(claims)), `"v":2}`, `"v":2.0}`, 1)
	refused("float claim", "json", s.sign(s.header(), []byte(float)), sKeys, raw, prod)
	duplicate := strings.Replace(string(canonical(claims)), `"v":2}`, `"v":2,"v":2}`, 1)
	refused("duplicate claim", "json", s.sign(s.header(), []byte(duplicate)), sKeys, raw, prod)
	spaced := strings.Replace(string(canonical(claims)), `"v":2}`, `"v": 2}`, 1)
	refused("claims spacing", "manifest issuer or encoding", s.sign(s.header(), []byte(spaced)), sKeys, raw, prod)
	wrongIss := claims
	wrongIss.Iss = "tr-someone-else"
	refused("issuer", "manifest issuer or encoding", s.sign(s.header(), canonical(wrongIss)), sKeys, raw, prod)
	wrongWindow := claims
	wrongWindow.NotAfter++
	refused("window", "manifest binding", s.sign(s.header(), canonical(wrongWindow)), sKeys, raw, VerifyOptions{
		Now: claims.CompiledAt, ExpectedPlane: goldenPlane, ExpectedAud: s.key.Aud,
	})
	refused("compact", "compact", s.sign(s.header(), canonical(claims))+".x", sKeys, raw, prod)
	padded := strings.Replace(s.sign(s.header(), canonical(claims)), ".", "=.", 1)
	refused("padding", "base64", padded, sKeys, raw, prod)

	// A body the claims bind exactly but that is not a v2 snapshot.
	notSnapshot := []byte(`{"v":2}`)
	bound := claims
	sum := sha256.Sum256(notSnapshot)
	bound.SHA256, bound.Size = hex.EncodeToString(sum[:]), int64(len(notSnapshot))
	if _, _, err := Verify(s.sign(s.header(), canonical(bound)), sKeys, notSnapshot, prod); !strings.HasPrefix(reason(err), "manifest binding") {
		t.Errorf("a bound non-snapshot body verified: %q", reason(err))
	}
}

// TestParseRefusals: the strict decoder refuses what the schema doc refuses,
// each beside the golden body, which it accepts.
func TestParseRefusals(t *testing.T) {
	_, raw := golden(t)
	if _, err := Parse(raw); err != nil {
		t.Fatalf("the golden body is refused: %v", err)
	}
	mutate := func(old, replacement string) []byte {
		t.Helper()
		out := bytes.Replace(raw, []byte(old), []byte(replacement), 1)
		if bytes.Equal(out, raw) {
			t.Fatalf("%q is not in the golden body", old)
		}
		return out
	}
	for name, body := range map[string][]byte{
		"unknown field":  mutate(`"epoch_seconds":3600`, `"epoch_seconds":3600,"extra":1`),
		"unknown nested": mutate(`"stores_content":true`, `"stores_content":true,"zdr":true`),
		// A snapshot compiled before models carried context_length.
		"no context_length":      mutate(`"context_length":65536,`, ``),
		"negative window":        mutate(`"context_length":65536,`, `"context_length":-1,`),
		"v1 without host bounds": mutate(`"v":2}`, `"v":1}`),
		"no host context":        mutate(`"context_length":0,`, ``),
		"negative host context":  mutate(`"context_length":0,`, `"context_length":-1,`),
		"missing field":          mutate(`"epoch_seconds":3600,`, ``),
		"float":                  mutate(`"epoch_seconds":3600`, `"epoch_seconds":3600.0`),
		"exponent":               mutate(`"epoch_seconds":3600`, `"epoch_seconds":36e2`),
		"duplicate key":          mutate(`"epoch_seconds":3600`, `"epoch_seconds":3600,"epoch_seconds":3600`),
		"null required":          mutate(`"epoch_seconds":3600`, `"epoch_seconds":null`),
		"string for int":         mutate(`"epoch_seconds":3600`, `"epoch_seconds":"3600"`),
		"lone surrogate":         mutate(`"golden-fixture"`, `"golden-\ud800"`),
		"trailing newline":       append(bytes.Clone(raw), '\n'),
		"two values":             append(bytes.Clone(raw), []byte(` {}`)...),
		"unknown pattern":        mutate(`"dated_suffix_pattern":"-\\d{4}-\\d{2}-\\d{2}$"`, `"dated_suffix_pattern":"-\\d{8}$"`),
		"unknown rounding":       mutate(`"rounding":"half_up_per_million"`, `"rounding":"half_even"`),
		"unknown usage":          mutate(`"usage_type":"Credits"`, `"usage_type":"Prepaid"`),
		"too long a window":      mutate(`"not_after":1792387800`, `"not_after":1792454401`),
	} {
		if _, err := Parse(body); err == nil {
			t.Errorf("%s: parsed", name)
		}
	}
	// A surrogate pair is a scalar, and Python accepts it.
	if err := surrogates([]byte(`{"a":"😀"}`)); err != nil {
		t.Errorf("a surrogate pair is refused: %v", err)
	}
}

// TestTheCasesNameTheGoldenSnapshot holds routing_cases_v1.json to the
// snapshot body its answers were computed over.
func TestTheCasesNameTheGoldenSnapshot(t *testing.T) {
	_, raw := golden(t)
	var cases struct {
		Snapshot       string            `json:"snapshot"`
		SnapshotSHA256 string            `json:"snapshot_sha256"`
		Cases          []json.RawMessage `json:"cases"`
	}
	if err := json.Unmarshal(testdata(t, "routing_cases_v1.json"), &cases); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if cases.SnapshotSHA256 != hex.EncodeToString(sum[:]) || cases.Snapshot != "fastpath/testdata/routing_snapshot_v2.body.json" {
		t.Fatalf("the cases name %s %s, not the golden body", cases.Snapshot, cases.SnapshotSHA256)
	}
	if len(cases.Cases) < 100 {
		t.Fatalf("%d cases", len(cases.Cases))
	}
}
