// Package terminalorder is the shadow of proofs/TerminalOrder.tla: one lease's
// records, who decides each authorization's terminal, and why the live
// auditor and a rebuild from the archive never disagree about it.
//
// The rule: for each authorization the first terminal in the lease's order
// wins, where the order is the owner's records by sequence number up to a
// stored boundary S, then the lease's drain log.
//
// Each action of the spec is a function here under the spec's name, and each
// invariant a method. Next gives every step the spec allows, labeled as TLC
// labels it, so the tests can hold the two side by side. Authorizations are
// numbered by their place in Config.Auths; records and drain rows count from
// 1, as the spec's sequences do.
package terminalorder

import "fmt"

// Bounds of a State, so that it is a small comparable value: an
// authorization's records are at most its terminal, its heartbeat and its
// listing.
const (
	MaxAuths  = 3
	MaxOutbox = 3 * MaxAuths
	MaxDrain  = 8
)

// Config is the spec's constants. Auths are the model values' names, in the
// order the tests use; Stream and Declared say which of them are streams and
// which streams' boots declared the stream-open heartbeat, and Listable which
// a forced exit may list.
type Config struct {
	Auths      []string
	Stream     []bool
	Declared   []bool
	Listable   []bool
	MaxAppends int
}

// Validate reports a configuration the spec's ASSUME refuses, or this package
// cannot hold.
func (c Config) Validate() error {
	n := len(c.Auths)
	switch {
	case n > MaxAuths:
		return fmt.Errorf("%d authorizations, more than %d", n, MaxAuths)
	case len(c.Stream) != n || len(c.Declared) != n || len(c.Listable) != n:
		return fmt.Errorf("Stream, Declared and Listable must say something of each authorization")
	case c.MaxAppends < 0 || c.MaxAppends > MaxDrain-n:
		return fmt.Errorf("MaxAppends %d and %d authorizations make more drain rows than %d", c.MaxAppends, n, MaxDrain)
	}
	names := map[string]bool{}
	for _, name := range c.Auths {
		if names[name] {
			return fmt.Errorf("%s is named twice: a set holds it once", name)
		}
		names[name] = true
	}
	for a := range n {
		if c.Declared[a] && !c.Stream[a] {
			return fmt.Errorf("%s is declared and is no stream", c.Auths[a])
		}
	}
	return nil
}

// MaxSeq is the most records the owner can issue: one terminal per
// authorization, one heartbeat per stream and one listing per hold a forced
// exit may list.
func (c Config) MaxSeq() int {
	n := len(c.Auths)
	for a := range c.Auths {
		if c.Stream[a] {
			n++
		}
		if c.Listable[a] {
			n++
		}
	}
	return n
}

// MaxRows is the most drain rows: the front doors' appends and one auditor
// reap per authorization.
func (c Config) MaxRows() int { return c.MaxAppends + len(c.Auths) }

// NoTick and NoS are the spec's markers for no tick and no stored boundary.
func (c Config) NoTick() int8 { return int8(c.MaxSeq() + 1) }
func (c Config) NoS() int8    { return int8(c.MaxSeq() + 1) }

// Record and row kinds.
const (
	Heartbeat int8 = iota + 1
	Settle
	Reap
	Release
	Refund
	List
)

var kindNames = map[int8]string{
	Heartbeat: "hb", Settle: "settle", Reap: "reap", Release: "release", Refund: "refund", List: "list",
}

func terminal(kind int8) bool {
	return kind == Settle || kind == Refund || kind == Reap || kind == Release
}

// Lease states.
const (
	Open int8 = iota
	Draining
	Closed
)

var leaseNames = map[int8]string{Open: "open", Draining: "draining", Closed: "closed"}

// Enclave states.
const (
	EncOpen int8 = iota
	EncDelivered
	EncGone
	EncPermitted
)

var encNames = map[int8]string{EncOpen: "open", EncDelivered: "delivered", EncGone: "gone", EncPermitted: "permitted"}

// Winner sources.
const (
	None int8 = iota
	Owner
	Drain
)

var srcNames = map[int8]string{None: "none", Owner: "owner", Drain: "drain"}

// Rec is an owner record: its authorization, its kind, and the drain row it
// adopted, or 0.
type Rec struct {
	Auth, Kind, Row int8
}

