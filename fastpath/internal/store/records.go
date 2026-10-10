package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// stagingTag marks the staging stand-in's writes, which the spike counts
// apart, since production stages full records elsewhere (spike plan §2).
const stagingTag = "spike-staging"

// StagedRecord is a full request record in the spike's stand-in for staging
// (design §4.9), keyed by authorization and digest: an authorization can
// have two, an original and an enclave retry's compacted copy.
type StagedRecord struct {
	AuthorizationID string
	Digest          []byte
	Ref             LeaseRef
	Body            []byte
	MessageID       string
	PublishTime     time.Time
}

// ErrRetired means the lease has retired: it closed with every pack's work
// done, so nothing reads a record staged for it.
var ErrRetired = errors.New("store: the lease has retired")

// StageRecord stages a full record, at least once, for a lease that has not
// retired: one transaction, tagged apart, reads the lease's retirement and
// writes the record, and writing it twice leaves one row. A pack's work
// waits for its winners' staged records, so a lease that has retired needs
// none: a record that comes after, a redelivery or a copy no winner names,
// is staged for no one (ErrRetired), and one whose lease's row is gone,
// neither (ErrNoLease). The retirement's write and this read exclude each
// other, so once a workspace's leases have retired and their staged records
// are dropped, no staged record of it returns.
func (s *Store) StageRecord(ctx context.Context, r StagedRecord) error {
	if r.AuthorizationID == "" || len(r.Digest) == 0 || len(r.Body) == 0 || r.MessageID == "" {
		return errors.New("store: a staged record has an authorization, a digest, a body and a message ID")
	}
	_, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		row, err := txn.ReadRowWithOptions(ctx, "tr_lease", r.Ref.key(), []string{"retire_at"},
			&spanner.ReadOptions{RequestTag: stagingTag})
		if spanner.ErrCode(err) == codes.NotFound {
			return fmt.Errorf("%w: %v", ErrNoLease, r.Ref)
		}
		if err != nil {
			return err
		}
		var retired spanner.NullTime
		if err := row.Column(0, &retired); err != nil {
			return err
		}
		if retired.Valid {
			return fmt.Errorf("%w: %v", ErrRetired, r.Ref)
		}
		return txn.BufferWrite([]*spanner.Mutation{spanner.InsertOrUpdate("tr_spike_staged",
			[]string{"authorization_id", "record_digest", "workspace_id", "lease_id", "body", "message_id", "publish_time"},
			[]any{r.AuthorizationID, r.Digest, r.Ref.Workspace, r.Ref.LeaseID, r.Body, r.MessageID, r.PublishTime})})
	}, spanner.TransactionOptions{TransactionTag: stagingTag})
	return err
}

// ReadStaged reads the staged record with an authorization and a digest,
// the winner's: the join is never by authorization alone (§4.9). It reports
// whether there is one.
func (s *Store) ReadStaged(ctx context.Context, authorization string, digest []byte) (StagedRecord, bool, error) {
	row, err := s.client.Single().ReadRowWithOptions(ctx, "tr_spike_staged", spanner.Key{authorization, digest},
		[]string{"workspace_id", "lease_id", "body", "message_id", "publish_time"},
		&spanner.ReadOptions{RequestTag: stagingTag})
	if spanner.ErrCode(err) == codes.NotFound {
		return StagedRecord{}, false, nil
	}
	if err != nil {
		return StagedRecord{}, false, err
	}
	r := StagedRecord{AuthorizationID: authorization, Digest: digest}
	if err := row.Columns(&r.Ref.Workspace, &r.Ref.LeaseID, &r.Body, &r.MessageID, &r.PublishTime); err != nil {
		return StagedRecord{}, false, err
	}
	return r, true, nil
}

// StagedKey names a staged record: its authorization and digest.
type StagedKey struct {
	AuthorizationID string
	Digest          []byte
}

// MaxDropStaged bounds the staged records one DropStaged removes: each is
// two of a commit's mutations, its row's and its index entry's.
const MaxDropStaged = 1000

