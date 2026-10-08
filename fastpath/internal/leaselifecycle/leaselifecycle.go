// Package leaselifecycle is the shadow of proofs/LeaseLifecycle.tla: one lease
// over time, its renewals and their answers, the owner's cutoff, expiry,
// draining and close, with clocks that differ by up to the skew allowance, and
// an owner process that dies and comes back.
//
// Each action of the spec is a function here under the spec's name, and each
// invariant a method. Next gives every step the spec allows from a state,
// labeled as TLC labels it, so the tests can hold the two side by side
// (docs/design/fast-admission-spike.md, section 2). The owner and the auditor
// of the spike decide with the predicates below (WithinCutoff, CanAdmit and
// the auditor's clock tests), so that the code and the model share them.
//
// What the spec abstracts, this does too: every hold is one unit, and money is
// CreditDebt's.
package leaselifecycle

import "fmt"

// Config is the spec's constants.
type Config struct {
	MaxHolds    int // holds open at once
	LeaseSize   int // the lease's allocation, in holds
	Window      int // a grant or renewal sets the expiry this far ahead
	Skew        int // the skew allowance
	MaxLife     int // the longest a hold lives
	Grace       int // the auditor's grace before it closes on time alone
	CacheAge    int // the oldest view of the workspace a process may admit on
	LastRenew   int // no renewal commits after this time
	MaxRestarts int // process restarts
}

// MaxSlots bounds Config.MaxHolds, so that a State is a small comparable value.
const MaxSlots = 4

// Validate reports a configuration the spec's ASSUME refuses, or this package
// cannot hold.
func (c Config) Validate() error {
	// Each constant is bounded before any sum of them is taken, so that no sum
	// below can overflow.
	for _, k := range []struct {
		name  string
		value int
	}{
		{"LeaseSize", c.LeaseSize}, {"Window", c.Window}, {"Skew", c.Skew}, {"MaxLife", c.MaxLife},
		{"Grace", c.Grace}, {"CacheAge", c.CacheAge}, {"LastRenew", c.LastRenew}, {"MaxRestarts", c.MaxRestarts},
	} {
		if k.value > 120 {
			return fmt.Errorf("%s %d does not fit a State", k.name, k.value)
		}
	}
	switch {
	case c.MaxHolds < 1 || c.MaxHolds > MaxSlots:
		return fmt.Errorf("MaxHolds %d is not in 1..%d", c.MaxHolds, MaxSlots)
	case c.LeaseSize < 1:
		return fmt.Errorf("LeaseSize %d is not positive", c.LeaseSize)
	case c.Window < 1 || c.MaxLife < 1:
		return fmt.Errorf("Window and MaxLife must be positive")
	case c.Skew < 0 || c.Grace < c.Skew || c.CacheAge < 0 || c.LastRenew < 0 || c.MaxRestarts < 0:
		return fmt.Errorf("Skew, CacheAge, LastRenew and MaxRestarts must be natural and Grace at least Skew")
	case c.MaxTime()+c.Skew > 120 || c.MaxRestarts > 120:
		return fmt.Errorf("times up to %d and %d restarts do not fit a State", c.MaxTime()+c.Skew, c.MaxRestarts)
	}
	return nil
}

// MaxExpiry is the latest expiry a renewal can set.
func (c Config) MaxExpiry() int { return c.LastRenew + c.Window }

// MaxTime is late enough that the lease can drain and close on time alone.
func (c Config) MaxTime() int { return c.MaxExpiry() + c.MaxLife + c.Grace }

// NoRead and NoAnswer are the spec's markers for an expiry not read and a
// renewal answer not on its way.
func (c Config) NoRead() int8   { return int8(c.MaxExpiry() + 1) }
func (c Config) NoAnswer() int8 { return int8(c.MaxExpiry() + 1) }

// LeaseState is the lease row's state in Spanner.
type LeaseState int8

const (
	Open LeaseState = iota
	Draining
	Closed
)

func (s LeaseState) String() string { return [...]string{"open", "draining", "closed"}[s] }

// Handoff is what the auditor has of the lease's hand-off.
type Handoff int8

