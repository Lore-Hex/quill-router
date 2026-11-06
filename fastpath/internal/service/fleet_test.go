package service

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"

	"github.com/Lore-Hex/quill-router/fastpath/internal/auditor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// TestTheFleetAuditIsDarkByDefault: the defaults run no fleet audit, carry
// the design's figures for it (docs/design/fast-admission-widening.md §7),
// and a process that runs one is an admission node, since the ring picks
// the key's owner of the members that take leases.
func TestTheFleetAuditIsDarkByDefault(t *testing.T) {
	cfg := Defaults()
	if cfg.FleetAudit {
		t.Fatal("the defaults run the fleet audit")
	}
	want := auditor.FleetConfig{Every: time.Hour, Check: time.Minute, Progress: 30 * time.Minute,
		Within: 26 * time.Hour, Rate: 5, Deadline: 10 * time.Second, Stale: 3 * time.Hour}
	if !reflect.DeepEqual(cfg.Fleet, want) {
		t.Fatalf("the fleet audit's defaults %+v, want %+v", cfg.Fleet, want)
	}
	cfg.Address, cfg.Region = "127.0.0.1:1", "us-central1"
	cfg.SettleTopic, cfg.RecordTopic = "projects/spike/topics/settle-log", "projects/spike/topics/records"
	cfg.SettleSubscription, cfg.RecordSubscription = "projects/spike/subscriptions/auditor",
		"projects/spike/subscriptions/stager"
	cfg.Auditor, cfg.FleetAudit = true, true
	if err := cfg.valid(); err == nil || !strings.Contains(err.Error(), "fleet audit runs on an admission node") {
		t.Fatalf("a fleet audit on an auditor member alone: %v", err)
	}
	cfg.Admission, cfg.Key = true, key
	if err := cfg.valid(); err != nil {
		t.Fatalf("a fleet audit on an admission node: %v", err)
	}
}

// TestAnAdmissionNodeWithTheFleetAuditTellsAFault: a node with the fleet
// audit on, the only member of its ring and so the key's owner, audits its
// database's workspaces at once, and writes a workspace whose credit row
// reserves what no lease holds as a fleet_audit_fault line of JSON at the
// journal's err priority, the workspace its subject, as the alert policy
// reads it.
func TestAnAdmissionNodeWithTheFleetAuditTellsAFault(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	// A database of its own, so the ring holds this node alone and the
	// audit reads no other test's workspaces.
	db, err := emulator.Database(ctx, storetest.UniqueID("fleet")[:20], nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	ws := storetest.UniqueID("ws")
	if _, err := db.Apply(ctx, []*spanner.Mutation{storetest.Enabled(ws), spanner.InsertMap("tr_credit_balance",
		map[string]any{"workspace_id": ws, "shard": int64(0), "total_credits": int64(100), "reserved": int64(5),
			"trust_tier": int64(3)})}); err != nil {
		t.Fatal(err)
	}
	out := &lines{}
	saved := alertOutput
	alertOutput = out
	t.Cleanup(func() { alertOutput = saved })
	ps, _ := fakePubSub(t)
	cfg := config(listen(t))
	cfg.Alert, cfg.FleetAudit = nil, true
	start(t, cfg, Clients{Spanner: db, PubSub: ps})
	var line string
	eventually(t, 20*time.Second, "the fault written", func() (bool, error) {
		for _, l := range strings.Split(out.String(), "\n") {
			if strings.Contains(l, auditor.KindFleetAuditFault) {
				line = l
				return true, nil
			}
		}
		return false, nil
	})
	body, found := strings.CutPrefix(line, "<3>")
	if !found {
		t.Fatalf("the line without the journal's err priority prefix: %q", line)
	}
	var got map[string]any
	want := map[string]any{
		"message": "fastpath alert: the fleet audit's identity check: shard 0 reserves 5; its live leases' donors " +
			"hold 0 and its synchronous holds 0",
		"subject":                       ws,
		"logging.googleapis.com/labels": map[string]any{"fastpath_alert": "fleet_audit_fault"}}
	if err := json.Unmarshal([]byte(body), &got); err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("the line %q: %v, want %v", line, err, want)
	}
}
