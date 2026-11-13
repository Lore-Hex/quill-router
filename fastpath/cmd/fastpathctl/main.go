// Command fastpathctl turns the fast path on or off for a workspace, and
// says whether one turned off is done (docs/design/fast-admission-
// production-rollout.md, W1 and W2).
//
//	fastpathctl -database projects/P/instances/I/databases/D enable WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D disable WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D status WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D disable-all
//	fastpathctl -database projects/P/instances/I/databases/D node ADDRESS
//	fastpathctl -database projects/P/instances/I/databases/D [-funded MICRO] audit WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D decide on|off|status WORKSPACE [-cloud gcp] [-region us-central1]
//	fastpathctl -database D decide delete WORKSPACE [-cloud gcp] [-region us-central1] [-follow-wildcard]
//	fastpathctl -database D decide wildcard on|off|status [-cloud gcp] [-region us-central1]
//	fastpathctl -database projects/P/instances/I/databases/D decide-off-all
//	fastpathctl -database projects/P/instances/I/databases/D decide-restore -at TS
//	fastpathctl -database projects/P/instances/I/databases/D ready WORKSPACE [-required-tier N] [-floor F] [-allowance-tier2 A2] [-allowance-tier1 A1]
//	fastpathctl -database projects/P/instances/I/databases/D release WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D release-all
//	fastpathctl -database projects/P/instances/I/databases/D enabler pause|resume|arm
//	fastpathctl -database projects/P/instances/I/databases/D release-stamped TS [-yes]
//	fastpathctl -database projects/P/instances/I/databases/D hold-backfill
//	fastpathctl -database projects/P/instances/I/databases/D holds
//
// disable revokes the workspace's open leases with the switch, and
// disable-all turns every workspace off and revokes every open lease, the one
// switch that empties the allow-list. Each holds what it stops, in the same
// transaction (docs/design/fast-admission-widening.md, section 5.6), so the
// enabler never turns it on again: disable holds the workspace, reason
// operator, and disable-all writes the fleet hold '*' and a kill row whose
// time, the at it prints, is the changed_at of every row it turned off.
// enable removes the workspace's hold with the switch. status reads
// the workspace's rows by its keys, read only, and exits 0 once it is off
// with no lease open or draining, nothing its leases' donors hold, no pack's
// work pending and no staged record, and 3 until then: turning a workspace
// off waits on it before any node stops (W2). node reads a node's row and the
// open leases it owns, and exits 0 once it is marked leaving and owns none,
// so a deploy may stop it, and 3 until then (W8). audit reads a workspace's
// rows in one snapshot and holds them to the design's identity (§4.7: each
// credit shard's reservation is its live leases' donors' remaining
// allocation, each lease's allocation and consumption its donors' sums, and
// the debt marks as the headroom has them), to each shard's usage not below
// what the retained leases booked on it, and to each lease's retained packs: one winner
// per authorization, their charges summing to what the lease booked; read
// only, with the
// workspace's status and sums (credits, usage, reservations, leases by state,
// what the leases booked) beside: exit 0 when the rows keep them, 3 with each
// way they do not, after every run of the load (rollout P1). One thing no
// row says is the operator's to assert: -funded MICRO holds the credit rows'
// total credits to what funded the workspace. A command whose answer cannot
// be written exits 1.
//
// decide sets or reads who decides the workspace's authorizations at the
// edge (tr_fastpath_edge_decide): on, its edge nodes of the cloud answer
// them for the region themselves, off they pass them on. A workspace with
// no row of its own follows the wildcard row of the cloud and region, and
// with no wildcard row there decides nowhere; its own row, on or off, wins
// over the wildcard (docs/design/fast-admission-widening.md section 6), so
// decide off is an override that holds under the wildcard. The cloud is gcp
// and the region us-central1 unless -cloud or -region, before the command
// or after the workspace, say otherwise. decide status reports the
// workspace's effective answer there, by its own row or the wildcard, with
// via (own, wildcard or none) and every row of the workspace's beside: exit
// 0 while it does not decide in that cloud and region and 3 while it does.
// decide delete removes the workspace's row for the cloud and region, exit 0
// when there was one and 3 when there was none, and only ever stops one
// deciding: it refuses, exit 2 and nothing written, to remove an off row
// while the wildcard there is on, since the workspace would then decide,
// unless -follow-wildcard, which removes it and says so. decide delete '*'
// removes the wildcard row itself, so every workspace without a row of its
// own stops deciding there. decide wildcard on, off and status set and read
// the wildcard row (status exits 3 while it is on); on and off write only
// gcp/us-central1, the one cloud and region the widening covers, and refuse
// any other, exit 2; decide wildcard off is the same stop as deleting it
// and keeps the row for decide-restore.
// decide-off-all turns every row off, the wildcard's too, and prints the
// commit timestamp they all keep; decide-restore -at that timestamp turns
// back on exactly the rows whose changed_at it is and that are off, exit 3
// when there were none, so an override turned off before the kill stays
// off. None of them touches the switch or the leases: disable is still what
// turns a workspace off.
//
// ready asks the store whether a grant could reach the workspace with the
// fleet's money settings (docs/design/fast-admission-widening.md, (b) and
// section 8.2), read only and by the grant's own check
// (store.GrantReadiness): enabled, not in debt, not paused, every credit
// shard at -required-tier or above and not latched, the workspace's tier
// one with an allowance, and headroom above -floor. Each defaults to the
// node's own default, today's values, and is held to the node's bounds. It
// exits 0 when a grant could reach the workspace, 3 when one would be
// refused, with the refusal beside, and 2 for a setting out of bounds,
// before the store is opened. Only a grant weighs an allowance against a
// lease's amount, so ready asks only whether the workspace's tier has one:
// tier 3's always does, and -allowance-tier2 and -allowance-tier1, the
// node's, say whether tiers 2 and 1 do (0, the default, is none).
//
// Holds are what the enabler reads before each of its writes (section 5.6).
// release removes a workspace's hold without enabling it, exit 0, or 3 when
// it had none; it refuses, exit 2 and nothing written, while the enabler is
// paused ("resume first"). release-all removes the fleet hold and every kill
// row in one transaction, exit 0, or 3 when there was neither, and refuses
// in the same way under a pause. enabler pause holds the enabler, so it
// writes nothing until resumed; pausing while paused keeps the first pause's
// time. enabler resume removes the pause only in the same transaction as a
// backfill since it: every row turned off at or after the pause with no hold
// of its own is held, reason paused, but those a kill stopped, which stay the
// kill's; it prints its window [from, until), and how many rows it held, how
// many it left to a kill and how many already had a hold. It exits 3 when
// there is no pause, and refuses, exit 2 and nothing written, while any row
// is enabled with a hold of its own, printing them: enable or disable each
// first. enabler arm lets the enabler write, and refuses, exit 2, unless every
// row turned off has a hold of its own, printing those that do not.
// release-stamped TS prints the holds of reason paused on rows whose
// changed_at is exactly TS, as an older release's disable-all stamped them,
// and removes them only with -yes; exit 3 when there are none. It refuses to
// write under a pause, as release does. hold-backfill holds, reason backfill,
// every row turned off with no hold of its own, and refuses, exit 2, once
// the enabler is armed. holds prints the hold table, the kills, the rows
// enabled and held, and every row turned off with no hold of its own
// (unheld_disabled, under_kill and other as the enabler's report has them),
// read only: exit 0 when there is none of the last, 3 while there is.
//
// Flags go before the command, or for decide and ready after the workspace
// too: Go's flags end at the command, so a word after it is the workspace or
// address however it looks. A WORKSPACE is a workspace's id as the routing
// snapshot takes one, beginning with a letter or digit, and an ADDRESS a
// node's host:port; anything else, a flag such as -h among them, is refused
// with the usage, exit 2, before the store is opened. decide delete alone
// takes any workspace the table can hold, so a row written under a malformed
// one can be removed, and '*', the wildcard row. -follow-wildcard is decide
// delete's alone, -at decide-restore's, after the command, and -yes
// release-stamped's, after TS. enable and release take a workspace's id
// alone, so never '*', '#pause' or '#armed', the hold table's own rows.
// With SPANNER_EMULATOR_HOST set, the client reaches the emulator instead.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"regexp"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// notDone is status's exit code for a workspace turned off with something of
// the fast path left of it, decide status's for a workspace that decides,
// and ready's for a workspace a grant would refuse.
const notDone = 3