const (
	NoHandoff Handoff = iota
	Partial
	Complete
)

func (h Handoff) String() string { return [...]string{"none", "partial", "complete"}[h] }

// Lease is the lease row.
type Lease struct {
	State   LeaseState
	Epoch   int8
	Expiry  int8
	Revoked bool
}

// Hold is one hold slot, with what was true when its hold was admitted.
type Hold struct {
	Open           bool
	Epoch          int8
	Life           int8
	UnderOpen      bool
	InPauseAge     bool
	InRevokeWindow bool
	Late           bool
}

// NoHold is a free slot.
var NoHold = Hold{UnderOpen: true, InPauseAge: true, InRevokeWindow: true}

// View is a process's view of the workspace: paused, and its age.
type View struct {
	Paused bool
	Age    int8
}

// State is the spec's variables. Holds[i] is the spec's holds[i+1], and bit i
// of Listed is hold i+1.
type State struct {
	Now        int8
	Lease      Lease
	Epoch      int8
	Has        bool
	Known      int8
	Stopped    bool
	Answer     int8
	Holds      [MaxSlots]Hold
	Handoff    Handoff
	Listed     uint8
	AudRead    int8
	Paused     bool
	PausedFor  int8
	View       View
	RevokedFor int8
}

// Init is the lease granted at time 0 to the first process.
func (c Config) Init() State {
	s := State{
		Lease:   Lease{State: Open, Expiry: int8(c.Window)},
		Has:     true,
		Known:   int8(c.Window),
		Answer:  c.NoAnswer(),
		AudRead: c.NoRead(),
	}
	for i := range s.Holds {
		s.Holds[i] = NoHold
	}
	return s
}

// Transition is one step: the action TLC would label it with, and the state
// it leads to.
type Transition struct {
	Action string
	To     State
}

// ReadingsMin and ReadingsMax bound what a clock may read at true time now
// (A1): every reading is within Skew of it, and none is negative.
func (c Config) ReadingsMin(now int8) int {
	return max(0, int(now)-c.Skew)
}

func (c Config) ReadingsMax(now int8) int {
	return min(int(now)+c.Skew, c.MaxTime()+c.Skew)
}

// WithinCutoff: some reading of the owner's clock is before the expiry it
// knows, less the skew.
func (c Config) WithinCutoff(now, known int8) bool {
	return c.ReadingsMin(now)+c.Skew < int(known)
}

// OpenCount is how many holds are open; OwnCount how many of them the current
// process admitted.
func (c Config) OpenCount(s State) int {
	n := 0
	for i := range c.MaxHolds {
		if s.Holds[i].Open {
			n++
		}
	}
	return n
}

func (c Config) own(s State) uint8 {
	var own uint8
	for i := range c.MaxHolds {
		if s.Holds[i].Open && s.Holds[i].Epoch == s.Epoch {
			own |= 1 << i
		}
	}
	return own
}

// CanAdmit is Admit's enabling condition: the process holds the lease and has
// not stopped, a slot is free, the lease has room for another of its holds,
// the cutoff has not passed, and its view is fresh and not paused.
func (c Config) CanAdmit(s State) bool {
	return s.Has &&
		!s.Stopped &&
		c.OpenCount(s) < c.MaxHolds &&
		popcount(c.own(s)) < c.LeaseSize &&
		c.WithinCutoff(s.Now, s.Known) &&
		!s.View.Paused &&
		int(s.View.Age) <= c.CacheAge
}

