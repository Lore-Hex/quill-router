package frontdoor

import (
	"bytes"
	"encoding/base64"
	"errors"
	"strings"
	"testing"
	"time"
)

// sealedOver is a seal that holds over the payload, whatever it says.
func sealedOver(payload string) string {
	return sealVersion + "." + base64.RawURLEncoding.EncodeToString([]byte(payload)) + "." +
		base64.RawURLEncoding.EncodeToString(mac(key, []byte(payload)))
}

// TestAnEnvelopeOpensOnlyUnderItsSeal: an envelope opens as it was sealed,
// and not once any character of it changes, under another key or version,
// or with a field missing, added or out of range, though its seal holds.
func TestAnEnvelopeOpensOnlyUnderItsSeal(t *testing.T) {
	e := Envelope{Auth: "gwa-1", Workspace: "ws-1", Lease: "lease-1", Owner: "node-b", Estimate: 40, Stream: true,
		EndOfLife: time.Date(2026, 10, 8, 13, 0, 0, 0, time.FixedZone("x", 3600))}
	sealed, err := Seal(key, e)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Open(key, sealed)
	want := e
	want.EndOfLife = e.EndOfLife.UTC()
	if err != nil || got != want {
		t.Fatalf("opened %+v, %v; want %+v", got, err, want)
	}
	for i := range sealed {
		if sealed[i] == '.' {
			continue
		}
		changed := []byte(sealed)
		changed[i] = map[bool]byte{true: 'B', false: 'A'}[changed[i] == 'A']
		if _, err := Open(key, string(changed)); !errors.Is(err, ErrSeal) {
			t.Fatalf("character %d changed: %v", i, err)
		}
	}
	if _, err := Open(bytes.Repeat([]byte{8}, MinKeySize), sealed); !errors.Is(err, ErrSeal) {
		t.Fatalf("another key: %v", err)
	}
	if _, err := Open(key, "v2"+strings.TrimPrefix(sealed, sealVersion)); !errors.Is(err, ErrSeal) {
		t.Fatalf("another version: %v", err)
	}
	if _, err := Open(key, sealed+".x"); !errors.Is(err, ErrSeal) {
		t.Fatalf("a fourth part: %v", err)
	}
	// The seal's 32 bytes take 43 characters, the last with two bits unused:
	// setting one encodes the same bytes another way.
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	last := strings.IndexByte(alphabet, sealed[len(sealed)-1])
	other := sealed[:len(sealed)-1] + string(alphabet[last^1])
	if _, err := Open(key, other); !errors.Is(err, ErrSeal) {
		t.Fatalf("the seal in another encoding of its bytes: %v", err)
	}
	dot := strings.LastIndexByte(sealed, '.')
	for _, at := range []int{len(sealVersion) + 5, dot + 5} {
		for _, extra := range []string{"\n", "\r"} {
			if _, err := Open(key, sealed[:at]+extra+sealed[at:]); !errors.Is(err, ErrSeal) {
				t.Fatalf("%q inserted at %d: %v", extra, at, err)
			}
		}
	}
	whole := `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00Z"}`
	if _, err := Open(key, sealedOver(whole)); err != nil {
		t.Fatalf("a whole envelope sealed by hand: %v", err)
	}
	for name, payload := range map[string]string{
		"a field added":        `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00Z","x":1}`,
		"no authorization":     `{"w":"ws-1","l":"lease-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00Z"}`,
		"no workspace":         `{"a":"gwa-1","l":"lease-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00Z"}`,
		"no lease":             `{"a":"gwa-1","w":"ws-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00Z"}`,
		"no owner":             `{"a":"gwa-1","w":"ws-1","l":"lease-1","e":40,"eol":"2026-10-08T12:00:00Z"}`,
		"no end of life":       `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":40}`,
		"a negative estimate":  `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":-1,"eol":"2026-10-08T12:00:00Z"}`,
		"a second value after": whole + ` {}`,
		"no estimate":          `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","eol":"2026-10-08T12:00:00Z"}`,
		"a null estimate":      `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":null,"eol":"2026-10-08T12:00:00Z"}`,
		"a field twice":        `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":0,"e":40,"eol":"2026-10-08T12:00:00Z"}`,
		"a field in capitals":  `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":40,"E":0,"eol":"2026-10-08T12:00:00Z"}`,
		"a bracket after":      whole + `]`,
		"a brace after":        whole + `}`,
		"spaces":               `{"a": "gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00Z"}`,
		"another time form":    `{"a":"gwa-1","w":"ws-1","l":"lease-1","o":"node-b","e":40,"eol":"2026-10-08T12:00:00.000Z"}`,
	} {
		if _, err := Open(key, sealedOver(payload)); !errors.Is(err, ErrSeal) {
			t.Fatalf("%s: %v", name, err)
		}
	}
	if _, err := Seal(key[:MinKeySize-1], e); err == nil {
		t.Fatal("a seal under a short key")
	}
	if _, err := Open(key[:MinKeySize-1], sealed); !errors.Is(err, ErrSeal) {
		t.Fatalf("an open under a short key: %v", err)
	}
	bad := e
	bad.Owner = ""
	if _, err := Seal(key, bad); err == nil {
		t.Fatal("a seal of an envelope with no owner")
	}
}
