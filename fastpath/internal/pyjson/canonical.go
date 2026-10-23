package pyjson

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"unicode/utf16"
	"unicode/utf8"
)

// What json.loads reads but the tree does not carry as Python holds it.
// Decode refuses each, so a caller can pass the document to Python with
// the reason rather than answer for it.
var (
	// ErrDuplicateKey: Python keeps the last value at the first key's place.
	ErrDuplicateKey = errors.New("pyjson: a key twice in one object")
	// ErrLoneSurrogate: Python's str holds a lone surrogate; UTF-8 cannot.
	ErrLoneSurrogate = errors.New("pyjson: a lone surrogate escape")
	// ErrNonFinite: NaN, Infinity, -Infinity, or a number too large for a
	// double, which Python reads as an infinity.
	ErrNonFinite = errors.New("pyjson: a non-finite number")
	// ErrTooDeep: more than maxDepth nested arrays and objects.
	ErrTooDeep = errors.New("pyjson: nested too deep")
)

// maxDepth bounds nesting well inside what CPython's scanner reads, and
// far past any request the control plane answers.
const maxDepth = 256

// Decode reads one JSON document as json.loads(str) does, into the tree
// Dumps writes: Object for an object, members in document order; []any;
// string; json.Number for a number, its literal as written (an integer
// literal is a Python int, exact at any size up to CPython's 4300 digits;
// any other a float); bool; nil. Canonical(Decode(b)) is
// json.dumps(json.loads(b), sort_keys=True, separators=(",", ":")).
//
// It refuses whatever json.loads refuses, and also a duplicate key, a lone
// surrogate, a non-finite number and deep nesting, which Python reads (the
// Err values above). The bytes must be UTF-8 without a byte order mark:
// json.loads of bytes would strip one, or read UTF-16 or UTF-32, and
// Decode refuses those rather than follow it.
func Decode(data []byte) (any, error) {
	if !utf8.Valid(data) {
		return nil, errors.New("pyjson: not UTF-8")
	}
	d := decoder{data: data}
	d.space()
	v, err := d.value(0)
	if err != nil {
		return nil, err
	}
	d.space()
	if d.pos != len(d.data) {
		return nil, d.syntax("extra data")
	}
	return v, nil
}

type decoder struct {
	data []byte
	pos  int
}

func (d *decoder) syntax(what string) error {
	return fmt.Errorf("pyjson: %s at byte %d", what, d.pos)
}

// space skips JSON's whitespace, the only whitespace CPython's scanner
// skips.
func (d *decoder) space() {
	for d.pos < len(d.data) {
		switch d.data[d.pos] {
		case ' ', '\t', '\n', '\r':
			d.pos++
		default:
			return
		}
	}
}

// literal consumes word if the document has it at the cursor.
func (d *decoder) literal(word string) bool {
	if len(d.data)-d.pos >= len(word) && string(d.data[d.pos:d.pos+len(word)]) == word {
		d.pos += len(word)
		return true
	}
	return false
}

func (d *decoder) value(depth int) (any, error) {
	if d.pos >= len(d.data) {
		return nil, d.syntax("expecting a value")
	}
	switch c := d.data[d.pos]; {
	case c == '{':
		return d.object(depth + 1)
	case c == '[':
		return d.array(depth + 1)
	case c == '"':
		return d.string()
	case d.literal("true"):
		return true, nil
	case d.literal("false"):
		return false, nil
	case d.literal("null"):
		return nil, nil
	case d.literal("NaN"), d.literal("Infinity"), d.literal("-Infinity"):
		return nil, ErrNonFinite
	case c == '-' || isDigit(c):
		return d.number()
	default:
		return nil, d.syntax("expecting a value")
	}
}

func (d *decoder) object(depth int) (any, error) {
	if depth > maxDepth {
		return nil, ErrTooDeep
	}
	d.pos++ // '{'
	d.space()
	members := Object{}
	if d.pos < len(d.data) && d.data[d.pos] == '}' {
		d.pos++
		return members, nil
	}
	seen := map[string]bool{}
	for {
		if d.pos >= len(d.data) || d.data[d.pos] != '"' {
			return nil, d.syntax("expecting a property name in double quotes")
		}
		key, err := d.string()
		if err != nil {
			return nil, err
		}
		d.space()
		if d.pos >= len(d.data) || d.data[d.pos] != ':' {
			return nil, d.syntax("expecting ':'")
		}
		d.pos++
		d.space()
		value, err := d.value(depth)
		if err != nil {
			return nil, err
		}
		if seen[key] {
			return nil, ErrDuplicateKey
		}
		seen[key] = true
		members = append(members, Member{Key: key, Value: value})
		d.space()
		if d.pos < len(d.data) && d.data[d.pos] == '}' {
			d.pos++
			return members, nil
		}
		if d.pos >= len(d.data) || d.data[d.pos] != ',' {
			return nil, d.syntax("expecting ',' or '}'")
		}
		d.pos++
		d.space()
	}
}

