package store

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// DrainTerminal is a terminal a front door appends to a lease's drain log
// (§4.5): a settle or a refund the owner does not take. RecordID is minted
// by the front door and reused on that append's retries. Money is the
// record's money fields, and Digest its full record's digest; Cause names
// the event that caused the append, for the spike's traces.
type DrainTerminal struct {
	Ref             LeaseRef
	AuthorizationID string
	RecordID        string
	Kind            string
	Charge          int64
	Estimate        int64
	Digest          []byte
	Money           []byte
	Cause           string
}

// AppendResult is an append's outcome: the raise it made on the lease, and
// its commit; or for a retry whose row is there, that row's raise and
// commit. Refused is a closed lease, which writes nothing; the
// authorization's answer then comes from its disposition.
type AppendResult struct {
	Raise    int64
	CommitTS time.Time
	Refused  Refusal
}

// Append appends a terminal to a lease's drain log in its own transaction,
// conditional on the lease not being closed (§4.5). A charge above the
// hold's estimate raises the lease's allocation, its first donor's and that
// shard's reservation by the difference, in the same transaction as the row
// and under the same condition (§4.2), so no raise exists without its row
// and none lands on a closed lease. The row is written last, with its
// commit timestamp, and read in the order of that timestamp, then record ID.
// An append retried with its record ID finds its row and writes nothing.
func (s *Store) Append(ctx context.Context, t DrainTerminal) (AppendResult, error) {
	if t.AuthorizationID == "" || t.RecordID == "" || t.Cause == "" || (t.Kind != "settle" && t.Kind != "refund") ||
		t.Charge < 0 || t.Estimate < 0 || len(t.Money) == 0 {
		return AppendResult{}, errors.New("store: an append needs an authorization, a record ID, a settle or refund, " +
			"charges that are not negative, money fields and a cause")
	}
	var out AppendResult
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = AppendResult{}
		row, err := txn.ReadRowWithOptions(ctx, "tr_lease_drain",
			spanner.Key{t.Ref.Workspace, t.Ref.LeaseID, t.AuthorizationID, t.RecordID},
			[]string{"kind", "charge", "estimate", "record_digest", "door_raise", "commit_ts"},
			&spanner.ReadOptions{RequestTag: tag("append")})
		switch {
		case err == nil:
			var kind string
			var charge, estimate int64
			var digest []byte
			if err := row.Columns(&kind, &charge, &estimate, &digest, &out.Raise, &out.CommitTS); err != nil {
				return err
			}
			if kind != t.Kind || charge != t.Charge || estimate != t.Estimate || !bytes.Equal(digest, t.Digest) {
				return fmt.Errorf("store: record %s is another terminal's", t.RecordID)
			}
			return nil
		case spanner.ErrCode(err) != codes.NotFound:
			return err
		}
		row, err = txn.ReadRowWithOptions(ctx, "tr_lease", t.Ref.key(), []string{"state"},
			&spanner.ReadOptions{RequestTag: tag("append")})
		if spanner.ErrCode(err) == codes.NotFound {
			out.Refused = RefusedClosed
			return nil
		}
		if err != nil {
			return err
		}
		var state string
		if err := row.Column(0, &state); err != nil {
			return err
		}
		if state == "closed" {
			out.Refused = RefusedClosed
			return nil
		}
		if t.Charge > t.Estimate {
			out.Raise = t.Charge - t.Estimate
			params := t.Ref.params()
			params["raise"] = out.Raise
			n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
				SQL: `UPDATE tr_lease SET allocation = allocation + @raise, door_raised = door_raised + @raise
				       WHERE workspace_id = @w AND lease_id = @l AND state != 'closed'`,
				Params: params,
			}, spanner.QueryOptions{RequestTag: tag("append")})
			if err != nil {
				return err
			}
			if n != 1 {
				return errors.New("store: the lease changed inside its own transaction")
			}
			if err := raiseFirstDonor(ctx, txn, t.Ref, out.Raise, "append"); err != nil {
				return err
			}
		}
		// The row last: a statement that writes a commit timestamp bars
		// later statements on its table.
		params := t.Ref.params()
		params["a"], params["r"], params["kind"], params["charge"], params["estimate"] = t.AuthorizationID, t.RecordID, t.Kind, t.Charge, t.Estimate
		params["raise"], params["digest"], params["money"], params["cause"] = out.Raise, t.Digest, t.Money, t.Cause
		_, err = txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `INSERT INTO tr_lease_drain (workspace_id, lease_id, authorization_id, record_id, kind, charge, estimate,
			                                 door_raise, record_digest, money, cause, commit_ts)
			      VALUES (@w, @l, @a, @r, @kind, @charge, @estimate, @raise, @digest, @money, @cause, PENDING_COMMIT_TIMESTAMP())`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("append")})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("append")})
	if err != nil {
		return AppendResult{}, err
	}
	if out.Refused == "" && out.CommitTS.IsZero() {
		out.CommitTS = resp.CommitTs.UTC()
	}
	return out, nil
}

