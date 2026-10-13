// Command fastpath runs one process of the fast-admission spike (spike plan
// §2): an admission node's front door and owner, an auditor member, or
// both, until it is interrupted or terminated.
//
//	fastpath -roles admission -address 10.0.0.7:8080 -region us-central1 \
//	    -database projects/P/instances/I/databases/spike -project P -key key.bin
//	fastpath -roles auditor -region us-central1 \
//	    -database projects/P/instances/I/databases/spike -project P
//
// With SPANNER_EMULATOR_HOST and PUBSUB_EMULATOR_HOST set, the clients
// reach the emulators instead.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
)

func main() {
	os.Exit(start(os.Getenv, run))
}

// start runs the node, and is its exit code: 1, before the node does
// anything, if the environment would print its secrets (printsSecrets).
func start(getenv func(string) string, node func() error) int {
	if why := printsSecrets(getenv); why != "" {
		fmt.Fprintln(os.Stderr, "fastpath:", why)
		return 1
	}
	if err := node(); err != nil {
		fmt.Fprintln(os.Stderr, "fastpath:", err)
		return 1
	}
	return 0
}

func run() error {
	cfg := service.Defaults()
	roles := flag.String("roles", "admission", "the roles to run: admission, auditor, or both, separated by a comma")
	flag.StringVar(&cfg.Address, "address", "", "an admission node's host and port, as other nodes reach it; an auditor member's own names its row")
	flag.StringVar(&cfg.Region, "region", "", "the settle log's region")
	database := flag.String("database", "", "the spike's database: projects/P/instances/I/databases/D")
	project := flag.String("project", "", "the Pub/Sub project")
	keyPath := flag.String("key", "", "a file with the fleet's envelope key, at least 32 bytes")
	keySecret := flag.String("key-secret", "",
		"the fleet's envelope key as a Secret Manager version pinned by its number: projects/P/secrets/S/versions/N")
	acceptSecrets := flag.String("accept-key-secrets", "",
		"keys also accepted when verifying, a rotation's other key, as pinned secret versions, separated by commas; "+
			"see docs/runbooks/fastpath-key-rotation.md")
	flag.Int64Var(&cfg.Shards, "shards", cfg.Shards, "every workspace's shard count")
	topics := flag.String("topics", "settle-log,records", "the settle log's topic and the record topic")
	subs := flag.String("subscriptions", "auditor,stager", "the auditor's subscriptions to the two topics")
	flag.DurationVar(&cfg.Store.Window, "window", cfg.Store.Window, "a lease's expiry window")
	flag.DurationVar(&cfg.Store.Skew, "skew", cfg.Store.Skew, "the skew allowance for nodes' clocks")
	flag.DurationVar(&cfg.Store.PublishDeadline, "publish-deadline", cfg.Store.PublishDeadline,
		"the owner's deadline for a publish")
	flag.DurationVar(&cfg.Store.MaxLife, "max-life", cfg.Store.MaxLife, "a hold's longest life")
	flag.DurationVar(&cfg.Store.Grace, "grace", cfg.Store.Grace, "the reaper's grace")
	flag.Int64Var(&cfg.Store.RequiredTier, "required-tier", cfg.Store.RequiredTier, "the trust tier leases need")
	flag.DurationVar(&cfg.Owner.HeartbeatEvery, "heartbeat-every", cfg.Owner.HeartbeatEvery,
		"the deadline each heartbeat's answer grants")
	flag.DurationVar(&cfg.Owner.RenewEvery, "renew-every", cfg.Owner.RenewEvery, "how often an owner renews its leases")
	flag.DurationVar(&cfg.Owner.FirstHeartbeat, "first-heartbeat", cfg.Owner.FirstHeartbeat,
		"how long a declared stream's hold waits for its first heartbeat, before the grace; 0 releases none")
	flag.DurationVar(&cfg.HandOff, "hand-off", cfg.HandOff,
		"how long a stopping node's owner has to hand its leases off; 0 hands none off")
	flag.DurationVar(&cfg.ClockOffset, "clock-offset", cfg.ClockOffset,
		"added to the owner's clock readings, to inject an owner's clock error; 0 for a true clock")
	flag.Parse()

	switch *roles {
	case "admission":
		cfg.Admission = true
	case "auditor":
		cfg.Auditor = true
	case "admission,auditor", "auditor,admission":
		cfg.Admission, cfg.Auditor = true, true
	default:
		return fmt.Errorf("-roles %q: admission, auditor, or both", *roles)
	}
	if *database == "" || *project == "" {
		return errors.New("-database and -project")
	}
	topicNames, subNames := strings.Split(*topics, ","), strings.Split(*subs, ",")
	if len(topicNames) != 2 || len(subNames) != 2 {
		return errors.New("-topics and -subscriptions name two each")
	}
	cfg.SettleTopic = "projects/" + *project + "/topics/" + topicNames[0]
	cfg.RecordTopic = "projects/" + *project + "/topics/" + topicNames[1]
	cfg.SettleSubscription = "projects/" + *project + "/subscriptions/" + subNames[0]
	cfg.RecordSubscription = "projects/" + *project + "/subscriptions/" + subNames[1]
	var err error
	if cfg.Key, cfg.AcceptKeys, err = keysOf(context.Background(), *keyPath, *keySecret, *acceptSecrets); err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	// SIGUSR1 marks the node leaving, as a deploy does before it stops it.
	usr1 := make(chan os.Signal, 1)
	signal.Notify(usr1, syscall.SIGUSR1)
	defer signal.Stop(usr1)
	leave := make(chan struct{})
	go func() {
		select {
		case <-usr1:
			close(leave)
		case <-ctx.Done():
		}
	}()
	cfg.Leave = leave
	sp, err := spanner.NewClient(ctx, *database)
	if err != nil {
		return err
	}
	defer sp.Close()
	ps, err := pubsub.NewClient(ctx, *project, service.PubSubOptions(cfg.Region)...)
	if err != nil {
		return err
	}
	defer ps.Close()
	began := time.Now()
	err = service.Run(ctx, cfg, service.Clients{Spanner: sp, PubSub: ps})
	fmt.Fprintf(os.Stderr, "fastpath: stopped after %v\n", time.Since(began).Round(time.Second))
	return err
}