// Row is a drain-log row.
type Row struct {
	Auth, Kind int8
}

// Winner is a stored winner: none, or an owner record or a drain row by its
// index.
type Winner struct {
	Src, Idx int8
}

// NoWinner is the spec's NoWinner.
var NoWinner = Winner{None, 0}

// State is the spec's variables. GwAckedOwner has bit i-1 set when owner
// record i is in gwAcked, and GwAckedDrain bit j-1 for drain row j; Got[a]
// is a's membership of got.
type State struct {
	Lease          int8
	OwnerUp        bool
	OwnerCutoff    bool
	IssuedAtCutoff int8
	DeadlinePassed bool
	Outbox         [MaxOutbox]Rec
	OutboxLen      int8
	Delivered      int8
	Acked          int8
	TickAt         int8
	S              int8
	Drain          [MaxDrain]Row
	DrainLen       int8
	Appends        int8
	OwnerApplied   int8
	DrainApplied   int8
	Winner         [MaxAuths]Winner
	OwnerWinner    [MaxAuths]int8
	GwAckedOwner   uint16
	GwAckedDrain   uint16
	Enc            [MaxAuths]int8
	Allowance      [MaxAuths]bool
	Got            [MaxAuths]bool
}

// Init is the spec's Init: every authorization admitted, nothing published.
func (c Config) Init() State {
	return State{
		Lease:   Open,
		OwnerUp: true,
		TickAt:  c.NoTick(),
		S:       c.NoS(),
	}
}

// Transition is one step: the action TLC would label it with, and the state
// it leads to.
type Transition struct {
	Action string
	To     State
}

// --- Helpers

// Undefined is what a function here panics with where TLC would stop because
// an expression has no value: an index outside a sequence, the least element
// of an empty set. No state the spec reaches does that.
type Undefined struct{ What string }

func (u Undefined) Error() string { return "no value: " + u.What }

func (s *State) outbox(i int8) Rec {
	if i < 1 || i > s.OutboxLen {
		panic(Undefined{fmt.Sprintf("outbox[%d] of %d records", i, s.OutboxLen)})
	}
	return s.Outbox[i-1]
}

func (s *State) drain(j int8) Row {
	if j < 1 || j > s.DrainLen {
		panic(Undefined{fmt.Sprintf("drain[%d] of %d rows", j, s.DrainLen)})
	}
	return s.Drain[j-1]
}

// HbIssued: the owner has issued a heartbeat record for a.
func (s *State) HbIssued(a int8) bool { return s.recIn(a, Heartbeat, s.OutboxLen) }

// HbAcked: a heartbeat for a is among the records acked to the owner.
func (s *State) HbAcked(a int8) bool { return s.recIn(a, Heartbeat, s.Acked) }

// recIn: one of outbox[1..n] is a record of kind for a.
func (s *State) recIn(a, kind, n int8) bool {
	for i := int8(1); i <= n; i++ {
		if r := s.outbox(i); r.Auth == a && r.Kind == kind {
			return true
		}
	}
	return false
}

// HbDurable: a heartbeat for a that the stored boundary covers.
func (c Config) HbDurable(s *State, a int8) bool {
	return s.S != c.NoS() && s.recIn(a, Heartbeat, s.S)
}

// Listed: a forced exit's listing of a that the stored boundary covers; the
// auditor knows the hold without a heartbeat.
func (c Config) Listed(s *State, a int8) bool {
	return s.S != c.NoS() && s.recIn(a, List, s.S)
}

// ListIssued: the owner has issued a listing of a.
func (s *State) ListIssued(a int8) bool { return s.recIn(a, List, s.OutboxLen) }

// ListAcked: a listing of a is among the records acked to the owner.
func (s *State) ListAcked(a int8) bool { return s.recIn(a, List, s.Acked) }

// Known: the auditor knows the hold, a heartbeat or a listing being durable.
func (c Config) Known(s *State, a int8) bool { return c.HbDurable(s, a) || c.Listed(s, a) }

