// Package settlelog is the spike's Pub/Sub (design §4.1, §4.5, §4.9; spike
// plan §2): the settle log, where an owner's records for a lease go in order
// on one ordering key per lease, through the lease's region's locational
// endpoint; the record topic, where each request's full record and each
// winner's outcome go, unordered, keyed by authorization; and the auditor's
// ordered subscription to the settle log. What the records say is the
// owner's and the auditor's business: this package carries their bytes and
// keeps their order.
//
// It publishes through Pub/Sub's Publish call, not the client library's
// publisher: that publisher starts a publish's timeout only when its batch
// begins sending, after its batching and its key's queue, so a record could
// be stored long after its deadline, past the fence the auditor sets from
// it (§4.8); and resuming a paused key can send a record queued behind the
// failed one before the failed one's republish. Here each record's deadline
// runs from its hand-over, through its queue and every retry, a record is
// never sent once its deadline has passed, and a failure fails every record
// queued behind it before the key can be resumed.
package settlelog

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// Endpoint is a region's locational endpoint. Pub/Sub keeps one key's
// messages in the order it receives them only across publishers in one
// region (design §4.1; the spike's probe P1), so every publisher of a
// lease's records, its owner's and the auditor's ticks, uses the lease's
// region's.
func Endpoint(region string) string { return region + "-pubsub.googleapis.com:443" }

// Errors a publish can end with besides Pub/Sub's own.
var (
	// ErrDeadline: the record's deadline passed before it could be sent.
	ErrDeadline = errors.New("settlelog: the record's publish deadline passed")
	// ErrPaused: the lease's key is paused by an earlier failure.
	ErrPaused = errors.New("settlelog: the lease's key is paused by an earlier failure; resume it")
	// ErrStopped: the publisher is stopped.
	ErrStopped = errors.New("settlelog: the publisher is stopped")
)

// Settings are a publisher's. Deadline bounds each publish from its
// hand-over, through its queue and every retry (§4.5): shorter than the
// reaper's grace less twice the skew allowance, for the settle log.
type Settings struct {
	Deadline time.Duration
}

// Pub/Sub's limits on one Publish call: its messages, and its request's
// size, 10 MB, which a batch keeps under with room for the topic's name.
const (
	maxBatchMessages = 1000
	maxBatchBytes    = 9 << 20
)

// requestSize is a message's part of a Publish call's request: its
// encoding, attributes and ordering key included, with its field's tag and
// length.
func requestSize(m *pubsubpb.PubsubMessage) int {
	n := proto.Size(m)
	return protowire.SizeTag(2) + protowire.SizeVarint(uint64(n)) + n
}

// publishFunc is Pub/Sub's Publish call.
type publishFunc func(ctx context.Context, req *pubsubpb.PublishRequest) (*pubsubpb.PublishResponse, error)

func publisherOf(client *pubsub.Client) publishFunc {
	return func(ctx context.Context, req *pubsubpb.PublishRequest) (*pubsubpb.PublishResponse, error) {
		// The call's own retries end with ctx, at the deadline.
		return client.TopicAdminClient.Publish(ctx, req)
	}
}

// Pending is a record handed over and not yet acknowledged.
type Pending struct {
	msg      *pubsubpb.PubsubMessage
	size     int
	deadline time.Time
	done     chan struct{}
	id       string
	err      error
}

func newPending(msg *pubsubpb.PubsubMessage, deadline time.Time) *Pending {
	return &Pending{msg: msg, size: requestSize(msg), deadline: deadline, done: make(chan struct{})}
}

func (p *Pending) finish(id string, err error) {
	p.id, p.err = id, err
	close(p.done)
}