// DrainRow is a row of a lease's drain log.
type DrainRow struct {
	AuthorizationID  string
	RecordID         string
	Kind             string
	Charge           int64
	Estimate         int64
	DoorRaise        int64
	Digest           []byte
	Money            []byte
	SnapshotOwnerSeq spanner.NullInt64
	Cause            string
	CommitTS         time.Time
}

const drainColumns = `authorization_id, record_id, kind, charge, estimate, door_raise, record_digest, money,
	snapshot_owner_seq, cause, commit_ts`

func scanDrainRows(iter *spanner.RowIterator) ([]DrainRow, error) {
	var rows []DrainRow
	err := iter.Do(func(row *spanner.Row) error {
		var d DrainRow
		if err := row.Columns(&d.AuthorizationID, &d.RecordID, &d.Kind, &d.Charge, &d.Estimate, &d.DoorRaise,
			&d.Digest, &d.Money, &d.SnapshotOwnerSeq, &d.Cause, &d.CommitTS); err != nil {
			return err
		}
		rows = append(rows, d)
		return nil
	})
	return rows, err
}

// ReadDrainSince reads a lease's drain log past a cursor, in the log's
// order, commit timestamp then record ID (§4.5), in one strong read. The
// cursor is the timestamp of the read before: every row committed at or
// before it was visible to that read, and a later commit has a later
// timestamp, so rows that share a timestamp come together and none is
// skipped, and an empty read still moves the cursor on. It returns the rows
// and this read's timestamp, the next cursor; the zero time reads the log
// from its start. Owners adopt from it, and the auditor reads a draining
// lease's terminals from it.
func (s *Store) ReadDrainSince(ctx context.Context, ref LeaseRef, cursor time.Time) ([]DrainRow, time.Time, error) {
	params := ref.params()
	params["cursor"] = cursor
	return s.readDrain(ctx, "read-drain-since", spanner.Statement{
		SQL: `SELECT ` + drainColumns + ` FROM tr_lease_drain@{FORCE_INDEX=tr_lease_drain_by_commit}
		       WHERE workspace_id = @w AND lease_id = @l AND commit_ts > @cursor ORDER BY commit_ts, record_id`,
		Params: params,
	})
}

// ReadHoldDrainRows reads one authorization's rows in a lease's drain log,
// in the log's order, in one strong read: the first is the one a terminal
// for it there wins with. The reaper reads it before it reaps the hold, as
// the backstop (§4.5).
func (s *Store) ReadHoldDrainRows(ctx context.Context, ref LeaseRef, authorization string) ([]DrainRow, time.Time, error) {
	params := ref.params()
	params["a"] = authorization
	return s.readDrain(ctx, "read-hold-drain-rows", spanner.Statement{
		SQL: `SELECT ` + drainColumns + ` FROM tr_lease_drain
		       WHERE workspace_id = @w AND lease_id = @l AND authorization_id = @a ORDER BY commit_ts, record_id`,
		Params: params,
	})
}

func (s *Store) readDrain(ctx context.Context, operation string, statement spanner.Statement) ([]DrainRow, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	rows, err := scanDrainRows(ro.QueryWithOptions(ctx, statement, spanner.QueryOptions{RequestTag: tag(operation)}))
	if err != nil {
		return nil, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	return rows, read, err
}
