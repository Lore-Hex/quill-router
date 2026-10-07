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
	text, err := os.ReadFile(filepath.Join(proofs, spec+".tla"))
	if err != nil {
		return nil, err
	}
	return DumpText(spec, string(text), cfg)
}

// DumpText is Dump of a spec's text, such as a changed copy of one in proofs/.
func DumpText(spec, specText, cfg string) (*Graph, error) {
	var g *Graph
	err := DumpFile(spec, specText, cfg, func(path string) error {
		var err error
		g, err = ReadDot(path)
		return err
	})
	return g, err
}

// DumpFile runs TLC on the spec's text with the configuration text cfg, as
// Dump does, and hands the path of the state graph it wrote to use, before
// the file is removed. A graph too large to hold as values is read this way,
// with Compare.
func DumpFile(spec, specText, cfg string, use func(path string) error) error {
	proofs, err := ProofsDir()
	if err != nil {
		return err
	}
	java, err := exec.LookPath("java")
	if err != nil {
		return fmt.Errorf("java is needed to run TLC: %w", err)
	}
	work, err := os.MkdirTemp("", "tlc-dump-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	if err := os.WriteFile(filepath.Join(work, spec+".tla"), []byte(specText), 0o644); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(work, spec+".cfg"), []byte(cfg), 0o644); err != nil {
		return err
	}
	dot := filepath.Join(work, "graph.dot")
	cmd := exec.Command(java, "-XX:+UseParallelGC", "-Xmx1g", "-cp", filepath.Join(proofs, "tla2tools.jar"),
		"tlc2.TLC", "-deadlock", "-workers", "1", "-metadir", filepath.Join(work, "states"),
		"-dump", "dot,actionlabels", dot, "-config", spec+".cfg", spec+".tla")
	cmd.Dir = work
	out, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(out), "Model checking completed. No error has been found.") {
		return fmt.Errorf("TLC did not finish cleanly (%v):\n%s", err, out)
	}
	return use(dot)
}

// SpecText reads proofs/<spec>.tla.
func SpecText(spec string) (string, error) {
	proofs, err := ProofsDir()
	if err != nil {
		return "", err
	}
	text, err := os.ReadFile(filepath.Join(proofs, spec+".tla"))
	return string(text), err
}

var (
	// A state: its fingerprint, its label, and the attributes TLC adds after
	// it, a tooltip and, on an initial state, a fill.
	nodeLine    = regexp.MustCompile(`^(-?\d+) \[label="((?:[^"\\]|\\.)*)"((?:,tooltip="(?:[^"\\]|\\.)*"|,style = filled)*)\];?$`)
	tooltipAttr = regexp.MustCompile(`,tooltip="(?:[^"\\]|\\.)*"`)
	edgeLine    = regexp.MustCompile(`^(-?\d+) -> (-?\d+) \[label="((?:[^"\\]|\\.)*)",color="black",fontcolor="black"\];$`)
	rankLine    = regexp.MustCompile(`^\{rank = same; (?:-?\d+;)+\}$`)
	// The rest of what TLC writes around the states and steps, line for line.
	// Anything else, an indented step included, is refused rather than
	// skipped.
	scaffolding = map[string]bool{
		"strict digraph DiskGraph {":     true,
		"node [shape=box,style=rounded]": true,
		"nodesep=0.35;":                  true,
		"subgraph cluster_graph {":       true,
		`color="white";`:                 true,
		"}":                              true,
		"":                               true,
	}
)

