// Package tlc holds a shadow's tests to its spec: it runs TLC from proofs/,
// reads the state graph TLC writes, and reads what proofs/ records about each
// spec. It is for tests only; nothing the service runs imports it.
package tlc

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// ProofsDir finds the repository's proofs/ directory from the working
// directory, which `go test` sets to the package's.
func ProofsDir() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for {
		candidate := filepath.Join(dir, "proofs")
		if _, err := os.Stat(filepath.Join(candidate, "tla2tools.jar")); err == nil {
			return candidate, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", errors.New("no proofs/tla2tools.jar above the working directory")
		}
		dir = parent
	}
}

// Graph is a state graph as TLC wrote it: every distinct state it reached, by
// TLC's fingerprint, and every step it explored between them.
type Graph struct {
	States map[string]Value // by fingerprint
	Init   []string         // the initial states' fingerprints
	Edges  []Edge
}

// Edge is one step TLC explored: from a state, by an action, to a state.
type Edge struct {
	From, To string
	Action   string
}

// Dump runs TLC on spec (proofs/<spec>.tla) with the configuration text cfg,
// with one worker and deadlock checking off as the repository's runs have it,
// and returns the state graph TLC dumps with its actions' names.
func Dump(spec, cfg string) (*Graph, error) {
	proofs, err := ProofsDir()
	if err != nil {
		return nil, err
	}
	java, err := exec.LookPath("java")
	if err != nil {
		return nil, fmt.Errorf("java is needed to run TLC: %w", err)
	}
	work, err := os.MkdirTemp("", "tlc-dump-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(work)
	text, err := os.ReadFile(filepath.Join(proofs, spec+".tla"))
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(work, spec+".tla"), text, 0o644); err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(work, spec+".cfg"), []byte(cfg), 0o644); err != nil {
		return nil, err
	}
	dot := filepath.Join(work, "graph.dot")
	cmd := exec.Command(java, "-XX:+UseParallelGC", "-cp", filepath.Join(proofs, "tla2tools.jar"),
		"tlc2.TLC", "-deadlock", "-workers", "1", "-metadir", filepath.Join(work, "states"),
		"-dump", "dot,actionlabels", dot, "-config", spec+".cfg", spec+".tla")
	cmd.Dir = work
	out, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(out), "Model checking completed. No error has been found.") {
		return nil, fmt.Errorf("TLC did not finish cleanly (%v):\n%s", err, out)
	}
	return ReadDot(dot)
}

var (
	// A state: its fingerprint, its label, and the attributes TLC adds after
	// it, a tooltip and, on an initial state, a fill.
	nodeLine    = regexp.MustCompile(`^(-?\d+) \[label="((?:[^"\\]|\\.)*)"((?:,tooltip="(?:[^"\\]|\\.)*"|,style = filled)*)\];?$`)
	tooltipAttr = regexp.MustCompile(`,tooltip="(?:[^"\\]|\\.)*"`)
	edgeLine    = regexp.MustCompile(`^(-?\d+) -> (-?\d+) \[label="((?:[^"\\]|\\.)*)"`)
)