// firstOwnerTerm is Min(OwnerTerms(a, n)), or 0 when there is none. OwnerTerms
// is a set built from all of outbox[1..n], so it has no value when n passes
// the outbox's end, whatever comes first.
func (s *State) firstOwnerTerm(a, n int8) int8 {
	if n > s.OutboxLen {
		panic(Undefined{fmt.Sprintf("OwnerTerms over outbox[1..%d] of %d records", n, s.OutboxLen)})
	}
	for i := int8(1); i <= n; i++ {
		if r := s.outbox(i); r.Auth == a && terminal(r.Kind) {
			return i
		}
	}
	return 0
}

// firstDrainRow is Min(DrainRows(a)), or 0 when there is none.
func (s *State) firstDrainRow(a int8) int8 {
	for j := int8(1); j <= s.DrainLen; j++ {
		if s.drain(j).Auth == a {
			return j
		}
	}
	return 0
}

// Canon is the lease's order as a function of durable state: owner records up
// to n by sequence number, then the drain log.
func (s *State) Canon(a, n int8) Winner {
	if i := s.firstOwnerTerm(a, n); i != 0 {
		return Winner{Owner, i}
	}
	if j := s.firstDrainRow(a); j != 0 {
		return Winner{Drain, j}
	}
	return NoWinner
}

// OwnerActive: the owner works until its cutoff.
func (s *State) OwnerActive() bool { return s.OwnerUp && !s.OwnerCutoff }

// Settles: an enclave sends a settle only once it has delivered something,
// and Refunds: a refund only once it gave up having delivered nothing (A4).
func (s *State) Settles(a int8) bool { return s.Enc[a] == EncDelivered }
func (s *State) Refunds(a int8) bool { return s.Enc[a] == EncGone }

// IssuedBeforeCutoff: the fence deadline bounds only what was issued before
// the cutoff.
func (s *State) IssuedBeforeCutoff(i int8) bool { return !s.OwnerCutoff || i <= s.IssuedAtCutoff }

func (s *State) appendOutbox(r Rec) {
	s.Outbox[s.OutboxLen] = r
	s.OutboxLen++
}

func (s *State) appendDrain(r Row) {
	s.Drain[s.DrainLen] = r
	s.DrainLen++
}

// Next is every step the spec's Next allows from s.
func (c Config) Next(s State) []Transition {
	var out []Transition
	add := func(action string, ok bool, to State) {
		if ok {
			out = append(out, Transition{action, to})
		}
	}
	each := func(f func(State, int8) (string, bool, State)) {
		for a := range int8(len(c.Auths)) {
			add(f(s, a))
		}
	}
	each(c.OwnerHeartbeat)
	each(c.OwnerSettle)
	each(c.OwnerRefund)
	each(c.OwnerList)
	each(c.OwnerReap)
	each(c.OwnerRelease)
	each(c.OwnerAdopt)
	add(c.OwnerCrash(s))
	add(c.Deliver(s))
	add(c.Ack(s))
	each(c.Answer)
	add(c.CutoffPass(s))
	add(c.DeadlinePass(s))
	each(c.AllowanceElapse)
	each(c.EnclaveDeliver)
	each(c.EnclaveGiveUp)
	each(c.FrontDoorAppend)
	add(c.MarkDraining(s))
	add(c.AuditorApplyOwner(s))
	add(c.PublishTick(s))
	add(c.StoreS(s))
	for _, to := range c.RebuildStoreS(s) {
		out = append(out, Transition{"RebuildStoreS", to})
	}
	add(c.ApplyDrain(s))
	each(c.AuditorReap)
	add(c.Close(s))
	return out
}

func (c Config) label(action string, a int8) string { return fmt.Sprintf("%s(%s)", action, c.Auths[a]) }

// --- The owner

// OwnerHeartbeat: a stream's first heartbeat reaches the owner, which issues
// its record, unless it has already decided the hold.
func (c Config) OwnerHeartbeat(s State, a int8) (string, bool, State) {
	ok := s.OwnerActive() && c.Stream[a] && s.OwnerWinner[a] == 0 && !s.HbIssued(a)
	if ok {
		s.appendOutbox(Rec{a, Heartbeat, 0})
	}
	return c.label("OwnerHeartbeat", a), ok, s
}

// decide issues the owner's one terminal for a and remembers it.
func (s *State) decide(r Rec) {
	s.appendOutbox(r)
	s.OwnerWinner[r.Auth] = s.OutboxLen
}

