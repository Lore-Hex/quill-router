package auditor

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// FleetKey is the ring key whose owner runs the fleet audit
// (docs/design/fast-admission-widening.md §7): of the members that take
// leases, the one ring.View.Owner picks for it, so the audit moves with a
// node's replacement and runs on one node of the fleet. As for a shard's
// owner, correctness does not rest on it: two nodes that both think they own
// the key both audit, and an audit only reads.
const FleetKey = "fleet-audit"

// The fleet audit's alert kinds. Its messages name the check, so it gives
// each alert its kind itself, as the edge does (writeAlertOf in
// internal/service): a policy each in infra/fastpath_alerts.tf, a row each in
// docs/runbooks/fastpath-alerts.md. A kind, once a policy names it, does not
// change.
const (
	// KindFleetAuditFault is a workspace whose rows do not keep what one of
	// the checks holds them to; the subject is the workspace, the message
	// the check and its problems.
	KindFleetAuditFault = "fleet_audit_fault"
	// KindFleetAuditStale is a workspace with a lease open or draining that
	// the audit has not audited within FleetConfig.Stale. The other way the
	// audit goes stale, no summary line at all, is seen by Cloud
	// Monitoring, not by a node (infra/fastpath_alerts.tf).
	KindFleetAuditStale = "fleet_audit_stale"
)

// The checks, as a fault names them: fastpathctl audit's, but for its
// -funded, which no row says. The counter reconciler's combined identity
// covers the credit rows across the fleet.
const (
	CheckIdentity    = "identity"     // store.CheckIdentity, the synchronous holds beside the leases'
	CheckWinners     = "winners"      // store.CheckWinners: one winner per authorization, the charges summed
	CheckBookedUsage = "booked_usage" // store.CheckBookedUsage: each shard's usage at least what was booked
	CheckSums        = "sums"         // store.AuditSums: the sums exact
)

// FleetSummaryLine is the line each pass writes once it ends, whatever it
// found, a pass with no workspace due included: the policy on its absence
// (infra/fastpath_alerts.tf) counts the lines by their beginning, up to the
// first count. due and audited are every workspace due and those whose
// checks all ran; leased_due and leased_audited, those with a lease open or
// draining; faults the fault lines written; failed the workspaces whose
// checks did not all run; stale the stale lines written; complete whether
// every workspace due was audited.
const FleetSummaryLine = "fastpath fleet audit: due=%d audited=%d leased_due=%d leased_audited=%d faults=%d " +
	"failed=%d stale=%d complete=%t took_ms=%d"

// FleetStore is what the fleet audit reads in Spanner; *store.Store has it.
type FleetStore interface {
	FleetDue(ctx context.Context, since time.Time) ([]store.FleetWorkspace, error)
	CheckIdentity(ctx context.Context, workspace string) ([]string, error)
	CheckWinners(ctx context.Context, workspace string) ([]string, error)
	CheckBookedUsage(ctx context.Context, workspace string) ([]string, error)
	AuditSums(ctx context.Context, workspace string) (store.Sums, error)
}

// FleetMembers is the fleet audit's view of the members; *ring.Watcher is
// one.
type FleetMembers interface {
	View() (ring.View, time.Time)
}

// FleetConfig is the fleet audit's.
type FleetConfig struct {
	Store   FleetStore
	Members FleetMembers
	// Self is the node's address, as its ring row names it.
	Self string
	// Alert tells a person of a fault or a stale workspace, the kind given.
	Alert func(kind, subject, what string)
	// Every is how often a pass begins, and bounds its length: a pass not
	// done when the next is due stops, and says so in its summary. Check is
	// how often the node asks whether it owns FleetKey, so a new owner
	// begins within Check of the ring's change.
	Every time.Duration
	Check time.Duration
	// Within is how recently a switch changed for its workspace to be due
	// though it is off, and to come second in a pass.
	Within time.Duration
	// Rate is how many workspaces a second a pass audits at most, and
	// Deadline bounds each workspace's checks together. A workspace whose
	// checks fail is not tried again within the pass.
	Rate     int
	Deadline time.Duration
	// Stale is how long a workspace with a lease may go unaudited before
	// it is told (KindFleetAuditStale), counted from the later of its last
	// audit and when this node first saw it leased while owning the key.
	Stale time.Duration
	Clock func() time.Time

	// The tests' own: how a pass waits between workspaces (a timer on the
	// wall clock) and where its lines go (the standard logger), each its
	// default when nil.
	sleep func(ctx context.Context, d time.Duration) error
	logf  func(format string, args ...any)
}