// Next is every step the spec's Next allows from s, in the order of its
// disjuncts.
func (c Config) Next(s State) []Transition {
	var out []Transition
	add := func(action string, ok bool, to State) {
		if ok {
			out = append(out, Transition{action, to})
		}
	}
	add(c.OwnerRenew(s))
	add(c.RenewAnswer(s))
	add(c.AnswerLost(s))
	add(c.ReplayedRenew(s))
	add(c.Revoke(s))
	add(c.Pause(s))
	add(c.RefreshView(s))
	add(c.Admit(s))
	for h := 1; h <= c.MaxHolds; h++ {
		add(c.HoldEnds(s, h))
	}
	add(c.OwnerStop(s))
	add(c.FinalCheckpoint(s))
	for _, to := range c.ForcedExitStart(s) {
		out = append(out, Transition{"ForcedExitStart", to})
	}
	add(c.ForcedExitManifest(s))
	add(c.OwnerDrains(s))
	add(c.OwnerDrops(s))
	add(c.Restart(s))
	add(c.AuditorRead(s))
	add(c.AuditorMark(s))
	add(c.AuditorMarkRefused(s))
	add(c.CloseOnTheList(s))
	add(c.CloseOnTime(s))
	add(c.Tick(s))
	return out
}

// --- Spanner: renewals, revocation, pause

// OwnerRenew is a renewal by the process the lease was granted to, conditional
// on the lease being open and not revoked and on the process's epoch. Its
// answer, the new expiry, sets out for the process.
func (c Config) OwnerRenew(s State) (string, bool, State) {
	ok := int(s.Now) <= c.LastRenew &&
		s.Answer == c.NoAnswer() &&
		s.Lease.State == Open &&
		!s.Lease.Revoked &&
		s.Lease.Epoch == s.Epoch
	s.Lease.Expiry = s.Now + int8(c.Window)
	s.Answer = s.Now + int8(c.Window)
	return "OwnerRenew", ok, s
}

// RenewAnswer: the answer arrives, and a process that still holds the lease
// takes the new expiry from it (A4).
func (c Config) RenewAnswer(s State) (string, bool, State) {
	ok := s.Answer != c.NoAnswer() && s.Has
	s.Known = s.Answer
	s.Answer = c.NoAnswer()
	return "RenewAnswer", ok, s
}

// AnswerLost: the answer is lost, or reaches a process that no longer holds
// the lease and is dropped there.
func (c Config) AnswerLost(s State) (string, bool, State) {
	ok := s.Answer != c.NoAnswer()
	s.Answer = c.NoAnswer()
	return "AnswerLost", ok, s
}

// ReplayedRenew is a renewal request from the lease's own process, applied
// when nobody is listening.
func (c Config) ReplayedRenew(s State) (string, bool, State) {
	ok := int(s.Now) <= c.LastRenew &&
		s.Lease.State == Open &&
		!s.Lease.Revoked &&
		int(s.Lease.Expiry) < int(s.Now)+c.Window
	s.Lease.Expiry = s.Now + int8(c.Window)
	return "ReplayedRenew", ok, s
}

// Revoke: a front door that cannot reach the owner revokes the lease's renewal.
func (c Config) Revoke(s State) (string, bool, State) {
	ok := !s.Lease.Revoked
	s.Lease.Revoked = true
	s.RevokedFor = 0
	return "Revoke", ok, s
}

// Pause pauses the workspace.
func (c Config) Pause(s State) (string, bool, State) {
	ok := !s.Paused
	s.Paused = true
	s.PausedFor = 0
	return "Pause", ok, s
}

// --- The owner process

// RefreshView: after a pause, a process that looks again sees it.
func (c Config) RefreshView(s State) (string, bool, State) {
	ok := s.Paused && !s.View.Paused
	s.View = View{Paused: true}
	return "RefreshView", ok, s
}

// Admit is an admission, in memory, into the lowest free slot. The process
// reads its clock after recording the hold, so the cutoff check and the hold
// are one step.
func (c Config) Admit(s State) (string, bool, State) {
	ok := c.CanAdmit(s)
	if ok {
		h := 0
		for s.Holds[h].Open {
			h++
		}
		s.Holds[h] = Hold{
			Open:           true,
			Epoch:          s.Epoch,
			Life:           int8(c.MaxLife),
			UnderOpen:      s.Lease.State == Open,
			InPauseAge:     !s.Paused || int(s.PausedFor) <= c.CacheAge,
			InRevokeWindow: !s.Lease.Revoked || int(s.RevokedFor) < c.Window,
			Late:           s.Handoff != NoHandoff,
		}
	}
	return "Admit", ok, s
}

