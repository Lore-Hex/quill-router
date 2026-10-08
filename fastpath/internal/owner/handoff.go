package owner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/record"
)

// Handoff is a forced exit (§4.2): the owner must stop sooner than its
// leases' holds end. From the call on it takes no new lease, so a grant
// that lands meanwhile is left to expire. Under each lease it holds, it
// stops admitting, then publishes the lease's open holds in hand-off
// records, chunked under the settle log's record size, then a manifest:
// the number of chunks, the digest of their holds and the sequence numbers
// they used. From the manifest on, the lease issues no record. Once the
// manifest is acknowledged the owner marks the lease draining, by the
// conditional write a final checkpoint's finish makes, while ctx lasts and
// the owner runs, and lets it go; with no manifest acknowledged by then it
// lets it go as it is, and the auditor marks it draining once it expires
// (§4.8). Leases are handed off at once; Handoff returns once each is let
// go, and reports ctx's end and each lease whose holds it could not hand
// over for another reason. A second call waits for the first and reports as
// it does; Stop ends a hand-off under way.
func (o *Owner) Handoff(ctx context.Context) error {
	if o.cfg.Spanner == nil {
		return errors.New("owner: no store to mark leases draining in")
	}
	o.mu.Lock()
	if done := o.handoff; done != nil {
		o.mu.Unlock()
		select {
		case <-done:
		case <-ctx.Done():
			return ctx.Err()
		}
		o.mu.Lock()
		defer o.mu.Unlock()
		return o.handoffErr
	}
	if o.stopped {
		o.mu.Unlock()
		return errors.New("owner: stopped")
	}
	done := make(chan struct{})
	o.handoff = done
	leases := make([]*Lease, 0, len(o.leases))
	for _, l := range o.leases {
		leases = append(leases, l)
	}
	o.writers.Add(1)
	o.mu.Unlock()
	defer o.writers.Done()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer context.AfterFunc(o.ctx, cancel)()
	slices.SortFunc(leases, func(a, b *Lease) int { return strings.Compare(a.id, b.id) })
	var all sync.WaitGroup
	var mu sync.Mutex
	errs := []error{nil}
	for _, l := range leases {
		all.Add(1)
		go func() {
			defer all.Done()
			handed, err := l.handedOver(ctx)
			if err != nil {
				mu.Lock()
				errs = append(errs, fmt.Errorf("owner: lease %s's hand-off: %w", l.id, err))
				mu.Unlock()
			}
			if handed {
				// The draining write runs on its own, a writer Stop waits
				// for: a call cancelled at the deadline may take time to
				// unwind, and the lease is let go at the deadline whatever
				// it does.
				marked := make(chan struct{})
				o.writers.Add(1)
				go func() {
					defer o.writers.Done()
					defer close(marked)
					for ctx.Err() == nil {
						if _, _, err := o.cfg.Spanner.OwnerMarkDraining(ctx, o.who(), l.ref()); err == nil {
							return
						}
						select {
						case <-time.After(firstBackoff):
						case <-ctx.Done():
						}
					}
				}()
				select {
				case <-marked:
				case <-ctx.Done():
				}
			}
			// The lease is let go now; its workers, such as a final
			// checkpoint's draining write already under way, end as they
			// unwind, writers Stop waits for.
			if stopped := o.release(l.id); stopped != nil {
				o.writers.Add(1)
				go func() {
					defer o.writers.Done()
					<-stopped
				}()
			}
		}()
	}
	all.Wait()
	errs[0] = ctx.Err()
	err := errors.Join(errs...)
	o.mu.Lock()
	o.handoffErr = err
	o.mu.Unlock()
	close(done)
	return err
}

// handedOver hands the lease's open holds off (handoff), and reports
// whether its manifest was acknowledged while ctx lasted and the lease was
// held, and why its holds could not be handed over, if not for time.
func (l *Lease) handedOver(ctx context.Context) (bool, error) {
	m, err := l.handoff(ctx)
	if m == nil {
		return false, err
	}
	select {
	case <-m.done: // closed once the manifest is acknowledged, and only then
		return true, nil
	case <-ctx.Done():
	case <-l.stop:
	}
	return false, nil
}

