package frontdoor

import (
	"context"
	"slices"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// heartbeatAt sends a heartbeat to its owner, and if this front door cannot
// reach it, through a peer within PeerWait. It reports whether an owner
// answered. A request that ended is no evidence that its owner is
// unreachable.
func (f *FrontDoor) heartbeatAt(ctx context.Context, env Envelope, req OwnerHeartbeat) (HeartbeatAnswer, bool) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	got, err := f.cfg.Owners.Heartbeat(octx, env.Owner, req)
	cancel()
	if err == nil {
		f.reached(env.Owner)
		return got, true
	}
	if ctx.Err() != nil {
		return HeartbeatAnswer{}, false
	}
	if peer, ok := f.peer(env); ok {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.PeerWait)
		got, err = f.cfg.Peers.Heartbeat(pctx, peer, env.Owner, req)
		cancel()
		if err == nil {
			f.reached(env.Owner)
			f.unreachedHere(env.Owner)
			return got, true
		}
		if ctx.Err() != nil {
			return HeartbeatAnswer{}, false
		}
	}
	f.unreachedAnywhere(env)
	return HeartbeatAnswer{}, false
}

// terminalAt sends a terminal to its owner, and if this front door cannot
// reach it, through a peer. It reports whether an owner answered. A request
// that ended is no evidence that its owner is unreachable.
func (f *FrontDoor) terminalAt(ctx context.Context, env Envelope, req OwnerTerminal) (OwnerTerminalAnswer, bool) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	got, err := f.cfg.Owners.Terminal(octx, env.Owner, req)
	cancel()
	if err == nil {
		f.reached(env.Owner)
		return got, true
	}
	if ctx.Err() != nil {
		return OwnerTerminalAnswer{}, false
	}
	if peer, ok := f.peer(env); ok {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		got, err = f.cfg.Peers.Terminal(pctx, peer, env.Owner, req)
		cancel()
		if err == nil {
			f.reached(env.Owner)
			f.unreachedHere(env.Owner)
			return got, true
		}
		if ctx.Err() != nil {
			return OwnerTerminalAnswer{}, false
		}
	}
	f.unreachedAnywhere(env)
	return OwnerTerminalAnswer{}, false
}

// peer is the front door a request whose owner this one cannot reach is
// sent through: of the live members, serving and taking leases, neither
// this node nor the owner, the one rendezvous hashing picks for the
// authorization, so a retry goes through the same peer.
func (f *FrontDoor) peer(env Envelope) (string, bool) {
	if f.cfg.Peers == nil {
		return "", false
	}
	view, _ := f.cfg.Members.View()
	var others ring.View
	for _, m := range view.Owners() {
		if m.Address != f.cfg.Self && m.Address != env.Owner {
			others.Members = append(others.Members, m)
		}
	}
	m, ok := others.Owner(env.Auth)
	return m.Address, ok
}

// RelayHeartbeat sends a heartbeat to its owner for a peer that cannot
// reach it.
func (f *FrontDoor) RelayHeartbeat(ctx context.Context, owner string, req OwnerHeartbeat) (HeartbeatAnswer, error) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	defer cancel()
	return f.cfg.Owners.Heartbeat(octx, owner, req)
}

// RelayTerminal sends a terminal to its owner for a peer that cannot reach
// it. It appends nothing: the peer does, if no owner answers.
func (f *FrontDoor) RelayTerminal(ctx context.Context, owner string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	defer cancel()
	return f.cfg.Owners.Terminal(octx, owner, req)
}

// DirectPeers reaches the front doors in this process, by address.
type DirectPeers map[string]*FrontDoor

// Heartbeat has the front door at peer send a heartbeat to its owner.
func (d DirectPeers) Heartbeat(ctx context.Context, peer, owner string, req OwnerHeartbeat) (HeartbeatAnswer, error) {
	req.Hash, req.Basis = slices.Clone(req.Hash), slices.Clone(req.Basis)
	return relay(ctx, d, peer, func(f *FrontDoor) (HeartbeatAnswer, error) { return f.RelayHeartbeat(ctx, owner, req) })
}

