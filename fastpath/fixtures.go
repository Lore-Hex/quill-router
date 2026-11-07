// Package fastpath holds the fixtures the fast path's Go code embeds and
// Python reads, from testdata, so the two cannot drift.
package fastpath

import _ "embed"

// SwitchStatementsV1 is testdata/switch_statements_v1.json: the switch's
// writes (docs/design/fast-admission-widening.md, section 5.5), which
// Store.SetWorkspace executes through internal/switchsql and the enabler
// reads.
//
//go:embed testdata/switch_statements_v1.json
var SwitchStatementsV1 []byte