// ReadDot reads a state graph TLC wrote with `-dump dot,actionlabels`.
func ReadDot(path string) (*Graph, error) {
	g := &Graph{States: map[string]Value{}}
	err := streamDot(path,
		func(fp string, v Value, initial bool) error {
			if old, seen := g.States[fp]; seen && !Equal(old, v) {
				return fmt.Errorf("fingerprint %s names two states", fp)
			}
			g.States[fp] = v
			if initial {
				g.Init = append(g.Init, fp)
			}
			return nil
		},
		func(from, to, action string) error {
			g.Edges = append(g.Edges, Edge{From: from, To: to, Action: action})
			return nil
		})
	if err != nil {
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

// streamDot reads a dump line by line, handing each state and each step to
// its function as it comes. It refuses any line that is not a state, a step
// or the scaffolding TLC writes around them.
func streamDot(path string, state func(fp string, v Value, initial bool) error,
	edge func(from, to, action string) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1<<20), 1<<26)
	for scanner.Scan() {
		line := scanner.Text()
		if m := edgeLine.FindStringSubmatch(line); m != nil {
			if err := edge(m[1], m[2], unescape(m[3])); err != nil {
				return err
			}
			continue
		}
		if m := nodeLine.FindStringSubmatch(line); m != nil {
			v, err := ParseState(unescape(m[2]))
			if err != nil {
				return fmt.Errorf("state %s: %w", m[1], err)
			}
			if err := state(m[1], v, tooltipAttr.ReplaceAllString(m[3], "") == ",style = filled"); err != nil {
				return err
			}
			continue
		}
		if !scaffolding[line] && !rankLine.MatchString(line) {
			return fmt.Errorf("a line that is no state, step or part of TLC's graph: %.80q", line)
		}
	}
	return scanner.Err()
}

// Step is one step of a shadow: the action TLC would label it with, and the
// state it leads to.
type Step[S comparable] struct {
	Action string
	To     S
}

// Shadow is what Compare needs of a spec's shadow.
type Shadow[S comparable] struct {
	Init S
	Next func(S) []Step[S]
	// Check judges each state the shadow reaches; an error is reported as a
	// difference, such as an invariant that does not hold.
	Check func(S) error
}

// Comparison is what Compare found: how many states and steps TLC's graph
// has, and the differences, the first limit of each kind.
type Comparison struct {
	States, Steps int
	Diffs         []string
}

type triple struct {
	from, to int64
	action   int32
}

