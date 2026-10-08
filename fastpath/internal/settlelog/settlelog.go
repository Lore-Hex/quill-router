// Package settlelog is the spike's Pub/Sub (design §4.1, §4.5, §4.9; spike
// plan §2): the settle log, where an owner's records for a lease go in order
// on one ordering key per lease, through the lease's region's locational
// endpoint; the record topic, where each request's full record and each
// winner's outcome go, unordered, keyed by authorization; and the auditor's
// ordered subscription to the settle log. What the records say is the
// owner's and the auditor's business: this package carries their bytes and
// keeps their order.
package settlelog

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cloud.google.com/go/pubsub/v2"
)

// Endpoint is a region's locational endpoint. Pub/Sub keeps one key's
// messages in the order it receives them only across publishers in one
// region (design §4.1; the spike's probe P1), so every publisher of a
// lease's records, its owner's and the auditor's ticks, uses the lease's
// region's.
func Endpoint(region string) string { return region + "-pubsub.googleapis.com:443" }

// Settings are a publisher's. Deadline bounds each publish, the request and
// the client library's own retries together (§4.5): shorter than the
// reaper's grace less twice the skew allowance, for the settle log. Delay is
// the longest a publish waits to be sent with others.
type Settings struct {
	Deadline time.Duration
	Delay    time.Duration
}

func (s Settings) apply(p *pubsub.Publisher) error {
	if s.Deadline <= 0 || s.Delay <= 0 {
		return fmt.Errorf("settlelog: a deadline of %v and a delay of %v", s.Deadline, s.Delay)
	}
	p.PublishSettings.Timeout = s.Deadline
	p.PublishSettings.DelayThreshold = s.Delay
	return nil
}

// Log publishes a region's settle log.
type Log struct {
	pub *pubsub.Publisher
}

// OpenLog opens the settle log's topic for publishing, with ordering on.
func OpenLog(client *pubsub.Client, topic string, s Settings) (*Log, error) {
	pub := client.Publisher(topic)
	pub.EnableMessageOrdering = true
	if err := s.apply(pub); err != nil {
		return nil, err
	}
	return &Log{pub: pub}, nil
}

// Publish hands a record to its lease's key without waiting. Records handed
// to one key are stored in the order of the calls, so an owner hands them
// over under the lease's lock and waits outside it (§4.2).
func (l *Log) Publish(lease string, data []byte, attrs map[string]string) *Pending {
	if lease == "" {
		return failed(errors.New("settlelog: a record needs its lease's key"))
	}
	return &Pending{res: l.pub.Publish(context.Background(),
		&pubsub.Message{Data: data, Attributes: attrs, OrderingKey: lease})}
}

// Resume lets a lease's paused key take records again. The owner then
// republishes the records from the first that failed, with their sequence
// numbers, before anything new (§4.5).
func (l *Log) Resume(lease string) { l.pub.ResumePublish(lease) }

// Stop sends what was handed over, waits for it, and stops.
func (l *Log) Stop() { l.pub.Stop() }

// Pending is a record handed over and not yet acknowledged.
type Pending struct {
	res *pubsub.PublishResult
	err error
}

func failed(err error) *Pending { return &Pending{err: err} }

// Wait waits for the record's acknowledgement and returns its message ID,
// or its failure. A failure on the settle log pauses the lease's key: every
// record handed to it after this one fails too, until Resume. A Wait that
// gives up on ctx leaves the publish under way: it may still be stored, as
// the auditor's boundary S allows for (§4.5).
func (p *Pending) Wait(ctx context.Context) (string, error) {
	if p.err != nil {
		return "", p.err
	}
	return p.res.Get(ctx)
}

// Records publishes the record topic.
type Records struct {
	pub *pubsub.Publisher
}

// OpenRecords opens the record topic for publishing, unordered.
func OpenRecords(client *pubsub.Client, topic string, s Settings) (*Records, error) {
	pub := client.Publisher(topic)
	if err := s.apply(pub); err != nil {
		return nil, err
	}
	return &Records{pub: pub}, nil
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
	if authorization == "" || (kind != FullRecord && kind != Outcome) {
		return failed(fmt.Errorf("settlelog: a record topic message keyed %q, of kind %q", authorization, kind))
	}
	return &Pending{res: r.pub.Publish(context.Background(), &pubsub.Message{Data: data,
		Attributes: map[string]string{AuthorizationAttr: authorization, KindAttr: kind}})}
}

// Stop sends what was handed over, waits for it, and stops.
func (r *Records) Stop() { r.pub.Stop() }

// Subscription is the auditor's subscription to a region's settle log,
// which must have message ordering on.
type Subscription struct {
	sub *pubsub.Subscriber
}

// Subscribe opens it. maxOutstanding bounds the records delivered and not
// yet acknowledged; a negative one is no bound.
func Subscribe(client *pubsub.Client, subscription string, maxOutstanding int) *Subscription {
	sub := client.Subscriber(subscription)
	sub.ReceiveSettings.MaxOutstandingMessages = maxOutstanding
	return &Subscription{sub: sub}
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
}

// Ack acknowledges the record, once what it did is committed (assumption
// A1, design §4.8): a record not acknowledged is delivered again, and so is
// every record after it on its lease's key.
func (d *Delivery) Ack() { d.msg.Ack() }

// Nack asks for the record again now, with every record after it on its
// lease's key.
func (d *Delivery) Nack() { d.msg.Nack() }

// Receive delivers records until ctx ends or the subscription fails: each
// lease's in the order the log stored them, and one at a time, the next
// only once handle has returned for the one before; leases concurrently.
func (s *Subscription) Receive(ctx context.Context, handle func(context.Context, *Delivery)) error {
	return s.sub.Receive(ctx, func(ctx context.Context, m *pubsub.Message) {
		handle(ctx, &Delivery{Lease: m.OrderingKey, Data: m.Data, Attrs: m.Attributes, ID: m.ID,
			PublishTime: m.PublishTime, Attempt: m.DeliveryAttempt, msg: m})
	})
}
