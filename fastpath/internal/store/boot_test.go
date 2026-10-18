package store

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"testing"

	"cloud.google.com/go/spanner"
)

func TestABootIsReadByItsKeyIdFromTheEntityTable(t *testing.T) {
	s := spikeStore(t)
	public, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	x := base64.RawURLEncoding.EncodeToString(public)
	rows := []struct{ kid, body string }{
		{"boot-ok", `{"kid":"boot-ok","jwk":{"kty":"OKP","crv":"Ed25519","x":"` + x + `"},"approved":true,"verified":true,` +
			`"image_digest":"sha256:abc","attestation_kind":"gcp-cs-jwt","registered_at":"2026-10-18T00:00:00Z"}`},
		{"boot-unverified", `{"kid":"boot-unverified","jwk":{"kty":"OKP","crv":"Ed25519","x":"` + x + `"},"verified":false,"image_digest":"sha256:abc"}`},
		{"boot-rsa", `{"kid":"boot-rsa","jwk":{"kty":"RSA","n":"x"},"verified":true,"image_digest":"sha256:abc"}`},
		{"boot-short", `{"kid":"boot-short","jwk":{"kty":"OKP","crv":"Ed25519","x":"AAAA"},"verified":true,"image_digest":"sha256:abc"}`},
	}
	var mutations []*spanner.Mutation
	for _, r := range rows {
		mutations = append(mutations, spanner.Insert("tr_entities", []string{"kind", "id", "body", "updated_at"},
			[]any{bootKind, r.kid, r.body, spanner.CommitTimestamp}))
	}
	// Another kind with the same id is not a boot.
	mutations = append(mutations, spanner.Insert("tr_entities", []string{"kind", "id", "body", "updated_at"},
		[]any{"api_key", "boot-other-kind", `{}`, spanner.CommitTimestamp}))
	if _, err := s.client.Apply(context.Background(), mutations); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	got, found, err := s.ReadBoot(ctx, "boot-ok")
	if err != nil || !found || got.Kid != "boot-ok" || !got.Verified || got.ImageDigest != "sha256:abc" || !got.PublicKey.Equal(public) {
		t.Fatalf("the boot: %+v %v %v", got, found, err)
	}
	got, found, err = s.ReadBoot(ctx, "boot-unverified")
	if err != nil || !found || got.Verified {
		t.Fatalf("an unverified boot: %+v %v %v", got, found, err)
	}
	for _, kid := range []string{"boot-rsa", "boot-short"} {
		if _, _, err := s.ReadBoot(ctx, kid); err == nil {
			t.Fatalf("%s: a body that is not a boot was read as one", kid)
		}
	}
	for _, kid := range []string{"boot-none", "boot-other-kind"} {
		if _, found, err := s.ReadBoot(ctx, kid); err != nil || found {
			t.Fatalf("%s: found %v err %v", kid, found, err)
		}
	}
	if _, _, err := s.ReadBoot(ctx, ""); err == nil {
		t.Fatal("an empty key id")
	}
}
