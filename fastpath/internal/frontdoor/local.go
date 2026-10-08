package frontdoor

import (
	"context"
	"errors"

	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// Local answers the owner's part of what front doors forward to its node
// (§4.3, §4.4, §4.5). It admits an authorize under the owner's leases for
// the request's shard and seals the envelope, and hands a heartbeat or a
// terminal to the lease its envelope names. A lease the owner does not
// hold, its predecessor's at the address or one it let go, is answered as
// an owner past its cutoff answers: a terminal gets PastCutoff, which the
// front door takes to the drain log, and a heartbeat Retry. The owner
// never takes such a lease up.
type Local struct {
	owner   *owner.Owner
	address string
	region  string
	key     []byte
}

// NewLocal is the owner's part, at its node's address in its region, with
// the fleet's envelope key.
func NewLocal(o *owner.Owner, address, region string, key []byte) (*Local, error) {
	if o == nil || address == "" || region == "" || len(key) < MinKeySize {
		return nil, errors.New("frontdoor: an owner, its address and region, and a key of at least 32 bytes")
	}
	return &Local{owner: o, address: address, region: region, key: key}, nil
}

// Authorize holds the estimate under one of the shard's leases (§4.4) and
// seals the envelope the gateway echoes. A request no lease takes is Busy:
// the owner asks for a top-up, and the front door tries another shard's
// owner or answers that the request should wait.
func (l *Local) Authorize(req OwnerAuthorize) OwnerAdmitted {
	got, err := l.owner.Admit(owner.ShardKey{Workspace: req.Workspace, Region: l.region, Shard: req.Shard},
		owner.Admission{Estimate: req.Estimate, Stream: req.Stream, Boot: req.Boot, OpenHeartbeat: req.OpenHeartbeat})
	switch {
	case errors.Is(err, owner.ErrNoRoom):
		return OwnerAdmitted{Status: Busy}
	case err != nil:
		return OwnerAdmitted{Status: Invalid}
	}
	sealed, err := Seal(l.key, Envelope{Auth: got.Auth, Workspace: req.Workspace, Lease: got.Lease, Owner: l.address,
		Estimate: req.Estimate, Stream: req.Stream, EndOfLife: got.EndOfLife})
	if err != nil {
		// The hold stays, unanswered: it ends uncharged at its lease's
		// close, as a hold whose answer was lost does (§4.3).
		return OwnerAdmitted{Status: Failed}
	}
	return OwnerAdmitted{Status: Admitted, Envelope: sealed, EndOfLife: got.EndOfLife}
}

// Heartbeat hands a heartbeat to its lease. Every answer other than the
// owner's own, a lease it does not hold or a heartbeat it could not take, is
// Retry, which stops the stream.
func (l *Local) Heartbeat(ctx context.Context, req OwnerHeartbeat) HeartbeatAnswer {
	lease, ok := l.owner.Lease(req.Lease)
	if !ok {
		return HeartbeatAnswer{Status: Retry}
	}
	deadline, err := lease.Heartbeat(ctx, req.Auth, owner.HeartbeatOf{GatewaySeq: req.GatewaySeq, Hash: req.Hash,
		Usage: req.Usage, Running: req.Running, Echoed: req.Echoed, Basis: req.Basis})
	switch {
	case err == nil:
		return HeartbeatAnswer{Status: Accepted, Deadline: deadline}
	case errors.Is(err, owner.ErrStale):
		return HeartbeatAnswer{Status: Stale}
	case errors.Is(err, owner.ErrRejected), errors.Is(err, owner.ErrUnknownHold):
		return HeartbeatAnswer{Status: Rejected}
	case errors.Is(err, owner.ErrDeadlinePassed):
		return HeartbeatAnswer{Status: DeadlinePassed}
	case errors.Is(err, owner.ErrDecided):
		return HeartbeatAnswer{Status: Decided}
	}
	return HeartbeatAnswer{Status: Retry}
}

// Terminal hands a settle or a refund to its lease: the winner's outcome
// once its record is acknowledged before the cutoff, Recorded once it is
// acknowledged after, and PastCutoff when the owner may never publish it. A
// hold the lease never had, or a settle the record cannot carry, a negative
// charge or a digest not SHA-256's, is Invalid; anything else the gateway
// retries.
func (l *Local) Terminal(ctx context.Context, req OwnerTerminal) OwnerTerminalAnswer {
	lease, ok := l.owner.Lease(req.Lease)
	if !ok {
		return OwnerTerminalAnswer{Status: PastCutoff}
	}
	var out owner.Outcome
	var err error
	switch {
	case req.Kind == record.Settle && req.Charge >= 0 && len(req.Digest) == record.DigestSize:
		out, err = lease.Settle(ctx, req.Auth, req.Charge, req.Digest)
	case req.Kind == record.Refund:
		out, err = lease.Refund(ctx, req.Auth)
	default:
		return OwnerTerminalAnswer{Status: Invalid}
	}
	switch {
	case err == nil && out.Recorded:
		return OwnerTerminalAnswer{Status: Recorded}
	case err == nil:
		return OwnerTerminalAnswer{Status: Won, Kind: out.Kind, Charge: out.Charge}
	case errors.Is(err, owner.ErrPastCutoff):
		return OwnerTerminalAnswer{Status: PastCutoff}
	case errors.Is(err, owner.ErrUnknownHold):
		return OwnerTerminalAnswer{Status: Invalid}
	}
	return OwnerTerminalAnswer{Status: Failed}
}
