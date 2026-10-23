package pyjson

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type vectors struct {
	Generator string `json:"generator"`
	Floats    []struct {
		Bits string `json:"bits"`
		Repr string `json:"repr"`
	} `json:"floats"`
	Money []struct {
		Microdollars int64  `json:"microdollars"`
		Repr         string `json:"repr"`
	} `json:"money"`
	Strings []struct {
		Codepoints []rune `json:"codepoints"`
		ASCII      string `json:"ascii"`
		Unicode    string `json:"unicode"`
	} `json:"strings"`
	Documents []struct {
		Text      string `json:"text"`
		Canonical string `json:"canonical"`
		Response  string `json:"response"`
	} `json:"documents"`
	Refused []struct {
		Text      string `json:"text"`
		Python    string `json:"python"`
		GoRefuses string `json:"go_refuses"`
	} `json:"refused"`
	ISOFormat []struct {
		UnixMicros int64  `json:"unix_micros"`
		ISOFormat  string `json:"isoformat"`
	} `json:"isoformat"`
}

// golden reads fastpath/testdata/pyjson_vectors_v1.json, which CPython
// writes (scripts/fastpath/pyjson_vectors.py) and
// tests/test_fastpath_pyjson_vectors.py keeps current.
func golden(t *testing.T) vectors {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", "pyjson_vectors_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if v.Generator != "scripts/fastpath/pyjson_vectors.py" {
		t.Fatalf("the vectors name the generator %q", v.Generator)
	}
	return v
}

// TestFormatFloatIsFloatRepr holds FormatFloat to repr() of every double
// in the vectors, given by its bits: the thresholds of the exponent form
// and their neighbours, integral values, subnormals, the extremes, and
// random bit patterns and decimals.
func TestFormatFloatIsFloatRepr(t *testing.T) {
	v := golden(t)
	if len(v.Floats) < 2000 {
		t.Fatalf("the vectors hold %d floats", len(v.Floats))
	}
	for _, c := range v.Floats {
		raw, err := hex.DecodeString(c.Bits)
		if err != nil || len(raw) != 8 {
			t.Fatalf("bits %q", c.Bits)
		}
		f := math.Float64frombits(binary.BigEndian.Uint64(raw))
		if got := FormatFloat(f); got != c.Repr {
			t.Errorf("FormatFloat(%s) = %q, repr %q", c.Bits, got, c.Repr)
		}
	}
}

// TestMoneyFloatIsMicrodollarsToFloat: money.microdollars_to_float is
// float(m) / 1_000_000, which float64(m) / 1e6 is (one rounding each, to
// nearest even), and its repr is FormatFloat's.
func TestMoneyFloatIsMicrodollarsToFloat(t *testing.T) {
	v := golden(t)
	if len(v.Money) < 500 {
		t.Fatalf("the vectors hold %d amounts", len(v.Money))
	}
	for _, c := range v.Money {
		if got := FormatFloat(float64(c.Microdollars) / 1_000_000); got != c.Repr {
			t.Errorf("%d microdollars: %q, Python %q", c.Microdollars, got, c.Repr)
		}
	}
}

func TestStringsAreJSONDumps(t *testing.T) {
	v := golden(t)
	if len(v.Strings) < 400 {
		t.Fatalf("the vectors hold %d strings", len(v.Strings))
	}
	for _, c := range v.Strings {
		s := string(c.Codepoints)
		for _, mode := range []struct {
			ensureASCII bool
			want        string
		}{{true, c.ASCII}, {false, c.Unicode}} {
			got, err := Dumps(s, Options{EnsureASCII: mode.ensureASCII})
			if err != nil || string(got) != mode.want {
				t.Errorf("Dumps(%U, ensure_ascii=%v) = %q, %v; Python %q", c.Codepoints, mode.ensureASCII, got, err, mode.want)
			}
		}
	}
}

// TestDocumentsAreJSONBodyAndJSONResponse decodes each document and writes
// it both ways: Canonical is json_body(json.loads(text)), and the
// insertion-ordered Unicode form is JSONResponse(json.loads(text)).body.
func TestDocumentsAreJSONBodyAndJSONResponse(t *testing.T) {
	v := golden(t)
	if len(v.Documents) < 300 {
		t.Fatalf("the vectors hold %d documents", len(v.Documents))
	}
	for _, c := range v.Documents {
		tree, err := Decode([]byte(c.Text))
		if err != nil {
			t.Errorf("Decode(%.60q): %v", c.Text, err)
			continue
		}
		canonical, err := Canonical(tree)
		if err != nil || string(canonical) != c.Canonical {
			t.Errorf("Canonical(%.60q) = %.80q, %v; json_body %.80q", c.Text, canonical, err, c.Canonical)
		}
		response, err := Dumps(tree, Options{})
		if err != nil || string(response) != c.Response {
			t.Errorf("Dumps(%.60q) = %.80q, %v; JSONResponse %.80q", c.Text, response, err, c.Response)
		}
	}
}

// TestRefusedDocumentsAreRefused: Decode refuses what json.loads refuses,
// and what json.loads reads but the tree cannot carry with the reason
// the generator names.
func TestRefusedDocumentsAreRefused(t *testing.T) {
	v := golden(t)
	reasons := map[string]error{
		"duplicate_key":  ErrDuplicateKey,
		"lone_surrogate": ErrLoneSurrogate,
		"non_finite":     ErrNonFinite,
		"too_deep":       ErrTooDeep,
	}
	counts := map[string]int{}
	for _, c := range v.Refused {
		tree, err := Decode([]byte(c.Text))
		switch c.Python {
		case "refuses":
			if err == nil {
				t.Errorf("Decode(%.60q) = %v; json.loads refuses it", c.Text, tree)
			}
			counts["python"]++
		case "reads":
			want, ok := reasons[c.GoRefuses]
			if !ok {
				t.Fatalf("an unknown reason %q", c.GoRefuses)
			}
			if !errors.Is(err, want) {
				t.Errorf("Decode(%.60q) = %v, %v; want %v", c.Text, tree, err, want)
			}
			counts[c.GoRefuses]++
		default:
			t.Fatalf("an unknown outcome %q", c.Python)
		}
	}
	if counts["python"] < 40 || len(counts) != len(reasons)+1 {
		t.Fatalf("the refusals cover %v", counts)
	}
}

func TestISOFormatIsDatetimeIsoformat(t *testing.T) {
	v := golden(t)
	if len(v.ISOFormat) < 150 {
		t.Fatalf("the vectors hold %d instants", len(v.ISOFormat))
	}
	for _, c := range v.ISOFormat {
		if got := ISOFormat(time.UnixMicro(c.UnixMicros)); got != c.ISOFormat {
			t.Errorf("ISOFormat(%d µs) = %q, Python %q", c.UnixMicros, got, c.ISOFormat)
		}
	}
}
