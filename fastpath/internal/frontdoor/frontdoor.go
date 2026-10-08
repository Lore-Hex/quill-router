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
	"slices"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Store is what a front door writes and reads in Spanner: the drain log's
// appends, an authorization's disposition when an append is refused, and
// the revocation of a lease whose owner no one reaches. *store.Store is
// one.
type Store interface {
	Append(ctx context.Context, t store.DrainTerminal) (store.AppendResult, error)
	Disposition(ctx context.Context, authorization string) (store.Disposition, error)
	Revoke(ctx context.Context, ref store.LeaseRef) (bool, time.Time, error)
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

	// Self is this node's address, Peers the other nodes' front doors: a
	// terminal or a heartbeat whose owner this one cannot reach is sent
	// through one of them, a heartbeat's within PeerWait, since its own
	// budget is five seconds (§4.3). Without Peers it is sent through none.
	Self     string
	Peers    Peers
	PeerWait time.Duration
	// Node is this node's row in the ring. Once calls to two or more owners
	// fail here within WithdrawWithin while a peer reaches them, the front
	// door withdraws and takes no new request; every ProbeEvery it tries
	// those owners again, and serves once it reaches each that is still a
	// member (§4.3, spike plan §4). Without Node it never withdraws.
	Node           States
	WithdrawWithin time.Duration
	ProbeEvery     time.Duration
	// A lease whose owner neither this front door nor its peer has reached
	// for RevokeAfter is revoked, at most one lease a RevokeEvery; a
	// withdrawn front door revokes none (§4.3). Zero RevokeAfter revokes
	// none.
	RevokeAfter time.Duration
	RevokeEvery time.Duration
	Clock       func() time.Time
}

// States sets a node's state in the ring; *ring.Node is one.
type States interface {
	SetState(ctx context.Context, state string) error
}

// FrontDoor takes a node's gateway requests.
type FrontDoor struct {
	cfg Config

	mu sync.Mutex
	// withdrawn is set while the front door is withdrawn; want is the
	// state its ring row should say, and handed the last Run handed the
	// node. seq numbers the reaches and failures the front door sees, so
	// their order is known whatever the clock says. unreached are the
	// owners whose calls last failed here while a peer reached them; here,
	// when a call to each owner last failed here, whatever the peer did;
	// and reachedAt, when each owner last answered, here, through a peer or
	// for one; failing, each lease's failures since its owner last
	// answered; revoked, the leases revoked, with when, and lastRevoke when
	// the last revocation ended.
	withdrawn    bool
	want, handed string
	seq          uint64
	unreached    map[string]seen
	here         map[string]seen
	reachedAt    map[string]seen
	failing      map[store.LeaseRef]failure
	revoked      map[store.LeaseRef]time.Time
	lastRevoke   time.Time
	// kick wakes Run for a state to hand the node or a lease to revoke.
	kick chan struct{}
}

// seen is when the front door saw a reach or a failure, by its clock and
// in its order.
type seen struct {
	at  time.Time
	seq uint64
}

// failure is a lease's first and last failure since its owner last
// answered.
type failure struct {
	owner       string
	first, last seen
}

