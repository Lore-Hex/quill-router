// Package auditorcommit is the shadow of proofs/AuditorCommit.tla: the
// auditor's per-lease commit, what one member stores so that any member can
// carry on after it, and why redelivery, a member taking over and a member
// that stalled never book a record twice or lose one.
//
// Each action of the spec is a function here under the spec's name, and each
// invariant a method. Next gives every step the spec allows, labeled as TLC
// labels it, so the tests can hold the two side by side. Authorizations and
// members are numbered by their place in Config.Auths and Config.Members;
// sequences count from 1, as the spec's do.
package auditorcommit

import "fmt"

// Bounds of a State, so that it is a small comparable value.
const (
	MaxAuths   = 2
	MaxMembers = 2
	MaxOut     = 4
	MaxLog     = 7
	MaxRows    = 4
)

// Config is the spec's constants.
type Config struct {
	Auths     []string
	Members   []string
	MaxSeq    int
	MaxSnap   int
	MaxDup    int
	MaxAhead  int
	MaxLate   int
	MaxRaise  int
	MaxAssign int
	MaxCrash  int
	MaxAppend int
	Lying     bool
	Grant     int
}

// Validate reports a configuration the spec's ASSUME refuses, or this package
// cannot hold.
func (c Config) Validate() error {
	for _, k := range []struct {
		name  string
		value int
	}{
		{"MaxSeq", c.MaxSeq}, {"MaxSnap", c.MaxSnap}, {"MaxDup", c.MaxDup}, {"MaxAhead", c.MaxAhead},
		{"MaxLate", c.MaxLate}, {"MaxRaise", c.MaxRaise}, {"MaxAssign", c.MaxAssign},
		{"MaxCrash", c.MaxCrash}, {"MaxAppend", c.MaxAppend}, {"Grant", c.Grant},
	} {
		if k.value < 0 || k.value > 20 {
			return fmt.Errorf("%s %d is not in 0..20", k.name, k.value)
		}
	}
	switch {
	case len(c.Auths) > MaxAuths:
		return fmt.Errorf("%d authorizations, more than %d", len(c.Auths), MaxAuths)
	case len(c.Members) < 1 || len(c.Members) > MaxMembers:
		// The spec assumes Members # {}.
		return fmt.Errorf("%d members, not 1..%d", len(c.Members), MaxMembers)
	case c.MaxSeq > MaxOut:
		return fmt.Errorf("MaxSeq %d is more than the %d records a State holds", c.MaxSeq, MaxOut)
	case c.MaxSeq+c.MaxDup+1 > MaxLog:
		return fmt.Errorf("a log of %d records is more than a State holds", c.MaxSeq+c.MaxDup+1)
	case c.MaxAppend+len(c.Auths) > MaxRows:
		return fmt.Errorf("a drain log of %d rows is more than a State holds", c.MaxAppend+len(c.Auths))
	}
	for _, names := range [][]string{c.Auths, c.Members} {
		seen := map[string]bool{}
		for _, name := range names {
			if seen[name] {
				return fmt.Errorf("%s is named twice: a set holds it once", name)
			}
			seen[name] = true
		}
	}
	return nil
}

// Record kinds.
const (
	KNone int8 = iota
	KHb
	KSettle
	KRefund
	KCkpt
	KReap
	KTick
)

var kindNames = map[int8]string{
	KNone: "none", KHb: "hb", KSettle: "settle", KRefund: "refund", KCkpt: "ckpt", KReap: "reap", KTick: "tick",
}

// Lease states.
const (
	Open int8 = iota
	Draining
	Closed
)

var stateNames = map[int8]string{Open: "open", Draining: "draining", Closed: "closed"}

// The spec's numbers.
const (
	SettleCharge = 2
	DoorCharge   = 1
	NoAuth       = -1
	NoHold       = -1
	NoS          = -1
)

// Rec is a record: its kind, authorization (NoAuth for none), charge,
// sequence number and drain-log index.
type Rec struct {
	K, A, C, Seq, Idx int8
}

// NoWin and Tick are the spec's.
var (
	NoWin = Rec{KNone, NoAuth, 0, 0, 0}
	Tick  = Rec{KTick, NoAuth, 0, 0, 0}
)

// OwnerTerminal: a settle or a refund.
func OwnerTerminal(r Rec) bool { return r.K == KSettle || r.K == KRefund }

