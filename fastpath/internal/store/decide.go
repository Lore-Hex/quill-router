package store

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"time"

	"cloud.google.com/go/spanner"
)

// Who decides at the edge (tr_fastpath_edge_decide): a workspace's edge
// nodes of a cloud may answer its authorizations for a region themselves
// only while its row for that cloud and region says so. A workspace with no
// row decides nowhere, and the table starts empty, so nobody decides. It
// chooses only who decides for a workspace the switch enables
// (tr_fastpath_workspace): turning a workspace off leaves its rows here as
// they are, and turning one on decides nothing here.

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
// and the commit's time. It leaves the switch, tr_fastpath_workspace, and
// the leases as they are.
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
