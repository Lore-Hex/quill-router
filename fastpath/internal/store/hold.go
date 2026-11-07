package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/spanner"
	"google.golang.org/grpc/codes"

	"github.com/Lore-Hex/quill-router/fastpath/internal/switchsql"
)

// Holds (docs/design/fast-admission-widening.md, section 5.6): every stop
// but the enabler's holds its workspace, in tr_fastpath_workspace_hold, and
// the enabler never turns on a held workspace, nor writes at all while the
// fleet hold or the pause stands or before the arm. So the enabler never has
// to guess whether a row turned off was its own change or someone's stop.

// A workspace's hold's reasons: a stop's (SetWorkspace off), hold-backfill's
// and enabler resume's backfill's.
const (
	HoldOperator = "operator"
	HoldWatch    = "watch"
	HoldBackfill = "backfill"
	HoldPaused   = "paused"
)

// The three rows of the hold table that are no workspace's, since a
// workspace's id begins with a letter or a digit, and their reasons.
const (
	// FleetHold is disable-all's, removed by release-all.
	FleetHold = "*"
	// PauseRow is enabler pause's, removed only by enabler resume.
	PauseRow = "#pause"
	// ArmedRow is enabler arm's, removed by nothing.
	ArmedRow = "#armed"

	holdKill  = "kill"
	holdPause = "pause"
	holdArmed = "armed"
)

// Reason is why a stop holds its workspace: Kind, the hold's reason,
// HoldOperator or HoldWatch, and Detail, what the stop says of itself, such
// as a watch's tripped ceilings.
type Reason struct {
	Kind   string
	Detail string
}

// switchOps are the switch's writes, the fixture's
// (fastpath/testdata/switch_statements_v1.json); a test may swap them to
// hold SetWorkspace to what it executes.
var switchOps = mustLoadSwitch()

func mustLoadSwitch() switchsql.Fixture {
	f, err := switchsql.Load()
	if err != nil {
		panic(err)
	}
	return f
}

// checkWorkspace refuses no workspace, and one of the hold table's own rows
// given as a workspace: turning '#pause' on would remove the pause.
func checkWorkspace(workspace string) error {
	if workspace == "" {
		return errors.New("store: no workspace")
	}
	if workspace[0] == '*' || workspace[0] == '#' {
		return fmt.Errorf("store: %q is a row of the hold table that is no workspace's, not a workspace", workspace)
	}
	return nil
}

// holdRow is the insert of a hold, held at the commit timestamp.
func holdRow(workspace, reason, detail string) *spanner.Mutation {
	return spanner.Insert("tr_fastpath_workspace_hold", []string{"workspace_id", "reason", "detail", "held_at"},
		[]any{workspace, reason, spanner.NullString{StringVal: detail, Valid: detail != ""}, spanner.CommitTimestamp})
}

// HeldRow is a switch row with a hold of its own.
type HeldRow struct {
	Workspace string
	Enabled   bool
	ChangedAt time.Time
	Reason    string
	HeldAt    time.Time
}

// UnheldRow is a switch row turned off with no hold of its own: under a
// kill when its changed_at is a row of tr_fastpath_kill.
type UnheldRow struct {
	Workspace string
	ChangedAt time.Time
	UnderKill bool
}

// holdTables are the switch, the hold table and the kill table, each read
// whole in one transaction.
type holdTables struct {
	switches []switchRow
	holds    map[string]heldAs
	kills    []time.Time
	killed   map[int64]bool
}

type switchRow struct {
	workspace string
	enabled   bool
	changedAt time.Time
}

type heldAs struct {
	reason string
	heldAt time.Time
}