// Mem is a member's memory.
type Mem struct {
	Loaded  bool
	Ver     int8
	Prog    int8
	Alloc   int8
	Holds   [MaxAuths]int8
	Win     [MaxAuths]Rec
	WL      bool
	Osum    int8
	Dbooked int8
	Dirty   bool
	S       int8
	Fault   bool
}

// Blank is a member's memory before it reads the lease row.
func (c Config) Blank() Mem {
	m := Mem{S: NoS}
	for a := range c.Auths {
		m.Holds[a] = NoHold
		m.Win[a] = NoWin
	}
	return m
}

// State is the spec's variables. OwnerDone has bit a set for an
// authorization with a terminal issued.
type State struct {
	Out       [MaxOut]Rec
	OutLen    int8
	NextSeq   int8
	Osnap     [MaxAuths]int8
	OwnerSum  int8
	OwnerDone uint8
	Log       [MaxLog]Rec
	LogLen    int8
	Dups      int8
	Aheads    int8
	Ticked    bool
	Lates     int8
	St        int8
	Ver       int8
	Prog      int8
	Booked    int8
	Alloc     int8
	Holds     [MaxAuths]int8
	Win       [MaxAuths]Rec
	Osum      int8
	Raised    int8
	S         int8
	Drain     [MaxRows]Rec
	DrainLen  int8
	Appends   int8
	Holder    int8
	Acked     int8
	Assigns   int8
	Mem       [MaxMembers]Mem
	Pos       [MaxMembers]int8
	Dpos      [MaxMembers]int8
	Done      [MaxMembers]int8
	Crashes   int8
	Alert     bool
	Gap       bool
}

// Init is the spec's Init. Its holder is the member TLC's CHOOSE picks from
// Members, which is the first by name.
func (c Config) Init() State {
	s := State{NextSeq: 1, St: Open, Alloc: int8(c.Grant), S: NoS}
	for a := range c.Auths {
		s.Holds[a] = NoHold
		s.Win[a] = NoWin
	}
	for m := range c.Members {
		s.Mem[m] = c.Blank()
		s.Pos[m] = 1
		s.Dpos[m] = 1
	}
	return s
}

// Transition is one step: the action TLC would label it with, and the state
// it leads to.
type Transition struct {
	Action string
	To     State
}

// Undefined is what a function here panics with where TLC would stop because
// an expression has no value, such as an index outside a sequence. No state
// the spec reaches does that.
type Undefined struct{ What string }

func (u Undefined) Error() string { return "no value: " + u.What }

func (s *State) log(i int8) Rec {
	if i < 1 || i > s.LogLen {
		panic(Undefined{fmt.Sprintf("log[%d] of %d records", i, s.LogLen)})
	}
	return s.Log[i-1]
}

func (s *State) drain(i int8) Rec {
	if i < 1 || i > s.DrainLen {
		panic(Undefined{fmt.Sprintf("drain[%d] of %d rows", i, s.DrainLen)})
	}
	return s.Drain[i-1]
}

func (s *State) appendLog(r Rec) {
	s.Log[s.LogLen] = r
	s.LogLen++
}

func (s *State) appendOut(r Rec) {
	s.Out[s.OutLen] = r
	s.OutLen++
}

func (s *State) appendDrain(r Rec) {
	s.Drain[s.DrainLen] = r
	s.DrainLen++
}

// AppliedTo is a member's memory after it applies the owner record r. A
// terminal for an authorization with a winner charges nothing; the audit's sum
// counts every owner terminal, once per sequence number.
func AppliedTo(m Mem, r Rec) Mem {
	decides := OwnerTerminal(r) && m.Win[r.A] == NoWin
	out := m
	out.Prog = r.Seq
	out.Dirty = true
	switch {
	case r.K == KHb && m.Win[r.A] == NoWin:
		out.Holds[r.A] = r.C
	case decides:
		out.Holds[r.A] = NoHold
	}
	if decides {
		out.Win[r.A] = r
		out.Dbooked = m.Dbooked + r.C
	}
	if OwnerTerminal(r) {
		out.Osum = m.Osum + r.C
	}
	out.Fault = m.Fault || (r.K == KCkpt && r.C != m.Osum)
	return out
}

