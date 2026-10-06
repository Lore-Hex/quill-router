---------------------------- MODULE TerminalOrder ----------------------------
(***************************************************************************)
(* One lease's records, from docs/design/fast-admission-and-batched-        *)
(* settlement.md sections 4.5 and 4.8: who decides each authorization's     *)
(* terminal, and why the live auditor and a rebuild from the archive can    *)
(* never disagree about it.                                                 *)
(*                                                                          *)
(* The rule being checked: for each authorization the FIRST terminal in the *)
(* lease's order wins, where the order is the owner's records by sequence   *)
(* number up to a stored boundary S, then the lease's drain log.            *)
(*                                                                          *)
(* This is written before the code. Nothing implements it yet.              *)
(*                                                                          *)
(* ACTORS                                                                   *)
(*                                                                          *)
(*   The owner. While the lease is open and its cutoff has not passed, it   *)
(*   is the only publisher of the lease's records. It publishes heartbeats  *)
(*   and at most one terminal per authorization, with consecutive sequence  *)
(*   numbers. A publish is ISSUED, later DELIVERED to the log (in sequence  *)
(*   order, because a failed publish is republished before anything new),   *)
(*   and later perhaps ACKED back to the owner. Delivery can come at any    *)
(*   time, including after the owner has stopped: a publish that timed out  *)
(*   can still be stored. That is the hazard the boundary S exists for.     *)
(*                                                                          *)
(*   Front doors. A terminal the owner does not take goes to the drain log, *)
(*   a table ordered by commit timestamp. FrontDoorAppend has no guard on   *)
(*   the owner's health: any front door may believe the owner unreachable,  *)
(*   whatever the truth, so the model allows an append whenever the lease   *)
(*   is not closed.                                                         *)
(*                                                                          *)
(*   The auditor. It applies the owner's records in the order the log       *)
(*   received them. Once the lease is draining and every publish the owner  *)
(*   issued before its cutoff has passed its deadline, it publishes a       *)
(*   fence tick. It stores S when it applies that tick, and only then       *)
(*   decides the drain log's rows and reaps.                                *)
(*                                                                          *)
(*   A rebuild. It reads the archive, which has every delivered owner       *)
(*   record with its sequence number but not the order between publishers.  *)
(*   If S was never stored it stores S itself. It and the auditor write the *)
(*   same stored winners, so "live and rebuild agree" is the statement that *)
(*   every stored winner equals one function of the durable state, Canon.   *)
(*                                                                          *)
(*   The enclave, only as far as "did the client get anything": open        *)
(*   (nothing delivered yet), delivered (a settle is owed and will be       *)
(*   sent), or gone (it gave up with nothing delivered, and sends no        *)
(*   settle). A stream delivers once its first heartbeat is answered. A     *)
(*   non-streaming request never heartbeats, and delivers when its provider *)
(*   answers: the auditor learns of its hold only from its terminal.        *)
(*                                                                          *)
(* WHAT IS ABSTRACTED                                                       *)
(*                                                                          *)
(*   - A Spanner transaction is one atomic action. Lock order and           *)
(*     wound-wait are outside this model.                                   *)
(*   - Time is two one-way flags and one per hold: the owner's cutoff has   *)
(*     passed; the publish deadline of everything it issued before the      *)
(*     cutoff has passed; and a hold's first-heartbeat allowance has        *)
(*     elapsed. Nothing here rests on WHEN the lease is marked draining:    *)
(*     MarkDraining may come at any time. What this spec needs from the     *)
(*     clocks is A1.                                                        *)
(*   - Amounts are not modeled. A reap charges its hold's last durable      *)
(*     heartbeat; that the amount is right is the checkpoint audit's job    *)
(*     (AuditorCommit) and the property tests'.                             *)
(*   - Redelivery and auditor member changes are AuditorCommit's. Here the  *)
(*     auditor applies each record once.                                    *)
(*   - Every authorization is admitted before the model starts, and         *)
(*     admissions are not published: the log shows a hold only through its  *)
(*     heartbeats and its terminal.                                         *)
(*   - A gateway's terminal is always a settle. A refund takes the same     *)
(*     paths.                                                               *)
(*                                                                          *)
(* ASSUMPTIONS, each with a mutant that widens it (proofs/manifest.toml)    *)
(*                                                                          *)
(*   A1. A publish the owner issued before its cutoff is acked only within  *)
(*       its deadline, and the fence tick is published only after the last  *)
(*       such deadline (the fence time F plus the skew allowance). Mutants  *)
(*       tick-before-deadline and ack-after-deadline. The owner's own part  *)
(*       is a guard, not an assumption: it issues nothing after its cutoff  *)
(*       (mutant owner-issues-after-cutoff).                                *)
(*   A2. A rebuild's archive holds every record received before the tick,   *)
(*       and nothing the log has not received. Mutants incomplete-archive   *)
(*       and an-archive-ahead-of-the-log.                                   *)
(*   A3. A boot that declared the stream-open heartbeat has either reached  *)
(*       the owner with it or given up by the time the allowance elapses.   *)
(*       Mutant allowance-before-heartbeat-resolves.                        *)
(*   A4. An enclave that delivered nothing sends no settle: no byte of a    *)
(*       stream reaches the client before a first heartbeat is answered,    *)
(*       and an enclave whose first heartbeat failed answers 503 and stops  *)
(*       (quill-cloud-proxy). Mutant a-settle-for-nothing-delivered.        *)
(*                                                                          *)
(* THE RELEASE RULE. Section 4.5 releases a stream's hold when it has no    *)
(* accepted heartbeat, and says accepted means durable. The owner cannot    *)
(* see durable: a heartbeat whose publish it issued and never saw           *)
(* acknowledged may still be stored. So OwnerRelease asks for no heartbeat  *)
(* ISSUED. Reading the rule as "none acknowledged" or "none stored yet"     *)
(* releases a hold whose heartbeat then lands (mutants                      *)
(* release-without-durable-check and release-on-an-undelivered-heartbeat).  *)
(*                                                                          *)
(* NOT HERE: what keeps a lease from closing while a request the auditor    *)
(* cannot see is still running. That is the drain's end condition, a wait   *)
(* in time, and LeaseLifecycle's. Here a lease may close over such a hold;  *)
(* NoStreamClosedOver is about streams, which the auditor does see.         *)
(*                                                                          *)
(* EVERY GUARD IS ACCOUNTED FOR in TerminalOrder.guards.toml: what breaks   *)
(* when it alone is removed, or why nothing does. guard_sweep.py writes     *)
(* and re-checks that table. A condition inside an IF or a LET is not a     *)
(* guard in that sense; the ones that matter have mutants in the manifest.  *)
(*                                                                          *)
(* A CHANGE THAT BREAKS NOTHING HERE, AND WHY                               *)
(* (the manifest's "survivor": it is run and must find no error)            *)
(*                                                                          *)
(*   - AuditorReap requires every drain row to be applied first, which      *)
(*     models the reap transaction reading the hold's rows. With only "one  *)
(*     reap row per hold" in its place, a reap row can land behind an       *)
(*     unapplied settle row, and simply loses: first-in-order still holds.  *)
(*     The guard saves a wasted row, not safety.                            *)
(*                                                                          *)
(* A guard that is only removed, with nothing put in its place, is a row    *)
(* of the guard table and not a survivor: that AuditorReap asks for a       *)
(* durable heartbeat, and that PublishTick asks for a draining lease.       *)
(*                                                                          *)
(* A guard that also keeps an expression defined says less in the table     *)
(* than it holds up: removing it stops TLC before any claim can break. The  *)
(* mutants book-a-record-the-log-has-not-received and                       *)
(* answer-before-the-publish-is-stored widen such a guard and keep the      *)
(* expression defined.                                                      *)
(*                                                                          *)
(* The owner publishes one terminal per authorization, whatever its kind.   *)
(* Once a terminal's publish is acknowledged the owner may answer a later   *)
(* terminal for that hold with its outcome, so every acknowledged terminal  *)
(* has to be the lease's decision: AckedOwnerTerminalWins. A second         *)
(* terminal for a hold, once acknowledged, is one that is not.              *)
(*                                                                          *)
(* A guard the model leaves out on purpose: the owner's reaper reads the    *)
(* drain log before it reaps (the backstop). OwnerReap has no such guard.   *)
(* Owner records come first in the order, so a reap that races a drain-log  *)
(* settle wins, which Decision 70 allows. Safety must hold without it.      *)
(***************************************************************************)

EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Auths,       \* the lease's authorizations
    Streams,     \* those that are streams; the rest never heartbeat
    Declared,    \* streams whose boot declared the stream-open heartbeat
    MaxAppends   \* front-door appends to the drain log

ASSUME /\ Streams \subseteq Auths
       /\ Declared \subseteq Streams
       /\ MaxAppends \in Nat

\* The owner issues at most one terminal per authorization, and one heartbeat
\* per stream, so at most this many records.
MaxSeq == Cardinality(Auths) + Cardinality(Streams)
\* Front-door rows, plus at most one auditor reap per authorization.
MaxRows == MaxAppends + Cardinality(Auths)

NoTick == MaxSeq + 1
NoS == MaxSeq + 1

Terminal == {"settle", "reap", "release"}
NoWinner == [src |-> "none", idx |-> 0]

VARIABLES
    lease,          \* "open", "draining", "closed"
    ownerUp,        \* the owner process is running
    ownerCutoff,    \* the owner's cutoff has passed: it issues nothing new
    issuedAtCutoff, \* how many records the owner had issued at its cutoff
    deadlinePassed, \* the fence deadline: the cutoff plus a publish deadline
    outbox,         \* the owner's issued records; index = sequence number
    delivered,      \* how many of them the log has received
    acked,          \* how many of them were acked to the owner
    tickAt,         \* owner records received before the latest tick
    S,              \* the stored owner boundary
    drain,          \* the drain log, in commit order
    appends,        \* front-door appends so far
    ownerApplied,   \* owner records the stored state covers
    drainApplied,   \* drain rows the stored state covers
    winner,         \* the stored winner of each authorization
    ownerWinner,    \* the owner's in-memory decision: a sequence number, or 0
    gwAcked,        \* terminals whose outcome a gateway may have been told
    enc,            \* the enclave: "open", "delivered", "gone"
    allowance       \* the first-heartbeat allowance has elapsed

vars == << lease, ownerUp, ownerCutoff, issuedAtCutoff, deadlinePassed, outbox,
           delivered, acked, tickAt, S, drain, appends, ownerApplied,
           drainApplied, winner, ownerWinner, gwAcked, enc, allowance >>

----------------------------------------------------------------------------
\* Helpers

Min(set) == CHOOSE x \in set : \A y \in set : x <= y

Rec(a, k, r) == [auth |-> a, kind |-> k, row |-> r]

HbIssued(a) ==
    \E i \in 1..Len(outbox) : outbox[i].auth = a /\ outbox[i].kind = "hb"

HbAcked(a) ==
    \E i \in 1..acked : outbox[i].auth = a /\ outbox[i].kind = "hb"

\* A heartbeat the stored boundary covers.
HbDurable(a) ==
    /\ S # NoS
    /\ \E i \in 1..S : outbox[i].auth = a /\ outbox[i].kind = "hb"

OwnerTerms(a, n) ==
    { i \in 1..n : outbox[i].auth = a /\ outbox[i].kind \in Terminal }

DrainRows(a) == { j \in 1..Len(drain) : drain[j].auth = a }

\* The lease's order, as a function of durable state only: owner records up
\* to n by sequence number, then the drain log.
Canon(a, n) ==
    IF OwnerTerms(a, n) # {}
        THEN [src |-> "owner", idx |-> Min(OwnerTerms(a, n))]
    ELSE IF DrainRows(a) # {}
        THEN [src |-> "drain", idx |-> Min(DrainRows(a))]
    ELSE NoWinner

\* The owner works until its cutoff. It does not know when its lease is
\* marked draining, and need not: that happens only after the cutoff.
OwnerActive == ownerUp /\ ~ownerCutoff

\* A4: an enclave sends a settle only once it has delivered something.
Settles(a) == enc[a] = "delivered"

\* The fence deadline bounds only what was issued before the cutoff.
IssuedBeforeCutoff(i) == ~ownerCutoff \/ i <= issuedAtCutoff

----------------------------------------------------------------------------
Init ==
    /\ lease = "open"
    /\ ownerUp = TRUE
    /\ ownerCutoff = FALSE
    /\ issuedAtCutoff = 0
    /\ deadlinePassed = FALSE
    /\ outbox = << >>
    /\ delivered = 0
    /\ acked = 0
    /\ tickAt = NoTick
    /\ S = NoS
    /\ drain = << >>
    /\ appends = 0
    /\ ownerApplied = 0
    /\ drainApplied = 0
    /\ winner = [a \in Auths |-> NoWinner]
    /\ ownerWinner = [a \in Auths |-> 0]
    /\ gwAcked = {}
    /\ enc = [a \in Auths |-> "open"]
    /\ allowance = [a \in Auths |-> FALSE]

----------------------------------------------------------------------------
\* The owner

\* A stream's first heartbeat reaches the owner, which issues its record.
\* The request may arrive after its enclave gave up waiting for the answer.
\* The owner refuses a heartbeat for a hold it has already decided.
OwnerHeartbeat(a) ==
    /\ OwnerActive
    /\ a \in Streams
    /\ ownerWinner[a] = 0
    /\ ~HbIssued(a)
    /\ outbox' = Append(outbox, Rec(a, "hb", 0))
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    gwAcked, enc, allowance >>

\* A settle reaches the owner. It decides under the authorization's lock and
\* publishes only that winner; a later terminal is answered from memory.
OwnerSettle(a) ==
    /\ OwnerActive
    /\ Settles(a)
    /\ ownerWinner[a] = 0
    /\ outbox' = Append(outbox, Rec(a, "settle", 0))
    /\ ownerWinner' = [ownerWinner EXCEPT ![a] = Len(outbox) + 1]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, gwAcked, enc,
                    allowance >>

\* The owner's reaper. When the hold's deadline passes is not modeled, so it
\* may reap any undecided hold.
OwnerReap(a) ==
    /\ OwnerActive
    /\ ownerWinner[a] = 0
    /\ outbox' = Append(outbox, Rec(a, "reap", 0))
    /\ ownerWinner' = [ownerWinner EXCEPT ![a] = Len(outbox) + 1]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, gwAcked, enc,
                    allowance >>

\* Releasing a stream's hold before its first heartbeat. Only for a boot that
\* declared the stream-open heartbeat, and only if no heartbeat for the hold
\* was even ISSUED: an issued one may yet become durable.
OwnerRelease(a) ==
    /\ OwnerActive
    /\ a \in Declared
    /\ allowance[a]
    /\ ~HbIssued(a)
    /\ ownerWinner[a] = 0
    /\ outbox' = Append(outbox, Rec(a, "release", 0))
    /\ ownerWinner' = [ownerWinner EXCEPT ![a] = Len(outbox) + 1]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, gwAcked, enc,
                    allowance >>

\* Adoption at a renewal: the owner takes the first drain row of an undecided
\* hold and publishes it as its own record, carrying the row's identity.
OwnerAdopt(a) ==
    /\ OwnerActive
    /\ ownerWinner[a] = 0
    /\ DrainRows(a) # {}
    /\ LET j == Min(DrainRows(a))
       IN  outbox' = Append(outbox, Rec(a, drain[j].kind, j))
    /\ ownerWinner' = [ownerWinner EXCEPT ![a] = Len(outbox) + 1]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, gwAcked, enc,
                    allowance >>

OwnerCrash ==
    /\ ownerUp
    /\ ownerUp' = FALSE
    /\ UNCHANGED << lease, ownerCutoff, issuedAtCutoff, deadlinePassed, outbox,
                    delivered, acked, tickAt, S, drain, appends, ownerApplied,
                    drainApplied, winner, ownerWinner, gwAcked, enc, allowance
                    >>

----------------------------------------------------------------------------
\* The log

\* The log receives the next owner record. Enabled at any time, including
\* after the tick and after close: that is a publish stored late. Its guard
\* is what a log is, and no rule of the protocol: it holds only what was
\* issued.
Deliver ==
    /\ delivered < Len(outbox)
    /\ delivered' = delivered + 1
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, acked, tickAt, S, drain, appends,
                    ownerApplied, drainApplied, winner, ownerWinner, gwAcked,
                    enc, allowance >>

\* The owner learns a publish was stored, and answers. A publish issued
\* before the cutoff is acked within its deadline, so before the fence
\* deadline (A1). Once a terminal is acked the owner may tell a gateway its
\* outcome: the settle's own gateway, or one that brings a later terminal
\* for the hold. The design gives such an answer only for a publish acked
\* before the cutoff. The model lets it up to the fence deadline, which is
\* later, and the claims hold there too: A1 carries them, not the cutoff.
\* An acked first heartbeat lets the enclave stream, unless it
\* already gave up.
Ack ==
    /\ acked < delivered
    /\ ownerUp
    /\ ~(deadlinePassed /\ IssuedBeforeCutoff(acked + 1))
    /\ acked' = acked + 1
    /\ LET r == outbox[acked + 1]
       IN  /\ gwAcked' =
                 IF r.kind \in Terminal
                     THEN gwAcked \cup {[src |-> "owner", idx |-> acked + 1]}
                     ELSE gwAcked
           /\ enc' =
                 IF r.kind = "hb" /\ enc[r.auth] = "open"
                     THEN [enc EXCEPT ![r.auth] = "delivered"]
                     ELSE enc
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    allowance >>

----------------------------------------------------------------------------
\* Time

CutoffPass ==
    /\ ~ownerCutoff
    /\ ownerCutoff' = TRUE
    /\ issuedAtCutoff' = Len(outbox)
    /\ UNCHANGED << lease, ownerUp, deadlinePassed, outbox, delivered, acked,
                    tickAt, S, drain, appends, ownerApplied, drainApplied,
                    winner, ownerWinner, gwAcked, enc, allowance >>

DeadlinePass ==
    /\ ownerCutoff
    /\ ~deadlinePassed
    /\ deadlinePassed' = TRUE
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff, outbox,
                    delivered, acked, tickAt, S, drain, appends, ownerApplied,
                    drainApplied, winner, ownerWinner, gwAcked, enc, allowance
                    >>

\* A3: for a declared boot the allowance elapses only once its stream-open
\* heartbeat has reached the owner or the enclave has given up.
AllowanceElapse(a) ==
    /\ ~allowance[a]
    /\ a \in Declared => (enc[a] # "open" \/ HbIssued(a))
    /\ allowance' = [allowance EXCEPT ![a] = TRUE]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    gwAcked, enc >>

\* A non-streaming request's provider answers and the client gets the
\* response. No owner is involved until the settle.
EnclaveDeliver(a) ==
    /\ a \notin Streams
    /\ enc[a] = "open"
    /\ enc' = [enc EXCEPT ![a] = "delivered"]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff, deadlinePassed,
                    outbox, delivered, acked, tickAt, S, drain, appends,
                    ownerApplied, drainApplied, winner, ownerWinner, gwAcked,
                    allowance >>

\* The enclave gives up with nothing delivered: a stream's first heartbeat
\* failed in transit, or its answer was lost, or the enclave died.
EnclaveGiveUp(a) ==
    /\ enc[a] = "open"
    /\ enc' = [enc EXCEPT ![a] = "gone"]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    gwAcked, allowance >>

----------------------------------------------------------------------------
\* Front doors

\* A front door appends a settle the owner did not take, in a transaction
\* conditional on the lease not being closed, and answers "recorded".
FrontDoorAppend(a) ==
    /\ lease # "closed"
    /\ Settles(a)
    /\ appends < MaxAppends
    /\ drain' = Append(drain, [auth |-> a, kind |-> "settle"])
    /\ appends' = appends + 1
    /\ gwAcked' = gwAcked \cup {[src |-> "drain", idx |-> Len(drain) + 1]}
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, S,
                    ownerApplied, drainApplied, winner, ownerWinner, enc,
                    allowance >>

----------------------------------------------------------------------------
\* The auditor and the rebuild

\* The lease is marked draining: by the auditor once it has expired, or by
\* its owner. When is LeaseLifecycle's; nothing here depends on it.
MarkDraining ==
    /\ lease = "open"
    /\ lease' = "draining"
    /\ UNCHANGED << ownerUp, ownerCutoff, issuedAtCutoff, deadlinePassed,
                    outbox, delivered, acked, tickAt, S, drain, appends,
                    ownerApplied, drainApplied, winner, ownerWinner, gwAcked,
                    enc, allowance >>

\* The auditor applies the next owner record the log received before the
\* tick. The first terminal for an authorization becomes its stored winner.
\* Once S is stored, an owner record above it is ignored (section 4.8).
AuditorApplyOwner ==
    /\ S = NoS
    /\ ownerApplied < delivered
    /\ ownerApplied < tickAt
    /\ LET i == ownerApplied + 1
           r == outbox[i]
       IN  winner' =
               IF r.kind \in Terminal /\ winner[r.auth] = NoWinner
                   THEN [winner EXCEPT ![r.auth] = [src |-> "owner", idx |-> i]]
                   ELSE winner
    /\ ownerApplied' = ownerApplied + 1
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, S, drain,
                    appends, drainApplied, ownerWinner, gwAcked, enc, allowance
                    >>

\* A tick, published once the lease drains and every owner publish has
\* passed its deadline (A1). It is received after `delivered` records. The
\* auditor goes on publishing ticks, and a later one is received after more
\* records. The first that it applies is the fence: S is stored then, and
\* does not move.
\*
\* The model keeps only the latest tick. A tick published after S is stored
\* is the system's own, and it is why AuditorApplyOwner asks for `S = NoS`:
\* without that guard such a tick lets the auditor apply a record beyond the
\* boundary (mutant apply-a-record-above-the-boundary). A tick between the
\* fence tick and the storing of S is more than section 4.8 has, where a
\* later tick changes nothing: in those behaviors the auditor applies past
\* an earlier tick before it stores S, and a rebuild's boundary is at or
\* above the latest tick. They break no claim, and every behavior of the
\* system is still one here, with no tick in that interval.
PublishTick ==
    /\ lease = "draining"
    /\ deadlinePassed
    /\ tickAt' = delivered
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, S, drain,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    gwAcked, enc, allowance >>

\* The auditor stores S when it applies the tick: in the commit that has
\* booked every record up to S, and only if S is unset.
StoreS ==
    /\ tickAt # NoTick
    /\ S = NoS
    /\ ownerApplied = tickAt
    /\ S' = tickAt
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, drain,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    gwAcked, enc, allowance >>

\* A rebuild finds S unset. Its archive holds every record received before
\* the tick (A2) and perhaps later ones. It books every record it holds and
\* stores S in that same commit, never re-deciding a stored winner.
RebuildStoreS ==
    /\ tickAt # NoTick
    /\ S = NoS
    /\ S' \in tickAt..delivered
    /\ ownerApplied' = S'
    /\ winner' =
           [a \in Auths |->
               IF winner[a] # NoWinner THEN winner[a]
               ELSE IF OwnerTerms(a, S') # {}
                   THEN [src |-> "owner", idx |-> Min(OwnerTerms(a, S'))]
               ELSE NoWinner]
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, drain,
                    appends, drainApplied, ownerWinner, gwAcked, enc, allowance
                    >>

\* The drain log is read only after S is stored: owner records up to S come
\* first. Rows are decided in commit order.
ApplyDrain ==
    /\ S # NoS
    /\ drainApplied < Len(drain)
    /\ LET j == drainApplied + 1
           a == drain[j].auth
       IN  winner' =
               IF winner[a] = NoWinner
                   THEN [winner EXCEPT ![a] = [src |-> "drain", idx |-> j]]
                   ELSE winner
    /\ drainApplied' = drainApplied + 1
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, S, drain,
                    appends, ownerApplied, ownerWinner, gwAcked, enc, allowance
                    >>

\* The auditor reaps a hold the log showed (a durable heartbeat) that has no
\* terminal, by inserting a reap row after reading the hold's rows.
AuditorReap(a) ==
    /\ lease = "draining"
    /\ S # NoS
    /\ drainApplied = Len(drain)
    /\ HbDurable(a)
    /\ winner[a] = NoWinner
    /\ drain' = Append(drain, [auth |-> a, kind |-> "reap"])
    /\ UNCHANGED << lease, ownerUp, ownerCutoff, issuedAtCutoff,
                    deadlinePassed, outbox, delivered, acked, tickAt, S,
                    appends, ownerApplied, drainApplied, winner, ownerWinner,
                    gwAcked, enc, allowance >>

\* Close reads the drain log beyond what was applied, in its transaction, and
\* needs every hold the log showed to have ended. A hold with no durable
\* trace is released uncharged at close.
Close ==
    /\ lease = "draining"
    /\ S # NoS
    /\ drainApplied = Len(drain)
    /\ \A a \in Auths : HbDurable(a) => winner[a] # NoWinner
    /\ lease' = "closed"
    /\ UNCHANGED << ownerUp, ownerCutoff, issuedAtCutoff, deadlinePassed,
                    outbox, delivered, acked, tickAt, S, drain, appends,
                    ownerApplied, drainApplied, winner, ownerWinner, gwAcked,
                    enc, allowance >>

----------------------------------------------------------------------------
Next ==
    \/ \E a \in Auths : OwnerHeartbeat(a)
    \/ \E a \in Auths : OwnerSettle(a)
    \/ \E a \in Auths : OwnerReap(a)
    \/ \E a \in Auths : OwnerRelease(a)
    \/ \E a \in Auths : OwnerAdopt(a)
    \/ OwnerCrash
    \/ Deliver
    \/ Ack
    \/ CutoffPass
    \/ DeadlinePass
    \/ \E a \in Auths : AllowanceElapse(a)
    \/ \E a \in Auths : EnclaveDeliver(a)
    \/ \E a \in Auths : EnclaveGiveUp(a)
    \/ \E a \in Auths : FrontDoorAppend(a)
    \/ MarkDraining
    \/ AuditorApplyOwner
    \/ PublishTick
    \/ StoreS
    \/ RebuildStoreS
    \/ ApplyDrain
    \/ \E a \in Auths : AuditorReap(a)
    \/ Close

\* Time passes and the auditor keeps working. Nothing promises that the
\* owner, the log's delivery or any front door makes progress.
Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(CutoffPass)
    /\ WF_vars(DeadlinePass)
    /\ WF_vars(MarkDraining)
    /\ WF_vars(AuditorApplyOwner)
    /\ WF_vars(PublishTick)
    /\ WF_vars(StoreS)
    /\ WF_vars(ApplyDrain)
    /\ WF_vars(\E a \in Auths : AuditorReap(a))
    /\ WF_vars(Close)

----------------------------------------------------------------------------
\* Invariants

WinnerType ==
    [src : {"none", "owner", "drain"}, idx : 0..(MaxSeq + MaxRows)]

TypeOK ==
    /\ lease \in {"open", "draining", "closed"}
    /\ ownerUp \in BOOLEAN
    /\ ownerCutoff \in BOOLEAN
    /\ issuedAtCutoff \in 0..MaxSeq
    /\ deadlinePassed \in BOOLEAN
    /\ Len(outbox) <= MaxSeq
    /\ \A i \in 1..Len(outbox) :
           /\ outbox[i].auth \in Auths
           /\ outbox[i].kind \in Terminal \cup {"hb"}
           /\ outbox[i].row \in 0..MaxRows
    /\ acked \in 0..MaxSeq
    /\ delivered \in 0..MaxSeq
    /\ tickAt \in 0..MaxSeq \cup {NoTick}
    /\ S \in 0..MaxSeq \cup {NoS}
    /\ Len(drain) <= MaxRows
    /\ \A j \in 1..Len(drain) :
           drain[j].auth \in Auths /\ drain[j].kind \in {"settle", "reap"}
    /\ appends \in 0..MaxAppends
    /\ ownerApplied \in 0..MaxSeq
    /\ drainApplied \in 0..MaxRows
    /\ winner \in [Auths -> WinnerType]
    /\ ownerWinner \in [Auths -> 0..MaxSeq]
    /\ gwAcked \subseteq WinnerType
    /\ enc \in [Auths -> {"open", "delivered", "gone"}]
    /\ allowance \in [Auths -> BOOLEAN]

\* The counts follow one another. Three of these are rules of the protocol:
\* the owner answers only once its publish is stored, the auditor books
\* only what the log received, and a boundary lies within what the log
\* received (a rebuild's archive is a copy of the log, A2). Two are what a
\* log is: it holds nothing that was not issued, and nothing is applied
\* from the drain log that is not in it. None is about the model's shape,
\* so none is TypeOK's.
CountsInOrder ==
    /\ acked <= delivered
    /\ delivered <= Len(outbox)
    /\ ownerApplied <= delivered
    /\ drainApplied <= Len(drain)
    /\ S # NoS => S <= delivered

\* Invariant 3. Every stored winner is the first terminal in the lease's
\* order, a function of durable state alone. So the live auditor and a
\* rebuild, which both store winners, cannot disagree, whenever each ran.
WinnerIsFirstInOrder ==
    \A a \in Auths :
        winner[a] # NoWinner =>
            IF S # NoS
                THEN winner[a] = Canon(a, S)
                ELSE /\ OwnerTerms(a, ownerApplied) # {}
                     /\ winner[a] = [src |-> "owner",
                                     idx |-> Min(OwnerTerms(a, ownerApplied))]

\* Section 4.5, adoption. Adopting a drain row changes who publishes that
\* terminal, not which terminal wins: an adopted record that wins is the
\* drain log's first row for its authorization. That is the row that wins
\* when the owner's copy is stored too late and the drain log decides, so
\* the outcome does not depend on which of the two happened. The claim sits
\* close to OwnerAdopt's own choice of row, by construction: it states what
\* any other way of adopting would have to keep.
AdoptionKeepsTheWinner ==
    \A a \in Auths :
        (winner[a].src = "owner" /\ outbox[winner[a].idx].row # 0) =>
            outbox[winner[a].idx].row = Min(DrainRows(a))

\* Invariant 4, first half. A terminal the owner acknowledged is never
\* beyond the stored boundary, where it would be ignored.
AckedOwnerRecordWithinBoundary ==
    S # NoS =>
        \A t \in gwAcked : t.src = "owner" => t.idx <= S

\* Invariant 3, for the owner's answers. A terminal the owner acknowledged,
\* of any kind, is the lease's decision for its authorization: the owner
\* publishes only the terminal that wins, so what it told a gateway is what
\* gets booked. It holds from the moment the record is applied or the
\* boundary is stored, not only once the lease has closed.
AckedOwnerTerminalWins ==
    \A t \in gwAcked :
        (t.src = "owner" /\ (t.idx <= ownerApplied \/ S # NoS)) =>
            winner[outbox[t.idx].auth] = t

\* Invariant 4. When the lease is closed, every drain row a gateway was told
\* is recorded has been read, and its authorization has a winner: the row
\* itself, or a terminal before it in the order.
NoAckedDrainRowLost ==
    lease = "closed" =>
        \A t \in gwAcked :
            t.src = "drain" =>
                /\ t.idx <= drainApplied
                /\ winner[drain[t.idx].auth] # NoWinner

\* Invariant 4, for heartbeats. A lease never closes over a stream that ran:
\* a hold whose first heartbeat was answered has a terminal by then, so it
\* is charged its settle or its snapshot, never released at close. It reads
\* the answer the owner gave, which is a fact that stays, and not what the
\* enclave did next.
NoStreamClosedOver ==
    lease = "closed" =>
        \A a \in Streams : HbAcked(a) => winner[a] # NoWinner

\* Section 4.5. A hold with a heartbeat in the log is never released: not by
\* a release record, and not by closing the lease over it.
DurableHeartbeatNeverReleased ==
    /\ \A i, j \in 1..delivered :
           ~(/\ outbox[i].auth = outbox[j].auth
             /\ outbox[i].kind = "hb"
             /\ outbox[j].kind = "release")
    /\ lease = "closed" =>
           \A a \in Auths :
               HbDurable(a) =>
                   /\ winner[a] # NoWinner
                   /\ ~(/\ winner[a].src = "owner"
                        /\ outbox[winner[a].idx].kind = "release")

\* Section 4.5. The enclave of a released hold has given up. It is not still
\* waiting for its provider, so the release cuts nothing; and it delivered
\* nothing, so no settle can follow and nothing is owed.
NoLiveRequestReleased ==
    \A a \in Auths :
        (ownerWinner[a] # 0 /\ outbox[ownerWinner[a]].kind = "release")
            => enc[a] = "gone"

\* Section 4.5, with A4. A released hold owes nothing: no settle exists for
\* it, in the owner's records or in the drain log.
ReleasedHoldOwesNothing ==
    \A a \in Auths :
        (ownerWinner[a] # 0 /\ outbox[ownerWinner[a]].kind = "release") =>
            /\ ~\E i \in 1..Len(outbox) :
                   outbox[i].auth = a /\ outbox[i].kind = "settle"
            /\ ~\E j \in 1..Len(drain) :
                   drain[j].auth = a /\ drain[j].kind = "settle"

\* A closed lease has no unapplied drain row. Close and append conflict.
ClosedLeaseHasNoUnappliedRow ==
    lease = "closed" => drainApplied = Len(drain)

----------------------------------------------------------------------------
\* Liveness. A draining lease whose records can be read closes, and stays
\* closed; with the invariants above, every acknowledged terminal is then
\* booked.

EventuallyClosed == <>[](lease = "closed")

=============================================================================
