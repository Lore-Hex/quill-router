package store

import (
	"context"
	"encoding/json"
	"errors"

	"cloud.google.com/go/spanner"
)

// The gateway's key metadata, read as production's authorize reads it
// (storage_gcp.py, _GATEWAY_API_KEY_AUTH_CONTEXT_SQL): one strong
// single-use query of production's entity table, every read in it bounded
// by a complete primary key or a primary-key range. The lookup pointer by
// its id, the key by the id the pointer names, the workspace by the id the
// key names, and the workspace's BYOK entities by the range [ws#, ws$), the
// ids of every entity whose id starts "ws#" ('$' follows '#'). Two
// differences from Python's query, both narrowings. The BYOK entities' ids
// are read and their bodies never are: a body holds a credential, and the
// edge needs only to know which providers have one. And an EXISTS over the
// same range of the broadcast_destination_by_workspace pointers
// (storage_gcp_broadcast.py) says whether the workspace has a broadcast
// destination, without reading a destination. The boot is not folded in:
// the edge reads boots by their key ids (ReadBoot) and caches them apart.
// Nor is a strict key's counter row: the key's id (its hash) is selected so
// that, for a body that says budget_strict, a point read of its tr_key_limit
// shard 0 follows (readStrictKeyLimit), and no other key reads that table.
//
// A workspace id may contain '#' (an imported one can), so the range of
// "ws" holds the entities of a workspace "ws#x" too. The BYOK ids are
// returned whole for the caller to match a complete id, as Python does; the
// broadcast EXISTS answers true for such a neighbour's pointer, which is
// the cautious answer (a workspace with a destination is passed to Python).
const keyContextSQL = `/* fastpath_key_context */
SELECT key_record.id, key_record.body, JSON_VALUE(key_record.body, '$.workspace_id') AS range_workspace_id,
  workspace_record.body,
  ARRAY(
    SELECT byok_record.id
    FROM tr_entities AS byok_record
    WHERE byok_record.kind = 'byok'
      AND byok_record.id >= CONCAT(JSON_VALUE(key_record.body, '$.workspace_id'), '#')
      AND byok_record.id < CONCAT(JSON_VALUE(key_record.body, '$.workspace_id'), '$')
    ORDER BY byok_record.id
  ) AS byok_ids,
  EXISTS(
    SELECT 1
    FROM tr_entities AS pointer_record
    WHERE pointer_record.kind = 'broadcast_destination_by_workspace'
      AND pointer_record.id >= CONCAT(JSON_VALUE(key_record.body, '$.workspace_id'), '#')
      AND pointer_record.id < CONCAT(JSON_VALUE(key_record.body, '$.workspace_id'), '$')
  ) AS broadcast_destination
FROM tr_entities AS lookup_record
JOIN tr_entities AS key_record
  ON key_record.kind = 'api_key'
 AND key_record.id = JSON_VALUE(lookup_record.body, '$.key_id')
LEFT JOIN tr_entities AS workspace_record
  ON workspace_record.kind = 'workspace'
 AND workspace_record.id = JSON_VALUE(key_record.body, '$.workspace_id')
WHERE lookup_record.kind = 'api_key_lookup'
  AND lookup_record.id = @lookup_hash`

// KeyContext is what one strong read finds for an API key's lookup hash:
// the key's body, its workspace's body if there is one, the ids of the
// workspace's BYOK entities, and whether the workspace has a broadcast
// destination pointer. The bodies are production's JSON, uninterpreted;
// gwstate reads them.
type KeyContext struct {
	KeyBody string
	// RangeWorkspaceID is the workspace id the query read from the key's
	// body (JSON_VALUE) and joined the workspace and took the ranges by: a
	// reader of KeyBody must find the same id, or the rest is not the
	// key's workspace's.
	RangeWorkspaceID string
	// WorkspaceBody is "" and WorkspaceFound false when the key names no
	// workspace row (Python answers 403 for it, as for a deleted one).
	WorkspaceBody  string
	WorkspaceFound bool
	// BYOKIDs are complete entity ids, "ws#provider", in id order.
	BYOKIDs []string
	// BroadcastDestination is whether any broadcast_destination_by_workspace
	// pointer lies in the workspace's range.
	BroadcastDestination bool
	// StrictLimit is the key's tr_key_limit shard-0 row, read only when the
	// key's body says budget_strict true (StrictLimit.Read).
	StrictLimit StrictKeyLimit
}

// StrictKeyLimit is what Python's strict reserve reads of a budget_strict
// key's counter row (storage_gcp_strict_budget.reserve_strict_key): whether
// shard 0 exists (Row; without it the reserve is KEY_MISSING, a 402) and
// whether its mirrored lifetime and window limits, limit_micro and
// <w>_limit_micro, are all NULL (Limitless; then the reserve's UPDATE
// matches the row whatever it holds, so it never refuses the key for its
// budget). Read is false, and the rest with it, for a key whose body does
// not say budget_strict true: no read was made
// (docs/design/fast-admission-e2-strict-keys.md section 4.2).
type StrictKeyLimit struct {
	Read, Row, Limitless bool
}

// strictLimitColumns are the mirrored limits Python's strict reserve
// checks, read as nullable.
var strictLimitColumns = []string{"limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro"}