// RowAppliedTo is a member's memory after it applies the drain-log row r.
func RowAppliedTo(m Mem, r Rec) Mem {
	if m.Win[r.A] != NoWin {
		return m
	}
	out := m
	out.Win[r.A] = r
	out.Holds[r.A] = NoHold
	out.Dbooked = m.Dbooked + r.C
	out.Dirty = true
	return out
}

// Next is every step the spec's Next allows from s.
func (c Config) Next(s State) []Transition {
	var out []Transition
	add := func(action string, ok bool, to State) {
		if ok {
			out = append(out, Transition{action, to})
		}
	}
	for a := range int8(len(c.Auths)) {
		add(c.IssueHeartbeat(s, a))
	}
	for a := range int8(len(c.Auths)) {
		for _, k := range []int8{KSettle, KRefund} {
			add(c.IssueTerminal(s, a, k))
		}
	}
	add(c.IssueCheckpoint(s))
	add(c.IssueWrongCheckpoint(s))
	add(c.Store(s))
	add(c.StoreLate(s))
	add(c.StoreAhead(s))
	for i := 1; i <= c.MaxSeq+c.MaxDup+1; i++ {
		add(c.StoreAgain(s, int8(i)))
	}
	add(c.Raise(s))
	add(c.MarkDraining(s))
	for a := range int8(len(c.Auths)) {
		add(c.FrontDoorAppend(s, a))
	}
	add(c.FenceTick(s))
	for m := range int8(len(c.Members)) {
		add(c.Assign(s, m))
		add(c.Load(s, m))
		add(c.LoadWinners(s, m))
		add(c.ApplyRecord(s, m))
		add(c.SkipRecord(s, m))
		add(c.Gap(s, m))
		add(c.ApplyTick(s, m))
		add(c.ApplyRow(s, m))
		for a := range int8(len(c.Auths)) {
			add(c.Reap(s, m, a))
		}
		add(c.Commit(s, m))
		add(c.Reread(s, m))
		add(c.Ack(s, m))
		add(c.Crash(s, m))
		add(c.Close(s, m))
	}
	return out
}

func (c Config) memberLabel(action string, m int8) string {
	return fmt.Sprintf("%s(%s)", action, c.Members[m])
}

// --- The owner

func (c Config) owned(s *State, a int8) bool { return s.OwnerDone&(1<<a) != 0 }

// IssueHeartbeat issues a heartbeat carrying its hold's running charge.
func (c Config) IssueHeartbeat(s State, a int8) (string, bool, State) {
	ok := s.St == Open && int(s.NextSeq) <= c.MaxSeq && !c.owned(&s, a) && int(s.Osnap[a]) < c.MaxSnap
	if ok {
		s.appendOut(Rec{KHb, a, s.Osnap[a], s.NextSeq, 0})
		s.Osnap[a]++
		s.NextSeq++
	}
	return fmt.Sprintf("IssueHeartbeat(%s)", c.Auths[a]), ok, s
}

// IssueTerminal issues a's one terminal, a settle or a refund.
func (c Config) IssueTerminal(s State, a, k int8) (string, bool, State) {
	ok := s.St == Open && int(s.NextSeq) <= c.MaxSeq && !c.owned(&s, a)
	if ok {
		charge := int8(0)
		if k == KSettle {
			charge = SettleCharge
		}
		s.appendOut(Rec{k, a, charge, s.NextSeq, 0})
		s.OwnerSum += charge
		s.OwnerDone |= 1 << a
		s.NextSeq++
	}
	return fmt.Sprintf("IssueTerminal(%s,%q)", c.Auths[a], kindNames[k]), ok, s
}

// IssueCheckpoint: an honest owner's `consumed` is what its terminals issued
// so far charge.
func (c Config) IssueCheckpoint(s State) (string, bool, State) {
	ok := s.St == Open && int(s.NextSeq) <= c.MaxSeq
	if ok {
		s.appendOut(Rec{KCkpt, NoAuth, s.OwnerSum, s.NextSeq, 0})
		s.NextSeq++
	}
	return "IssueCheckpoint", ok, s
}

// IssueWrongCheckpoint: a lying owner's is one more.
func (c Config) IssueWrongCheckpoint(s State) (string, bool, State) {
	ok := c.Lying && s.St == Open && int(s.NextSeq) <= c.MaxSeq
	if ok {
		s.appendOut(Rec{KCkpt, NoAuth, s.OwnerSum + 1, s.NextSeq, 0})
		s.NextSeq++
	}
	return "IssueWrongCheckpoint", ok, s
}

