package store

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"
)

// Who decides at the edge (tr_fastpath_edge_decide): a workspace's edge
// nodes of a cloud may answer its authorizations for a region themselves
// only while its row for that cloud and region says so. A workspace with no
// row decides nowhere, and the table starts empty, so nobody decides. It
// chooses only who decides for a workspace the switch enables
// (tr_fastpath_workspace): turning a workspace off leaves its rows here as
// they are, and turning one on decides nothing here.
//
// One row is no workspace's: the wildcard, DecideWildcard, decides for a
// cloud and region for every workspace with no row of its own there
// (docs/design/fast-admission-widening.md section 6.1). A workspace's own
// row, on or off, wins over it, so an off row is an override under an on
// wildcard; with no wildcard row the table means what it meant before it.

// DecideWildcard is the wildcard row's workspace. No workspace has it as
// its id: fastpathctl takes ids beginning with a letter or digit.
const DecideWildcard = "*"

// Where a workspace's decision for a cloud and region comes from (section
// 6.1), as fastpathctl's decide status and the edge's count line name it.
const (
	DecideViaOwn      = "own"
	DecideViaWildcard = "wildcard"
	DecideViaNone     = "none"
)

// EffectiveDecide is section 6.1's rule for a workspace at a cloud and
// region, from its own row there and the wildcard's (each with whether it
// exists): its own row's decide, on or off; else the wildcard's; else off.
// It reports the decision and where it came from.
func EffectiveDecide(own, ownFound, wildcard, wildcardFound bool) (bool, string) {
	switch {
	case ownFound:
		return own, DecideViaOwn
	case wildcardFound:
		return wildcard, DecideViaWildcard
	}
	return false, DecideViaNone
}

// DecideRow is a workspace's row for a cloud and region.
type DecideRow struct {
	Workspace string
	Cloud     string
	Region    string
	Decide    bool
	ChangedAt time.Time
}

// decideName is the form of a cloud's or a region's name: lower case
// letters, digits and dashes, beginning with a letter.
var decideName = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)

// CheckDecideKey refuses a key the table cannot hold, or one no edge would
// ever look up, such as a cloud or region in upper case: such a row would
// decide nothing.
func CheckDecideKey(workspace, cloud, region string) error {
	switch {
	case workspace == "" || len(workspace) > 64:
		return errors.New("store: a workspace is 1 to 64 bytes")
	case len(cloud) > 16 || !decideName.MatchString(cloud):
		return fmt.Errorf("store: cloud %q is not a cloud's name, lower case, at most 16 bytes", cloud)
	case len(region) > 32 || !decideName.MatchString(region):
		return fmt.Errorf("store: region %q is not a region's name, lower case, at most 32 bytes", region)
	}
	return nil
}

// SetDecide sets whether the workspace's edge nodes of the cloud decide its
// authorizations for the region, and reports the commit's time, which the
// row keeps as changed_at.
func (s *Store) SetDecide(ctx context.Context, workspace, cloud, region string, decide bool) (time.Time, error) {
	if err := CheckDecideKey(workspace, cloud, region); err != nil {
		return time.Time{}, err
	}
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		return txn.BufferWrite([]*spanner.Mutation{spanner.InsertOrUpdate("tr_fastpath_edge_decide",
			[]string{"workspace_id", "cloud", "region", "decide", "changed_at"},
			[]any{workspace, cloud, region, decide, spanner.CommitTimestamp})})
	}, spanner.TransactionOptions{TransactionTag: tag("decide")})
	if err != nil {
		return time.Time{}, err
	}
	return resp.CommitTs.UTC(), nil
}

// ReadDecide reads the whole table, every workspace's rows, on or off, in
// key order, in one strong read, and the read's timestamp: the table is a
// row per workspace, cloud and region an operator has set, so it is small,
// and a node's copy of it is this read (docs/design/
// fast-admission-e2-decide.md, §9).
func (s *Store) ReadDecide(ctx context.Context) ([]DecideRow, time.Time, error) {
	ro := s.client.Single()
	defer ro.Close()
	rows := []DecideRow{}
	err := ro.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT workspace_id, cloud, region, decide, changed_at FROM tr_fastpath_edge_decide
		       ORDER BY workspace_id, cloud, region`,
	}, spanner.QueryOptions{RequestTag: tag("decide")}).Do(func(r *spanner.Row) error {
		var row DecideRow
		if err := r.Columns(&row.Workspace, &row.Cloud, &row.Region, &row.Decide, &row.ChangedAt); err != nil {
			return err
		}
		row.ChangedAt = row.ChangedAt.UTC()
		rows = append(rows, row)
		return nil
	})
	if err != nil {
		return nil, time.Time{}, err
	}
	read, err := readTimestamp(ro)
	return rows, read, err
}

// DecideOffAll stops every workspace deciding at the edge, in every cloud
// and region, in one transaction, and reports how many rows it turned off
// and the commit's time, which every row it turned off keeps as changed_at
// (DecideRestore reads it). The wildcard rows are rows like any other, so
// it turns them off too and nobody decides. It leaves the switch,
// tr_fastpath_workspace, and the leases as they are.
func (s *Store) DecideOffAll(ctx context.Context) (int64, time.Time, error) {
	var off int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_fastpath_edge_decide SET decide = FALSE, changed_at = PENDING_COMMIT_TIMESTAMP()
			       WHERE decide`,
		}, spanner.QueryOptions{RequestTag: tag("decide")})
		off = n
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("decide")})
	if err != nil {
		return 0, time.Time{}, err
	}
	return off, resp.CommitTs.UTC(), nil
}

