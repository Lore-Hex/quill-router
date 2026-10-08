package owner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// The answers a request can get besides its own.
var (
	// ErrNoRoom: the lease has no room for the hold and its buffer.
	ErrNoRoom = errors.New("owner: the lease has no room for the hold")
	// ErrPastCutoff: the lease is past its cutoff, or its owner let it go,
	// and no answer stands for the request. A terminal goes to the lease's
	// drain log at once (§4.5); an admission goes elsewhere.
	ErrPastCutoff = errors.New("owner: past the lease's cutoff")
	// ErrPublishing: the lease's publishes are failing, so it admits
	// nothing new until one succeeds again (§4.5).
	ErrPublishing = errors.New("owner: the lease's publishes are failing")
	// ErrRetry: the record was not acknowledged in time; the decision
	// stands, and a retry finds it. A heartbeat gets it too past the cutoff,
	// which stops its stream (§4.3).
	ErrRetry = errors.New("owner: retry")
	// ErrUnknownHold: the lease admitted no such authorization.
	ErrUnknownHold = errors.New("owner: no such hold under the lease")
	// ErrDecided: a heartbeat for a hold whose terminal is decided.
	ErrDecided = errors.New("owner: the hold has its terminal")
	// ErrStale, ErrRejected and ErrDeadlinePassed are a heartbeat's (§4.5).
	ErrStale          = errors.New("owner: a stale heartbeat")
	ErrRejected       = errors.New("owner: a heartbeat the hold cannot take")
	ErrDeadlinePassed = errors.New("owner: the heartbeat's record was acknowledged after the deadline it echoed")
)

// Admission is an authorize's: the hold's estimate e, whether it is a
// stream, which heartbeats, and the boot binding of the enclave that asked.
type Admission struct {
	Estimate int64
	Stream   bool
	Boot     []byte
}

// Admitted is an admission's answer.
type Admitted struct {
	Auth      string
	Lease     string
	EndOfLife time.Time
}