// HoldEnds is hold h's terminal, wherever it lands. h counts from 1, as the
// spec's HoldIds do.
func (c Config) HoldEnds(s State, h int) (string, bool, State) {
	ok := s.Holds[h-1].Open
	s.Holds[h-1] = NoHold
	s.Listed &^= 1 << (h - 1)
	return fmt.Sprintf("HoldEnds(%d)", h), ok, s
}

// OwnerStop: the process stops admitting under the lease.
func (c Config) OwnerStop(s State) (string, bool, State) {
	ok := s.Has && !s.Stopped
	s.Stopped = true
	return "OwnerStop", ok, s
}

// FinalCheckpoint: with no hold of its own open, the process publishes the
// lease's last record, which lists the holds still open.
func (c Config) FinalCheckpoint(s State) (string, bool, State) {
	own := c.own(s)
	ok := s.Has && s.Stopped && own == 0
	s.Handoff = Complete
	s.Listed = own
	return "FinalCheckpoint", ok, s
}

// ForcedExitStart: the process stops admitting and publishes its open holds
// in chunks, of which the auditor may have any, or none. One successor for
// each subset of its holds.
func (c Config) ForcedExitStart(s State) []State {
	if !s.Has || s.Handoff != NoHandoff {
		return nil
	}
	own := c.own(s)
	s.Stopped = true
	s.Handoff = Partial
	var out []State
	// Every subset of own, the empty one included.
	for sub := own; ; sub = (sub - 1) & own {
		next := s
		next.Listed = sub
		out = append(out, next)
		if sub == 0 {
			break
		}
	}
	return out
}

// ForcedExitManifest makes the list whole: the holds the chunks were cut from.
func (c Config) ForcedExitManifest(s State) (string, bool, State) {
	ok := s.Has && s.Handoff == Partial
	s.Handoff = Complete
	var listed uint8
	own := c.own(s)
	for i := range c.MaxHolds {
		if own&(1<<i) != 0 && !s.Holds[i].Late {
			listed |= 1 << i
		}
	}
	s.Listed = listed
	return "ForcedExitManifest", ok, s
}

// OwnerDrains: its last record published, the process marks the lease
// draining, conditional on the lease being open and on its epoch, and lets it
// go.
func (c Config) OwnerDrains(s State) (string, bool, State) {
	ok := s.Has && s.Handoff == Complete && s.Lease.State == Open && s.Lease.Epoch == s.Epoch
	s.Lease.State = Draining
	s.Has = false
	return "OwnerDrains", ok, s
}

// OwnerDrops: a renewal changed no row, and the process, re-reading, finds
// the lease revoked or no longer open and lets it go.
func (c Config) OwnerDrops(s State) (string, bool, State) {
	ok := s.Has && (s.Lease.State != Open || s.Lease.Revoked)
	s.Has = false
	return "OwnerDrops", ok, s
}

// Restart: the process dies and another starts, with a new epoch and nothing
// in memory, reading the workspace's state as it starts.
func (c Config) Restart(s State) (string, bool, State) {
	ok := int(s.Epoch) < c.MaxRestarts
	s.Epoch++
	s.Has = false
	s.Known = 0
	s.Stopped = false
	s.Answer = c.NoAnswer()
	s.View = View{Paused: s.Paused}
	return "Restart", ok, s
}

// --- The auditor

// ExpiredForAuditor: some reading of the auditor's clock is past the expiry
// plus the skew allowance.
func (c Config) ExpiredForAuditor(now, expiry int8) bool {
	return c.ReadingsMax(now) >= int(expiry)+c.Skew
}

// ClosableOnTime: some reading of the auditor's clock is past the expiry plus
// the longest a hold can live plus the grace.
func (c Config) ClosableOnTime(now, expiry int8) bool {
	return c.ReadingsMax(now) >= int(expiry)+c.MaxLife+c.Grace
}

// AuditorRead reads a lease whose expiry, plus the skew, its clock has passed.
func (c Config) AuditorRead(s State) (string, bool, State) {
	ok := s.Lease.State == Open && s.AudRead == c.NoRead() && c.ExpiredForAuditor(s.Now, s.Lease.Expiry)
	s.AudRead = s.Lease.Expiry
	return "AuditorRead", ok, s
}