// ReadDot reads a state graph TLC wrote with `-dump dot,actionlabels`.
func ReadDot(path string) (*Graph, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	g := &Graph{States: map[string]Value{}}
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1<<20), 1<<26)
	for scanner.Scan() {
		line := scanner.Text()
		if m := edgeLine.FindStringSubmatch(line); m != nil {
			g.Edges = append(g.Edges, Edge{From: m[1], To: m[2], Action: unescape(m[3])})
			continue
		}
		if m := nodeLine.FindStringSubmatch(line); m != nil {
			v, err := ParseState(unescape(m[2]))
			if err != nil {
				return nil, fmt.Errorf("state %s: %w", m[1], err)
			}
			if old, seen := g.States[m[1]]; seen && !Equal(old, v) {
				return nil, fmt.Errorf("fingerprint %s names two states", m[1])
			}
			g.States[m[1]] = v
			if tooltipAttr.ReplaceAllString(m[3], "") == ",style = filled" {
				g.Init = append(g.Init, m[1])
			}
			continue
		}
		if strings.HasPrefix(line, "-") || line != "" && line[0] >= '0' && line[0] <= '9' {
			return nil, fmt.Errorf("a line that is neither a state nor a step: %.80q", line)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(g.States) == 0 {
		return nil, fmt.Errorf("%s holds no state", path)
	}
	for _, e := range g.Edges {
		if _, ok := g.States[e.From]; !ok {
			return nil, fmt.Errorf("an edge leaves %s, which is no state", e.From)
		}
		if _, ok := g.States[e.To]; !ok {
			return nil, fmt.Errorf("an edge reaches %s, which is no state", e.To)
		}
	}
	return g, nil
}

// unescape undoes DOT's escapes in a quoted label.
func unescape(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+1 < len(s) {
			i++
			switch s[i] {
			case 'n':
				b.WriteByte('\n')
			default:
				b.WriteByte(s[i])
			}
			continue
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// --- TLA+ values, as TLC prints them

// Value is a TLA+ value: int64, bool, string, Seq, Set or Record.
type Value any

// Seq is a sequence, which is also how TLC prints a function on 1..n.
type Seq []Value

// Set is a set, its members in the order TLC printed them.
type Set []Value

// Record is a record.
type Record map[string]Value

// Equal compares two values, sets as sets.
func Equal(a, b Value) bool { return Key(a) == Key(b) }

// Key is a canonical text for a value: sets sorted, records by field name.
func Key(v Value) string {
	switch x := v.(type) {
	case int64:
		return strconv.FormatInt(x, 10)
	case bool:
		return strconv.FormatBool(x)
	case string:
		return strconv.Quote(x)
	case Seq:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = Key(e)
		}
		return "<<" + strings.Join(parts, ",") + ">>"
	case Set:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = Key(e)
		}
		sort.Strings(parts)
		return "{" + strings.Join(parts, ",") + "}"
	case Record:
		names := make([]string, 0, len(x))
		for name := range x {
			names = append(names, name)
		}
		sort.Strings(names)
		parts := make([]string, len(names))
		for i, name := range names {
			parts[i] = name + "|->" + Key(x[name])
		}
		return "[" + strings.Join(parts, ",") + "]"
	}
	return fmt.Sprintf("?%T", v)
}

// ParseState reads a state as TLC prints it: `/\ name = value` for each
// variable. It returns a Record of the variables.
func ParseState(text string) (Record, error) {
	p := &parser{toks: tokenize(text)}
	state := Record{}
	for !p.done() {
		if err := p.expect(`/\`); err != nil {
			return nil, err
		}
		name := p.next()
		if err := p.expect("="); err != nil {
			return nil, err
		}
		v, err := p.value()
		if err != nil {
			return nil, fmt.Errorf("%s: %w", name, err)
		}
		state[name] = v
	}
	return state, nil
}

// ParseValue reads one value.
func ParseValue(text string) (Value, error) {
	p := &parser{toks: tokenize(text)}
	v, err := p.value()
	if err == nil && !p.done() {
		err = fmt.Errorf("text after the value: %q", p.toks[p.at:])
	}
	return v, err
}

func tokenize(s string) []string {
	var toks []string
	for i := 0; i < len(s); {
		c := s[i]
		switch {
		case c == ' ' || c == '\n' || c == '\t' || c == '\r':
			i++
		case c == '"':
			j := i + 1
			for j < len(s) && s[j] != '"' {
				if s[j] == '\\' {
					j++
				}
				j++
			}
			toks = append(toks, s[i:j+1])
			i = j + 1
		case strings.HasPrefix(s[i:], `/\`), strings.HasPrefix(s[i:], "<<"), strings.HasPrefix(s[i:], ">>"),
			strings.HasPrefix(s[i:], ":>"), strings.HasPrefix(s[i:], "@@"):
			toks = append(toks, s[i:i+2])
			i += 2
		case strings.HasPrefix(s[i:], "|->"):
			toks = append(toks, "|->")
			i += 3
		case strings.ContainsRune("{}[](),=", rune(c)):
			toks = append(toks, string(c))
			i++
		default:
			j := i
			for j < len(s) && (s[j] == '_' || s[j] == '-' && j == i ||
				s[j] >= '0' && s[j] <= '9' || s[j] >= 'a' && s[j] <= 'z' || s[j] >= 'A' && s[j] <= 'Z') {
				j++
			}
			if j == i {
				j = i + 1
			}
			toks = append(toks, s[i:j])
			i = j
		}
	}
	return toks
}

type parser struct {
	toks []string
	at   int
}

func (p *parser) done() bool { return p.at >= len(p.toks) }

func (p *parser) peek() string {
	if p.done() {
		return ""
	}
	return p.toks[p.at]
}

func (p *parser) next() string {
	t := p.peek()
	p.at++
	return t
}

func (p *parser) expect(tok string) error {
	if got := p.next(); got != tok {
		return fmt.Errorf("expected %q, got %q", tok, got)
	}
	return nil
}

func (p *parser) value() (Value, error) {
	t := p.next()
	switch {
	case t == "TRUE":
		return true, nil
	case t == "FALSE":
		return false, nil
	case strings.HasPrefix(t, `"`):
		s, err := strconv.Unquote(t)
		if err != nil {
			return nil, err
		}
		return s, nil
	case t == "{":
		items, err := p.list("}")
		return Set(items), err
	case t == "<<":
		items, err := p.list(">>")
		return Seq(items), err
	case t == "[":
		return p.record()
	case t == "(":
		return nil, errors.New("a function printed with :> and @@ is not read here")
	default:
		n, err := strconv.ParseInt(t, 10, 64)
		if err != nil {
			return nil, fmt.Errorf("not a value: %q", t)
		}
		return n, nil
	}
}

func (p *parser) list(end string) ([]Value, error) {
	var items []Value
	if p.peek() == end {
		p.next()
		return items, nil
	}
	for {
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		items = append(items, v)
		switch p.next() {
		case ",":
		case end:
			return items, nil
		default:
			return nil, fmt.Errorf("expected , or %s", end)
		}
	}
}

func (p *parser) record() (Value, error) {
	r := Record{}
	for {
		name := p.next()
		if err := p.expect("|->"); err != nil {
			return nil, err
		}
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		r[name] = v
		switch p.next() {
		case ",":
		case "]":
			return r, nil
		default:
			return nil, errors.New("expected , or ]")
		}
	}
}

// --- What proofs/ records

// Constants reads the CONSTANTS section of a .cfg as integers, the only kind
// the shadows take.
func Constants(cfgText string) (map[string]int, error) {
	consts := map[string]int{}
	inSection := false
	for _, line := range strings.Split(cfgText, "\n") {
		if i := strings.Index(line, `\*`); i >= 0 {
			line = line[:i]
		}
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		switch fields[0] {
		case "CONSTANT", "CONSTANTS":
			inSection = true
			fields = fields[1:]
		case "SPECIFICATION", "INVARIANT", "INVARIANTS", "PROPERTY", "PROPERTIES", "INIT", "NEXT", "SYMMETRY",
			"VIEW", "CONSTRAINT", "CONSTRAINTS", "ACTION_CONSTRAINT", "ACTION_CONSTRAINTS", "CHECK_DEADLOCK",
			"POSTCONDITION", "ALIAS":
			inSection = false
			continue
		}
		if !inSection || len(fields) == 0 {
			continue
		}
		joined := strings.Join(fields, " ")
		name, value, ok := strings.Cut(joined, "=")
		if !ok {
			return nil, fmt.Errorf("a constant that is not `Name = value`: %q", joined)
		}
		n, err := strconv.Atoi(strings.TrimSpace(value))
		if err != nil {
			return nil, fmt.Errorf("constant %s is not an integer: %q", strings.TrimSpace(name), value)
		}
		consts[strings.TrimSpace(name)] = n
	}
	return consts, nil
}

var statesLine = regexp.MustCompile(`^"([^"]+)" = (\d+)$`)

// GuardTableStates reads the `[states]` of proofs/<spec>.guards.toml: the
// distinct states each configuration reaches with every guard in place, as
// TLC counted them, by configuration file.
func GuardTableStates(spec string) (map[string]int, error) {
	proofs, err := ProofsDir()
	if err != nil {
		return nil, err
	}
	text, err := os.ReadFile(filepath.Join(proofs, spec+".guards.toml"))
	if err != nil {
		return nil, err
	}
	states := map[string]int{}
	in := false
	for _, line := range strings.Split(string(text), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "[") {
			in = line == "[states]"
			continue
		}
		if m := statesLine.FindStringSubmatch(line); in && m != nil {
			n, _ := strconv.Atoi(m[2])
			states[m[1]] = n
		}
	}
	if len(states) == 0 {
		return nil, fmt.Errorf("%s.guards.toml has no [states]", spec)
	}
	return states, nil
}
