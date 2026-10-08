package store

import (
	"context"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// A member's states (spike plan, §4). A serving member takes new leases. A
// leaving member keeps the leases it has until they drain and takes no new
// ones, until it starts again. A withdrawn front door takes no new requests
// until it reaches owners again, and then serves.
const (
	Serving   = "serving"
	Leaving   = "leaving"
	Withdrawn = "withdrawn"
)

// Member is a node's row in tr_fastpath_member.
type Member struct {
	Address     string
	Epoch       int64
	Roles       []string
	State       string
	StartedAt   time.Time
	HeartbeatAt time.Time
	// Live is whether the heartbeat was younger than Config.LiveFor at the
	// read's timestamp.
	Live bool
}

// Join starts a node: it writes the node's row, serving, with the node's
// next epoch, one more than the row's last or 1 for a new row, and Spanner's
// commit time as both its start and its heartbeat. The node's leases carry
// the epoch as their owner_epoch, so a node that starts again owns nothing it
// owned before (LeaseLifecycle's Restart). It returns the epoch and the
// commit timestamp.
func (s *Store) Join(ctx context.Context, address string, roles []string) (int64, time.Time, error) {
	if address == "" || len(roles) == 0 {
		return 0, time.Time{}, fmt.Errorf("store: a member needs an address and roles")
	}
	var epoch int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		row, err := txn.ReadRowWithOptions(ctx, "tr_fastpath_member", spanner.Key{address}, []string{"epoch"},
			&spanner.ReadOptions{RequestTag: tag("join")})
		switch {
		case spanner.ErrCode(err) == codes.NotFound:
			epoch = 1
		case err != nil:
			return err
		default:
			var last int64
			if err := row.Column(0, &last); err != nil {
				return err
			}
			epoch = last + 1
		}
		return txn.BufferWrite([]*spanner.Mutation{spanner.InsertOrUpdate("tr_fastpath_member",
			[]string{"address", "epoch", "roles", "state", "started_at", "heartbeat_at"},
			[]any{address, epoch, roles, Serving, spanner.CommitTimestamp, spanner.CommitTimestamp})})
	}, spanner.TransactionOptions{TransactionTag: tag("join")})
	if err != nil {
		return 0, time.Time{}, err
	}
	return epoch, resp.CommitTs.UTC(), nil
}

// Heartbeat writes a node's heartbeat at Spanner's commit time, and its
// state, if the row still has the node's epoch: once the node has started
// again, the old process's heartbeats are refused. A leaving node stays
// leaving: a heartbeat that would have it serve or withdraw is refused too.
// It reports whether the row took the heartbeat, and the commit timestamp.
func (s *Store) Heartbeat(ctx context.Context, address string, epoch int64, state string) (bool, time.Time, error) {
	if state != Serving && state != Leaving && state != Withdrawn {
		return false, time.Time{}, fmt.Errorf("store: no member state %q", state)
	}
	var written int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		var err error
		written, err = txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_fastpath_member
			         SET state = @state, heartbeat_at = PENDING_COMMIT_TIMESTAMP()
			       WHERE address = @address AND epoch = @epoch
			         AND (state != 'leaving' OR @state = 'leaving')`,
			Params: map[string]any{"address": address, "epoch": epoch, "state": state},
		}, spanner.QueryOptions{RequestTag: tag("heartbeat")})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("heartbeat")})
	if err != nil {
		return false, time.Time{}, err
	}
	return written == 1, resp.CommitTs.UTC(), nil
}

// Members reads every member's row in one strong read, and marks each live
// whose heartbeat is younger than Config.LiveFor at the read's timestamp, so
// both times are Spanner's. It returns the read timestamp too.
func (s *Store) Members(ctx context.Context) ([]Member, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	iter := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT address, epoch, roles, state, started_at, heartbeat_at
		        FROM tr_fastpath_member ORDER BY address`,
	}, spanner.QueryOptions{RequestTag: tag("members")})
	var members []Member
	err := iter.Do(func(row *spanner.Row) error {
		var m Member
		if err := row.Columns(&m.Address, &m.Epoch, &m.Roles, &m.State, &m.StartedAt, &m.HeartbeatAt); err != nil {
			return err
		}
		members = append(members, m)
		return nil
	})
	if err != nil {
		return nil, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	if err != nil {
		return nil, time.Time{}, err
	}
	for i := range members {
		members[i].Live = read.Sub(members[i].HeartbeatAt) < s.cfg.LiveFor
	}
	return members, read, nil
}