// Admit holds e against the lease (§4.2, §4.4): only while it has room,
// free at least e with the new hold's own buffer counted, and before its
// cutoff, read after the hold is recorded and undone if it has passed. A
// stream's buffer joins the lease's at its first heartbeat, since until
// then it may never run; another hold's at once.
func (l *Lease) Admit(a Admission) (Admitted, error) {
	if a.Estimate < 0 || len(a.Boot) == 0 {
		return Admitted{}, fmt.Errorf("owner: an estimate of %d with a boot binding of %d bytes", a.Estimate, len(a.Boot))
	}
	auth, err := l.o.cfg.NewAuthorization(l.id)
	if err != nil {
		return Admitted{}, err
	}
	if !record.ValidAuth(auth) {
		return Admitted{}, fmt.Errorf("owner: minted authorization %q", auth)
	}
	over := l.o.cfg.overrun(a.Estimate)
	need, ok := add(a.Estimate, over)
	if !ok {
		return Admitted{}, fmt.Errorf("owner: a hold of %d with a buffer of %d", a.Estimate, over)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.let {
		return Admitted{}, ErrPastCutoff
	}
	if _, open := l.holds[auth]; open || l.decided[auth] != nil {
		return Admitted{}, fmt.Errorf("owner: minted authorization %q twice", auth)
	}
	if l.failed {
		return Admitted{}, ErrPublishing
	}
	if l.booksLocked().Free() < need {
		return Admitted{}, ErrNoRoom
	}
	h := &hold{auth: auth, estimate: a.Estimate, overrun: over, stream: a.Stream, boot: append([]byte(nil), a.Boot...)}
	l.held += h.estimate
	l.buffer += h.counted()
	l.holds[auth] = h
	now := l.o.cfg.Clock()
	if !l.withinCutoff(now) {
		l.held -= h.estimate
		l.buffer -= h.counted()
		delete(l.holds, auth)
		return Admitted{}, ErrPastCutoff
	}
	h.endOfLife = now.Add(l.o.cfg.HoldLife)
	return Admitted{Auth: auth, Lease: l.id, EndOfLife: h.endOfLife}, nil
}

// counted is the hold's part of the lease's buffer: a stream's from its first
// heartbeat, another's from its admission.
func (h *hold) counted() int64 {
	if h.stream && !h.heartbeat {
		return 0
	}
	return h.overrun
}

func (l *Lease) booksLocked() Books {
	return Books{Allocation: l.allocation, Held: l.held, Consumed: l.consumed, Pending: l.pending, Buffer: l.buffer,
		Shortfall: l.shortfall, NextSeq: l.nextSeq, Open: len(l.holds)}
}

// HeartbeatOf is a gateway's heartbeat: its sequence, from 1, and its
// snapshot's SHA-256 hash, the tokens delivered so far, the running charge
// priced from them, the deadline it echoes, which every heartbeat after the
// hold's first must, and the basis a reap of the hold needs, which the
// hold's first must bring.
type HeartbeatOf struct {
	GatewaySeq int64
	Hash       []byte
	Usage      int64
	Running    int64
	Echoed     time.Time
	Basis      []byte
}

// Heartbeat validates a stream's heartbeat as today (§4.5), publishes the
// accepted one under the lease, and answers with the deadline it grants once
// the record is acknowledged before the cutoff. A replay, the same sequence
// and hash, is answered as the heartbeat it repeats, without a second
// publish.
func (l *Lease) Heartbeat(ctx context.Context, auth string, hb HeartbeatOf) (time.Time, error) {
	l.mu.Lock()
	h := l.holds[auth]
	if h == nil {
		_, decided := l.decided[auth]
		l.mu.Unlock()
		if decided {
			return time.Time{}, ErrDecided
		}
		return time.Time{}, ErrUnknownHold
	}
	if !h.stream {
		// Only a stream heartbeats; another hold keeps its end of life.
		l.mu.Unlock()
		return time.Time{}, ErrRejected
	}
	if h.heartbeat {
		switch {
		case hb.GatewaySeq < h.gatewaySeq, hb.GatewaySeq == h.gatewaySeq && !bytes.Equal(hb.Hash, h.hash):
			l.mu.Unlock()
			return time.Time{}, ErrStale
		case hb.GatewaySeq == h.gatewaySeq:
			// A replay is answered as the heartbeat it repeats, by the
			// deadline that one echoed.
			s, granted, echoed := h.sent, h.deadline, h.echoed
			l.mu.Unlock()
			return l.heartbeatAnswer(ctx, s, granted, echoed)
		}
	}
	if hb.GatewaySeq < 1 || len(hb.Hash) != record.DigestSize || (!h.heartbeat && len(hb.Basis) == 0) ||
		(h.heartbeat && hb.Echoed.IsZero()) || hb.Usage < h.usage || hb.Running < h.running || hb.Running > h.estimate {
		l.mu.Unlock()
		return time.Time{}, ErrRejected
	}
	// A stream's buffer joins the lease's at its first heartbeat.
	buffer := l.buffer
	if !h.heartbeat {
		var ok bool
		if buffer, ok = add(l.buffer, h.overrun); !ok {
			l.mu.Unlock()
			return time.Time{}, ErrRejected
		}
	}
	now := l.o.cfg.Clock()
	if !l.withinCutoff(now) || l.failed {
		l.mu.Unlock()
		return time.Time{}, ErrRetry
	}
	deadline := now.Add(l.o.cfg.HeartbeatEvery)
	if deadline.After(h.endOfLife) {
		deadline = h.endOfLife
	}
	usage, err := json.Marshal(map[string]int64{"tokens": hb.Usage})
	if err != nil {
		l.mu.Unlock()
		return time.Time{}, err
	}
	r := record.Record{Kind: record.Heartbeat, Auth: auth, Estimate: h.estimate,
		Snapshot: &record.Snapshot{GatewaySeq: hb.GatewaySeq, Hash: hb.Hash, Usage: usage, Running: hb.Running,
			Deadline: deadline.UTC()}}
	if !h.heartbeat {
		// The hold's first heartbeat record carries the reap's basis.
		r.First, r.Basis = true, hb.Basis
	}
	s, err := l.handOver(r, 0)
	if errors.Is(err, ErrPastCutoff) {
		err = ErrRetry
	}
	if err != nil {
		l.mu.Unlock()
		return time.Time{}, err
	}
	l.buffer = buffer
	h.heartbeat, h.echoed, h.gatewaySeq, h.hash, h.usage, h.running, h.deadline, h.snapSeq, h.sent = true, hb.Echoed,
		hb.GatewaySeq, append([]byte(nil), hb.Hash...), hb.Usage, hb.Running, deadline, s.seq, s
	l.mu.Unlock()
	return l.heartbeatAnswer(ctx, s, deadline, hb.Echoed)
}

// heartbeatAnswer answers a heartbeat whose record is s: the deadline it
// granted, once s is acknowledged before the cutoff and by the deadline the
// heartbeat echoed; deadline_passed if after that; else retry (§4.5).
func (l *Lease) heartbeatAnswer(ctx context.Context, s *sent, granted, echoed time.Time) (time.Time, error) {
	acked, before, err := l.awaitAck(ctx, s)
	switch {
	case err != nil:
		return time.Time{}, err
	case !acked || !before:
		return time.Time{}, ErrRetry
	case !echoed.IsZero() && s.ackedAt.After(echoed):
		return time.Time{}, ErrDeadlinePassed
	}
	return granted, nil
}

// Outcome is a terminal's answer: the winner's kind and charge, and whether
// it is only recorded, its record acknowledged after the owner's cutoff, so
// the lease's order decides (§4.5).
type Outcome struct {
	Kind     record.Kind
	Charge   int64
	Recorded bool
}

// Settle decides a settle of charge for auth, its full record named by
// digest. One the record format cannot carry, a negative charge or a digest
// not SHA-256's, decides nothing.
func (l *Lease) Settle(ctx context.Context, auth string, charge int64, digest []byte) (Outcome, error) {
	return l.terminal(ctx, auth, record.Settle, charge, digest)
}

// Refund decides a refund for auth.
func (l *Lease) Refund(ctx context.Context, auth string) (Outcome, error) {
	return l.terminal(ctx, auth, record.Refund, 0, nil)
}

// terminal decides auth's terminal under the lease's lock (§4.2, §4.5): the
// first wins, and a later one is answered with the winner's outcome and not
// published. The decision moves the books once its record is handed over:
// the hold's estimate out of held, the charge into consumed, any overrun the
// lease has no room for into the shortfall total and the allocation, which
// the record carries, and what the hold frees into pending until the record
// is acknowledged.
func (l *Lease) terminal(ctx context.Context, auth string, kind record.Kind, charge int64, digest []byte) (Outcome, error) {
	l.mu.Lock()
	if d := l.decided[auth]; d != nil {
		s := d.sent
		out := Outcome{Kind: d.kind, Charge: d.charge}
		l.mu.Unlock()
		return l.terminalAnswer(ctx, s, out)
	}
	h := l.holds[auth]
	if h == nil {
		l.mu.Unlock()
		return Outcome{}, ErrUnknownHold
	}
	if !l.withinCutoff(l.o.cfg.Clock()) {
		l.mu.Unlock()
		return Outcome{}, ErrPastCutoff
	}
	held, charged, freed := l.held-h.estimate, int64(0), h.estimate
	if kind == record.Settle {
		charged, freed = charge, max(h.estimate-charge, 0)
	}
	// What is booked and held once decided. The allocation rises to it at
	// most, and the shortfall total, never above the allocation, with it; so
	// if it fits an int64, so do they.
	total, ok := add(l.consumed, charged, held)
	if !ok {
		l.mu.Unlock()
		return Outcome{}, fmt.Errorf("owner: a %s of %d the lease's books cannot hold", kind, charge)
	}
	consumed, allocation, shortfall := l.consumed+charged, l.allocation, l.shortfall
	if short := total - allocation; short > 0 {
		shortfall += short
		allocation += short
	}
	r := record.Record{Kind: kind, Auth: auth, Estimate: h.estimate, Charge: charge, Shortfall: shortfall, Digest: digest}
	if kind == record.Refund {
		r.Boot = h.boot
	}
	s, err := l.handOver(r, freed)
	if err != nil {
		// Not handed over: nothing is decided.
		l.mu.Unlock()
		return Outcome{}, err
	}
	l.held, l.consumed, l.allocation, l.shortfall = held, consumed, allocation, shortfall
	l.buffer -= h.counted()
	l.pending += freed
	delete(l.holds, auth)
	l.decided[auth] = &decision{kind: kind, charge: charge, sent: s}
	l.mu.Unlock()
	return l.terminalAnswer(ctx, s, Outcome{Kind: kind, Charge: charge})
}

// terminalAnswer answers a terminal decided as out, whose record is s: the
// outcome once s is acknowledged, recorded if after the cutoff (§4.5);
// past_cutoff if s is not acknowledged and the cutoff has passed, since the
// owner may never publish it, so the front door takes it to the drain log;
// else retry.
func (l *Lease) terminalAnswer(ctx context.Context, s *sent, out Outcome) (Outcome, error) {
	acked, before, err := l.awaitAck(ctx, s)
	switch {
	case err != nil:
		return Outcome{}, err
	case acked:
		out.Recorded = !before
		return out, nil
	}
	l.mu.Lock()
	past := !l.withinCutoff(l.o.cfg.Clock())
	l.mu.Unlock()
	if past {
		return Outcome{}, ErrPastCutoff
	}
	return Outcome{}, ErrRetry
}

// handOver gives a record the lease's next owner sequence number and hands
// it to the lease's key, with l.mu held: at once if every record before it
// is published since the last failure, so it follows them; else it waits for
// the flusher's republish, which sends it in its order. It reads the cutoff
// again once the record is encoded, which takes time: past it, nothing is
// handed over, numbered or decided.
func (l *Lease) handOver(r record.Record, freed int64) (*sent, error) {
	r.Version, r.Lease, r.Epoch, r.Seq = record.Version, l.id, l.o.cfg.Epoch, l.nextSeq
	data, err := record.Encode(r)
	if err != nil {
		return nil, err
	}
	if !l.withinCutoff(l.o.cfg.Clock()) {
		return nil, ErrPastCutoff
	}
	l.nextSeq++
	s := &sent{seq: r.Seq, data: data, freed: freed, waiter: notSent{}, done: make(chan struct{})}
	if l.live == len(l.inflight) {
		s.waiter = l.o.pub.Publish(l.id, data)
		l.live++
	}
	l.inflight = append(l.inflight, s)
	select {
	case l.kick <- struct{}{}:
	default:
	}
	return s, nil
}

// awaitAck waits up to AnswerWait for a record's acknowledgement, and
// reports whether it came, and whether before the cutoff. It does not wait
// for a record the flusher holds past the cutoff, nor once the owner let the
// lease go.
func (l *Lease) awaitAck(ctx context.Context, s *sent) (acked, beforeCutoff bool, err error) {
	l.mu.Lock()
	held := !s.acked && l.failed && !l.withinCutoff(l.o.cfg.Clock())
	l.mu.Unlock()
	if !held {
		timer := time.NewTimer(l.o.cfg.AnswerWait)
		defer timer.Stop()
		select {
		case <-s.done:
		case <-timer.C:
		case <-l.stop:
		case <-ctx.Done():
			return false, false, ctx.Err()
		}
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return s.acked, s.beforeCutoff, nil
}
