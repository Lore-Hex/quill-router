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
	f.failedHere(env.Owner)
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
	f.failedHere(env.Owner)
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
	got, err := f.cfg.Owners.Heartbeat(octx, owner, req)
	f.relayed(ctx, owner, err)
	return got, err
}

// RelayTerminal sends a terminal to its owner for a peer that cannot reach
// it. It appends nothing: the peer does, if no owner answers.
func (f *FrontDoor) RelayTerminal(ctx context.Context, owner string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	octx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
	defer cancel()
	got, err := f.cfg.Owners.Terminal(octx, owner, req)
	f.relayed(ctx, owner, err)
	return got, err
}

// relayed keeps what a relay's call to the owner showed: a reach, or a
// call that failed here, unless the relay's request ended first.
func (f *FrontDoor) relayed(ctx context.Context, owner string, err error) {
	switch {
	case err == nil:
		f.reached(owner)
	case ctx.Err() == nil:
		f.failedHere(owner)
	}
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

// reached: an owner answered, here, through a peer, or for a peer, so none
// of its leases is failing: each one's failures start over at its next.
func (f *FrontDoor) reached(owner string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reachedAt[owner] = f.see()
}

// failedHere: a call to the owner failed here, whatever a peer then
// answers. A probe forgets an owner only if no call to it has failed here
// since the probe began.
func (f *FrontDoor) failedHere(owner string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.here[owner] = f.see()
}

// see numbers what the front door sees now, under its lock: the clock is
// read there too, so a later number is never an earlier time.
func (f *FrontDoor) see() seen {
	f.seq++
	return seen{f.cfg.Clock(), f.seq}
}

// unreachedHere: a call to the owner failed here and a peer then reached
// it, which is evidence against this front door, complete when the peer
// answers and dated then. Evidence for two or more owners within
// WithdrawWithin of one another withdraws the front door, whose ring row
// Run then writes. The front door learns evidence in the order of its
// dates, so the window does not depend on the order in which the peers'
// answers come: a failure here is no evidence until one does.
func (f *FrontDoor) unreachedHere(owner string) {
	if f.cfg.Node == nil {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	now := f.see()
	f.unreached[owner] = now
	n := 0
	for o, at := range f.unreached {
		if now.at.Sub(at.at) <= f.cfg.WithdrawWithin {
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

// unreachedAnywhere: no owner answered, here or at a peer. The lease's
// failures are kept, from the first since its owner last answered to the
// last; once they span RevokeAfter, Run may revoke it (due), so no request
// waits for the write. A request for a hold past its end of life, which its
// envelope states, is no evidence: every hold of a lease revoked ends
// within HoldLife of the revocation, so no request can make a lease the
// front door has forgotten it revoked due again.
func (f *FrontDoor) unreachedAnywhere(env Envelope) {
	if f.cfg.RevokeAfter == 0 {
		return
	}
	ref := store.LeaseRef{Workspace: env.Workspace, LeaseID: env.Lease}
	f.mu.Lock()
	defer f.mu.Unlock()
	at := f.see()
	if !at.at.Before(env.EndOfLife) {
		return
	}
	fl, ok := f.failing[ref]
	if !ok || f.reachedAt[env.Owner].seq > fl.first.seq {
		fl = failure{owner: env.Owner, first: at}
	}
	fl.last = at
	f.failing[ref] = fl
	if fl.last.at.Sub(fl.first.at) >= f.cfg.RevokeAfter {
		f.wake()
	}
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
// then revokes the leases due, one at a time. The node keeps the state it
// was handed, and writes it again with its heartbeats if this write fails
// (ring.Node). A revocation that fails leaves the lease to revoke at the
// rate's next turn.
func (f *FrontDoor) write(ctx context.Context) {
	for ctx.Err() == nil {
		f.mu.Lock()
		now := f.cfg.Clock()
		state := f.want
		hand := f.cfg.Node != nil && state != f.handed
		var ref store.LeaseRef
		revoke := false
		if hand {
			f.handed = state
		} else if ref, revoke = f.due(now); revoke {
			f.revoked[ref] = now
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
			f.mu.Lock()
			// The next revocation waits a RevokeEvery from this one's
			// end, however long it took; and the lease's mark lasts from
			// then, since a renewal may land while the revocation is
			// under way.
			f.lastRevoke = f.cfg.Clock()
			if err != nil {
				delete(f.revoked, ref)
			} else {
				f.revoked[ref] = f.lastRevoke
			}
			f.mu.Unlock()
		default:
			return
		}
	}
}

// due is the lease to revoke now, under the front door's lock: of the
// leases not revoked whose failures span RevokeAfter with no answer from
// their owner since the first, the one failing longest. None is due while
// the front door is withdrawn, its view its own, nor within RevokeEvery of
// the last revocation's end: Run tries again at its next wake, a failure
// or a probe's tick.
func (f *FrontDoor) due(now time.Time) (store.LeaseRef, bool) {
	if f.cfg.RevokeAfter == 0 || f.withdrawn ||
		(!f.lastRevoke.IsZero() && now.Sub(f.lastRevoke) < f.cfg.RevokeEvery) {
		return store.LeaseRef{}, false
	}
	var ref store.LeaseRef
	var first uint64
	found := false
	for r, fl := range f.failing {
		if _, done := f.revoked[r]; done || f.reachedAt[fl.owner].seq > fl.first.seq ||
			fl.last.at.Sub(fl.first.at) < f.cfg.RevokeAfter {
			continue
		}
		if !found || fl.first.seq < first {
			ref, first, found = r, fl.first.seq, true
		}
	}
	return ref, found
}

// probe tries the owners a withdrawn front door could not reach. An owner
// no longer a live member is forgotten; one that answers is forgotten too,
// unless a call to it failed here after the probe began, which the probe
// reads once every ping is done, under one lock with its decision. The
// front door serves again once no owner is left: one a request finds
// unreachable during the probe keeps it withdrawn for the next.
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
	var owners []string
	for o := range f.unreached {
		if live[o] {
			owners = append(owners, o)
		} else {
			delete(f.unreached, o)
		}
	}
	began := f.seq
	f.mu.Unlock()
	var answered []string
	for _, o := range owners {
		pctx, cancel := context.WithTimeout(ctx, f.cfg.OwnerWait)
		err := f.cfg.Owners.Ping(pctx, o)
		cancel()
		if err == nil {
			f.reached(o)
			answered = append(answered, o)
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, o := range answered {
		if f.here[o].seq <= began {
			delete(f.unreached, o)
		}
	}
	if f.withdrawn && len(f.unreached) == 0 {
		f.withdrawn, f.want = false, store.Serving
	}
}

// forget drops what the front door kept of leases and owners an hour old: a
// lease lives at most its maximum life, far less. A revoked lease's mark
// stays HoldLife from the revocation's end, and then goes with the failures
// kept for it: by then each of its holds has ended, so no request is
// evidence against it again (unreachedAnywhere), and the marks number at
// most HoldLife over RevokeEvery.
func (f *FrontDoor) forget() {
	f.mu.Lock()
	defer f.mu.Unlock()
	now := f.cfg.Clock()
	for ref, fl := range f.failing {
		if now.Sub(fl.last.at) > time.Hour {
			delete(f.failing, ref)
		}
	}
	for ref, at := range f.revoked {
		if now.Sub(at) > f.cfg.HoldLife {
			delete(f.revoked, ref)
			delete(f.failing, ref)
		}
	}
	for _, m := range []map[string]seen{f.reachedAt, f.here} {
		for o, at := range m {
			if now.Sub(at.at) > time.Hour {
				delete(m, o)
			}
		}
	}
}
