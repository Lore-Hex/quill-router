package store

import (
	"context"
	"errors"

	"cloud.google.com/go/spanner"
)

// CreditStanding is a workspace's credit rows as the decide shadow reads
// them to settle Python's credit refusal (edge, confirmFunding): each row's
// headroom (its credit less its usage and its reservations) in shard order,
// and whether any row is marked in debt.
type CreditStanding struct {
	Headroom []int64
	Marked   bool
}

// ReadCreditStanding reads every credit row of the workspace in one strong
// single-use read at low priority, outside any transaction, as Python's own
// check of a remembered workspace reads them
// (storage_gcp_authorize.py's snapshot of tr_credit_balance): the shadow
// asks only after Python refused a request for credit, off the request path
// and within its own limiter. Strong, since a stale read would miss the hold
// or the mark Python's refusal saw. Rows that are not shards 0 to n-1 are
// ErrCreditRowsIncomplete, and a headroom past int64 is the statement's own
// error: the caller then settles nothing.
func (s *Store) ReadCreditStanding(ctx context.Context, workspace string) (CreditStanding, error) {
	if workspace == "" {
		return CreditStanding{}, errors.New("store: no workspace")
	}
	ro := s.client.Single()
	defer ro.Close()
	var out CreditStanding
	err := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT shard, total_credits - total_usage - reserved, COALESCE(in_debt, FALSE)
		        FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": workspace},
	}, lowPriority("read-credit-standing")).Do(func(row *spanner.Row) error {
		var shard, headroom int64
		var marked bool
		if err := row.Columns(&shard, &headroom, &marked); err != nil {
			return err
		}
		if shard != int64(len(out.Headroom)) {
			return ErrCreditRowsIncomplete
		}
		out.Headroom = append(out.Headroom, headroom)
		out.Marked = out.Marked || marked
		return nil
	})
	if err != nil {
		return CreditStanding{}, err
	}
	if len(out.Headroom) == 0 {
		return CreditStanding{}, ErrCreditRowsIncomplete
	}
	return out, nil
}
