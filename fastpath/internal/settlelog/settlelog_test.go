package settlelog

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

// The tests run on pstest, the client library's own fake server, which
// keeps each ordering key's order for a subscription with ordering on and
// redelivers what is not acknowledged. Real Pub/Sub's timing, quotas and
// regional order are the spike's probes P1-P4 (spike plan §5).

// failing fails a publish request that carries a message with the attribute
// fail: "once" fails it the first time with a code the client does not
// retry, "always" every time with one it does, "held" holds it until the
// gate opens and then fails it, and "slow" holds it until the gate opens.
// Every message the server is sent is counted, in sent.
type failing struct {
	mu   sync.Mutex
	seen map[string]bool
	sent []string
	gate chan struct{}
}

func (f *failing) React(req any) (bool, any, error) {
	r, ok := req.(*pubsubpb.PublishRequest)
	if !ok {
		return false, nil, nil
	}
	f.mu.Lock()
	for _, m := range r.Messages {
		f.sent = append(f.sent, string(m.Data))
	}
	gate := f.gate
	f.mu.Unlock()
	for _, m := range r.Messages {
		switch m.Attributes["fail"] {
		case "held":
			if gate != nil {
				<-gate
				return true, nil, status.Error(codes.InvalidArgument, "refused after a wait")
			}
		case "slow":
			if gate != nil {
				<-gate
			}
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, m := range r.Messages {
		switch m.Attributes["fail"] {
		case "once":
			if !f.seen[string(m.Data)] {
				f.seen[string(m.Data)] = true
				return true, nil, status.Error(codes.InvalidArgument, "refused once")
			}
		case "always":
			return true, nil, status.Error(codes.Unavailable, "unavailable")
		}
	}
	return false, nil, nil
}

type fakeLog struct {
	srv     *pstest.Server
	client  *pubsub.Client
	topic   string
	sub     string
	reactor *failing
}

func newFakeLog(t *testing.T, ordered bool, opts ...grpc.DialOption) *fakeLog {
	t.Helper()
	ctx := context.Background()
	reactor := &failing{seen: map[string]bool{}}
	srv := pstest.NewServer(pstest.ServerReactorOption{FuncName: "Publish", Reactor: reactor})
	t.Cleanup(func() { _ = srv.Close() })
	conn, err := grpc.NewClient(srv.Addr, append([]grpc.DialOption{grpc.WithTransportCredentials(insecure.NewCredentials())},
		opts...)...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client, err := pubsub.NewClient(ctx, "spike", option.WithGRPCConn(conn))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	f := &fakeLog{srv: srv, client: client, topic: "projects/spike/topics/settle-log",
		sub: "projects/spike/subscriptions/auditor", reactor: reactor}
	if _, err := client.TopicAdminClient.CreateTopic(ctx, &pubsubpb.Topic{Name: f.topic}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.SubscriptionAdminClient.CreateSubscription(ctx, &pubsubpb.Subscription{Name: f.sub, Topic: f.topic,
		EnableMessageOrdering: ordered, AckDeadlineSeconds: 10}); err != nil {
		t.Fatal(err)
	}
	return f
}

var settings = Settings{Deadline: 2 * time.Second}

func (f *fakeLog) log(t *testing.T) *Log {
	t.Helper()
	l, err := OpenLog(f.client, f.topic, settings)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(l.Stop)
	return l
}

func wait(t *testing.T, p *Pending) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := p.Wait(ctx)
	return err
}

// receive runs the subscription until want records have come, or within,
// acknowledging each unless nack says to ask for it again, and returns each
// lease's records in the order delivered. It fails if one lease's records
// were ever handled two at a time.
func (f *fakeLog) receive(t *testing.T, want int, within time.Duration, nack func(lease, data string) bool) map[string][]string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), within)
	defer cancel()
	var mu sync.Mutex
	got := map[string][]string{}
	busy := map[string]bool{}
	overlap := ""
	n := 0
	err := Subscribe(f.client, f.sub, -1).Receive(ctx, func(_ context.Context, d *Delivery) {
		mu.Lock()
		if busy[d.Lease] {
			overlap = d.Lease
		}
		busy[d.Lease] = true
		mu.Unlock()
		time.Sleep(200 * time.Microsecond)
		mu.Lock()
		defer mu.Unlock()
		busy[d.Lease] = false
		got[d.Lease] = append(got[d.Lease], string(d.Data))
		if nack != nil && nack(d.Lease, string(d.Data)) {
			d.Nack()
			return
		}
		d.Ack()
		if n++; n == want {
			cancel()
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	if n != want {
		t.Fatalf("%d of %d records delivered: %v", n, want, got)
	}
	if overlap != "" {
		t.Fatalf("two of %s's records were handled at once", overlap)
	}
	return got
}

func records(lease string, from, to int) []string {
	var out []string
	for i := from; i <= to; i++ {
		out = append(out, fmt.Sprintf("%s#%d", lease, i))
	}
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestEachLeaseIsDeliveredInOrder: records handed to a lease's key, among
// other leases', come to the auditor in that order, one at a time per lease.
func TestEachLeaseIsDeliveredInOrder(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	var pending []*Pending
	for i := 1; i <= 30; i++ {
		for _, lease := range []string{"la", "lb", "lc"} {
			pending = append(pending, l.Publish(lease, []byte(fmt.Sprintf("%s#%d", lease, i)), nil))
		}
	}
	for _, p := range pending {
		if err := wait(t, p); err != nil {
			t.Fatal(err)
		}
	}
	got := f.receive(t, 90, 10*time.Second, nil)
	for _, lease := range []string{"la", "lb", "lc"} {
		if !equal(got[lease], records(lease, 1, 30)) {
			t.Fatalf("%s came as %v", lease, got[lease])
		}
	}
	if err := wait(t, l.Publish("", []byte("x"), nil)); err == nil {
		t.Fatal("a record with no lease is published")
	}
}

// TestAFailedPublishPausesOnlyItsLease: a publish that fails pauses its
// lease's key, so nothing after it is stored before it; other leases go on;
// the owner resumes the key and republishes from the first that failed.
func TestAFailedPublishPausesOnlyItsLease(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	if err := wait(t, l.Publish("la", []byte("la#1"), nil)); err != nil {
		t.Fatal(err)
	}
	if err := wait(t, l.Publish("la", []byte("la#2"), map[string]string{"fail": "once"})); err == nil {
		t.Fatal("the refused publish succeeds")
	}
	if err := wait(t, l.Publish("la", []byte("la#3"), nil)); err == nil {
		t.Fatal("a record after a failed one is stored before it")
	}
	if err := wait(t, l.Publish("lb", []byte("lb#1"), nil)); err != nil {
		t.Fatalf("another lease's key is paused too: %v", err)
	}
	l.Resume("la")
	for _, r := range []string{"la#2", "la#3"} {
		if err := wait(t, l.Publish("la", []byte(r), nil)); err != nil {
			t.Fatalf("the republish of %s: %v", r, err)
		}
	}
	got := f.receive(t, 4, 10*time.Second, nil)
	if !equal(got["la"], records("la", 1, 3)) || !equal(got["lb"], records("lb", 1, 1)) {
		t.Fatalf("the log holds %v", got)
	}
}

// TestTheDeadlineCoversTheClientsRetries: a publish the server keeps
// answering with a code the client retries fails once the deadline passes,
// so no owner publish is still being retried after it.
func TestTheDeadlineCoversTheClientsRetries(t *testing.T) {
	f := newFakeLog(t, true)
	l, err := OpenLog(f.client, f.topic, Settings{Deadline: 300 * time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Stop()
	start := time.Now()
	err = wait(t, l.Publish("la", []byte("la#1"), map[string]string{"fail": "always"}))
	if took := time.Since(start); err == nil || took > 3*time.Second {
		t.Fatalf("a publish retried past its deadline: %v after %v", err, took)
	}
	if _, err := OpenLog(f.client, f.topic, Settings{}); err == nil {
		t.Error("a log opens with no deadline")
	}
}

// TestARecordNotAcknowledgedIsDeliveredAgain: assumption A1. A record
// asked for again comes back, and every record after it on its key, in
// order.
func TestARecordNotAcknowledgedIsDeliveredAgain(t *testing.T) {
	f := newFakeLog(t, true)
	l := f.log(t)
	for _, r := range records("la", 1, 4) {
		if err := wait(t, l.Publish("la", []byte(r), nil)); err != nil {
			t.Fatal(err)
		}
	}
	// A nack can reach the server before the client's own receipt of the
	// message extends its deadline, and then the record comes back only once
	// that deadline, ten seconds, has passed: assumption A1 says a record not
	// acknowledged comes back, not when. pstest delivers a key's next record
	// only once the one before is acknowledged, so this shows the nacked
	// record coming back ahead of those after it, not a replay of records
	// already delivered after it, which real Pub/Sub's ordering also does:
	// that is the spike's probe P3.
	nacked := false
	got := f.receive(t, 4, 40*time.Second, func(_, data string) bool {
		if data == "la#2" && !nacked {
			nacked = true
			return true
		}
		return false
	})
	// la#1, then la#2 asked for again, then la#2 to la#4, in order: a record
	// after it may come before the redelivery, but never acknowledged out of
	// order.
	seq := got["la"]
	if len(seq) < 5 || seq[0] != "la#1" || seq[1] != "la#2" {
		t.Fatalf("the deliveries were %v", seq)
	}
	last := seq[len(seq)-3:]
	if !equal(last, records("la", 2, 4)) {
		t.Fatalf("after the redelivery came %v", last)
	}
}

// TestTheRecordTopicIsKeyedByAuthorization: full records and outcomes carry
// their authorization and kind; nothing else goes there.
func TestTheRecordTopicIsKeyedByAuthorization(t *testing.T) {
	f := newFakeLog(t, false)
	r, err := OpenRecords(f.client, f.topic, settings)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	for _, kind := range []string{FullRecord, Outcome} {
		if err := wait(t, r.Publish("gwa-1", kind, []byte(kind))); err != nil {
			t.Fatal(err)
		}
	}
	for name, p := range map[string]*Pending{
		"no authorization": r.Publish("", FullRecord, []byte("x")),
		"another kind":     r.Publish("gwa-1", "settle", []byte("x")),
	} {
		if err := wait(t, p); err == nil {
			t.Errorf("a message with %s is published", name)
		}
	}
	msgs := f.srv.Messages()
	if len(msgs) != 2 {
		t.Fatalf("the topic holds %d messages", len(msgs))
	}
	for _, m := range msgs {
		if m.Attributes[AuthorizationAttr] != "gwa-1" || m.Attributes[KindAttr] != string(m.Data) || m.OrderingKey != "" {
			t.Fatalf("a record topic message: %+v", m)
		}
	}
}

func TestEndpointIsTheRegions(t *testing.T) {
	if got := Endpoint("us-central1"); got != "us-central1-pubsub.googleapis.com:443" {
		t.Fatal(got)
	}
}

// TestTheRecordTopicIsDeliveredWithItsAttributes: the staging consumer gets
// each message with its authorization and kind, several at once though
// they share no ordering key, and one it asks for again comes back.
func TestTheRecordTopicIsDeliveredWithItsAttributes(t *testing.T) {
	f := newFakeLog(t, false)
	r, err := OpenRecords(f.client, f.topic, settings)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	for i := range 4 {
		if err := wait(t, r.Publish(fmt.Sprint("gwa-", i), FullRecord, []byte(fmt.Sprint("record-", i)))); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var mu sync.Mutex
	got := map[string]string{}
	nacked := false
	inside, most := 0, 0
	both := make(chan struct{})
	var twice sync.Once
	err = SubscribeRecords(f.client, f.sub, -1).Receive(ctx, func(_ context.Context, d *RecordDelivery) {
		mu.Lock()
		inside++
		most = max(most, inside)
		if inside == 2 {
			twice.Do(func() { close(both) })
		}
		mu.Unlock()
		select {
		case <-both:
		case <-time.After(5 * time.Second):
		}
		mu.Lock()
		defer mu.Unlock()
		inside--
		if d.Kind != FullRecord || d.ID == "" || d.PublishTime.IsZero() {
			t.Errorf("a delivery %+v", d)
		}
		if d.Authorization == "gwa-0" && !nacked {
			nacked = true
			d.Nack()
			return
		}
		got[d.Authorization] = string(d.Data)
		d.Ack()
		if len(got) == 4 {
			cancel()
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	for i := range 4 {
		if got[fmt.Sprint("gwa-", i)] != fmt.Sprint("record-", i) {
			t.Fatalf("delivered %v", got)
		}
	}
	if most < 2 || !nacked {
		t.Fatalf("at most %d handled at once; asked again %v", most, nacked)
	}
}

// losing is a dial option whose streams lose the first delivery of the
// message with data data, as a stream does that the log sends a message on
// as its member closes it: the log holds the message for that member, and
// the member never receives it. The flag reports the loss.
func losing(data string) (grpc.DialOption, *atomic.Bool) {
	lost := &atomic.Bool{}
	return grpc.WithStreamInterceptor(func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn,
		method string, streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		s, err := streamer(ctx, desc, cc, method, opts...)
		if err != nil || method != "/google.pubsub.v1.Subscriber/StreamingPull" {
			return s, err
		}
		return &losingStream{ClientStream: s, data: data, lost: lost}, nil
	}), lost
}

type losingStream struct {
	grpc.ClientStream
	data string
	lost *atomic.Bool
}

// RecvMsg passes on each response but the lost message; a response that
// held only that one is not passed on at all.
func (s *losingStream) RecvMsg(m any) error {
	for {
		if err := s.ClientStream.RecvMsg(m); err != nil {
			return err
		}
		resp := m.(*pubsubpb.StreamingPullResponse)
		var kept []*pubsubpb.ReceivedMessage
		for _, rm := range resp.ReceivedMessages {
			if string(rm.GetMessage().GetData()) == s.data && s.lost.CompareAndSwap(false, true) {
				continue
			}
			kept = append(kept, rm)
		}
		if len(kept) > 0 || len(resp.ReceivedMessages) == 0 {
			resp.ReceivedMessages = kept
			return nil
		}
	}
}

// TestARecordMessageNeverReceivedComesBackSoon: a message of the record
// topic the log sent on the staging consumer's stream, and the consumer
// never received, is held for no longer than ackExtension, and then comes
// back, not after the client library's default minute.
func TestARecordMessageNeverReceivedComesBackSoon(t *testing.T) {
	t.Parallel()
	opt, lost := losing("record-0")
	f := newFakeLog(t, false, opt)
	r, err := OpenRecords(f.client, f.topic, settings)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Stop()
	if err := wait(t, r.Publish("gwa-0", FullRecord, []byte("record-0"))); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*ackExtension)
	defer cancel()
	began := time.Now()
	var took time.Duration
	err = SubscribeRecords(f.client, f.sub, -1).Receive(ctx, func(_ context.Context, d *RecordDelivery) {
		took = time.Since(began)
		d.Ack()
		cancel()
	})
	if err != nil {
		t.Fatal(err)
	}
	if !lost.Load() || took == 0 {
		t.Fatalf("lost %v; delivered after %v", lost.Load(), took)
	}
}