func main() {
	code, err := run(context.Background(), os.Args[1:], os.Stdout, open)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fastpathctl:", err)
	}
	os.Exit(code)
}

// open is the store at the database with the configuration, closed by its
// second result.
func open(ctx context.Context, database string, cfg store.Config) (*store.Store, func(), error) {
	client, err := spanner.NewClient(ctx, database)
	if err != nil {
		return nil, nil, err
	}
	s, err := store.New(client, cfg)
	if err != nil {
		client.Close()
		return nil, nil, err
	}
	return s, client.Close, nil
}

// workspaceForm is the routing snapshot's form of a workspace's id
// (internal/routingsnapshot/edgepolicy.go), beginning with a letter or a
// digit, so a flag after the command is not taken for a workspace.
var workspaceForm = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,63}$`)

// addressForm is a node's address, host:port, its host a name, an IPv4
// address or a bracketed IPv6 one, beginning with a letter, a digit or the
// bracket, so a flag after the command is not taken for an address.
var addressForm = regexp.MustCompile(`^[A-Za-z0-9\[][A-Za-z0-9._:\[\]\-]{0,254}$`)

// The one cloud and region decide wildcard on and off write
// (docs/design/fast-admission-widening.md section 4(c): the AWS and Azure
// planes and every region but us-central1 stay out). status reads any, and
// decide delete '*' removes any, both the stop direction.
const (
	wildcardCloud  = "gcp"
	wildcardRegion = "us-central1"
)

// decideStatus is decide status's answer from the whole table and whether
// the workspace decides in the cloud and region by section 6.1's rule
// (store.EffectiveDecide): its own row there, on or off, else the
// wildcard's, else off, with via naming which. For the wildcard itself
// (decide wildcard status) its own row is the wildcard's, via own.
func decideStatus(all []store.DecideRow, read time.Time, workspace, cloud, region string) (map[string]any, bool) {
	rows := []store.DecideRow{}
	var own, wildcard *store.DecideRow
	for i, r := range all {
		if r.Workspace == workspace {
			rows = append(rows, r)
		}
		if r.Cloud != cloud || r.Region != region {
			continue
		}
		switch r.Workspace {
		case workspace:
			own = &all[i]
		case store.DecideWildcard:
			wildcard = &all[i]
		}
	}
	answer := map[string]any{"workspace": workspace, "cloud": cloud, "region": region, "found": own != nil,
		"rows": rows, "wildcard": nil, "read_at": read}
	var ownOn, wildcardOn bool
	if own != nil {
		ownOn = own.Decide
		answer["changed_at"] = own.ChangedAt
	}
	if wildcard != nil {
		wildcardOn = wildcard.Decide
		answer["wildcard"] = map[string]any{"decide": wildcard.Decide, "changed_at": wildcard.ChangedAt}
	}
	decide, via := store.EffectiveDecide(ownOn, own != nil, wildcardOn, wildcard != nil)
	answer["decide"], answer["via"] = decide, via
	return answer, decide
}

// run is the command, its exit code and what went wrong.
func run(ctx context.Context, args []string, out io.Writer,
	open func(context.Context, string, store.Config) (*store.Store, func(), error)) (int, error) {
	fs := flag.NewFlagSet("fastpathctl", flag.ContinueOnError)
	database := fs.String("database", "", "the database: projects/P/instances/I/databases/D")
	timeout := fs.Duration("timeout", 30*time.Second, "how long the command may take")
	funded := fs.Int64("funded", -1, "audit: the micro-credits that funded the workspace, which its credit rows must sum to")
	cloud := fs.String("cloud", "gcp", "decide: the edge plane's cloud")
	region := fs.String("region", "us-central1", "decide: the region of the authorizations")
	followWildcard := fs.Bool("follow-wildcard", false,
		"decide delete: remove an off row though the wildcard is on, so the workspace follows it")
	restoreAt := fs.String("at", "", "decide-restore: the commit timestamp decide-off-all printed (RFC 3339)")
	yes := fs.Bool("yes", false, "release-stamped: release the holds it finds, rather than only print them")
	cfg := service.Defaults()
	fs.Var(service.Money{P: &cfg.Store.RequiredTier}, "required-tier",
		"ready: the trust tier the fleet's leases need (the node's -required-tier)")
	fs.Var(service.Money{P: &cfg.Store.Floor}, "floor",
		"ready: the headroom the fleet's grants leave outside leases, in microdollars (the node's -floor)")
	fs.Var(service.Money{P: &cfg.Store.AllowanceTier2}, "allowance-tier2",
		"ready: a tier-2 workspace's allowance, in microdollars, 0 for none (the node's -allowance-tier2)")
	fs.Var(service.Money{P: &cfg.Store.AllowanceTier1}, "allowance-tier1",
		"ready: a tier-1 workspace's allowance, in microdollars, 0 for none (the node's -allowance-tier1)")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0, nil
		}
		return 2, err
	}
	usage := errors.New("usage: fastpathctl -database D enable|disable|status|audit WORKSPACE, node|leave|closed ADDRESS, " +
		"decide on|off|status WORKSPACE [-cloud C] [-region R], decide delete WORKSPACE [-cloud C] [-region R] " +
		"[-follow-wildcard], decide wildcard on|off|status [-cloud C] [-region R], " +
		"ready WORKSPACE [-required-tier N] [-floor F] [-allowance-tier2 A2] [-allowance-tier1 A1], " +
		"release WORKSPACE, enabler pause|resume|arm, " +
		"release-stamped TS [-yes], disable-all, release-all, hold-backfill, holds, decide-off-all or decide-restore -at TS")
	if *database == "" || fs.NArg() < 1 {
		return 2, usage
	}
	command, workspace := fs.Arg(0), fs.Arg(1)
	verb, wildcardVerb := "", ""
	if command == "decide" {
		if fs.NArg() < 3 {
			return 2, usage
		}
		verb, workspace = fs.Arg(1), fs.Arg(2)
		if verb == "wildcard" {
			// decide wildcard on|off|status: the row of no workspace.
			wildcardVerb, workspace = fs.Arg(2), store.DecideWildcard
		}
		// -cloud, -region and -follow-wildcard may follow the workspace.
		if err := fs.Parse(fs.Args()[3:]); err != nil {
			return 2, err
		}
		if fs.NArg() != 0 {
			return 2, usage
		}
	}
	if command == "decide-restore" {
		// -at follows the command.
		if err := fs.Parse(fs.Args()[1:]); err != nil {
			return 2, err
		}
		if fs.NArg() != 0 {
			return 2, usage
		}
	}
	// -follow-wildcard is decide delete's and -at decide-restore's: on any
	// other command each would be ignored, which an operator could misread.
	if *followWildcard && (command != "decide" || verb != "delete") {
		return 2, fmt.Errorf("-follow-wildcard is decide delete's alone: %w", usage)
	}
	var restore time.Time
	if command == "decide-restore" {
		at, err := time.Parse(time.RFC3339Nano, *restoreAt)
		if err != nil || at.IsZero() {
			return 2, fmt.Errorf("decide-restore -at %q is not decide-off-all's printed at: %w", *restoreAt, usage)
		}
		restore = at
	} else if *restoreAt != "" {
		return 2, fmt.Errorf("-at is decide-restore's alone: %w", usage)
	}
	var stamp time.Time
	if command == "release-stamped" {
		if fs.NArg() < 2 {
			return 2, usage
		}
		at, err := time.Parse(time.RFC3339Nano, fs.Arg(1))
		if err != nil || at.IsZero() {
			return 2, fmt.Errorf("release-stamped %q is not a disable-all's printed at: %w", fs.Arg(1), usage)
		}
		stamp = at
		// -yes follows the stamp.
		if err := fs.Parse(fs.Args()[2:]); err != nil {
			return 2, err
		}
		if fs.NArg() != 0 {
			return 2, usage
		}
	}
	// -yes is release-stamped's: on any other command it would be ignored.
	if *yes && command != "release-stamped" {
		return 2, fmt.Errorf("-yes is release-stamped's alone: %w", usage)
	}
	if command == "ready" {
		if fs.NArg() < 2 {
			return 2, usage
		}
		// The money settings may follow the workspace.
		if err := fs.Parse(fs.Args()[2:]); err != nil {
			return 2, err
		}
		if fs.NArg() != 0 {
			return 2, usage
		}
	}
	// The money settings are ready's, and held to the node's bounds whatever
	// the command, so none opens a store the node would refuse.
	if err := service.CheckMoney(cfg); err != nil {
		return 2, err
	}
	switch {
	case command == "disable-all" || command == "decide-off-all" || command == "release-all" ||
		command == "hold-backfill" || command == "holds":
		if fs.NArg() != 1 {
			return 2, usage
		}
	case command == "enabler":
		if fs.NArg() != 2 || fs.Arg(1) != "pause" && fs.Arg(1) != "resume" && fs.Arg(1) != "arm" {
			return 2, fmt.Errorf("enabler takes pause, resume or arm: %w", usage)
		}
	case command == "decide-restore" || command == "release-stamped":
	case command == "decide" && verb == "wildcard":
		if wildcardVerb != "on" && wildcardVerb != "off" && wildcardVerb != "status" {
			return 2, fmt.Errorf("decide wildcard %q is not on, off or status: %w", wildcardVerb, usage)
		}
		if wildcardVerb != "status" && (*cloud != wildcardCloud || *region != wildcardRegion) {
			return 2, fmt.Errorf("decide wildcard %s: the wildcard row names only %s/%s, not %s/%s", wildcardVerb,
				wildcardCloud, wildcardRegion, *cloud, *region)
		}
		if err := store.CheckDecideKey(workspace, *cloud, *region); err != nil {
			return 2, err
		}
	case command == "decide":
		if verb != "on" && verb != "off" && verb != "status" && verb != "delete" {
			return 2, fmt.Errorf("decide %q is not on, off, status, delete or wildcard", verb)
		}
		if verb != "delete" && !workspaceForm.MatchString(workspace) {
			return 2, fmt.Errorf("%q is not a workspace's id: %w", workspace, usage)
		}
		if err := store.CheckDecideKey(workspace, *cloud, *region); err != nil {
			return 2, err
		}
	case command == "ready":
		if !workspaceForm.MatchString(workspace) {
			return 2, fmt.Errorf("%q is not a workspace's id: %w", workspace, usage)
		}
	case command != "enable" && command != "disable" && command != "status" && command != "node" && command != "leave" &&
		command != "closed" && command != "audit" && command != "release":
		return 2, fmt.Errorf("%q is not enable, disable, release, status, audit, node, leave, closed, decide, ready, "+
			"enabler, release-stamped, disable-all, release-all, hold-backfill, holds, decide-off-all or decide-restore",
			command)
	case fs.NArg() != 2 || workspace == "":
		return 2, usage
	case (command == "node" || command == "leave" || command == "closed") && !addressForm.MatchString(workspace):
		return 2, fmt.Errorf("%q is not a node's address: %w", workspace, usage)
	case command != "node" && command != "leave" && command != "closed" && !workspaceForm.MatchString(workspace):
		return 2, fmt.Errorf("%q is not a workspace's id: %w", workspace, usage)
	}
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	s, closeStore, err := open(ctx, *database, cfg.Store)
	if err != nil {
		return 1, err
	}
	defer closeStore()
	var answer map[string]any
	code := 0
	switch command {
	case "disable-all":
		off, revoked, at, err := s.DisableAll(ctx)
		if err != nil {
			return 1, err
		}
		// The kill's row is at, the changed_at of every row it turned off.
		answer = map[string]any{"workspaces_turned_off": off, "revoked_leases": revoked, "fleet_hold": true,
			"kill_at": at, "at": at}
	case "release", "release-all", "enabler", "release-stamped", "hold-backfill", "holds":
		return holdCommand(ctx, s, command, fs.Arg(1), stamp, *yes, out)
	case "decide-off-all":
		off, at, err := s.DecideOffAll(ctx)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"rows_turned_off": off, "at": at}
	case "decide-restore":
		on, at, err := s.DecideRestore(ctx, restore)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"rows_turned_on": on, "restored": restore, "at": at}
		if on == 0 {
			code = notDone
		}
	case "decide":
		if verb == "delete" {
			got, err := s.DeleteDecide(ctx, workspace, *cloud, *region, *followWildcard)
			if err != nil {
				return 1, err
			}
			answer = map[string]any{"workspace": workspace, "cloud": *cloud, "region": *region, "deleted": got.Deleted,
				"wildcard": got.Wildcard, "follows_wildcard": got.FollowsWildcard, "refused": got.Refused, "at": got.At}
			switch {
			case got.Refused:
				// Written before the refusal, so the caller sees what was
				// left; nothing was removed.
				if err := json.NewEncoder(out).Encode(answer); err != nil {
					return 1, err
				}
				return 2, fmt.Errorf("the wildcard for %s/%s is on: removing %s's off row would make it decide; "+
					"-follow-wildcard removes it anyway", *cloud, *region, workspace)
			case !got.Found:
				code = notDone
			}
			break
		}
		if verb == "wildcard" && wildcardVerb != "status" || verb == "on" || verb == "off" {
			decide := verb == "on" || wildcardVerb == "on"
			at, err := s.SetDecide(ctx, workspace, *cloud, *region, decide)
			if err != nil {
				return 1, err
			}
			answer = map[string]any{"workspace": workspace, "wildcard": verb == "wildcard", "cloud": *cloud,
				"region": *region, "decide": decide, "at": at}
			break
		}
		all, read, err := s.ReadDecide(ctx)
		if err != nil {
			return 1, err
		}
		var decide bool
		answer, decide = decideStatus(all, read, workspace, *cloud, *region)
		if decide {
			code = notDone
		}
	case "ready":
		why, err := s.GrantReadiness(ctx, workspace)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"workspace": workspace, "ready": why == "", "refused": string(why),
			"required_tier": cfg.Store.RequiredTier, "floor": cfg.Store.Floor,
			"allowance_tier2": cfg.Store.AllowanceTier2, "allowance_tier1": cfg.Store.AllowanceTier1}
		if why != "" {
			code = notDone
		}
	case "enable", "disable":
		// disable holds the workspace, an operator's stop; enable removes
		// its hold.
		var reason store.Reason
		if command == "disable" {
			reason = store.Reason{Kind: store.HoldOperator}
		}
		revoked, at, err := s.SetWorkspace(ctx, workspace, command == "enable", reason)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"workspace": workspace, "enabled": command == "enable", "revoked_leases": revoked,
			"at": at}
	case "leave":
		// The forced exit's fence: a node that stopped before it was marked
		// leaving is marked so, at whatever epoch, so no grant lands after.
		found, err := s.Leave(ctx, workspace)
		if err != nil {
			return 1, err
		}
		answer = map[string]any{"address": workspace, "found": found}
		if !found {
			code = notDone
		}
	case "node", "closed":
		// node is the planned replacement's check, closed the forced exit's:
		// whether anything of the node's is left to the auditor.
		st, err := s.NodeStatus(ctx, workspace)
		if err != nil {
			return 1, err
		}
		done, why := st.Done()
		if command == "closed" {
			done, why = st.Closed()
		}
		answer = map[string]any{"node": st, "done": done, "why_not": why}
		if !done {
			code = notDone
		}
	case "audit":
		problems, err := s.CheckIdentity(ctx, workspace)
		if err != nil {
			return 1, err
		}
		st, err := s.WorkspaceStatus(ctx, workspace)
		if err != nil {
			return 1, err
		}
		sums, err := s.AuditSums(ctx, workspace)
		if err != nil {
			return 1, err
		}
		twice, err := s.CheckWinners(ctx, workspace)
		if err != nil {
			return 1, err
		}
		problems = append(problems, twice...)
		// The leases' rows retire, so usage is not held equal to what they
		// booked; each shard's is never below what the retained ones booked
		// on it.
		below, err := s.CheckBookedUsage(ctx, workspace)
		if err != nil {
			return 1, err
		}
		problems = append(problems, below...)
		if *funded >= 0 && sums.Credits != *funded {
			problems = append(problems, fmt.Sprintf("the credit rows hold %d credits, and the workspace was funded %d",
				sums.Credits, *funded))
		}
		if problems == nil {
			problems = []string{}
		}
		answer = map[string]any{"workspace": workspace, "problems": problems, "status": st, "sums": sums}
		if len(problems) > 0 {
			code = notDone
		}
	default:
		st, err := s.WorkspaceStatus(ctx, workspace)
		if err != nil {
			return 1, err
		}
		done, why := st.Done()
		answer = map[string]any{"status": st, "done": done, "why_not": why}
		if !done {
			code = notDone
		}
	}
	// An answer not written is a failure, whatever was done: the caller
	// cannot tell what happened.
	if err := json.NewEncoder(out).Encode(answer); err != nil {
		return 1, err
	}
	return code, nil
}
