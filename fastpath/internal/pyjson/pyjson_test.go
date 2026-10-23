package pyjson

import (
	"encoding/json"
	"math"
	"math/big"
	"strings"
	"testing"
	"time"
)

// The float cases the E2 plan names, written out so a reader sees them
// without the vectors (which hold them too, from repr itself).
func TestFormatFloatNamedCases(t *testing.T) {
	// Variables, not constants: Go adds constants exactly, so 0.1 + 0.2
	// written as constants is 0.3.
	tenth, fifth := 0.1, 0.2
	for _, c := range []struct {
		f    float64
		want string
	}{
		{1e-05, "1e-05"},
		{0.0001, "0.0001"},
		{1e16, "1e+16"},
		{9999999999999998.0, "9999999999999998.0"},
		{5e-324, "5e-324"},
		{1.7976931348623157e308, "1.7976931348623157e+308"},
		{tenth + fifth, "0.30000000000000004"},
		{math.Copysign(0, -1), "-0.0"},
		{0, "0.0"},
		{1, "1.0"},
		{100, "100.0"},
		{1e15, "1000000000000000.0"},
		{-1.5e-05, "-1.5e-05"},
		{123.456, "123.456"},
	} {
		if got := FormatFloat(c.f); got != c.want {
			t.Errorf("FormatFloat(%v) = %q, want %q", c.f, got, c.want)
		}
	}
}

// Python writes an Object's members in the order given unless sorted, and
// a Python dict cannot hold a key twice.
func TestDumpsOrderAndRefusals(t *testing.T) {
	tree := Object{
		{"b", 1}, {"a", []any{json.Number("-0"), json.Number("1E2"), 0.5, int64(-7), big.NewInt(0).Lsh(big.NewInt(1), 70)}},
		{"é", []string{"\u007f", "😀"}}, {"c", Object{}}, {"d", nil}, {"e", true},
	}
	got, err := Dumps(tree, Options{})
	if want := `{"b":1,"a":[0,100.0,0.5,-7,1180591620717411303424],"é":["` + "\u007f" + `","😀"],"c":{},"d":null,"e":true}`; err != nil || string(got) != want {
		t.Fatalf("insertion order: %s, %v; want %s", got, err, want)
	}
	got, err = Canonical(tree)
	const bs = "\x5c" // a backslash: the escapes below are the output's text
	if want := `{"a":[0,100.0,0.5,-7,1180591620717411303424],"b":1,"c":{},"d":null,"e":true,"` +
		bs + `u00e9":["` + bs + `u007f","` + bs + `ud83d` + bs + `ude00"]}`; err != nil || string(got) != want {
		t.Fatalf("canonical: %s, %v; want %s", got, err, want)
	}
	for name, bad := range map[string]any{
		"a key twice":        Object{{"a", 1}, {"a", 2}},
		"NaN":                math.NaN(),
		"an infinity":        []any{math.Inf(-1)},
		"a map":              map[string]any{"a": 1},
		"an int32":           int32(1),
		"invalid UTF-8":      "\xff",
		"an invalid key":     Object{{"\xc3", 1}},
		"not a number":       json.Number("1."),
		"a number and more":  json.Number("1 "),
		"an infinite number": json.Number("1e400"),
		"4301 digits":        json.Number(strings.Repeat("1", 4301)),
		"a nil *big.Int":     (*big.Int)(nil),
	} {
		if out, err := Canonical(bad); err == nil {
			t.Errorf("%s written as %s", name, out)
		}
	}
	if out, err := Canonical(json.Number(strings.Repeat("9", 4300))); err != nil || len(out) != 4300 {
		t.Errorf("4300 digits: %d bytes, %v", len(out), err)
	}
	if out, err := Canonical(json.Number("1e-400")); err != nil || string(out) != "0.0" {
		t.Errorf("an underflow: %s, %v", out, err)
	}
}

// Bytes a Python str cannot come from: Decode refuses them before
// json.loads's rules apply.
func TestDecodeRefusesWhatIsNotUTF8(t *testing.T) {
	for _, in := range []string{"\"\xff\"", "\xef\xbb\xbf{}", "\"\xed\xa0\x80\"", "[\"\xc3\"]"} {
		if tree, err := Decode([]byte(in)); err == nil {
			t.Errorf("Decode(%q) = %v", in, tree)
		}
	}
}

// Decode keeps each number's literal, so the tree carries an integer of
// any size exactly and a float exactly as written.
func TestDecodeKeepsLiterals(t *testing.T) {
	tree, err := Decode([]byte(` {"n": 18446744073709551616, "f": 1.10, "e": -0E-0, "s": "\/` + "\x5cu00E9" + `"} `))
	if err != nil {
		t.Fatal(err)
	}
	want := Object{{"n", json.Number("18446744073709551616")}, {"f", json.Number("1.10")}, {"e", json.Number("-0E-0")}, {"s", "/é"}}
	got := tree.(Object)
	if len(got) != len(want) {
		t.Fatalf("%v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("member %d: %#v, want %#v", i, got[i], want[i])
		}
	}
}

func TestISOFormatTruncatesToTheMicrosecondInUTC(t *testing.T) {
	at := time.Date(2026, 10, 23, 12, 0, 0, 999, time.FixedZone("x", -5*3600))
	if got := ISOFormat(at); got != "2026-10-23T17:00:00Z" {
		t.Fatalf("%q", got)
	}
	if got := ISOFormat(at.Add(1001 * time.Nanosecond)); got != "2026-10-23T17:00:00.000002Z" {
		t.Fatalf("%q", got)
	}
}
