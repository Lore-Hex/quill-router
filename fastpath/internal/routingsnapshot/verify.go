package routingsnapshot

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"regexp"
	"strings"
)

// The manifest's literals (src/trusted_router/routing_snapshot_signing.py).
const (
	Purpose = "routing-snapshot"
	Typ     = "tr-routing-snapshot-v2"
	// TestKID and TestPublicKey name the golden files' key, derived from a
	// public seed in scripts/fastpath/routing_snapshot_vectors.py. Anyone can
	// sign with it, so Verify refuses it unless AllowTestKey is set.
	TestKID       = "routing-snapshot-test-key-1"
	TestPublicKey = "U1I1REk6JqeFqKq5ZGH0P8b1b_OEwbFG0lCOkYL-jjY"

	maxToken      = 65536
	maxClaimDepth = 16
)

var (
	identity = regexp.MustCompile(`^[A-Za-z0-9_./:@+\-]{1,512}$`)
	digest   = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// TrustedKey is a verifier's accepted manifest key: its kid, purpose, Ed25519
// public key, and the issuer and audience it signs for (detached_jws.TrustedKey).
type TrustedKey struct {
	Kid       string
	Purpose   string
	PublicKey ed25519.PublicKey
	Iss       string
	Aud       string
}

// VerifyOptions are the verifier's own facts: the instant (Unix seconds), its
// plane and audience, and whether the public test key may be accepted.
type VerifyOptions struct {
	Now           int64
	ExpectedPlane Plane
	ExpectedAud   string
	AllowTestKey  bool
}

// Claims are a manifest's claims; the canonical encoding of this struct (its
// fields are in lexical order) is the signed payload.
type Claims struct {
	Aud            string `json:"aud"`
	CompiledAt     int64  `json:"compiled_at"`
	Iss            string `json:"iss"`
	NotAfter       int64  `json:"not_after"`
	Plane          Plane  `json:"plane"`
	SHA256         string `json:"sha256"`
	Size           int64  `json:"size"`
	SourceRevision string `json:"source_revision"`
	V              int64  `json:"v"`
}

type jwsHeader struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Typ string `json:"typ"`
}

// VerifyError names the check that refused a manifest, in
// routing_snapshot_signing's and detached_jws's words.
type VerifyError struct{ Reason string }

func (e *VerifyError) Error() string { return "routing snapshot manifest: " + e.Reason }

func refuse(reason string) error { return &VerifyError{Reason: reason} }

func canonical(v any) []byte {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(v)
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
}

func b64decode(value string) ([]byte, error) {
	if value == "" || strings.Trim(value, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-") != "" {
		return nil, refuse("base64")
	}
	raw, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || base64.RawURLEncoding.EncodeToString(raw) != value {
		return nil, refuse("base64")
	}
	return raw, nil
}

// jwsString is detached_jws._string: non-empty printable ASCII without
// backslash, quote or HTML-significant characters.
func jwsString(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c < 0x20 || c > 0x7e || strings.IndexByte(`\"<>&`, c) >= 0 {
			return false
		}
	}
	return true
}

func isTestKey(k TrustedKey) bool {
	return k.Kid == TestKID || base64.RawURLEncoding.EncodeToString(k.PublicKey) == TestPublicKey
}

