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
		f.reached(env)
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
			f.reached(env)
			f.unreachedHere(env.Owner)
			return got, true
		}
		if ctx.Err() != nil {
			return HeartbeatAnswer{}, false
		}
	}
	f.unreachedAnywhere(ctx, env)
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
		f.reached(env)
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
			f.reached(env)
			f.unreachedHere(env.Owner)
			return got, true
		}
		if ctx.Err() != nil {
			return OwnerTerminalAnswer{}, false
		}
	}
	f.unreachedAnywhere(ctx, env)
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

// reached: a lease's owner answered, here or through a peer, so its lease is
// not failing.
func (f *FrontDoor) reached(env Envelope) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.failing, store.LeaseRef{Workspace: env.Workspace, LeaseID: env.Lease})
}

// unreachedHere: a call to the owner failed here while a peer reached it.
// Two or more such owners within WithdrawWithin withdraw the front door,
// whose ring row then says so.
func (f *FrontDoor) unreachedHere(owner string) {
	if f.cfg.Node == nil {
		return
	}
	now := f.cfg.Clock()
	f.mu.Lock()
	f.unreached[owner] = now
	n := 0
	for o, at := range f.unreached {
		if now.Sub(at) <= f.cfg.WithdrawWithin {
			n++
		} else if !f.withdrawn {
			delete(f.unreached, o)
		}
	}
	withdraw := n >= 2 && !f.withdrawn
	if withdraw {
		f.withdrawn, f.want = true, store.Withdrawn
	}
	f.mu.Unlock()
	if withdraw {
		f.writeState()
	}
}

// unreachedAnywhere: no owner answered, here or at a peer. A lease whose
// owner no one has reached for RevokeAfter is revoked, at most one lease a
// RevokeEvery, and none by a withdrawn front door, whose view is its own.
// The revocation is written before the request is answered, past its end if
// need be, within the owner's wait.
func (f *FrontDoor) unreachedAnywhere(ctx context.Context, env Envelope) {
	if f.cfg.RevokeAfter == 0 {
		return
	}
	ref := store.LeaseRef{Workspace: env.Workspace, LeaseID: env.Lease}
	now := f.cfg.Clock()
	f.mu.Lock()
	first, ok := f.failing[ref]
	if !ok {
		f.failing[ref] = now
		f.mu.Unlock()
		return
	}
	_, done := f.revoked[ref]
	due := !done && !f.withdrawn && now.Sub(first) >= f.cfg.RevokeAfter &&
		(f.lastRevoke.IsZero() || now.Sub(f.lastRevoke) >= f.cfg.RevokeEvery)
	if due {
		f.revoked[ref], f.lastRevoke = now, now
	}
	f.mu.Unlock()
	if !due {
		return
	}
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), f.cfg.OwnerWait)
	defer cancel()
	if _, _, err := f.cfg.Store.Revoke(rctx, ref); err != nil {
		// Not revoked: a later failure may revoke it.
		f.mu.Lock()
		delete(f.revoked, ref)
		f.mu.Unlock()
	}
}

// Withdrawn reports whether the front door is withdrawn.
func (f *FrontDoor) Withdrawn() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.withdrawn
}

// writeState writes the state the front door wants in its ring row, one
// write at a time, each the state wanted when it runs: so however writes
// interleave, the last is the latest. The node keeps writing the state it
// was last given, so a write that fails is tried again with its heartbeats
// (ring.Node).
func (f *FrontDoor) writeState() {
	f.writing.Lock()
	defer f.writing.Unlock()
	f.mu.Lock()
	want := f.want
	f.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), f.cfg.OwnerWait)
	defer cancel()
	_ = f.cfg.Node.SetState(ctx, want)
}

// Run tries the owners a withdrawn front door could not reach every
// ProbeEvery, until ctx ends, and serves again once it reaches each that is
// still a live member; it forgets what it kept of leases an hour old.
func (f *FrontDoor) Run(ctx context.Context) error {
	every := f.cfg.ProbeEvery
	if every <= 0 {
		every = time.Minute
	}
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-t.C:
		}
		f.probe(ctx)
		f.forget()
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
	tried := map[string]time.Time{}
	for o, at := range f.unreached {
		if live[o] {
			tried[o] = at
		} else {
			delete(f.unreached, o)
		}
	}
	f.mu.Unlock()
	for o, at := range tried {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		err := f.cfg.Owners.Ping(pctx, o)
		cancel()
		if err != nil {
			continue
		}
		f.mu.Lock()
		if f.unreached[o].Equal(at) {
			delete(f.unreached, o)
		}
		f.mu.Unlock()
	}
	f.mu.Lock()
	serve := f.withdrawn && len(f.unreached) == 0
	if serve {
		f.withdrawn, f.want = false, store.Serving
	}
	f.mu.Unlock()
	if serve {
		f.writeState()
	}
}

// forget drops what the front door kept of leases an hour old: a lease
// lives at most its maximum life, far less.
func (f *FrontDoor) forget() {
	now := f.cfg.Clock()
	f.mu.Lock()
	defer f.mu.Unlock()
	for ref, at := range f.failing {
		if now.Sub(at) > time.Hour {
			delete(f.failing, ref)
		}
	}
	for ref, at := range f.revoked {
		if now.Sub(at) > time.Hour {
			delete(f.revoked, ref)
		}
	}
}