// OwnerSettle: a settle reaches the owner, which decides under the
// authorization's lock and publishes only that winner.
func (c Config) OwnerSettle(s State, a int8) (string, bool, State) {
	ok := s.OwnerActive() && s.Settles(a) && s.OwnerWinner[a] == 0
	if ok {
		s.decide(Rec{a, Settle, 0})
	}
	return c.label("OwnerSettle", a), ok, s
}

// OwnerRefund: a refund reaches the owner, from an enclave that gave up having
// delivered nothing, and it decides it as it decides a settle.
func (c Config) OwnerRefund(s State, a int8) (string, bool, State) {
	ok := s.OwnerActive() && s.Refunds(a) && s.OwnerWinner[a] == 0
	if ok {
		s.decide(Rec{a, Refund, 0})
	}
	return c.label("OwnerRefund", a), ok, s
}

// OwnerList: a forced exit lists a hold it has not decided in its hand-off,
// at most once.
func (c Config) OwnerList(s State, a int8) (string, bool, State) {
	ok := s.OwnerActive() && c.Listable[a] && s.OwnerWinner[a] == 0 && !s.ListIssued(a)
	if ok {
		s.appendOutbox(Rec{a, List, 0})
	}
	return c.label("OwnerList", a), ok, s
}

// OwnerReap: the owner's reaper may reap any undecided hold.
func (c Config) OwnerReap(s State, a int8) (string, bool, State) {
	ok := s.OwnerActive() && s.OwnerWinner[a] == 0
	if ok {
		s.decide(Rec{a, Reap, 0})
	}
	return c.label("OwnerReap", a), ok, s
}

// OwnerRelease releases a declared stream's hold before its first heartbeat,
// only if no heartbeat for it was even issued.
func (c Config) OwnerRelease(s State, a int8) (string, bool, State) {
	ok := s.OwnerActive() && c.Declared[a] && s.Allowance[a] && !s.HbIssued(a) && s.OwnerWinner[a] == 0
	if ok {
		s.decide(Rec{a, Release, 0})
	}
	return c.label("OwnerRelease", a), ok, s
}

// OwnerAdopt: at a renewal the owner takes the first drain row of an
// undecided hold and publishes it as its own record, carrying the row.
func (c Config) OwnerAdopt(s State, a int8) (string, bool, State) {
	j := s.firstDrainRow(a)
	ok := s.OwnerActive() && s.OwnerWinner[a] == 0 && j != 0
	if ok {
		s.decide(Rec{a, s.drain(j).Kind, j})
	}
	return c.label("OwnerAdopt", a), ok, s
}

// OwnerCrash: the owner process dies.
func (c Config) OwnerCrash(s State) (string, bool, State) {
	ok := s.OwnerUp
	s.OwnerUp = false
	return "OwnerCrash", ok, s
}

// --- The log

// Deliver: the log receives the next owner record, at any time.
func (c Config) Deliver(s State) (string, bool, State) {
	ok := s.Delivered < s.OutboxLen
	s.Delivered++
	return "Deliver", ok, s
}

// Ack: the owner learns a publish was stored, and answers. An acked terminal's
// outcome may reach a gateway; an acked first heartbeat lets the owner answer
// it accepted, which the gateway may or may not receive (Answer).
func (c Config) Ack(s State) (string, bool, State) {
	ok := s.Acked < s.Delivered && s.OwnerUp && !(s.DeadlinePassed && s.IssuedBeforeCutoff(s.Acked+1))
	if ok {
		if r := s.outbox(s.Acked + 1); terminal(r.Kind) {
			s.GwAckedOwner |= 1 << s.Acked
		}
		s.Acked++
	}
	return "Ack", ok, s
}

// Answer: a stream's gateway receives an accepted answer to its first
// heartbeat, given once the heartbeat's record is acknowledged, and the
// stream may deliver.
func (c Config) Answer(s State, a int8) (string, bool, State) {
	ok := s.Enc[a] == EncOpen && s.HbAcked(a)
	s.Enc[a] = EncPermitted
	return c.label("Answer", a), ok, s
}

// --- Time

// CutoffPass: the owner's cutoff passes.
func (c Config) CutoffPass(s State) (string, bool, State) {
	ok := !s.OwnerCutoff
	s.OwnerCutoff = true
	s.IssuedAtCutoff = s.OutboxLen
	return "CutoffPass", ok, s
}