// DecideRestore turns back on exactly the rows whose changed_at is at and
// that are off, in one transaction, and reports how many and the commit's
// time: the way back from a DecideOffAll, whose commit timestamp every row
// it turned off keeps (docs/design/fast-admission-widening.md section 6.2).
// A row turned off before or after it keeps its own changed_at, so an off
// override set before the kill stays off. The table is small and read whole
// every 5 s by each node (ReadDecide), so the statement's scan of it is the
// same read.
func (s *Store) DecideRestore(ctx context.Context, at time.Time) (int64, time.Time, error) {
	if at.IsZero() {
		return 0, time.Time{}, errors.New("store: decide restore needs a commit timestamp")
	}
	var on int64
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL: `UPDATE tr_fastpath_edge_decide SET decide = TRUE, changed_at = PENDING_COMMIT_TIMESTAMP()
			       WHERE NOT decide AND changed_at = @at`,
			Params: map[string]any{"at": at},
		}, spanner.QueryOptions{RequestTag: tag("decide")})
		on = n
		return err
	}, spanner.TransactionOptions{TransactionTag: tag("decide")})
	if err != nil {
		return 0, time.Time{}, err
	}
	return on, resp.CommitTs.UTC(), nil
}

// DecideDeleted is what DeleteDecide found and did: whether the row was
// there, whether it was the wildcard, whether it was removed, and the
// commit's time. Refused is a row left in place because removing it would
// make the workspace decide: an off row under an on wildcard for its cloud
// and region, deleted without followWildcard. FollowsWildcard is a
// workspace's own row removed, on or off, while the wildcard there is on:
// the workspace now decides under the wildcard.
type DecideDeleted struct {
	Found, Wildcard, Deleted, Refused, FollowsWildcard bool
	At                                                 time.Time
}

// DeleteDecide removes the workspace's row for the cloud and region, and
// reports what it found. Without followWildcard it only ever stops a
// workspace deciding: a workspace's own off row under an on wildcard for
// the cloud and region would start deciding once removed, so that row is
// left, nothing written, and Refused said; with followWildcard it is
// removed and the workspace follows the wildcard. An own row that is on
// is removed always (under an on wildcard it changes nothing, under none
// it stops the workspace deciding), and so is the wildcard row itself
// (DecideWildcard): every workspace without a row of its own stops
// deciding in that cloud and region. The rows are read in the delete's
// transaction, so a wildcard turned on meanwhile is seen. It checks only
// what the table can hold, not the workspace's form, so a row with a
// malformed workspace can still be removed.
func (s *Store) DeleteDecide(ctx context.Context, workspace, cloud, region string, followWildcard bool) (DecideDeleted, error) {
	if err := CheckDecideKey(workspace, cloud, region); err != nil {
		return DecideDeleted{}, err
	}
	var out DecideDeleted
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = DecideDeleted{Wildcard: workspace == DecideWildcard}
		read := func(w string) (bool, bool, error) {
			row, err := txn.ReadRowWithOptions(ctx, "tr_fastpath_edge_decide", spanner.Key{w, cloud, region},
				[]string{"decide"}, &spanner.ReadOptions{RequestTag: tag("decide")})
			if spanner.ErrCode(err) == codes.NotFound {
				return false, false, nil
			}
			if err != nil {
				return false, false, err
			}
			var decide bool
			err = row.Columns(&decide)
			return decide, true, err
		}
		decide, found, err := read(workspace)
		if err != nil || !found {
			return err
		}
		out.Found = true
		if !out.Wildcard {
			// The wildcard decides for the workspace once its own row is
			// gone: an off row would start it deciding, refused without
			// followWildcard; an on row leaves it deciding.
			wildcard, wildcardFound, err := read(DecideWildcard)
			if err != nil {
				return err
			}
			if !decide && wildcardFound && wildcard && !followWildcard {
				out.Refused = true
				return nil
			}
			out.FollowsWildcard = wildcardFound && wildcard
		}
		out.Deleted = true
		return txn.BufferWrite([]*spanner.Mutation{spanner.Delete("tr_fastpath_edge_decide",
			spanner.Key{workspace, cloud, region})})
	}, spanner.TransactionOptions{TransactionTag: tag("decide")})
	if err != nil {
		return DecideDeleted{}, err
	}
	out.At = resp.CommitTs.UTC()
	return out, nil
}
