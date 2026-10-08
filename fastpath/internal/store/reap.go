package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// Why a reap or a close is refused, beyond the version and the gap.
const (
	RefusedNotDraining Refusal = "the lease is not draining"
	RefusedNoBoundary  Refusal = "the lease's boundary S is not stored"
	RefusedTerminal    Refusal = "the authorization has a terminal in the drain log"
	RefusedNoHold      Refusal = "the authorization has no open hold"
	RefusedNotDue      Refusal = "the hold's deadline plus the grace has not passed"
	RefusedUndecided   Refusal = "a terminal in the drain log has no stored winner"
	RefusedHoldsOpen   Refusal = "the lease has open holds"
	RefusedRowsBeyond  Refusal = "the drain log has rows past the member's read"
	RefusedTooSoon     Refusal = "the lease's holds may not all have ended"
)

// ReapRow is the auditor's reap of an open hold of a draining lease (§4.8):
// the hold's latest snapshot's running charge, capped at its estimate, the
// heartbeat record the charge came from, and the full record the auditor
// built and published for it (Digest, Money).
type ReapRow struct {
	AuthorizationID  string
	RecordID         string
	Charge           int64
	Estimate         int64
	SnapshotOwnerSeq int64
	Digest           []byte
	Money            []byte
}

// Reap appends a reap to a draining lease's drain log (§4.8), conditional on
// the version the member read, no gap, the lease draining with S stored, no
// terminal for the authorization in the drain log already, which then wins
// and leaves nothing to reap, and its hold stored and open: a stored winner
// takes its hold with it, and an authorization the lease never held has
// none. It is taken only at a tick past the hold's deadline plus
// Config.Grace, tick being the time of the tick the member applied, so no
// settle the hold may still have can lose to it. The reap is the hold's: its
// estimate, and its latest snapshot's running charge capped at the
// estimate, from that snapshot's owner record, or nothing for a hold with no
// snapshot; another is an error. It does not advance the version: the
// commit that applies the row books it. It returns the refusal, if any, and
// the row's commit.
func (s *Store) Reap(ctx context.Context, ref LeaseRef, readVersion int64, tick time.Time, r ReapRow) (Refusal, time.Time, error) {
	if r.AuthorizationID == "" || r.RecordID == "" || r.Charge < 0 || r.Charge > r.Estimate || len(r.Money) == 0 ||
		len(r.Digest) == 0 {
		return "", time.Time{}, errors.New("store: a reap names its authorization, its record and the record's digest, " +
			"and charges at most the hold")
	}
	var refused Refusal
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		refused = ""
		row, err := txn.ReadRowWithOptions(ctx, "tr_lease", ref.key(), []string{"commit_version", "state", "gap_seq", "boundary_seq"},
			&spanner.ReadOptions{RequestTag: tag("reap")})
		if spanner.ErrCode(err) == codes.NotFound {
			return fmt.Errorf("%w: %v", ErrNoLease, ref)
		}
		if err != nil {
			return err
		}
		var version int64
		var state string
		var gap, boundary spanner.NullInt64
		if err := row.Columns(&version, &state, &gap, &boundary); err != nil {
			return err
		}
		switch {
		case version != readVersion:
			refused = RefusedVersion
		case gap.Valid:
			refused = RefusedGap
		case state != "draining":
			refused = RefusedNotDraining
		case !boundary.Valid:
			refused = RefusedNoBoundary
		}
		if refused != "" {
			return nil
		}
		// A's rows in the drain log: reading their range takes the lock an
		// append for A would need, so a front door's terminal and this reap
		// cannot both land.
		params := ref.params()
		params["a"] = r.AuthorizationID
		found := false
		err = txn.QueryWithOptions(ctx, spanner.Statement{
			SQL:    `SELECT record_id FROM tr_lease_drain WHERE workspace_id = @w AND lease_id = @l AND authorization_id = @a LIMIT 1`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("reap")}).Do(func(*spanner.Row) error {
			found = true
			return nil
		})
		if err != nil {
			return err
		}
		if found {
			refused = RefusedTerminal
			return nil
		}
		row, err = txn.ReadRowWithOptions(ctx, "tr_lease_hold", spanner.Key{ref.Workspace, ref.LeaseID, r.AuthorizationID},
			[]string{"estimate", "deadline", "running_charge", "snapshot_owner_seq"}, &spanner.ReadOptions{RequestTag: tag("reap")})
		if spanner.ErrCode(err) == codes.NotFound {
			refused = RefusedNoHold
			return nil
		}
		if err != nil {
			return err
		}
		var estimate int64
		var deadline time.Time
		var running, seq spanner.NullInt64
		if err := row.Columns(&estimate, &deadline, &running, &seq); err != nil {
			return err
		}
		if tick.Before(deadline.Add(s.cfg.Grace)) {
			refused = RefusedNotDue
			return nil
		}
		if want := min(running.Int64, estimate); r.Estimate != estimate || r.Charge != want || r.SnapshotOwnerSeq != seq.Int64 {
			return fmt.Errorf("store: a reap of %s at %d of %d from record %d, and the hold is %d of %d from record %d",
				r.AuthorizationID, r.Charge, r.Estimate, r.SnapshotOwnerSeq, want, estimate, seq.Int64)
		}
		params["r"], params["charge"], params["estimate"], params["seq"] = r.RecordID, r.Charge, r.Estimate, r.SnapshotOwnerSeq
		params["digest"], params["money"] = r.Digest, r.Money
		_, err = txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `INSERT INTO tr_lease_drain (workspace_id, lease_id, authorization_id, record_id, kind, charge, estimate,
			                                 record_digest, money, snapshot_owner_seq, cause, commit_ts)
			      VALUES (@w, @l, @a, @r, 'reap', @charge, @estimate, @digest, @money, @seq, 'reap', PENDING_COMMIT_TIMESTAMP())`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("reap")})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("reap")})
	if err != nil {
		return "", time.Time{}, err
	}
	if refused != "" {
		return refused, time.Time{}, nil
	}
	return "", resp.CommitTs.UTC(), nil
}
