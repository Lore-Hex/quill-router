package owner

import (
	"context"
	"errors"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// ErrClosing: the lease admits nothing more (§4.2); its holds are still
// served, and the shard uses its other leases.
var ErrClosing = errors.New("owner: the lease admits nothing more")

// Spanner is what an owner writes to the store (§4.2): its leases'
// grants, renewals, their shortfall totals and its draining writes.
// *store.Store is one.
type Spanner interface {
	Grant(ctx context.Context, req store.GrantRequest) (store.GrantResult, error)
	Renew(ctx context.Context, owner store.Owner, refs []store.LeaseRef) ([]store.RenewResult, time.Time, error)
	ShortfallWrite(ctx context.Context, owner store.Owner, ref store.LeaseRef, total int64) (store.ShortfallResult, error)
	OwnerMarkDraining(ctx context.Context, owner store.Owner, ref store.LeaseRef) (bool, time.Time, error)
	ReadDrainSince(ctx context.Context, ref store.LeaseRef, cursor time.Time) ([]store.DrainRow, time.Time, error)
	ReadHoldDrainRows(ctx context.Context, ref store.LeaseRef, authorization string) ([]store.DrainRow, time.Time, error)
}

// who is the owner as the store's conditions name it.
func (o *Owner) who() store.Owner { return store.Owner{Node: o.cfg.Node, Epoch: o.cfg.Epoch} }

func (l *Lease) ref() store.LeaseRef { return store.LeaseRef{Workspace: l.workspace, LeaseID: l.id} }

// Run renews the owner's leases every RenewEvery until ctx ends or the
// owner stops, and, given the record topic, runs its reaper as often,
// apart, so neither waits for the other. A round or a pass that fails is
// tried again at the next.
func (o *Owner) Run(ctx context.Context) {
	var reaper sync.WaitGroup
	defer reaper.Wait()
	if o.cfg.Records != nil {
		reaper.Add(1)
		go func() {
			defer reaper.Done()
			o.every(ctx, func() { _ = o.Reap(ctx) })
		}()
	}
	o.every(ctx, func() { _ = o.Renew(ctx) })
}

// every runs f every RenewEvery until ctx ends or the owner stops.
func (o *Owner) every(ctx context.Context, f func()) {
	ticker := time.NewTicker(o.cfg.RenewEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-o.ctx.Done():
			return
		case <-ticker.C:
		}
		f()
	}
}

// Renew is one round of renewals (§4.2): one batch, a conditional statement
// for each lease the owner holds and still renews. A lease takes its new
// expiry from Spanner's answer, and only while the owner holds it; one that
// takes no renewal is draining, revoked or another process's, and the owner
// stops using it at once, as LeaseLifecycle's OwnerDrops does: it lets it
// go, and answers what names it as an owner past its cutoff does. With each
// round every lease publishes a checkpoint record. A lease the owner no
// longer renews, its publishes failing or its workspace one the switch does
// not enable (W1), is let go once past its cutoff; the switch on again
// before then, it is renewed again. The auditor finishes a lease let go so. Rounds run one at a time, and each
// ends with ctx or the owner, which waits for it to end when it stops.
func (o *Owner) Renew(ctx context.Context) error {
	if o.cfg.Spanner == nil {
		return errors.New("owner: no store to renew in")
	}
	o.mu.Lock()
	if o.stopped {
		o.mu.Unlock()
		return errors.New("owner: stopped")
	}
	o.rounds.Add(1)
	o.mu.Unlock()
	defer o.rounds.Done()
	o.round.Lock()
	defer o.round.Unlock()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer context.AfterFunc(o.ctx, cancel)()
	o.mu.Lock()
	leases := make([]*Lease, 0, len(o.leases))
	for _, l := range o.leases {
		leases = append(leases, l)
	}
	o.mu.Unlock()
	slices.SortFunc(leases, func(a, b *Lease) int { return strings.Compare(a.id, b.id) })
	now := o.cfg.Clock()
	var refs []store.LeaseRef
	var renewing []*Lease
	for _, l := range leases {
		if l.renewable(now) && o.cfg.Enabled(l.workspace) {
			refs, renewing = append(refs, l.ref()), append(renewing, l)
		}
	}
	if len(refs) > 0 {
		results, _, err := o.cfg.Spanner.Renew(ctx, o.who(), refs)
		if err != nil {
			return err
		}
		if len(results) != len(refs) {
			return errors.New("owner: a renewal answered for other leases than it renewed")
		}
		// Every refused lease is released before any is waited for, so a
		// slow finish of one keeps no other in use.
		var dropped []<-chan struct{}
		for i, r := range results {
			if r.Renewed {
				renewing[i].Renewed(r.Expiry)
			} else if stopped := o.release(renewing[i].id); stopped != nil {
				dropped = append(dropped, stopped)
			}
		}
		for _, stopped := range dropped {
			<-stopped
		}
	}
	now = o.cfg.Clock()
	for _, l := range leases {
		o.adoptDrain(ctx, l)
		l.closeIfDone(now, o.cfg.TopUps)
		if final := l.checkpoint(); final != nil {
			go o.finish(l, final)
		}
		if l.letIfAbandoned(now, !o.cfg.Enabled(l.workspace)) {
			o.Let(l.id)
		}
	}
	o.retryLost()
	return nil
}