// AuditorMark marks it draining, conditional on the lease still being open
// with the expiry it read.
func (c Config) AuditorMark(s State) (string, bool, State) {
	ok := s.AudRead != c.NoRead() && s.Lease.State == Open && s.Lease.Expiry == s.AudRead
	s.Lease.State = Draining
	s.AudRead = c.NoRead()
	return "AuditorMark", ok, s
}

// AuditorMarkRefused: the write changed no row. The auditor will read again.
func (c Config) AuditorMarkRefused(s State) (string, bool, State) {
	ok := s.AudRead != c.NoRead() && (s.Lease.State != Open || s.Lease.Expiry != s.AudRead)
	s.AudRead = c.NoRead()
	return "AuditorMarkRefused", ok, s
}

// CloseOnTheList: the owner's last record listed its holds and they have all
// ended.
func (c Config) CloseOnTheList(s State) (string, bool, State) {
	ok := s.Lease.State == Draining && s.Handoff == Complete && s.Listed == 0
	s.Lease.State = Closed
	return "CloseOnTheList", ok, s
}

// CloseOnTime: the auditor's clock is past the expiry plus the longest a hold
// can live plus the grace.
func (c Config) CloseOnTime(s State) (string, bool, State) {
	ok := s.Lease.State == Draining && c.ClosableOnTime(s.Now, s.Lease.Expiry)
	s.Lease.State = Closed
	return "CloseOnTime", ok, s
}

// --- Time

// Tick: time passes. A hold that reaches its life has ended (A3), and the ages
// of a pause, a revocation and a view are counted as far as anything asks.
func (c Config) Tick(s State) (string, bool, State) {
	ok := int(s.Now) < c.MaxTime()
	prev := s
	s.Now++
	for i := range c.MaxHolds {
		switch {
		case !prev.Holds[i].Open:
		case prev.Holds[i].Life == 1:
			s.Holds[i] = NoHold
			s.Listed &^= 1 << i
		default:
			s.Holds[i].Life--
		}
	}
	if prev.Paused {
		s.View.Age = int8(min(int(prev.View.Age)+1, c.CacheAge+1))
		s.PausedFor = int8(min(int(prev.PausedFor)+1, c.CacheAge+1))
	} else {
		s.View = View{}
		s.PausedFor = 0
	}
	if prev.Lease.Revoked {
		s.RevokedFor = int8(min(int(prev.RevokedFor)+1, c.Window))
	} else {
		s.RevokedFor = 0
	}
	return "Tick", ok, s
}

// --- Invariants

// Invariants are the spec's, by name, in its .cfg's order.
func (c Config) Invariants() []Invariant {
	return []Invariant{
		{"TypeOK", c.TypeOK},
		{"CutoffBeforeDrain", c.CutoffBeforeDrain},
		{"AdmittedOnlyUnderOpenLease", c.AdmittedOnlyUnderOpenLease},
		{"NoOpenHoldOnClosedLease", c.NoOpenHoldOnClosedLease},
		{"HoldsFitAllocation", c.HoldsFitAllocation},
		{"SingleWriter", c.SingleWriter},
		{"PauseBoundsAdmission", c.PauseBoundsAdmission},
		{"RevocationBoundsAdmission", c.RevocationBoundsAdmission},
	}
}

// Invariant is a named predicate on a state.
type Invariant struct {
	Name  string
	Holds func(State) bool
}

func in(v int8, lo, hi int) bool { return int(v) >= lo && int(v) <= hi }

