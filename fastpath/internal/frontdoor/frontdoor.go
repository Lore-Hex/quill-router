// Package frontdoor is an admission node's front door (design §4.3, §4.4,
// §4.5): it sends a gateway's authorize to its workspace shard's owner, and
// the request's heartbeats, settle and refund to the owner and lease its
// envelope names. A terminal the owner does not take, because it cannot be
// reached or answers past its cutoff, goes to the lease's drain log and is
// answered recorded; a heartbeat it does not take gets retry, which stops
// the stream. Local is the owner's part of each request, on its node.
package frontdoor

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Store is what a front door writes and reads in Spanner: the drain log's
// appends, and an authorization's disposition when an append is refused.
// *store.Store is one.
type Store interface {
	Append(ctx context.Context, t store.DrainTerminal) (store.AppendResult, error)
	Disposition(ctx context.Context, authorization string) (store.Disposition, error)
}

// Waiter is a publish: its acknowledgement's message ID, or its failure.
type Waiter interface {
	Wait(ctx context.Context) (string, error)
}

// RecordLog publishes the record topic (settlelog.Records): a settle's full
// record goes there before the settle is recorded (§4.9).
type RecordLog interface {
	Publish(authorization, kind string, data []byte) Waiter
}

// FromRecords is the record topic's publisher as a RecordLog.
func FromRecords(r *settlelog.Records) RecordLog { return recordLog{r} }

type recordLog struct{ r *settlelog.Records }

func (r recordLog) Publish(authorization, kind string, data []byte) Waiter {
	return r.r.Publish(authorization, kind, data)
}

// Members is a front door's view of the members; *ring.Watcher is one.
type Members interface {
	View() (ring.View, time.Time)
}

// Config is a front door's.
type Config struct {
	Owners  Owners
	Store   Store
	Records RecordLog
	Members Members
	// Key is the fleet's envelope key, which owners seal with.
	Key []byte
	// Shards is a workspace's shard count K (§4.3), at least 1.
	Shards func(workspace string) int64
	// OwnerWait bounds a call to an owner: one not answered by then is an
	// owner not reached.
	OwnerWait time.Duration
	// PublishWait bounds a full record's publish (§4.9).
	PublishWait time.Duration
}

// FrontDoor takes a node's gateway requests.
type FrontDoor struct {
	cfg Config
}

// New is a front door with its configuration.
func New(cfg Config) (*FrontDoor, error) {
	if cfg.Owners == nil || cfg.Store == nil || cfg.Records == nil || cfg.Members == nil ||
		len(cfg.Key) < MinKeySize || cfg.Shards == nil || cfg.OwnerWait <= 0 || cfg.PublishWait <= 0 {
		return nil, errors.New("frontdoor: owners, a store, the record topic, the members, a key of at least " +
			"32 bytes, the workspaces' shard counts and positive waits")
	}
	return &FrontDoor{cfg: cfg}, nil
}

// AuthorizeOf is a gateway's authorize.
type AuthorizeOf struct {
	Workspace string
	// Request is the request's own hash, which picks its shard (§4.3).
	Request  string
	Estimate int64
	Stream   bool
	Boot     []byte
}

// Authorized is an authorize's answer: Admitted with the sealed envelope and
// the hold's end of life, Busy, Invalid or Failed.
type Authorized struct {
	Status    Status
	Envelope  string
	EndOfLife time.Time
}

// Authorize sends an authorize to its shard's owner, the shard its own hash
// picks (§4.3). If that owner has no lease to take it, or cannot be reached,
// a sharded workspace tries one other shard's owner, and then is Busy
// (§4.4). The spike has no synchronous path, so an unsharded workspace is
// Busy at once.
func (f *FrontDoor) Authorize(ctx context.Context, a AuthorizeOf) Authorized {
	k := f.cfg.Shards(a.Workspace)
	if a.Workspace == "" || k < 1 || a.Estimate < 0 || len(a.Boot) == 0 {
		return Authorized{Status: Invalid}
	}
	shard := pick(a.Request, k)
	shards := []int64{shard}
	if k > 1 {
		shards = append(shards, (shard+1)%k)
	}
	view, _ := f.cfg.Members.View()
	for _, s := range shards {
		m, ok := view.Owner(ring.ShardKey(a.Workspace, s))
		if !ok {
			continue
		}
		octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		got, err := f.cfg.Owners.Authorize(octx, m.Address, OwnerAuthorize{Workspace: a.Workspace, Shard: s,
			Estimate: a.Estimate, Stream: a.Stream, Boot: a.Boot})
		cancel()
		switch {
		case err != nil, got.Status == Busy:
		case got.Status == Admitted:
			return Authorized{Status: Admitted, Envelope: got.Envelope, EndOfLife: got.EndOfLife}
		default:
			return Authorized{Status: got.Status}
		}
	}
	return Authorized{Status: Busy}
}

// pick is the shard a request's own hash picks, of k.
func pick(request string, k int64) int64 {
	h := sha256.Sum256([]byte(request))
	return int64(binary.BigEndian.Uint64(h[:8]) % uint64(k))
}

// HeartbeatOf is a gateway's heartbeat, with the envelope it echoes.
type HeartbeatOf struct {
	Envelope   string
	GatewaySeq int64
	Hash       []byte
	Usage      int64
	Running    int64
	Echoed     time.Time
	Basis      []byte
}

