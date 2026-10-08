package owner

import (
	"context"
	"crypto/sha256"
	"runtime"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The owner's hot path, in the process (design §4.1: the request's overhead
// within about ten milliseconds): an admission takes the lease's lock and
// mints a hold; a heartbeat or a terminal also encodes its record and waits
// for its acknowledgement, which discardLog gives at once and forgets, so
// what these measure is the owner's own work, not Pub/Sub's, and not a
// fake's memory growing with the run.

// discardLog acknowledges every publish at once and keeps nothing.
type discardLog struct{}

func (discardLog) Publish(string, []byte) Waiter { return waiter{} }
func (discardLog) Resume(string)                 {}

// benchBatch is how many holds one benchmark lease takes before the
// benchmark replaces it, with the timer stopped, so the lease's state stays
// the size a busy lease's is, whatever b.N.
const benchBatch = 4096

// benchLeases are fresh leases under one owner, as the owner mints them:
// authorization IDs from store.NewAuthorizationID, valid lease IDs.
type benchLeases struct {
	b     *testing.B
	owner *Owner
	n     int
}

func newBenchLeases(b *testing.B) *benchLeases {
	b.Helper()
	cfg := Config{Epoch: 3, Skew: 2 * time.Second, AnswerWait: time.Second, HoldLife: time.Hour,
		HeartbeatEvery: 30 * time.Second, Clock: func() time.Time { return start },
		NewAuthorization: store.NewAuthorizationID}
	o, err := New(cfg, discardLog{})
	if err != nil {
		b.Fatal(err)
	}
	b.Cleanup(o.Stop)
	return &benchLeases{b: b, owner: o}
}

// next is a new lease with room for every hold of a batch; the last one is
// let go.
func (ls *benchLeases) next(last *Lease) *Lease {
	ls.b.Helper()
	if last != nil {
		ls.owner.Let(last.ID())
	}
	ls.n++
	l, err := ls.owner.Take(store.NewLeaseID(), "ws-1", 1<<60, start.Add(time.Minute))
	if err != nil {
		ls.b.Fatal(err)
	}
	return l
}

func BenchmarkAdmit(b *testing.B) {
	ls := newBenchLeases(b)
	l := ls.next(nil)
	b.ReportAllocs()
	i := 0
	for b.Loop() {
		if i++; i%benchBatch == 0 {
			b.StopTimer()
			l = ls.next(l)
			b.StartTimer()
		}
		if _, err := l.Admit(Admission{Estimate: 100, Boot: boot}); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkAdmitParallel is admissions under one lease from every core at
// once: its lock is what they share. A batch at a time, the timer stopped
// while the next lease is taken and the last let go, the batch's admissions
// spread over GOMAXPROCS goroutines.
func BenchmarkAdmitParallel(b *testing.B) {
	ls := newBenchLeases(b)
	workers := runtime.GOMAXPROCS(0)
	var l *Lease
	b.ReportAllocs()
	b.StopTimer()
	for left := b.N; left > 0; {
		n := min(left, benchBatch)
		left -= n
		l = ls.next(l)
		var all sync.WaitGroup
		b.StartTimer()
		for w := range workers {
			k := n / workers
			if w < n%workers {
				k++
			}
			all.Add(1)
			go func() {
				defer all.Done()
				for range k {
					if _, err := l.Admit(Admission{Estimate: 100, Boot: boot}); err != nil {
						b.Error(err)
						return
					}
				}
			}()
		}
		all.Wait()
		b.StopTimer()
	}
}

// BenchmarkSettle is a settle of a hold admitted beforehand, a batch at a
// time with the timer stopped.
func BenchmarkSettle(b *testing.B) {
	ls := newBenchLeases(b)
	ctx := context.Background()
	digest := sum("full record")
	var l *Lease
	var held []string
	b.ReportAllocs()
	for b.Loop() {
		if len(held) == 0 {
			b.StopTimer()
			l = ls.next(l)
			for range benchBatch {
				a, err := l.Admit(Admission{Estimate: 100, Boot: boot})
				if err != nil {
					b.Fatal(err)
				}
				held = append(held, a.Auth)
			}
			b.StartTimer()
		}
		auth := held[len(held)-1]
		held = held[:len(held)-1]
		if _, err := l.Settle(ctx, auth, 90, digest); err != nil {
			b.Fatal(err)
		}
	}
}

// BenchmarkHeartbeat is a stream's heartbeats after its first, each new,
// their snapshots' hashes made beforehand.
func BenchmarkHeartbeat(b *testing.B) {
	ls := newBenchLeases(b)
	l := ls.next(nil)
	ctx := context.Background()
	a, err := l.Admit(Admission{Estimate: 1 << 40, Stream: true, Boot: boot})
	if err != nil {
		b.Fatal(err)
	}
	deadline, err := l.Heartbeat(ctx, a.Auth, HeartbeatOf{GatewaySeq: 1, Hash: sum("1"), Usage: 1, Running: 1,
		Basis: []byte(`{"model":"m"}`)})
	if err != nil {
		b.Fatal(err)
	}
	hashes := make([][]byte, benchBatch)
	for i := range hashes {
		h := sha256.Sum256([]byte(strconv.Itoa(i)))
		hashes[i] = h[:]
	}
	seq := int64(1)
	b.ReportAllocs()
	for b.Loop() {
		seq++
		deadline, err = l.Heartbeat(ctx, a.Auth, HeartbeatOf{GatewaySeq: seq, Hash: hashes[seq%benchBatch],
			Usage: seq, Running: 1, Echoed: deadline})
		if err != nil {
			b.Fatal(err)
		}
	}
}