// DeadlinePass: the publish deadline of everything issued before the cutoff
// passes.
func (c Config) DeadlinePass(s State) (string, bool, State) {
	ok := s.OwnerCutoff && !s.DeadlinePassed
	s.DeadlinePassed = true
	return "DeadlinePass", ok, s
}

// AllowanceElapse: a hold's first-heartbeat allowance elapses, for a declared
// boot only once its heartbeat reached the owner or the enclave gave up (A3).
func (c Config) AllowanceElapse(s State, a int8) (string, bool, State) {
	ok := !s.Allowance[a] && (!c.Declared[a] || s.Enc[a] != EncOpen || s.HbIssued(a))
	s.Allowance[a] = true
	return c.label("AllowanceElapse", a), ok, s
}

// EnclaveDeliver: the client gets its first byte, a non-streaming request's
// provider having answered, or a stream's gateway having been permitted (A4).
func (c Config) EnclaveDeliver(s State, a int8) (string, bool, State) {
	from := EncOpen
	if c.Stream[a] {
		from = EncPermitted
	}
	ok := s.Enc[a] == from
	s.Enc[a] = EncDelivered
	s.Got[a] = true
	return c.label("EnclaveDeliver", a), ok, s
}

// EnclaveGiveUp: the enclave gives up with nothing delivered, permitted or
// not.
func (c Config) EnclaveGiveUp(s State, a int8) (string, bool, State) {
	ok := s.Enc[a] == EncOpen || s.Enc[a] == EncPermitted
	s.Enc[a] = EncGone
	return c.label("EnclaveGiveUp", a), ok, s
}

// --- Front doors

// FrontDoorAppend: a front door appends a settle or a refund the owner did not
// take, conditional on the lease not being closed, and answers "recorded".
func (c Config) FrontDoorAppend(s State, a int8) (string, bool, State) {
	ok := s.Lease != Closed && (s.Settles(a) || s.Refunds(a)) && int(s.Appends) < c.MaxAppends
	if ok {
		kind := Refund
		if s.Settles(a) {
			kind = Settle
		}
		s.appendDrain(Row{a, kind})
		s.Appends++
		s.GwAckedDrain |= 1 << (s.DrainLen - 1)
	}
	return c.label("FrontDoorAppend", a), ok, s
}

// --- The auditor and the rebuild

// MarkDraining: the lease is marked draining.
func (c Config) MarkDraining(s State) (string, bool, State) {
	ok := s.Lease == Open
	s.Lease = Draining
	return "MarkDraining", ok, s
}

// AuditorApplyOwner applies the next owner record the log received before the
// tick. The first terminal for an authorization becomes its stored winner.
func (c Config) AuditorApplyOwner(s State) (string, bool, State) {
	ok := s.S == c.NoS() && s.OwnerApplied < s.Delivered && s.OwnerApplied < s.TickAt
	if ok {
		i := s.OwnerApplied + 1
		if r := s.outbox(i); terminal(r.Kind) && s.Winner[r.Auth] == NoWinner {
			s.Winner[r.Auth] = Winner{Owner, i}
		}
		s.OwnerApplied++
	}
	return "AuditorApplyOwner", ok, s
}

// PublishTick: a tick, once the lease drains and every owner publish has
// passed its deadline. It is received after every record delivered so far.
func (c Config) PublishTick(s State) (string, bool, State) {
	ok := s.Lease == Draining && s.DeadlinePassed
	s.TickAt = s.Delivered
	return "PublishTick", ok, s
}

// StoreS: the auditor stores S when it applies the tick.
func (c Config) StoreS(s State) (string, bool, State) {
	ok := s.TickAt != c.NoTick() && s.S == c.NoS() && s.OwnerApplied == s.TickAt
	s.S = s.TickAt
	return "StoreS", ok, s
}