func (d *decoder) array(depth int) (any, error) {
	if depth > maxDepth {
		return nil, ErrTooDeep
	}
	d.pos++ // '['
	d.space()
	items := []any{}
	if d.pos < len(d.data) && d.data[d.pos] == ']' {
		d.pos++
		return items, nil
	}
	for {
		item, err := d.value(depth)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
		d.space()
		if d.pos < len(d.data) && d.data[d.pos] == ']' {
			d.pos++
			return items, nil
		}
		if d.pos >= len(d.data) || d.data[d.pos] != ',' {
			return nil, d.syntax("expecting ',' or ']'")
		}
		d.pos++
		d.space()
	}
}

// string reads a string at the cursor (its opening quote). The document
// is UTF-8 already, so bytes other than escapes are copied as they are; a
// raw control character is refused, as json.loads's strict mode refuses
// it.
func (d *decoder) string() (string, error) {
	d.pos++ // '"'
	var out []byte
	for {
		if d.pos >= len(d.data) {
			return "", d.syntax("unterminated string")
		}
		c := d.data[d.pos]
		switch {
		case c == '"':
			d.pos++
			return string(out), nil
		case c < 0x20:
			return "", d.syntax("invalid control character")
		case c != '\\':
			out = append(out, c)
			d.pos++
			continue
		}
		d.pos++ // '\\'
		if d.pos >= len(d.data) {
			return "", d.syntax("unterminated string")
		}
		escape := d.data[d.pos]
		d.pos++
		switch escape {
		case '"', '\\', '/':
			out = append(out, escape)
		case 'b':
			out = append(out, '\b')
		case 'f':
			out = append(out, '\f')
		case 'n':
			out = append(out, '\n')
		case 'r':
			out = append(out, '\r')
		case 't':
			out = append(out, '\t')
		case 'u':
			r, err := d.hex4()
			if err != nil {
				return "", err
			}
			if utf16.IsSurrogate(r) {
				if r >= 0xdc00 || !d.literal(`\u`) {
					return "", ErrLoneSurrogate
				}
				low, err := d.hex4()
				if err != nil {
					return "", err
				}
				if low < 0xdc00 || low > 0xdfff {
					return "", ErrLoneSurrogate
				}
				r = utf16.DecodeRune(r, low)
			}
			out = utf8.AppendRune(out, r)
		default:
			return "", d.syntax("invalid escape")
		}
	}
}

func (d *decoder) hex4() (rune, error) {
	if len(d.data)-d.pos < 4 {
		return 0, d.syntax(`invalid \uXXXX escape`)
	}
	var r rune
	for _, c := range d.data[d.pos : d.pos+4] {
		var digit byte
		switch {
		case isDigit(c):
			digit = c - '0'
		case c >= 'a' && c <= 'f':
			digit = c - 'a' + 10
		case c >= 'A' && c <= 'F':
			digit = c - 'A' + 10
		default:
			return 0, d.syntax(`invalid \uXXXX escape`)
		}
		r = r<<4 | rune(digit)
	}
	d.pos += 4
	return r, nil
}

// number reads a number at the cursor and keeps its literal. An integer of
// more than CPython's 4300 digits is refused, as json.loads refuses it; a
// float that rounds to an infinity is ErrNonFinite.
func (d *decoder) number() (any, error) {
	end := scanNumber(d.data, d.pos)
	if end == d.pos {
		return nil, d.syntax("expecting a value")
	}
	literal := string(d.data[d.pos:end])
	integer, _ := numberLiteral(literal)
	if integer {
		digits := len(literal)
		if literal[0] == '-' {
			digits--
		}
		if digits > maxIntDigits {
			return nil, d.syntax("an integer of more than 4300 digits")
		}
	} else if f, _ := strconv.ParseFloat(literal, 64); math.IsInf(f, 0) {
		return nil, ErrNonFinite
	}
	d.pos = end
	return json.Number(literal), nil
}
