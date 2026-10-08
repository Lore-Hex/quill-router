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
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "fastpath:", err)
		os.Exit(1)
	}
}

func run() error {
	cfg := service.Defaults()
	roles := flag.String("roles", "admission", "the roles to run: admission, auditor, or both, separated by a comma")
	flag.StringVar(&cfg.Address, "address", "", "the admission node's host and port, as other nodes reach it")
	flag.StringVar(&cfg.Region, "region", "", "the settle log's region")
	database := flag.String("database", "", "the spike's database: projects/P/instances/I/databases/D")
	project := flag.String("project", "", "the Pub/Sub project")
	keyPath := flag.String("key", "", "a file with the fleet's envelope key, at least 32 bytes")
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
	if *keyPath != "" {
		key, err := os.ReadFile(*keyPath)
		if err != nil {
			return err
		}
		cfg.Key = key
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
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
