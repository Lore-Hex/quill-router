package routingsnapshot

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
)

// provider.max_price, as routing._max_token_prices and
// _price_limit_microdollars read it: an object whose members are prompt and
// completion, each null (no limit) or a non-negative decimal of USD per
// million tokens with at most six decimal places, held as integer
// microdollars per million tokens. A route whose headline prompt or
// completion price (the effective endpoint's
// prompt_price_microdollars_per_million_tokens and
// completion_price_microdollars_per_million_tokens at the instant, which the
// snapshot's epochs carry as Headline) is above its limit is dropped
// (_endpoint_matches_parameter_and_price_filters); equal is kept.
//
// Python reads each limit as Decimal(str(value)), where value is what
// json.loads made of the literal: an int for an integer literal, which str
// writes exactly, and a float for any other, which str writes as repr, the
// shortest digits that read back as the same double. Go reads a limit only
// when its literal is plainLimit: no sign, no exponent, at most nine digits
// before the point and at most six after it. Such a literal has at most 15
// significant digits, and every decimal of at most 15 significant digits
// (in the double's normal range, which these are, or zero) reads back from
// its double as itself (DBL_DIG): no other decimal of 15 digits or fewer
// rounds to that double, so repr's shortest digits are the literal's own
// value, trailing zeros aside. Decimal(str(value)) is then the literal's
// exact decimal value, and Go computes that from the digits alone, with no
// float. Six decimal places make the microdollars whole, so there is no
// rounding to reproduce: Python refuses a seventh place (a 400) rather than
// round it, and Go passes such a literal. The microdollars are below 10^15,
// inside int64.
//
// Anything else goes to Python with one of the reasons below, never a guess.
// Python answers some of these (a string such as "0.95", an exponent, an
// integer of ten digits or more, trailing zeros past the sixth place) and
// refuses the rest with a 400 (not an object, another member, a boolean, a
// negative number, a seventh decimal place).

// The evaluator's passes for a provider.max_price Go does not read. Each is
// a fixed sentence that names no value and no key from the body; the edge
// counts each under its own reason (edge.PassMaxPrice*).
const (
	MaxPriceNotObject = "provider.max_price is not an object"
	MaxPriceOption    = "provider.max_price has a member other than prompt and completion"
	MaxPriceType      = "provider.max_price has a limit that is not a number"
	MaxPriceNegative  = "provider.max_price has a negative limit"
	MaxPriceExponent  = "provider.max_price has a limit with an exponent"
	MaxPriceDigits    = "provider.max_price has a limit with more digits than Go reads"
)

// MaxPricePasses are the reasons above, for a test or a caller to hold a
// vocabulary to.
var MaxPricePasses = []string{MaxPriceNotObject, MaxPriceOption, MaxPriceType, MaxPriceNegative,
	MaxPriceExponent, MaxPriceDigits}

// plainLimit is the one spelling of a limit Go reads: the integer part
// (0, or up to nine digits with no leading zero, which JSON forbids anyway)
// and an optional fraction of one to six digits.
var plainLimit = regexp.MustCompile(`^(0|[1-9][0-9]{0,8})(?:\.([0-9]{1,6}))?$`)

// maxTokenPrices is routing._max_token_prices over provider.max_price as
// sent (nil or null when absent): the prompt and completion limits in
// microdollars per million tokens, nil for none, or the reason Go passes.
func maxTokenPrices(raw json.RawMessage) (prompt, completion *int64, reason string) {
	if isJSONNull(raw) {
		return nil, nil, ""
	}
	var members map[string]json.RawMessage
	if err := json.Unmarshal(raw, &members); err != nil || members == nil {
		return nil, nil, MaxPriceNotObject
	}
	for name := range members {
		if name != "prompt" && name != "completion" {
			return nil, nil, MaxPriceOption
		}
	}
	if prompt, reason = priceLimit(members["prompt"]); reason != "" {
		return nil, nil, reason
	}
	if completion, reason = priceLimit(members["completion"]); reason != "" {
		return nil, nil, reason
	}
	return prompt, completion, ""
}

// priceLimit is _price_limit_microdollars for one member's value as sent:
// nil for an absent or null member, the microdollars for a plainLimit
// literal, else the reason Go passes.
func priceLimit(raw json.RawMessage) (*int64, string) {
	if isJSONNull(raw) {
		return nil, ""
	}
	text := string(bytes.TrimSpace(raw))
	switch c := text[0]; {
	case c == '-':
		return nil, MaxPriceNegative
	case c < '0' || c > '9':
		// A string, a boolean, a list or an object.
		return nil, MaxPriceType
	case strings.ContainsAny(text, "eE"):
		return nil, MaxPriceExponent
	}
	micro, ok := PlainLimitMicrodollars(text)
	if !ok {
		return nil, MaxPriceDigits
	}
	return &micro, ""
}

// PlainLimitMicrodollars is a plainLimit literal's exact value times
// 1,000,000, from its digits alone; ok is false for any other text. It is
// exported for gwauth's tests, which hold the fingerprint's float spelling
// of the same literals to this value.
func PlainLimitMicrodollars(text string) (int64, bool) {
	m := plainLimit.FindStringSubmatch(text)
	if m == nil {
		return 0, false
	}
	whole, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return 0, false
	}
	fraction, err := strconv.ParseInt(m[2]+strings.Repeat("0", 6-len(m[2])), 10, 64)
	if err != nil {
		return 0, false
	}
	return whole*1_000_000 + fraction, true
}

// overMaxPrice reports a route whose headline prices at the instant are
// above either limit.
func (p prefs) overMaxPrice(h Headline) bool {
	return (p.maxPrompt != nil && h.Prompt > *p.maxPrompt) ||
		(p.maxCompletion != nil && h.Completion > *p.maxCompletion)
}