// renewable: the owner renews a lease it holds, unless its publishes have
// failed for longer than the expiry window (§4.2). That lease then expires
// and drains, and a hold whose records were never stored ends at its close.
func (l *Lease) renewable(now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return !l.let && !l.failingTooLong(now)
}

func (l *Lease) failingTooLong(now time.Time) bool {
	return !l.failedAt.IsZero() && now.Sub(l.failedAt) > l.o.cfg.Window
}

// letIfAbandoned marks the lease let go if it is no longer renewed, its
// publishes failing too long or its workspace off, and is past its cutoff,
// in one step under its lock: a renewal's answer that comes after, which a
// lease let go ignores, cannot bring it back between the check and the
// letting go.
func (l *Lease) letIfAbandoned(now time.Time, off bool) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.let || !(off || l.failingTooLong(now)) || l.withinCutoff(now) {
		return false
	}
	l.let = true
	return true
}

// Close stops admitting under the lease (§4.2): it went idle, reached its
// maximum life, or its workspace was paused. It keeps serving its holds and
// renewing; its next checkpoint returns its free room, and once no hold is
// open a final checkpoint ends it.
func (l *Lease) Close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.closing = true
}

// checkpoint publishes the lease's checkpoint record (§4.2): its consumed,
// over the terminals with lower numbers; its open holds' count, sum and
// latest end of life; the key-status version; and, once it is closing, its
// free room, returned, which leaves its allocation as the record is handed
// over, and, with no hold open, final. It reports the final checkpoint's
// record once handed over, with the lease's finish counted among its
// workers. Past the cutoff it publishes nothing.
func (l *Lease) checkpoint() *sent {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.let || l.final != nil {
		return nil
	}
	c := record.CheckpointOf{Consumed: l.consumed, Open: int64(len(l.holds)), OpenSum: l.held,
		KeyStatus: l.o.cfg.KeyStatus}
	if len(l.ends) > 0 {
		// The open holds are kept as a heap by end of life (endHeap), so
		// the checkpoint holds the lease's lock no longer for many holds.
		c.LatestEnd = l.ends[0].endOfLife.UTC()
	}
	if l.closing {
		c.Return = max(l.booksLocked().Free(), 0)
		c.Final = len(l.holds) == 0
	}
	s, err := l.handOver(record.Record{Kind: record.Checkpoint, Checkpoint: &c}, 0)
	if err != nil {
		return nil
	}
	l.allocation -= c.Return
	if c.Final {
		l.final = s
		l.workers.Add(1)
		return s
	}
	return nil
}

// finish ends a lease whose final checkpoint is handed over (§4.2): once it
// is acknowledged, the owner marks the lease draining, conditional on its
// being open and the owner's, and lets it go. A write that changes no row
// means the lease is no longer open: the auditor marked it first, or an
// earlier try landed and its answer was lost. It is one of the lease's
// workers, so it only begins the letting go, which waits for it.
func (o *Owner) finish(l *Lease, final *sent) {
	defer l.workers.Done()
	select {
	case <-final.done:
	case <-l.stop:
		return
	}
	ctx, cancel := l.stopContext()
	defer cancel()
	backoff := firstBackoff
	for {
		if _, _, err := o.cfg.Spanner.OwnerMarkDraining(ctx, o.who(), l.ref()); err == nil {
			break
		}
		select {
		case <-time.After(backoff):
		case <-l.stop:
			return
		}
		backoff = min(2*backoff, lastBackoff)
	}
	o.release(l.id)
}

// writeShortfalls is the lease's one shortfall writer (§4.2). It stores the
// lease's shortfall total in Spanner as soon as a terminal raises it, one
// write in flight at a time, each carrying the total as it then stands. It
// retries a write until it lands, past the cutoff if need be and after the
// lease is let go, or until Spanner refuses it: the lease closed, or
// another process's. Once the lease is let go with nothing more to store it
// ends, and the owner's Stop ends it, leaving any total it did not store to
// the auditor's commit.
func (l *Lease) writeShortfalls() {
	ctx := l.o.ctx
	backoff := firstBackoff
	for {
		l.mu.Lock()
		total, stored, let := l.shortfall, l.stored, l.let
		l.mu.Unlock()
		if total <= stored {
			if let {
				return
			}
			select {
			case <-l.shortKick:
			case <-l.stop:
			case <-ctx.Done():
				return
			}
			continue
		}
		got, err := l.o.cfg.Spanner.ShortfallWrite(ctx, l.o.who(), l.ref(), total)
		switch {
		case ctx.Err() != nil:
			return
		case err == nil && got.Refused != "":
			return
		case err == nil:
			backoff = firstBackoff
			l.mu.Lock()
			l.stored = max(l.stored, total)
			l.mu.Unlock()
			continue
		}
		select {
		case <-time.After(backoff):
		case <-ctx.Done():
			return
		}
		backoff = min(2*backoff, lastBackoff)
	}
}

// stopContext is a context that ends when the lease's workers stop.
func (l *Lease) stopContext() (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		select {
		case <-l.stop:
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, cancel
}
