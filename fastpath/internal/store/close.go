package store

import (
	"context"
	"errors"
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
// remains, while the drain log has a row committed after drainRead, the
// timestamp of the member's last read of it, which the member must apply
// first, and while a terminal in the drain log has no stored winner, which
// the member must commit first: so no acknowledged append is left behind,
// read or not (AuditorCommit's Close: nothing uncommitted). The holds must all have
// ended: an applied list named them all, or now, the auditor's clock, is
// past the expiry plus Config.MaxLife plus Config.Grace. The close returns
// the remaining allocation as a commit's return does (applyMoney), from the
// last donor first, each part freed on its shard, repaying debt first, and
// lets the lease's row go, its packs with it, once no pack's work is
// pending. Appends read the state the close writes, so one that races the
// close is refused or lands before it.
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
		if undecided, err := undecidedRows(ctx, txn, ref); err != nil || undecided {
			if undecided {
				out.Refused = RefusedUndecided
			}
			return err
		}
		for _, d := range money.Donors {
			if left := d.Allocation - d.Consumed; left > 0 {
				if out.Released, err = plus(out.Released, left); err != nil {
					return err
				}
			}
		}
		eff, err := applyMoney(money, []MoneyOp{Return(out.Released)})
		if err != nil {
			return err
		}
		for i, d := range eff.After.Donors {
			if d == money.Donors[i] {
				continue
			}
			p := ref.params()
			p["shard"], p["allocation"] = d.Shard, d.Allocation
			n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
				SQL: `UPDATE tr_lease_donor SET allocation = @allocation
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
		for _, r := range eff.Releases {
			if err := release(ctx, txn, ref.Workspace, r.Shard, r.Amount, "close"); err != nil {
				return err
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

// undecidedRows reports whether the drain log has a terminal whose
// authorization has no stored winner, its own or an earlier terminal's.
func undecidedRows(ctx context.Context, txn *spanner.ReadWriteTransaction, ref LeaseRef) (bool, error) {
	packs, err := readPacks(ctx, txn, ref, "close")
	if err != nil {
		return false, err
	}
	won := map[string]bool{}
	for _, p := range packs {
		for _, w := range p.Winners {
			won[w.AuthorizationID] = true
		}
	}
	undecided := false
	err = txn.QueryWithOptions(ctx, spanner.Statement{
		SQL:    `SELECT DISTINCT authorization_id FROM tr_lease_drain WHERE workspace_id = @w AND lease_id = @l`,
		Params: ref.params(),
	}, spanner.QueryOptions{RequestTag: tag("close")}).Do(func(row *spanner.Row) error {
		var a string
		if err := row.Column(0, &a); err != nil {
			return err
		}
		undecided = undecided || !won[a]
		return nil
	})
	return undecided, err
}

// MarkPackDone records that a pack's pending work is done (§4.8, §4.9): its
// records are written and its outcome published. If the lease has closed and
// this was its last pending pack, the lease's row may then go, its packs
// with it. It reads the lease's state, which a close writes, so the two
// settle in either order. It reports whether this call marked it.
//
// Every auditor member sweeps every pack whose work is pending
// (auditor.Pending), so members mark one lease's packs at once: the same
// pack, and a closed lease's different packs, whose retire decision reads
// them all. A statement that reads a row under a shared lock and then
// writes it upgrades the lock, and two transactions that each hold the
// shared lock on what the other writes abort one of them: two markers of
// one pack each read its row, and two markers of a closed lease each read
// the other's pack as pending (2026-11-23). So each marker's transaction
// first takes, exclusively, what it is to decide on: an open lease's
// marker its pack's row (markOpenPack), a closed lease's marker the lease's
// pending packs (markClosedPack). Spanner ages a transaction from its first
// read, and wound-wait aborts only a younger holder an older transaction
// waits for, so a marker that asks for a lock another marker holds is the
// younger and waits. Which of the two a call runs is a strong read of the
// lease's state, outside the transaction and so locking nothing; a lease
// that closes after it is found closed in the transaction, which then
// rolls back for markClosedPack.
func (s *Store) MarkPackDone(ctx context.Context, ref LeaseRef, version int64) (bool, error) {
	row, err := s.client.Single().ReadRowWithOptions(ctx, "tr_lease", ref.key(), []string{"state"},
		&spanner.ReadOptions{RequestTag: tag("mark-pack-done")})
	if err != nil {
		return false, err
	}
	var state string
	if err := row.Column(0, &state); err != nil {
		return false, err
	}
	if state != "closed" {
		if s.markStateRead != nil {
			s.markStateRead()
		}
		marked, err := s.markOpenPack(ctx, ref, version)
		if !errors.Is(err, errRollback) {
			return marked, err
		}
	}
	return s.markClosedPack(ctx, ref, version)
}

// markPackDone marks one pack done, taking its row's lock exclusively as it
// reads the row, so markers of the pack queue rather than each upgrade a
// shared lock the other holds. It has no subquery: the hint takes every
// range a statement reads, exclusively.
const markPackDone = `@{LOCK_SCANNED_RANGES=EXCLUSIVE} UPDATE tr_lease_winners SET work_done_at = CURRENT_TIMESTAMP()
       WHERE workspace_id = @w AND lease_id = @l AND commit_version = @v AND work_done_at IS NULL`

// lockPendingPacks reads a lease's packs whose work is pending, through the
// packs' index on that time, taking their range exclusively: the packs a
// closed lease's retire decision reads, and the lock its markers queue on.
// Only markers of a closed lease take it, and nothing else writes in that
// range once the lease has closed: a commit adds a pack only to a lease
// that is not closed.
const lockPendingPacks = `@{LOCK_SCANNED_RANGES=EXCLUSIVE} SELECT commit_version
       FROM tr_lease_winners@{FORCE_INDEX=tr_lease_winners_by_work}
       WHERE work_done_at IS NULL AND workspace_id = @w AND lease_id = @l`

// retireLease lets a closed lease's row go, once the transaction has found
// no pack's work pending but the one it marks.
const retireLease = `UPDATE tr_lease SET retire_at = CURRENT_TIMESTAMP()
       WHERE workspace_id = @w AND lease_id = @l AND state = 'closed' AND retire_at IS NULL`

// markOpenPack marks a pack of a lease that was not closed when
// MarkPackDone read it: its row first, then the lease's state, which a
// close writes, under the shared lock a close waits for. A lease not closed
// is not retired, so the mark commits. A lease that has closed since rolls
// the mark back, returning errRollback, for markClosedPack to decide on its
// retirement with the lease's other markers.
func (s *Store) markOpenPack(ctx context.Context, ref LeaseRef, version int64) (bool, error) {
	var marked bool
	_, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		marked = false
		params := ref.params()
		params["v"] = version
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{SQL: markPackDone, Params: params},
			spanner.QueryOptions{RequestTag: tag("mark-pack-done")})
		if err != nil || n == 0 {
			return err
		}
		row, err := txn.ReadRowWithOptions(ctx, "tr_lease", ref.key(), []string{"state"},
			&spanner.ReadOptions{RequestTag: tag("mark-pack-done")})
		if err != nil {
			return err
		}
		var state string
		if err := row.Column(0, &state); err != nil {
			return err
		}
		if state == "closed" {
			return errRollback
		}
		marked = true
		return nil
	}, spanner.TransactionOptions{TransactionTag: tag("mark-pack-done")})
	return marked, err
}

// markClosedPack marks a pack of a closed lease: it first takes the lease's
// pending packs exclusively, so its markers decide one at a time, then marks
// the pack if it is pending, and lets the lease's row go if no other is. A
// closed lease stays closed, and the retirement's own guard says so.
func (s *Store) markClosedPack(ctx context.Context, ref LeaseRef, version int64) (bool, error) {
	var marked bool
	_, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		marked = false
		pending, mine := 0, false
		err := txn.QueryWithOptions(ctx, spanner.Statement{SQL: lockPendingPacks, Params: ref.params()},
			spanner.QueryOptions{RequestTag: tag("mark-pack-done")}).Do(func(row *spanner.Row) error {
			var v int64
			if err := row.Column(0, &v); err != nil {
				return err
			}
			pending++
			mine = mine || v == version
			return nil
		})
		if err != nil || !mine {
			return err
		}
		params := ref.params()
		params["v"] = version
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{SQL: markPackDone, Params: params},
			spanner.QueryOptions{RequestTag: tag("mark-pack-done")})
		if err != nil {
			return err
		}
		if n != 1 {
			return fmt.Errorf("store: pack %d of %v changed inside its mark", version, ref)
		}
		marked = true
		if pending > 1 {
			return nil
		}
		_, err = txn.UpdateWithOptions(ctx, spanner.Statement{SQL: retireLease, Params: ref.params()},
			spanner.QueryOptions{RequestTag: tag("mark-pack-done")})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("mark-pack-done")})
	return marked, err
}
