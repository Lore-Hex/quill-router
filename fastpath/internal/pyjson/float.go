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
	// Go's shortest digits are Python's: both the fewest that round-trip,
	// the closest of those to the value. Go writes them [-]d[.ddd]e±dd,
	// which is also Python's exponent form, two exponent digits at least.
	scientific := strconv.FormatFloat(f, 'e', -1, 64)
	mantissa, exponent, _ := strings.Cut(scientific, "e")
	exp, _ := strconv.Atoi(exponent)
	// decpt is where the decimal point falls relative to the first digit,
	// as CPython's format_float_short counts it: value = 0.ddd * 10^decpt.
	decpt := exp + 1
	if decpt <= -4 || decpt > 16 {
		return scientific
	}
	sign := ""
	if mantissa[0] == '-' {
		sign, mantissa = "-", mantissa[1:]
	}
	digits := strings.Replace(mantissa, ".", "", 1)
	switch {
	case decpt <= 0:
		return sign + "0." + strings.Repeat("0", -decpt) + digits
	case decpt < len(digits):
		return sign + digits[:decpt] + "." + digits[decpt:]
	default:
		return sign + digits + strings.Repeat("0", decpt-len(digits)) + ".0"
	}
}