// readHoldTables reads the three tables whole, in the transaction: a
// read-write transaction's reads of them conflict with any switch write, hold
// or kill committed during it.
func readHoldTables(ctx context.Context, txn rowReader, operation string) (holdTables, error) {
	h := holdTables{holds: map[string]heldAs{}, killed: map[int64]bool{}}
	opts := spanner.QueryOptions{RequestTag: tag(operation)}
	err := txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT workspace_id, enabled, changed_at FROM tr_fastpath_workspace ORDER BY workspace_id`,
	}, opts).Do(func(r *spanner.Row) error {
		var w switchRow
		if err := r.Columns(&w.workspace, &w.enabled, &w.changedAt); err != nil {
			return err
		}
		w.changedAt = w.changedAt.UTC()
		h.switches = append(h.switches, w)
		return nil
	})
	if err != nil {
		return holdTables{}, err
	}
	err = txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT workspace_id, reason, held_at FROM tr_fastpath_workspace_hold`,
	}, opts).Do(func(r *spanner.Row) error {
		var w string
		var held heldAs
		if err := r.Columns(&w, &held.reason, &held.heldAt); err != nil {
			return err
		}
		held.heldAt = held.heldAt.UTC()
		h.holds[w] = held
		return nil
	})
	if err != nil {
		return holdTables{}, err
	}
	err = txn.QueryWithOptions(ctx, spanner.Statement{
		SQL: `SELECT kill_at FROM tr_fastpath_kill ORDER BY kill_at`,
	}, opts).Do(func(r *spanner.Row) error {
		var at time.Time
		if err := r.Column(0, &at); err != nil {
			return err
		}
		h.kills = append(h.kills, at.UTC())
		h.killed[at.UnixNano()] = true
		return nil
	})
	if err != nil {
		return holdTables{}, err
	}
	return h, nil
}

// unheld are the switch rows turned off with no hold of their own, whatever
// the fleet hold: only a workspace's own hold counts.
func (h holdTables) unheld() []UnheldRow {
	out := []UnheldRow{}
	for _, w := range h.switches {
		if _, held := h.holds[w.workspace]; !w.enabled && !held {
			out = append(out, UnheldRow{Workspace: w.workspace, ChangedAt: w.changedAt, UnderKill: h.killed[w.changedAt.UnixNano()]})
		}
	}
	return out
}

// enabledHeld are the switch rows enabled with a hold of their own, as an
// older release's enable leaves one (section 5.6).
func (h holdTables) enabledHeld() []HeldRow {
	out := []HeldRow{}
	for _, w := range h.switches {
		if held, ok := h.holds[w.workspace]; ok && w.enabled {
			out = append(out, HeldRow{Workspace: w.workspace, Enabled: true, ChangedAt: w.changedAt, Reason: held.reason,
				HeldAt: held.heldAt})
		}
	}
	return out
}

// at is the time of the row of the hold table, if there is one.
func (h holdTables) at(row string) *time.Time {
	held, ok := h.holds[row]
	if !ok {
		return nil
	}
	return &held.heldAt
}

// HoldReport is the holds as the enabler's report counts them (section 5.4),
// read whole in one strong snapshot.
type HoldReport struct {
	// Fleet, Pause and Armed are when '*', '#pause' and '#armed' were
	// written, nil for each absent.
	Fleet, Pause, Armed *time.Time
	// Kills are tr_fastpath_kill's rows, in order.
	Kills []time.Time
	// Holds counts the workspaces' own holds by reason.
	Holds map[string]int64
	// UnheldDisabled are the switch rows turned off with no hold of their
	// own, whatever their rank or eligibility: UnderKill of them whose
	// changed_at is a kill's, Other the rest.
	UnheldDisabled   []UnheldRow
	UnderKill, Other int64
	// EnabledHeld are the switch rows enabled with a hold of their own,
	// which enabler resume refuses.
	EnabledHeld []HeldRow
	ReadTS      time.Time
}

// Holds reads the hold report, read only.
func (s *Store) Holds(ctx context.Context) (HoldReport, error) {
	ro := s.client.ReadOnlyTransaction()
	defer ro.Close()
	h, err := readHoldTables(ctx, ro, "holds")
	if err != nil {
		return HoldReport{}, err
	}
	out := HoldReport{Fleet: h.at(FleetHold), Pause: h.at(PauseRow), Armed: h.at(ArmedRow), Kills: h.kills,
		Holds: map[string]int64{}, UnheldDisabled: h.unheld(), EnabledHeld: h.enabledHeld()}
	if out.Kills == nil {
		out.Kills = []time.Time{}
	}
	for w, held := range h.holds {
		if checkWorkspace(w) == nil {
			out.Holds[held.reason]++
		}
	}
	for _, u := range out.UnheldDisabled {
		if u.UnderKill {
			out.UnderKill++
		} else {
			out.Other++
		}
	}
	out.ReadTS, err = readTimestamp(ro)
	return out, err
}

