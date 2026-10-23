package pyjson

import (
	"math"
	"strconv"
	"strings"
)

// FormatFloat is Python's float.__repr__, which json.dumps writes for a
// float: the shortest digits that read back as the same double, written
// with an exponent when the decimal point falls more than four places
// before the first digit or more than sixteen after it (1e-05, 0.0001,
// 9999999999999998.0, 1e+16), and with ".0" on a value written without an
// exponent or a fraction. A NaN or an infinity is "nan", "inf" or "-inf",
// as repr writes them; the encoders refuse them.
func FormatFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "nan"
	case math.IsInf(f, 1):
		return "inf"
	case math.IsInf(f, -1):
		return "-inf"
	}
	scientific, sign, digits, decpt := shortest(f)
	if decpt <= -4 || decpt > 16 {
		// Go's exponent form is repr's: two exponent digits at least.
		return scientific
	}
	return positional(sign, digits, decpt)
}

// FormatFloatPydantic is the float pydantic-core's to_json writes, which
// is what FastAPI's response path writes for a route with a response
// model (the authorize 200 body, typed dict[str, Any]): repr's shortest
// digits, but positional down to five places before the first digit
// (0.00001, 0.000012345, where repr writes 1e-05), and in exponent form
// an exponent of as few digits as it takes, signed either way (1e-6,
// 9.999999999999999e-6, 1e+16, 1.7976931348623157e+308). Above, the
// threshold is repr's: 9999999999999998.0, then 1e+16. A NaN or an
// infinity is "null", as to_json writes it (ser_json_inf_nan="null").
func FormatFloatPydantic(f float64) string {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "null"
	}
	scientific, sign, digits, decpt := shortest(f)
	if decpt <= -5 || decpt > 16 {
		mantissa, _, _ := strings.Cut(scientific, "e")
		exp := decpt - 1
		if exp < 0 {
			return mantissa + "e-" + strconv.Itoa(-exp)
		}
		return mantissa + "e+" + strconv.Itoa(exp)
	}
	return positional(sign, digits, decpt)
}

// shortest is f's shortest round-tripping digits. Go's shortest digits
// are Python's (and pydantic-core's): both the fewest that read back as
// f, the closest of those to it. scientific is Go's [-]d[.ddd]e±dd form;
// digits are the significant digits alone, and decpt is where the decimal
// point falls relative to the first of them, as CPython's
// format_float_short counts it: |f| = 0.digits * 10^decpt.
func shortest(f float64) (scientific, sign, digits string, decpt int) {
	scientific = strconv.FormatFloat(f, 'e', -1, 64)
	mantissa, exponent, _ := strings.Cut(scientific, "e")
	exp, _ := strconv.Atoi(exponent)
	if mantissa[0] == '-' {
		sign, mantissa = "-", mantissa[1:]
	}
	return scientific, sign, strings.Replace(mantissa, ".", "", 1), exp + 1
}

// positional writes the digits without an exponent, with ".0" when there
// would be no fraction.
func positional(sign, digits string, decpt int) string {
	switch {
	case decpt <= 0:
		return sign + "0." + strings.Repeat("0", -decpt) + digits
	case decpt < len(digits):
		return sign + digits[:decpt] + "." + digits[decpt:]
	default:
		return sign + digits + strings.Repeat("0", decpt-len(digits)) + ".0"
	}
}
