// Package switchsql reads and executes the switch's writes,
// fastpath/testdata/switch_statements_v1.json
// (docs/design/fast-admission-widening.md, section 5.5): the fixture
// Store.SetWorkspace executes and the enabler reads, so the columns, the
// values and the revocation of a switch write cannot drift between them.
package switchsql

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath"
)

// The fixture's operations.
const (
	Enable        = "enable"
	Disable       = "disable"
	DisableUnheld = "disable_unheld"
)

// Mutation is one of an operation's mutations: an insert_or_update of the
// columns with the values, each a literal, "@name" for the parameter name,
// or "@commit_timestamp" for the transaction's commit timestamp.
type Mutation struct {
	Op      string            `json:"op"`
	Table   string            `json:"table"`
	Columns []string          `json:"columns"`
	Values  []json.RawMessage `json:"values"`
}

// Statement is one of an operation's statements, its row count reported
// under Counts.
type Statement struct {
	SQL    string `json:"sql"`
	Counts string `json:"counts"`
}

// Operation is one read-write transaction's writes.
type Operation struct {
	Mutations  []Mutation  `json:"mutations"`
	Statements []Statement `json:"statements"`
}

// Fixture is the whole of switch_statements_v1.json.
type Fixture struct {
	About      string               `json:"about"`
	Version    int                  `json:"version"`
	Operations map[string]Operation `json:"operations"`
}

// commitTimestamp is the value naming the transaction's commit timestamp.
const commitTimestamp = "@commit_timestamp"

// parameter is a statement's parameter.
var parameter = regexp.MustCompile(`@(\w+)`)

// Parse reads a fixture and refuses one this package cannot execute
// exactly: another version, a missing operation, a mutation other than an
// insert_or_update, columns and values that do not pair, a statement with
// no count's name or two with the same one.
func Parse(b []byte) (Fixture, error) {
	var f Fixture
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(&f); err != nil {
		return Fixture{}, fmt.Errorf("switchsql: %w", err)
	}
	if f.Version != 1 {
		return Fixture{}, fmt.Errorf("switchsql: version %d, want 1", f.Version)
	}
	names := []string{Enable, Disable, DisableUnheld}
	for name := range f.Operations {
		if !slices.Contains(names, name) {
			return Fixture{}, fmt.Errorf("switchsql: an operation %q this package does not know", name)
		}
	}
	for _, name := range names {
		op, ok := f.Operations[name]
		if !ok {
			return Fixture{}, fmt.Errorf("switchsql: no operation %q", name)
		}
		counts := map[string]bool{}
		for _, m := range op.Mutations {
			if m.Op != "insert_or_update" || m.Table == "" || len(m.Columns) == 0 || len(m.Columns) != len(m.Values) {
				return Fixture{}, fmt.Errorf("switchsql: %s: a mutation of %q that is not an insert_or_update of "+
					"paired columns and values", name, m.Table)
			}
		}
		for _, s := range op.Statements {
			if strings.TrimSpace(s.SQL) == "" || s.Counts == "" || counts[s.Counts] {
				return Fixture{}, fmt.Errorf("switchsql: %s: a statement without SQL or a count's name of its own", name)
			}
			counts[s.Counts] = true
		}
	}
	return f, nil
}

// Load is the embedded fixture, fastpath.SwitchStatementsV1.
func Load() (Fixture, error) {
	return Parse(fastpath.SwitchStatementsV1)
}

// value is a mutation's value: the literal, the parameter or the commit
// timestamp.
func value(raw json.RawMessage, params map[string]any) (any, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var v any
	if err := d.Decode(&v); err != nil {
		return nil, err
	}
	switch v := v.(type) {
	case string:
		if v == commitTimestamp {
			return spanner.CommitTimestamp, nil
		}
		if name, ok := strings.CutPrefix(v, "@"); ok {
			p, ok := params[name]
			if !ok {
				return nil, fmt.Errorf("no parameter %q", name)
			}
			return p, nil
		}
		return v, nil
	case json.Number:
		return v.Int64()
	case bool:
		return v, nil
	}
	return nil, fmt.Errorf("a value %s that is no string, integer or bool", raw)
}

// Apply buffers the operation's mutations and executes its statements in
// the transaction, in order, with the parameters each names, and returns
// each statement's row count by its count's name.
func (o Operation) Apply(ctx context.Context, txn *spanner.ReadWriteTransaction, params map[string]any,
	opts spanner.QueryOptions) (map[string]int64, error) {
	var ms []*spanner.Mutation
	for _, m := range o.Mutations {
		values := make([]any, len(m.Values))
		for i, raw := range m.Values {
			v, err := value(raw, params)
			if err != nil {
				return nil, fmt.Errorf("switchsql: %s.%s: %w", m.Table, m.Columns[i], err)
			}
			values[i] = v
		}
		ms = append(ms, spanner.InsertOrUpdate(m.Table, m.Columns, values))
	}
	if err := txn.BufferWrite(ms); err != nil {
		return nil, err
	}
	counts := map[string]int64{}
	for _, s := range o.Statements {
		named := map[string]any{}
		for _, m := range parameter.FindAllStringSubmatch(s.SQL, -1) {
			p, ok := params[m[1]]
			if !ok {
				return nil, fmt.Errorf("switchsql: %s: no parameter %q", s.Counts, m[1])
			}
			named[m[1]] = p
		}
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{SQL: s.SQL, Params: named}, opts)
		if err != nil {
			return nil, err
		}
		counts[s.Counts] = n
	}
	return counts, nil
}

// ErrNoOperation is Get's error for an operation the fixture lacks.
var ErrNoOperation = errors.New("switchsql: no such operation")

// Get is the fixture's operation by name.
func (f Fixture) Get(name string) (Operation, error) {
	op, ok := f.Operations[name]
	if !ok {
		return Operation{}, fmt.Errorf("%w: %q", ErrNoOperation, name)
	}
	return op, nil
}