// DropStaged removes up to MaxDropStaged staged records that its caller
// knows nothing needs (§4.9): a pack's winners', once the pack's work is
// done, or those RetiredStaged reads.
func (s *Store) DropStaged(ctx context.Context, keys ...StagedKey) error {
	if len(keys) > MaxDropStaged {
		return fmt.Errorf("store: %d staged records dropped at once, past %d", len(keys), MaxDropStaged)
	}
	if len(keys) == 0 {
		return nil
	}
	mutations := make([]*spanner.Mutation, 0, len(keys))
	for _, k := range keys {
		mutations = append(mutations, spanner.Delete("tr_spike_staged", spanner.Key{k.AuthorizationID, k.Digest}))
	}
	_, err := s.client.Apply(ctx, mutations, spanner.TransactionTag(stagingTag))
	return err
}

// RetiredStaged reads up to limit staged records that nothing can need,
// since their lease has retired, closed with every pack's work done, or its
// row is gone, seven days after (§4.9): a record no winner names, such as a
// losing terminal's or an enclave retry's compacted copy, one a crash left
// between its pack's mark and its drop, and one a redelivery staged again
// after its drop, before the lease retired. A lease's row goes only once it
// has retired, and nothing is staged for a lease retired or gone
// (StageRecord), so neither kind needs it again. It reads the staged records' index on their lease, in one
// strong read.
func (s *Store) RetiredStaged(ctx context.Context, limit int) ([]StagedKey, error) {
	var out []StagedKey
	err := s.client.Single().QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT s.authorization_id, s.record_digest FROM tr_spike_staged@{FORCE_INDEX=tr_spike_staged_by_lease} AS s
		       LEFT JOIN tr_lease AS l ON l.workspace_id = s.workspace_id AND l.lease_id = s.lease_id
		       WHERE l.lease_id IS NULL OR l.retire_at IS NOT NULL
		       LIMIT @limit`,
		Params: map[string]any{"limit": int64(limit)},
	}, spanner.QueryOptions{RequestTag: stagingTag}).Do(func(row *spanner.Row) error {
		var k StagedKey
		if err := row.Columns(&k.AuthorizationID, &k.Digest); err != nil {
			return err
		}
		out = append(out, k)
		return nil
	})
	return out, err
}

// LeaseRecord is a record the pending work writes (§4.9): an
// authorization's generation and activity records, standing for production's,
// or a refund's or a release's disposition record. Cost is NULL when it is
// not known, never zero in its place.
type LeaseRecord struct {
	AuthorizationID string
	Kind            string
	Ref             LeaseRef
	Outcome         string
	Cost            spanner.NullInt64
	WinnerDigest    []byte
	BootBinding     []byte
	Body            []byte
}

// WriteRecords writes records, idempotent on authorization and kind: a
// record written twice, as after a crash between writing and marking the
// pack done, leaves one row.
func (s *Store) WriteRecords(ctx context.Context, records []LeaseRecord) error {
	mutations := make([]*spanner.Mutation, 0, len(records))
	for _, r := range records {
		if r.AuthorizationID == "" || len(r.Body) == 0 {
			return errors.New("store: a record has an authorization and a body")
		}
		mutations = append(mutations, spanner.InsertOrUpdate("tr_lease_record",
			[]string{"authorization_id", "kind", "workspace_id", "lease_id", "outcome", "cost", "winner_digest",
				"boot_binding", "body"},
			[]any{r.AuthorizationID, r.Kind, r.Ref.Workspace, r.Ref.LeaseID, r.Outcome, r.Cost, r.WinnerDigest,
				r.BootBinding, r.Body}))
	}
	_, err := s.client.Apply(ctx, mutations, spanner.TransactionTag(tag("write-records")))
	return err
}

// PendingPack names a pack whose work is not done.
type PendingPack struct {
	Ref           LeaseRef
	CommitVersion int64
}

// PendingPacks reads up to limit packs whose work is not done, in key order
// after the pack named by after (the zero PendingPack: from the first), in
// one strong read through the packs' index on that time: the sweep over
// closed leases' pending work. The sweep pages from the last pack it read
// and starts again from the first at the end, so a pack whose work cannot be
// done yet, such as one whose staged record is missing, holds up none after
// it.
func (s *Store) PendingPacks(ctx context.Context, after PendingPack, limit int) ([]PendingPack, error) {
	var out []PendingPack
	err := s.client.Single().QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT workspace_id, lease_id, commit_version FROM tr_lease_winners@{FORCE_INDEX=tr_lease_winners_by_work}
		       WHERE work_done_at IS NULL
		         AND (workspace_id > @w OR (workspace_id = @w AND (lease_id > @l OR (lease_id = @l AND commit_version > @v))))
		       ORDER BY workspace_id, lease_id, commit_version LIMIT @limit`,
		Params: map[string]any{"w": after.Ref.Workspace, "l": after.Ref.LeaseID, "v": after.CommitVersion, "limit": int64(limit)},
	}, spanner.QueryOptions{RequestTag: tag("pending-packs")}).Do(func(row *spanner.Row) error {
		var p PendingPack
		if err := row.Columns(&p.Ref.Workspace, &p.Ref.LeaseID, &p.CommitVersion); err != nil {
			return err
		}
		out = append(out, p)
		return nil
	})
	return out, err
}

