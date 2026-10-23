package store

import (
	"context"
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
//
// A workspace id may contain '#' (an imported one can), so the range of
// "ws" holds the entities of a workspace "ws#x" too. The BYOK ids are
// returned whole for the caller to match a complete id, as Python does; the
// broadcast EXISTS answers true for such a neighbour's pointer, which is
// the cautious answer (a workspace with a destination is passed to Python).
const keyContextSQL = `/* fastpath_key_context */
SELECT key_record.body, workspace_record.body,
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
	// WorkspaceBody is "" and WorkspaceFound false when the key names no
	// workspace row (Python answers 403 for it, as for a deleted one).
	WorkspaceBody  string
	WorkspaceFound bool
	// BYOKIDs are complete entity ids, "ws#provider", in id order.
	BYOKIDs []string
	// BroadcastDestination is whether any broadcast_destination_by_workspace
	// pointer lies in the workspace's range.
	BroadcastDestination bool
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
	rows := 0
	err := iter.Do(func(row *spanner.Row) error {
		rows++
		var workspace spanner.NullString
		if err := row.Columns(&out.KeyBody, &workspace, &out.BYOKIDs, &out.BroadcastDestination); err != nil {
			return err
		}
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
		Latched: anyTrue(rows.latched), Tier: 3}
	for i, t := range rows.tiers {
		if rows.latched[i] {
			t = 0
		}
		out.Tier = min(out.Tier, t)
	}
	_, out.Refused = s.rowsRefuse(rows)
	return out, nil
}