// Terminal has the front door at peer send a terminal to its owner.
func (d DirectPeers) Terminal(ctx context.Context, peer, owner string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	req.Digest = slices.Clone(req.Digest)
	return relay(ctx, d, peer, func(f *FrontDoor) (OwnerTerminalAnswer, error) { return f.RelayTerminal(ctx, owner, req) })
}

// relay runs f at the front door at peer, and returns its answer, or the
// context's end if that comes first. The front door's call to the owner
// ends with the context, so a relay whose context has ended starts nothing
// there.
func relay[A any](ctx context.Context, d DirectPeers, peer string, f func(*FrontDoor) (A, error)) (A, error) {
	var zero A
	door := d[peer]
	if door == nil {
		return zero, ErrUnreachable
	}
	type answer struct {
		a   A
		err error
	}
	done := make(chan answer, 1)
	go func() {
		a, err := f(door)
		done <- answer{a, err}
	}()
	select {
	case got := <-done:
		return got.a, got.err
	case <-ctx.Done():
		return zero, ctx.Err()
	}
}

// reached: an owner answered, here or through a peer, so none of its
// leases is failing: each one's failure starts over at its next.
func (f *FrontDoor) reached(owner string) {
	now := f.cfg.Clock()
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	f.reachedAt[owner] = seen{now, f.seq}
}

// unreachedHere: a call to the owner failed here while a peer reached it.
// Two or more such owners within WithdrawWithin withdraw the front door,
// whose ring row Run then writes.
func (f *FrontDoor) unreachedHere(owner string) {
	if f.cfg.Node == nil {
		return
	}
	now := f.cfg.Clock()
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	f.unreached[owner] = seen{now, f.seq}
	n := 0
	for o, at := range f.unreached {
		if now.Sub(at.at) <= f.cfg.WithdrawWithin {
			n++
		} else if !f.withdrawn {
			delete(f.unreached, o)
		}
	}
	if n >= 2 && !f.withdrawn {
		f.withdrawn, f.want = true, store.Withdrawn
		f.wake()
	}
}

// unreachedAnywhere: no owner answered, here or at a peer. A lease whose
// owner no one has reached for RevokeAfter is to be revoked, at most one
// lease a RevokeEvery: Run revokes it, so no request waits for the write,
// unless the front door has withdrawn by then, whose view is its own.
func (f *FrontDoor) unreachedAnywhere(env Envelope) {
	if f.cfg.RevokeAfter == 0 {
		return
	}
	ref := store.LeaseRef{Workspace: env.Workspace, LeaseID: env.Lease}
	now := f.cfg.Clock()
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	fl, ok := f.failing[ref]
	if !ok || f.reachedAt[env.Owner].seq > fl.since.seq {
		// The first failure since the owner last answered, for any of
		// its leases.
		f.failing[ref] = failure{owner: env.Owner, since: seen{now, f.seq}}
		return
	}
	if _, done := f.revoked[ref]; done || now.Sub(fl.since.at) < f.cfg.RevokeAfter ||
		(!f.lastRevoke.IsZero() && now.Sub(f.lastRevoke) < f.cfg.RevokeEvery) {
		return
	}
	f.revoked[ref], f.lastRevoke = now, now
	f.toRevoke = append(f.toRevoke, ref)
	f.wake()
}

// wake has Run look for work, if it is not about to.
func (f *FrontDoor) wake() {
	select {
	case f.kick <- struct{}{}:
	default:
	}
}

// Withdrawn reports whether the front door is withdrawn.
func (f *FrontDoor) Withdrawn() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.withdrawn
}

