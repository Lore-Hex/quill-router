// Package pyjson writes JSON byte for byte as the control plane's Python
// does, so a row or a response the edge composes is the one Python would
// have written (docs/design/fast-admission-enclave-direct.md, E2).
//
//   - FormatFloat is float.__repr__, the digits json.dumps writes for a
//     float (money.money_pair's float twin of a microdollar amount).
//   - FormatFloatPydantic and DumpsPydantic are TypeAdapter.dump_json
//     (pydantic-core's serializer under pydantic's default config), which
//     FastAPI writes a route's return value with when the route has a
//     response model and the default response class: the authorize 200
//     body. Error bodies are Starlette's JSONResponse, Dumps's.
//   - Dumps is json.dumps with compact separators, ensure_ascii either way
//     and sort_keys either way: Canonical is storage_codec.json_body's and
//     the authorize fingerprint's form (sorted, ASCII), and the insertion
//     order with ensure_ascii off is Starlette's JSONResponse body. Objects
//     are Object, an ordered list of members, so the order Python's dict
//     would hold is the order written.
//   - Decode is json.loads into that tree, order kept, integers exact, so a
//     request body can be re-encoded canonically. What Python reads but
//     the tree cannot carry exactly (a duplicate key, a lone surrogate, a
//     non-finite number, deep nesting) is refused with its reason, for the
//     caller to pass to Python rather than guess.
//   - ISOFormat is datetime.isoformat() with "Z".
//
// The edge (G6) composes an authorize 200 body with DumpsPydantic and
// nothing else. Any error from it means the edge cannot write that body as
// Python would: it passes the request to Python, never answers 500 itself,
// and never writes a null of its own in place of a value.
//
// fastpath/testdata/pyjson_vectors_v1.json holds the outputs of CPython
// itself, which scripts/fastpath/pyjson_vectors.py writes and
// tests/test_fastpath_pyjson_vectors.py keeps current; the tests hold
// every function here to every vector.
package pyjson

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"slices"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Member is one key and value of an Object.
type Member struct {
	Key   string
	Value any
}

// Object is a JSON object as a Python dict holds it: its members in
// insertion order, no key twice.
type Object []Member

// Options are json.dumps's: sort_keys, and ensure_ascii, which escapes
// every character from U+007F up. The separators are always compact,
// (",", ":"), as both json_body and JSONResponse write.
type Options struct {
	SortKeys    bool
	EnsureASCII bool

	// pydantic is DumpsPydantic's: floats by FormatFloatPydantic, a NaN or
	// an infinity as null.
	pydantic bool
}

// maxIntDigits is CPython's int_max_str_digits default: json.loads refuses
// an integer of more digits, and str() refuses to write one.
const maxIntDigits = 4300

// Dumps writes v as json.dumps(v, separators=(",", ":")) with o's
// sort_keys and ensure_ascii. v is a tree of nil, bool, string (valid
// UTF-8), json.Number (a JSON number literal: an integer is written
// exactly, "-0" as 0, and any other is read as a double and written as
// its repr), int, int64, *big.Int, float64, []any, []string and Object.
// A NaN or an infinity is refused, as JSONResponse refuses it
// (allow_nan=False); json_body would write a token that is not JSON.
func Dumps(v any, o Options) ([]byte, error) {
	return appendValue(nil, v, o)
}

// DumpsPydantic writes v as TypeAdapter.dump_json writes the Python value
// Dumps would write v from, which is FastAPI's body for a route
// with a response model (TypeAdapter(dict[str, Any]).dump_json for the
// authorize route): compact, members in insertion order, non-ASCII as
// itself, strings escaped as json.dumps escapes them with ensure_ascii
// off, integers exactly, floats by FormatFloatPydantic, and a NaN or an
// infinity, which Dumps refuses, as null. A value more than
// pydanticMaxLevels levels down is refused (ErrTooDeep), as dump_json
// refuses it and FastAPI answers 500. A caller passes any error to Python.
func DumpsPydantic(v any) ([]byte, error) {
	if levels(v) > pydanticMaxLevels {
		return nil, fmt.Errorf("%w: pydantic-core writes %d levels at most", ErrTooDeep, pydanticMaxLevels)
	}
	return appendValue(nil, v, Options{pydantic: true})
}

