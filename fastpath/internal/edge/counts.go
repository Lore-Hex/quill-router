package edge

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// The edge's count lines, read back: each period the decide path logs one
// line of its counts ("fastpath edge decide: ...", decider.flush) and the
// decide shadow one a snapshot and policy and one of the requests it did not
// follow ("fastpath edge decide shadow: ...", fullShadow.flush). Every count
// in them is the period's alone, so a span's lines, summed, are its counts:
// fastpathwatch's edge report sums them for the I2 gates
// (docs/runbooks/fastpath-watch.md). The reader is here, beside the writers,
// so that a test holds the two to one format.

// The lines' kinds, as CountsLine.Of names them.
const (
	CountsDecide       = "decide"
	CountsDecideShadow = "decide shadow"
)

// countsPrefixes are the lines' prefixes, the logger's own prefix (its date
// and time) before them.
var countsPrefixes = []struct{ prefix, of string }{
	{"fastpath edge decide: ", CountsDecide},
	{"fastpath edge decide shadow: ", CountsDecideShadow},
}

// CountsLine is one count line: its kind, the snapshot and policy digests
// of a shadow's line of them ("snapshot=S policy=P", empty for the others),
// its plain counts and its counts by name (classes, fields, passed, ...).
type CountsLine struct {
	Of     string
	Digest string
	Counts map[string]int64
	ByName map[string]map[string]int64
}

// ParseCountsLine reads a log message as a count line. ok is false for a
// message that is not one; an error is one that is, and does not read as
// one, which a reader must not skip: its counts would be lost.
func ParseCountsLine(message string) (line CountsLine, ok bool, err error) {
	rest := ""
	for _, p := range countsPrefixes {
		if i := strings.Index(message, p.prefix); i >= 0 {
			line.Of, rest, ok = p.of, message[i+len(p.prefix):], true
			break
		}
	}
	if !ok {
		return CountsLine{}, false, nil
	}
	line.Counts, line.ByName = map[string]int64{}, map[string]map[string]int64{}
	var digest []string
	for _, field := range strings.Fields(rest) {
		name, value, found := strings.Cut(field, "=")
		switch {
		case !found || name == "":
			return CountsLine{}, true, fmt.Errorf("edge: count line field %q", field)
		case line.Of == CountsDecideShadow && (name == "snapshot" || name == "policy"):
			digest = append(digest, field)
		case strings.HasPrefix(value, "{"):
			m := map[string]int64{}
			if err := json.Unmarshal([]byte(value), &m); err != nil {
				return CountsLine{}, true, fmt.Errorf("edge: count line field %s: %w", name, err)
			}
			line.ByName[name] = m
		default:
			n, err := strconv.ParseInt(value, 10, 64)
			if err != nil {
				return CountsLine{}, true, fmt.Errorf("edge: count line field %s: %w", name, err)
			}
			line.Counts[name] = n
		}
	}
	line.Digest = strings.Join(digest, " ")
	return line, true, nil
}