// Fleet is the fleet audit (docs/design/fast-admission-widening.md §7): on
// the node that owns FleetKey, a pass every Every over every workspace the
// switch names enabled or changed within Within, read from the switch's
// table and never from the leases, each workspace's rows held to
// fastpathctl audit's checks, each check one read-only transaction by the
// workspace's keys at low priority. Workspaces with a lease open or draining
// go first, then those whose switch changed, then the rest, each group the
// least recently audited first, so a pass that cannot reach them all
// rotates through them. A fault is an alert naming the workspace and the
// check; each pass ends with its summary line.
//
// What it remembers, the passes' times, each workspace's last audit and
// when each was first seen leased, is the tenure's: it is forgotten when
// the node finds it no longer owns the key, so a node that owns it again
// starts afresh, as the node that took it over did. Only Run's goroutine
// reads and writes it.
type Fleet struct {
	cfg  FleetConfig
	j    *journal
	logf func(format string, args ...any)

	// began is when the tenure's last pass began; zero before its first.
	began       time.Time
	audited     map[string]time.Time
	leasedSince map[string]time.Time
}

// NewFleet is the fleet audit with its configuration.
func NewFleet(cfg FleetConfig) (*Fleet, error) {
	if cfg.Store == nil || cfg.Members == nil || cfg.Self == "" || cfg.Alert == nil || cfg.Every <= 0 ||
		cfg.Check <= 0 || cfg.Within <= 0 || cfg.Rate < 1 || cfg.Deadline <= 0 || cfg.Stale <= 0 {
		return nil, errors.New("auditor: the fleet audit needs a store, the members, its own address, someone to " +
			"tell, an interval, an ownership check's interval, a window for changed switches, a rate, a deadline " +
			"and a stale allowance")
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	if cfg.sleep == nil {
		cfg.sleep = sleep
	}
	j := newJournal(cfg.logf)
	return &Fleet{cfg: cfg, j: j, logf: j.logf, audited: map[string]time.Time{}, leasedSince: map[string]time.Time{}}, nil
}

// Run asks every Check whether the node owns FleetKey, and while it does
// runs a pass once Every has passed since the last began, until ctx ends.
func (f *Fleet) Run(ctx context.Context) error {
	tk := time.NewTicker(f.cfg.Check)
	defer tk.Stop()
	for {
		f.round(ctx)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-tk.C:
		}
	}
}

// round is one of Run's checks: a pass if the node owns the key and one is
// due; the tenure forgotten if it does not.
func (f *Fleet) round(ctx context.Context) {
	if !f.owns() {
		f.began = time.Time{}
		clear(f.audited)
		clear(f.leasedSince)
		return
	}
	if now := f.cfg.Clock(); f.began.IsZero() || now.Sub(f.began) >= f.cfg.Every {
		f.pass(ctx, now)
	}
}

// owns says whether the node owns FleetKey in its latest view of the
// members: a node leaving, or not live, owns nothing.
func (f *Fleet) owns() bool {
	view, _ := f.cfg.Members.View()
	owner, ok := view.Owner(FleetKey)
	return ok && owner.Address == f.cfg.Self
}

// fleetGroup is a workspace's place in a pass: leased first, then a switch
// changed within the window, then the rest.
func fleetGroup(w store.FleetWorkspace, changedSince time.Time) int {
	switch {
	case w.Leased:
		return 0
	case !w.ChangedAt.Before(changedSince):
		return 1
	}
	return 2
}

// fleetOrder is the order a pass audits the workspaces due: by group, then
// the least recently audited first (one never audited before any), then by
// workspace.
func fleetOrder(due []store.FleetWorkspace, audited map[string]time.Time, changedSince time.Time) []store.FleetWorkspace {
	out := slices.Clone(due)
	slices.SortStableFunc(out, func(a, b store.FleetWorkspace) int {
		return cmp.Or(cmp.Compare(fleetGroup(a, changedSince), fleetGroup(b, changedSince)),
			audited[a.Workspace].Compare(audited[b.Workspace]), strings.Compare(a.Workspace, b.Workspace))
	})
	return out
}

