package routingsnapshot

import (
	"encoding/json"
	"fmt"
	"math/big"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"
)

// pythonsLimit is what Python's _price_limit_microdollars makes of a JSON
// number literal, by its own route: json.loads reads the double nearest the
// literal (an integer literal is an int, exact), str() writes repr's
// shortest digits, Decimal reads those, times 1,000,000; ok is false when
// that is not whole (Python's "at most six decimal places" 400). strconv's
// ParseFloat is correctly rounded and its shortest digits are repr's, as
// pyjson.FormatFloat relies on. The test's oracle only: the evaluator never
// reads a float.
func pythonsLimit(t testing.TB, literal string) (*big.Int, bool) {
	t.Helper()
	text := literal // an int: str() writes it exactly
	if strings.ContainsAny(literal, ".eE") {
		f, err := strconv.ParseFloat(literal, 64)
		if err != nil {
			t.Fatalf("%s: %v", literal, err)
		}
		text = strconv.FormatFloat(f, 'e', -1, 64)
	}
	value, ok := new(big.Rat).SetString(text)
	if !ok {
		t.Fatalf("%s: %s is not a decimal", literal, text)
	}
	value.Mul(value, big.NewRat(1_000_000, 1))
	if !value.IsInt() {
		return nil, false
	}
	return value.Num(), true
}

// exactLimit is the literal's own decimal value times 1,000,000, whole or
// not.
func exactLimit(t testing.TB, literal string) *big.Rat {
	t.Helper()
	value, ok := new(big.Rat).SetString(literal)
	if !ok {
		t.Fatalf("%s is not a decimal", literal)
	}
	return value.Mul(value, big.NewRat(1_000_000, 1))
}

// TestALimitIsItsLiteralExactly: every spelling of a limit, read or passed.
// A read limit is its literal's exact microdollars and Python's; a half
// microdollar, or anything past the sixth place, is never rounded but
// passed (Python refuses a seventh place; trailing zeros past the sixth it
// reads, and Go passes them all the same).
func TestALimitIsItsLiteralExactly(t *testing.T) {
	read := map[string]int64{
		"0": 0, "0.0": 0, "0.000000": 0, "1": 1_000_000, "1.0": 1_000_000, "1.50": 1_500_000,
		"0.95": 950_000, "0.950000": 950_000, "0.949999": 949_999, "3.30": 3_300_000, "0.000001": 1,
		"0.00005": 50, "0.0001": 100, "12.66": 12_660_000, "999999999": 999_999_999_000_000,
		"999999999.999999": 999_999_999_999_999, "123456789.123456": 123_456_789_123_456,
	}
	for literal, want := range read {
		got, reason := priceLimit(json.RawMessage(literal))
		if reason != "" || got == nil || *got != want {
			t.Errorf("%s: %v %q, want %d", literal, got, reason, want)
			continue
		}
		python, ok := pythonsLimit(t, literal)
		if !ok || !python.IsInt64() || python.Int64() != want {
			t.Errorf("%s: Go reads %d, Python %v (%v)", literal, want, python, ok)
		}
	}
	passed := map[string]string{
		// A half microdollar and the places past the sixth: passed, never
		// rounded, whether Python refuses them (0.0000005, 1.0000005) or reads
		// them (0.95000000).
		"0.0000005": MaxPriceDigits, "1.0000005": MaxPriceDigits, "0.9999995": MaxPriceDigits,
		"0.0000015": MaxPriceDigits, "0.9500001": MaxPriceDigits, "0.95000000": MaxPriceDigits,
		"1000000000": MaxPriceDigits, "1234567890.5": MaxPriceDigits,
		"1e0": MaxPriceExponent, "1E0": MaxPriceExponent, "1E-6": MaxPriceExponent, "5e-07": MaxPriceExponent,
		"1.5e+3": MaxPriceExponent, "0e0": MaxPriceExponent,
		"-1": MaxPriceNegative, "-0": MaxPriceNegative, "-0.0": MaxPriceNegative, "-0.5": MaxPriceNegative,
		`"0.95"`: MaxPriceType, `"1"`: MaxPriceType, "true": MaxPriceType, "false": MaxPriceType,
		"[1]": MaxPriceType, `{"usd":1}`: MaxPriceType,
	}
	for literal, want := range passed {
		if got, reason := priceLimit(json.RawMessage(literal)); got != nil || reason != want {
			t.Errorf("%s: %v %q, want %q", literal, got, reason, want)
		}
	}
	for _, absent := range []json.RawMessage{nil, json.RawMessage("null"), json.RawMessage(" null ")} {
		if got, reason := priceLimit(absent); got != nil || reason != "" {
			t.Errorf("%q: %v %q, want no limit", absent, got, reason)
		}
	}
}