// pydanticMaxLevels is how deep pydantic-core's recursion guard lets
// dump_json go: a value, scalar or container, at most this many levels
// down, v the first. One deeper raises "Circular reference detected
// (depth exceeded)".
const pydanticMaxLevels = 256

// levels is how many levels v spans: 1 for a scalar or an empty
// container, else 1 more than its deepest member.
func levels(v any) int {
	deepest := 0
	switch v := v.(type) {
	case []any:
		for _, item := range v {
			deepest = max(deepest, levels(item))
		}
	case []string:
		if len(v) > 0 {
			deepest = 1
		}
	case Object:
		for _, m := range v {
			deepest = max(deepest, levels(m.Value))
		}
	}
	return 1 + deepest
}

// Canonical is Dumps sorted and ASCII: json_body(value), and the
// authorize fingerprint's json.dumps(sort_keys=True, separators=(",", ":")).
func Canonical(v any) ([]byte, error) {
	return Dumps(v, Options{SortKeys: true, EnsureASCII: true})
}

func appendValue(dst []byte, v any, o Options) ([]byte, error) {
	switch v := v.(type) {
	case nil:
		return append(dst, "null"...), nil
	case bool:
		return strconv.AppendBool(dst, v), nil
	case string:
		return appendString(dst, v, o.EnsureASCII)
	case json.Number:
		return appendNumber(dst, string(v), o)
	case int:
		return strconv.AppendInt(dst, int64(v), 10), nil
	case int64:
		return strconv.AppendInt(dst, v, 10), nil
	case *big.Int:
		if v == nil {
			return nil, errors.New("pyjson: a nil *big.Int")
		}
		return v.Append(dst, 10), nil
	case float64:
		if o.pydantic {
			return append(dst, FormatFloatPydantic(v)...), nil
		}
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, fmt.Errorf("pyjson: %v is not JSON compliant", v)
		}
		return append(dst, FormatFloat(v)...), nil
	case []any:
		dst = append(dst, '[')
		for i, item := range v {
			if i > 0 {
				dst = append(dst, ',')
			}
			var err error
			if dst, err = appendValue(dst, item, o); err != nil {
				return nil, err
			}
		}
		return append(dst, ']'), nil
	case []string:
		dst = append(dst, '[')
		for i, item := range v {
			if i > 0 {
				dst = append(dst, ',')
			}
			var err error
			if dst, err = appendString(dst, item, o.EnsureASCII); err != nil {
				return nil, err
			}
		}
		return append(dst, ']'), nil
	case Object:
		return appendObject(dst, v, o)
	default:
		return nil, fmt.Errorf("pyjson: cannot write a %T", v)
	}
}

func appendObject(dst []byte, members Object, o Options) ([]byte, error) {
	sorted := slices.Clone(members)
	slices.SortStableFunc(sorted, func(a, b Member) int { return strings.Compare(a.Key, b.Key) })
	for i := 1; i < len(sorted); i++ {
		if sorted[i].Key == sorted[i-1].Key {
			return nil, fmt.Errorf("pyjson: the key %q twice in one object", sorted[i].Key)
		}
	}
	if o.SortKeys {
		// Python sorts str keys by code point, and UTF-8's byte order is
		// code point order, so strings.Compare is Python's order.
		members = sorted
	}
	dst = append(dst, '{')
	for i, m := range members {
		if i > 0 {
			dst = append(dst, ',')
		}
		var err error
		if dst, err = appendString(dst, m.Key, o.EnsureASCII); err != nil {
			return nil, err
		}
		dst = append(dst, ':')
		if dst, err = appendValue(dst, m.Value, o); err != nil {
			return nil, err
		}
	}
	return append(dst, '}'), nil
}

