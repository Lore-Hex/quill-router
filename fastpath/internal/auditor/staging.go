package auditor

import (
	"context"
	"crypto/sha256"
	"errors"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// StagingStore is what the staging consumer reads and writes in Spanner;
// *store.Store has it.
type StagingStore interface {
	FindLease(ctx context.Context, leaseID string) (store.LeaseRef, error)
	StageRecord(ctx context.Context, r store.StagedRecord) error
}

// Staged is one message of the record topic as the stager takes it.
type Staged interface {
	Authorization() string
	Kind() string
	Data() []byte
	ID() string
	Published() time.Time
	Ack()
	Nack()
}

// StagedSource delivers the record topic's messages to a handler, many at
// once.
type StagedSource interface {
	Receive(ctx context.Context, handle func(context.Context, Staged)) error
}

// FromRecordSubscription is the record topic's subscription as a
// StagedSource.
func FromRecordSubscription(s *settlelog.RecordSubscription) StagedSource { return recordSource{s} }

type recordSource struct{ s *settlelog.RecordSubscription }

func (r recordSource) Receive(ctx context.Context, handle func(context.Context, Staged)) error {
	return r.s.Receive(ctx, func(ctx context.Context, d *settlelog.RecordDelivery) { handle(ctx, staged{d}) })
}

type staged struct{ d *settlelog.RecordDelivery }

func (s staged) Authorization() string { return s.d.Authorization }
func (s staged) Kind() string          { return s.d.Kind }
func (s staged) Data() []byte          { return s.d.Data }
func (s staged) ID() string            { return s.d.ID }
func (s staged) Published() time.Time  { return s.d.PublishTime }
func (s staged) Ack()                  { s.d.Ack() }
func (s staged) Nack()                 { s.d.Nack() }

// Stager is the record topic's consumer (§4.9), standing for production's
// staging in ClickHouse (spike plan §2): it stages each full record under
// its authorization and digest, with its lease, and acknowledges it only
// once written, so an outage of the stage loses nothing. An outcome is
// acknowledged as it comes: the topic's export keeps it, and nothing reads
// it from the stage.
type Stager struct {
	store StagingStore
	alert func(authorization, what string)
}

// NewStager is the consumer, which tells alert of a message no retry makes
// a record of.
func NewStager(s StagingStore, alert func(authorization, what string)) (*Stager, error) {
	if s == nil || alert == nil {
		return nil, errors.New("auditor: a stager needs a store and someone to tell")
	}
	return &Stager{store: s, alert: alert}, nil
}

// Run stages what src delivers until ctx ends or src fails.
func (s *Stager) Run(ctx context.Context, src StagedSource) error {
	return src.Receive(ctx, s.Handle)
}

// Handle stages one message. A full record's lease is the one its
// authorization names; one that names none the store has, or a message of
// another kind, is told and acknowledged, since no retry stages it. A read
// or a write that fails asks for the message again.
func (s *Stager) Handle(ctx context.Context, m Staged) {
	switch m.Kind() {
	case settlelog.Outcome:
		m.Ack()
		return
	case settlelog.FullRecord:
	default:
		s.alert(m.Authorization(), "a record topic message of no kind the stager knows")
		m.Ack()
		return
	}
	leaseID, err := store.LeaseOfAuthorization(m.Authorization())
	if err != nil || len(m.Data()) == 0 {
		s.alert(m.Authorization(), "a full record of no lease's authorization")
		m.Ack()
		return
	}
	ref, err := s.store.FindLease(ctx, leaseID)
	switch {
	case errors.Is(err, store.ErrNoLease):
		// Never granted, or gone seven days after its close with its
		// work done: no winner of it needs the record.
		s.alert(m.Authorization(), "a full record of a lease the store does not have")
		m.Ack()
		return
	case err != nil:
		m.Nack()
		return
	}
	digest := sha256.Sum256(m.Data())
	if err := s.store.StageRecord(ctx, store.StagedRecord{AuthorizationID: m.Authorization(), Digest: digest[:],
		Ref: ref, Body: m.Data(), MessageID: m.ID(), PublishTime: m.Published()}); err != nil {
		m.Nack()
		return
	}
	m.Ack()
}
