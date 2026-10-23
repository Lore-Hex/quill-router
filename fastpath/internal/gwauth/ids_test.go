package gwauth

import (
	"regexp"
	"testing"
)

// TestMintedIDsHavePythonsForms: uuid4 with its version and variant bits,
// in the form each column holds, and no two alike.
func TestMintedIDsHavePythonsForms(t *testing.T) {
	authorization := regexp.MustCompile(`^gwa-[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$`)
	hyphenatedV4 := regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	seen := map[string]bool{}
	for range 1000 {
		a, r, k := NewAuthorizationID(), NewReservationID(), NewIdempotencyKey()
		if !authorization.MatchString(a) || !authorizationIDForm.MatchString(a) {
			t.Fatalf("authorization id %q", a)
		}
		if !hyphenatedV4.MatchString(r) || !reservationIDForm.MatchString(r) {
			t.Fatalf("reservation id %q", r)
		}
		if !hyphenatedV4.MatchString(k) {
			t.Fatalf("idempotency key %q", k)
		}
		for _, id := range []string{a, r, k} {
			if seen[id] {
				t.Fatalf("%q twice", id)
			}
			seen[id] = true
		}
	}
}

// TestGenerationIDIsUUID5: Python's uuid5 for known names, and every
// answered golden's generation id (the 200 test compares it too).
func TestGenerationIDIsUUID5(t *testing.T) {
	// uuid.uuid5(uuid.NAMESPACE_URL, "trustedrouter:gwa-da235f8103b541818b0a9a656b52460a").hex
	if got := GenerationID("gwa-da235f8103b541818b0a9a656b52460a"); got != "gen-b5a7f86d351a524a9a7d6727780c093c" {
		t.Errorf("GenerationID = %s", got)
	}
	f := loadGoldens(t)
	n := 0
	for _, c := range answered(f) {
		if got := GenerationID(c.IDs.AuthorizationID); got != c.Expect.GenerationID {
			t.Errorf("%s: %s, want %s", c.Name, got, c.Expect.GenerationID)
		}
		n++
	}
	if n < 150 {
		t.Fatalf("%d answered cases", n)
	}
}