// appendNumber writes a JSON number literal as Python writes what
// json.loads reads from it: an integer as int, exactly; any other as a
// float, by repr (or as pydantic-core writes that float).
func appendNumber(dst []byte, literal string, o Options) ([]byte, error) {
	integer, ok := numberLiteral(literal)
	if !ok {
		return nil, fmt.Errorf("pyjson: %q is not a JSON number", literal)
	}
	if integer {
		if digits := strings.TrimPrefix(literal, "-"); len(digits) > maxIntDigits {
			return nil, fmt.Errorf("pyjson: an integer of %d digits", len(digits))
		}
		n, _ := new(big.Int).SetString(literal, 10)
		return n.Append(dst, 10), nil
	}
	f, err := strconv.ParseFloat(literal, 64)
	if err != nil && !errors.Is(err, strconv.ErrRange) {
		return nil, err
	}
	if o.pydantic {
		// json.loads reads a literal too large for a double as an
		// infinity, which dump_json writes as null.
		return append(dst, FormatFloatPydantic(f)...), nil
	}
	if math.IsInf(f, 0) {
		return nil, fmt.Errorf("pyjson: %s is not JSON compliant once read as a float", literal)
	}
	return append(dst, FormatFloat(f)...), nil
}

// numberLiteral reports whether s is exactly one JSON number, and whether
// it is an integer (no fraction and no exponent), which json.loads reads
// as int rather than float.
func numberLiteral(s string) (integer, ok bool) {
	n := scanNumber([]byte(s), 0)
	if n != len(s) {
		return false, false
	}
	return !strings.ContainsAny(s, ".eE"), true
}

// scanNumber returns the end of the JSON number starting at data[start],
// or start when there is none. It stops where CPython's scanner stops: a
// "." or an exponent without digits after it is not part of the number.
func scanNumber(data []byte, start int) int {
	i := start
	if i < len(data) && data[i] == '-' {
		i++
	}
	switch {
	case i < len(data) && data[i] == '0':
		i++
	case i < len(data) && data[i] >= '1' && data[i] <= '9':
		i = digitsEnd(data, i)
	default:
		return start
	}
	if i+1 < len(data) && data[i] == '.' && isDigit(data[i+1]) {
		i = digitsEnd(data, i+1)
	}
	if i < len(data) && (data[i] == 'e' || data[i] == 'E') {
		j := i + 1
		if j < len(data) && (data[j] == '+' || data[j] == '-') {
			j++
		}
		if j < len(data) && isDigit(data[j]) {
			i = digitsEnd(data, j)
		}
	}
	return i
}

func isDigit(c byte) bool { return c >= '0' && c <= '9' }

func digitsEnd(data []byte, i int) int {
	for i < len(data) && isDigit(data[i]) {
		i++
	}
	return i
}

const hexDigits = "0123456789abcdef"

// appendString writes s as json.dumps writes a str: the two-character
// escapes for '"', '\\', '\b', '\f', '\n', '\r' and '\t', \u00XX for any
// other character below U+0020, and with ensureASCII \uXXXX (lowercase
// hex, a surrogate pair above U+FFFF) for every character from U+007F;
// everything else as itself, in UTF-8. '/' is not escaped.
func appendString(dst []byte, s string, ensureASCII bool) ([]byte, error) {
	if !utf8.ValidString(s) {
		return nil, fmt.Errorf("pyjson: %q is not UTF-8", s)
	}
	dst = append(dst, '"')
	for _, r := range s {
		switch r {
		case '"':
			dst = append(dst, `\"`...)
		case '\\':
			dst = append(dst, `\\`...)
		case '\b':
			dst = append(dst, `\b`...)
		case '\f':
			dst = append(dst, `\f`...)
		case '\n':
			dst = append(dst, `\n`...)
		case '\r':
			dst = append(dst, `\r`...)
		case '\t':
			dst = append(dst, `\t`...)
		default:
			switch {
			case r < 0x20:
				dst = appendEscape(dst, r)
			case ensureASCII && r > 0xffff:
				high, low := utf16.EncodeRune(r)
				dst = appendEscape(appendEscape(dst, high), low)
			case ensureASCII && r >= 0x7f:
				dst = appendEscape(dst, r)
			default:
				dst = utf8.AppendRune(dst, r)
			}
		}
	}
	return append(dst, '"'), nil
}

func appendEscape(dst []byte, r rune) []byte {
	return append(dst, '\\', 'u', hexDigits[r>>12&0xf], hexDigits[r>>8&0xf], hexDigits[r>>4&0xf], hexDigits[r&0xf])
}