// TestMaxTokenPricesShapes: _max_token_prices over each shape of
// provider.max_price: no limit, one, both, or the reason Go passes.
func TestMaxTokenPricesShapes(t *testing.T) {
	limit := func(v int64) *int64 { return &v }
	for _, c := range []struct {
		raw                string
		prompt, completion *int64
		reason             string
	}{
		{"", nil, nil, ""},
		{"null", nil, nil, ""},
		{"{}", nil, nil, ""},
		{`{"prompt":null,"completion":null}`, nil, nil, ""},
		{`{"prompt":0.5}`, limit(500_000), nil, ""},
		{`{"completion":2}`, nil, limit(2_000_000), ""},
		{`{"prompt":1,"completion":3.2}`, limit(1_000_000), limit(3_200_000), ""},
		{`{"completion":3.20,"prompt":null}`, nil, limit(3_200_000), ""},
		// json.loads unescapes a key; Python reads it as prompt.
		{`{"prompt":0.5}`, limit(500_000), nil, ""},
		// A key twice: the last value, as Python's dict keeps.
		{`{"prompt":9,"prompt":0.5}`, limit(500_000), nil, ""},
		{"1", nil, nil, MaxPriceNotObject},
		{"0.5", nil, nil, MaxPriceNotObject},
		{"[1]", nil, nil, MaxPriceNotObject},
		{`"1"`, nil, nil, MaxPriceNotObject},
		{"true", nil, nil, MaxPriceNotObject},
		{`{"prompt":1,"image":1}`, nil, nil, MaxPriceOption},
		{`{"image":null}`, nil, nil, MaxPriceOption},
		{`{"Prompt":1}`, nil, nil, MaxPriceOption},
		{`{"prompt ":1}`, nil, nil, MaxPriceOption},
		{`{"request":0}`, nil, nil, MaxPriceOption},
		{`{"prompt":-1}`, nil, nil, MaxPriceNegative},
		{`{"prompt":0.5,"completion":"1"}`, nil, nil, MaxPriceType},
		{`{"prompt":1e-6}`, nil, nil, MaxPriceExponent},
		{`{"completion":0.1234567}`, nil, nil, MaxPriceDigits},
	} {
		prompt, completion, reason := maxTokenPrices(json.RawMessage(c.raw))
		if reason != c.reason || !equalLimit(prompt, c.prompt) || !equalLimit(completion, c.completion) {
			t.Errorf("%s: %v %v %q, want %v %v %q", c.raw, prompt, completion, reason, c.prompt, c.completion, c.reason)
		}
	}
	for _, reason := range MaxPricePasses {
		if !strings.HasPrefix(reason, "provider.max_price ") || strings.ContainsAny(reason, `"%`) {
			t.Errorf("%q: not a fixed sentence about provider.max_price", reason)
		}
	}
}

func equalLimit(a, b *int64) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}

// TestTheDigitBoundIsWhereFloatsStopBeingExact is the positive control for
// plainLimit's bounds: just past them the literal's exact value and
// Python's (through the double) differ, so a reader that took more digits
// would decide on a value Python does not hold. A sixteen-digit literal
// with six places, a seventeen-digit fraction, and a literal whose excess
// digits round away; while within the bounds the two agree.
func TestTheDigitBoundIsWhereFloatsStopBeingExact(t *testing.T) {
	for _, literal := range []string{"9999999999.999999", "0.1000000000000000055511151231257827",
		"9007199254740993.0", "0.30000000000000004"} {
		python, whole := pythonsLimit(t, literal)
		exact := exactLimit(t, literal)
		if whole && exact.IsInt() && python.Cmp(exact.Num()) == 0 {
			t.Errorf("%s: Python's limit is its exact value, so this is no control", literal)
		}
		if _, reason := priceLimit(json.RawMessage(literal)); reason != MaxPriceDigits {
			t.Errorf("%s: %q, want %q", literal, reason, MaxPriceDigits)
		}
	}
	for _, literal := range []string{"999999999.999999", "0.000001", "123456789.987654", "0.1"} {
		python, whole := pythonsLimit(t, literal)
		if exact := exactLimit(t, literal); !whole || !exact.IsInt() || python.Cmp(exact.Num()) != 0 {
			t.Errorf("%s: Python %v, exact %v", literal, python, exact)
		}
	}
}