// Compare holds a shadow against the state graph TLC dumped at path, reading
// each of TLC's states into the shadow's type with read, which must refuse
// anything the type does not hold exactly. It is the comparison of
// whole graphs: the same initial state, the same states, and from each the
// same actions to the same states. States are converted as the dump is read
// and kept only as the shadow's values, so a graph of a few hundred thousand
// states fits in memory.
func Compare[S comparable](path string, read func(Record) (S, error), shadow Shadow[S], limit int) (Comparison, error) {
	var out Comparison
	byFP := map[int64]S{}
	byState := map[S]int64{}
	var inits []int64
	actions := map[string]int32{}
	actionID := func(name string) int32 {
		id, ok := actions[name]
		if !ok {
			id = int32(len(actions))
			actions[name] = id
		}
		return id
	}
	theirs := map[triple]struct{}{}
	parseFP := func(text string) (int64, error) { return strconv.ParseInt(text, 10, 64) }
	err := streamDot(path,
		func(text string, v Value, initial bool) error {
			fp, err := parseFP(text)
			if err != nil {
				return err
			}
			r, ok := v.(Record)
			if !ok {
				return fmt.Errorf("state %s is not a record of variables", text)
			}
			s, err := read(r)
			if err != nil {
				return fmt.Errorf("state %s: %w", text, err)
			}
			if old, seen := byFP[fp]; seen {
				if old != s {
					return fmt.Errorf("fingerprint %s names two states", text)
				}
			} else if other, dup := byState[s]; dup {
				return fmt.Errorf("TLC's states %d and %d are one state here: the reading loses something", other, fp)
			}
			byFP[fp], byState[s] = s, fp
			if initial {
				inits = append(inits, fp)
			}
			return nil
		},
		func(fromText, toText, action string) error {
			from, err := parseFP(fromText)
			if err != nil {
				return err
			}
			to, err := parseFP(toText)
			if err != nil {
				return err
			}
			theirs[triple{from, to, actionID(action)}] = struct{}{}
			return nil
		})
	if err != nil {
		return out, err
	}
	for t := range theirs {
		if _, ok := byFP[t.from]; !ok {
			return out, fmt.Errorf("a step leaves %d, which is no state", t.from)
		}
		if _, ok := byFP[t.to]; !ok {
			return out, fmt.Errorf("a step reaches %d, which is no state", t.to)
		}
	}
	out.States, out.Steps = len(byFP), len(theirs)
	add := func(format string, args ...any) {
		if len(out.Diffs) < limit {
			out.Diffs = append(out.Diffs, fmt.Sprintf(format, args...))
		}
	}
	if len(inits) != 1 || byFP[inits[0]] != shadow.Init {
		add("TLC's initial states %v are not the shadow's Init", inits)
	}
	// The shadow's graph, from Init: each state it reaches must be one of
	// TLC's, and each of its steps one TLC took.
	seen := map[S]struct{}{shadow.Init: {}}
	queue := []S{shadow.Init}
	ours := map[triple]struct{}{}
	for len(queue) > 0 {
		s := queue[0]
		queue = queue[1:]
		if shadow.Check != nil {
			if err := shadow.Check(s); err != nil {
				add("%v in %+v", err, s)
			}
		}
		from, known := byState[s]
		if !known {
			add("the shadow reaches a state TLC does not: %+v", s)
		}
		for _, st := range shadow.Next(s) {
			to, knownTo := byState[st.To]
			if known && knownTo {
				id, named := actions[st.Action]
				t := triple{from, to, id}
				if _, ok := theirs[t]; !named || !ok {
					add("the shadow takes %s where TLC does not, from %+v", st.Action, s)
				}
				ours[t] = struct{}{}
			}
			if _, ok := seen[st.To]; !ok {
				seen[st.To] = struct{}{}
				queue = append(queue, st.To)
			}
		}
	}
	if len(seen) != len(byFP) {
		add("the shadow reaches %d states and TLC %d", len(seen), len(byFP))
	}
	names := make([]string, len(actions))
	for name, id := range actions {
		names[id] = name
	}
	for t := range theirs {
		if _, ok := ours[t]; !ok {
			add("TLC takes %s where the shadow does not, from %+v", names[t.action], byFP[t.from])
		}
	}
	return out, nil
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

// Value is a TLA+ value: int64, bool, string, ModelValue, Seq, Set, Record
// or Func.
type Value any

// ModelValue is a model value, such as a1 in `Auths = {a1, a2}`: an
// identifier, unlike a string.
type ModelValue string

// Func is a function TLC prints as `(k1 :> v1 @@ k2 :> v2)`: one whose domain
// is not 1..n. Its pairs are in the order TLC printed them.
type Func []Pair

// Pair is one argument of a Func and its value.
type Pair struct {
	Arg, Val Value
}

// At is the value of f at arg, and whether arg is in its domain.
func (f Func) At(arg Value) (Value, bool) {
	for _, p := range f {
		if Equal(p.Arg, arg) {
			return p.Val, true
		}
	}
	return nil, false
}

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
	case ModelValue:
		return "@" + string(x)
	case Func:
		parts := make([]string, len(x))
		for i, p := range x {
			parts[i] = Key(p.Arg) + ":>" + Key(p.Val)
		}
		sort.Strings(parts)
		return "(" + strings.Join(parts, "@@") + ")"
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
		return p.function()
	default:
		if n, err := strconv.ParseInt(t, 10, 64); err == nil {
			return n, nil
		}
		if identifier.MatchString(t) {
			return ModelValue(t), nil
		}
		return nil, fmt.Errorf("not a value: %q", t)
	}
}

// identifier is a TLA+ identifier: letters, digits and underscores, with a letter.
var identifier = regexp.MustCompile(`^[A-Za-z0-9_]*[A-Za-z][A-Za-z0-9_]*$`)

