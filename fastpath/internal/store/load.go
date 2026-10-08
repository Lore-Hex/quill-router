package store

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// Pack is one commit's winners.
type Pack struct {
	CommitVersion int64
	Winners       []Winner
	WorkDoneAt    spanner.NullTime
}

// Loaded is what a member loads to take over a lease (§4.8): its row, with
// its version and progress, its stored open holds and, once the lease is not
// open, its winners, all from one snapshot, whose timestamp comes too. While
// a lease is open only its owner publishes terminals, so its winners are
// needed only once it drains (LoadWinners).
type Loaded struct {
	Lease  Lease
	Holds  []HoldRow
	Packs  []Pack
	ReadTS time.Time
}

// Load reads a lease for a member, in one read-only transaction.
func (s *Store) Load(ctx context.Context, ref LeaseRef) (Loaded, error) {
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	row, err := ro.ReadRowWithOptions(ctx, "tr_lease", ref.key(), leaseColumns, &spanner.ReadOptions{RequestTag: tag("load")})
	if spanner.ErrCode(err) == codes.NotFound {
		return Loaded{}, fmt.Errorf("%w: %v", ErrNoLease, ref)
	}
	if err != nil {
		return Loaded{}, err
	}
	out := Loaded{Lease: Lease{Ref: ref}}
	if err := out.Lease.scan(row); err != nil {
		return Loaded{}, err
	}
	err = ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT authorization_id, estimate, deadline, listed, snapshot_seq, snapshot_hash, snapshot_usage,
		             running_charge, snapshot_owner_seq, reap_basis
		        FROM tr_lease_hold WHERE workspace_id = @w AND lease_id = @l ORDER BY authorization_id`,
		Params: ref.params(),
	}, spanner.QueryOptions{RequestTag: tag("load")}).Do(func(row *spanner.Row) error {
		var h HoldRow
		if err := row.Columns(&h.AuthorizationID, &h.Estimate, &h.Deadline, &h.Listed, &h.SnapshotSeq, &h.SnapshotHash,
			&h.SnapshotUsage, &h.RunningCharge, &h.SnapshotOwnerSeq, &h.ReapBasis); err != nil {
			return err
		}
		out.Holds = append(out.Holds, h)
		return nil
	})
	if err != nil {
		return Loaded{}, err
	}
	if out.Lease.State != "open" {
		if out.Packs, err = readPacks(ctx, ro, ref, "load"); err != nil {
			return Loaded{}, err
		}
	}
	out.ReadTS, err = readTimestamp(ro)
	return out, err
}

// LoadWinners reads a lease's winners, every commit's pack in version order,
// in one strong read: for a member that loaded the lease open and has seen
// it drain since.
func (s *Store) LoadWinners(ctx context.Context, ref LeaseRef) ([]Pack, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	packs, err := readPacks(ctx, ro, ref, "load-winners")
	if err != nil {
		return nil, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	return packs, read, err
}

// querier is a transaction that reads, read-only or read-write.
type querier interface {
	QueryWithOptions(ctx context.Context, statement spanner.Statement, opts spanner.QueryOptions) *spanner.RowIterator
}

func readPacks(ctx context.Context, q querier, ref LeaseRef, operation string) ([]Pack, error) {
	var packs []Pack
	err := q.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT commit_version, pack, work_done_at FROM tr_lease_winners
		       WHERE workspace_id = @w AND lease_id = @l ORDER BY commit_version`,
		Params: ref.params(),
	}, spanner.QueryOptions{RequestTag: tag(operation)}).Do(func(row *spanner.Row) error {
		var p Pack
		var body []byte
		if err := row.Columns(&p.CommitVersion, &body, &p.WorkDoneAt); err != nil {
			return err
		}
		var decoded pack
		if err := json.Unmarshal(body, &decoded); err != nil {
			return fmt.Errorf("store: pack %d of %v: %w", p.CommitVersion, ref, err)
		}
		if decoded.Version != 1 {
			return fmt.Errorf("store: pack %d of %v has version %d", p.CommitVersion, ref, decoded.Version)
		}
		p.Winners = decoded.Winners
		packs = append(packs, p)
		return nil
	})
	return packs, err
}
