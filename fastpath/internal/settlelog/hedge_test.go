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
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// calls is a fake Publish call: each call's answer is decided by answer,
// given the call's number from 0 and its context, and every call's messages
// are kept in the order the calls began.
type calls struct {
	mu     sync.Mutex
	sent   [][]string
	ended  []error
	answer func(n int, ctx context.Context) error
}

func (c *calls) publish(ctx context.Context, req *pubsubpb.PublishRequest) (*pubsubpb.PublishResponse, error) {
	c.mu.Lock()
	n := len(c.sent)
	var data []string
	for _, m := range req.Messages {
		data = append(data, string(m.Data))
	}
	c.sent = append(c.sent, data)
	c.mu.Unlock()
	err := c.answer(n, ctx)
	c.mu.Lock()
	c.ended = append(c.ended, err)
	c.mu.Unlock()
	if err != nil {
		return nil, err
	}
	ids := make([]string, len(req.Messages))
	for i := range ids {
		ids[i] = fmt.Sprintf("id-%d-%d", n, i)
	}
	return &pubsubpb.PublishResponse{MessageIds: ids}, nil
}

func (c *calls) seen() [][]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([][]string(nil), c.sent...)
}

// held is a call the server holds until its context ends, as 2026-11-16's
// settle log calls were held until their 5 s deadline cancelled them.
func held(ctx context.Context) error {
	<-ctx.Done()
	return status.FromContextError(ctx.Err()).Err()
}

func openFake(t *testing.T, c *calls, s Settings) *Log {
	t.Helper()
	l, err := openLog(c.publish, "projects/spike/topics/settle-log", s, time.Now)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(l.Stop)
	return l
}

// TestAHeldCallIsHedged: a call the server holds is joined by another
// carrying the same batch, whose answer acknowledges the records long before
// the deadline, and the held call is then cancelled. Without a hedge, the
// same held call holds the key until the deadline fails it: the incident.
func TestAHeldCallIsHedged(t *testing.T) {
	t.Run("hedged", func(t *testing.T) {
		c := &calls{answer: func(n int, ctx context.Context) error {
			if n == 0 {
				return held(ctx)
			}
			return nil
		}}
		l := openFake(t, c, Settings{Deadline: 10 * time.Second, Hedge: 20 * time.Millisecond})
		began := time.Now()
		p := l.Publish("la", []byte("la#1"), nil)
		id, err := p.Wait(context.Background())
		if took := time.Since(began); err != nil || took > 5*time.Second {
			t.Fatalf("a held call's hedge: %q, %v after %v", id, err, took)
		}
		if id != "id-1-0" {
			t.Fatalf("the batch's answer is %q, not the hedge's", id)
		}
		if got := c.seen(); len(got) != 2 || !equal(got[0], []string{"la#1"}) || !equal(got[1], []string{"la#1"}) {
			t.Fatalf("the calls carried %v", got)
		}
		waitUntil(t, "the held call's cancellation", func() bool {
			c.mu.Lock()
			defer c.mu.Unlock()
			return len(c.ended) == 2 && status.Code(c.ended[1]) == codes.Canceled
		})
		if c := l.TakeCalls(); c != (Calls{Batches: 1, Hedges: 1, Won: 1}) {
			t.Fatalf("the calls: %+v", c)
		}
		if c := l.TakeCalls(); c != (Calls{}) {
			t.Fatalf("the calls taken again: %+v", c)
		}
	})
	t.Run("not hedged", func(t *testing.T) {
		c := &calls{answer: func(n int, ctx context.Context) error {
			if n == 0 {
				return held(ctx)
			}
			return nil
		}}
		l := openFake(t, c, Settings{Deadline: 300 * time.Millisecond})
		began := time.Now()
		_, err := l.Publish("la", []byte("la#1"), nil).Wait(context.Background())
		if took := time.Since(began); err == nil || took < 250*time.Millisecond {
			t.Fatalf("a held call with no hedge: %v after %v", err, took)
		}
		if got := c.seen(); len(got) != 1 {
			t.Fatalf("the calls carried %v", got)
		}
		if err := wait(t, l.Publish("la", []byte("la#2"), nil)); !errors.Is(err, ErrPaused) {
			t.Fatalf("a record after the failed batch: %v", err)
		}
	})
}

