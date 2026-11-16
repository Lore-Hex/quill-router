package store

import (
	"context"
	"errors"
	"time"

	"cloud.google.com/go/spanner"
	sppb "cloud.google.com/go/spanner/apiv1/spannerpb"
)

// DebtMark is a workspace's debt mark as its credit rows stood at one
// instant: how many credit rows the workspace had, and whether any of them
// was marked in debt. Marked is the edge's own workspace.in_debt fact
// (CreditState.InDebt, creditRows.marked) and Python's (CreditRows.marked):
// any row's COALESCE(in_debt, FALSE). Every money write sets one mark on all
// of a workspace's rows, but a row written apart from them can differ until
// the next write, so one marked row is a marked workspace.
type DebtMark struct {
	Rows   int
	Marked bool
}

// ReadDebtMarkAt reads the workspace's debt mark as its credit rows stood at
// exactly at, for the decide shadow's triage of a Python 402 it would have
// admitted (docs/runbooks/fastpath-alerts.md, edge_would_admit_python_refused).
// One single-use read of tr_credit_balance by the workspace's primary-key
// prefix (workspace_id, every shard), of the in_debt column alone, at the
// read timestamp at, at low priority so it yields to the requests whose rows
// it reads. It is outside any transaction and takes no lock. The caller
// bounds it with a short deadline and does not ask again: a read that fails,
// a timestamp past the database's version retention among them
// (FailedPrecondition), leaves the triage unconfirmed. A workspace with no
// credit rows at that instant reads zero rows, which is not an error here:
// the caller decides what no rows mean.
func (s *Store) ReadDebtMarkAt(ctx context.Context, workspace string, at time.Time) (DebtMark, error) {
	if workspace == "" {
		return DebtMark{}, errors.New("store: no workspace")
	}
	if at.IsZero() {
		return DebtMark{}, errors.New("store: no read timestamp")
	}
	ro := s.client.Single().WithTimestampBound(spanner.ReadTimestamp(at))
	defer ro.Close()
	var out DebtMark
	err := ro.ReadWithOptions(ctx, "tr_credit_balance", spanner.Key{workspace}.AsPrefix(), []string{"in_debt"},
		&spanner.ReadOptions{RequestTag: tag("read-debt-mark-at"), Priority: sppb.RequestOptions_PRIORITY_LOW}).Do(
		func(r *spanner.Row) error {
			// NULL on rows older than the column: not marked.
			var marked spanner.NullBool
			if err := r.Columns(&marked); err != nil {
				return err
			}
			out.Rows++
			out.Marked = out.Marked || (marked.Valid && marked.Bool)
			return nil
		})
	if err != nil {
		return DebtMark{}, err
	}
	return out, nil
}
