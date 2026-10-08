package frontdoor

import (
	"context"
	"strconv"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// BenchmarkAuthorize is an authorize through the front door to the node's
// own owner in the process: routing by the ring, the owner's admission under
// a warm lease, and the envelope sealed (design §4.1's overhead of about ten
// milliseconds has the network and Pub/Sub besides).
func BenchmarkAuthorize(b *testing.B) {
	c := &clock{now: start}
	cfg := ownerConfig(c, &clockGrants{c: c})
	cfg.TopUps.Min, cfg.TopUps.Max = 1<<50, 1<<50
	o, err := owner.New(cfg, &fakeLog{records: map[string][][]byte{}})
	if err != nil {
		b.Fatal(err)
	}
	b.Cleanup(o.Stop)
	local, err := NewLocal(o, "node-a", "us-central1", key)
	if err != nil {
		b.Fatal(err)
	}
	door, err := New(Config{Owners: Direct{"node-a": local}, Store: &fakeStore{ev: &events{}},
		Records: &fakeRecords{ev: &events{}}, Members: fakeMembers{owners("node-a")}, Key: key,
		Shards: func(string) int64 { return 1 }, OwnerWait: time.Second, PublishWait: time.Second})
	if err != nil {
		b.Fatal(err)
	}
	ctx := context.Background()
	// warm lets the last lease go, if any, and authorizes until the
	// shard's next lease, its top-up granted, admits; then the clock moves
	// past the top-up's cooldown, so each admission checks the shard's
	// room, as a warm lease's do. It returns the lease that admitted.
	n := 0
	warm := func(last string) string {
		if last != "" {
			o.Let(last)
		}
		for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
			n++
			got := door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: strconv.Itoa(n), Estimate: 1,
				Boot: []byte("boot")})
			if got.Status == Admitted {
				e, err := Open(key, got.Envelope)
				if err != nil {
					b.Fatal(err)
				}
				c.mu.Lock()
				c.now = c.now.Add(cfg.TopUps.Cooldown + time.Millisecond)
				c.mu.Unlock()
				return e.Lease
			}
			if time.Now().After(deadline) {
				b.Fatal("no lease was granted")
			}
		}
	}
	// A lease is replaced every batch, with the timer stopped, so its holds
	// stay a busy lease's, whatever b.N.
	lease := warm("")
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		if i++; i%benchBatch == 0 {
			b.StopTimer()
			lease = warm(lease)
			b.StartTimer()
		}
		n++
		got := door.Authorize(ctx, AuthorizeOf{Workspace: "ws-1", Request: strconv.Itoa(n), Estimate: 1,
			Boot: []byte("boot")})
		if got.Status != Admitted {
			b.Fatalf("authorize %d: %+v", n, got)
		}
	}
}

// benchBatch is how many holds a benchmark's lease takes before it is
// replaced.
const benchBatch = 4096

// clockGrants grants each lease to expire an hour past the clock's time
// then, so a lease granted after the clock has moved lasts as the first
// did, however long the run. The owner makes no other call of it here.
type clockGrants struct {
	owner.Spanner
	c *clock
}

func (g *clockGrants) Grant(context.Context, store.GrantRequest) (store.GrantResult, error) {
	return store.GrantResult{Expiry: g.c.Now().Add(time.Hour)}, nil
}