// TestNothingBehindABatchIsSentBeforeItIsAcknowledged: a record queued while
// a batch's call is held waits for the batch's acknowledgement, the hedge's;
// the hedge carries the batch alone.
func TestNothingBehindABatchIsSentBeforeItIsAcknowledged(t *testing.T) {
	release := make(chan struct{})
	c := &calls{answer: func(n int, ctx context.Context) error {
		switch n {
		case 0:
			return held(ctx)
		case 1:
			select {
			case <-release:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return nil
	}}
	l := openFake(t, c, Settings{Deadline: 10 * time.Second, Hedge: 20 * time.Millisecond})
	first := l.Publish("la", []byte("la#1"), nil)
	waitUntil(t, "the hedge", func() bool { return len(c.seen()) == 2 })
	second := l.Publish("la", []byte("la#2"), nil)
	time.Sleep(30 * time.Millisecond)
	if got := c.seen(); len(got) != 2 {
		t.Fatalf("a record behind an unacknowledged batch was sent: %v", got)
	}
	close(release)
	for _, p := range []*Pending{first, second} {
		if err := wait(t, p); err != nil {
			t.Fatal(err)
		}
	}
	got := c.seen()
	if len(got) != 3 || !equal(got[0], []string{"la#1"}) || !equal(got[1], []string{"la#1"}) ||
		!equal(got[2], []string{"la#2"}) {
		t.Fatalf("the calls carried %v", got)
	}
}

// TestHedgesEndAtTheirLimitAndTheDeadline: a batch every call of which is
// held is sent at most 1+maxHedges times, at Hedge, three and seven times
// Hedge, none at or past the deadline, and fails at the deadline, the key
// paused as before.
func TestHedgesEndAtTheirLimitAndTheDeadline(t *testing.T) {
	for _, tc := range []struct {
		name            string
		hedge, deadline time.Duration
		calls           int
	}{
		// Hedges at 10, 30 and 70 ms, all far inside the deadline.
		{"the limit", 10 * time.Millisecond, 2 * time.Second, 1 + maxHedges},
		// A hedge at 0.5 s, half a second inside the deadline; the next
		// would be at 1.5 s, half a second past it.
		{"the deadline", 500 * time.Millisecond, time.Second, 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := &calls{answer: func(_ int, ctx context.Context) error { return held(ctx) }}
			l := openFake(t, c, Settings{Deadline: tc.deadline, Hedge: tc.hedge})
			began := time.Now()
			_, err := l.Publish("la", []byte("la#1"), nil).Wait(context.Background())
			if took := time.Since(began); err == nil || took < tc.deadline-50*time.Millisecond {
				t.Fatalf("a batch whose every call is held: %v after %v", err, took)
			}
			if got := c.seen(); len(got) != tc.calls {
				t.Fatalf("%d calls, want %d: %v", len(got), tc.calls, got)
			}
			if c := l.TakeCalls(); c != (Calls{Batches: 1, Hedges: int64(tc.calls - 1)}) {
				t.Fatalf("the calls: %+v", c)
			}
			if err := wait(t, l.Publish("la", []byte("la#2"), nil)); !errors.Is(err, ErrPaused) {
				t.Fatalf("a record after the failed batch: %v", err)
			}
		})
	}
}

// TestAFailedCallEndsTheBatchOnlyWithNoneUnderWay: a call that fails while a
// hedge is under way leaves the batch to the hedge; a call that fails with
// none under way fails the batch at once, as with no hedge, since the client
// library has already retried what it could.
func TestAFailedCallEndsTheBatchOnlyWithNoneUnderWay(t *testing.T) {
	t.Run("a hedge under way", func(t *testing.T) {
		fail, answer := make(chan struct{}), make(chan struct{})
		c := &calls{answer: func(n int, ctx context.Context) error {
			if n == 0 {
				<-fail
				return status.Error(codes.Internal, "refused after a wait")
			}
			<-answer
			return nil
		}}
		l := openFake(t, c, Settings{Deadline: 10 * time.Second, Hedge: 20 * time.Millisecond})
		p := l.Publish("la", []byte("la#1"), nil)
		waitUntil(t, "the hedge", func() bool { return len(c.seen()) == 2 })
		close(fail)
		waitUntil(t, "the first call's failure", func() bool {
			c.mu.Lock()
			defer c.mu.Unlock()
			return len(c.ended) == 1
		})
		select {
		case <-p.done:
			t.Fatalf("the batch ended with its hedge under way: %v", p.err)
		case <-time.After(30 * time.Millisecond):
		}
		close(answer)
		if err := wait(t, p); err != nil {
			t.Fatalf("the hedge's answer: %v", err)
		}
		if c := l.TakeCalls(); c != (Calls{Batches: 1, Hedges: 1, Won: 1}) {
			t.Fatalf("the calls: %+v", c)
		}
	})
	t.Run("none under way", func(t *testing.T) {
		c := &calls{answer: func(int, context.Context) error { return status.Error(codes.InvalidArgument, "refused") }}
		// The hedge would come at 10 s: a batch that waited for it, rather
		// than failing at once, would take at least that.
		l := openFake(t, c, Settings{Deadline: 20 * time.Second, Hedge: 10 * time.Second})
		began := time.Now()
		err := wait(t, l.Publish("la", []byte("la#1"), nil))
		if took := time.Since(began); status.Code(err) != codes.InvalidArgument || took > 5*time.Second {
			t.Fatalf("a refused call: %v after %v", err, took)
		}
		if got := c.seen(); len(got) != 1 {
			t.Fatalf("a refused call was hedged: %v", got)
		}
	})
}

// lateFirstPublish holds the connection's first Publish call: its caller
// waits until its context ends, and the server is sent the call only once
// gate opens, though the caller has gone by then, as a held call the client
// cancelled can still be stored. sent closes once the server has answered it.
func lateFirstPublish(gate <-chan struct{}, sent chan<- error) grpc.DialOption {
	var once sync.Once
	return grpc.WithChainUnaryInterceptor(func(ctx context.Context, method string, req, reply any,
		cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		r, ok := req.(*pubsubpb.PublishRequest)
		first := false
		if ok && strings.HasSuffix(method, "/Publish") {
			once.Do(func() { first = true })
		}
		if !first {
			return invoker(ctx, method, req, reply, cc, opts...)
		}
		late := proto.Clone(r)
		go func() {
			<-gate
			sent <- invoker(context.WithoutCancel(ctx), method, late, new(pubsubpb.PublishResponse), cc, opts...)
		}()
		<-ctx.Done()
		return status.FromContextError(ctx.Err()).Err()
	})
}

// TestALateCopyOfAHedgedBatchIsADuplicateBehindTheLog: on the fake server,
// a held call that the server stores after its hedge, and after the batch
// behind it, reaches the subscriber as a copy of a record already delivered,
// after the later one: what the auditor skips as an owner record at or
// below its progress. Nothing is delivered before a record ahead of it.
func TestALateCopyOfAHedgedBatchIsADuplicateBehindTheLog(t *testing.T) {
	gate, sent := make(chan struct{}), make(chan error, 1)
	f := newFakeLog(t, true, lateFirstPublish(gate, sent))
	l, err := OpenLog(f.client, f.topic, Settings{Deadline: 10 * time.Second, Hedge: 50 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Stop()
	if err := wait(t, l.Publish("la", []byte("la#1"), nil)); err != nil {
		close(gate)
		t.Fatalf("the hedged batch: %v", err)
	}
	if err := wait(t, l.Publish("la", []byte("la#2"), nil)); err != nil {
		close(gate)
		t.Fatal(err)
	}
	close(gate)
	select {
	case err := <-sent:
		if err != nil {
			t.Fatalf("the late copy: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the late copy was never answered")
	}
	got := f.receive(t, 3, 10*time.Second, nil)
	if !equal(got["la"], []string{"la#1", "la#2", "la#1"}) {
		t.Fatalf("the log holds %v", got)
	}
	f.reactor.mu.Lock()
	defer f.reactor.mu.Unlock()
	if !equal(f.reactor.sent, []string{"la#1", "la#2", "la#1"}) {
		t.Fatalf("the server was sent %v", f.reactor.sent)
	}
}

// heldOnce answers calls as the server held 2026-11-16's: the first call
// carrying a batch whose first record's data begins "held" is held until its
// context ends; every other call is answered at once.
func heldOnce(c *calls) func(n int, ctx context.Context) error {
	var mu sync.Mutex
	seen := map[string]bool{}
	return func(n int, ctx context.Context) error {
		first := c.seen()[n][0]
		mu.Lock()
		again := seen[first]
		seen[first] = true
		mu.Unlock()
		if strings.HasPrefix(first, "held") && !again {
			return held(ctx)
		}
		return nil
	}
}

// TestALargeBatchIsNotHedged: a batch whose request passes maxHedgeBytes is
// sent once, however long its call is held, and the hedge withheld is
// counted; one at the bound is hedged.
func TestALargeBatchIsNotHedged(t *testing.T) {
	c := &calls{}
	c.answer = heldOnce(c)
	l := openFake(t, c, Settings{Deadline: time.Second, Hedge: 20 * time.Millisecond})
	// A record whose request is exactly the bound, and one a byte past it.
	pad := func(n int) []byte {
		data := []byte("held-" + strings.Repeat("x", n))
		for requestSize(&pubsubpb.PubsubMessage{Data: data, OrderingKey: "la"}) > n {
			data = data[:len(data)-1]
		}
		return data
	}
	at, past := pad(maxHedgeBytes), pad(maxHedgeBytes)
	past = append(past, 'y')
	if got := requestSize(&pubsubpb.PubsubMessage{Data: at, OrderingKey: "la"}); got != maxHedgeBytes {
		t.Fatalf("the bound's record is %d bytes, want %d", got, maxHedgeBytes)
	}
	if err := wait(t, l.Publish("la", at, nil)); err != nil {
		t.Fatalf("a batch at the bound: %v", err)
	}
	if got := c.seen(); len(got) != 2 {
		t.Fatalf("a batch at the bound was sent %d times, want 2", len(got))
	}
	_, err := l.Publish("lb", past, nil).Wait(context.Background())
	if err == nil {
		t.Fatal("a held batch past the bound succeeded")
	}
	if got := c.seen(); len(got) != 3 {
		t.Fatalf("a batch past the bound was hedged: %d calls in all, want 3", len(got))
	}
	if got := l.TakeCalls(); got != (Calls{Batches: 2, Hedges: 1, Won: 1, Withheld: 1}) {
		t.Fatalf("the calls: %+v", got)
	}
}

// TestHedgesAreBudgeted: with no hedge banked, a held batch is sent once and
// the hedge withheld is counted; every batch's first call earns a tenth of a
// hedge, and the tenth call since the budget ran dry earns the hedge the next
// held batch takes.
func TestHedgesAreBudgeted(t *testing.T) {
	c := &calls{}
	c.answer = heldOnce(c)
	l := openFake(t, c, Settings{Deadline: time.Second, Hedge: 20 * time.Millisecond})
	l.calls.mu.Lock()
	l.calls.tenths = 0
	l.calls.mu.Unlock()
	// A dry budget: held-a earns a tenth and is not hedged.
	if _, err := l.Publish("la", []byte("held-a"), nil).Wait(context.Background()); err == nil {
		t.Fatal("a held batch with no hedge banked succeeded")
	}
	// Seven quick batches: eight tenths, and held-b's own call a ninth.
	for i := range 7 {
		if err := wait(t, l.Publish("lq", []byte(fmt.Sprintf("q#%d", i)), nil)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := l.Publish("lb", []byte("held-b"), nil).Wait(context.Background()); err == nil {
		t.Fatal("a held batch with nine tenths of a hedge banked succeeded")
	}
	// held-c's own call is the tenth: it is hedged, and the hedge answers.
	if err := wait(t, l.Publish("lc", []byte("held-c"), nil)); err != nil {
		t.Fatalf("a held batch with a hedge banked: %v", err)
	}
	if got := len(c.seen()); got != 1+7+1+2 {
		t.Fatalf("%d calls, want 11", got)
	}
	if got := l.TakeCalls(); got != (Calls{Batches: 10, Hedges: 1, Won: 1, Withheld: 2}) {
		t.Fatalf("the calls: %+v", got)
	}
}

// TestTheBudgetBanksAtMostTheBurst: however many calls earn it, at most
// hedgeBurst hedges are banked, and a log starts with them.
func TestTheBudgetBanksAtMostTheBurst(t *testing.T) {
	for _, earned := range []int{0, 1000} {
		b := newHedgeBudget()
		for range earned {
			b.sent()
		}
		for i := range hedgeBurst {
			if !b.take() {
				t.Fatalf("after %d calls, hedge %d of the burst was withheld", earned, i+1)
			}
		}
		if b.take() {
			t.Fatalf("after %d calls, a hedge past the burst was taken", earned)
		}
		for range hedgeShare - 1 {
			b.sent()
		}
		if b.take() {
			t.Fatal("a hedge was taken with nine tenths banked")
		}
		b.sent()
		if !b.take() {
			t.Fatal("a hedge was withheld with ten tenths banked")
		}
	}
}

func TestANegativeHedgeIsRefused(t *testing.T) {
	c := &calls{answer: func(int, context.Context) error { return nil }}
	if _, err := openLog(c.publish, "t", Settings{Deadline: time.Second, Hedge: -time.Millisecond}, time.Now); err == nil {
		t.Fatal("a log opens with a negative hedge")
	}
}

func waitUntil(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("%s did not happen", what)
		}
		time.Sleep(time.Millisecond)
	}
}