// --- The log

func (s *State) popOut() Rec {
	r := s.Out[0]
	copy(s.Out[:], s.Out[1:s.OutLen])
	s.OutLen--
	s.Out[s.OutLen] = Rec{}
	return r
}

// Store: the log stores the next record issued, before the fence tick.
func (c Config) Store(s State) (string, bool, State) {
	ok := s.OutLen > 0 && !s.Ticked
	if ok {
		s.appendLog(s.popOut())
	}
	return "Store", ok, s
}

// StoreLate: a publish that timed out lands after the fence tick.
func (c Config) StoreLate(s State) (string, bool, State) {
	ok := s.OutLen > 0 && s.Ticked && int(s.Lates) < c.MaxLate
	if ok {
		s.appendLog(s.popOut())
		s.Lates++
	}
	return "StoreLate", ok, s
}

// StoreAhead: the record issued second is stored before the first.
func (c Config) StoreAhead(s State) (string, bool, State) {
	ok := int(s.Aheads) < c.MaxAhead && !s.Ticked && s.OutLen >= 2
	if ok {
		second := s.Out[1]
		copy(s.Out[1:], s.Out[2:s.OutLen])
		s.OutLen--
		s.Out[s.OutLen] = Rec{}
		s.appendLog(second)
		s.Aheads++
	}
	return "StoreAhead", ok, s
}

// StoreAgain: the log stores record i again.
func (c Config) StoreAgain(s State, i int8) (string, bool, State) {
	ok := int(s.Dups) < c.MaxDup && i <= s.LogLen
	if ok {
		s.appendLog(s.log(i))
		s.Dups++
	}
	return fmt.Sprintf("StoreAgain(%d)", i), ok, s
}

// --- Other writers of the lease row, and draining

// Raise: an owner's shortfall write or a front door's raise.
func (c Config) Raise(s State) (string, bool, State) {
	ok := s.St != Closed && int(s.Raised) < c.MaxRaise
	s.Alloc++
	s.Raised++
	return "Raise", ok, s
}

// MarkDraining: the lease expires, or its owner stops.
func (c Config) MarkDraining(s State) (string, bool, State) {
	ok := s.St == Open
	s.St = Draining
	return "MarkDraining", ok, s
}

// FrontDoorAppend appends a terminal the owner could not take.
func (c Config) FrontDoorAppend(s State, a int8) (string, bool, State) {
	ok := s.St == Draining && int(s.Appends) < c.MaxAppend
	if ok {
		s.appendDrain(Rec{KSettle, a, DoorCharge, 0, s.DrainLen + 1})
		s.Appends++
	}
	return fmt.Sprintf("FrontDoorAppend(%s)", c.Auths[a]), ok, s
}

// FenceTick: once the lease drains, the auditor publishes the fence tick.
func (c Config) FenceTick(s State) (string, bool, State) {
	ok := s.St == Draining && !s.Ticked
	if ok {
		s.appendLog(Tick)
		s.Ticked = true
	}
	return "FenceTick", ok, s
}

// --- Pub/Sub and the members

// Assign moves the lease's records to member m, from the first not
// acknowledged (A1).
func (c Config) Assign(s State, m int8) (string, bool, State) {
	ok := int(s.Assigns) < c.MaxAssign && m != s.Holder
	s.Holder = m
	s.Pos[m] = s.Acked + 1
	s.Assigns++
	return c.memberLabel("Assign", m), ok, s
}

// Load: a member reads the lease row, its winners only once it is draining.
func (c Config) Load(s State, m int8) (string, bool, State) {
	ok := m == s.Holder && !s.Mem[m].Loaded
	if ok {
		mem := Mem{
			Loaded: true, Ver: s.Ver, Prog: s.Prog, Alloc: s.Alloc, Holds: s.Holds,
			WL: s.St != Open, Osum: s.Osum, S: s.S,
		}
		if s.St == Open {
			for a := range c.Auths {
				mem.Win[a] = NoWin
			}
		} else {
			mem.Win = s.Win
		}
		s.Mem[m] = mem
		s.Dpos[m] = 1
	}
	return c.memberLabel("Load", m), ok, s
}

