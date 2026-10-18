package store

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// bootKind is the entity kind an attested enclave's boot registers as
// (gateway_boot.py, GATEWAY_BOOT_KIND; the name predates the spend-lease
// pilot's removal and stays fixed for the fleet).
const bootKind = "spend_lease_boot"

// BootRecord is a registered boot as the edge checks it: its key id, its
// Ed25519 public key, whether its registration was verified, and the image
// digest it attested (storage_models.GatewayBoot).
type BootRecord struct {
	Kid         string
	PublicKey   ed25519.PublicKey
	Verified    bool
	ImageDigest string
}

type bootBody struct {
	Kid string `json:"kid"`
	JWK struct {
		Kty string `json:"kty"`
		Crv string `json:"crv"`
		X   string `json:"x"`
	} `json:"jwk"`
	Verified    bool   `json:"verified"`
	ImageDigest string `json:"image_digest"`
}

// ReadBoot reads a boot by its key id: one point read of production's entity
// table, by its primary key. A boot whose body is not one (its JWK not an
// Ed25519 key of 32 bytes) is an error, not a boot.
func (s *Store) ReadBoot(ctx context.Context, kid string) (BootRecord, bool, error) {
	if kid == "" {
		return BootRecord{}, false, errors.New("store: no boot key id")
	}
	ro := s.client.Single()
	defer ro.Close()
	row, err := ro.ReadRowWithOptions(ctx, "tr_entities", spanner.Key{bootKind, kid}, []string{"body"},
		&spanner.ReadOptions{RequestTag: tag("read-boot")})
	if spanner.ErrCode(err) == codes.NotFound {
		return BootRecord{}, false, nil
	}
	if err != nil {
		return BootRecord{}, false, err
	}
	var body string
	if err := row.Column(0, &body); err != nil {
		return BootRecord{}, false, err
	}
	boot, err := parseBoot(kid, body)
	if err != nil {
		return BootRecord{}, false, err
	}
	return boot, true, nil
}

func parseBoot(kid, body string) (BootRecord, error) {
	var b bootBody
	if err := json.Unmarshal([]byte(body), &b); err != nil {
		return BootRecord{}, fmt.Errorf("store: boot %s: %w", kid, err)
	}
	if b.JWK.Kty != "OKP" || b.JWK.Crv != "Ed25519" {
		return BootRecord{}, fmt.Errorf("store: boot %s: its JWK is not an Ed25519 key", kid)
	}
	public, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(b.JWK.X, "="))
	if err != nil || len(public) != ed25519.PublicKeySize {
		return BootRecord{}, fmt.Errorf("store: boot %s: its public key is not 32 bytes", kid)
	}
	return BootRecord{Kid: b.Kid, PublicKey: ed25519.PublicKey(public), Verified: b.Verified, ImageDigest: b.ImageDigest}, nil
}