// Heartbeat sends a heartbeat to the owner its envelope names. One the
// owner does not answer gets Retry, which stops the stream (§4.3).
func (f *FrontDoor) Heartbeat(ctx context.Context, hb HeartbeatOf) HeartbeatAnswer {
	env, err := Open(f.cfg.Key, hb.Envelope)
	if err != nil {
		return HeartbeatAnswer{Status: Invalid}
	}
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	defer cancel()
	got, err := f.cfg.Owners.Heartbeat(octx, env.Owner, OwnerHeartbeat{Lease: env.Lease, Auth: env.Auth,
		GatewaySeq: hb.GatewaySeq, Hash: hb.Hash, Usage: hb.Usage, Running: hb.Running, Echoed: hb.Echoed,
		Basis: hb.Basis})
	if err != nil {
		return HeartbeatAnswer{Status: Retry}
	}
	return got
}

// SettleOf is a gateway's settle: the envelope it echoes, its charge, its
// full record, and its money fields, which a drain-log row carries (§4.13).
type SettleOf struct {
	Envelope string
	Charge   int64
	Full     []byte
	Money    []byte
}

// RefundOf is a gateway's refund, with the envelope it echoes and its money
// fields.
type RefundOf struct {
	Envelope string
	Money    []byte
}

// TerminalAnswer is a terminal's answer: Won with the winner's kind and
// charge, Recorded, Settled with the authorization's disposition, Failed or
// Invalid.
type TerminalAnswer struct {
	Status Status
	Kind   record.Kind
	Charge int64
	// Outcome and Cost are a disposition's (store.Disposition); CostKnown
	// is false when it has no cost.
	Outcome   string
	Cost      int64
	CostKnown bool
}

// Settle records a settle (§4.5, §4.9): its full record is published to the
// record topic first, and only once that is acknowledged is the settle sent
// to its owner, or to the drain log, with the full record's digest.
func (f *FrontDoor) Settle(ctx context.Context, s SettleOf) TerminalAnswer {
	env, err := Open(f.cfg.Key, s.Envelope)
	if err != nil || s.Charge < 0 || len(s.Full) == 0 || len(s.Money) == 0 {
		return TerminalAnswer{Status: Invalid}
	}
	digest := sha256.Sum256(s.Full)
	pctx, cancel := context.WithTimeout(ctx, f.cfg.PublishWait)
	_, err = f.cfg.Records.Publish(env.Auth, settlelog.FullRecord, s.Full).Wait(pctx)
	cancel()
	if err != nil {
		return TerminalAnswer{Status: Failed}
	}
	// A settle's drain-log row is named by its full record, so a retry of
	// it, through this front door or another, finds its row (§4.5).
	return f.terminal(ctx, env, OwnerTerminal{Lease: env.Lease, Auth: env.Auth, Kind: record.Settle,
		Charge: s.Charge, Digest: digest[:]}, "settle-"+hex.EncodeToString(digest[:16]), s.Money)
}

// Refund records a refund (§4.5).
func (f *FrontDoor) Refund(ctx context.Context, r RefundOf) TerminalAnswer {
	env, err := Open(f.cfg.Key, r.Envelope)
	if err != nil || len(r.Money) == 0 {
		return TerminalAnswer{Status: Invalid}
	}
	return f.terminal(ctx, env, OwnerTerminal{Lease: env.Lease, Auth: env.Auth, Kind: record.Refund}, "refund",
		r.Money)
}

// terminal sends a terminal to the owner its envelope names. If the owner
// cannot be reached, or answers that it may never publish it, the terminal
// goes to the lease's drain log at once, under the record ID given.
func (f *FrontDoor) terminal(ctx context.Context, env Envelope, req OwnerTerminal, recordID string,
	money []byte) TerminalAnswer {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	got, err := f.cfg.Owners.Terminal(octx, env.Owner, req)
	cancel()
	cause := "unreachable"
	switch {
	case err != nil:
	case got.Status == PastCutoff:
		cause = "past_cutoff"
	case got.Status == Won:
		return TerminalAnswer{Status: Won, Kind: got.Kind, Charge: got.Charge}
	default:
		return TerminalAnswer{Status: got.Status}
	}
	if ctx.Err() != nil {
		// The gateway stopped waiting: it retries.
		return TerminalAnswer{Status: Failed}
	}
	return f.appendTerminal(ctx, env, req, recordID, money, cause)
}

// appendTerminal appends a terminal to its lease's drain log, with the
// hold's estimate its envelope carries, so a charge above it raises the
// lease's allocation in the same transaction (§4.2). The terminal is then
// Recorded: the lease's order decides it. A closed lease refuses the
// append, and the authorization's disposition answers.
func (f *FrontDoor) appendTerminal(ctx context.Context, env Envelope, req OwnerTerminal, recordID string,
	money []byte, cause string) TerminalAnswer {
	got, err := f.cfg.Store.Append(ctx, store.DrainTerminal{Ref: store.LeaseRef{Workspace: env.Workspace,
		LeaseID: env.Lease}, AuthorizationID: env.Auth, RecordID: recordID, Kind: string(req.Kind),
		Charge: req.Charge, Estimate: env.Estimate, Digest: req.Digest, Money: money, Cause: cause})
	switch {
	case err != nil:
		return TerminalAnswer{Status: Failed}
	case got.Refused != "":
		d, err := f.cfg.Store.Disposition(ctx, env.Auth)
		if err != nil {
			return TerminalAnswer{Status: Failed}
		}
		return TerminalAnswer{Status: Settled, Outcome: d.Outcome, Cost: d.Cost.Int64, CostKnown: d.Cost.Valid}
	}
	return TerminalAnswer{Status: Recorded}
}
