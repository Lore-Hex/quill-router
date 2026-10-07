package store

import (
	"context"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
)

// CloseResult is a close's outcome: refused and why, or the lease's new
// version and the remaining allocation the close released.
type CloseResult struct {
	Refused    Refusal
	NewVersion int64
	Released   int64
	CommitTS   time.Time
}

// CloseLease closes a draining lease (§4.5, §4.8): conditional on the
// version the member read, no gap, and S stored; refused while any hold row
// remains, and while the drain log has a row committed after drainRead, the
// timestamp of the member's last read of it, which the member must apply
// first: so no acknowledged append is left behind. The holds must all have
// ended: an applied list named them all, or now, the auditor's clock, is
// past the expiry plus Config.MaxLife plus Config.Grace. The close releases
// each donor's remaining allocation as a return, repaying debt first, sets
// the packs whose work is done to be deleted, and lets the lease's row go
// once no pack's work is pending. Appends read the state the close writes,
// so one that races the close is refused or lands before it.
func (s *Store) CloseLease(ctx context.Context, ref LeaseRef, readVersion int64, drainRead, now time.Time) (CloseResult, error) {
	var out CloseResult
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = CloseResult{}
		l, money, err := readLeaseMoney(ctx, txn, ref, "close")
		if err != nil {
			return err
		}
		switch {
		case l.CommitVersion != readVersion:
			out.Refused = RefusedVersion
		case l.GapSeq.Valid:
			out.Refused = RefusedGap
		case l.State != "draining":
			out.Refused = RefusedNotDraining
		case !l.BoundarySeq.Valid:
			out.Refused = RefusedNoBoundary
		case !l.HoldsListedSeq.Valid && now.Before(l.Expiry.Add(s.cfg.MaxLife+s.cfg.Grace)):
			out.Refused = RefusedTooSoon
		}
		if out.Refused != "" {
			return nil
		}
		params := ref.params()
		params["read"] = drainRead
		var holds, beyond, pending int64
		err = txn.QueryWithOptions(ctx, spanner.Statement{
			SQL: `SELECT (SELECT COUNT(*) FROM tr_lease_hold WHERE workspace_id = @w AND lease_id = @l),
			             (SELECT COUNT(*) FROM tr_lease_drain WHERE workspace_id = @w AND lease_id = @l AND commit_ts > @read),
			             (SELECT COUNT(*) FROM tr_lease_winners WHERE workspace_id = @w AND lease_id = @l AND work_done_at IS NULL)`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("close")}).Do(func(row *spanner.Row) error {
			return row.Columns(&holds, &beyond, &pending)
		})
		if err != nil {
			return err
		}
		switch {
		case holds > 0:
			out.Refused = RefusedHoldsOpen
		case beyond > 0:
			out.Refused = RefusedRowsBeyond
		}
		if out.Refused != "" {
			return nil
		}
		for _, d := range money.Donors {
			left := d.Allocation - d.Consumed
			if left <= 0 {
				continue
			}
			out.Released += left
			p := ref.params()
			p["shard"], p["consumed"] = d.Shard, d.Consumed
			n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
				SQL: `UPDATE tr_lease_donor SET allocation = @consumed
				       WHERE workspace_id = @w AND lease_id = @l AND credit_shard = @shard`,
				Params: p,
			}, spanner.QueryOptions{RequestTag: tag("close")})
			if err != nil {
				return err
			}
			if n != 1 {
				return fmt.Errorf("store: donor %d of %v changed inside its close", d.Shard, ref)
			}
		}
		params["released"], params["retire"] = out.Released, pending == 0
		rows := 0
		err = txn.QueryWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease
			         SET state = 'closed', closed_at = CURRENT_TIMESTAMP(), close_kind = 'auditor',
			             commit_version = commit_version + 1,
			             allocation = allocation - @released, returned = returned + @released,
			             retire_at = IF(@retire, CURRENT_TIMESTAMP(), NULL)
			       WHERE workspace_id = @w AND lease_id = @l AND commit_version = @read AND state = 'draining'
			         AND gap_seq IS NULL
			      THEN RETURN commit_version`,
			Params: map[string]any{"w": ref.Workspace, "l": ref.LeaseID, "read": readVersion, "released": out.Released,
				"retire": pending == 0},
		}, spanner.QueryOptions{RequestTag: tag("close")}).Do(func(row *spanner.Row) error {
			rows++
			return row.Column(0, &out.NewVersion)
		})
		if err != nil {
			return err
		}
		if rows != 1 {
			return fmt.Errorf("store: lease %v changed inside its close", ref)
		}
		if _, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease_winners SET deletable_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND lease_id = @l AND work_done_at IS NOT NULL AND deletable_at IS NULL`,
			Params: ref.params(),
		}, spanner.QueryOptions{RequestTag: tag("close")}); err != nil {
			return err
		}
		for _, d := range money.Donors {
			if left := d.Allocation - d.Consumed; left > 0 {
				if err := release(ctx, txn, ref.Workspace, d.Shard, left, "close"); err != nil {
					return err
				}
			}
		}
		return nil
	}, spanner.TransactionOptions{TransactionTag: tag("close")})
	if err != nil {
		return CloseResult{}, err
	}
	if out.Refused == "" {
		out.CommitTS = resp.CommitTs.UTC()
	}
	return out, nil
}

// MarkPackDone records that a pack's pending work is done (§4.8, §4.9): its
// records are written and its outcome published. If the lease has closed,
// the pack may then be deleted, and the lease's row may go once this was its
// last pending pack. It reads the lease's state, which a close writes, so
// the two settle in either order. It reports whether this call marked it.
func (s *Store) MarkPackDone(ctx context.Context, ref LeaseRef, version int64) (bool, error) {
	var marked bool
	_, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		marked = false
		row, err := txn.ReadRowWithOptions(ctx, "tr_lease", ref.key(), []string{"state"}, &spanner.ReadOptions{RequestTag: tag("mark-pack-done")})
		if err != nil {
			return err
		}
		var state string
		if err := row.Column(0, &state); err != nil {
			return err
		}
		params := ref.params()
		params["v"], params["closed"] = version, state == "closed"
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease_winners
			         SET work_done_at = CURRENT_TIMESTAMP(), deletable_at = IF(@closed, CURRENT_TIMESTAMP(), NULL)
			       WHERE workspace_id = @w AND lease_id = @l AND commit_version = @v AND work_done_at IS NULL`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("mark-pack-done")})
		if err != nil || n == 0 {
			return err
		}
		marked = true
		if state != "closed" {
			return nil
		}
		_, err = txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease SET retire_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND lease_id = @l AND retire_at IS NULL
			         AND NOT EXISTS (SELECT 1 FROM tr_lease_winners
			                          WHERE workspace_id = @w AND lease_id = @l AND work_done_at IS NULL AND commit_version != @v)`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("mark-pack-done")})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("mark-pack-done")})
	return marked, err
}