// Verify checks a routing snapshot manifest against the body bytes received
// and returns the parsed snapshot, exactly as verify_manifest does:
//
//   - the compact JWS: canonical header {alg EdDSA, kid, typ}, the one key of
//     that kid with this purpose, and its Ed25519 signature;
//   - the public test key is refused unless AllowTestKey is set;
//   - the claims: exactly the v1 fields, strict integers, the signed payload
//     their canonical encoding, iss and aud the key's;
//   - aud the verifier's own, and the plane the verifier's own;
//   - the binding: sha256 and size of the bytes received (never re-encoded),
//     and the body's own compiled_at, not_after, source_revision and plane;
//   - freshness: compiled_at <= now < not_after.
//
// The body is then decoded strictly (Parse).
func Verify(token string, keys []TrustedKey, body []byte, opts VerifyOptions) (*Snapshot, Claims, error) {
	var claims Claims
	payload, key, err := verifyJWS(token, keys, Typ, opts.AllowTestKey)
	if err != nil {
		return nil, claims, err
	}
	if err := exact(payload, &claims); err != nil || !validClaims(claims) {
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
	snapshot, err := Parse(body)
	if err != nil {
		return nil, claims, refuse("manifest binding: " + err.Error())
	}
	if snapshot.CompiledAt != claims.CompiledAt || snapshot.NotAfter != claims.NotAfter ||
		snapshot.SourceRevision != claims.SourceRevision || snapshot.Plane != claims.Plane {
		return nil, claims, refuse("manifest binding")
	}
	if !(claims.CompiledAt <= opts.Now && opts.Now < claims.NotAfter) {
		return nil, claims, refuse("snapshot freshness")
	}
	return snapshot, claims, nil
}

// verifyJWS checks a compact JWS of this typ, signed by the routing-snapshot
// key (detached_jws.verify): the canonical header {alg EdDSA, kid, typ}, the
// typ before anything else is read (so neither the snapshot's nor the edge
// policy's manifest can stand in for the other), the one key of that kid
// with this purpose, its Ed25519 signature, and the public test key refused
// unless allowed. It returns the signed payload, wire-checked, and the key.
func verifyJWS(token string, keys []TrustedKey, typ string, allowTestKey bool) ([]byte, TrustedKey, error) {
	var none TrustedKey
	if len(token) > maxToken || strings.Count(token, ".") != 2 {
		return nil, none, refuse("compact")
	}
	parts := strings.Split(token, ".")
	headerBytes, err := b64decode(parts[0])
	if err != nil {
		return nil, none, err
	}
	if err := wireCheck(headerBytes, maxClaimDepth, false); err != nil {
		return nil, none, refuse("json")
	}
	var header jwsHeader
	if err := exact(headerBytes, &header); err != nil || !jwsString(header.Alg) ||
		!jwsString(header.Kid) || !jwsString(header.Typ) {
		return nil, none, refuse("fields")
	}
	if !bytes.Equal(headerBytes, canonical(header)) {
		return nil, none, refuse("canonical_header")
	}
	if header.Alg != "EdDSA" {
		return nil, none, refuse("algorithm")
	}
	if header.Typ != typ {
		return nil, none, refuse("type")
	}
	var matches []TrustedKey
	for _, k := range keys {
		if k.Kid == header.Kid {
			matches = append(matches, k)
		}
	}
	if len(matches) != 1 {
		return nil, none, refuse("key")
	}
	key := matches[0]
	if key.Purpose != Purpose {
		return nil, none, refuse("purpose")
	}
	payload, err := b64decode(parts[1])
	if err != nil {
		return nil, none, err
	}
	if err := wireCheck(payload, maxClaimDepth, false); err != nil {
		return nil, none, refuse("json")
	}
	signature, err := b64decode(parts[2])
	if err != nil {
		return nil, none, err
	}
	if len(key.PublicKey) != ed25519.PublicKeySize ||
		!ed25519.Verify(key.PublicKey, []byte(parts[0]+"."+parts[1]), signature) {
		return nil, none, refuse("signature")
	}
	if isTestKey(key) && !allowTestKey {
		return nil, none, refuse("test key outside test mode")
	}
	return payload, key, nil
}

// exact decodes JSON that wireCheck has accepted into v, refusing missing
// and unknown fields and null.
func exact(raw []byte, v any) error {
	var generic any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&generic); err != nil {
		return err
	}
	if err := shape(generic, reflect.TypeOf(v).Elem(), "$"); err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

func validClaims(c Claims) bool {
	for _, value := range []string{c.SourceRevision, c.Plane.Cloud, c.Plane.Region, c.Iss, c.Aud} {
		if !identity.MatchString(value) {
			return false
		}
	}
	return c.V == snapshotVersion && digest.MatchString(c.SHA256) &&
		c.Size >= 0 && c.CompiledAt >= 0 && c.NotAfter >= 0
}
