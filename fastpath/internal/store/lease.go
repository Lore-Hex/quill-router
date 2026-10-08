package store

import (
	"context"
	"errors"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// LeaseRef names a lease: its workspace and its ID.
type LeaseRef struct {
	Workspace string
	LeaseID   string
}

func (r LeaseRef) key() spanner.Key { return spanner.Key{r.Workspace, r.LeaseID} }

func (r LeaseRef) params() map[string]any {
	return map[string]any{"w": r.Workspace, "l": r.LeaseID}
}

// Why a lease's own writes are refused.
const (
	RefusedClosed Refusal = "the lease is closed"
	RefusedOwner  Refusal = "the lease is not this process's"
)

// Lease is a lease's row as the store reads it. Columns that may be NULL are
// spanner's Null types.
type Lease struct {
	Ref                 LeaseRef
	Region              string
	WorkspaceShard      int64
	Owner               Owner
	State               string
	Granted             int64
	Allocation          int64
	Consumed            int64
	ShortfallTotal      int64
	DoorRaised          int64
	Returned            int64
	FaultUsage          int64
	Expiry              time.Time
	Revoked             bool
	KeyStatusVersion    int64
	CommitVersion       int64
	AppliedSeq          int64
	LastTick            int64
	AuditOsum           int64
	HoldsListedSeq      spanner.NullInt64
	AuditFaultSeq       spanner.NullInt64
	GapSeq              spanner.NullInt64
	FenceTime           spanner.NullTime
	BoundarySeq         spanner.NullInt64
	BoundaryPublishTime spanner.NullTime
	DrainedBy           spanner.NullString
	ClosedAt            spanner.NullTime
	CloseKind           spanner.NullString
}

var leaseColumns = []string{"region", "workspace_shard", "owner_node", "owner_epoch", "state", "granted", "allocation",
	"consumed", "shortfall_total", "door_raised", "returned", "fault_usage", "expiry", "revoked", "key_status_version",
	"commit_version", "applied_seq", "last_tick", "audit_osum", "holds_listed_seq", "audit_fault_seq", "gap_seq",
	"fence_time", "boundary_seq", "boundary_publish_time", "drained_by", "closed_at", "close_kind"}

func (l *Lease) scan(row *spanner.Row) error {
	return row.Columns(&l.Region, &l.WorkspaceShard, &l.Owner.Node, &l.Owner.Epoch, &l.State, &l.Granted,
		&l.Allocation, &l.Consumed, &l.ShortfallTotal, &l.DoorRaised, &l.Returned, &l.FaultUsage, &l.Expiry,
		&l.Revoked, &l.KeyStatusVersion, &l.CommitVersion, &l.AppliedSeq, &l.LastTick, &l.AuditOsum,
		&l.HoldsListedSeq, &l.AuditFaultSeq, &l.GapSeq, &l.FenceTime, &l.BoundarySeq, &l.BoundaryPublishTime,
		&l.DrainedBy, &l.ClosedAt, &l.CloseKind)
}

// ErrNoLease means the lease does not exist: never granted, or deleted after
// it closed.
var ErrNoLease = errors.New("store: no such lease")

// ReadLease reads a lease's row in a strong read, and returns the read's
// timestamp: the owner's re-read of a lease a renewal did not take, and a
// front door's look at a lease it may append to.
func (s *Store) ReadLease(ctx context.Context, ref LeaseRef) (Lease, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	row, err := ro.ReadRowWithOptions(ctx, "tr_lease", ref.key(), leaseColumns, &spanner.ReadOptions{RequestTag: tag("read-lease")})
	if spanner.ErrCode(err) == codes.NotFound {
		return Lease{}, time.Time{}, ErrNoLease
	}
	if err != nil {
		return Lease{}, time.Time{}, err
	}
	l := Lease{Ref: ref}
	if err := l.scan(row); err != nil {
		return Lease{}, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	return l, read, err
}

// FindLease finds a lease's row key from its ID alone (tr_lease_by_id): the
// settle log keys a lease's records by its ID, and the auditor loads the
// lease by its workspace and ID. A lease ID names one lease, and its
// workspace never changes.
func (s *Store) FindLease(ctx context.Context, leaseID string) (LeaseRef, error) {
	ro := s.client.Single()
	defer ro.Close()
	ref, found := LeaseRef{LeaseID: leaseID}, false
	err := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL:    `SELECT workspace_id FROM tr_lease@{FORCE_INDEX=tr_lease_by_id} WHERE lease_id = @l`,
		Params: map[string]any{"l": leaseID},
	}, spanner.QueryOptions{RequestTag: tag("find-lease")}).Do(func(row *spanner.Row) error {
		found = true
		return row.Columns(&ref.Workspace)
	})
	switch {
	case err != nil:
		return LeaseRef{}, err
	case !found:
		return LeaseRef{}, ErrNoLease
	}
	return ref, nil
}

