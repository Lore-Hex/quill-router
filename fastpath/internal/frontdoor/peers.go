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
// answered.
func (f *FrontDoor) heartbeatAt(ctx context.Context, env Envelope, req OwnerHeartbeat) (HeartbeatAnswer, bool) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	got, err := f.cfg.Owners.Heartbeat(octx, env.Owner, req)
	cancel()
	if err == nil {
		f.reached(env)
		return got, true
	}
	if peer, ok := f.peer(env); ok && ctx.Err() == nil {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.PeerWait)
		got, err = f.cfg.Peers.Heartbeat(pctx, peer, env.Owner, req)
		cancel()
		if err == nil {
			f.unreachedHere(env.Owner)
			return got, true
		}
	}
	f.unreachedAnywhere(env)
	return HeartbeatAnswer{}, false
}

// terminalAt sends a terminal to its owner, and if this front door cannot
// reach it, through a peer. It reports whether an owner answered.
func (f *FrontDoor) terminalAt(ctx context.Context, env Envelope, req OwnerTerminal) (OwnerTerminalAnswer, bool) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	got, err := f.cfg.Owners.Terminal(octx, env.Owner, req)
	cancel()
	if err == nil {
		f.reached(env)
		return got, true
	}
	if peer, ok := f.peer(env); ok && ctx.Err() == nil {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		got, err = f.cfg.Peers.Terminal(pctx, peer, env.Owner, req)
		cancel()
		if err == nil {
			f.unreachedHere(env.Owner)
			return got, true
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

// reached: a lease's owner answered this front door, so its lease is not
// failing.
func (f *FrontDoor) reached(env Envelope) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.failing, store.LeaseRef{Workspace: env.Workspace, LeaseID: env.Lease})
}

// unreachedHere: a call to the owner failed here while a peer reached it.
// Two or more such owners within WithdrawWithin withdraw the front door.
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
		f.withdrawn = true
	}
	f.mu.Unlock()
	if withdraw {
		f.setState(store.Withdrawn)
	}
}

// unreachedAnywhere: no owner answered, here or at a peer. A lease whose
// owner no one has reached for RevokeAfter is revoked, at most one lease a
// RevokeEvery, and none by a withdrawn front door, whose view is its own.
func (f *FrontDoor) unreachedAnywhere(env Envelope) {
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
	f.work.Add(1)
	go func() {
		defer f.work.Done()
		ctx, cancel := context.WithTimeout(context.Background(), f.cfg.OwnerWait)
		defer cancel()
		if _, _, err := f.cfg.Store.Revoke(ctx, ref); err != nil {
			// Not revoked: a later failure may revoke it.
			f.mu.Lock()
			delete(f.revoked, ref)
			f.mu.Unlock()
		}
	}()
}

// Withdrawn reports whether the front door is withdrawn.
func (f *FrontDoor) Withdrawn() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.withdrawn
}

// setState writes the node's state, off the request's path. The node keeps
// writing the state it was last given, so a write that fails is tried
// again with its heartbeats (ring.Node).
func (f *FrontDoor) setState(state string) {
	f.work.Add(1)
	go func() {
		defer f.work.Done()
		ctx, cancel := context.WithTimeout(context.Background(), f.cfg.OwnerWait)
		defer cancel()
		_ = f.cfg.Node.SetState(ctx, state)
	}()
}

// Run tries the owners a withdrawn front door could not reach every
// ProbeEvery, until ctx ends, and serves again once it reaches each that is
// still a live member; it forgets what it kept of leases an hour old. It
// returns once its revocations and state writes have ended.
func (f *FrontDoor) Run(ctx context.Context) error {
	defer f.work.Wait()
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

// probe tries each owner a withdrawn front door could not reach.
func (f *FrontDoor) probe(ctx context.Context) {
	f.mu.Lock()
	if !f.withdrawn || f.cfg.Node == nil {
		f.mu.Unlock()
		return
	}
	owners := make([]string, 0, len(f.unreached))
	for o := range f.unreached {
		owners = append(owners, o)
	}
	f.mu.Unlock()
	view, _ := f.cfg.Members.View()
	live := map[string]bool{}
	for _, m := range view.Members {
		if m.Live {
			live[m.Address] = true
		}
	}
	for _, o := range owners {
		if !live[o] {
			continue
		}
		pctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		err := f.cfg.Owners.Ping(pctx, o)
		cancel()
		if err != nil {
			return
		}
	}
	f.mu.Lock()
	serve := f.withdrawn
	f.withdrawn = false
	clear(f.unreached)
	f.mu.Unlock()
	if serve {
		f.setState(store.Serving)
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