// LoadWinners: a member that loaded an open lease which has since drained
// loads its winners, beside the ones it decided itself.
func (c Config) LoadWinners(s State, m int8) (string, bool, State) {
	ok := s.St != Open && s.Mem[m].Loaded && !s.Mem[m].WL
	if ok {
		for a := range c.Auths {
			if s.Mem[m].Win[a] == NoWin {
				s.Mem[m].Win[a] = s.Win[a]
			}
		}
		s.Mem[m].WL = true
	}
	return c.memberLabel("LoadWinners", m), ok, s
}

// ApplyRecord applies the next owner record, the one after its progress.
func (c Config) ApplyRecord(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && mem.Loaded && !s.Gap && s.Pos[m] <= s.LogLen && (s.St == Open || mem.WL) &&
		mem.S == NoS && s.log(s.Pos[m]).Seq == mem.Prog+1
	if ok {
		s.Mem[m] = AppliedTo(mem, s.log(s.Pos[m]))
		s.Pos[m]++
	}
	return c.memberLabel("ApplyRecord", m), ok, s
}

// SkipRecord: a redelivery, a duplicate, or once the member knows S, any
// owner record.
func (c Config) SkipRecord(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && mem.Loaded && !s.Gap && s.Pos[m] <= s.LogLen && s.log(s.Pos[m]).K != KTick &&
		(s.log(s.Pos[m]).Seq <= mem.Prog || mem.S != NoS)
	if ok {
		s.Pos[m]++
	}
	return c.memberLabel("SkipRecord", m), ok, s
}

// Gap: a record beyond the next sequence number stops the lease, declared with
// the commit version the member read and with nothing uncommitted.
func (c Config) Gap(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && mem.Loaded && !s.Gap && s.Pos[m] <= s.LogLen && mem.S == NoS &&
		s.log(s.Pos[m]).Seq > mem.Prog+1 && !mem.Dirty && s.Ver == mem.Ver
	s.Gap = true
	return c.memberLabel("Gap", m), ok, s
}

// ApplyTick: the fence tick. S is the highest owner sequence number applied
// before it, unless the member loaded a stored S.
func (c Config) ApplyTick(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && mem.Loaded && !s.Gap && s.Pos[m] <= s.LogLen && s.log(s.Pos[m]).K == KTick
	if ok {
		if mem.S == NoS {
			s.Mem[m].S = mem.Prog
			s.Mem[m].Dirty = true
		}
		s.Pos[m]++
	}
	return c.memberLabel("ApplyTick", m), ok, s
}

// ApplyRow: once S is stored, the member books the drain log in order.
func (c Config) ApplyRow(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && mem.Loaded && mem.WL && !s.Gap && s.St == Draining && s.S != NoS &&
		s.Pos[m] == s.LogLen+1 && s.Dpos[m] <= s.DrainLen
	if ok {
		s.Mem[m] = RowAppliedTo(mem, s.drain(s.Dpos[m]))
		s.Dpos[m]++
	}
	return c.memberLabel("ApplyRow", m), ok, s
}

// Reap reaps an open hold at its latest snapshot, conditional on the commit
// version, after reading the hold's rows.
func (c Config) Reap(s State, m, a int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && mem.Loaded && mem.WL && !s.Gap && s.St == Draining && s.S != NoS &&
		s.Pos[m] == s.LogLen+1 && mem.Holds[a] != NoHold && mem.Win[a] == NoWin && s.Ver == mem.Ver
	for i := int8(1); ok && i <= s.DrainLen; i++ {
		if s.drain(i).A == a {
			ok = false
		}
	}
	if ok {
		s.appendDrain(Rec{KReap, a, mem.Holds[a], 0, s.DrainLen + 1})
	}
	return fmt.Sprintf("Reap(%s,%s)", c.Members[m], c.Auths[a]), ok, s
}

// Commit is the per-lease commit, conditional on the commit version, with the
// bookings as arithmetic on the row.
func (c Config) Commit(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := mem.Loaded && mem.Dirty && !s.Gap && s.Ver == mem.Ver
	if ok {
		s.Ver++
		s.Prog = mem.Prog
		s.Booked += mem.Dbooked
		s.Alloc -= mem.Dbooked
		s.Holds = mem.Holds
		for a := range c.Auths {
			if mem.Win[a] != NoWin {
				s.Win[a] = mem.Win[a]
			}
		}
		s.Osum = mem.Osum
		s.S = mem.S
		s.Alert = s.Alert || mem.Fault
		s.Mem[m].Ver = s.Ver
		s.Mem[m].Dbooked = 0
		s.Mem[m].Dirty = false
		s.Mem[m].Fault = false
		s.Done[m] = s.Pos[m] - 1
	}
	return c.memberLabel("Commit", m), ok, s
}