// RenewResult is one lease's renewal: whether the lease took it, and the
// expiry Spanner holds after it.
type RenewResult struct {
	Ref     LeaseRef
	Renewed bool
	Expiry  time.Time
}

// Renew extends the expiries of an owner's leases, in one transaction with a
// conditional statement for each (§4.2): a lease takes its renewal only while
// it is open, not revoked and the owner's, epoch and all. Its expiry becomes
// Config.Window past Spanner's time, never earlier than it was, so a renewal
// applied twice, or after a later one, extends nothing, and a renewal changes
// nothing else. A lease that takes none is draining, closed, revoked or
// another process's; its owner re-reads it and stops using it. The expiries
// returned are Spanner's, read in the transaction, for the owner to take
// rather than its own clock. It returns the commit timestamp too.
func (s *Store) Renew(ctx context.Context, owner Owner, refs []LeaseRef) ([]RenewResult, time.Time, error) {
	if len(refs) == 0 {
		return nil, time.Time{}, nil
	}
	var out []RenewResult
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = make([]RenewResult, len(refs))
		statements := make([]spanner.Statement, len(refs))
		for i, ref := range refs {
			params := ref.params()
			params["node"], params["epoch"], params["window"] = owner.Node, owner.Epoch, s.cfg.Window.Microseconds()
			statements[i] = spanner.Statement{
				SQL: `UPDATE tr_lease
				         SET expiry = GREATEST(expiry, TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @window MICROSECOND))
				       WHERE workspace_id = @w AND lease_id = @l AND state = 'open' AND NOT revoked
				         AND owner_node = @node AND owner_epoch = @epoch`,
				Params: params,
			}
		}
		counts, err := txn.BatchUpdateWithOptions(ctx, statements, spanner.QueryOptions{RequestTag: tag("renew")})
		if err != nil {
			return err
		}
		keys := spanner.KeySets()
		for i, ref := range refs {
			out[i] = RenewResult{Ref: ref, Renewed: counts[i] == 1}
			if out[i].Renewed {
				keys = spanner.KeySets(keys, ref.key())
			}
		}
		expiries := map[LeaseRef]time.Time{}
		err = txn.ReadWithOptions(ctx, "tr_lease", keys, []string{"workspace_id", "lease_id", "expiry"},
			&spanner.ReadOptions{RequestTag: tag("renew")}).Do(func(row *spanner.Row) error {
			var ref LeaseRef
			var expiry time.Time
			if err := row.Columns(&ref.Workspace, &ref.LeaseID, &expiry); err != nil {
				return err
			}
			expiries[ref] = expiry
			return nil
		})
		if err != nil {
			return err
		}
		for i := range out {
			if out[i].Renewed {
				out[i].Expiry = expiries[out[i].Ref]
			}
		}
		return nil
	}, spanner.TransactionOptions{TransactionTag: tag("renew")})
	if err != nil {
		return nil, time.Time{}, err
	}
	return out, resp.CommitTs.UTC(), nil
}

// Revoke stops a lease's renewals (§4.3): the lease runs to its expiry, and
// the auditor then drains it. It reports whether this call revoked it, not
// when the lease was revoked already, is closed, or does not exist, and the
// commit timestamp.
func (s *Store) Revoke(ctx context.Context, ref LeaseRef) (bool, time.Time, error) {
	return s.conditional(ctx, "revoke", spanner.Statement{
		SQL: `UPDATE tr_lease SET revoked = TRUE, revoked_at = CURRENT_TIMESTAMP()
		       WHERE workspace_id = @w AND lease_id = @l AND NOT revoked AND state != 'closed'`,
		Params: ref.params(),
	})
}