// pass reads the workspaces due and audits them in fleetOrder, at most Rate
// a second, until they are done, Every has passed since it began, or the
// node no longer owns the key; then it tells of the stale workspaces, unless
// the key moved, and writes its summary. A read of the workspaces due that
// fails writes no summary, and is tried again at the next round.
func (f *Fleet) pass(ctx context.Context, began time.Time) {
	since := began.Add(-f.cfg.Within)
	dctx, cancel := context.WithTimeout(ctx, f.cfg.Deadline)
	due, err := f.cfg.Store.FleetDue(dctx, since)
	cancel()
	if err != nil {
		if ctx.Err() == nil {
			f.j.printf("fleet due", "auditor: fleet audit: reading the workspaces due failed: %s", errorClass(err))
		}
		return
	}
	f.began = began
	leased := map[string]bool{}
	for _, w := range due {
		if !w.Leased {
			continue
		}
		leased[w.Workspace] = true
		if _, ok := f.leasedSince[w.Workspace]; !ok {
			f.leasedSince[w.Workspace] = began
		}
	}
	for w := range f.leasedSince {
		if !leased[w] {
			delete(f.leasedSince, w)
		}
	}
	var audited, leasedAudited, faults, failed, stale int
	moved := false
	interval := time.Second / time.Duration(f.cfg.Rate)
	var last time.Time
	for _, w := range fleetOrder(due, f.audited, since) {
		// Each workspace's checks begin at least 1/Rate after the last's
		// began, however long those took.
		if !last.IsZero() && f.cfg.sleep(ctx, last.Add(interval).Sub(f.cfg.Clock())) != nil {
			break
		}
		if ctx.Err() != nil || f.cfg.Clock().Sub(began) >= f.cfg.Every {
			break
		}
		if !f.owns() {
			moved = true
			break
		}
		last = f.cfg.Clock()
		found, ok := f.audit(ctx, w.Workspace)
		faults += found
		if !ok {
			failed++
			continue
		}
		f.audited[w.Workspace] = f.cfg.Clock()
		audited++
		if w.Leased {
			leasedAudited++
		}
	}
	if !moved && ctx.Err() == nil {
		now := f.cfg.Clock()
		for _, w := range slices.Sorted(maps.Keys(leased)) {
			from := f.leasedSince[w]
			if at := f.audited[w]; at.After(from) {
				from = at
			}
			if d := now.Sub(from); d >= f.cfg.Stale {
				f.cfg.Alert(KindFleetAuditStale, w, fmt.Sprintf(
					"the fleet audit has not audited the workspace, which has a lease open or draining, for %v",
					d.Round(time.Minute)))
				stale++
			}
		}
	}
	f.logf(FleetSummaryLine, len(due), audited, len(leased), leasedAudited, faults, failed, stale, audited == len(due),
		f.cfg.Clock().Sub(began).Milliseconds())
}

// audit runs a workspace's checks under one Deadline, each one read-only
// transaction, and tells of each check whose rows break it. It returns the
// faults told and whether every check ran: a check that fails is logged by
// its class alone, as the auditor's other lines are, and not run again
// within the pass. A check the store refuses on what the rows hold (credit
// rows that are not shards 0 to n-1, sums past int64) is a fault.
func (f *Fleet) audit(ctx context.Context, workspace string) (faults int, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, f.cfg.Deadline)
	defer cancel()
	ok = true
	checks := []struct {
		name string
		run  func(context.Context, string) ([]string, error)
	}{
		{CheckIdentity, f.cfg.Store.CheckIdentity},
		{CheckWinners, f.cfg.Store.CheckWinners},
		{CheckBookedUsage, f.cfg.Store.CheckBookedUsage},
		{CheckSums, func(ctx context.Context, w string) ([]string, error) {
			_, err := f.cfg.Store.AuditSums(ctx, w)
			return nil, err
		}},
	}
	for _, c := range checks {
		problems, err := c.run(ctx, workspace)
		if errors.Is(err, store.ErrCreditRowsIncomplete) || errors.Is(err, store.ErrSumsPastInt64) {
			problems, err = []string{err.Error()}, nil
		}
		if err != nil {
			ok = false
			if ctx.Err() == nil || errors.Is(ctx.Err(), context.DeadlineExceeded) {
				f.j.printf("fleet "+c.name, "auditor: fleet audit: the %s check of a workspace failed: %s", c.name,
					errorClass(err))
			}
			continue
		}
		if len(problems) > 0 {
			f.cfg.Alert(KindFleetAuditFault, workspace, faultMessage(c.name, problems))
			faults++
		}
	}
	return faults, ok
}

// faultMessage is a fault's message: the check and its first problems, the
// rest counted, so one line stays readable however many leases broke it.
func faultMessage(check string, problems []string) string {
	const shown = 3
	msg := fmt.Sprintf("the fleet audit's %s check: %s", check, strings.Join(problems[:min(len(problems), shown)], "; "))
	if len(problems) > shown {
		msg += fmt.Sprintf(" (and %d more)", len(problems)-shown)
	}
	return msg
}

// sleep waits d, or until ctx ends.
func sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
