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
// leases' holds end. Under each lease it holds, it stops admitting, then
// publishes the lease's open holds in hand-off records, chunked under the
// settle log's record size, then a manifest: the number of chunks, the
// digest of their holds and the sequence numbers they used. From the
// manifest on, the lease decides nothing. Once the manifest is acknowledged
// the owner marks the lease draining, by the conditional write a final
// checkpoint's finish makes, while ctx lasts, and lets it go; at ctx's end
// it lets it go as it is, and the auditor marks it draining once it
// expires. A hand-off whose manifest or a chunk is not stored counts as
// none, and the auditor drains the lease by time (§4.8). Leases are handed
// off at once; Handoff returns once each is let go.
func (o *Owner) Handoff(ctx context.Context) error {
	if o.cfg.Spanner == nil {
		return errors.New("owner: no store to mark leases draining in")
	}
	o.mu.Lock()
	leases := make([]*Lease, 0, len(o.leases))
	for _, l := range o.leases {
		leases = append(leases, l)
	}
	o.mu.Unlock()
	slices.SortFunc(leases, func(a, b *Lease) int { return strings.Compare(a.id, b.id) })
	var all sync.WaitGroup
	for _, l := range leases {
		all.Add(1)
		go func() {
			defer all.Done()
			manifest := l.handoff()
			if manifest != nil {
				select {
				case <-manifest.done:
				case <-ctx.Done():
				case <-l.stop:
				}
			}
			for ctx.Err() == nil {
				if _, _, err := o.cfg.Spanner.OwnerMarkDraining(ctx, o.who(), l.ref()); err == nil {
					break
				}
				select {
				case <-time.After(firstBackoff):
				case <-ctx.Done():
				}
			}
			o.Let(l.id)
		}()
	}
	all.Wait()
	return ctx.Err()
}

// handoff stops the lease admitting, hands over its open holds in chunks
// and then the manifest, and decides nothing after it, all under the
// lease's lock, so no hold is admitted after its chunk is cut and no
// decision follows the manifest. It returns the manifest's record, or nil
// if it handed none over: past the cutoff, or with a record the log cannot
// take.
func (l *Lease) handoff() *sent {
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
	var seqs []int64
	for _, chunk := range l.chunked(holds) {
		s, err := l.handOver(record.Record{Kind: record.Handoff, Holds: chunk}, 0)
		if err != nil {
			return nil
		}
		seqs = append(seqs, s.seq)
	}
	m, err := l.handOver(record.Record{Kind: record.Manifest, Manifest: &record.ManifestOf{Chunks: len(seqs),
		HoldsDigest: digest, Seqs: seqs}}, 0)
	if err != nil {
		return nil
	}
	l.handedOff = true
	return m
}

// chunked splits the holds into hand-off records each the settle log takes:
// as many holds a record as fit, at least one, in order.
func (l *Lease) chunked(holds []record.HeldHold) [][]record.HeldHold {
	var chunks [][]record.HeldHold
	for len(holds) > 0 {
		n := 1
		for n < len(holds) && l.fits(holds[:n+1]) {
			n++
		}
		chunks = append(chunks, holds[:n])
		holds = holds[n:]
	}
	return chunks
}

// fits: a hand-off record of the holds, numbered as the lease's next, is
// within the settle log's record size.
func (l *Lease) fits(holds []record.HeldHold) bool {
	data, err := record.Encode(record.Record{Version: record.Version, Lease: l.id, Epoch: l.o.cfg.Epoch,
		Seq: l.nextSeq, Kind: record.Handoff, Holds: holds})
	return err == nil && len(data) <= maxRecord
}
