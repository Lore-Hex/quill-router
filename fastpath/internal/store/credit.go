package store

import (
	"context"
	"errors"
	"fmt"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/creditdebt"
)

// A workspace's credit rows, read and written inside a read-write
// transaction as production's storage_gcp_credit_debt.py does it: the rows
// are read with their headroom and mark, creditdebt decides what they end
// with, and each change is conditional on the headroom read, so a row that is
// not what was read stops the transaction. Rows are written in ascending
// shard order, the lock order of the design's section 4.7. Writes stamp
// updated_at with Spanner's CURRENT_TIMESTAMP(): a commit timestamp written
// by DML would bar the later statements on the same table that covering
// needs.
//
// Production's release also offers what repaid nothing to unrecovered
// payment claims (absorb_unrecovered_recovery_tx). The spike has no claims
// table, so its release does not; production's port of the store adds it.

// ErrCreditRowsChanged means a credit row is not what the transaction read:
// the transaction is to be rolled back.
var ErrCreditRowsChanged = errors.New("store: a credit row is not what this transaction read")

// ErrCreditRowsIncomplete means the workspace's credit rows are not shards
// 0 to n-1.
var ErrCreditRowsIncomplete = errors.New("store: the workspace's credit rows are not shards 0 to n-1")

// creditRows are a workspace's credit rows, in shard order: each row's
// headroom and mark, and what a grant reads besides, each row's effective
// trust tier as stored, whether a dispute has latched it, and whether
// billing is paused.
type creditRows struct {
	headroom []int64
	marks    []bool
	tiers    []int64
	latched  []bool
	paused   []bool
}

func (r creditRows) marked() bool {
	for _, m := range r.marks {
		if m {
			return true
		}
	}
	return false
}

