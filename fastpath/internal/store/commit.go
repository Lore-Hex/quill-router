package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// Why the auditor's writes to a lease are refused. Every one of them is
// conditional on the commit version the member read (§4.8: one guard on
// every write); after a refusal the member re-reads the lease and applies
// again from there.
const (
	RefusedVersion  Refusal = "another member has committed since this one read the lease"
	RefusedGap      Refusal = "the lease is stopped at a gap"
	RefusedBoundary Refusal = "the lease's progress is held at its stored boundary S"
)

// HoldRow is a stored open hold (§4.8): its estimate and deadline, its
// latest valid snapshot, and what a reap of it needs.
type HoldRow struct {
	AuthorizationID  string
	Estimate         int64
	Deadline         time.Time
	Listed           bool
	SnapshotSeq      spanner.NullInt64
	SnapshotHash     []byte
	SnapshotUsage    []byte
	RunningCharge    spanner.NullInt64
	SnapshotOwnerSeq spanner.NullInt64
	ReapBasis        []byte
}

var holdColumns = []string{"workspace_id", "lease_id", "authorization_id", "estimate", "deadline", "listed",
	"snapshot_seq", "snapshot_hash", "snapshot_usage", "running_charge", "snapshot_owner_seq", "reap_basis"}

func (h HoldRow) values(ref LeaseRef) []any {
	return []any{ref.Workspace, ref.LeaseID, h.AuthorizationID, h.Estimate, h.Deadline, h.Listed, h.SnapshotSeq,
		h.SnapshotHash, h.SnapshotUsage, h.RunningCharge, h.SnapshotOwnerSeq, h.ReapBasis}
}

// Winner is the terminal a commit stores as its authorization's winner, with
// its pending work (§4.8, §4.9): a settle, a refund or a reap, from the
// owner's records or the drain log, its charge (zero for a refund), and the
// record ID that names it.
type Winner struct {
	AuthorizationID string          `json:"a"`
	Kind            string          `json:"kind"`
	Charge          int64           `json:"charge"`
	FromDrain       bool            `json:"from_drain,omitempty"`
	RecordID        string          `json:"record_id"`
	Work            json.RawMessage `json:"work,omitempty"`
}

// pack is a commit's winners as tr_lease_winners stores them: versioned
// JSON, one row per lease per commit.
type pack struct {
	Version int      `json:"v"`
	Winners []Winner `json:"winners"`
}

// Boundary is S, the highest owner sequence number applied when the auditor
// applies its fence tick, with T, the tick's publish time (§4.8).
type Boundary struct {
	S int64
	T time.Time
}

// CommitRequest is one lease's part of an auditor's commit (§4.8): the
// version the member read, its progress, the money its records book or
// return in log order, the holds to store, the winners, whose holds go with
// them, and, optionally, S and T, the sequence number of an applied final
// checkpoint or complete hand-off that listed the open holds, and the
// sequence number of a checkpoint whose audit failed, which stores the alert
// and revokes the lease with the commit.
type CommitRequest struct {
	Ref            LeaseRef
	ReadVersion    int64
	AppliedSeq     int64
	LastTick       int64
	AuditOsum      int64
	Money          []MoneyOp
	PutHolds       []HoldRow
	Winners        []Winner
	Boundary       *Boundary
	HoldsListedSeq *int64
	AuditFault     *int64
}

// CommitResult is one lease's outcome: refused and why, with nothing
// written for the lease; or its new version and state, and each fault, the
// part of a booking beyond the allocation, which the commit booked as usage.
type CommitResult struct {
	Ref        LeaseRef
	Refused    Refusal
	NewVersion int64
	State      string
	Faults     []int64
}

// validate checks what a request says of itself.
func (r CommitRequest) validate() error {
	if r.Ref.Workspace == "" || r.Ref.LeaseID == "" || r.ReadVersion < 0 || r.AppliedSeq < 0 || r.LastTick < 0 {
		return errors.New("store: a commit names its lease, and its version and progress are not negative")
	}
	if r.Boundary != nil && r.Boundary.S != r.AppliedSeq {
		return fmt.Errorf("store: S %d is stored with the progress it ends, not %d (§4.8)", r.Boundary.S, r.AppliedSeq)
	}
	won := map[string]bool{}
	for _, w := range r.Winners {
		if w.AuthorizationID == "" || w.RecordID == "" || w.Charge < 0 || won[w.AuthorizationID] {
			return fmt.Errorf("store: winner %+v is not one winner of an authorization", w)
		}
		if w.Kind != "settle" && w.Kind != "refund" && w.Kind != "reap" && w.Kind != "release" {
			return fmt.Errorf("store: winner of kind %q", w.Kind)
		}
		won[w.AuthorizationID] = true
	}
	put := map[string]bool{}
	for _, h := range r.PutHolds {
		if h.AuthorizationID == "" || h.Estimate < 0 || put[h.AuthorizationID] || won[h.AuthorizationID] ||
			h.SnapshotSeq.Valid != h.RunningCharge.Valid {
			return fmt.Errorf("store: hold %s is not one open hold", h.AuthorizationID)
		}
		put[h.AuthorizationID] = true
	}
	return nil
}

