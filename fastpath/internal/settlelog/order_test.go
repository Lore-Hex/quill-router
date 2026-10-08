package settlelog

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
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

// TestShutdownKeepsHandlersSerial: once a member stops, the client library
// stops waiting for a handler still running; a lease's next record is then
// asked for again, not handled beside it.
func TestShutdownKeepsHandlersSerial(t *testing.T) {
	was := shutdownTimeout
	shutdownTimeout = 200 * time.Millisecond
	defer func() { shutdownTimeout = was }()
	f := newFakeLog(t, true)
	l := f.log(t)
	for _, r := range records("la", 1, 2) {
		if err := wait(t, l.Publish("la", []byte(r), nil)); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	var mu sync.Mutex
	running, overlapped := false, false
	var handled []string
	release := make(chan struct{})
	received := make(chan error, 1)
	go func() {
		received <- Subscribe(f.client, f.sub, -1).Receive(ctx, func(_ context.Context, d *Delivery) {
			mu.Lock()
			overlapped = overlapped || running
			running = true
			handled = append(handled, string(d.Data))
			mu.Unlock()
			if string(d.Data) == "la#1" {
				d.Ack()
				// la#2 reaches the member, queued behind this handler,
				// before the member stops.
				for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(time.Millisecond) {
					if m := f.srv.Messages(); len(m) == 2 && m[1].Deliveries > 0 {
						break
					}
				}
				time.Sleep(50 * time.Millisecond)
				cancel()
				<-release // still running past the shutdown's timeout
			}
			mu.Lock()
			running = false
			mu.Unlock()
		})
	}()
	time.Sleep(time.Second)
	close(release)
	if err := <-received; err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if overlapped || !equal(handled, []string{"la#1"}) {
		t.Fatalf("handled %v, overlapping: %v", handled, overlapped)
	}
}

// TestResumeAfterAFailedWait: by the time an owner sees a publish fail, the
// key is paused and what was queued behind it has failed, so the owner's
// resume and republish go through at once.
func TestResumeAfterAFailedWait(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	for i := range 50 {
		lease := fmt.Sprintf("l%d", i)
		if err := wait(t, l.Publish(lease, []byte(lease+"#1"), map[string]string{"fail": "once"})); err == nil {
			t.Fatal("the refused publish succeeds")
		}
		l.Resume(lease)
		if err := wait(t, l.Publish(lease, []byte(lease+"#1"), nil)); err != nil {
			t.Fatalf("lease %d: the republish right after the failure: %v", i, err)
		}
	}
}

// TestABatchIsCutByItsRequestsSize: records queued together are sent in
// Publish requests within Pub/Sub's 10 MB, every record's attributes and
// ordering key counted, not its payload alone. The test's publish call
// refuses a request over 10 MB, as Pub/Sub does; pstest's server takes no
// request over gRPC's default 4 MB, so it cannot tell.
func TestABatchIsCutByItsRequestsSize(t *testing.T) {
	gate := make(chan struct{})
	var mu sync.Mutex
	var sizes []int
	var sent []string
	publish := func(_ context.Context, req *pubsubpb.PublishRequest) (*pubsubpb.PublishResponse, error) {
		mu.Lock()
		first := len(sizes) == 0
		sizes = append(sizes, proto.Size(req))
		mu.Unlock()
		if first {
			<-gate
		}
		if proto.Size(req) > 10_000_000 {
			return nil, status.Error(codes.InvalidArgument, "publish request exceeds 10MB")
		}
		mu.Lock()
		defer mu.Unlock()
		ids := make([]string, len(req.Messages))
		for i, m := range req.Messages {
			sent = append(sent, string(m.Data))
			ids[i] = fmt.Sprint(len(sent))
		}
		return &pubsubpb.PublishResponse{MessageIds: ids}, nil
	}
	l, err := openLog(publish, "projects/spike/topics/settle-log", Settings{Deadline: 30 * time.Second}, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Stop()
	pending := []*Pending{l.Publish("la", []byte("la#0"), nil)}
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		mu.Lock()
		calls := len(sizes)
		mu.Unlock()
		if calls == 1 {
			break // la#0 is in its call, held, and the rest queue behind it
		}
		if time.Now().After(deadline) {
			t.Fatal("la#0 was never sent")
		}
	}
	attrs := map[string]string{}
	for i := range 10 {
		attrs[fmt.Sprintf("a%d", i)] = strings.Repeat("v", 1024)
	}
	for i := 1; i <= 1000; i++ {
		pending = append(pending, l.Publish("la", []byte(fmt.Sprintf("la#%d%s", i, strings.Repeat(".", 250))), attrs))
	}
	close(gate)
	for i, p := range pending {
		if err := wait(t, p); err != nil {
			t.Fatalf("record %d: %v", i, err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(sent) != 1001 || len(sizes) < 3 {
		t.Fatalf("%d records sent in %d requests", len(sent), len(sizes))
	}
	for i, n := range sizes {
		if n > 10_000_000 {
			t.Fatalf("request %d is %d bytes", i, n)
		}
	}
	for i, r := range sent {
		if !strings.HasPrefix(r, fmt.Sprintf("la#%d", i)) || (i > 0 && r[len(fmt.Sprintf("la#%d", i))] != '.') {
			t.Fatalf("record %d sent as %.8s", i, r)
		}
	}
}

// TestAReturnIsNoAcknowledgement: a handler that returns without Ack, its
// member still receiving, leaves its record unacknowledged; once the member
// stops, the record comes back.
func TestAReturnIsNoAcknowledgement(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	if err := wait(t, l.Publish("la", []byte("la#1"), nil)); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	returned := make(chan struct{})
	var once sync.Once
	received := make(chan error, 1)
	go func() {
		received <- Subscribe(f.client, f.sub, -1).Receive(ctx, func(context.Context, *Delivery) {
			once.Do(func() { close(returned) })
		})
	}()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("la#1 was never delivered")
	}
	time.Sleep(500 * time.Millisecond) // the member keeps receiving after the handler returned
	if m := f.srv.Messages(); len(m) != 1 || m[0].Acks != 0 {
		t.Fatalf("the record after its handler returned: %+v", m)
	}
	cancel()
	if err := <-received; err != nil {
		t.Fatal(err)
	}
	if got := f.receive(t, 1, 40*time.Second, nil); !equal(got["la"], []string{"la#1"}) {
		t.Fatalf("the next member got %v", got)
	}
}

// TestAStoppedMemberLetsGoOfItsRecords: once a member's Receive returns, it
// holds none of the records its handlers had and did not settle: the client
// library, left alone, can go on extending such a record's deadline, here
// la#1's, whose handler ran past the stop while lb#1's acknowledged handler
// and lb#2, waiting behind it, filled the member's two places. The next
// member gets la#1.
func TestAStoppedMemberLetsGoOfItsRecords(t *testing.T) {
	was := shutdownTimeout
	shutdownTimeout = 200 * time.Millisecond
	defer func() { shutdownTimeout = was }()
	f := newFakeLog(t, true)
	l := f.log(t)
	for _, r := range []struct{ lease, data string }{{"la", "la#1"}, {"lb", "lb#1"}, {"lb", "lb#2"}} {
		if err := wait(t, l.Publish(r.lease, []byte(r.data), nil)); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	release := make(chan struct{})
	running := make(chan string, 4)
	received := make(chan error, 1)
	go func() {
		received <- Subscribe(f.client, f.sub, 2).Receive(ctx, func(_ context.Context, d *Delivery) {
			switch string(d.Data) {
			case "la#1": // held, and never settled
			case "lb#1":
				d.Ack()
			default:
				return
			}
			running <- string(d.Data)
			<-release
		})
	}()
	for range 2 {
		select {
		case <-running:
		case <-time.After(10 * time.Second):
			t.Fatal("the handlers did not run")
		}
	}
	// lb#1's acknowledgement reaches the server, which then sends lb#2, and
	// the member receives it, as its receipt's deadline change shows, to
	// wait behind lb#1's handler.
	received2 := func(m *pstest.Message) bool {
		for _, a := range m.Modacks {
			if a.AckDeadline > 0 {
				return true
			}
		}
		return false
	}
	for deadline := time.Now().Add(5 * time.Second); ; time.Sleep(time.Millisecond) {
		if m := f.srv.Messages(); len(m) == 3 && m[1].Acks == 1 && m[2].Deliveries > 0 && received2(m[2]) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("lb#2 did not reach the member")
		}
	}
	cancel()
	if err := <-received; err != nil {
		t.Fatal(err)
	}
	stopped := time.Now()
	close(release)
	time.Sleep(6 * time.Second) // the library extends a held record's deadline every few seconds
	for _, m := range f.srv.Messages() {
		for _, a := range m.Modacks {
			if a.AckDeadline > 0 && a.ReceivedAt.After(stopped) {
				t.Fatalf("%s's deadline was extended %v after the member stopped", m.Data, a.ReceivedAt.Sub(stopped))
			}
		}
	}
	ctx2, cancel2 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel2()
	got := false
	err := Subscribe(f.client, f.sub, -1).Receive(ctx2, func(_ context.Context, d *Delivery) {
		d.Ack()
		if string(d.Data) == "la#1" {
			got = true
			cancel2()
		}
	})
	if err != nil || !got {
		t.Fatalf("the next member did not get la#1: %v", err)
	}
}
