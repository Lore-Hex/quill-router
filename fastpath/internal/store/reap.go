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
// the version the member read, no gap, the lease draining with S stored,
// and no terminal for the authorization in the drain log already, which
// then wins and leaves nothing to reap. Its charge is at most the hold's
// estimate. It does not advance the version: the commit that applies the
// row books it. It returns the refusal, if any, and the row's commit.
func (s *Store) Reap(ctx context.Context, ref LeaseRef, readVersion int64, r ReapRow) (Refusal, time.Time, error) {
	if r.AuthorizationID == "" || r.RecordID == "" || r.Charge < 0 || r.Charge > r.Estimate || len(r.Money) == 0 {
		return "", time.Time{}, errors.New("store: a reap names its authorization and record, and charges at most the hold")
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