// Reread: another member committed since this one read the row; it drops what
// it applied and re-reads.
func (c Config) Reread(s State, m int8) (string, bool, State) {
	ok := s.Mem[m].Loaded && s.Ver != s.Mem[m].Ver
	s.Mem[m] = c.Blank()
	s.Pos[m] = s.Acked + 1
	s.Dpos[m] = 1
	return c.memberLabel("Reread", m), ok, s
}

// Ack: only what a commit made durable is acknowledged.
func (c Config) Ack(s State, m int8) (string, bool, State) {
	ok := s.Done[m] > s.Acked
	s.Acked = s.Done[m]
	return c.memberLabel("Ack", m), ok, s
}

// Crash: a member loses its memory.
func (c Config) Crash(s State, m int8) (string, bool, State) {
	ok := int(s.Crashes) < c.MaxCrash && s.Mem[m].Loaded
	s.Mem[m] = c.Blank()
	s.Pos[m] = s.Acked + 1
	s.Dpos[m] = 1
	s.Crashes++
	return c.memberLabel("Crash", m), ok, s
}

// Close: after the fence, with every record and row applied and committed, no
// open hold left, conditional on the commit version.
func (c Config) Close(s State, m int8) (string, bool, State) {
	mem := s.Mem[m]
	ok := m == s.Holder && s.St == Draining && mem.Loaded && mem.WL && !s.Gap && mem.S != NoS &&
		s.Pos[m] == s.LogLen+1 && s.Dpos[m] == s.DrainLen+1 && !mem.Dirty && s.Ver == mem.Ver
	for a := range c.Auths {
		if mem.Holds[a] != NoHold {
			ok = false
		}
	}
	if ok {
		s.St = Closed
		s.Ver++
		s.Mem[m].Ver = s.Ver
	}
	return c.memberLabel("Close", m), ok, s
}

// --- Claims

// TickAt is the index of the fence tick in the log, or 0.
func (s *State) TickAt() int8 {
	for i := int8(1); i <= s.LogLen; i++ {
		if s.log(i).K == KTick {
			return i
		}
	}
	return 0
}

// Bound is the highest owner sequence number up to which every record was
// received before the fence tick; before the tick, MaxSeq.
func (c Config) Bound(s *State) int8 {
	if !s.Ticked {
		return int8(c.MaxSeq)
	}
	tick := s.TickAt()
	before := map[int8]bool{}
	for i := int8(1); i < tick; i++ {
		if r := s.log(i); r.K != KTick {
			before[r.Seq] = true
		}
	}
	k := int8(0)
	for int(k) < c.MaxSeq && before[k+1] {
		k++
	}
	return k
}

// logRange is the distinct records of the log: Range(log).
func (s *State) logRange() []Rec {
	var out []Rec
	seen := map[Rec]bool{}
	for i := int8(1); i <= s.LogLen; i++ {
		if r := s.log(i); !seen[r] {
			seen[r] = true
			out = append(out, r)
		}
	}
	return out
}

func (c Config) ownerTerms(s *State, a int8) []Rec {
	var out []Rec
	bound := c.Bound(s)
	for _, r := range s.logRange() {
		if OwnerTerminal(r) && r.A == a && r.Seq <= bound {
			out = append(out, r)
		}
	}
	return out
}

func (c Config) heartbeats(s *State, a int8) []Rec {
	var out []Rec
	bound := c.Bound(s)
	for _, r := range s.logRange() {
		if r.K == KHb && r.A == a && r.Seq <= bound {
			out = append(out, r)
		}
	}
	return out
}

func (s *State) firstRow(a int8) int8 {
	for i := int8(1); i <= s.DrainLen; i++ {
		if s.drain(i).A == a {
			return i
		}
	}
	return 0
}

// Invariant is a named predicate on a state.
type Invariant struct {
	Name  string
	Holds func(State) bool
}