// Commit is the auditor's per-lease commit (§4.8): one transaction for
// many leases. Each lease's part is conditional on the version the member
// read, on the lease not being closed, its row kept or deleted since, on no
// gap, and, once S is stored, on the request's progress being S, since no
// owner record past S is applied; a lease refused writes nothing, and the
// others commit. A closed lease's remainder is returned and its row may be
// going, so nothing more is booked on it or left pending in a pack. For a
// lease it commits, it advances the version by one, stores the progress,
// books and returns the money in log order against the row as read in the
// transaction (applyMoney), so a raise an owner or a front door made since
// the member loaded the lease is kept, and stores the holds and one pack of
// the winners, each of whose holds goes with it. S, once stored, does not
// change; a winner from the drain log needs it stored by an earlier commit,
// since the drain log is applied only once S is durable. Then, for each
// workspace,
// the credit rows take the bookings and raises in ascending shard order and
// are squared (§4.7), and each return frees its money, repaying any debt
// first. Money that would pass int64's range is an error, and nothing is
// written.
func (s *Store) Commit(ctx context.Context, reqs []CommitRequest) ([]CommitResult, time.Time, error) {
	leases := map[LeaseRef]bool{}
	for _, r := range reqs {
		if err := r.validate(); err != nil {
			return nil, time.Time{}, err
		}
		if leases[r.Ref] {
			return nil, time.Time{}, fmt.Errorf("store: lease %v twice in one commit", r.Ref)
		}
		leases[r.Ref] = true
	}
	var out []CommitResult
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = make([]CommitResult, len(reqs))
		shards := map[string]map[int64]shardMoney{}
		releases := map[string][]creditRelease{}
		var mutations []*spanner.Mutation
		for i, r := range reqs {
			out[i] = CommitResult{Ref: r.Ref}
			l, money, err := readLeaseMoney(ctx, txn, r.Ref, "commit")
			if errors.Is(err, ErrNoLease) {
				// Closed, and its row deleted since: a member that stalled
				// past both learns it here, and the batch's others commit.
				out[i].Refused = RefusedClosed
				continue
			}
			if err != nil {
				return err
			}
			switch {
			case l.CommitVersion != r.ReadVersion:
				out[i].Refused = RefusedVersion
			case l.State == "closed":
				out[i].Refused = RefusedClosed
			case l.GapSeq.Valid:
				out[i].Refused = RefusedGap
			case l.BoundarySeq.Valid && l.BoundarySeq.Int64 != r.AppliedSeq:
				out[i].Refused = RefusedBoundary
			}
			if out[i].Refused != "" {
				continue
			}
			if r.Boundary != nil && l.State == "open" {
				return fmt.Errorf("store: S is stored on a draining lease, and %v is open", r.Ref)
			}
			for _, w := range r.Winners {
				if w.FromDrain && !l.BoundarySeq.Valid {
					return fmt.Errorf("store: a winner from the drain log of %v before S is stored (§4.8)", r.Ref)
				}
			}
			eff, err := applyMoney(money, r.Money)
			if err != nil {
				return err
			}
			out[i].Faults = eff.Faults
			if err := writeLeaseCommit(ctx, txn, r, eff.After, money, &out[i]); err != nil {
				return err
			}
			if shards[r.Ref.Workspace] == nil {
				shards[r.Ref.Workspace] = map[int64]shardMoney{}
			}
			for shard, d := range eff.Shards {
				t := shards[r.Ref.Workspace][shard]
				if t.Reserved, err = plus(t.Reserved, d.Reserved); err != nil {
					return err
				}
				if t.Usage, err = plus(t.Usage, d.Usage); err != nil {
					return err
				}
				shards[r.Ref.Workspace][shard] = t
			}
			releases[r.Ref.Workspace] = append(releases[r.Ref.Workspace], eff.Releases...)
			for _, h := range r.PutHolds {
				mutations = append(mutations, spanner.InsertOrUpdate("tr_lease_hold", holdColumns, h.values(r.Ref)))
			}
			for _, w := range r.Winners {
				mutations = append(mutations, spanner.Delete("tr_lease_hold", spanner.Key{r.Ref.Workspace, r.Ref.LeaseID, w.AuthorizationID}))
			}
			body, err := json.Marshal(pack{Version: 1, Winners: append([]Winner{}, r.Winners...)})
			if err != nil {
				return err
			}
			mutations = append(mutations, spanner.Insert("tr_lease_winners",
				[]string{"workspace_id", "lease_id", "commit_version", "pack", "winner_count"},
				[]any{r.Ref.Workspace, r.Ref.LeaseID, out[i].NewVersion, body, int64(len(r.Winners))}))
		}
		workspaces := make([]string, 0, len(shards))
		for w := range shards {
			workspaces = append(workspaces, w)
		}
		sort.Strings(workspaces)
		for _, w := range workspaces {
			if err := settleCreditRows(ctx, txn, w, shards[w], releases[w], "commit"); err != nil {
				return err
			}
		}
		return txn.BufferWrite(mutations)
	}, spanner.TransactionOptions{TransactionTag: tag("commit")})
	if err != nil {
		return nil, time.Time{}, err
	}
	return out, resp.CommitTs.UTC(), nil
}