// readCreditRows reads every credit row of the workspace in shard order.
func readCreditRows(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace, operation string) (creditRows, error) {
	iter := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT shard, total_credits - total_usage - reserved, COALESCE(in_debt, FALSE),
		             COALESCE(trust_tier, 0), trust_latched_at IS NOT NULL, billing_pause_causes
		        FROM tr_credit_balance WHERE workspace_id = @w ORDER BY shard`,
		Params: map[string]any{"w": workspace},
	}, spanner.QueryOptions{RequestTag: tag(operation)})
	var rows creditRows
	err := iter.Do(func(row *spanner.Row) error {
		var shard, headroom, tier int64
		var marked, latched bool
		var causes []spanner.NullString
		if err := row.Columns(&shard, &headroom, &marked, &tier, &latched, &causes); err != nil {
			return err
		}
		if shard != int64(len(rows.headroom)) {
			return ErrCreditRowsIncomplete
		}
		rows.headroom = append(rows.headroom, headroom)
		rows.marks = append(rows.marks, marked)
		rows.tiers = append(rows.tiers, tier)
		rows.latched = append(rows.latched, latched)
		// Production's reading: a NULL or empty list is no pause.
		rows.paused = append(rows.paused, len(causes) > 0)
		return nil
	})
	if err != nil {
		return creditRows{}, err
	}
	if len(rows.headroom) == 0 {
		return creditRows{}, ErrCreditRowsIncomplete
	}
	return rows, nil
}

// writeCreditRows moves credit so each row's headroom goes from before's to
// after, and sets marked on every row if any row's mark differs.
func writeCreditRows(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string, before creditRows,
	after []int64, marked bool, operation string) error {
	deltas, err := creditdebt.CreditDeltas(before.headroom, after)
	if err != nil {
		return err
	}
	for shard, delta := range deltas {
		if delta == 0 {
			continue
		}
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance SET total_credits = total_credits + @delta, updated_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND shard = @shard AND total_credits - total_usage - reserved = @before`,
			Params: map[string]any{"delta": delta, "w": workspace, "shard": int64(shard), "before": before.headroom[shard]},
		}, spanner.QueryOptions{RequestTag: tag(operation)})
		if err != nil {
			return err
		}
		if n != 1 {
			return fmt.Errorf("%w: %s/%d", ErrCreditRowsChanged, workspace, shard)
		}
	}
	for _, m := range before.marks {
		if m == marked {
			continue
		}
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance SET in_debt = @marked, updated_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND shard >= 0 AND shard < @n`,
			Params: map[string]any{"marked": marked, "w": workspace, "n": int64(len(before.marks))},
		}, spanner.QueryOptions{RequestTag: tag(operation)})
		if err != nil {
			return err
		}
		if n != int64(len(before.marks)) {
			return fmt.Errorf("%w: the debt mark did not reach every row of %s", ErrCreditRowsChanged, workspace)
		}
		break
	}
	return nil
}

// squareCreditRows ends a write that took money out: if the signed sum is
// negative, every row is marked; otherwise every negative row is covered from
// the others, and the mark is cleared.
func squareCreditRows(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace, operation string) (creditdebt.Squared, error) {
	rows, err := readCreditRows(ctx, txn, workspace, operation)
	if err != nil {
		return creditdebt.Squared{}, err
	}
	squared, err := creditdebt.Square(rows.headroom)
	if err != nil {
		return creditdebt.Squared{}, err
	}
	return squared, writeCreditRows(ctx, txn, workspace, rows, squared.Headroom, squared.Marked, operation)
}

// reserve adds amount to a row's reservation if the row has the headroom for
// it and is not marked: a grant's reservation on a donor (§4.2, §4.7). It
// reports whether the row took it.
func reserve(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string, shard, amount int64, operation string) (bool, error) {
	n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
		SQL: `UPDATE tr_credit_balance SET reserved = reserved + @x, updated_at = CURRENT_TIMESTAMP()
		       WHERE workspace_id = @w AND shard = @shard AND total_credits - total_usage - reserved >= @x
		         AND NOT COALESCE(in_debt, FALSE)`,
		Params: map[string]any{"x": amount, "w": workspace, "shard": shard},
	}, spanner.QueryOptions{RequestTag: tag(operation)})
	return n == 1, err
}

// adjust changes a row's reservation and usage by the amounts given and
// returns its headroom after. Then, as the design has a settle cover its
// shard, only if that headroom is negative does it square the workspace's
// rows: cover from the others in ascending order, or mark them all. It
// stands for the three money writes that can leave a row negative: a raise
// (reserved up), a matched booking (reserved down and usage up by the same
// charge, headroom unchanged), and a fault booked as usage (§4.7).
func adjust(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string, shard, reservedBy, usageBy int64,
	operation string) (int64, error) {
	iter := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `UPDATE tr_credit_balance
		         SET reserved = reserved + @r, total_usage = total_usage + @u, updated_at = CURRENT_TIMESTAMP()
		       WHERE workspace_id = @w AND shard = @shard AND reserved >= -@r
		      THEN RETURN total_credits - total_usage - reserved`,
		Params: map[string]any{"r": reservedBy, "u": usageBy, "w": workspace, "shard": shard},
	}, spanner.QueryOptions{RequestTag: tag(operation)})
	var headroom int64
	rows := 0
	err := iter.Do(func(row *spanner.Row) error {
		rows++
		return row.Column(0, &headroom)
	})
	if err != nil {
		return 0, err
	}
	if rows != 1 {
		return 0, fmt.Errorf("%w: %s/%d has no row, or less reserved than is taken off it", ErrCreditRowsChanged, workspace, shard)
	}
	if headroom < 0 {
		if _, err := squareCreditRows(ctx, txn, workspace, operation); err != nil {
			return 0, err
		}
	}
	return headroom, nil
}

// release frees amount of a row's reservation, as production's
// release_credit does, never taking more than the row reserves. Then, as
// production's take_inflow does, the freed money repays any negative rows
// first, lowest first, and the rows are squared; what repaid nothing stays
// on the row that freed it (§4.7, Inflow). The spike's own writes leave no
// row negative unmarked, but production has such rows from before the debt
// rules, and the port repairs them as production does.
func release(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string, shard, amount int64, operation string) error {
	if amount < 0 {
		return fmt.Errorf("store: a release of %d", amount)
	}
	n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
		SQL: `UPDATE tr_credit_balance SET reserved = reserved - @x, updated_at = CURRENT_TIMESTAMP()
		       WHERE workspace_id = @w AND shard = @shard AND reserved >= @x`,
		Params: map[string]any{"x": amount, "w": workspace, "shard": shard},
	}, spanner.QueryOptions{RequestTag: tag(operation)})
	if err != nil {
		return err
	}
	if n != 1 {
		return fmt.Errorf("%w: %s/%d reserves less than %d", ErrCreditRowsChanged, workspace, shard, amount)
	}
	rows, err := readCreditRows(ctx, txn, workspace, operation)
	if err != nil {
		return err
	}
	negative := false
	for _, h := range rows.headroom {
		negative = negative || h < 0
	}
	if !rows.marked() && !negative {
		return nil
	}
	if shard < 0 || shard >= int64(len(rows.headroom)) {
		return ErrCreditRowsIncomplete
	}
	without := append([]int64(nil), rows.headroom...)
	without[shard] -= amount
	in, err := creditdebt.TakeInflow(without, amount)
	if err != nil {
		return err
	}
	after := append([]int64(nil), in.Headroom...)
	after[shard] += in.Left
	return writeCreditRows(ctx, txn, workspace, rows, after, in.Marked, operation)
}
