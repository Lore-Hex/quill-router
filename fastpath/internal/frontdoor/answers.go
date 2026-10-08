package frontdoor

import (
	"context"
	"errors"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// Status is an answer's kind.
type Status string

// What a front door answers a gateway, and an owner a front door.
const (
	// Admitted: an authorize held under a lease; the sealed envelope
	// follows.
	Admitted Status = "admitted"
	// Busy: no lease took the authorize. The gateway waits Retry-After
	// (503); it is never 402 (§4.4).
	Busy Status = "busy"
	// Accepted: a heartbeat taken, with the deadline it grants.
	Accepted Status = "accepted"
	// Retry: a heartbeat not taken, which stops the stream (§4.3); the
	// stream then settles, into the drain log if need be.
	Retry Status = "retry"
	// Stale, Rejected and DeadlinePassed are today's heartbeat answers
	// (§4.5); Decided, a heartbeat for a hold that has its terminal.
	Stale          Status = "stale"
	Rejected       Status = "rejected"
	DeadlinePassed Status = "deadline_passed"
	Decided        Status = "decided"
	// Won: a terminal's winner as its owner decided it, acknowledged before
	// the owner's cutoff: its kind and charge.
	Won Status = "won"
	// Recorded: the terminal is durable and the lease's order decides its
	// outcome, today's intent_durable (§4.5).
	Recorded Status = "recorded"
	// Settled: the lease is closed, so the drain log refused the terminal,
	// and the authorization's disposition answers (§4.5), as today's
	// already_settled does.
	Settled Status = "settled"
	// PastCutoff is an owner's answer to a terminal it may never publish:
	// past its cutoff, or for a lease it does not hold. The front door
	// takes the terminal to the drain log.
	PastCutoff Status = "past_cutoff"
	// Failed: the gateway retries, as today after an error.
	Failed Status = "failed"
	// Invalid: a request no retry makes good: an envelope whose seal does
	// not hold, or what the owner cannot take.
	Invalid Status = "invalid"
)

// OwnerAuthorize is an authorize as a front door forwards it to its shard's
// owner.
type OwnerAuthorize struct {
	Workspace string
	Shard     int64
	Estimate  int64
	Stream    bool
	Boot      []byte
}

// OwnerAdmitted is an owner's answer to an authorize: Admitted with the
// sealed envelope, Busy, Invalid or Failed.
type OwnerAdmitted struct {
	Status    Status
	Envelope  string
	EndOfLife time.Time
}

// OwnerHeartbeat is a heartbeat as a front door forwards it to the owner
// its envelope names.
type OwnerHeartbeat struct {
	Lease      string
	Auth       string
	GatewaySeq int64
	Hash       []byte
	Usage      int64
	Running    int64
	Echoed     time.Time
	Basis      []byte
}

// HeartbeatAnswer is a heartbeat's answer, the owner's and the front
// door's: Accepted with the deadline it grants, or why not.
type HeartbeatAnswer struct {
	Status   Status
	Deadline time.Time
}

// OwnerTerminal is a settle or a refund as a front door forwards it to the
// owner its envelope names: a settle's charge and its full record's digest.
type OwnerTerminal struct {
	Lease  string
	Auth   string
	Kind   record.Kind
	Charge int64
	Digest []byte
}

// OwnerTerminalAnswer is an owner's answer to a terminal: Won with the
// winner's kind and charge, Recorded, PastCutoff, Invalid or Failed.
type OwnerTerminalAnswer struct {
	Status Status
	Kind   record.Kind
	Charge int64
}

// Owners reaches owners by address: a node's own, or another node's over
// the network. An error is an owner not reached in time; an answer is the
// owner's own.
type Owners interface {
	Authorize(ctx context.Context, address string, req OwnerAuthorize) (OwnerAdmitted, error)
	Heartbeat(ctx context.Context, address string, req OwnerHeartbeat) (HeartbeatAnswer, error)
	Terminal(ctx context.Context, address string, req OwnerTerminal) (OwnerTerminalAnswer, error)
}

// ErrUnreachable is an owner a front door cannot reach.
var ErrUnreachable = errors.New("frontdoor: the owner cannot be reached")

// Direct reaches the owners in this process, by address.
type Direct map[string]*Local

func (d Direct) local(ctx context.Context, address string) (*Local, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	l := d[address]
	if l == nil {
		return nil, ErrUnreachable
	}
	return l, nil
}

// Authorize forwards to the owner at address.
func (d Direct) Authorize(ctx context.Context, address string, req OwnerAuthorize) (OwnerAdmitted, error) {
	l, err := d.local(ctx, address)
	if err != nil {
		return OwnerAdmitted{}, err
	}
	return l.Authorize(req), nil
}

// Heartbeat forwards to the owner at address.
func (d Direct) Heartbeat(ctx context.Context, address string, req OwnerHeartbeat) (HeartbeatAnswer, error) {
	l, err := d.local(ctx, address)
	if err != nil {
		return HeartbeatAnswer{}, err
	}
	return l.Heartbeat(ctx, req), nil
}

// Terminal forwards to the owner at address.
func (d Direct) Terminal(ctx context.Context, address string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	l, err := d.local(ctx, address)
	if err != nil {
		return OwnerTerminalAnswer{}, err
	}
	return l.Terminal(ctx, req), nil
}
