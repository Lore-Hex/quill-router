package gwauth

import (
	"crypto/rand"
	"crypto/sha1" //nolint:gosec // uuid5 is SHA-1 by definition (RFC 9562 §5.5); it is an id, not a digest of trust.
	"encoding/hex"
	"regexp"
)

// The ids an authorization the edge decides mints, as Python mints them
// (docs/design/fast-admission-e2-decide.md, §5 and §6). Each is random
// from crypto/rand; the caller injects them into ComposeInput, so a test
// pins them.

// NewAuthorizationID is "gwa-" + uuid4().hex (gateway.py, the
// authorization's id): a uuid4's 32 lowercase hex digits, no hyphens.
func NewAuthorizationID() string {
	u := uuid4()
	return "gwa-" + hex.EncodeToString(u[:])
}

// NewReservationID is str(uuid4()): the hyphenated lowercase form, which
// tr_reservation.reservation_id and the payload's credit_reservation_id
// hold.
func NewReservationID() string {
	u := uuid4()
	return hyphenated(u)
}

// NewIdempotencyKey is the key Python mints when the request presents none,
// str(uuid4()) (gateway.py:1119-1120): IdentityOf's mint.
func NewIdempotencyKey() string {
	u := uuid4()
	return hyphenated(u)
}

// GenerationID is generation_id_for_authorization (storage_models.py):
// "gen-" + uuid5(NAMESPACE_URL, "trustedrouter:" + authorizationID).hex.
func GenerationID(authorizationID string) string {
	h := sha1.New() //nolint:gosec // see the import.
	h.Write(namespaceURL[:])
	h.Write([]byte("trustedrouter:" + authorizationID))
	var u [16]byte
	copy(u[:], h.Sum(nil))
	u[6] = u[6]&0x0f | 0x50 // version 5
	u[8] = u[8]&0x3f | 0x80 // RFC 4122 variant
	return "gen-" + hex.EncodeToString(u[:])
}

// namespaceURL is uuid.NAMESPACE_URL, 6ba7b811-9dad-11d1-80b4-00c04fd430c8.
var namespaceURL = [16]byte{
	0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1,
	0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
}

// uuid4 is uuid.uuid4(): 122 random bits, version 4, the RFC 4122 variant.
// crypto/rand.Read never fails (it panics rather than return short).
func uuid4() [16]byte {
	var u [16]byte
	_, _ = rand.Read(u[:])
	u[6] = u[6]&0x0f | 0x40
	u[8] = u[8]&0x3f | 0x80
	return u
}

func hyphenated(u [16]byte) string {
	s := hex.EncodeToString(u[:])
	return s[0:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:32]
}

// The forms Stage accepts: an id of any other shape is not one this edge
// (or Python) minted, and Stage refuses it rather than write it.
var (
	authorizationIDForm = regexp.MustCompile(`^gwa-[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$`)
	reservationIDForm   = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
)