// paused reads the pause's row in the transaction: when it was written, and
// whether it is there.
func paused(ctx context.Context, txn rowReader, operation string) (time.Time, bool, error) {
	row, err := txn.ReadRowWithOptions(ctx, "tr_fastpath_workspace_hold", spanner.Key{PauseRow}, []string{"held_at"},
		&spanner.ReadOptions{RequestTag: tag(operation)})
	if spanner.ErrCode(err) == codes.NotFound {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, err
	}
	var at time.Time
	if err := row.Column(0, &at); err != nil {
		return time.Time{}, false, err
	}
	return at.UTC(), true, nil
}

// present says whether the row of the hold table is there, read in the
// transaction.
func present(ctx context.Context, txn rowReader, row, operation string) (bool, error) {
	_, err := txn.ReadRowWithOptions(ctx, "tr_fastpath_workspace_hold", spanner.Key{row}, []string{"reason"},
		&spanner.ReadOptions{RequestTag: tag(operation)})
	if spanner.ErrCode(err) == codes.NotFound {
		return false, nil
	}
	return err == nil, err
}

// holdTxn runs a read-write transaction of the hold table's and returns its
// commit timestamp.
func (s *Store) holdTxn(ctx context.Context, operation string,
	f func(context.Context, *spanner.ReadWriteTransaction) error) (time.Time, error) {
	resp, err := s.client.ReadWriteTransactionWithOptions(ctx, f, spanner.TransactionOptions{TransactionTag: tag(operation)})
	if err != nil {
		return time.Time{}, err
	}
	return resp.CommitTs.UTC(), nil
}

// ReleaseResult is what Release did: Released when the workspace had a hold,
// now removed; Paused when it refused, writing nothing, for the pause.
type ReleaseResult struct {
	Released, Paused bool
	At               time.Time
}

// Release removes a workspace's hold without enabling it, so the enabler
// manages it again: its next run turns it on if it is eligible. It refuses,
// writing nothing, while the pause stands, read in its transaction: released
// during a pause, the row would be held again by resume's backfill.
func (s *Store) Release(ctx context.Context, workspace string) (ReleaseResult, error) {
	if err := checkWorkspace(workspace); err != nil {
		return ReleaseResult{}, err
	}
	var out ReleaseResult
	at, err := s.holdTxn(ctx, "release", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = ReleaseResult{}
		_, pausedNow, err := paused(ctx, txn, "release")
		if err != nil || pausedNow {
			out.Paused = pausedNow
			return err
		}
		n, err := txn.UpdateWithOptions(ctx, spanner.Statement{
			SQL:    `DELETE FROM tr_fastpath_workspace_hold WHERE workspace_id = @w`,
			Params: map[string]any{"w": workspace},
		}, spanner.QueryOptions{RequestTag: tag("release")})
		out.Released = n > 0
		return err
	})
	out.At = at
	return out, err
}

// ReleaseAllResult is what ReleaseAll did: Fleet when the fleet hold was
// there, Kills the kill rows removed; Paused when it refused, writing
// nothing, for the pause.
type ReleaseAllResult struct {
	Fleet  bool
	Kills  int64
	Paused bool
	At     time.Time
}

// ReleaseAll removes the fleet hold and every kill row in one transaction,
// undoing disable-all's hold: the enabler's next run turns on every row the
// kills stopped that is eligible and has no hold of its own. It refuses,
// writing nothing, while the pause stands, read in its transaction: released
// first, the kill rows would be gone before resume could leave the rows they
// stamped alone, and resume would hold the whole allow-list one row at a
// time. Under a pause the only order is enabler resume, then release-all.
func (s *Store) ReleaseAll(ctx context.Context) (ReleaseAllResult, error) {
	var out ReleaseAllResult
	at, err := s.holdTxn(ctx, "release-all", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = ReleaseAllResult{}
		_, pausedNow, err := paused(ctx, txn, "release-all")
		if err != nil || pausedNow {
			out.Paused = pausedNow
			return err
		}
		counts, err := txn.BatchUpdateWithOptions(ctx, []spanner.Statement{
			{SQL: `DELETE FROM tr_fastpath_workspace_hold WHERE workspace_id = @fleet`,
				Params: map[string]any{"fleet": FleetHold}},
			{SQL: `DELETE FROM tr_fastpath_kill WHERE TRUE`},
		}, spanner.QueryOptions{RequestTag: tag("release-all")})
		if err != nil {
			return err
		}
		out.Fleet, out.Kills = counts[0] > 0, counts[1]
		return nil
	})
	out.At = at
	return out, err
}

// PauseResult is what Pause did: Paused when it wrote the pause, and HeldAt
// the pause's time, the first pause's when one stood already.
type PauseResult struct {
	Paused bool
	HeldAt time.Time
}