// bodySaysStrict is whether a key body's budget_strict is the JSON literal
// true, its last occurrence as Python's json.loads keeps it. Anything else
// (absent, false, another type, a body that does not decode) reads no
// row; gwstate decides on the body itself, and passes a key whose strict
// row was not read.
func bodySaysStrict(body string) bool {
	var key struct {
		BudgetStrict json.RawMessage `json:"budget_strict"`
	}
	return json.Unmarshal([]byte(body), &key) == nil && string(key.BudgetStrict) == "true"
}

// readStrictKeyLimit reads a strict key's tr_key_limit shard-0 row by its
// complete primary key (key_hash, shard), strongly and alone: a point read
// of one row, only for a key whose body says budget_strict, so no other
// key's context read touches the counter table.
func (s *Store) readStrictKeyLimit(ctx context.Context, keyHash string) (StrictKeyLimit, error) {
	ro := s.client.Single()
	defer ro.Close()
	row, err := ro.ReadRowWithOptions(ctx, "tr_key_limit", spanner.Key{keyHash, int64(0)},
		strictLimitColumns, &spanner.ReadOptions{RequestTag: tag("read-strict-key-limit")})
	// Only the row's absence is a missing row: another NotFound (no such
	// table) is a failed read, which leaves the row unread.
	if errors.Is(err, spanner.ErrRowNotFound) {
		return StrictKeyLimit{Read: true}, nil
	}
	if err != nil {
		return StrictKeyLimit{}, err
	}
	limits := make([]spanner.NullInt64, len(strictLimitColumns))
	ptrs := make([]any, len(limits))
	for i := range limits {
		ptrs[i] = &limits[i]
	}
	if err := row.Columns(ptrs...); err != nil {
		return StrictKeyLimit{}, err
	}
	out := StrictKeyLimit{Read: true, Row: true, Limitless: true}
	for _, l := range limits {
		if l.Valid {
			out.Limitless = false
		}
	}
	return out, nil
}

// ReadKeyContext reads the key a lookup hash names, with its workspace,
// BYOK ids and broadcast presence, in one strong single-use query. Found is
// false when there is no lookup pointer, or no key where it points: Python
// answers 401 for both.
func (s *Store) ReadKeyContext(ctx context.Context, lookupHash string) (KeyContext, bool, error) {
	if lookupHash == "" {
		return KeyContext{}, false, errors.New("store: no lookup hash")
	}
	ro := s.client.Single()
	defer ro.Close()
	iter := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL:    keyContextSQL,
		Params: map[string]any{"lookup_hash": lookupHash},
	}, spanner.QueryOptions{RequestTag: tag("read-key-context")})
	defer iter.Stop()
	var out KeyContext
	var keyID string
	rows := 0
	err := iter.Do(func(row *spanner.Row) error {
		rows++
		var rangeWorkspace, workspace spanner.NullString
		if err := row.Columns(&keyID, &out.KeyBody, &rangeWorkspace, &workspace, &out.BYOKIDs,
			&out.BroadcastDestination); err != nil {
			return err
		}
		out.RangeWorkspaceID = rangeWorkspace.StringVal
		out.WorkspaceBody, out.WorkspaceFound = workspace.StringVal, workspace.Valid
		return nil
	})
	if err != nil {
		return KeyContext{}, false, err
	}
	switch rows {
	case 0:
		return KeyContext{}, false, nil
	case 1:
		if bodySaysStrict(out.KeyBody) {
			// A second strong read, after the first, not in its snapshot:
			// a strict key's limits never go from none to some
			// (storage_key_patch.StrictKeyFirstLimit), its row mirrors them
			// in the same commit, and the row is never deleted while the
			// key exists. So a body read limitless meets a row as limitless
			// later, and a row present then is present after; a row found
			// missing that a backfill writes meanwhile only passes. A read
			// that fails leaves StrictLimit unread, which passes the key
			// (key.budget_strict_row.unread) until the next refresh, rather
			// than failing the key's other facts with it.
			if limit, err := s.readStrictKeyLimit(ctx, keyID); err == nil {
				out.StrictLimit = limit
			}
		}
		return out, true, nil
	}
	// Every join is on a complete primary key: more than one row is not a
	// table production's schema allows.
	return KeyContext{}, false, errors.New("store: a lookup hash named more than one key")
}

// CreditState is a workspace's credit rows as a grant reads them, read only:
// how many shards, whether any is marked in debt, paused or latched, the
// least trust tier (a latched shard's counted as zero), and the refusal the
// rows alone give a grant before its amount is weighed ("" for none).
type CreditState struct {
	Shards  int
	InDebt  bool
	Paused  bool
	Latched bool
	Tier    int64
	Refused Refusal
}

// ReadCreditState reads the workspace's credit rows in one strong
// single-use read, with the query a grant reads them with (readCreditRows).
// A workspace whose rows are not shards 0 to n-1, or that has none, is
// ErrCreditRowsIncomplete.
func (s *Store) ReadCreditState(ctx context.Context, workspace string) (CreditState, error) {
	if workspace == "" {
		return CreditState{}, errors.New("store: no workspace")
	}
	ro := s.client.Single()
	defer ro.Close()
	rows, err := readCreditRows(ctx, ro, workspace, "read-credit-state")
	if err != nil {
		return CreditState{}, err
	}
	out := CreditState{Shards: len(rows.headroom), InDebt: rows.marked(), Paused: anyTrue(rows.paused),
		Latched: anyTrue(rows.latched)}
	_, out.Tier, out.Refused = s.rowsRefuse(rows)
	return out, nil
}
