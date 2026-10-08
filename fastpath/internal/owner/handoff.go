package owner

import (
	"context"
	"encoding/json"
	"errors"
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
// go. A second call waits for the first and reports as it does; Stop ends
// a hand-off under way.
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
	for _, l := range leases {
		all.Add(1)
		go func() {
			defer all.Done()
			if l.handedOver(ctx) {
				for ctx.Err() == nil {
					if _, _, err := o.cfg.Spanner.OwnerMarkDraining(ctx, o.who(), l.ref()); err == nil {
						break
					}
					select {
					case <-time.After(firstBackoff):
					case <-ctx.Done():
					}
				}
			}
			o.Let(l.id)
		}()
	}
	all.Wait()
	err := ctx.Err()
	o.mu.Lock()
	o.handoffErr = err
	o.mu.Unlock()
	close(done)
	return err
}

// handedOver hands the lease's open holds off (handoff), and reports
// whether its manifest was acknowledged while ctx lasted and the lease was
// held.
func (l *Lease) handedOver(ctx context.Context) bool {
	m := l.handoff(ctx)
	if m == nil {
		return false
	}
	select {
	case <-m.done: // closed once the manifest is acknowledged, and only then
		return true
	case <-ctx.Done():
	case <-l.stop:
	}
	return false
}

// handoff stops the lease admitting, hands over its open holds in chunks
// and then the manifest, and issues no record after it, all under the
// lease's lock, so no hold is admitted after its chunk is cut and no
// record follows the manifest. It returns the manifest's record, or nil
// if it handed none over: past the cutoff, or with a record the log cannot
// take.
func (l *Lease) handoff(ctx context.Context) *sent {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.closing = true
	if l.let || l.handedOff {
		return nil
	}
	holds := make([]record.HeldHold, 0, len(l.holds))
	for _, h := range l.holds {
		held := record.HeldHold{Auth: h.auth, Estimate: h.estimate, Deadline: h.endOfLife.UTC(), Boot: h.boot}
		if h.heartbeat {
			usage, err := json.Marshal(map[string]int64{"tokens": h.usage})
			if err != nil {
				return nil
			}
			held.Deadline = h.deadline.UTC()
			held.Snapshot = &record.Snapshot{GatewaySeq: h.gatewaySeq, Hash: h.hash, Usage: usage,
				Running: h.running, Deadline: held.Deadline}
			held.SnapshotSeq, held.Basis = h.snapSeq, h.basis
		}
		holds = append(holds, held)
	}
	slices.SortFunc(holds, func(a, b record.HeldHold) int { return strings.Compare(a.Auth, b.Auth) })
	digest, err := record.HoldsDigest(holds)
	if err != nil {
		return nil
	}
	// Each chunk is sized as it is handed over, at the sequence number it
	// takes: its first hold's record is encoded, and each hold after it adds
	// its own encoding and a comma, as JSON writes a list, so sizing takes
	// time in step with the holds. A hand-off whose time is up stops between
	// chunks, and counts as none.
	sizes := make([]int, len(holds))
	for i, h := range holds {
		data, err := json.Marshal(h)
		if err != nil {
			return nil
		}
		sizes[i] = len(data)
	}
	var seqs []int64
	for at := 0; at < len(holds); {
		if ctx.Err() != nil {
			return nil
		}
		size := l.encodedSize(holds[at : at+1])
		n := 1
		for at+n < len(holds) && size+1+sizes[at+n] <= maxRecord {
			size += 1 + sizes[at+n]
			n++
		}
		s, err := l.handOver(record.Record{Kind: record.Handoff, Holds: holds[at : at+n]}, 0)
		if err != nil {
			return nil
		}
		seqs = append(seqs, s.seq)
		at += n
	}
	m, err := l.handOver(record.Record{Kind: record.Manifest, Manifest: &record.ManifestOf{Chunks: len(seqs),
		HoldsDigest: digest, Seqs: seqs}}, 0)
	if err != nil {
		return nil
	}
	l.handedOff = true
	return m
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
