package auditor

import (
	"context"
	"crypto/sha256"
	"errors"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// StagingStore is what the staging consumer reads and writes in Spanner;
// *store.Store has it.
type StagingStore interface {
	FindLease(ctx context.Context, leaseID string) (store.LeaseRef, error)
	StageRecords(ctx context.Context, rs []store.StagedRecord) ([]error, error)
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

// Stager is the record topic's consumer (§4.9): it stages each full record under
// its authorization and digest, with its lease, and acknowledges it only
// once written, so an outage of the stage loses nothing. An outcome is
// acknowledged as it comes: the topic's export keeps it, and nothing reads
// it from the stage.
//
// The records go to the store together: those that come while a
// transaction stages others wait and go in the next, up to
// store.MaxStageRecords, so a transaction stages as many as came while the
// last ran, one when they come one at a time, and none waits for a timer.
// Each record has its own answer, as it would alone.
type Stager struct {
	store StagingStore
	alert func(authorization, what string)

	mu      sync.Mutex
	waiting []*staging
	running bool
}

// staging is one record waiting for its transaction, and its answer.
type staging struct {
	r    store.StagedRecord
	done chan error
}

// NewStager is the consumer, which tells alert of a message no retry makes
// a record of.
func NewStager(s StagingStore, alert func(authorization, what string)) (*Stager, error) {
	return newStager(s, alert, processCalls)
}

// newStager is NewStager noting its calls under way in calls.
func newStager(s StagingStore, alert func(authorization, what string), calls *inFlight) (*Stager, error) {
	if s == nil || alert == nil {
		return nil, errors.New("auditor: a stager needs a store and someone to tell")
	}
	return &Stager{store: boundStagingStore{s: s, bound: bound{d: StoreDeadline, j: newJournal(nil), calls: calls}},
		alert: alert}, nil
}

// Run stages what src delivers until ctx ends or src fails.
func (s *Stager) Run(ctx context.Context, src StagedSource) error {
	return src.Receive(ctx, s.Handle)
}

// Handle stages one message. A full record's lease is the one its
// authorization names; one that names none the store has, or a message of
// another kind, is told and acknowledged, since no retry stages it. One
// whose lease has retired is acknowledged and staged for no one: the lease's
// work is done, so it is a redelivery or a copy no winner names. A read or a
// write that fails asks for the message again.
func (s *Stager) Handle(ctx context.Context, m Staged) {
	switch m.Kind() {
	case settlelog.Outcome:
		m.Ack()
		return
	case settlelog.FullRecord:
	default:
		s.alert(m.Authorization(), AlertUnknownRecordKind)
		m.Ack()
		return
	}
	leaseID, err := store.LeaseOfAuthorization(m.Authorization())
	if err != nil || len(m.Data()) == 0 {
		s.alert(m.Authorization(), AlertNoLeaseAuth)
		m.Ack()
		return
	}
	ref, err := s.store.FindLease(ctx, leaseID)
	switch {
	case errors.Is(err, store.ErrNoLease):
		// Never granted, or gone seven days after its close with its
		// work done: no winner of it needs the record.
		s.alert(m.Authorization(), AlertFullRecordNoLease)
		m.Ack()
		return
	case err != nil:
		m.Nack()
		return
	}
	digest := sha256.Sum256(m.Data())
	err = s.stage(ctx, store.StagedRecord{AuthorizationID: m.Authorization(), Digest: digest[:],
		Ref: ref, Body: m.Data(), MessageID: m.ID(), PublishTime: m.Published()})
	switch {
	case errors.Is(err, store.ErrNoLease):
		s.alert(m.Authorization(), AlertFullRecordNoLease)
	case errors.Is(err, store.ErrRetired):
	case err != nil:
		m.Nack()
		return
	}
	m.Ack()
}

// stage stages r with the records that wait with it and answers for r: its
// own error, or the transaction's, or ctx's if it ends first, when r may
// still be staged, as a redelivery would stage it again.
func (s *Stager) stage(ctx context.Context, r store.StagedRecord) error {
	w := &staging{r: r, done: make(chan error, 1)}
	s.mu.Lock()
	s.waiting = append(s.waiting, w)
	start := !s.running
	s.running = true
	s.mu.Unlock()
	if start {
		go s.drain(context.WithoutCancel(ctx))
	}
	select {
	case err := <-w.done:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

// drain stages what waits, a transaction at a time, until nothing does.
// Each call is under the store's deadline (boundStagingStore).
func (s *Stager) drain(ctx context.Context) {
	for {
		s.mu.Lock()
		batch := s.take()
		if len(batch) == 0 {
			s.running = false
			s.mu.Unlock()
			return
		}
		s.mu.Unlock()
		rs := make([]store.StagedRecord, len(batch))
		for i, w := range batch {
			rs[i] = w.r
		}
		errs, err := s.store.StageRecords(ctx, rs)
		for i, w := range batch {
			if err != nil {
				w.done <- err
			} else {
				w.done <- errs[i]
			}
		}
	}
}

// take removes from waiting the next transaction's records: the oldest, up
// to store.MaxStageRecords and store.MaxStageBytes, and at least one. Its
// caller holds mu.
func (s *Stager) take() []*staging {
	n, bytes := 0, 0
	for n < len(s.waiting) && n < store.MaxStageRecords {
		size := len(s.waiting[n].r.Body)
		if n > 0 && bytes+size > store.MaxStageBytes {
			break
		}
		bytes += size
		n++
	}
	batch := s.waiting[:n:n]
	s.waiting = s.waiting[n:]
	return batch
}