func (p *parser) function() (Value, error) {
	var f Func
	for {
		arg, err := p.value()
		if err != nil {
			return nil, err
		}
		if err := p.expect(":>"); err != nil {
			return nil, err
		}
		val, err := p.value()
		if err != nil {
			return nil, err
		}
		if _, dup := f.At(arg); dup {
			return nil, fmt.Errorf("a function with %s twice in its domain", Key(arg))
		}
		f = append(f, Pair{arg, val})
		switch p.next() {
		case "@@":
		case ")":
			return f, nil
		default:
			return nil, errors.New("expected @@ or )")
		}
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

// ConstantValues reads the CONSTANT and CONSTANTS sections of a .cfg: each
// `Name = value`, where a value is an integer, a string, TRUE or FALSE, a
// model value, or a set of these, whose repeated members count once as TLC's
// do. A replacement `Name <- Op`, or a value of any other kind, is refused.
// It reads tokens, not lines, so a string keeps its spaces and a value may
// run across lines.
func ConstantValues(cfgText string) (map[string]Value, error) {
	text, err := withoutComments(cfgText)
	if err != nil {
		return nil, err
	}
	p := &parser{toks: tokenize(text)}
	consts := map[string]Value{}
	in := false
	for !p.done() {
		t := p.next()
		if cfgKeywords[t] {
			in = t == "CONSTANT" || t == "CONSTANTS"
			continue
		}
		if !in {
			continue
		}
		if !identifier.MatchString(t) {
			return nil, fmt.Errorf("a constant named %q", t)
		}
		if err := p.expect("="); err != nil {
			return nil, fmt.Errorf("constant %s: only `Name = value` is read: %w", t, err)
		}
		v, err := p.configValue()
		if err != nil {
			return nil, fmt.Errorf("constant %s: %w", t, err)
		}
		if _, dup := consts[t]; dup {
			return nil, fmt.Errorf("constant %s is set twice", t)
		}
		consts[t] = v
	}
	return consts, nil
}

// The words that open a section of a .cfg.
var cfgKeywords = map[string]bool{
	"CONSTANT": true, "CONSTANTS": true, "INIT": true, "NEXT": true, "SPECIFICATION": true,
	"INVARIANT": true, "INVARIANTS": true, "PROPERTY": true, "PROPERTIES": true, "SYMMETRY": true,
	"VIEW": true, "CONSTRAINT": true, "CONSTRAINTS": true, "ACTION_CONSTRAINT": true,
	"ACTION_CONSTRAINTS": true, "CHECK_DEADLOCK": true, "POSTCONDITION": true, "ALIAS": true,
}

// withoutComments removes `\*` line comments and `(* *)` block comments,
// which may nest, leaving strings as they are.
func withoutComments(text string) (string, error) {
	var b strings.Builder
	depth := 0
	for i := 0; i < len(text); i++ {
		switch {
		case depth == 0 && text[i] == '"':
			j := i + 1
			for j < len(text) && text[j] != '"' {
				if text[j] == '\\' {
					j++
				}
				j++
			}
			if j >= len(text) {
				return "", errors.New("a string that never closes")
			}
			b.WriteString(text[i : j+1])
			i = j
		case strings.HasPrefix(text[i:], "(*"):
			depth++
			i++
		case depth > 0 && strings.HasPrefix(text[i:], "*)"):
			depth--
			i++
		case depth > 0:
		case strings.HasPrefix(text[i:], `\*`):
			for i < len(text) && text[i] != '\n' {
				i++
			}
			b.WriteByte('\n')
		default:
			b.WriteByte(text[i])
		}
	}
	if depth != 0 {
		return "", errors.New("a comment that never closes")
	}
	return b.String(), nil
}

// configValue reads a value as a .cfg may give one.
func (p *parser) configValue() (Value, error) {
	t := p.peek()
	if t == "{" {
		p.next()
		var members Set
		seen := map[string]bool{}
		if p.peek() == "}" {
			p.next()
			return members, nil
		}
		for {
			v, err := p.configValue()
			if err != nil {
				return nil, err
			}
			if k := Key(v); !seen[k] {
				seen[k] = true
				members = append(members, v)
			}
			switch p.next() {
			case ",":
			case "}":
				return members, nil
			default:
				return nil, errors.New("expected , or } in a set")
			}
		}
	}
	if t == "<<" || t == "[" || t == "(" || cfgKeywords[t] {
		return nil, fmt.Errorf("a .cfg gives no value that starts %q", t)
	}
	return p.value()
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