// Invariants are the spec's, in its .cfg's order.
func (c Config) Invariants() []Invariant {
	return []Invariant{
		{"TypeOK", c.TypeOK},
		{"WinnerIsFirst", c.WinnerIsFirst},
		{"BookedIsWinners", c.BookedIsWinners},
		{"NoRaiseLost", c.NoRaiseLost},
		{"ReapAtLastSnapshot", c.ReapAtLastSnapshot},
		{"NoChargeLost", c.NoChargeLost},
		{"BoundaryIsS", c.BoundaryIsS},
		{"StoredSFirst", c.StoredSFirst},
		{"GapIsReal", c.GapIsReal},
	}
}

func in(v int8, lo, hi int) bool { return int(v) >= lo && int(v) <= hi }

func (c Config) recTyped(r Rec) bool {
	maxC := SettleCharge*len(c.Auths) + c.MaxSnap + 1
	return in(r.K, int(KNone), int(KTick)) && in(r.A, NoAuth, len(c.Auths)-1) && in(r.C, 0, maxC) &&
		in(r.Seq, 0, c.MaxSeq) && in(r.Idx, 0, c.MaxAppend+len(c.Auths))
}

// MaxVer bounds the commit version.
func (c Config) MaxVer() int {
	return (c.MaxSeq+c.MaxDup+1+c.MaxAppend+len(c.Auths))*(2+2*c.MaxAssign+c.MaxCrash) + 1
}

// TypeOK is the spec's TypeOK.
func (c Config) TypeOK(s State) bool {
	n, members := len(c.Auths), len(c.Members)
	for i := int8(1); i <= s.OutLen; i++ {
		if !c.recTyped(s.Out[i-1]) {
			return false
		}
	}
	for i := int8(1); i <= s.LogLen; i++ {
		if !c.recTyped(s.log(i)) {
			return false
		}
	}
	for i := int8(1); i <= s.DrainLen; i++ {
		if !c.recTyped(s.drain(i)) {
			return false
		}
	}
	if !in(s.OutLen, 0, c.MaxSeq) || !in(s.LogLen, 0, c.MaxSeq+c.MaxDup+1) || !in(s.Lates, 0, c.MaxLate) ||
		!in(s.DrainLen, 0, c.MaxAppend+n) || !in(s.NextSeq, 1, c.MaxSeq+1) || s.OwnerDone>>uint(n) != 0 ||
		!in(s.Dups, 0, c.MaxDup) || !in(s.Aheads, 0, c.MaxAhead) || !in(s.Raised, 0, c.MaxRaise) ||
		!in(s.Appends, 0, c.MaxAppend) || !in(s.Assigns, 0, c.MaxAssign) || !in(s.Crashes, 0, c.MaxCrash) ||
		!in(s.St, int(Open), int(Closed)) || !in(s.Ver, 0, c.MaxVer()) || !in(s.Prog, 0, c.MaxSeq) ||
		s.Booked < 0 || !(s.S == NoS || in(s.S, 0, c.MaxSeq)) || !in(s.Holder, 0, members-1) ||
		!in(s.Acked, 0, int(s.LogLen)) {
		return false
	}
	for a := range n {
		if !in(s.Osnap[a], 0, c.MaxSnap) || !(s.Holds[a] == NoHold || in(s.Holds[a], 0, c.MaxSnap)) ||
			!c.recTyped(s.Win[a]) {
			return false
		}
	}
	for m := range members {
		if !in(s.Pos[m], 1, int(s.LogLen)+1) || !in(s.Dpos[m], 1, int(s.DrainLen)+1) {
			return false
		}
	}
	return true
}

// BoundaryIsS: the stored S is the boundary the order of receipt defines.
func (c Config) BoundaryIsS(s State) bool { return s.S == NoS || s.S == c.Bound(&s) }

// StoredSFirst: S is stored before the drain log is reaped or booked.
func (c Config) StoredSFirst(s State) bool {
	if s.S != NoS {
		return true
	}
	for i := int8(1); i <= s.DrainLen; i++ {
		if s.drain(i).K == KReap {
			return false
		}
	}
	for a := range c.Auths {
		if s.Win[a].Idx > 0 {
			return false
		}
		for m := range c.Members {
			if s.Mem[m].Win[a].Idx > 0 {
				return false
			}
		}
	}
	return true
}