// Run does the front door's writes, until ctx ends: it hands the node the
// ring state wanted, and revokes the leases due. Every ProbeEvery it tries
// the owners a withdrawn front door could not reach, and serves again once
// it reaches each that is still a live member; it forgets what it kept of
// leases an hour old. Its goroutine alone writes, so the writes are in the
// order wanted, a revocation is checked against a withdrawal just before
// it is made, and none outlives Run.
func (f *FrontDoor) Run(ctx context.Context) error {
	every := f.cfg.ProbeEvery
	if every <= 0 {
		every = time.Minute
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		f.write(ctx)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-f.kick:
		case <-t.C:
			f.probe(ctx)
			f.forget()
		}
	}
}

// write hands the node the ring state wanted, if it has not had it, and
// then revokes the leases due, one at a time, each only if, just before,
// the front door is still serving and the lease's owner has not answered
// since its failure began. The node keeps the state it was handed, and
// writes it again with its heartbeats if this write fails (ring.Node). A
// revocation that fails is not kept: a later failure may revoke the lease.
func (f *FrontDoor) write(ctx context.Context) {
	for ctx.Err() == nil {
		f.mu.Lock()
		state := f.want
		hand := f.cfg.Node != nil && state != f.handed
		var ref store.LeaseRef
		revoke := false
		if !hand && len(f.toRevoke) > 0 {
			ref, f.toRevoke = f.toRevoke[0], f.toRevoke[1:]
			fl, failing := f.failing[ref]
			if revoke = !f.withdrawn && failing && f.reachedAt[fl.owner].seq < fl.since.seq; !revoke {
				delete(f.revoked, ref)
			}
		}
		if hand {
			f.handed = state
		}
		f.mu.Unlock()
		switch {
		case hand:
			wctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
			_ = f.cfg.Node.SetState(wctx, state)
			cancel()
		case revoke:
			rctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
			_, _, err := f.cfg.Store.Revoke(rctx, ref)
			cancel()
			if err != nil {
				f.mu.Lock()
				delete(f.revoked, ref)
				f.mu.Unlock()
			}
		default:
			f.mu.Lock()
			idle := len(f.toRevoke) == 0
			f.mu.Unlock()
			if idle {
				return
			}
		}
	}
}

// probe tries the owners a withdrawn front door could not reach. An owner
// no longer a live member is forgotten; one that answers is forgotten too,
// unless a call failed here again after the probe began. The front door
// serves again once no owner is left: one a request finds unreachable
// during the probe keeps it withdrawn for the next.
func (f *FrontDoor) probe(ctx context.Context) {
	f.mu.Lock()
	if !f.withdrawn || f.cfg.Node == nil {
		f.mu.Unlock()
		return
	}
	view, _ := f.cfg.Members.View()
	live := map[string]bool{}
	for _, m := range view.Members {
		if m.Live {
			live[m.Address] = true
		}
	}
	tried := map[string]uint64{}
	for o, at := range f.unreached {
		if live[o] {
			tried[o] = at.seq
		} else {
			delete(f.unreached, o)
		}
	}
	f.mu.Unlock()
	for o, seq := range tried {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		err := f.cfg.Owners.Ping(pctx, o)
		cancel()
		if err != nil {
			continue
		}
		f.mu.Lock()
		if f.unreached[o].seq == seq {
			delete(f.unreached, o)
		}
		f.mu.Unlock()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.withdrawn && len(f.unreached) == 0 {
		f.withdrawn, f.want = false, store.Serving
	}
}

// forget drops what the front door kept of leases and owners an hour old: a
// lease lives at most its maximum life, far less.
func (f *FrontDoor) forget() {
	now := f.cfg.Clock()
	f.mu.Lock()
	defer f.mu.Unlock()
	for ref, fl := range f.failing {
		if now.Sub(fl.since.at) > time.Hour {
			delete(f.failing, ref)
		}
	}
	for ref, at := range f.revoked {
		if now.Sub(at) > time.Hour {
			delete(f.revoked, ref)
		}
	}
	for o, at := range f.reachedAt {
		if now.Sub(at.at) > time.Hour {
			delete(f.reachedAt, o)
		}
	}
}
