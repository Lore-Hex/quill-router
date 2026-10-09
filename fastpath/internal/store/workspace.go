package store

import (
	"context"
	"errors"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// The fast path's switch (docs/design/fast-admission-production-rollout.md,
// W1): a workspace is granted leases only while its row in
// tr_fastpath_workspace says it is enabled. None is by default.

// workspaceEnabled reads the workspace's switch in the transaction: false
// when it has no row.
func workspaceEnabled(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string) (bool, error) {
	row, err := txn.ReadRowWithOptions(ctx, "tr_fastpath_workspace", spanner.Key{workspace}, []string{"enabled"},
		&spanner.ReadOptions{RequestTag: tag("grant")})
	if spanner.ErrCode(err) == codes.NotFound {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var enabled bool
	err = row.Column(0, &enabled)
	return enabled && err == nil, err
}

// SetWorkspace turns the fast path on or off for a workspace, and reports
// how many open leases it revoked. Turning it off revokes every open lease of
// the workspace in the same transaction: each takes no renewal, so it
// expires, drains and closes, and what it admitted still settles; and a grant
// is either before it, its lease then revoked, or after it, and refused.
func (s *Store) SetWorkspace(ctx context.Context, workspace string, enabled bool) (int64, time.Time, error) {
	if workspace == "" {
		return 0, time.Time{}, errors.New("store: no workspace to switch")
	}
	var revoked int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		revoked = 0
		if err := txn.BufferWrite([]*spanner.Mutation{spanner.InsertOrUpdate("tr_fastpath_workspace",
			[]string{"workspace_id", "enabled", "changed_at"},
			[]any{workspace, enabled, spanner.CommitTimestamp})}); err != nil {
			return err
		}
		if enabled {
			return nil
		}
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease SET revoked = TRUE, revoked_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND state = 'open' AND NOT revoked`,
			Params: map[string]any{"w": workspace},
		}, spanner.QueryOptions{RequestTag: tag("switch")})
		revoked = n
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("switch")})
	if err != nil {
		return 0, time.Time{}, err
	}
	return revoked, resp.CommitTs.UTC(), nil
}

// EnabledWorkspaces reads, strongly, the workspaces the fast path may grant
// leases for, and the read's timestamp.
func (s *Store) EnabledWorkspaces(ctx context.Context) (map[string]bool, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	out := map[string]bool{}
	err := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT workspace_id FROM tr_fastpath_workspace WHERE enabled`,
	}, spanner.QueryOptions{RequestTag: tag("switch")}).Do(func(row *spanner.Row) error {
		var w string
		if err := row.Column(0, &w); err != nil {
			return err
		}
		out[w] = true
		return nil
	})
	if err != nil {
		return nil, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	return out, read, err
}
