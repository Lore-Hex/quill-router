package settlelog

import (
	"context"
	"fmt"
	"sync"
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
// retry, "always" every time with one it does.
type failing struct {
	mu   sync.Mutex
	seen map[string]bool
}

func (f *failing) React(req any) (bool, any, error) {
	r, ok := req.(*pubsubpb.PublishRequest)
	if !ok {
		return false, nil, nil
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
	srv    *pstest.Server
	client *pubsub.Client
	topic  string
	sub    string
}

func newFakeLog(t *testing.T, ordered bool) *fakeLog {
	t.Helper()
	ctx := context.Background()
	srv := pstest.NewServer(pstest.ServerReactorOption{FuncName: "Publish", Reactor: &failing{seen: map[string]bool{}}})
	t.Cleanup(func() { _ = srv.Close() })
	conn, err := grpc.NewClient(srv.Addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
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
		sub: "projects/spike/subscriptions/auditor"}
	if _, err := client.TopicAdminClient.CreateTopic(ctx, &pubsubpb.Topic{Name: f.topic}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.SubscriptionAdminClient.CreateSubscription(ctx, &pubsubpb.Subscription{Name: f.sub, Topic: f.topic,
		EnableMessageOrdering: ordered, AckDeadlineSeconds: 10}); err != nil {
		t.Fatal(err)
	}
	return f
}

var settings = Settings{Deadline: 2 * time.Second, Delay: time.Millisecond}

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
	l, err := OpenLog(f.client, f.topic, Settings{Deadline: 300 * time.Millisecond, Delay: time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Stop()
	start := time.Now()
	err = wait(t, l.Publish("la", []byte("la#1"), map[string]string{"fail": "always"}))
	if took := time.Since(start); err == nil || took > 3*time.Second {
		t.Fatalf("a publish retried past its deadline: %v after %v", err, took)
	}
	for _, s := range []Settings{{Deadline: 0, Delay: time.Millisecond}, {Deadline: time.Second}} {
		if _, err := OpenLog(f.client, f.topic, s); err == nil {
			t.Errorf("a log opens with %+v", s)
		}
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
	// acknowledged comes back, not when.
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
