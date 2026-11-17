package gwauth

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// TestMaxPriceLimitsAreNotBodyFloats: a float that is provider.max_price
// itself or a member's value parses, whatever its spelling (the evaluator
// reads it, and names its own pass for one it does not take), and reaches
// the evaluator spelled as the body spelled it; a float anywhere else, a
// limit's insides and a key that only strips to max_price included, still
// passes as body.float. Each case beside a control in the other class.
func TestMaxPriceLimitsAreNotBodyFloats(t *testing.T) {
	for _, c := range []struct {
		name, provider string
	}{
		{"prompt", `{"max_price":{"prompt":0.95}}`},
		{"both, as written", `{"max_price":{"prompt":0.950000,"completion":3.30}}`},
		{"a tiny limit, not as repr writes it", `{"max_price":{"prompt":0.00005}}`},
		{"beside other preferences", `{"order":["wafer"],"max_price":{"completion":2.8},"sort":"price"}`},
		{"an exponent", `{"max_price":{"prompt":1E0}}`},
		{"a seventh place", `{"max_price":{"prompt":0.9500001}}`},
		{"negative", `{"max_price":{"completion":-1.5}}`},
		{"another member", `{"max_price":{"image":0.5}}`},
		{"max_price a float", `{"max_price":1.5}`},
	} {
		r, p := ParseRequest(bodyOf(`"provider":`+c.provider), jsonHeaders)
		if p.Passed() {
			t.Errorf("%s: passed %q at %q", c.name, p.Reason, p.FloatAt)
			continue
		}
		if string(r.Routing.Provider) != c.provider {
			t.Errorf("%s: the evaluator reads %s, the body sent %s", c.name, r.Routing.Provider, c.provider)
		}
	}
	for _, c := range []struct {
		name, want string
		body       []byte
	}{
		{"inside a limit", FloatAtProviderMaxPrice, bodyOf(`"provider":{"max_price":{"prompt":[0.5]}}`)},
		{"a key that strips to max_price", FloatAtProviderOther, bodyOf(`"provider":{" max_price":{"prompt":0.5}}`)},
		{"max_price outside provider", FloatAtMetadata, bodyOf(`"metadata":{"max_price":{"prompt":0.5}}`)},
		{"a top-level max_price", FloatAtExtra, bodyOf(`"max_price":{"prompt":0.5}`)},
		{"beside a limit", FloatAtProviderPreferredMaxLatency,
			bodyOf(`"provider":{"max_price":{"prompt":0.5},"preferred_max_latency":2.5}`)},
		{"provider not an object", FloatAtProviderOther, bodyOf(`"provider":[{"max_price":{"prompt":0.5}}]`)},
	} {
		if _, p := ParseRequest(c.body, jsonHeaders); p.Reason != ReasonFloat || p.FloatAt != c.want {
			t.Errorf("%s: %q at %q, want body.float at %q", c.name, p.Reason, p.FloatAt, c.want)
		}
	}
}

// TestALimitReachesTheFingerprintAsPythonsRepr: the material holds each
// limit as json.dumps writes the float json.loads made of it (0.950000 is
// 0.95, 3.30 is 3.3, 0.00005 is 5e-05), and the evaluator its literal.
func TestALimitReachesTheFingerprintAsPythonsRepr(t *testing.T) {
	r, p := ParseRequest(bodyOf(`"provider":{"max_price":{"prompt":0.950000,"completion":3.30},"sort":"price"}`),
		jsonHeaders)
	if p.Passed() {
		t.Fatal(p)
	}
	id, p := IdentityOf(r, jsonHeaders, "ws_test", "key_test", func() string { return "minted" })
	if p.Passed() {
		t.Fatal(p)
	}
	if want := `"provider":{"max_price":{"completion":3.3,"prompt":0.95},"sort":"price"}`; !strings.Contains(
		string(id.Material), want) {
		t.Errorf("material %s, want %s in it", id.Material, want)
	}
	tiny, _ := pyjson.Canonical(pyjson.Object{{Key: "prompt", Value: json.Number("0.00005")}})
	if string(tiny) != `{"prompt":5e-05}` {
		t.Errorf("0.00005 written %s, repr writes 5e-05", tiny)
	}
}

// reprOfMicro is Python's repr of the float a plainLimit literal reads as,
// worked from its exact microdollars with no float: repr's shortest digits
// of a decimal of at most 15 significant digits are its own (max_price.go),
// written with an exponent below 1e-4 (two exponent digits at least) and
// positionally otherwise, with ".0" on an integral value.
func reprOfMicro(micro int64) string {
	if micro == 0 {
		return "0.0"
	}
	text := strconv.FormatInt(micro, 10)
	decpt := len(text) - 6 // |v| = 0.text * 10^decpt
	digits := strings.TrimRight(text, "0")
	switch {
	case decpt <= -4:
		mantissa := digits[:1]
		if len(digits) > 1 {
			mantissa += "." + digits[1:]
		}
		return fmt.Sprintf("%se-%02d", mantissa, 1-decpt)
	case decpt <= 0:
		return "0." + strings.Repeat("0", -decpt) + digits
	case decpt < len(digits):
		return digits[:decpt] + "." + digits[decpt:]
	default:
		return digits + strings.Repeat("0", decpt-len(digits)) + ".0"
	}
}