// handoff stops the lease admitting, hands over its open holds in chunks
// and then the manifest, and issues no record after it, all under the
// lease's lock, so no hold is admitted after its chunk is cut and no
// record follows the manifest. It returns the manifest's record, or nil if
// it handed none over: let go, past the cutoff, or once ctx ended, which it
// checks as it orders the holds, between holds and before the manifest; or,
// with an error, a hold or a record the log cannot take.
func (l *Lease) handoff(ctx context.Context) (*sent, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.closing = true
	if l.let || l.handedOff {
		return nil, nil
	}
	auths, ok := l.sortedAuths(func() bool { return ctx.Err() != nil })
	if !ok {
		return nil, nil
	}
	holds := make([]record.HeldHold, 0, len(auths))
	for _, auth := range auths {
		if ctx.Err() != nil {
			return nil, nil
		}
		h := l.holds[auth]
		held := record.HeldHold{Auth: h.auth, Estimate: h.estimate, Deadline: h.endOfLife.UTC(), Boot: h.boot}
		if h.heartbeat {
			usage, err := json.Marshal(map[string]int64{"tokens": h.usage})
			if err != nil {
				return nil, err
			}
			held.Deadline = h.deadline.UTC()
			held.Snapshot = &record.Snapshot{GatewaySeq: h.gatewaySeq, Hash: h.hash, Usage: usage,
				Running: h.running, Deadline: held.Deadline}
			held.SnapshotSeq, held.Basis = h.snapSeq, h.basis
		}
		holds = append(holds, held)
	}
	// Each hold is encoded once, as the digest takes it. Each chunk is sized
	// as it is handed over, at the sequence number it takes: its first
	// hold's record is encoded, and each hold after it adds its own
	// encoding and a comma, as JSON writes a list, so sizing takes time in
	// step with the holds. A hand-off whose time is up stops between holds,
	// and counts as none.
	digest := record.NewHoldsHasher()
	sizes := make([]int, len(holds))
	for i, h := range holds {
		if ctx.Err() != nil {
			return nil, nil
		}
		data, err := digest.Add(h)
		if err != nil {
			return nil, err
		}
		sizes[i] = len(data)
	}
	var seqs []int64
	for at := 0; at < len(holds); {
		if ctx.Err() != nil {
			return nil, nil
		}
		size := l.encodedSize(holds[at : at+1])
		n := 1
		for at+n < len(holds) && size+1+sizes[at+n] <= maxRecord {
			size += 1 + sizes[at+n]
			n++
		}
		s, err := l.handOver(record.Record{Kind: record.Handoff, Holds: holds[at : at+n]}, 0)
		if err != nil {
			return nil, handOverErr(err)
		}
		seqs = append(seqs, s.seq)
		at += n
	}
	if ctx.Err() != nil {
		return nil, nil
	}
	m, err := l.handOver(record.Record{Kind: record.Manifest, Manifest: &record.ManifestOf{Chunks: len(seqs),
		HoldsDigest: digest.Sum(), Seqs: seqs}}, 0)
	if err != nil {
		return nil, handOverErr(err)
	}
	l.handedOff = true
	return m, nil
}

// sortedAuths is the lease's holds' authorizations in order, as the digest
// takes them, or false once stopped says so, which it asks before every
// sortRun holds it gathers and as it sorts them (sortChecked): a lease of a
// million holds keeps its lock little past a hand-off's deadline.
func (l *Lease) sortedAuths(stopped func() bool) ([]string, bool) {
	auths := make([]string, 0, len(l.holds))
	for auth := range l.holds {
		if len(auths)%sortRun == 0 && stopped() {
			return nil, false
		}
		auths = append(auths, auth)
	}
	return sortChecked(auths, stopped)
}

// sortRun is how many strings sortChecked sorts between its checks.
const sortRun = 1 << 12

// sortChecked sorts xs, in runs of sortRun sorted alone and then merged in
// pairs, asking stopped before each run and before every sortRun strings a
// merge writes. It returns the sorted strings, xs or another slice, or
// false at once when stopped says so.
func sortChecked(xs []string, stopped func() bool) ([]string, bool) {
	n := len(xs)
	for lo := 0; lo < n; lo += sortRun {
		if stopped() {
			return nil, false
		}
		slices.Sort(xs[lo:min(lo+sortRun, n)])
	}
	src, dst := xs, make([]string, n)
	for width := sortRun; width < n; width *= 2 {
		for lo := 0; lo < n; lo += 2 * width {
			mid, hi := min(lo+width, n), min(lo+2*width, n)
			a, b, out := src[lo:mid], src[mid:hi], dst[lo:hi]
			i, j := 0, 0
			for k := range out {
				if k%sortRun == 0 && stopped() {
					return nil, false
				}
				if j == len(b) || (i < len(a) && a[i] <= b[j]) {
					out[k], i = a[i], i+1
				} else {
					out[k], j = b[j], j+1
				}
			}
		}
		src, dst = dst, src
	}
	return src, true
}

// handOverErr is a hand-off record's failure as handoff reports it: none
// past the cutoff, whose time is up.
func handOverErr(err error) error {
	if errors.Is(err, ErrPastCutoff) {
		return nil
	}
	return err
}

// encodedSize is the size of a hand-off record of the holds, numbered as
// the lease's next; past the settle log's record size if it cannot be
// encoded.
func (l *Lease) encodedSize(holds []record.HeldHold) int {
	data, err := record.Encode(record.Record{Version: record.Version, Lease: l.id, Epoch: l.o.cfg.Epoch,
		Seq: l.nextSeq, Kind: record.Handoff, Holds: holds})
	if err != nil {
		return maxRecord + 1
	}
	return len(data)
}
