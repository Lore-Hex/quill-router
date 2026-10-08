package frontdoor

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// Envelope is an authorization's envelope (design §4.4): what its owner
// answers an authorize with, signed, and the gateway echoes on the
// request's heartbeats, settle and refund. A front door sends those to the
// owner and lease it names, and takes a terminal the owner does not take to
// that lease's drain log with the hold's estimate it carries (§4.5); so it
// acts on an envelope only once its seal holds.
//
// The spike's envelope carries what the front door and the owner need. The
// design's frozen candidates, prices and stage_d payload are the routing
// snapshot's and the gateway's, which the spike stands in for (spike plan,
// §2).
type Envelope struct {
	Auth      string `json:"a"`
	Workspace string `json:"w"`
	Lease     string `json:"l"`
	// Owner is the address of the owner node that admitted the hold.
	Owner     string    `json:"o"`
	Estimate  int64     `json:"e"`
	Stream    bool      `json:"s,omitempty"`
	EndOfLife time.Time `json:"eol"`
}

// MinKeySize is the least key a seal takes: HMAC-SHA256's block of output.
const MinKeySize = sha256.Size

// sealVersion begins every sealed envelope; a later format takes another.
const sealVersion = "v1"

// ErrSeal is an envelope that is not one this fleet's key sealed, or not
// whole.
var ErrSeal = errors.New("frontdoor: an envelope whose seal does not hold")

// strict decodes base64url only as Seal encodes it, its unused bits zero,
// so one envelope has one sealed form.
var strict = base64.RawURLEncoding.Strict()

// Seal signs an envelope with the fleet's key: the version, the envelope's
// JSON and its HMAC-SHA256 over the version and the JSON, each part
// base64url, joined by dots.
func Seal(key []byte, e Envelope) (string, error) {
	if len(key) < MinKeySize {
		return "", errors.New("frontdoor: a seal needs a key of at least 32 bytes")
	}
	if err := e.valid(); err != nil {
		return "", err
	}
	e.EndOfLife = e.EndOfLife.UTC()
	payload, err := json.Marshal(e)
	if err != nil {
		return "", err
	}
	return sealVersion + "." + base64.RawURLEncoding.EncodeToString(payload) + "." +
		base64.RawURLEncoding.EncodeToString(mac(key, payload)), nil
}

// Open checks a sealed envelope against the fleet's key and reads it. It
// takes only what Seal writes: a known version, each part in its one
// encoding, a seal that holds over the envelope's exact bytes, and an
// envelope with every field it needs and no other.
func Open(key []byte, sealed string) (Envelope, error) {
	parts := strings.Split(sealed, ".")
	if len(key) < MinKeySize || len(parts) != 3 || parts[0] != sealVersion {
		return Envelope{}, ErrSeal
	}
	payload, err := strict.DecodeString(parts[1])
	if err != nil {
		return Envelope{}, ErrSeal
	}
	sum, err := strict.DecodeString(parts[2])
	if err != nil || !hmac.Equal(sum, mac(key, payload)) {
		return Envelope{}, ErrSeal
	}
	var e Envelope
	d := json.NewDecoder(bytes.NewReader(payload))
	d.DisallowUnknownFields()
	if err := d.Decode(&e); err != nil || d.More() {
		return Envelope{}, ErrSeal
	}
	if err := e.valid(); err != nil {
		return Envelope{}, ErrSeal
	}
	return e, nil
}

func (e Envelope) valid() error {
	if e.Auth == "" || e.Workspace == "" || e.Lease == "" || e.Owner == "" || e.Estimate < 0 || e.EndOfLife.IsZero() {
		return errors.New("frontdoor: an envelope needs an authorization, a workspace, a lease, an owner, " +
			"an estimate that is not negative and an end of life")
	}
	return nil
}

func mac(key, payload []byte) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(sealVersion))
	m.Write([]byte{0})
	m.Write(payload)
	return m.Sum(nil)
}