// Pause inserts the pause, '#pause', held at the commit timestamp, the
// switch's clock: while it stands the enabler writes nothing. It is insert
// only: pausing while paused writes nothing and keeps the first pause's
// time, so a resume's backfill reaches back to the first pause.
func (s *Store) Pause(ctx context.Context) (PauseResult, error) {
	var out PauseResult
	at, err := s.holdTxn(ctx, "pause", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = PauseResult{}
		heldAt, pausedNow, err := paused(ctx, txn, "pause")
		if err != nil || pausedNow {
			out.HeldAt = heldAt
			return err
		}
		out.Paused = true
		return txn.BufferWrite([]*spanner.Mutation{holdRow(PauseRow, holdPause, "")})
	})
	if err == nil && out.Paused {
		out.HeldAt = at
	}
	return out, err
}

// ResumeResult is what Resume did. Paused says a pause stood. Refused are
// the rows enabled with a hold of their own, for which it refused, writing
// nothing. Otherwise it removed the pause and held, reason paused, the rows
// Held, every row turned off at or after the pause with no hold of its own
// but those under a kill, UnderKill of them, while AlreadyHeld of the rows
// turned off since the pause had a hold of their own. Its window is [From,
// At): from the pause's time to its own commit.
type ResumeResult struct {
	Paused      bool
	Refused     []HeldRow
	From, At    time.Time
	Held        []string
	UnderKill   int64
	AlreadyHeld int64
}

// Resume removes the pause, only in the same transaction as a backfill
// since it: a stop an older release made while paused wrote no hold, and no
// enabler write could commit during the pause, so every row turned off since
// it with no hold of its own is an operator's stop, and is held, reason
// paused. The rows whose changed_at is a kill row's are left to that kill,
// so release-all undoes the kill, and only it. It refuses, writing nothing,
// while any row is enabled with a hold of its own, as an older release's
// enable leaves one: the enabler would read it held and turn it off. It
// counts each workspace's own holds only, never the fleet hold.
func (s *Store) Resume(ctx context.Context) (ResumeResult, error) {
	var out ResumeResult
	at, err := s.holdTxn(ctx, "resume", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = ResumeResult{Held: []string{}, Refused: []HeldRow{}}
		h, err := readHoldTables(ctx, txn, "resume")
		if err != nil {
			return err
		}
		from := h.at(PauseRow)
		if from == nil {
			return nil
		}
		out.Paused, out.From = true, *from
		if out.Refused = h.enabledHeld(); len(out.Refused) > 0 {
			return nil
		}
		var ms []*spanner.Mutation
		for _, w := range h.switches {
			if w.enabled || w.changedAt.Before(out.From) {
				continue
			}
			_, held := h.holds[w.workspace]
			switch {
			case held:
				out.AlreadyHeld++
			case h.killed[w.changedAt.UnixNano()]:
				out.UnderKill++
			default:
				out.Held = append(out.Held, w.workspace)
				ms = append(ms, holdRow(w.workspace, HoldPaused, ""))
			}
		}
		ms = append(ms, spanner.Delete("tr_fastpath_workspace_hold", spanner.Key{PauseRow}))
		return txn.BufferWrite(ms)
	})
	out.At = at
	return out, err
}

// ArmResult is what Arm did: Armed when the arm stands, Already when it
// stood before; Unheld, the rows turned off with no hold of their own for
// which it refused, writing nothing.
type ArmResult struct {
	Armed, Already bool
	Unheld         []UnheldRow
	At             time.Time
}

// Arm inserts '#armed', before which the enabler writes nothing, and refuses
// unless every row turned off has a hold of its own: WS2's gate, enforced.
// The check and the insert are one read-write transaction that reads the
// switch and the hold table whole, so a stop committed during it is either
// seen by the check or ordered after the arm, and then held by its own
// disable. Nothing removes '#armed'.
func (s *Store) Arm(ctx context.Context) (ArmResult, error) {
	var out ArmResult
	at, err := s.holdTxn(ctx, "arm", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = ArmResult{Unheld: []UnheldRow{}}
		h, err := readHoldTables(ctx, txn, "arm")
		if err != nil {
			return err
		}
		if s.armChecked != nil {
			s.armChecked()
		}
		if h.at(ArmedRow) != nil {
			out.Armed, out.Already = true, true
			return nil
		}
		if out.Unheld = h.unheld(); len(out.Unheld) > 0 {
			return nil
		}
		out.Armed = true
		return txn.BufferWrite([]*spanner.Mutation{holdRow(ArmedRow, holdArmed, "")})
	})
	out.At = at
	return out, err
}

