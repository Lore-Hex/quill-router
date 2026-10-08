package trace

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestEventsAreNumberedInTheirProcess: each event of a process takes the
// next number, however many record at once, and its clocks; they read back
// as recorded.
func TestEventsAreNumberedInTheirProcess(t *testing.T) {
	var buf bytes.Buffer
	r, err := New(&buf, "node-a:8080", 3)
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for i := range 50 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r.Record("settle", nil, Facts{Lease: "l", Auth: "a", OwnerSeq: int64(i)})
		}()
	}
	wg.Wait()
	cause := ID{Node: "node-b:8080", Epoch: 1, Seq: 7}
	last := r.Record("append", &cause, Facts{Commit: time.Date(2026, 10, 8, 12, 0, 0, 1000, time.UTC), Detail: "unreachable"})
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
	events, err := Read(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 51 || last != (ID{Node: "node-a:8080", Epoch: 3, Seq: 51}) {
		t.Fatalf("%d events, the last %+v", len(events), last)
	}
	for i, e := range events {
		if e.ID.Seq != int64(i+1) || e.ID.Node != "node-a:8080" || e.ID.Epoch != 3 || e.Wall.IsZero() ||
			(i > 0 && e.Mono < events[i-1].Mono) {
			t.Fatalf("event %d: %+v", i, e)
		}
	}
	if e := events[50]; e.Cause == nil || *e.Cause != cause || e.Kind != "append" || e.Detail != "unreachable" ||
		!e.Commit.Equal(time.Date(2026, 10, 8, 12, 0, 0, 1000, time.UTC)) {
		t.Fatalf("the last event: %+v", e)
	}
	if r.Record("late", nil, Facts{}) != (ID{Node: "node-a:8080", Epoch: 3, Seq: 52}) {
		t.Fatal("an event after Close is not numbered")
	}
	if buf.Len() != 0 {
		t.Fatalf("an event after Close was written: %q", buf.String())
	}
}

// TestAnIdentityCrossesProcesses: an identity reads back as a message
// carries it, and a context carries its cause.
func TestAnIdentityCrossesProcesses(t *testing.T) {
	id := ID{Node: "10.0.0.7:8080", Epoch: 12, Seq: 99}
	got, err := ParseID(id.String())
	if err != nil || got != id {
		t.Fatalf("%q read back as %+v %v", id.String(), got, err)
	}
	for _, bad := range []string{"", "a/1", "/1/2", "a/0/2", "a/1/0", "a/x/2", "a/1/2/3"} {
		if _, err := ParseID(bad); err == nil {
			t.Fatalf("%q read as an identity", bad)
		}
	}
	if Cause(context.Background()) != nil {
		t.Fatal("a cause in a context that carries none")
	}
	if c := Cause(WithCause(context.Background(), id)); c == nil || *c != id {
		t.Fatalf("the cause carried: %v", c)
	}
}

type failing struct{}

func (failing) Write([]byte) (int, error) { return 0, errors.New("the disk is full") }

// TestARecorderReportsItsFailure: a write that fails is reported at Close,
// and events are numbered still; a nil recorder records nothing.
func TestARecorderReportsItsFailure(t *testing.T) {
	r, err := New(failing{}, "n", 1)
	if err != nil {
		t.Fatal(err)
	}
	for range 5000 {
		r.Record("heartbeat", nil, Facts{Detail: strings.Repeat("x", 10)})
	}
	if err := r.Close(); err == nil {
		t.Fatal("a recorder whose writes failed closed clean")
	}
	var none *Recorder
	if id := none.Record("x", nil, Facts{}); id != (ID{}) || none.Close() != nil {
		t.Fatal("a nil recorder recorded")
	}
	for _, bad := range []struct {
		node  string
		epoch int64
	}{{"", 1}, {"a/b", 1}, {"a", 0}} {
		if _, err := New(&bytes.Buffer{}, bad.node, bad.epoch); err == nil {
			t.Fatalf("a recorder for %+v", bad)
		}
	}
	if _, err := Read(strings.NewReader(`{"id":{"node":"n","epoch":1,"seq":1},"mono":0,"wall":"2026-10-08T12:00:00Z","kind":"x","extra":1}`)); err == nil {
		t.Fatal("an event with a field the format has not read")
	}
}
