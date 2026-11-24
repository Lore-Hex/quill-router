package owner

import (
	"fmt"
	"log"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Starved is what an owner does with a starved shard's leases
// (docs/design/billing-503-reduction-2026-11.md, option A).
//
// A shard is starved when a request finds no room under any of its leases
// that still admit, just after the store refused the shard a top-up because
// the workspace's headroom outside leases is under the floor plus the amount
// asked (store.RefusedFloor), or because its leases already hold its
// allowance (store.RefusedAllowance), the headroom that refusal read is
// under the request's hold, and what the leases too small for it hold
// beyond what they consumed would make up the difference. An ask is sized for the shard's
// recent spend, not the request, so a refusal alone does not mean the
// synchronous path cannot take the request; the headroom does. The money the
// request needs is then split: part is the free room of a lease too small
// for the request, part is outside leases, too little for it. Python
// answers that synchronous hold 503 (fastpath_lease_headroom_503), and a
// retry meets the same answer until the lease stops admitting: idle for
// TopUps.IdleAfter, which any admission that still fits restarts, or
// TopUps.MaxLife old. On 2026-11-23 one workspace's requests were answered
// that 503 for about fifteen minutes, every gateway retry included.
//
// Closing such a lease is the transition idleness, age, a pause and a
// retirement already take (Lease.Close): it admits nothing more, serves
// every hold it has, and its next checkpoint returns its free room, its
// allocation less what it consumed, holds, pending and buffers. A stream's
// buffer before its first heartbeat, and its ceiling above its hold, are
// not reserved there, as for any close: an overrun past the returned room
// is the lease's shortfall, recorded and raised as today, so nothing is
// lost or charged twice. Its final checkpoint ends it once no hold is
// open. Only the moment changes.
type Starved string

const (
	// StarvedOff does nothing: today's behaviour. The zero value is off too.
	StarvedOff Starved = "off"
	// StarvedLog closes nothing and logs each starved shard at most once a
	// starvedLogEvery, with the admissions it refused since its last line:
	// the shadow mode, read before StarvedClose is turned on.
	StarvedLog Starved = "log"
	// StarvedClose closes the leases that refused the request for room,
	// and logs as StarvedLog does.
	StarvedClose Starved = "close"
)

// starvedFresh is how recent the store's refusal must be for a shard to be
// starved: a refusal older than this may no longer stand (credit added,
// holds settled), and the request's own ask comes first.
const starvedFresh = time.Minute

// starvedLogEvery is how often a starved shard is logged at most.
const starvedLogEvery = time.Minute

// String is the flag's value.
func (s *Starved) String() string {
	if s == nil || *s == "" {
		return string(StarvedOff)
	}
	return string(*s)
}

// Set parses the flag's value: off, log or close.
func (s *Starved) Set(v string) error {
	switch Starved(v) {
	case StarvedOff, StarvedLog, StarvedClose:
		*s = Starved(v)
		return nil
	}
	return fmt.Errorf("owner: starved leases are off, log or close, not %q", v)
}

func (s Starved) valid() bool {
	switch s {
	case "", StarvedOff, StarvedLog, StarvedClose:
		return true
	}
	return false
}

// starvingRefusal says whether a grant's refusal leaves money stranded in a
// lease too small for the request: the floor's and the allowance's. A
// workspace refused for its pause, debt, tier or switch has its leases
// closed by those paths, or is refused at admission.
func starvingRefusal(r store.Refusal) bool {
	return r == store.RefusedFloor || r == store.RefusedAllowance
}

// starved acts on a request for key that the leases in short refused for
// room (ErrNoRoom), estimate being its hold and need its hold and buffer: if the store's last
// answer to the shard's ask, within starvedFresh, was a starving refusal
// that read a headroom under estimate, the request's hold, and that
// headroom with what the leases hold beyond what they consumed would cover
// it, it closes them under StarvedClose and logs the shard (rate-limited).
// Nothing is done while the owner stops, retires or hands off: each of
// those closes its leases itself.
func (o *Owner) starved(key ShardKey, short []*Lease, estimate, need int64) {
	mode := o.cfg.StarvedLeases
	if len(short) == 0 || (mode != StarvedLog && mode != StarvedClose) {
		return
	}
	// The cheap checks first, under the owner's lock alone. Python's
	// synchronous hold is the estimate (the front door sends the reservation
	// as it), so the refusal's headroom is weighed against that, not the
	// owner's buffer: the synchronous path answers the 503 only while the
	// headroom is under it.
	o.mu.Lock()
	sh := o.shardLocked(key)
	now := o.cfg.Clock()
	if o.stopped || o.retiring || o.handoff != nil || sh.refusedAt.IsZero() || now.Sub(sh.refusedAt) > starvedFresh ||
		sh.refusedHeadroom >= estimate {
		o.mu.Unlock()
		return
	}
	headroom := sh.refusedHeadroom
	o.mu.Unlock()
	// Then the leases, each under its own lock and none under the owner's:
	// free is what their next checkpoints would return now, kept what they
	// hold beyond what they consumed, open holds included, which Python
	// counts as the leases' (fastpath_lease_held_of) and which comes back
	// at later checkpoints as the holds end. A workspace whose headroom
	// with that is still under the hold is spent: Python answers 402, and
	// the leases keep admitting what fits.
	var free, kept int64
	for _, l := range short {
		b := l.Books()
		free = sat(free, max(b.Free(), 0))
		kept = sat(kept, max(b.Allocation-b.Consumed, 0))
	}
	if signedSum(headroom, kept) < estimate {
		return
	}
	if mode == StarvedClose {
		for _, l := range short {
			l.Close()
		}
	}
	o.mu.Lock()
	sh.starvedN++
	refused, count, asked := sh.refused, sh.starvedN, sh.refusedAsk
	line := sh.starvedAt.IsZero() || now.Sub(sh.starvedAt) >= starvedLogEvery
	if line {
		sh.starvedAt, sh.starvedN = now, 0
	}
	o.mu.Unlock()
	if line {
		o.logf("fastpath: owner starved shard workspace=%s region=%s shard=%d mode=%s leases=%d free=%d kept=%d "+
			"need=%d headroom=%d asked=%d refused=%q admissions=%d", key.Workspace, key.Region, key.Shard, mode,
			len(short), free, kept, need, headroom, asked, refused, count)
	}
}

// signedSum is a headroom, which may be negative, plus an amount at least 0,
// saturating at the largest.
func signedSum(headroom, amount int64) int64 {
	if headroom < 0 {
		return headroom + amount // opposite signs: no overflow
	}
	return sat(headroom, amount)
}

// noteGrant keeps the store's answer to the shard's ask of asked, with o.mu
// held: a starving refusal is remembered with when it came, the amount
// asked and the headroom it read; any other answer forgets the last. A lost
// answer changes nothing.
func (o *Owner) noteGrant(sh *shard, asked int64, got store.GrantResult, err error) {
	switch {
	case err != nil:
	case starvingRefusal(got.Refused):
		sh.refusedAt, sh.refused, sh.refusedAsk, sh.refusedHeadroom = o.cfg.Clock(), got.Refused, asked, got.Headroom
	default:
		sh.refusedAt, sh.refused, sh.refusedAsk, sh.refusedHeadroom = time.Time{}, "", 0, 0
	}
}

func (o *Owner) logf(format string, args ...any) {
	if o.cfg.Logf != nil {
		o.cfg.Logf(format, args...)
		return
	}
	log.Printf(format, args...)
}
