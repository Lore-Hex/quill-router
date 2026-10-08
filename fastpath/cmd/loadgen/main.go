// Command loadgen plays the spike's gateways against its front doors
// (spike plan §2, §5) and prints the run's report as JSON.
//
//	loadgen -front http://10.0.0.7:8080,http://10.0.0.8:8080 -mix load-mix.json \
//	    -rate 1000 -duration 10m -workspaces 50 -log generations.jsonl
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/loadgen"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "loadgen:", err)
		os.Exit(1)
	}
}

func run() error {
	front := flag.String("front", "", "the front doors' base URLs, separated by commas")
	mixPath := flag.String("mix", "", "the load's mix, as fastpath/testdata/load-mix.json")
	rate := flag.Float64("rate", 100, "generations started a second")
	duration := flag.Duration("duration", time.Minute, "how long generations start")
	inFlight := flag.Int("in-flight", 100_000, "the most generations at once")
	workspaces := flag.Int("workspaces", 10, "how many workspaces the load spreads over, ws-0 on")
	heartbeat := flag.Duration("heartbeat", 20*time.Second, "how often a stream heartbeats")
	boot := flag.String("boot", "spike-boot", "the boot binding every request carries")
	retryEvery := flag.Duration("retry-every", 5*time.Second, "how often the retry queue sends a terminal again")
	retryFor := flag.Duration("retry-for", 30*time.Minute, "how long the retry queue keeps a terminal")
	callWait := flag.Duration("call-wait", 10*time.Second, "how long a call may take")
	keyPath := flag.String("key", "", "a file with the fleet's envelope key, to name each generation's authorization")
	seed := flag.Uint64("seed", uint64(time.Now().UnixNano()), "the run's seed")
	logPath := flag.String("log", "", "a file to log each generation to, one JSON object a line")
	flag.Parse()

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
		HeartbeatEvery: *heartbeat, Boot: []byte(*boot), RetryEvery: *retryEvery, RetryFor: *retryFor,
		CallWait: *callWait, Seed: *seed}
	for _, base := range strings.Split(*front, ",") {
		if base = strings.TrimSpace(base); base != "" {
			cfg.Gateways = append(cfg.Gateways, frontdoor.Gateway{Client: client, Base: base})
		}
	}
	for i := range *workspaces {
		cfg.Workspaces = append(cfg.Workspaces, fmt.Sprintf("ws-%d", i))
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
	rep, err := loadgen.Run(ctx, cfg)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(struct {
		Seed uint64 `json:"seed"`
		loadgen.Report
	}{*seed, rep})
}