// FuzzALimitGoReadsIsPythons: whatever the text, a limit Go reads is the
// literal's exact value and Python's through the double; a text Go does not
// read, it passes with a reason of the vocabulary.
func FuzzALimitGoReadsIsPythons(f *testing.F) {
	for _, seed := range []string{"0", "0.95", "0.949999", "999999999.999999", "0.0000005", "1e0", "-0", `"1"`,
		"1.0000000000000002", "9999999999.999999", "true", "[1]", "0.000001"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, text string) {
		if !json.Valid([]byte(text)) {
			return
		}
		got, reason := priceLimit(json.RawMessage(text))
		if reason != "" {
			if got != nil || !slices.Contains(MaxPricePasses, reason) {
				t.Fatalf("%q: %v %q", text, got, reason)
			}
			return
		}
		if got == nil {
			if strings.TrimSpace(text) != "null" {
				t.Fatalf("%q: no limit and no reason", text)
			}
			return
		}
		literal := strings.TrimSpace(text)
		exact := exactLimit(t, literal)
		python, whole := pythonsLimit(t, literal)
		if !exact.IsInt() || !exact.Num().IsInt64() || exact.Num().Int64() != *got || !whole ||
			python.Cmp(exact.Num()) != 0 {
			t.Fatalf("%q: Go %d, exact %v, Python %v (%v)", text, *got, exact, python, whole)
		}
	})
}

// TestTheLimitIsTheHeadlinePriceAtTheInstantEqualKept: for each of GLM 5.3's
// routes in the golden snapshot, a prompt (then completion) limit at its
// headline price keeps it, and one a microdollar below drops it, every other
// route kept or dropped by its own price; the price is the epoch's at the
// instant (DeepSeek V4 Flash's halves off peak).
func TestTheLimitIsTheHeadlinePriceAtTheInstantEqualKept(t *testing.T) {
	s := goldenSnapshot(t)
	decimal := func(micro int64) string { return fmt.Sprintf("%d.%06d", micro/1_000_000, micro%1_000_000) }
	evaluate := func(model, member string, micro int64, at Request) Result {
		r := at
		r.Model = model
		r.Provider = json.RawMessage(`{"max_price":{"` + member + `":` + decimal(micro) + `}}`)
		return s.Evaluate(r)
	}
	glm := baseRequest()
	all := s.Evaluate(Request{Model: "z-ai/glm-5.3", RouteType: glm.RouteType,
		EstimatedInputTokens: glm.EstimatedInputTokens, At: glm.At})
	if all.Outcome != Answered || len(all.Candidates) < 5 {
		t.Fatalf("GLM 5.3 unfiltered: %+v", all)
	}
	for _, member := range []string{"prompt", "completion"} {
		for _, id := range all.Candidates {
			h := s.Endpoints[s.endpointIndex[id]].epochAt(glm.At.Unix()).Headline
			price := h.Prompt
			if member == "completion" {
				price = h.Completion
			}
			for _, limit := range []int64{price, price - 1} {
				got := evaluate("z-ai/glm-5.3", member, limit, glm)
				var want []string
				for _, other := range all.Candidates {
					oh := s.Endpoints[s.endpointIndex[other]].epochAt(glm.At.Unix()).Headline
					if p := map[string]int64{"prompt": oh.Prompt, "completion": oh.Completion}[member]; p <= limit {
						want = append(want, other)
					}
				}
				switch {
				case len(want) == 0 && (got.Outcome != Refused || got.Refusal.Message != noRouteMessage):
					t.Errorf("%s %s<=%d: %+v, want the no-route refusal", id, member, limit, got)
				case len(want) > 0 && (got.Outcome != Answered || !slices.Equal(got.Candidates, want)):
					t.Errorf("%s %s<=%d: %+v, want %v", id, member, limit, got.Candidates, want)
				}
				if kept := slices.Contains(got.Candidates, id); kept != (limit == price) {
					t.Errorf("%s %s<=%d: kept %v", id, member, limit, kept)
				}
			}
		}
	}
	// The price at the instant: 0.2 is under V4 Flash's off-peak prompt
	// price (0.15825) and over its peak one (0.3165).
	offPeak, atPeak := baseRequest(), baseRequest()
	offPeak.At = glm.At.Add(-90 * time.Minute)
	if got := evaluate("deepseek/deepseek-v4-flash", "prompt", 200_000, offPeak); got.Outcome != Answered {
		t.Errorf("off peak: %+v", got)
	}
	if got := evaluate("deepseek/deepseek-v4-flash", "prompt", 200_000, atPeak); got.Outcome != Refused {
		t.Errorf("at peak: %+v", got)
	}
}
