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
//
// disable revokes the workspace's open leases with the switch, and
// disable-all turns every workspace off and revokes every open lease, the one
// switch that empties the allow-list. status reads
// the workspace's rows by its keys, read only, and exits 0 once it is off
// with no lease open or draining, nothing its leases' donors hold, no pack's
// work pending and no staged record, and 3 until then: turning a workspace
// off waits on it before any node stops (W2). node reads a node's row and the
// open leases it owns, and exits 0 once it is marked leaving and owns none,
// so a deploy may stop it, and 3 until then (W8). audit reads a workspace's
// rows in one snapshot and holds them to the design's identity (§4.7: each
// credit shard's reservation is its live leases' donors' remaining
// allocation, each lease's allocation and consumption its donors' sums, and
// the debt marks as the headroom has them) and to one winner per
// authorization across each lease's retained packs, read only, with the
// workspace's status and sums (credits, usage, reservations, leases by state,
// what the leases booked) beside: exit 0 when the rows keep them, 3 with each
// way they do not, after every run of the load (rollout P1). One thing no
// row says is the operator's to assert: -funded MICRO holds the credit rows'
// total credits to what funded the workspace. A command whose answer cannot
// be written exits 1. With SPANNER_EMULATOR_HOST set, the client reaches the
// emulator instead.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// notDone is status's exit code for a workspace turned off with something of
// the fast path left of it.
const notDone = 3

func main() {
	code, err := run(context.Background(), os.Args[1:], os.Stdout, open)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fastpathctl:", err)
	}
	os.Exit(code)
}

// open is the store at the database, closed by its second result.
func open(ctx context.Context, database string) (*store.Store, func(), error) {
	client, err := spanner.NewClient(ctx, database)
	if err != nil {
		return nil, nil, err
	}
	s, err := store.New(client, service.Defaults().Store)
	if err != nil {
		client.Close()
		return nil, nil, err
	}
	return s, client.Close, nil
}

// run is the command, its exit code and what went wrong.
func run(ctx context.Context, args []string, out io.Writer,
	open func(context.Context, string) (*store.Store, func(), error)) (int, error) {
	fs := flag.NewFlagSet("fastpathctl", flag.ContinueOnError)
	database := fs.String("database", "", "the database: projects/P/instances/I/databases/D")
	timeout := fs.Duration("timeout", 30*time.Second, "how long the command may take")
	funded := fs.Int64("funded", -1, "audit: the micro-credits that funded the workspace, which its credit rows must sum to")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0, nil
		}
		return 2, err
	}
	usage := errors.New("usage: fastpathctl -database D enable|disable|status|audit WORKSPACE, node|leave|closed ADDRESS, " +
		"or disable-all")
	if *database == "" || fs.NArg() < 1 {
		return 2, usage
	}
	command, workspace := fs.Arg(0), fs.Arg(1)
	switch {
	case command == "disable-all":
		if fs.NArg() != 1 {
			return 2, usage
		}
	case command != "enable" && command != "disable" && command != "status" && command != "node" && command != "leave" &&
		command != "closed" && command != "audit":
		return 2, fmt.Errorf("%q is not enable, disable, status, audit, node, leave, closed or disable-all", command)
	case fs.NArg() != 2 || workspace == "":
		return 2, usage
	}
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	s, closeStore, err := open(ctx, *database)
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
		answer = map[string]any{"workspaces_turned_off": off, "revoked_leases": revoked, "at": at}
	case "enable", "disable":
		revoked, at, err := s.SetWorkspace(ctx, workspace, command == "enable")
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