// RebuildStoreS: a rebuild finds S unset, books every record its archive
// holds, at least those received before the tick (A2), and stores S, never
// re-deciding a stored winner. One successor for each boundary it may store.
func (c Config) RebuildStoreS(s State) []State {
	if s.TickAt == c.NoTick() || s.S != c.NoS() {
		return nil
	}
	var out []State
	for b := s.TickAt; b <= s.Delivered; b++ {
		next := s
		next.S = b
		next.OwnerApplied = b
		for a := range int8(len(c.Auths)) {
			if next.Winner[a] != NoWinner {
				continue
			}
			if i := s.firstOwnerTerm(a, b); i != 0 {
				next.Winner[a] = Winner{Owner, i}
			}
		}
		out = append(out, next)
	}
	return out
}

// ApplyDrain decides the next drain row, once S is stored.
func (c Config) ApplyDrain(s State) (string, bool, State) {
	ok := s.S != c.NoS() && s.DrainApplied < s.DrainLen
	if ok {
		j := s.DrainApplied + 1
		if a := s.drain(j).Auth; s.Winner[a] == NoWinner {
			s.Winner[a] = Winner{Drain, j}
		}
		s.DrainApplied++
	}
	return "ApplyDrain", ok, s
}

// AuditorReap reaps a hold the log showed, through a durable heartbeat or
// listing, that has no terminal, by inserting a reap row once its transaction
// finds no row of the hold's: rows of other holds may wait.
func (c Config) AuditorReap(s State, a int8) (string, bool, State) {
	ok := s.Lease == Draining && s.S != c.NoS() && s.firstDrainRow(a) == 0 &&
		c.Known(&s, a) && s.Winner[a] == NoWinner
	if ok {
		s.appendDrain(Row{a, Reap})
	}
	return c.label("AuditorReap", a), ok, s
}

// Close: the drain log is read to its end, and every hold the log showed has
// a terminal.
func (c Config) Close(s State) (string, bool, State) {
	ok := s.Lease == Draining && s.S != c.NoS() && s.DrainApplied == s.DrainLen
	for a := range int8(len(c.Auths)) {
		if ok && c.Known(&s, a) && s.Winner[a] == NoWinner {
			ok = false
		}
	}
	s.Lease = Closed
	return "Close", ok, s
}

// --- Invariants

// Invariant is a named predicate on a state.
type Invariant struct {
	Name  string
	Holds func(State) bool
}

// Invariants are the spec's, in its .cfg's order, NoListedHoldClosedOver
// where TerminalOrder.list.cfg has it.
func (c Config) Invariants() []Invariant {
	return []Invariant{
		{"TypeOK", c.TypeOK},
		{"CountsInOrder", c.CountsInOrder},
		{"WinnerIsFirstInOrder", c.WinnerIsFirstInOrder},
		{"AdoptionKeepsTheWinner", c.AdoptionKeepsTheWinner},
		{"AckedOwnerRecordWithinBoundary", c.AckedOwnerRecordWithinBoundary},
		{"AckedOwnerTerminalWins", c.AckedOwnerTerminalWins},
		{"NoAckedDrainRowLost", c.NoAckedDrainRowLost},
		{"NoStreamClosedOver", c.NoStreamClosedOver},
		{"NoDeliveredStreamClosedOver", c.NoDeliveredStreamClosedOver},
		{"NoListedHoldClosedOver", c.NoListedHoldClosedOver},
		{"DurableHeartbeatNeverReleased", c.DurableHeartbeatNeverReleased},
		{"NoLiveRequestReleased", c.NoLiveRequestReleased},
		{"ReleasedHoldOwesNothing", c.ReleasedHoldOwesNothing},
		{"RefundOnlyWhenNothingDelivered", c.RefundOnlyWhenNothingDelivered},
		{"ClosedLeaseHasNoUnappliedRow", c.ClosedLeaseHasNoUnappliedRow},
	}
}

func in(v int8, lo, hi int) bool { return int(v) >= lo && int(v) <= hi }

func (c Config) winnerTyped(w Winner) bool {
	return in(w.Src, int(None), int(Drain)) && in(w.Idx, 0, c.MaxSeq()+c.MaxRows())
}

