package settlelog

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// TestResumeKeepsQueuedRecordsBehindAFailedOne: a record queued behind one
// whose publish then fails fails too, without being sent, so the owner's
// republish after Resume stores them in order.
func TestResumeKeepsQueuedRecordsBehindAFailedOne(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	f.reactor.mu.Lock()
	f.reactor.gate = make(chan struct{})
	f.reactor.mu.Unlock()
	first := l.Publish("la", []byte("la#1"), map[string]string{"fail": "held"})
	time.Sleep(20 * time.Millisecond) // la#1 is in its call, held
	second := l.Publish("la", []byte("la#2"), nil)
	close(f.reactor.gate)
	if err := wait(t, first); err == nil {
		t.Fatal("the held publish succeeds")
	}
	if err := wait(t, second); !errors.Is(err, ErrPaused) {
		t.Fatalf("the record queued behind it: %v", err)
	}
	if err := wait(t, l.Publish("la", []byte("la#3"), nil)); !errors.Is(err, ErrPaused) {
		t.Fatalf("a record handed to the paused key: %v", err)
	}
	l.Resume("la")
	for _, r := range []string{"la#1", "la#2"} {
		if err := wait(t, l.Publish("la", []byte(r), nil)); err != nil {
			t.Fatalf("the republish of %s: %v", r, err)
		}
	}
	got := f.receive(t, 2, 10*time.Second, nil)
	if !equal(got["la"], records("la", 1, 2)) {
		t.Fatalf("the log holds %v", got)
	}
	f.reactor.mu.Lock()
	defer f.reactor.mu.Unlock()
	if !equal(f.reactor.sent, []string{"la#1", "la#1", "la#2"}) {
		t.Fatalf("the server was sent %v", f.reactor.sent)
	}
}

// TestTheDeadlineRunsFromHandOver: a record whose deadline passes while it
// waits behind another's call is never sent.
func TestTheDeadlineRunsFromHandOver(t *testing.T) {
	f := newFakeLog(t, true)
	var mu sync.Mutex
	offset := time.Duration(0)
	clock := func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		return time.Now().Add(offset)
	}
	l, err := openLog(publisherOf(f.client), f.topic, Settings{Deadline: time.Second}, clock)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Stop()
	f.reactor.mu.Lock()
	f.reactor.gate = make(chan struct{})
	f.reactor.mu.Unlock()
	first := l.Publish("la", []byte("la#1"), map[string]string{"fail": "slow"})
	time.Sleep(20 * time.Millisecond)
	second := l.Publish("la", []byte("la#2"), nil)
	mu.Lock()
	offset = 2 * time.Second // la#2's deadline passes while la#1's call is held
	mu.Unlock()
	close(f.reactor.gate)
	if err := wait(t, first); err != nil {
		t.Fatalf("the slow publish: %v", err)
	}
	if err := wait(t, second); !errors.Is(err, ErrDeadline) {
		t.Fatalf("a record past its deadline: %v", err)
	}
	f.reactor.mu.Lock()
	defer f.reactor.mu.Unlock()
	if !equal(f.reactor.sent, []string{"la#1"}) {
		t.Fatalf("the server was sent %v", f.reactor.sent)
	}
}

// TestReceiveWaitsForPreviousHandler: a lease's next record is handled only
// once the handler of the one before has returned, though it acknowledged
// that one at once.
func TestReceiveWaitsForPreviousHandler(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	for _, r := range records("la", 1, 2) {
		if err := wait(t, l.Publish("la", []byte(r), nil)); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var mu sync.Mutex
	var firstEnded, secondBegan time.Time
	err := Subscribe(f.client, f.sub, -1).Receive(ctx, func(_ context.Context, d *Delivery) {
		switch string(d.Data) {
		case "la#1":
			d.Ack()
			time.Sleep(200 * time.Millisecond)
			mu.Lock()
			firstEnded = time.Now()
			mu.Unlock()
		case "la#2":
			mu.Lock()
			secondBegan = time.Now()
			mu.Unlock()
			d.Ack()
			cancel()
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if firstEnded.IsZero() || secondBegan.IsZero() || secondBegan.Before(firstEnded) {
		t.Fatalf("la#2 began at %v, and la#1's handler ended at %v", secondBegan, firstEnded)
	}
}

// TestAWithheldRecordComesBack: assumption A1 as a member sees it. A record
// a member's handler returned without acknowledging comes back once the
// member stops, to the next member, with those after it, in order; one it
// acknowledged does not.
func TestAWithheldRecordComesBack(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	for _, r := range records("la", 1, 3) {
		if err := wait(t, l.Publish("la", []byte(r), nil)); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	var seen []string
	var mu sync.Mutex
	err := Subscribe(f.client, f.sub, -1).Receive(ctx, func(_ context.Context, d *Delivery) {
		mu.Lock()
		defer mu.Unlock()
		seen = append(seen, string(d.Data))
		if string(d.Data) == "la#1" {
			d.Ack()
			return
		}
		// la#2 is withheld: neither acknowledged nor asked for again.
		cancel()
	})
	cancel()
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	first := append([]string(nil), seen...)
	mu.Unlock()
	if !equal(first, records("la", 1, 2)) {
		t.Fatalf("the first member saw %v", first)
	}
	got := f.receive(t, 2, 40*time.Second, nil)
	if !equal(got["la"], records("la", 2, 3)) {
		t.Fatalf("the next member got %v", got)
	}
}