// readLeaseMoney reads a lease's row and its donors inside a transaction.
func readLeaseMoney(ctx context.Context, txn *spanner.ReadWriteTransaction, ref LeaseRef, operation string) (Lease, leaseMoney, error) {
	row, err := txn.ReadRowWithOptions(ctx, "tr_lease", ref.key(), leaseColumns, &spanner.ReadOptions{RequestTag: tag(operation)})
	if spanner.ErrCode(err) == codes.NotFound {
		return Lease{}, leaseMoney{}, fmt.Errorf("%w: %v", ErrNoLease, ref)
	}
	if err != nil {
		return Lease{}, leaseMoney{}, err
	}
	l := Lease{Ref: ref}
	if err := l.scan(row); err != nil {
		return Lease{}, leaseMoney{}, err
	}
	money := leaseMoney{Allocation: l.Allocation, Consumed: l.Consumed, ShortfallTotal: l.ShortfallTotal,
		Returned: l.Returned, FaultUsage: l.FaultUsage}
	err = txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT credit_shard, allocation, consumed FROM tr_lease_donor
		       WHERE workspace_id = @w AND lease_id = @l ORDER BY credit_shard`,
		Params: ref.params(),
	}, spanner.QueryOptions{RequestTag: tag(operation)}).Do(func(row *spanner.Row) error {
		var d donorMoney
		if err := row.Columns(&d.Shard, &d.Allocation, &d.Consumed); err != nil {
			return err
		}
		money.Donors = append(money.Donors, d)
		return nil
	})
	return l, money, err
}

// writeLeaseCommit writes a lease's row and its donors for a commit. The
// row's statement repeats the guards the transaction checked on its read.
func writeLeaseCommit(ctx context.Context, txn *spanner.ReadWriteTransaction, r CommitRequest, after, before leaseMoney,
	result *CommitResult) error {
	params := r.Ref.params()
	params["read"], params["applied"], params["tick"], params["osum"] = r.ReadVersion, r.AppliedSeq, r.LastTick, r.AuditOsum
	params["allocation"], params["consumed"], params["total"] = after.Allocation, after.Consumed, after.ShortfallTotal
	params["returned"], params["fault"] = after.Returned, after.FaultUsage
	params["s"], params["t"] = spanner.NullInt64{}, spanner.NullTime{}
	if r.Boundary != nil {
		params["s"] = spanner.NullInt64{Int64: r.Boundary.S, Valid: true}
		params["t"] = spanner.NullTime{Time: r.Boundary.T, Valid: true}
	}
	params["listed"], params["fault_seq"] = spanner.NullInt64{}, spanner.NullInt64{}
	if r.HoldsListedSeq != nil {
		params["listed"] = spanner.NullInt64{Int64: *r.HoldsListedSeq, Valid: true}
	}
	if r.AuditFault != nil {
		params["fault_seq"] = spanner.NullInt64{Int64: *r.AuditFault, Valid: true}
	}
	rows := 0
	err := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `UPDATE tr_lease
		         SET commit_version = commit_version + 1, applied_seq = @applied, last_tick = @tick, audit_osum = @osum,
		             allocation = @allocation, consumed = @consumed, shortfall_total = @total, returned = @returned,
		             fault_usage = @fault,
		             boundary_seq = COALESCE(boundary_seq, @s),
		             boundary_publish_time = COALESCE(boundary_publish_time, @t),
		             holds_listed_seq = COALESCE(@listed, holds_listed_seq),
		             audit_fault_seq = COALESCE(@fault_seq, audit_fault_seq),
		             revoked = revoked OR @fault_seq IS NOT NULL
		       WHERE workspace_id = @w AND lease_id = @l AND commit_version = @read AND state != 'closed'
		         AND gap_seq IS NULL AND (boundary_seq IS NULL OR boundary_seq = @applied)
		      THEN RETURN state, commit_version`,
		Params: params,
	}, spanner.QueryOptions{RequestTag: tag("commit")}).Do(func(row *spanner.Row) error {
		rows++
		return row.Columns(&result.State, &result.NewVersion)
	})
	if err != nil {
		return err
	}
	if rows != 1 {
		return fmt.Errorf("store: lease %v changed inside its commit's transaction", r.Ref)
	}
	for i, d := range after.Donors {
		if d == before.Donors[i] {
			continue
		}
		p := r.Ref.params()
		p["shard"], p["allocation"], p["consumed"] = d.Shard, d.Allocation, d.Consumed
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_lease_donor SET allocation = @allocation, consumed = @consumed
			       WHERE workspace_id = @w AND lease_id = @l AND credit_shard = @shard`,
			Params: p,
		}, spanner.QueryOptions{RequestTag: tag("commit")})
		if err != nil {
			return err
		}
		if n != 1 {
			return fmt.Errorf("store: donor %d of %v changed inside its commit's transaction", d.Shard, r.Ref)
		}
	}
	return nil
}

