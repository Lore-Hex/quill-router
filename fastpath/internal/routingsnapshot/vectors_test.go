package routingsnapshot

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	typ     = "tr-routing-snapshot-v1"
	purpose = "routing-snapshot"
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

// header's fields are in lexical order, so json.Marshal writes Python's
// canonical encoding of it.
type header struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	Typ string `json:"typ"`
}

type plane struct {
	Cloud  string `json:"cloud"`
	Region string `json:"region"`
}

type claims struct {
	V              int64  `json:"v"`
	SHA256         string `json:"sha256"`
	Size           int64  `json:"size"`
	CompiledAt     int64  `json:"compiled_at"`
	NotAfter       int64  `json:"not_after"`
	SourceRevision string `json:"source_revision"`
	Plane          plane  `json:"plane"`
	Iss            string `json:"iss"`
	Aud            string `json:"aud"`
}

type body struct {
	V              int64  `json:"v"`
	Kind           string `json:"kind"`
	CompiledAt     int64  `json:"compiled_at"`
	NotAfter       int64  `json:"not_after"`
	EpochSeconds   int64  `json:"epoch_seconds"`
	SourceRevision string `json:"source_revision"`
	Plane          plane  `json:"plane"`
	Endpoints      []struct {
		ID     string `json:"id"`
		Epochs []struct {
			StartsAt int64 `json:"starts_at"`
		} `json:"epochs"`
	} `json:"endpoints"`
}

func strict(raw []byte, v any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	return decoder.Decode(v)
}

// verify checks a manifest against the bytes received, never re-encoding them.
func verify(m manifestFile, raw []byte) (claims, error) {
	var c claims
	parts := strings.Split(m.JWS, ".")
	if len(parts) != 3 {
		return c, errors.New("compact")
	}
	decoded := make([][]byte, 3)
	for i, part := range parts {
		b, err := base64.RawURLEncoding.DecodeString(part)
		if err != nil {
			return c, fmt.Errorf("base64: %w", err)
		}
		decoded[i] = b
	}
	want, _ := json.Marshal(header{"EdDSA", m.Kid, typ})
	if !bytes.Equal(decoded[0], want) {
		return c, fmt.Errorf("header %s", decoded[0])
	}
	key, err := base64.RawURLEncoding.DecodeString(m.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || m.Purpose != purpose {
		return c, errors.New("key")
	}
	if !ed25519.Verify(ed25519.PublicKey(key), []byte(parts[0]+"."+parts[1]), decoded[2]) {
		return c, errors.New("signature")
	}
	if err := strict(decoded[1], &c); err != nil {
		return c, fmt.Errorf("claims: %w", err)
	}
	sum := sha256.Sum256(raw)
	if c.V != 1 || c.SHA256 != hex.EncodeToString(sum[:]) || c.Size != int64(len(raw)) {
		return c, errors.New("binding")
	}
	if c.Iss != m.Iss || c.Aud != m.Aud {
		return c, errors.New("issuer")
	}
	var b body
	if err := json.Unmarshal(raw, &b); err != nil {
		return c, fmt.Errorf("body: %w", err)
	}
	if b.V != 1 || b.Kind != "tr-routing-snapshot" || b.CompiledAt != c.CompiledAt ||
		b.NotAfter != c.NotAfter || b.SourceRevision != c.SourceRevision || b.Plane != c.Plane {
		return c, errors.New("body disagrees with its manifest")
	}
	return c, nil
}

func golden(t *testing.T) (manifestFile, []byte) {
	t.Helper()
	dir := filepath.Join("..", "..", "testdata")
	raw, err := os.ReadFile(filepath.Join(dir, "routing_snapshot_v1.body.json"))
	if err != nil {
		t.Fatal(err)
	}
	text, err := os.ReadFile(filepath.Join(dir, "routing_snapshot_v1.manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var m manifestFile
	if err := strict(text, &m); err != nil {
		t.Fatal(err)
	}
	return m, raw
}

// TestTheGoldenManifestSignsTheGoldenBody holds Go's verification to the
// files tests/test_fastpath_routing_snapshot_vectors.py holds to Python.
func TestTheGoldenManifestSignsTheGoldenBody(t *testing.T) {
	m, raw := golden(t)
	if !m.TestKey || !strings.Contains(m.Kid, "test") {
		t.Fatalf("the golden manifest must be signed by the named test key, not %q", m.Kid)
	}
	c, err := verify(m, raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) == 0 || raw[len(raw)-1] == '\n' || bytes.IndexFunc(raw, func(r rune) bool { return r > 0x7e || r < 0x20 }) >= 0 {
		t.Fatal("the body must be printable ASCII with no trailing newline")
	}
	var b body
	if err := json.Unmarshal(raw, &b); err != nil {
		t.Fatal(err)
	}
	if b.EpochSeconds != 3600 || c.NotAfter <= c.CompiledAt || len(b.Endpoints) == 0 {
		t.Fatalf("window %d..%d, %d endpoints", c.CompiledAt, c.NotAfter, len(b.Endpoints))
	}
	for _, e := range b.Endpoints {
		if len(e.Epochs) == 0 || e.Epochs[0].StartsAt != c.CompiledAt {
			t.Errorf("%s: the first epoch must start at compiled_at", e.ID)
		}
		for i, epoch := range e.Epochs {
			if epoch.StartsAt >= c.NotAfter || (i > 0 && epoch.StartsAt <= e.Epochs[i-1].StartsAt) {
				t.Errorf("%s: epochs must increase within the window", e.ID)
			}
		}
	}
}

func TestTamperingIsRefused(t *testing.T) {
	m, raw := golden(t)
	altered := bytes.Replace(raw, []byte(`"golden-fixture"`), []byte(`"golden-fixturf"`), 1)
	if bytes.Equal(altered, raw) {
		t.Fatal("the alteration must change the body")
	}
	if _, err := verify(m, altered); err == nil || err.Error() != "binding" {
		t.Errorf("an altered body verified: %v", err)
	}
	parts := strings.Split(m.JWS, ".")
	signature, _ := base64.RawURLEncoding.DecodeString(parts[2])
	signature[0] ^= 1
	forged := m
	forged.JWS = parts[0] + "." + parts[1] + "." + base64.RawURLEncoding.EncodeToString(signature)
	if _, err := verify(forged, raw); err == nil || err.Error() != "signature" {
		t.Errorf("a forged signature verified: %v", err)
	}
	impostor := m
	impostor.PublicKey = base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.PublicKeySize))
	if _, err := verify(impostor, raw); err == nil {
		t.Error("another key verified the manifest")
	}
	renamed := m
	renamed.Kid = "routing-snapshot-other-key"
	if _, err := verify(renamed, raw); err == nil {
		t.Error("a manifest verified under another kid")
	}
}
