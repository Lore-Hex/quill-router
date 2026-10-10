package store

import (
	"context"
	"errors"
	"fmt"
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

// DisableAll turns the fast path off for every workspace, the one switch
// that empties the allow-list, and reports how many workspaces it turned off
// and how many open leases it revoked: every open lease, in the same
// transaction, so none takes a renewal and none is granted after it.
func (s *Store) DisableAll(ctx context.Context) (int64, int64, time.Time, error) {
	var off, revoked int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		counts, err := txn.BatchUpdateWithOptions(ctx, []spanner.Statement{
			{SQL: `UPDATE tr_fastpath_workspace SET enabled = FALSE, changed_at = PENDING_COMMIT_TIMESTAMP()
			        WHERE enabled`},
			{SQL: `UPDATE tr_lease SET revoked = TRUE, revoked_at = CURRENT_TIMESTAMP()
			        WHERE state = 'open' AND NOT revoked`},
		}, spanner.QueryOptions{RequestTag: tag("switch")})
		if err != nil {
			return err
		}
		off, revoked = counts[0], counts[1]
		return nil
	}, spanner.TransactionOptions{TransactionTag: tag("switch")})
	if err != nil {
		return 0, 0, time.Time{}, err
	}
	return off, revoked, resp.CommitTs.UTC(), nil
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

// WorkspaceStatus is what turning a workspace off waits for (the production
// rollout's W2), as one read-only snapshot of its rows read by its keys.
type WorkspaceStatus struct {
	Workspace string
	Enabled   bool
	// Open, Revoked, Draining and Closed count its leases by state; Revoked
	// counts the open ones revoked.
	Open, Revoked, Draining, Closed int64
	// LeaseReserved is what its leases' donors hold beyond what they
	// booked; CreditReserved, its credit rows' reservations, the leases'
	// and any synchronous holds' together.
	LeaseReserved  int64
	CreditReserved int64
	// PendingPacks are its leases' packs whose work is not done.
	PendingPacks int64
	// Staged are its leases' staged full records not yet dropped: the
	// pending work drops its winners', and its sweep the rest once the
	// lease retires.
	Staged int64
	ReadTS time.Time
}

// Done says whether the workspace is off and nothing of the fast path is
// left of it: no lease open or draining, nothing its leases' donors hold, no
// pack's work pending and no staged record; and if not, why not. Done
// stays done: no lease is granted for a workspace off, and nothing is
// staged for a lease once retired, as each of its leases is, closed with
// its work done.
func (w WorkspaceStatus) Done() (bool, []string) {
	var why []string
	if w.Enabled {
		why = append(why, "it is enabled")
	}
	if w.Open > 0 {
		why = append(why, fmt.Sprintf("%d leases are open, %d of them revoked", w.Open, w.Revoked))
	}
	if w.Draining > 0 {
		why = append(why, fmt.Sprintf("%d leases are draining", w.Draining))
	}
	if w.LeaseReserved != 0 {
		why = append(why, fmt.Sprintf("its leases' donors hold %d", w.LeaseReserved))
	}
	if w.PendingPacks > 0 {
		why = append(why, fmt.Sprintf("%d packs' work is pending", w.PendingPacks))
	}
	if w.Staged > 0 {
		why = append(why, fmt.Sprintf("%d staged records are not dropped", w.Staged))
	}
	return len(why) == 0, why
}

// WorkspaceStatus reads a workspace's switch, leases, donors, credit rows,
// pending packs and staged records in one read-only transaction, each by the
// workspace's key, the staged records through their index on their lease:
// it scans nothing of another workspace's.
func (s *Store) WorkspaceStatus(ctx context.Context, workspace string) (WorkspaceStatus, error) {
	if workspace == "" {
		return WorkspaceStatus{}, errors.New("store: no workspace")
	}
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	out := WorkspaceStatus{Workspace: workspace}
	w := map[string]any{"w": workspace}
	row, err := ro.ReadRowWithOptions(ctx, "tr_fastpath_workspace", spanner.Key{workspace}, []string{"enabled"},
		&spanner.ReadOptions{RequestTag: tag("status")})
	switch {
	case spanner.ErrCode(err) == codes.NotFound:
	case err != nil:
		return WorkspaceStatus{}, err
	default:
		if err := row.Column(0, &out.Enabled); err != nil {
			return WorkspaceStatus{}, err
		}
	}
	queries := []struct {
		sql  string
		read func(*spanner.Row) error
	}{
		{`SELECT state, revoked FROM tr_lease WHERE workspace_id = @w`, func(r *spanner.Row) error {
			var state string
			var revoked bool
			if err := r.Columns(&state, &revoked); err != nil {
				return err
			}
			switch state {
			case "open":
				out.Open++
				if revoked {
					out.Revoked++
				}
			case "draining":
				out.Draining++
			default:
				out.Closed++
			}
			return nil
		}},
		{`SELECT COALESCE(SUM(allocation - consumed), 0) FROM tr_lease_donor WHERE workspace_id = @w`,
			func(r *spanner.Row) error { return r.Column(0, &out.LeaseReserved) }},
		{`SELECT COALESCE(SUM(reserved), 0) FROM tr_credit_balance WHERE workspace_id = @w`,
			func(r *spanner.Row) error { return r.Column(0, &out.CreditReserved) }},
		{`SELECT COUNT(*) FROM tr_lease_winners WHERE workspace_id = @w AND work_done_at IS NULL`,
			func(r *spanner.Row) error { return r.Column(0, &out.PendingPacks) }},
		{`SELECT COUNT(*) FROM tr_lease_staged@{FORCE_INDEX=tr_lease_staged_by_lease} WHERE workspace_id = @w`,
			func(r *spanner.Row) error { return r.Column(0, &out.Staged) }},
	}
	for _, q := range queries {
		err := ro.QueryWithOptions(ctx, spanner.Statement{SQL: q.sql, Params: w},
			spanner.QueryOptions{RequestTag: tag("status")}).Do(q.read)
		if err != nil {
			return WorkspaceStatus{}, err
		}
	}
	out.ReadTS, err = readTimestamp(ro)
	return out, err
}

// Booked is what a workspace has booked on its credit rows, all told: the
// sum of their total_usage, read by the workspace's key in a strong read.
func (s *Store) Booked(ctx context.Context, workspace string) (int64, error) {
	if workspace == "" {
		return 0, errors.New("store: no workspace")
	}
	var booked int64
	err := s.client.Single().QueryWithOptions(ctx, spanner.Statement{
		SQL:    `SELECT COALESCE(SUM(total_usage), 0) FROM tr_credit_balance WHERE workspace_id = @w`,
		Params: map[string]any{"w": workspace},
	}, spanner.QueryOptions{RequestTag: tag("booked")}).Do(func(r *spanner.Row) error { return r.Column(0, &booked) })
	return booked, err
}
