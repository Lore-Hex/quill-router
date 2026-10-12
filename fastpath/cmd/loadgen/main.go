// Command loadgen plays the spike's gateways against its front doors
// (spike plan §2, §5) and prints the run's report as JSON.
//
//	loadgen -front http://10.0.0.7:8080,http://10.0.0.8:8080 -mix load-mix.json \
//	    -rate 1000 -duration 10m -workspaces 50 -log generations.jsonl
//	loadgen -front http://10.128.0.51:8080 -mix load-mix.json -rate 1 -duration 10m \
//	    -workspace ws-synthetic -database projects/P/instances/I/databases/D
//
// With -workspace the load spreads over the workspaces named, instead of
// ws-0 on; with -database the run starts only once each is enabled, funded
// and at the tier that allows leases, read from the store as a grant reads
// (rollout P1), and otherwise says which is not and why.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/loadgen"
	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// readiness says why a run must not start against the workspaces, by the
// database: an error naming the first workspace a grant would refuse.
type readiness func(ctx context.Context, database string, workspaces []string) error

// storeReadiness asks the store, as a grant would, whether each workspace is
// enabled, funded and at the tier.
func storeReadiness(ctx context.Context, database string, workspaces []string) error {
	client, err := spanner.NewClient(ctx, database)
	if err != nil {
		return err
	}
	defer client.Close()
	s, err := store.New(client, service.Defaults().Store)
	if err != nil {
		return err
	}
	for _, ws := range workspaces {
		why, err := s.GrantReadiness(ctx, ws)
		if err != nil {
			return fmt.Errorf("workspace %s: %w", ws, err)
		}
		if why != "" {
			return fmt.Errorf("workspace %s is not ready for the load: %s", ws, why)
		}
	}
	return nil
}

func main() {
	if err := run(os.Args[1:], os.Stdout, storeReadiness); err != nil {
		fmt.Fprintln(os.Stderr, "loadgen:", err)
		os.Exit(1)
	}
}

// run runs the load generator as args say, and prints its report to out;
// with -database it starts only once ready, asked of the database, allows.
func run(args []string, out io.Writer, ready readiness) error {
	fs := flag.NewFlagSet("loadgen", flag.ContinueOnError)
	front := fs.String("front", "", "the front doors' base URLs, separated by commas")
	mixPath := fs.String("mix", "", "the load's mix, as fastpath/testdata/load-mix.json")
	rate := fs.Float64("rate", 100, "generations started a second")
	duration := fs.Duration("duration", time.Minute, "how long generations start")
	inFlight := fs.Int("in-flight", 100_000, "the most generations at once")
	workspaces := fs.Int("workspaces", 10, "how many workspaces the load spreads over, ws-0 on")
	workspace := fs.String("workspace", "", "the workspaces the load spreads over, by ID, separated by commas, instead of -workspaces")
	database := fs.String("database", "",
		"the database, projects/P/instances/I/databases/D: the run starts only once every workspace is enabled, funded and at the tier that allows leases")
	heartbeat := fs.Duration("heartbeat", 20*time.Second, "how often a stream heartbeats")
	openHeartbeat := fs.Bool("open-heartbeat", false,
		"the boot declares the heartbeat at stream open: each stream's authorize says so, and its first heartbeat is sent as it opens")
	heartbeatWait := fs.Duration("heartbeat-wait", 5*time.Second, "how long a heartbeat's attempts may take, together")
	boot := fs.String("boot", "spike-boot", "the boot binding every request carries")
	enclaves := fs.Int("enclaves", 8, "how many enclaves the load spreads over, each with its retry queue's one worker")
	retryDelays := fs.String("retry-delays", "0s,500ms,1s,2s,4s,8s",
		"the delays before each of the retry queue's attempts at a terminal, separated by commas")
	retryQueue := fs.Int("retry-queue", 1024, "how many terminals each enclave's retry queue holds")
	callWait := fs.Duration("call-wait", 28*time.Second, "how long a call but a heartbeat's may take")
	keyPath := fs.String("key", "", "a file with the fleet's envelope key, to name each generation's authorization")
	seed := fs.Uint64("seed", uint64(time.Now().UnixNano()), "the run's seed")
	logPath := fs.String("log", "", "a file to log each generation to, one JSON object a line")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			// -h asked for the flags, which Parse has printed: done.
			return nil
		}
		return err
	}

	f, err := os.Open(*mixPath)
	if err != nil {
		return err
	}
	mix, err := loadgen.ReadMix(f)
	f.Close()
	if err != nil {
		return err
	}
	client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 1024}}
	cfg := loadgen.Config{Rate: *rate, Duration: *duration, MaxInFlight: *inFlight, Mix: mix,
		HeartbeatEvery: *heartbeat, OpenHeartbeat: *openHeartbeat, HeartbeatWait: *heartbeatWait,
		Boot: []byte(*boot), Enclaves: *enclaves, RetryQueue: *retryQueue, CallWait: *callWait, Seed: *seed}
	for _, d := range strings.Split(*retryDelays, ",") {
		if d = strings.TrimSpace(d); d == "" {
			continue
		}
		delay, err := time.ParseDuration(d)
		if err != nil {
			return fmt.Errorf("-retry-delays: %w", err)
		}
		cfg.RetryDelays = append(cfg.RetryDelays, delay)
	}
	for _, base := range strings.Split(*front, ",") {
		if base = strings.TrimSpace(base); base != "" {
			cfg.Gateways = append(cfg.Gateways, frontdoor.Gateway{Client: client, Base: base})
		}
	}
	counted := false
	fs.Visit(func(f *flag.Flag) { counted = counted || f.Name == "workspaces" })
	switch {
	case *workspace != "" && counted:
		return errors.New("-workspace names the workspaces, or -workspaces counts them, not both")
	case *workspace != "":
		for _, ws := range strings.Split(*workspace, ",") {
			if ws = strings.TrimSpace(ws); ws != "" {
				cfg.Workspaces = append(cfg.Workspaces, ws)
			}
		}
	default:
		for i := range *workspaces {
			cfg.Workspaces = append(cfg.Workspaces, fmt.Sprintf("ws-%d", i))
		}
	}
	if *keyPath != "" {
		if cfg.Key, err = os.ReadFile(*keyPath); err != nil {
			return err
		}
	}
	if *logPath != "" {
		log, err := os.Create(*logPath)
		if err != nil {
			return err
		}
		defer log.Close()
		cfg.Log = log
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *database != "" {
		if ready == nil {
			return errors.New("-database: no store to ask")
		}
		if err := ready(ctx, *database, cfg.Workspaces); err != nil {
			return err
		}
	}
	rep, err := loadgen.Run(ctx, cfg)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	return enc.Encode(struct {
		Seed uint64 `json:"seed"`
		loadgen.Report
	}{*seed, rep})
}