// Disposition is the answer for an authorization (§4.5, §4.9), as today's
// helper answers one that is already terminal: settled, refunded,
// reaped_snapshot or released, with the cost where there is one; pending
// when no winner or record can be found.
type Disposition struct {
	Outcome string
	Cost    spanner.NullInt64
	// From says where the answer came from: the stored winner, the written
	// record, the lease the auditor closed without a winner for it, or
	// nowhere.
	From string
}

var outcomes = map[string]string{"settle": "settled", "refund": "refunded", "reap": "reaped_snapshot", "release": "released"}

// Disposition answers for an authorization: from its stored winner while
// the winner is kept, then from its written records, then, for a lease the
// auditor closed with no winner for it, released, since the close released
// its hold uncharged. That answer rests on the packs going only with their
// lease's row: a kept row has every pack the lease stored, so none naming
// the authorization means no winner was stored, not one deleted whose
// record awaits rebuilding. A lease an operator closed answers pending,
// since the request may have run. An authorization the store cannot place
// answers pending, never released: a missing record is never read as a
// refund. The lease comes from the authorization's ID (tr_lease_by_id), and
// its packs from the lease, so the lookup is bounded.
func (s *Store) Disposition(ctx context.Context, authorization string) (Disposition, error) {
	pending := Disposition{Outcome: "pending", From: "nowhere"}
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	var lease *Lease
	if leaseID, err := LeaseOfAuthorization(authorization); err == nil {
		var l Lease
		found := false
		err := ro.QueryWithOptions(ctx, spanner.Statement{
			SQL:    `SELECT workspace_id, state, close_kind FROM tr_lease@{FORCE_INDEX=tr_lease_by_id} WHERE lease_id = @l`,
			Params: map[string]any{"l": leaseID},
		}, spanner.QueryOptions{RequestTag: tag("disposition")}).Do(func(row *spanner.Row) error {
			found = true
			l.Ref.LeaseID = leaseID
			return row.Columns(&l.Ref.Workspace, &l.State, &l.CloseKind)
		})
		if err != nil {
			return Disposition{}, err
		}
		if found {
			lease = &l
			packs, err := readPacks(ctx, ro, l.Ref, "disposition")
			if err != nil {
				return Disposition{}, err
			}
			for _, p := range packs {
				for _, w := range p.Winners {
					if w.AuthorizationID == authorization {
						return Disposition{Outcome: outcomes[w.Kind], Cost: spanner.NullInt64{Int64: w.Charge, Valid: true},
							From: "winner"}, nil
					}
				}
			}
		}
	}
	var d Disposition
	found := false
	err := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT outcome, cost FROM tr_lease_record WHERE authorization_id = @a
		       ORDER BY CASE kind WHEN 'generation' THEN 0 WHEN 'disposition' THEN 1 ELSE 2 END LIMIT 1`,
		Params: map[string]any{"a": authorization},
	}, spanner.QueryOptions{RequestTag: tag("disposition")}).Do(func(row *spanner.Row) error {
		found = true
		return row.Columns(&d.Outcome, &d.Cost)
	})
	if err != nil {
		return Disposition{}, err
	}
	switch {
	case found:
		d.From = "record"
		return d, nil
	case lease != nil && lease.State == "closed" && lease.CloseKind.StringVal == "auditor":
		return Disposition{Outcome: "released", Cost: spanner.NullInt64{Int64: 0, Valid: true}, From: "closed"}, nil
	}
	return pending, nil
}
