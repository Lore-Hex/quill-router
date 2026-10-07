package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// Owner names the process that owns a lease: its node, and the epoch the
// node took when it started (Join).
type Owner struct {
	Node  string
	Epoch int64
}

// Refusal says why the store refused an operation; "" is no refusal.
type Refusal string

// Why a grant is refused (§4.2, §4.7, §4.11).
const (
	RefusedInDebt    Refusal = "the workspace is in debt"
	RefusedPaused    Refusal = "the workspace's billing is paused"
	RefusedTier      Refusal = "the workspace is below the tier that allows leases"
	RefusedAllowance Refusal = "the lease would take the workspace's exposure past its allowance"
	RefusedFloor     Refusal = "the lease would leave less headroom outside leases than the floor"
	RefusedLeaseID   Refusal = "the lease ID is another lease's"
)

// GrantRequest asks for a lease of Amount, L, for a workspace's shard. The
// caller mints LeaseID, so that a grant retried after an unknown outcome
// finds its lease.
type GrantRequest struct {
	Workspace        string
	LeaseID          string
	Region           string
	WorkspaceShard   int64
	Owner            Owner
	Amount           int64
	KeyStatusVersion int64
}

// Donor is a credit shard a lease's allocation was reserved on.
type Donor struct {
	Shard      int64
	Allocation int64
}

// GrantResult is a grant's outcome: refused and why, or the lease's expiry
// and donors. CommitTS is the grant's commit, or for a retry that found its
// lease, the retry's.
type GrantResult struct {
	Refused  Refusal
	Expiry   time.Time
	Donors   []Donor
	CommitTS time.Time
}

// Grant grants a lease in one read-write transaction, never on a request's
// path (§4.2). It reads the workspace's credit rows and its leases under the
// range lock those reads take, so two regions' grants cannot both pass, and
// refuses a workspace that is marked in debt, paused, latched or below
// Config.RequiredTier; a lease that would take the workspace's exposure past
// Config.Allowance; and one that would leave its signed headroom less than
// Config.Floor. Otherwise it reserves L on donor shards in ascending order,
// each up to its headroom, and writes the lease, its donors and an expiry
// Config.Window past Spanner's time. A refused grant writes nothing.
func (s *Store) Grant(ctx context.Context, req GrantRequest) (GrantResult, error) {
	if req.Workspace == "" || req.LeaseID == "" || req.Region == "" || req.Owner.Node == "" || req.Amount <= 0 {
		return GrantResult{}, errors.New("store: a grant needs a workspace, lease ID, region, owner and positive amount")
	}
	var out GrantResult
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = GrantResult{}
		existing, err := readGranted(ctx, txn, req)
		if err != nil || existing != nil {
			if existing != nil {
				out = *existing
			}
			return err
		}
		rows, err := readCreditRows(ctx, txn, req.Workspace, "grant")
		if err != nil {
			return err
		}
		var signed int64
		tier := int64(3)
		for i, h := range rows.headroom {
			signed += h
			t := rows.tiers[i]
			if rows.latched[i] {
				t = 0
			}
			tier = min(tier, t)
		}
		switch {
		case rows.marked():
			out.Refused = RefusedInDebt
		case anyTrue(rows.paused):
			out.Refused = RefusedPaused
		case tier < s.cfg.RequiredTier:
			out.Refused = RefusedTier
		}
		if out.Refused != "" {
			return nil
		}
		exposure, err := readExposure(ctx, txn, req.Workspace)
		if err != nil {
			return err
		}
		switch {
		case exposure+req.Amount > s.cfg.Allowance:
			out.Refused = RefusedAllowance
		case signed-req.Amount < s.cfg.Floor:
			out.Refused = RefusedFloor
		}
		if out.Refused != "" {
			return nil
		}
		// Unmarked, no row is negative, so the signed sum is the positive
		// headroom, and the floor check left at least L of it.
		left := req.Amount
		for shard, h := range rows.headroom {
			if left == 0 {
				break
			}
			if h <= 0 {
				continue
			}
			x := min(h, left)
			ok, err := reserve(ctx, txn, req.Workspace, int64(shard), x, "grant")
			if err != nil {
				return err
			}
			if !ok {
				return fmt.Errorf("%w: donor %s/%d", ErrCreditRowsChanged, req.Workspace, shard)
			}
			out.Donors = append(out.Donors, Donor{Shard: int64(shard), Allocation: x})
			left -= x
		}
		if left != 0 {
			return fmt.Errorf("store: the donors of %s held %d less than the floor check found", req.Workspace, left)
		}
		iter := txn.QueryWithOptions(ctx, spanner.Statement{
			SQL: `INSERT INTO tr_lease (workspace_id, lease_id, region, workspace_shard, owner_node, owner_epoch, state,
			                           granted, allocation, expiry, key_status_version)
			      VALUES (@w, @l, @region, @wshard, @node, @epoch, 'open', @amount, @amount,
			              TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @window MILLISECOND), @ksv)
			      THEN RETURN expiry`,
			Params: map[string]any{"w": req.Workspace, "l": req.LeaseID, "region": req.Region, "wshard": req.WorkspaceShard,
				"node": req.Owner.Node, "epoch": req.Owner.Epoch, "amount": req.Amount,
				"window": s.cfg.Window.Milliseconds(), "ksv": req.KeyStatusVersion},
		}, spanner.QueryOptions{RequestTag: tag("grant")})
		if err := iter.Do(func(row *spanner.Row) error { return row.Column(0, &out.Expiry) }); err != nil {
			if spanner.ErrCode(err) == codes.AlreadyExists {
				// The ID is a lease in another workspace (tr_lease_by_id).
				out = GrantResult{Refused: RefusedLeaseID}
				return errRollback
			}
			return err
		}
		mutations := make([]*spanner.Mutation, 0, len(out.Donors))
		for _, d := range out.Donors {
			mutations = append(mutations, spanner.Insert("tr_lease_donor",
				[]string{"workspace_id", "lease_id", "credit_shard", "allocation"},
				[]any{req.Workspace, req.LeaseID, d.Shard, d.Allocation}))
		}
		return txn.BufferWrite(mutations)
	}, spanner.TransactionOptions{TransactionTag: tag("grant")})
	if errors.Is(err, errRollback) {
		return out, nil
	}
	if err != nil {
		return GrantResult{}, err
	}
	out.CommitTS = resp.CommitTs
	return out, nil
}