// StoredAhead: before the fence tick, the log stored an owner record before
// one the owner issued before it.
func (c Config) StoredAhead(s *State) bool {
	tick := s.TickAt()
	for i := int8(1); i <= s.LogLen; i++ {
		r := s.log(i)
		if (s.Ticked && i >= tick) || r.K == KTick {
			continue
		}
		for seq := int8(1); seq < r.Seq; seq++ {
			missing := true
			for k := int8(1); k < i; k++ {
				if s.log(k).Seq == seq {
					missing = false
					break
				}
			}
			if missing {
				return true
			}
		}
	}
	return false
}

// GapIsReal: a gap stops the lease only where the log has one.
func (c Config) GapIsReal(s State) bool { return !s.Gap || c.StoredAhead(&s) }

// FirstInOrder is the lease's order: the owner's terminal, else the drain
// log's first row, else none. It reports false where the spec's CHOOSE would
// pick among two different terminals, which the owner never issues.
func (c Config) FirstInOrder(s *State, a int8) (Rec, bool) {
	terms := c.ownerTerms(s, a)
	switch {
	case len(terms) > 1:
		return Rec{}, false
	case len(terms) == 1:
		return terms[0], true
	}
	if i := s.firstRow(a); i != 0 {
		return s.drain(i), true
	}
	return NoWin, true
}

// WinnerIsFirst: an authorization's stored winner is the first terminal in
// the lease's order.
func (c Config) WinnerIsFirst(s State) bool {
	for a := range int8(len(c.Auths)) {
		if s.Win[a] == NoWin {
			continue
		}
		if first, ok := c.FirstInOrder(&s, a); !ok || s.Win[a] != first {
			return false
		}
	}
	return true
}

// BookedIsWinners: the booked consumption is the sum of the winners' charges.
func (c Config) BookedIsWinners(s State) bool {
	sum := 0
	for a := range c.Auths {
		sum += int(s.Win[a].C)
	}
	return int(s.Booked) == sum
}

// NoRaiseLost: the allocation is the grant, plus every raise, less what is
// booked.
func (c Config) NoRaiseLost(s State) bool {
	return int(s.Alloc) == c.Grant+int(s.Raised)-int(s.Booked)
}

// LastSnap is the charge of a's latest accepted heartbeat, or 0.
func (c Config) LastSnap(s *State, a int8) int8 {
	var last *Rec
	for _, r := range c.heartbeats(s, a) {
		if last == nil || r.Seq > last.Seq {
			r := r
			last = &r
		}
	}
	if last == nil {
		return 0
	}
	return last.C
}

// ReapAtLastSnapshot: a reap charges its hold's latest snapshot in the log.
func (c Config) ReapAtLastSnapshot(s State) bool {
	for i := int8(1); i <= s.DrainLen; i++ {
		if r := s.drain(i); r.K == KReap && r.C != c.LastSnap(&s, r.A) {
			return false
		}
	}
	return true
}

// NoChargeLost: a closed lease has a winner for every authorization the log or
// the drain log showed.
func (c Config) NoChargeLost(s State) bool {
	if s.St != Closed {
		return true
	}
	for a := range int8(len(c.Auths)) {
		shown := len(c.ownerTerms(&s, a)) > 0 || s.firstRow(a) != 0 || len(c.heartbeats(&s, a)) > 0
		if shown && s.Win[a] == NoWin {
			return false
		}
	}
	return true
}

// badCheckpoint: a checkpoint whose `consumed` differs from the sum of the
// owner's terminals in the log with lower sequence numbers, each record once.
func (c Config) badCheckpoint(s *State, r Rec) bool {
	if r.K != KCkpt {
		return false
	}
	sum := 0
	for _, q := range s.logRange() {
		if OwnerTerminal(q) && q.Seq < r.Seq {
			sum += int(q.C)
		}
	}
	return int(r.C) != sum
}

// AuditsEachCheckpoint is the spec's step property: a commit that stores a
// wrong checkpoint at or below S raises the alert, and nothing else raises
// it. A step that changes nothing passes, as [A]_vars does.
func (c Config) AuditsEachCheckpoint(from, to State) bool {
	if from == to {
		return true
	}
	if to.Prog != from.Prog {
		want := from.Alert
		bound := c.Bound(&from)
		for _, r := range from.logRange() {
			if r.K == KCkpt && from.Prog < r.Seq && r.Seq <= to.Prog && r.Seq <= bound && c.badCheckpoint(&from, r) {
				want = true
			}
		}
		if to.Alert != want {
			return false
		}
	}
	return to.Alert == from.Alert || to.Prog != from.Prog
}