// TestTheFingerprintSpellsEveryLimitGoReadsAsRepr holds the fingerprint's
// float path (pyjson: strconv's correctly rounded read, then repr's
// shortest digits) to reprOfMicro, which uses no float, over every
// plainLimit literal with an integer part of 0 (the values repr writes with
// an exponent among them) or 999,999,999 (fifteen significant digits, the
// most a limit has) and a six-digit fraction, two million, some spellings
// with trailing zeros, and a fixed-seed sample of the rest. The evaluator reads the same
// literals with no float (routingsnapshot.PlainLimitMicrodollars), so the
// fingerprint and the decision agree with Python's for each.
func TestTheFingerprintSpellsEveryLimitGoReadsAsRepr(t *testing.T) {
	if got := reprOfMicro(1); got != "1e-06" {
		t.Fatalf("reprOfMicro(1) = %s", got) // positive control on the oracle: repr(0.000001)
	}
	check := func(literal string) bool {
		micro, ok := routingsnapshot.PlainLimitMicrodollars(literal)
		if !ok {
			t.Errorf("%s: not a limit Go reads", literal)
			return false
		}
		got, err := pyjson.Canonical(json.Number(literal))
		want := reprOfMicro(micro)
		if !strings.Contains(literal, ".") {
			want = literal // an integer literal is an int, which json.dumps writes exactly
		}
		if err != nil || string(got) != want {
			t.Errorf("%s: the fingerprint writes %s (%v), repr is %s", literal, got, err, want)
			return false
		}
		return true
	}
	checked := 0
	for _, whole := range []string{"0", "999999999"} {
		for fraction := 0; fraction < 1_000_000; fraction++ {
			if !check(whole + "." + fmt.Sprintf("%06d", fraction)) {
				t.FailNow()
			}
			checked++
		}
	}
	for _, literal := range []string{"0.950000", "0.950", "3.30", "1.0", "1", "0", "0.0", "0.00005", "0.000100"} {
		if !check(literal) {
			t.FailNow()
		}
		checked++
	}
	rng := rand.New(rand.NewPCG(20261117, 1))
	for range 200_000 {
		literal := strconv.FormatInt(rng.Int64N(1_000_000_000), 10)
		if places := rng.IntN(7); places > 0 {
			literal += "." + fmt.Sprintf("%0*d", places, rng.Int64N(int64(pow10(places))))
		}
		if !check(literal) {
			t.FailNow()
		}
		checked++
	}
	t.Logf("%d limits spelled as repr", checked)
}

func pow10(n int) int {
	out := 1
	for range n {
		out *= 10
	}
	return out
}

// TestGoAnswersEveryMaxPriceVectorPythonFingerprinted: of the P1 bodies
// whose floats are all provider.max_price limits Go reads
// (max_price_float, and no pass-required label), every one Python answered
// Go answers too, with Python's fingerprint and scope; any Python refused
// Go does not answer.
func TestGoAnswersEveryMaxPriceVectorPythonFingerprinted(t *testing.T) {
	var file fingerprintFileT
	readJSON(t, fingerprintFile, &file)
	s := goldenSnapshot(t)
	answered := 0
	for _, c := range file.Cases {
		if !slices.Contains(c.Labels, "max_price_float") ||
			slices.ContainsFunc(c.Labels, func(l string) bool { return passRequired[l] }) {
			continue
		}
		d := decide(t, s, []byte(c.Body), headersOf(c.Headers), c.WorkspaceID, c.KeyHash, fingerprintKeyTags, peak)
		switch {
		case c.Expect.Outcome != "fingerprint":
			if d.answered {
				t.Errorf("%s: Go answers a body Python refused", c.Name)
			}
		case !d.answered:
			t.Errorf("%s: Python answered, Go passed (%s): %s", c.Name, d.reason, c.Body)
		case agrees(t, c.Name, d.identity, c.Expect.Fingerprint, c.Expect.IdempotencyKey, c.Expect.IdempotencyScope):
			answered++
		}
	}
	// The four hand-written limits at least (0.950000 and 3.30, 1.0,
	// 2.800000, 999999999.999999).
	if answered < 4 {
		t.Fatalf("%d max_price bodies answered with Python's fingerprint", answered)
	}
	t.Logf("%d max_price bodies answered with Python's fingerprint", answered)
}