// settleCreditRows applies a workspace's credit changes from one commit: the
// bookings, raises and faults, each shard's in one statement in ascending
// order, squared once if any row ends negative; then each return's release,
// in order, which repays debt first on a marked workspace (§4.7).
func settleCreditRows(ctx context.Context, txn *spanner.ReadWriteTransaction, workspace string, changes map[int64]shardMoney,
	releases []creditRelease, operation string) error {
	order := make([]int64, 0, len(changes))
	for shard := range changes {
		order = append(order, shard)
	}
	sort.Slice(order, func(i, j int) bool { return order[i] < order[j] })
	negative := false
	for _, shard := range order {
		c := changes[shard]
		if c == (shardMoney{}) {
			continue
		}
		var headroom int64
		rows := 0
		err := txn.QueryWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_credit_balance
			         SET reserved = reserved + @r, total_usage = total_usage + @u, updated_at = CURRENT_TIMESTAMP()
			       WHERE workspace_id = @w AND shard = @shard AND reserved >= -@r
			      THEN RETURN total_credits - total_usage - reserved`,
			Params: map[string]any{"r": c.Reserved, "u": c.Usage, "w": workspace, "shard": shard},
		}, spanner.QueryOptions{RequestTag: tag(operation)}).Do(func(row *spanner.Row) error {
			rows++
			return row.Column(0, &headroom)
		})
		if err != nil {
			return err
		}
		if rows != 1 {
			return fmt.Errorf("%w: %s/%d has no row, or less reserved than is taken off it", ErrCreditRowsChanged, workspace, shard)
		}
		negative = negative || headroom < 0
	}
	if negative {
		if _, err := squareCreditRows(ctx, txn, workspace, operation); err != nil {
			return err
		}
	}
	for _, r := range releases {
		if err := release(ctx, txn, workspace, r.Shard, r.Amount, operation); err != nil {
			return err
		}
	}
	return nil
}

// StopForGap stores the gap that stops a lease (§4.8): seq is a record's
// sequence number beyond the next one after the stored progress. It is
// conditional on the version the member read, so a member another member
// overtook does not stop the lease for a gap the log does not have; on no
// gap stored; and on S not stored, since records past S are not applied
// and leave no gap. It does not advance the version. It reports whether the
// lease took it.
func (s *Store) StopForGap(ctx context.Context, ref LeaseRef, readVersion, seq int64) (bool, time.Time, error) {
	if seq < 1 {
		return false, time.Time{}, fmt.Errorf("store: a gap at sequence number %d", seq)
	}
	params := ref.params()
	params["read"], params["seq"] = readVersion, seq
	return s.conditional(ctx, "stop-for-gap", spanner.Statement{
		SQL: `UPDATE tr_lease SET gap_seq = @seq
		       WHERE workspace_id = @w AND lease_id = @l AND commit_version = @read AND gap_seq IS NULL
		         AND boundary_seq IS NULL AND applied_seq < @seq - 1`,
		Params: params,
	})
}