// Wait waits for the record's acknowledgement and returns its message ID,
// or its failure. A failure on the settle log pauses the lease's key: every
// record handed to it after this one fails too, until Resume. A Wait that
// gives up on ctx leaves the publish under way, within its deadline.
func (p *Pending) Wait(ctx context.Context) (string, error) {
	select {
	case <-p.done:
		return p.id, p.err
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// Log publishes a region's settle log: one sender per lease key, one
// Publish call at a time, each carrying every record queued for the key.
type Log struct {
	publish  publishFunc
	topic    string
	deadline time.Duration
	clock    func() time.Time

	mu      sync.Mutex
	keys    map[string]*key
	stopped bool
	senders sync.WaitGroup
}

// key is a lease's ordering key: the records handed over and not yet sent,
// whether a sender is at work on it, and whether a failure paused it.
type key struct {
	queue   []*Pending
	sending bool
	paused  bool
}

// OpenLog opens the settle log's topic for publishing, through client's
// endpoint, which is the region's (Endpoint).
func OpenLog(client *pubsub.Client, topic string, s Settings) (*Log, error) {
	return openLog(publisherOf(client), topic, s, time.Now)
}

func openLog(publish publishFunc, topic string, s Settings, clock func() time.Time) (*Log, error) {
	if s.Deadline <= 0 || topic == "" {
		return nil, fmt.Errorf("settlelog: a topic %q and a deadline of %v", topic, s.Deadline)
	}
	return &Log{publish: publish, topic: topic, deadline: s.Deadline, clock: clock, keys: map[string]*key{}}, nil
}

// Publish hands a record to its lease's key without waiting. Records handed
// to one key are stored in the order of the calls, so an owner hands them
// over under the lease's lock and waits outside it (§4.2).
func (l *Log) Publish(lease string, data []byte, attrs map[string]string) *Pending {
	p := newPending(&pubsubpb.PubsubMessage{Data: data, Attributes: attrs, OrderingKey: lease}, l.clock().Add(l.deadline))
	if lease == "" {
		p.finish("", errors.New("settlelog: a record needs its lease's key"))
		return p
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stopped {
		p.finish("", ErrStopped)
		return p
	}
	k := l.keys[lease]
	if k == nil {
		k = &key{}
		l.keys[lease] = k
	}
	if k.paused {
		p.finish("", ErrPaused)
		return p
	}
	k.queue = append(k.queue, p)
	if !k.sending {
		k.sending = true
		l.senders.Add(1)
		go l.send(lease, k)
	}
	return p
}

// Resume lets a lease's paused key take records again. Every record queued
// when the key failed has failed already, so the owner's republish, from the
// first that failed, with its sequence numbers, comes before anything new
// (§4.5).
func (l *Log) Resume(lease string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if k := l.keys[lease]; k != nil {
		k.paused = false
		if !k.sending && len(k.queue) == 0 {
			delete(l.keys, lease)
		}
	}
}

// Stop sends what was handed over, waits for it, and takes no more.
func (l *Log) Stop() {
	l.mu.Lock()
	l.stopped = true
	l.mu.Unlock()
	l.senders.Wait()
}

// send is a key's sender: it takes what is queued, a batch at a time, and
// sends each before its first record's deadline, which is the earliest. A
// batch whose deadline has passed is not sent. A failure fails the batch
// and every record queued behind it, and pauses the key.
func (l *Log) send(lease string, k *key) {
	defer l.senders.Done()
	for {
		l.mu.Lock()
		if len(k.queue) == 0 {
			k.sending = false
			if !k.paused {
				delete(l.keys, lease)
			}
			l.mu.Unlock()
			return
		}
		batch := takeBatch(&k.queue)
		l.mu.Unlock()
		ids, err := l.sendBatch(batch)
		if err == nil {
			for i, p := range batch {
				p.finish(ids[i], nil)
			}
			continue
		}
		// The key is paused, and every record queued behind the batch
		// failed, before anyone learns the batch failed: so a resume and a
		// republish that follow the failure find the key paused no more.
		l.mu.Lock()
		k.paused = true
		for _, p := range k.queue {
			p.finish("", ErrPaused)
		}
		k.queue = nil
		k.sending = false
		for _, p := range batch {
			p.finish("", err)
		}
		l.mu.Unlock()
		return
	}
}

// takeBatch takes records from the front of queue, within Pub/Sub's limits.
// A record too large for a request by itself goes alone, and Pub/Sub's
// refusal fails it like any other.
func takeBatch(queue *[]*Pending) []*Pending {
	n, size := 0, 0
	for n < len(*queue) && n < maxBatchMessages {
		size += (*queue)[n].size
		if n > 0 && size > maxBatchBytes {
			break
		}
		n++
	}
	batch := (*queue)[:n:n]
	*queue = (*queue)[n:]
	return batch
}

// sendBatch sends one batch and returns its message IDs, or its error.
func (l *Log) sendBatch(batch []*Pending) ([]string, error) {
	deadline := batch[0].deadline
	if !l.clock().Before(deadline) {
		return nil, ErrDeadline
	}
	msgs := make([]*pubsubpb.PubsubMessage, len(batch))
	for i, p := range batch {
		msgs[i] = p.msg
	}
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	defer cancel()
	resp, err := l.publish(ctx, &pubsubpb.PublishRequest{Topic: l.topic, Messages: msgs})
	if err == nil && len(resp.GetMessageIds()) != len(batch) {
		err = fmt.Errorf("settlelog: %d message IDs for %d records", len(resp.GetMessageIds()), len(batch))
	}
	if err != nil {
		return nil, err
	}
	return resp.GetMessageIds(), nil
}

// Records publishes the record topic, unordered, each record in its own
// Publish call within its deadline.
type Records struct {
	publish  publishFunc
	topic    string
	deadline time.Duration
	clock    func() time.Time

	mu      sync.Mutex
	stopped bool
	calls   sync.WaitGroup
}

// OpenRecords opens the record topic for publishing.
func OpenRecords(client *pubsub.Client, topic string, s Settings) (*Records, error) {
	return openRecords(publisherOf(client), topic, s, time.Now)
}

func openRecords(publish publishFunc, topic string, s Settings, clock func() time.Time) (*Records, error) {
	if s.Deadline <= 0 || topic == "" {
		return nil, fmt.Errorf("settlelog: a topic %q and a deadline of %v", topic, s.Deadline)
	}
	return &Records{publish: publish, topic: topic, deadline: s.Deadline, clock: clock}, nil
}

// The record topic's attributes: the authorization a message is keyed by,
// and its kind, which the staging consumer and the export's reader both
// read (§4.9).
const (
	AuthorizationAttr = "authorization"
	KindAttr          = "kind"
	FullRecord        = "record"
	Outcome           = "outcome"
)

// Publish hands a full record or an outcome to the record topic.
func (r *Records) Publish(authorization, kind string, data []byte) *Pending {
	p := newPending(&pubsubpb.PubsubMessage{Data: data,
		Attributes: map[string]string{AuthorizationAttr: authorization, KindAttr: kind}}, r.clock().Add(r.deadline))
	if authorization == "" || (kind != FullRecord && kind != Outcome) {
		p.finish("", fmt.Errorf("settlelog: a record topic message keyed %q, of kind %q", authorization, kind))
		return p
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.stopped {
		p.finish("", ErrStopped)
		return p
	}
	r.calls.Add(1)
	go func() {
		defer r.calls.Done()
		ctx, cancel := context.WithDeadline(context.Background(), p.deadline)
		defer cancel()
		resp, err := r.publish(ctx, &pubsubpb.PublishRequest{Topic: r.topic, Messages: []*pubsubpb.PubsubMessage{p.msg}})
		if err == nil && len(resp.GetMessageIds()) != 1 {
			err = fmt.Errorf("settlelog: %d message IDs for one record", len(resp.GetMessageIds()))
		}
		if err != nil {
			p.finish("", err)
			return
		}
		p.finish(resp.GetMessageIds()[0], nil)
	}()
	return p
}

// Stop waits for the records handed over, and takes no more.
func (r *Records) Stop() {
	r.mu.Lock()
	r.stopped = true
	r.mu.Unlock()
	r.calls.Wait()
}

// shutdownTimeout bounds how long a stopping member waits for the client
// library to ask for its outstanding records again.
var shutdownTimeout = 10 * time.Second

// beforeKeeping, when a test sets it, runs as each delivery's callback
// begins, before the delivery is kept: as when the client library, stopping,
// stopped waiting for a callback that had not yet run.
var beforeKeeping func()

// ackExtension is how long the log holds a record for a member at a time:
// each extension of its deadline the client library asks for, and the
// deadline of each record the log sends on the member's stream, the one the
// stream opens with and any the library sends it later. A record the log
// sends and the member never receives is held that long: as when the client
// library, stopping, asks for the member's records again and the log sends
// one straight back on the member's stream before that stream is closed.
// Once the deadline passes the log may deliver the record again, as the
// subscription's retry policy allows. Unset, the library asks for a minute,
// and on a subscription with exactly-once delivery for a minute at least
// once it learns of it, and the record's lease waits behind the hold; so
// both the least and the most each extension may be are set to it.
const ackExtension = 10 * time.Second

// Subscription is the auditor's subscription to a region's settle log,
// which must have message ordering on. It runs one Receive at a time.
type Subscription struct {
	sub *pubsub.Subscriber

	mu          sync.Mutex
	active      map[string]bool
	outstanding map[*pubsub.Message]bool
}

// Subscribe opens it. maxOutstanding bounds the records delivered and not
// yet acknowledged; a negative one is no bound. When Receive ends, the
// records it delivered and was not asked to acknowledge are asked for again
// at once, so another member can get them (assumption A1). One the log sends
// back on this member's closing stream is held until its deadline,
// ackExtension, has passed, and may then be delivered again.
func Subscribe(client *pubsub.Client, subscription string, maxOutstanding int) *Subscription {
	sub := client.Subscriber(subscription)
	sub.ReceiveSettings.MaxOutstandingMessages = maxOutstanding
	sub.ReceiveSettings.MinDurationPerAckExtension = ackExtension
	sub.ReceiveSettings.MaxDurationPerAckExtension = ackExtension
	sub.ReceiveSettings.ShutdownOptions = &pubsub.ShutdownOptions{Behavior: pubsub.ShutdownBehaviorNackImmediately,
		Timeout: shutdownTimeout}
	return &Subscription{sub: sub, active: map[string]bool{}, outstanding: map[*pubsub.Message]bool{}}
}

// Delivery is one record as the log delivered it.
type Delivery struct {
	Lease       string
	Data        []byte
	Attrs       map[string]string
	ID          string
	PublishTime time.Time
	// Attempt counts deliveries of this message when the subscription
	// tracks them, else it is nil.
	Attempt *int

	msg *pubsub.Message
	sub *Subscription
}

// Ack acknowledges the record, once what it did is committed (assumption
// A1, design §4.8): a record not acknowledged is delivered again.
func (d *Delivery) Ack() { d.sub.settle(d.msg, true) }

// Nack asks for the record again now.
func (d *Delivery) Nack() { d.sub.settle(d.msg, false) }

// settle acknowledges a delivered record, or asks for it again, once. A
// record its member's stop has asked for again already stays so: its
// handler's acknowledgement after the stop does nothing, and the record
// comes back, which A1's redelivery allows.
func (s *Subscription) settle(m *pubsub.Message, ack bool) {
	s.mu.Lock()
	out := s.outstanding[m]
	delete(s.outstanding, m)
	s.mu.Unlock()
	switch {
	case !out:
	case ack:
		m.Ack()
	default:
		m.Nack()
	}
}

// Receive delivers records until ctx ends or the subscription fails: each
// lease's in the order the log stored them, and one at a time, the next
// only once handle has returned for the one before; leases concurrently.
// handle acknowledges a record only by Ack: a record it returns without
// acknowledging stays outstanding, and comes back if the member stops.
//
// The client library keeps one lease's records one at a time only until it
// begins to shut down, when it stops waiting for a handler still running.
// So Receive itself never hands a record over once ctx has ended, nor while
// another of its lease's is being handled: such a record is asked for again,
// and comes back in its order. Nor does the library always let go of a
// record its handler returned without settling, or one whose handler is
// still running when it returns: it can go on extending its deadline, for up
// to an hour. So when Receive returns it asks for every such record again
// itself.
func (s *Subscription) Receive(ctx context.Context, handle func(context.Context, *Delivery)) error {
	// done is set, under s.mu, once Receive has asked again for what it
	// held: a callback the library left running past its return keeps
	// nothing, and asks for its record again too.
	done := false
	err := s.sub.Receive(ctx, func(cctx context.Context, m *pubsub.Message) {
		if beforeKeeping != nil {
			beforeKeeping()
		}
		lease := m.OrderingKey
		s.mu.Lock()
		if done || ctx.Err() != nil || cctx.Err() != nil || s.active[lease] {
			s.mu.Unlock()
			m.Nack()
			return
		}
		s.active[lease] = true
		s.outstanding[m] = true
		s.mu.Unlock()
		defer func() {
			s.mu.Lock()
			delete(s.active, lease)
			s.mu.Unlock()
		}()
		handle(cctx, &Delivery{Lease: lease, Data: m.Data, Attrs: m.Attributes, ID: m.ID,
			PublishTime: m.PublishTime, Attempt: m.DeliveryAttempt, msg: m, sub: s})
	})
	s.mu.Lock()
	done = true
	left := s.outstanding
	s.outstanding = map[*pubsub.Message]bool{}
	s.mu.Unlock()
	for m := range left {
		m.Nack()
	}
	return err
}

// RecordSubscription is the record topic's staging consumer's subscription
// (§4.9): unordered, so its messages are handled concurrently, each
// acknowledged by its handler once staged.
type RecordSubscription struct {
	sub *pubsub.Subscriber

	mu          sync.Mutex
	outstanding map[*pubsub.Message]bool
}

// SubscribeRecords opens it. maxOutstanding bounds the messages delivered
// and not yet settled; a negative one is no bound.
func SubscribeRecords(client *pubsub.Client, subscription string, maxOutstanding int) *RecordSubscription {
	sub := client.Subscriber(subscription)
	sub.ReceiveSettings.MaxOutstandingMessages = maxOutstanding
	sub.ReceiveSettings.MinDurationPerAckExtension = ackExtension
	sub.ReceiveSettings.MaxDurationPerAckExtension = ackExtension
	sub.ReceiveSettings.ShutdownOptions = &pubsub.ShutdownOptions{Behavior: pubsub.ShutdownBehaviorNackImmediately,
		Timeout: shutdownTimeout}
	return &RecordSubscription{sub: sub, outstanding: map[*pubsub.Message]bool{}}
}

// RecordDelivery is one message of the record topic as it was delivered:
// its authorization and kind, from its attributes.
type RecordDelivery struct {
	Authorization string
	Kind          string
	Data          []byte
	ID            string
	PublishTime   time.Time

	msg *pubsub.Message
	sub *RecordSubscription
}

// Ack acknowledges the message once it is staged: it is not delivered
// again.
func (d *RecordDelivery) Ack() { d.sub.settle(d.msg, true) }

// Nack asks for the message again.
func (d *RecordDelivery) Nack() { d.sub.settle(d.msg, false) }

// settle acknowledges a delivered message, or asks for it again, once, as
// Subscription's does: one its consumer's stop has asked for again already
// stays so.
func (s *RecordSubscription) settle(m *pubsub.Message, ack bool) {
	s.mu.Lock()
	out := s.outstanding[m]
	delete(s.outstanding, m)
	s.mu.Unlock()
	switch {
	case !out:
	case ack:
		m.Ack()
	default:
		m.Nack()
	}
}

// Receive delivers messages until ctx ends or the subscription fails, many
// at once. handle settles each: Ack once it is staged, Nack to have it
// again. A message whose handler returns without settling it, as the
// service's does for one that comes once it is stopping, or whose handler
// still runs when Receive returns, the client library can go on holding,
// extending its deadline for up to an hour, as Subscription's Receive
// says; so when Receive returns it asks for each such message again itself.
func (s *RecordSubscription) Receive(ctx context.Context, handle func(context.Context, *RecordDelivery)) error {
	// done is as Subscription's Receive keeps it: a callback the library
	// left running past Receive's return keeps nothing, and asks for its
	// message again, as one that begins once ctx has ended does.
	done := false
	err := s.sub.Receive(ctx, func(cctx context.Context, m *pubsub.Message) {
		if beforeKeeping != nil {
			beforeKeeping()
		}
		s.mu.Lock()
		if done || ctx.Err() != nil || cctx.Err() != nil {
			s.mu.Unlock()
			m.Nack()
			return
		}
		s.outstanding[m] = true
		s.mu.Unlock()
		handle(cctx, &RecordDelivery{Authorization: m.Attributes[AuthorizationAttr], Kind: m.Attributes[KindAttr],
			Data: m.Data, ID: m.ID, PublishTime: m.PublishTime, msg: m, sub: s})
	})
	s.mu.Lock()
	done = true
	left := s.outstanding
	s.outstanding = map[*pubsub.Message]bool{}
	s.mu.Unlock()
	for m := range left {
		m.Nack()
	}
	return err
}