// OwnerMarkDraining is the owner's draining write (§4.8), at its final
// checkpoint or a forced exit: it marks its open lease draining and stores
// the fence F, the last renewed expiry plus Config.Skew plus
// Config.PublishDeadline. It reports whether the lease took it, not when the
// lease is no longer open or is another process's, and the commit timestamp.
func (s *Store) OwnerMarkDraining(ctx context.Context, owner Owner, ref LeaseRef) (bool, time.Time, error) {
	params := ref.params()
	params["node"], params["epoch"], params["fence"] = owner.Node, owner.Epoch, s.fence()
	return s.conditional(ctx, "owner-mark-draining", spanner.Statement{
		SQL: `UPDATE tr_lease
		         SET state = 'draining', drained_by = 'owner', fence_time = TIMESTAMP_ADD(expiry, INTERVAL @fence MICROSECOND)
		       WHERE workspace_id = @w AND lease_id = @l AND state = 'open' AND owner_node = @node AND owner_epoch = @epoch`,
		Params: params,
	})
}

// Expired is an open lease whose expiry, as a scan read it, has passed.
type Expired struct {
	Ref    LeaseRef
	Expiry time.Time
}

// ScanExpired finds up to limit open leases whose expiry plus Config.Skew is
// at or before now, the auditor's clock (§4.8), in one strong read through
// the leases' state index. Each comes with its expiry as read, the condition
// AuditorMarkDraining needs; the read's timestamp comes too.
func (s *Store) ScanExpired(ctx context.Context, now time.Time, limit int) ([]Expired, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	var out []Expired
	err := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT workspace_id, lease_id, expiry FROM tr_lease@{FORCE_INDEX=tr_lease_by_state}
		       WHERE state = 'open' AND expiry <= @cutoff ORDER BY expiry LIMIT @limit`,
		Params: map[string]any{"cutoff": now.Add(-s.cfg.Skew), "limit": int64(limit)},
	}, spanner.QueryOptions{RequestTag: tag("scan-expired")}).Do(func(row *spanner.Row) error {
		var e Expired
		if err := row.Columns(&e.Ref.Workspace, &e.Ref.LeaseID, &e.Expiry); err != nil {
			return err
		}
		out = append(out, e)
		return nil
	})
	if err != nil {
		return nil, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	return out, read, err
}

// AuditorMarkDraining is the auditor's draining write for a lease a scan
// found expired (§4.8): conditional on the lease being open still, with the
// expiry the scan read, so a lease renewed since stays open. It stores F as
// the owner's write does. It reports whether the lease took it, and the
// commit timestamp.
func (s *Store) AuditorMarkDraining(ctx context.Context, ref LeaseRef, readExpiry time.Time) (bool, time.Time, error) {
	params := ref.params()
	params["read"], params["fence"] = readExpiry, s.fence()
	return s.conditional(ctx, "auditor-mark-draining", spanner.Statement{
		SQL: `UPDATE tr_lease
		         SET state = 'draining', drained_by = 'auditor', fence_time = TIMESTAMP_ADD(expiry, INTERVAL @fence MICROSECOND)
		       WHERE workspace_id = @w AND lease_id = @l AND state = 'open' AND expiry = @read`,
		Params: params,
	})
}

// fence is F's distance past the expiry, in microseconds: exact, since New
// takes only whole microseconds. Cut short, F would let a fence tick pass
// a publish its owner was still allowed to make.
func (s *Store) fence() int64 {
	return (s.cfg.Skew + s.cfg.PublishDeadline).Microseconds()
}

// conditional runs one conditional statement in its own transaction and
// reports whether it changed a row.
func (s *Store) conditional(ctx context.Context, operation string, statement spanner.Statement) (bool, time.Time, error) {
	var n int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		var err error
		n, err = txn.UpdateWithOptions(ctx, statement, spanner.QueryOptions{RequestTag: tag(operation)})
		return err
	}, spanner.TransactionOptions{TransactionTag: tag(operation)})
	if err != nil {
		return false, time.Time{}, err
	}
	return n == 1, resp.CommitTs.UTC(), nil
}

// ShortfallResult is the owner's shortfall write's outcome. Rise is what this
// write added to the stored total: 0 for a repeat, or for a write the
// auditor's commit has overtaken. Refused is a closed lease or another
// process's, after which the owner stops retrying (§4.2).
type ShortfallResult struct {
	Rise     int64
	Refused  Refusal
	CommitTS time.Time
}

// ShortfallWrite is the owner's write of its lease's shortfall total (§4.2):
// the stored total becomes the larger of the two, and what it rose by is
// added to the lease's allocation, to its first donor's, and to that donor
// shard's reservation, whatever the shard's headroom, under section 4.7's
// rules for a write that leaves a shard negative. It is conditional on the
// owner's epoch and on the lease not being closed, so a draining lease takes
// it, and close releases what it added. It reads the row and writes the
// difference, never a figure from an earlier read, so a return applied in
// between is not undone; and it leaves the auditor's commit version alone.
func (s *Store) ShortfallWrite(ctx context.Context, owner Owner, ref LeaseRef, total int64) (ShortfallResult, error) {
	if total < 0 {
		return ShortfallResult{}, errors.New("store: a shortfall total is not negative")
	}
	var out ShortfallResult
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = ShortfallResult{}
		row, err := txn.ReadRowWithOptions(ctx, "tr_lease", ref.key(), []string{"state", "owner_node", "owner_epoch", "shortfall_total"},
			&spanner.ReadOptions{RequestTag: tag("shortfall-write")})
		if spanner.ErrCode(err) == codes.NotFound {
			out.Refused = RefusedClosed
			return nil
		}
		if err != nil {
			return err
		}
		var state, node string
		var epoch, stored int64
		if err := row.Columns(&state, &node, &epoch, &stored); err != nil {
			return err
		}
		switch {
		case state == "closed":
			out.Refused = RefusedClosed
			return nil
		case node != owner.Node || epoch != owner.Epoch:
			out.Refused = RefusedOwner
			return nil
		case total <= stored:
			return nil
		}
		out.Rise = total - stored
		params := ref.params()
		params["total"], params["stored"], params["rise"] = total, stored, out.Rise
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease SET shortfall_total = @total, allocation = allocation + @rise
			       WHERE workspace_id = @w AND lease_id = @l AND shortfall_total = @stored AND state != 'closed'`,
			Params: params,
		}, spanner.QueryOptions{RequestTag: tag("shortfall-write")})
		if err != nil {
			return err
		}
		if n != 1 {
			return errors.New("store: the lease row changed inside its own transaction")
		}
		return raiseFirstDonor(ctx, txn, ref, out.Rise, "shortfall-write")
	}, spanner.TransactionOptions{TransactionTag: tag("shortfall-write")})
	if err != nil {
		return ShortfallResult{}, err
	}
	if out.Rise > 0 {
		out.CommitTS = resp.CommitTs.UTC()
	}
	return out, nil
}

