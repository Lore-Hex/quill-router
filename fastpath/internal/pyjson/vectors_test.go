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
	// The pydantic_* sections are the authorize route's 200 bodies, which
	// FastAPI writes with TypeAdapter.dump_json.
	PydanticFloats []struct {
		Bits string `json:"bits"`
		JSON string `json:"json"`
	} `json:"pydantic_floats"`
	PydanticMoney []struct {
		Microdollars int64  `json:"microdollars"`
		JSON         string `json:"json"`
	} `json:"pydantic_money"`
	PydanticStrings []struct {
		Codepoints []rune `json:"codepoints"`
		JSON       string `json:"json"`
	} `json:"pydantic_strings"`
	PydanticDocuments []struct {
		Text string  `json:"text"`
		JSON *string `json:"json"`
	} `json:"pydantic_documents"`
}

func float64FromBits(t *testing.T, bits string) float64 {
	t.Helper()
	raw, err := hex.DecodeString(bits)
	if err != nil || len(raw) != 8 {
		t.Fatalf("bits %q", bits)
	}
	return math.Float64frombits(binary.BigEndian.Uint64(raw))
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
		f := float64FromBits(t, c.Bits)
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

// TestFormatFloatPydanticIsPydanticCore holds FormatFloatPydantic, and
// DumpsPydantic of a float, to what the authorize route writes for every
// double in the vectors (the same doubles as TestFormatFloatIsFloatRepr's),
// a NaN and the infinities as null.
func TestFormatFloatPydanticIsPydanticCore(t *testing.T) {
	v := golden(t)
	if len(v.PydanticFloats) < 2000 {
		t.Fatalf("the vectors hold %d floats", len(v.PydanticFloats))
	}
	differs := 0
	for _, c := range v.PydanticFloats {
		f := float64FromBits(t, c.Bits)
		if got := FormatFloatPydantic(f); got != c.JSON {
			t.Errorf("FormatFloatPydantic(%s) = %q, pydantic-core %q", c.Bits, got, c.JSON)
		}
		if got, err := DumpsPydantic(f); err != nil || string(got) != c.JSON {
			t.Errorf("DumpsPydantic(%s) = %q, %v; pydantic-core %q", c.Bits, got, err, c.JSON)
		}
		if FormatFloat(f) != c.JSON {
			differs++
		}
	}
	// The two differ only between 1e-10 and 1e-04 (repr pads the exponent
	// to two digits and writes 1e-05 where pydantic-core writes 0.00001);
	// the vectors must hold enough of those to tell them apart.
	if differs < 40 {
		t.Fatalf("repr and pydantic-core differ on only %d of %d floats", differs, len(v.PydanticFloats))
	}
}

// TestPydanticMoneyIsMoneyPair: money_pair("estimated_cost", m) as the
// authorize body writes it, the float float64(m) / 1e6.
func TestPydanticMoneyIsMoneyPair(t *testing.T) {
	v := golden(t)
	if len(v.PydanticMoney) < 500 {
		t.Fatalf("the vectors hold %d amounts", len(v.PydanticMoney))
	}
	for _, c := range v.PydanticMoney {
		got, err := DumpsPydantic(Object{
			{"estimated_cost", float64(c.Microdollars) / 1_000_000},
			{"estimated_cost_microdollars", c.Microdollars},
		})
		if err != nil || string(got) != c.JSON {
			t.Errorf("%d microdollars: %q, %v; pydantic-core %q", c.Microdollars, got, err, c.JSON)
		}
	}
}

func TestPydanticStringsArePydanticCore(t *testing.T) {
	v := golden(t)
	if len(v.PydanticStrings) < 400 {
		t.Fatalf("the vectors hold %d strings", len(v.PydanticStrings))
	}
	for _, c := range v.PydanticStrings {
		got, err := DumpsPydantic(string(c.Codepoints))
		if err != nil || string(got) != c.JSON {
			t.Errorf("DumpsPydantic(%U) = %q, %v; pydantic-core %q", c.Codepoints, got, err, c.JSON)
		}
	}
}

// TestPydanticDocumentsAreTheAuthorizeBody decodes each document and writes
// {"data": it} as the authorize route does, or refuses it where the route
// cannot write it (nested past pydantic-core's bound).
func TestPydanticDocumentsAreTheAuthorizeBody(t *testing.T) {
	v := golden(t)
	if len(v.PydanticDocuments) < 300 {
		t.Fatalf("the vectors hold %d documents", len(v.PydanticDocuments))
	}
	refused := 0
	for _, c := range v.PydanticDocuments {
		tree, err := Decode([]byte(c.Text))
		if err != nil {
			t.Errorf("Decode(%.60q): %v", c.Text, err)
			continue
		}
		got, err := DumpsPydantic(Object{{"data", tree}})
		if c.JSON == nil {
			refused++
			if !errors.Is(err, ErrTooDeep) {
				t.Errorf("DumpsPydantic(%.60q) = %.80q, %v; the route answers 500", c.Text, got, err)
			}
			continue
		}
		if err != nil || string(got) != *c.JSON {
			t.Errorf("DumpsPydantic(%.60q) = %.80q, %v; the route %.80q", c.Text, got, err, *c.JSON)
		}
	}
	if refused < 10 || refused == len(v.PydanticDocuments) {
		t.Fatalf("%d of %d documents refused", refused, len(v.PydanticDocuments))
	}
}
