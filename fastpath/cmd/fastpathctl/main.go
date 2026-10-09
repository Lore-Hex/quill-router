// Command fastpathctl turns the fast path on or off for a workspace, and
// says whether one turned off is done (docs/design/fast-admission-
// production-rollout.md, W1 and W2).
//
//	fastpathctl -database projects/P/instances/I/databases/D enable WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D disable WORKSPACE
//	fastpathctl -database projects/P/instances/I/databases/D status WORKSPACE
//
// disable revokes the workspace's open leases with the switch. status reads
// the workspace's rows by its keys, read only, and exits 0 once it is off
// with no lease open or draining, nothing its leases' donors hold and no
// pack's work pending, and 3 until then: turning a workspace off waits on
// it before any node stops (W2). With SPANNER_EMULATOR_HOST set, the client
// reaches the emulator instead.
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
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0, nil
		}
		return 2, err
	}
	if *database == "" || fs.NArg() != 2 || fs.Arg(1) == "" {
		return 2, errors.New("usage: fastpathctl -database D enable|disable|status WORKSPACE")
	}
	command, workspace := fs.Arg(0), fs.Arg(1)
	if command != "enable" && command != "disable" && command != "status" {
		return 2, fmt.Errorf("%q is not enable, disable or status", command)
	}
	ctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	s, closeStore, err := open(ctx, *database)
	if err != nil {
		return 1, err
	}
	defer closeStore()
	enc := json.NewEncoder(out)
	switch command {
	case "enable", "disable":
		revoked, at, err := s.SetWorkspace(ctx, workspace, command == "enable")
		if err != nil {
			return 1, err
		}
		return 0, enc.Encode(map[string]any{"workspace": workspace, "enabled": command == "enable",
			"revoked_leases": revoked, "at": at})
	}
	st, err := s.WorkspaceStatus(ctx, workspace)
	if err != nil {
		return 1, err
	}
	done, why := st.Done()
	if err := enc.Encode(map[string]any{"status": st, "done": done, "why_not": why}); err != nil {
		return 1, err
	}
	if !done {
		return notDone, nil
	}
	return 0, nil
}
