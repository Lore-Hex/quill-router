package storetest

import (
	"context"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"github.com/Lore-Hex/quill-router/fastpath/internal/switchsql"
)

// Writes that stand for others the tests cannot run: an older release's
// switch writes, which hold nothing, and the enabler's (PR 6 of
// docs/design/fast-admission-widening.md, section 11), whose own disable
// holds nothing either.

// switchOperation runs one of the fixture's operations
// (fastpath/testdata/switch_statements_v1.json) for a workspace in a
// transaction of its own.
func switchOperation(ctx context.Context, client *spanner.Client, name, workspace string) (time.Time, error) {
	f, err := switchsql.Load()
	if err != nil {
		return time.Time{}, err
	}
	op, err := f.Get(name)
	if err != nil {
		return time.Time{}, err
	}
	resp, err := client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := op.Apply(ctx, txn, map[string]any{"workspace": workspace}, spanner.QueryOptions{})
		return err
	}, spanner.TransactionOptions{})
	if err != nil {
		return time.Time{}, err
	}
	return resp.CommitTs.UTC(), nil
}

// UnheldDisable turns a workspace off with the fixture's disable_unheld:
// the switch and the revocation, and no hold, as the enabler's own disable
// writes it, and as a release older than holds wrote every disable.
func UnheldDisable(ctx context.Context, client *spanner.Client, workspace string) (time.Time, error) {
	return switchOperation(ctx, client, switchsql.DisableUnheld, workspace)
}

// OlderEnable turns a workspace on as a release older than holds did: the
// switch alone, leaving any hold of the workspace's as it is.
func OlderEnable(ctx context.Context, client *spanner.Client, workspace string) (time.Time, error) {
	at, err := client.Apply(ctx, []*spanner.Mutation{Enabled(workspace)})
	return at.UTC(), err
}

// OlderDisableAll turns every workspace off as a release older than holds
// did: the switch and the revocations, stamped with the commit timestamp,
// with no fleet hold and no kill row.
func OlderDisableAll(ctx context.Context, client *spanner.Client) (time.Time, error) {
	resp, err := client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		_, err := txn.BatchUpdate(ctx, []spanner.Statement{
			{SQL: `UPDATE tr_fastpath_workspace SET enabled = FALSE, changed_at = PENDING_COMMIT_TIMESTAMP() WHERE enabled`},
			{SQL: `UPDATE tr_lease SET revoked = TRUE, revoked_at = CURRENT_TIMESTAMP() WHERE state = 'open' AND NOT revoked`},
		})
		return err
	}, spanner.TransactionOptions{})
	if err != nil {
		return time.Time{}, err
	}
	return resp.CommitTs.UTC(), nil
}

// EnablerRun is what an enabler's run in apply mode wrote for the
// workspaces given, each taken as eligible (section 5.3). It stands for the
// enabler PR 6 builds, so the tests can say what its next run does.
type EnablerRun struct {
	Enabled, Disabled []string
	// Stopped is why a write wrote nothing: "fleet", "paused" or
	// "unarmed".
	Stopped string
}

// RunEnabler runs an enabler's writes for the workspaces given: each its
// own read-write transaction that first reads '*', '#pause', '#armed' and
// the workspace's own hold, and writes nothing while '*' or '#pause' stands
// or '#armed' does not; otherwise a workspace off with no hold of its own is
// turned on with the fixture's enable, and one on with a hold of its own is
// turned off with its disable_unheld, holding nothing.
func RunEnabler(ctx context.Context, client *spanner.Client, workspaces ...string) (EnablerRun, error) {
	f, err := switchsql.Load()
	if err != nil {
		return EnablerRun{}, err
	}
	enable, err := f.Get(switchsql.Enable)
	if err != nil {
		return EnablerRun{}, err
	}
	disable, err := f.Get(switchsql.DisableUnheld)
	if err != nil {
		return EnablerRun{}, err
	}
	var run EnablerRun
	for _, w := range workspaces {
		var did, stopped string
		_, err := client.ReadWriteTransactionWithOptions(ctx, func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
			did, stopped = "", ""
			rows := map[string]bool{}
			err := txn.Query(ctx, spanner.Statement{
				SQL:    `SELECT workspace_id FROM tr_fastpath_workspace_hold WHERE workspace_id IN UNNEST(@ids)`,
				Params: map[string]any{"ids": []string{"*", "#pause", "#armed", w}},
			}).Do(func(r *spanner.Row) error {
				var id string
				if err := r.Column(0, &id); err != nil {
					return err
				}
				rows[id] = true
				return nil
			})
			if err != nil {
				return err
			}
			switch {
			case rows["*"]:
				stopped = "fleet"
				return nil
			case rows["#pause"]:
				stopped = "paused"
				return nil
			case !rows["#armed"]:
				stopped = "unarmed"
				return nil
			}
			var on bool
			row, err := txn.ReadRow(ctx, "tr_fastpath_workspace", spanner.Key{w}, []string{"enabled"})
			switch {
			case spanner.ErrCode(err) == codes.NotFound:
			case err != nil:
				return err
			default:
				if err := row.Column(0, &on); err != nil {
					return err
				}
			}
			params := map[string]any{"workspace": w}
			switch {
			case !on && !rows[w]:
				did = "enabled"
				_, err = enable.Apply(ctx, txn, params, spanner.QueryOptions{})
			case on && rows[w]:
				did = "disabled"
				_, err = disable.Apply(ctx, txn, params, spanner.QueryOptions{})
			}
			return err
		}, spanner.TransactionOptions{})
		if err != nil {
			return EnablerRun{}, err
		}
		switch {
		case stopped != "":
			run.Stopped = stopped
		case did == "enabled":
			run.Enabled = append(run.Enabled, w)
		case did == "disabled":
			run.Disabled = append(run.Disabled, w)
		}
	}
	return run, nil
}
