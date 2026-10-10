package store

import (
	"context"
	"errors"
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

// NodeStatus is what stopping a node waits for (the production rollout's
// W8): its row, and the open leases it owns, as one read-only snapshot.
type NodeStatus struct {
	Address string
	// Found says the node has a row; State, Epoch and Live are the row's.
	Found bool
	State string
	Epoch int64
	Live  bool
	// Roles are the row's.
	Roles []string
	// OpenLeases are the open leases whose owner is the node, of any of
	// its epochs.
	OpenLeases int64
	ReadTS     time.Time
}

// Done says whether the node may stop: it has a row, the row says it is
// leaving, and it owns no open lease, so the holds it admitted have ended
// and its leases are draining or closed; and if not, why not. A node with
// no row is never done, so an address mistyped is not taken for one that
// left.
func (n NodeStatus) Done() (bool, []string) {
	var why []string
	switch {
	case !n.Found:
		why = append(why, "it has no row: check the address")
	case n.State != Leaving:
		why = append(why, fmt.Sprintf("it is %s, not leaving", n.State))
	}
	if n.OpenLeases > 0 {
		why = append(why, fmt.Sprintf("it owns %d open leases", n.OpenLeases))
	}
	return len(why) == 0, why
}

// NodeStatus reads a node's row by its address and counts the open leases
// it owns, through the leases' index on their owner, in one read-only
// transaction: a read of the node's row and one of the index's entries
// for it, whatever the fleet holds.
func (s *Store) NodeStatus(ctx context.Context, address string) (NodeStatus, error) {
	if address == "" {
		return NodeStatus{}, errors.New("store: no address")
	}
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	out := NodeStatus{Address: address}
	row, err := ro.ReadRowWithOptions(ctx, "tr_fastpath_member", spanner.Key{address},
		[]string{"state", "epoch", "roles", "heartbeat_at"}, &spanner.ReadOptions{RequestTag: tag("node-status")})
	var heartbeat time.Time
	switch {
	case spanner.ErrCode(err) == codes.NotFound:
	case err != nil:
		return NodeStatus{}, err
	default:
		out.Found = true
		if err := row.Columns(&out.State, &out.Epoch, &out.Roles, &heartbeat); err != nil {
			return NodeStatus{}, err
		}
	}
	err = ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT COUNT(*) FROM tr_lease@{FORCE_INDEX=tr_lease_by_owner}
		       WHERE owner_node = @node AND state = 'open'`,
		Params: map[string]any{"node": address},
	}, spanner.QueryOptions{RequestTag: tag("node-status")}).Do(func(r *spanner.Row) error {
		return r.Column(0, &out.OpenLeases)
	})
	if err != nil {
		return NodeStatus{}, err
	}
	if out.ReadTS, err = readTimestamp(ro); err != nil {
		return NodeStatus{}, err
	}
	out.Live = out.Found && out.ReadTS.Sub(heartbeat) < s.cfg.LiveFor
	return out, nil
}
