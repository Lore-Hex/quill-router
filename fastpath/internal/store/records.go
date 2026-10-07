package store

import (
	"context"
	"errors"
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

// StageRecord stages a full record, at least once: a write in its own
// transaction, tagged apart, with no guard, since writing it twice leaves
// one row.
func (s *Store) StageRecord(ctx context.Context, r StagedRecord) error {
	if r.AuthorizationID == "" || len(r.Digest) == 0 || len(r.Body) == 0 || r.MessageID == "" {
		return errors.New("store: a staged record has an authorization, a digest, a body and a message ID")
	}
	_, err := s.client.Apply(ctx, []*spanner.Mutation{spanner.InsertOrUpdate("tr_spike_staged",
		[]string{"authorization_id", "record_digest", "workspace_id", "lease_id", "body", "message_id", "publish_time"},
		[]any{r.AuthorizationID, r.Digest, r.Ref.Workspace, r.Ref.LeaseID, r.Body, r.MessageID, r.PublishTime})},
		spanner.TransactionTag(stagingTag))
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

// DropStaged removes an authorization's staged records with the digests
// given, once nothing can need them (§4.9): once the pack that holds the
// authorization's winner is marked done, so the winner's records are all
// written and its outcome published; or, for an authorization with no
// winner, once its lease has closed, as for losing terminals. A record
// written alone is not enough: the winner's others may not be. Until then
// the records stay and DropStaged reports false; so too while the store
// cannot find the lease. The lease comes from the authorization's ID, and
// its packs from the lease.
func (s *Store) DropStaged(ctx context.Context, authorization string, digests ...[]byte) (bool, error) {
	leaseID, err := LeaseOfAuthorization(authorization)
	if err != nil {
		return false, err
	}
	var dropped bool
	_, err = s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		dropped = false
		ref := LeaseRef{LeaseID: leaseID}
		var state string
		found := false
		err := txn.QueryWithOptions(ctx, spanner.Statement{
			SQL:    `SELECT workspace_id, state FROM tr_lease@{FORCE_INDEX=tr_lease_by_id} WHERE lease_id = @l`,
			Params: map[string]any{"l": leaseID},
		}, spanner.QueryOptions{RequestTag: stagingTag}).Do(func(row *spanner.Row) error {
			found = true
			return row.Columns(&ref.Workspace, &state)
		})
		if err != nil || !found {
			return err
		}
		packs, err := readPacks(ctx, txn, ref, stagingTag)
		if err != nil {
			return err
		}
		won, done := false, false
		for _, p := range packs {
			for _, w := range p.Winners {
				if w.AuthorizationID == authorization && !won {
					won, done = true, p.WorkDoneAt.Valid
				}
			}
		}
		if (won && !done) || (!won && state != "closed") {
			return nil
		}
		mutations := make([]*spanner.Mutation, 0, len(digests))
		for _, d := range digests {
			mutations = append(mutations, spanner.Delete("tr_spike_staged", spanner.Key{authorization, d}))
		}
		dropped = true
		return txn.BufferWrite(mutations)
	}, spanner.TransactionOptions{TransactionTag: stagingTag})
	return dropped, err
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