// New is a front door with its configuration.
func New(cfg Config) (*FrontDoor, error) {
	if cfg.Owners == nil || cfg.Store == nil || cfg.Records == nil || cfg.Members == nil ||
		len(cfg.Key) < MinKeySize || cfg.Shards == nil || cfg.OwnerWait <= 0 || cfg.PublishWait <= 0 {
		return nil, errors.New("frontdoor: owners, a store, the record topic, the members, a key of at least " +
			"32 bytes, the workspaces' shard counts and positive waits")
	}
	if (cfg.Peers != nil && (cfg.Self == "" || cfg.PeerWait <= 0)) ||
		(cfg.Node != nil && (cfg.WithdrawWithin <= 0 || cfg.ProbeEvery <= 0)) || cfg.RevokeAfter < 0 ||
		(cfg.RevokeAfter > 0 && cfg.RevokeEvery <= 0) {
		return nil, errors.New("frontdoor: peers need this node's address and a wait, withdrawing a window and " +
			"a probe interval, and revoking an interval")
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	return &FrontDoor{cfg: cfg, unreached: map[string]seen{}, here: map[string]seen{}, reachedAt: map[string]seen{},
		failing: map[store.LeaseRef]failure{}, revoked: map[store.LeaseRef]time.Time{}, kick: make(chan struct{}, 1)}, nil
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
// Busy at once. A withdrawn front door takes no new request: Busy.
func (f *FrontDoor) Authorize(ctx context.Context, a AuthorizeOf) Authorized {
	k := f.cfg.Shards(a.Workspace)
	if a.Workspace == "" || k < 1 || a.Estimate < 0 || len(a.Boot) == 0 {
		return Authorized{Status: Invalid}
	}
	if f.Withdrawn() {
		return Authorized{Status: Busy}
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
		if err == nil {
			f.reached(m.Address)
		}
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

// Heartbeat sends a heartbeat to the owner its envelope names, through a
// peer if this front door cannot reach it. One that no owner answers gets
// Retry, which stops the stream (§4.3).
func (f *FrontDoor) Heartbeat(ctx context.Context, hb HeartbeatOf) HeartbeatAnswer {
	env, err := Open(f.cfg.Key, hb.Envelope)
	if err != nil {
		return HeartbeatAnswer{Status: Invalid}
	}
	got, ok := f.heartbeatAt(ctx, env, OwnerHeartbeat{Lease: env.Lease, Auth: env.Auth,
		GatewaySeq: hb.GatewaySeq, Hash: hb.Hash, Usage: hb.Usage, Running: hb.Running, Echoed: hb.Echoed,
		Basis: hb.Basis})
	if !ok {
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
	// The publish may outlive the request: it publishes its own copy.
	full := slices.Clone(s.Full)
	digest := sha256.Sum256(full)
	pctx, cancel := context.WithTimeout(ctx, f.cfg.PublishWait)
	_, err = f.cfg.Records.Publish(env.Auth, settlelog.FullRecord, full).Wait(pctx)
	cancel()
	if err != nil {
		return TerminalAnswer{Status: Failed}
	}
	req := OwnerTerminal{Lease: env.Lease, Auth: env.Auth, Kind: record.Settle, Charge: s.Charge, Digest: digest[:]}
	return f.terminal(ctx, env, req, rowID(req, s.Money), s.Money)
}

// rowID names a terminal's drain-log row by all it states, its kind, charge,
// full record's digest and money fields: a retry of it, through this front
// door or another, finds its row, and another terminal for the hold makes
// its own (§4.5).
func rowID(req OwnerTerminal, money []byte) string {
	h := sha256.New()
	for _, part := range [][]byte{[]byte(req.Kind), binary.BigEndian.AppendUint64(nil, uint64(req.Charge)), req.Digest,
		money} {
		h.Write(binary.BigEndian.AppendUint64(nil, uint64(len(part))))
		h.Write(part)
	}
	return string(req.Kind) + "-" + hex.EncodeToString(h.Sum(nil)[:16])
}

// Refund records a refund (§4.5).
func (f *FrontDoor) Refund(ctx context.Context, r RefundOf) TerminalAnswer {
	env, err := Open(f.cfg.Key, r.Envelope)
	if err != nil || len(r.Money) == 0 {
		return TerminalAnswer{Status: Invalid}
	}
	req := OwnerTerminal{Lease: env.Lease, Auth: env.Auth, Kind: record.Refund}
	return f.terminal(ctx, env, req, rowID(req, r.Money), r.Money)
}

// terminal sends a terminal to the owner its envelope names, through a peer
// if this front door cannot reach it. If no owner answers, or the owner
// answers that it may never publish it, the terminal goes to the lease's
// drain log at once, under the row ID given.
func (f *FrontDoor) terminal(ctx context.Context, env Envelope, req OwnerTerminal, recordID string,
	money []byte) TerminalAnswer {
	got, ok := f.terminalAt(ctx, env, req)
	cause := "unreachable"
	switch {
	case !ok:
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