// raiseFirstDonor adds a raise to the lease's first donor's allocation and
// to that credit shard's reservation (§4.2: the raise lands on the first
// donor shard, whatever its headroom), covering or marking as section 4.7
// says when the shard goes negative.
func raiseFirstDonor(ctx context.Context, txn *spanner.ReadWriteTransaction, ref LeaseRef, rise int64, operation string) error {
	var shard int64
	found := false
	err := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT credit_shard FROM tr_lease_donor WHERE workspace_id = @w AND lease_id = @l
		       ORDER BY credit_shard LIMIT 1`,
		Params: ref.params(),
	}, spanner.QueryOptions{RequestTag: tag(operation)}).Do(func(row *spanner.Row) error {
		found = true
		return row.Column(0, &shard)
	})
	if err != nil {
		return err
	}
	if !found {
		return errors.New("store: the lease has no donor")
	}
	params := ref.params()
	params["shard"], params["rise"] = shard, rise
	n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
		SQL: `UPDATE tr_lease_donor SET allocation = allocation + @rise
		       WHERE workspace_id = @w AND lease_id = @l AND credit_shard = @shard`,
		Params: params,
	}, spanner.QueryOptions{RequestTag: tag(operation)})
	if err != nil {
		return err
	}
	if n != 1 {
		return errors.New("store: the lease's first donor changed inside its own transaction")
	}
	_, err = adjust(ctx, txn, ref.Workspace, shard, rise, 0, operation)
	return err
}