// TypeOK is the spec's TypeOK: every variable within its bounds.
func (c Config) TypeOK(s State) bool {
	maxExpiry := c.MaxExpiry()
	if !in(s.Now, 0, c.MaxTime()) ||
		s.Lease.State < Open || s.Lease.State > Closed ||
		!in(s.Lease.Epoch, 0, c.MaxRestarts) ||
		!in(s.Lease.Expiry, 0, maxExpiry) ||
		!in(s.Epoch, 0, c.MaxRestarts) ||
		!in(s.Known, 0, maxExpiry) ||
		!in(s.Answer, 0, maxExpiry+1) ||
		s.Handoff < NoHandoff || s.Handoff > Complete ||
		!in(s.AudRead, 0, maxExpiry+1) ||
		!in(s.PausedFor, 0, c.CacheAge+1) ||
		!in(s.View.Age, 0, c.CacheAge+1) ||
		!in(s.RevokedFor, 0, c.Window) {
		return false
	}
	var open uint8
	for i := range c.MaxHolds {
		h := s.Holds[i]
		if !in(h.Epoch, 0, c.MaxRestarts) || !in(h.Life, 0, c.MaxLife) {
			return false
		}
		if h.Open {
			open |= 1 << i
		}
	}
	return s.Listed&^open == 0
}

// CutoffBeforeDrain: once the lease is draining or closed, a process that
// still holds it is past its cutoff.
func (c Config) CutoffBeforeDrain(s State) bool {
	return !(s.Has && s.Lease.State != Open) || !c.WithinCutoff(s.Now, s.Known)
}

func (c Config) everyOpen(s State, p func(Hold) bool) bool {
	for i := range c.MaxHolds {
		if s.Holds[i].Open && !p(s.Holds[i]) {
			return false
		}
	}
	return true
}

// AdmittedOnlyUnderOpenLease: every admission was under a lease open in
// Spanner at that moment.
func (c Config) AdmittedOnlyUnderOpenLease(s State) bool {
	return c.everyOpen(s, func(h Hold) bool { return h.UnderOpen })
}

// NoOpenHoldOnClosedLease: the lease is not closed while a hold admitted under
// it is open.
func (c Config) NoOpenHoldOnClosedLease(s State) bool {
	return c.OpenCount(s) == 0 || s.Lease.State != Closed
}

// HoldsFitAllocation: the lease's open holds fit its allocation.
func (c Config) HoldsFitAllocation(s State) bool { return c.OpenCount(s) <= c.LeaseSize }

// SingleWriter: only the process the lease was granted to admits under it.
func (c Config) SingleWriter(s State) bool {
	return c.everyOpen(s, func(h Hold) bool { return h.Epoch == s.Lease.Epoch })
}

// PauseBoundsAdmission: a pause stops admission within the cache's age.
func (c Config) PauseBoundsAdmission(s State) bool {
	return c.everyOpen(s, func(h Hold) bool { return h.InPauseAge })
}

// RevocationBoundsAdmission: nothing is admitted under a revoked lease a
// window later.
func (c Config) RevocationBoundsAdmission(s State) bool {
	return c.everyOpen(s, func(h Hold) bool { return h.InRevokeWindow })
}

// --- What a step may do to the lease's row

// StepProperties are the spec's [][A]_vars properties, by name.
func StepProperties() []StepProperty {
	return []StepProperty{
		{"LeaseMovesOneWay", LeaseMovesOneWay},
		{"DrainingExpiryStays", DrainingExpiryStays},
		{"RevokedExpiryStays", RevokedExpiryStays},
	}
}

// StepProperty is a named predicate on a step.
type StepProperty struct {
	Name  string
	Holds func(from, to State) bool
}

// LeaseMovesOneWay: open, then draining, then closed, one step at a time.
func LeaseMovesOneWay(from, to State) bool {
	return to.Lease.State == from.Lease.State ||
		from.Lease.State == Open && to.Lease.State == Draining ||
		from.Lease.State == Draining && to.Lease.State == Closed
}

// DrainingExpiryStays: once a lease is draining its expiry does not move.
func DrainingExpiryStays(from, to State) bool {
	return from.Lease.State == Open || to.Lease.Expiry == from.Lease.Expiry
}

// RevokedExpiryStays: once a lease's renewal is revoked its expiry does not
// move.
func RevokedExpiryStays(from, to State) bool {
	return !from.Lease.Revoked || to.Lease.Expiry == from.Lease.Expiry
}

func popcount(x uint8) int {
	n := 0
	for ; x != 0; x &= x - 1 {
		n++
	}
	return n
}