// TypeOK is the spec's TypeOK.
func (c Config) TypeOK(s State) bool {
	n := len(c.Auths)
	maxSeq, maxRows := c.MaxSeq(), c.MaxRows()
	if !in(s.Lease, int(Open), int(Closed)) || !in(s.IssuedAtCutoff, 0, maxSeq) ||
		!in(s.OutboxLen, 0, maxSeq) || !in(s.Acked, 0, maxSeq) || !in(s.Delivered, 0, maxSeq) ||
		!in(s.TickAt, 0, maxSeq+1) || !in(s.S, 0, maxSeq+1) || !in(s.DrainLen, 0, maxRows) ||
		!in(s.Appends, 0, c.MaxAppends) || !in(s.OwnerApplied, 0, maxSeq) || !in(s.DrainApplied, 0, maxRows) {
		return false
	}
	for i := int8(1); i <= s.OutboxLen; i++ {
		r := s.outbox(i)
		if !in(r.Auth, 0, n-1) || !in(r.Kind, int(Heartbeat), int(List)) || !in(r.Row, 0, maxRows) {
			return false
		}
	}
	for j := int8(1); j <= s.DrainLen; j++ {
		if r := s.drain(j); !in(r.Auth, 0, n-1) || (r.Kind != Settle && r.Kind != Refund && r.Kind != Reap) {
			return false
		}
	}
	for a := range n {
		if !c.winnerTyped(s.Winner[a]) || !in(s.OwnerWinner[a], 0, maxSeq) || !in(s.Enc[a], int(EncOpen), int(EncPermitted)) {
			return false
		}
	}
	// gwAcked holds only owner and drain entries in this representation, so
	// each is of WinnerType when its index is.
	return s.GwAckedOwner>>uint(maxSeq+maxRows) == 0 && s.GwAckedDrain>>uint(maxSeq+maxRows) == 0
}

// CountsInOrder: the counts follow one another.
func (c Config) CountsInOrder(s State) bool {
	return s.Acked <= s.Delivered && s.Delivered <= s.OutboxLen && s.OwnerApplied <= s.Delivered &&
		s.DrainApplied <= s.DrainLen && (s.S == c.NoS() || s.S <= s.Delivered)
}

// WinnerIsFirstInOrder: every stored winner is the first terminal in the
// lease's order, a function of durable state alone (Invariant 3).
func (c Config) WinnerIsFirstInOrder(s State) bool {
	for a := range int8(len(c.Auths)) {
		w := s.Winner[a]
		if w == NoWinner {
			continue
		}
		if s.S != c.NoS() {
			if w != s.Canon(a, s.S) {
				return false
			}
			continue
		}
		i := s.firstOwnerTerm(a, s.OwnerApplied)
		if i == 0 || w != (Winner{Owner, i}) {
			return false
		}
	}
	return true
}

// AdoptionKeepsTheWinner: an adopted record that wins is the drain log's first
// row for its authorization.
func (c Config) AdoptionKeepsTheWinner(s State) bool {
	for a := range int8(len(c.Auths)) {
		w := s.Winner[a]
		if w.Src != Owner {
			continue
		}
		row := s.outbox(w.Idx).Row
		if row == 0 {
			continue
		}
		first := s.firstDrainRow(a)
		if first == 0 {
			panic(Undefined{fmt.Sprintf("Min(DrainRows(%d)), which is empty", a)})
		}
		if row != first {
			return false
		}
	}
	return true
}

func bits(x uint16) []int8 {
	var out []int8
	for i := int8(0); x != 0; i, x = i+1, x>>1 {
		if x&1 != 0 {
			out = append(out, i+1)
		}
	}
	return out
}

// AckedOwnerRecordWithinBoundary: a terminal the owner acknowledged is never
// beyond the stored boundary (Invariant 4, first half).
func (c Config) AckedOwnerRecordWithinBoundary(s State) bool {
	if s.S == c.NoS() {
		return true
	}
	for _, i := range bits(s.GwAckedOwner) {
		if i > s.S {
			return false
		}
	}
	return true
}

// AckedOwnerTerminalWins: a terminal the owner acknowledged is the lease's
// decision for its authorization, once applied or once S is stored.
func (c Config) AckedOwnerTerminalWins(s State) bool {
	for _, i := range bits(s.GwAckedOwner) {
		if i > s.OwnerApplied && s.S == c.NoS() {
			continue
		}
		if s.Winner[s.outbox(i).Auth] != (Winner{Owner, i}) {
			return false
		}
	}
	return true
}