// BackfillResult is what HoldBackfill did: Armed when it refused, writing
// nothing, for the arm; else Held, the rows it held.
type BackfillResult struct {
	Armed bool
	Held  []string
	At    time.Time
}

// HoldBackfill holds, reason backfill, every row turned off with no hold of
// its own, in one transaction; run again, it writes nothing. Rows turned off
// before holds existed have none, and the include list is exempt from the
// rank, so the enabler would turn them on again. It refuses once the arm
// stands: after it, the rows turned off with no hold include the enabler's
// own, which a backfill would wrongly hold.
func (s *Store) HoldBackfill(ctx context.Context) (BackfillResult, error) {
	var out BackfillResult
	at, err := s.holdTxn(ctx, "hold-backfill", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = BackfillResult{Held: []string{}}
		h, err := readHoldTables(ctx, txn, "hold-backfill")
		if err != nil {
			return err
		}
		if h.at(ArmedRow) != nil {
			out.Armed = true
			return nil
		}
		var ms []*spanner.Mutation
		for _, u := range h.unheld() {
			out.Held = append(out.Held, u.Workspace)
			ms = append(ms, holdRow(u.Workspace, HoldBackfill, ""))
		}
		return txn.BufferWrite(ms)
	})
	out.At = at
	return out, err
}

// StampedResult is what ReleaseStamped found and did: Workspaces, the holds
// of reason paused on rows whose changed_at is the stamp; Applied when it
// removed them; Paused when it refused to, writing nothing, for the pause.
type StampedResult struct {
	Workspaces []string
	Applied    bool
	Paused     bool
	At         time.Time
}

// ReleaseStamped finds the holds of reason paused on switch rows whose
// changed_at is exactly at, the stamp an older release's disable-all printed
// and resume held one by one, and with apply removes them, in one
// transaction, so the enabler manages those rows again. A hold of any other
// reason, or on a row of another stamp, is left as it is. Without apply it
// writes nothing. As Release, it refuses to write while the pause stands.
func (s *Store) ReleaseStamped(ctx context.Context, at time.Time, apply bool) (StampedResult, error) {
	if at.IsZero() {
		return StampedResult{}, errors.New("store: no stamp")
	}
	find := func(ctx context.Context, txn rowReader) ([]string, error) {
		out := []string{}
		err := txn.QueryWithOptions(ctx, spanner.Statement{
			SQL: `SELECT w.workspace_id FROM tr_fastpath_workspace w
			        JOIN tr_fastpath_workspace_hold h ON h.workspace_id = w.workspace_id
			       WHERE w.changed_at = @at AND h.reason = @paused
			       ORDER BY w.workspace_id`,
			Params: map[string]any{"at": at, "paused": HoldPaused},
		}, spanner.QueryOptions{RequestTag: tag("release-stamped")}).Do(func(r *spanner.Row) error {
			var w string
			if err := r.Column(0, &w); err != nil {
				return err
			}
			out = append(out, w)
			return nil
		})
		return out, err
	}
	if !apply {
		ro := s.client.ReadOnlyTransaction()
		defer ro.Close()
		found, err := find(ctx, ro)
		if err != nil {
			return StampedResult{}, err
		}
		_, pausedNow, err := paused(ctx, ro, "release-stamped")
		if err != nil {
			return StampedResult{}, err
		}
		read, err := readTimestamp(ro)
		return StampedResult{Workspaces: found, Paused: pausedNow, At: read}, err
	}
	var out StampedResult
	commit, err := s.holdTxn(ctx, "release-stamped", func(ctx context.Context, txn *spanner.ReadWriteTransaction) error {
		out = StampedResult{}
		found, err := find(ctx, txn)
		if err != nil {
			return err
		}
		out.Workspaces = found
		_, pausedNow, err := paused(ctx, txn, "release-stamped")
		if err != nil || pausedNow {
			out.Paused = pausedNow
			return err
		}
		var ms []*spanner.Mutation
		for _, w := range found {
			ms = append(ms, spanner.Delete("tr_fastpath_workspace_hold", spanner.Key{w}))
		}
		out.Applied = true
		return txn.BufferWrite(ms)
	})
	out.At = commit
	return out, err
}
