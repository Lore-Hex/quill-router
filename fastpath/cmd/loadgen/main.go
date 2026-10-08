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
	openHeartbeat := flag.Bool("open-heartbeat", false,
		"the boot declares the heartbeat at stream open: each stream's authorize says so, and its first heartbeat is sent as it opens")
	heartbeatWait := flag.Duration("heartbeat-wait", 5*time.Second, "how long a heartbeat's attempts may take, together")
	boot := flag.String("boot", "spike-boot", "the boot binding every request carries")
	enclaves := flag.Int("enclaves", 8, "how many enclaves the load spreads over, each with its retry queue's one worker")
	retryDelays := flag.String("retry-delays", "0s,500ms,1s,2s,4s,8s",
		"the delays before each of the retry queue's attempts at a terminal, separated by commas")
	retryQueue := flag.Int("retry-queue", 1024, "how many terminals each enclave's retry queue holds")
	callWait := flag.Duration("call-wait", 28*time.Second, "how long a call but a heartbeat's may take")
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
		HeartbeatEvery: *heartbeat, OpenHeartbeat: *openHeartbeat, HeartbeatWait: *heartbeatWait, Boot: []byte(*boot), Enclaves: *enclaves,
		RetryQueue: *retryQueue, CallWait: *callWait, Seed: *seed}
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