// NoAckedDrainRowLost: once the lease is closed, every drain row a gateway was
// told is recorded has been read, and its authorization has a winner.
func (c Config) NoAckedDrainRowLost(s State) bool {
	if s.Lease != Closed {
		return true
	}
	for _, j := range bits(s.GwAckedDrain) {
		if j > s.DrainApplied || s.Winner[s.drain(j).Auth] == NoWinner {
			return false
		}
	}
	return true
}

// NoStreamClosedOver: a lease never closes over a stream whose first heartbeat
// was answered.
func (c Config) NoStreamClosedOver(s State) bool {
	if s.Lease != Closed {
		return true
	}
	for a := range int8(len(c.Auths)) {
		if c.Stream[a] && s.HbAcked(a) && s.Winner[a] == NoWinner {
			return false
		}
	}
	return true
}

// NoDeliveredStreamClosedOver: a lease never closes over a stream that
// delivered (A4, end to end).
func (c Config) NoDeliveredStreamClosedOver(s State) bool {
	if s.Lease != Closed {
		return true
	}
	for a := range int8(len(c.Auths)) {
		if c.Stream[a] && s.Got[a] && s.Winner[a] == NoWinner {
			return false
		}
	}
	return true
}

// NoListedHoldClosedOver: a lease never closes over a hold a forced exit
// listed and was told is stored.
func (c Config) NoListedHoldClosedOver(s State) bool {
	if s.Lease != Closed {
		return true
	}
	for a := range int8(len(c.Auths)) {
		if s.ListAcked(a) && s.Winner[a] == NoWinner {
			return false
		}
	}
	return true
}

// DurableHeartbeatNeverReleased: a hold with a heartbeat in the log is never
// released, by a release record or by closing the lease over it.
func (c Config) DurableHeartbeatNeverReleased(s State) bool {
	for i := int8(1); i <= s.Delivered; i++ {
		for j := int8(1); j <= s.Delivered; j++ {
			ri, rj := s.outbox(i), s.outbox(j)
			if ri.Auth == rj.Auth && ri.Kind == Heartbeat && rj.Kind == Release {
				return false
			}
		}
	}
	if s.Lease != Closed {
		return true
	}
	for a := range int8(len(c.Auths)) {
		if !c.HbDurable(&s, a) {
			continue
		}
		w := s.Winner[a]
		if w == NoWinner {
			return false
		}
		if w.Src == Owner && s.outbox(w.Idx).Kind == Release {
			return false
		}
	}
	return true
}

func (s *State) releasedByOwner(a int8) bool {
	i := s.OwnerWinner[a]
	return i != 0 && s.outbox(i).Kind == Release
}

// NoLiveRequestReleased: the enclave of a released hold has given up.
func (c Config) NoLiveRequestReleased(s State) bool {
	for a := range int8(len(c.Auths)) {
		if s.releasedByOwner(a) && s.Enc[a] != EncGone {
			return false
		}
	}
	return true
}

// ReleasedHoldOwesNothing: no settle exists for a released hold, in the
// owner's records or in the drain log.
func (c Config) ReleasedHoldOwesNothing(s State) bool {
	for a := range int8(len(c.Auths)) {
		if !s.releasedByOwner(a) {
			continue
		}
		for i := int8(1); i <= s.OutboxLen; i++ {
			if r := s.outbox(i); r.Auth == a && r.Kind == Settle {
				return false
			}
		}
		for j := int8(1); j <= s.DrainLen; j++ {
			if r := s.drain(j); r.Auth == a && r.Kind == Settle {
				return false
			}
		}
	}
	return true
}

// RefundOnlyWhenNothingDelivered: a refund, the owner's or the drain log's, is
// only for an enclave that delivered nothing, whatever it did after (A4).
func (c Config) RefundOnlyWhenNothingDelivered(s State) bool {
	for a := range int8(len(c.Auths)) {
		if !s.Got[a] {
			continue
		}
		if s.recIn(a, Refund, s.OutboxLen) {
			return false
		}
		for j := int8(1); j <= s.DrainLen; j++ {
			if r := s.drain(j); r.Auth == a && r.Kind == Refund {
				return false
			}
		}
	}
	return true
}

// ClosedLeaseHasNoUnappliedRow: a closed lease has no unapplied drain row.
func (c Config) ClosedLeaseHasNoUnappliedRow(s State) bool {
	return s.Lease != Closed || s.DrainApplied == s.DrainLen
}