// errRollback ends a transaction whose outcome is a refusal found after a
// write, so the write is rolled back.
var errRollback = errors.New("store: rolled back")

// readGranted finds a lease the request's ID already names in its
// workspace: the request's own lease, granted by an earlier attempt, or
// another owner's, which refuses it.
func readGranted(ctx context.Context, txn *spanner.ReadWriteTransaction, req GrantRequest) (*GrantResult, error) {
	row, err := txn.ReadRowWithOptions(ctx, "tr_lease", spanner.Key{req.Workspace, req.LeaseID},
		[]string{"owner_node", "owner_epoch", "granted", "expiry"}, &spanner.ReadOptions{RequestTag: tag("grant")})
	if spanner.ErrCode(err) == codes.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var node string
	var epoch, granted int64
	var out GrantResult
	if err := row.Columns(&node, &epoch, &granted, &out.Expiry); err != nil {
		return nil, err
	}
	if node != req.Owner.Node || epoch != req.Owner.Epoch || granted != req.Amount {
		return &GrantResult{Refused: RefusedLeaseID}, nil
	}
	iter := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT credit_shard, allocation FROM tr_lease_donor
		       WHERE workspace_id = @w AND lease_id = @l ORDER BY credit_shard`,
		Params: map[string]any{"w": req.Workspace, "l": req.LeaseID},
	}, spanner.QueryOptions{RequestTag: tag("grant")})
	err = iter.Do(func(row *spanner.Row) error {
		var d Donor
		if err := row.Columns(&d.Shard, &d.Allocation); err != nil {
			return err
		}
		out.Donors = append(out.Donors, d)
		return nil
	})
	return &out, err
}

// readExposure is the workspace's exposure (§4.2): each open or draining
// lease's remaining allocation, never below zero, summed lease by lease,
// except that a draining lease whose open holds an applied list has named
// counts those holds' estimates. It reads every lease of the workspace,
// not through an index, so the range it locks is the workspace's.
func readExposure(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string) (int64, error) {
	listed := map[string]bool{}
	var exposure int64
	iter := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT lease_id, state, allocation - consumed, holds_listed_seq IS NOT NULL
		        FROM tr_lease WHERE workspace_id = @w`,
		Params: map[string]any{"w": workspace},
	}, spanner.QueryOptions{RequestTag: tag("grant")})
	err := iter.Do(func(row *spanner.Row) error {
		var lease, state string
		var remaining int64
		var holdsListed bool
		if err := row.Columns(&lease, &state, &remaining, &holdsListed); err != nil {
			return err
		}
		switch {
		case state == "draining" && holdsListed:
			listed[lease] = true
		case state == "open" || state == "draining":
			exposure += max(remaining, 0)
		}
		return nil
	})
	if err != nil || len(listed) == 0 {
		return exposure, err
	}
	iter = txn.QueryWithOptions(ctx, spanner.Statement{
		SQL:    `SELECT lease_id, estimate FROM tr_lease_hold WHERE workspace_id = @w`,
		Params: map[string]any{"w": workspace},
	}, spanner.QueryOptions{RequestTag: tag("grant")})
	err = iter.Do(func(row *spanner.Row) error {
		var lease string
		var estimate int64
		if err := row.Columns(&lease, &estimate); err != nil {
			return err
		}
		if listed[lease] {
			exposure += estimate
		}
		return nil
	})
	return exposure, err
}

func anyTrue(values []bool) bool {
	for _, v := range values {
		if v {
			return true
		}
	}
	return false
}
