---------------------------- MODULE LeaseLifecycle ----------------------------
(***************************************************************************)
(* One lease over time, from docs/design/fast-admission-and-batched-        *)
(* settlement.md sections 4.2, 4.3, 4.7 and 4.8: renewals, the owner's      *)
(* cutoff, expiry, draining and close, with clocks that differ by up to the *)
(* skew allowance, and an owner process that dies and comes back.           *)
(*                                                                          *)
(* This is written before the code. Nothing implements it yet.              *)
(*                                                                          *)
(* The question it answers: can an owner admit a request under a lease that *)
(* is no longer its to use? The owner admits in memory, on its own clock,   *)
(* against an expiry that lives in Spanner. The auditor drains and closes   *)
(* the lease on its own clock, and closing releases the lease's money.      *)
(* Nothing but the skew allowance and three conditional writes keeps the    *)
(* two apart.                                                               *)
(*                                                                          *)
(* ACTORS                                                                   *)
(*                                                                          *)
(*   Spanner. It holds the lease row: its state, the epoch of the process   *)
(*   it was granted to, its expiry, and whether its renewal is revoked.     *)
(*   Each action that writes it is one transaction.                         *)
(*                                                                          *)
(*   Owner processes. The node runs one process at a time; a restart gives  *)
(*   it a new epoch and an empty memory. A process that holds the lease     *)
(*   knows the expiry Spanner last answered and whether it has stopped      *)
(*   admitting. It admits while its own clock is before that expiry less    *)
(*   the skew allowance, and while its view of the workspace's state is     *)
(*   fresh and not paused.                                                  *)
(*                                                                          *)
(*   The auditor. It marks the lease draining once its own clock passes the *)
(*   expiry plus the skew allowance, in a write conditional on the expiry   *)
(*   it read. It closes a draining lease once it knows every hold has       *)
(*   ended: from the owner's final checkpoint or complete hand-off, or else *)
(*   once its clock passes the expiry plus the longest a hold can live plus *)
(*   a grace.                                                               *)
(*                                                                          *)
(*   Holds. Each is one admitted request. It ends at its terminal, or at    *)
(*   the longest life the gateway allows, whichever is first.               *)
(*                                                                          *)
(* WHAT IS ABSTRACTED                                                       *)
(*                                                                          *)
(*   - Money. Every hold is one unit against an allocation of LeaseSize.    *)
(*     Grants, the trust allowance, returns, consumption, overruns and debt *)
(*     are CreditDebt's. Leases interact only through money, so one lease   *)
(*     and one node are the whole of this model: a second owner would hold  *)
(*     a lease of its own.                                                  *)
(*   - A renewal and its answer are one step. A lost answer is the same as  *)
(*     ReplayedRenew, where Spanner extends the expiry and nobody learns:   *)
(*     the owner then knows an older expiry, which only stops it sooner.    *)
(*   - An owner learns at once that one of its holds ended. A real owner    *)
(*     may learn later, and so admits less than this one, never more.       *)
(*   - Which records the lease's log holds, and who decides a terminal, are *)
(*     TerminalOrder's. Here a hold is open or it is gone.                  *)
(*   - A hand-off is none, partial (chunks without their manifest) or       *)
(*     complete. A final checkpoint with no open hold is a complete one.    *)
(*     While it is partial the auditor may have any of its chunks, or none. *)
(*   - Before a pause every process's view of the workspace is as fresh as  *)
(*     can be. Only after one does it matter whether a process looked       *)
(*     again, and that is left free.                                        *)
(*   - Time is whole steps up to MaxTime. No renewal commits after          *)
(*     LastRenew, so that the lease can close inside the model.             *)
(*                                                                          *)
(* ASSUMPTIONS, each with a mutant that widens it (proofs/manifest.toml)    *)
(*                                                                          *)
(*   A1. Bounded skew. Every clock reading is within Skew of the true time. *)
(*       Mutant skew-wider-than-allowed.                                    *)
(*   A2. A process measures the age of its view of the workspace exactly    *)
(*       (a monotonic clock) and never admits on one older than CacheAge.   *)
(*       Mutant admit-on-a-stale-view.                                      *)
(*   A3. No hold outlives MaxLife: the gateway ends a request by its own    *)
(*       timer. Mutant a-hold-outlives-its-life.                            *)
(*                                                                          *)
(* TerminalOrder does not need the lease to be marked draining after its    *)
(* owner's cutoff: it needs the fence tick after the publish deadline.      *)
(* CutoffBeforeDrain is here for admission's sake: an owner that is still   *)
(* within its cutoff may admit, and a draining lease must admit nothing.    *)
(*                                                                          *)
(* A pause here stands for every stop that reaches an owner through its     *)
(* view of the workspace: the debt mark, a trust downgrade and a switch     *)
(* out of fast mode travel the same way. The one pause is never lifted;     *)
(* a stop that comes and goes, as the debt mark can, is outside the bounds. *)
(***************************************************************************)

EXTENDS Naturals, FiniteSets

CONSTANTS
    MaxHolds,     \* holds open at once
    LeaseSize,    \* the lease's allocation, in holds
    Window,       \* a grant or renewal sets the expiry this far ahead
    Skew,         \* the skew allowance
    MaxLife,      \* the longest a hold lives
    Grace,        \* the auditor's grace before it closes on time alone
    CacheAge,     \* the oldest view of the workspace a process may admit on
    LastRenew,    \* no renewal commits after this time
    MaxRestarts   \* process restarts

ASSUME /\ Window \in Nat \ {0}
       /\ Skew \in Nat
       /\ MaxLife \in Nat \ {0}
       /\ Grace \in Nat
       /\ Grace >= Skew
       /\ CacheAge \in Nat
       /\ LeaseSize \in Nat \ {0}

HoldIds == 1..MaxHolds

MaxExpiry == LastRenew + Window
\* Late enough that the lease can drain and close on time alone.
MaxTime == MaxExpiry + MaxLife + Grace

NoRead == MaxExpiry + 1
\* A free hold slot. A hold that ends frees its slot, so MaxHolds bounds how
\* many are open at once, not how many are ever admitted.
NoHold == [open |-> FALSE, epoch |-> 0, life |-> 0, underOpen |-> TRUE,
           inPauseAge |-> TRUE, inRevokeWindow |-> TRUE, late |-> FALSE]

VARIABLES
    now,         \* the true time
    lease,       \* Spanner: the lease row
    epoch,       \* the node's current process
    has,         \* that process holds the lease in memory
    known,       \* the expiry it last learned
    stopped,     \* it has stopped admitting under the lease
    holds,       \* the open holds, with what was true when each was admitted
    handoff,     \* what the auditor has of the lease's hand-off
    listed,      \* open holds named by the hand-off, as the auditor has it
    audRead,     \* the expiry the auditor read before marking draining
    paused,      \* Spanner: the workspace is paused
    pausedAt,    \* when it was paused
    view,        \* the process's view of the workspace: paused, and when read
    revokedAt    \* when the lease's renewal was revoked

vars == << now, lease, epoch, has, known, stopped, holds, handoff, listed,
           audRead, paused, pausedAt, view, revokedAt >>

----------------------------------------------------------------------------
\* Helpers

Min(set) == CHOOSE x \in set : \A y \in set : x <= y

\* A1: what a clock may read now.
Readings == { c \in 0..(MaxTime + Skew) : c + Skew >= now /\ c <= now + Skew }

Open == { h \in HoldIds : holds[h].open }

\* The holds the current process knows it has open: those it admitted.
Own == { h \in Open : holds[h].epoch = epoch }

\* The owner's cutoff: its clock is before the expiry it knows, less the skew.
WithinCutoff == \E c \in Readings : c + Skew < known

----------------------------------------------------------------------------
\* The lease was granted at time 0 to the first process.
Init ==
    /\ now = 0
    /\ lease = [state |-> "open", epoch |-> 0, expiry |-> Window,
                revoked |-> FALSE]
    /\ epoch = 0
    /\ has = TRUE
    /\ known = Window
    /\ stopped = FALSE
    /\ holds = [h \in HoldIds |-> NoHold]
    /\ handoff = "none"
    /\ listed = {}
    /\ audRead = NoRead
    /\ paused = FALSE
    /\ pausedAt = 0
    /\ view = [paused |-> FALSE, at |-> 0]
    /\ revokedAt = 0

----------------------------------------------------------------------------
\* Spanner: renewals, revocation, pause

\* A renewal by the node's current process. The write is conditional on the
\* lease being open and not revoked, and on the process's epoch. The owner
\* takes the new expiry from Spanner's answer, not from its own clock. A
\* process that finds a lease it does not remember starts with nothing held.
OwnerRenew ==
    /\ now <= LastRenew
    /\ lease.state = "open"
    /\ ~lease.revoked
    /\ lease.epoch = epoch
    /\ lease' = [lease EXCEPT !.expiry = now + Window]
    /\ known' = now + Window
    /\ has' = TRUE
    /\ stopped' = IF has THEN stopped ELSE FALSE
    /\ UNCHANGED << now, epoch, holds, handoff, audRead, paused, pausedAt,
                    view, revokedAt, listed >>

\* A renewal request from the lease's own process, applied when nobody is
\* listening: a duplicate, or one whose answer was lost.
ReplayedRenew ==
    /\ now <= LastRenew
    /\ lease.state = "open"
    /\ ~lease.revoked
    /\ lease.expiry < now + Window
    /\ lease' = [lease EXCEPT !.expiry = now + Window]
    /\ UNCHANGED << now, epoch, has, known, stopped, holds, handoff, audRead,
                    paused, pausedAt, view, revokedAt, listed >>

\* A front door that cannot reach the owner revokes the lease's renewal.
Revoke ==
    /\ lease.state = "open"
    /\ ~lease.revoked
    /\ lease' = [lease EXCEPT !.revoked = TRUE]
    /\ revokedAt' = now
    /\ UNCHANGED << now, epoch, has, known, stopped, holds, handoff, audRead,
                    paused, pausedAt, view, listed >>

Pause ==
    /\ ~paused
    /\ paused' = TRUE
    /\ pausedAt' = now
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, holds, handoff,
                    audRead, view, revokedAt, listed >>

----------------------------------------------------------------------------
\* The owner process

\* After a pause, a process that looks again sees it.
RefreshView ==
    /\ paused
    /\ ~view.paused
    /\ view' = [paused |-> TRUE, at |-> now]
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, holds, handoff,
                    audRead, paused, pausedAt, revokedAt, listed >>

\* An admission, in memory. The process reads its clock after recording the
\* hold, so the cutoff check and the hold are one step.
Admit ==
    /\ has
    /\ ~stopped
    /\ Open # HoldIds
    /\ Cardinality(Own) < LeaseSize
    /\ WithinCutoff
    /\ ~view.paused
    /\ now <= view.at + CacheAge
    /\ LET h == Min(HoldIds \ Open)
       IN  holds' = [holds EXCEPT ![h] =
                        [open |-> TRUE, epoch |-> epoch, life |-> MaxLife,
                         underOpen |-> lease.state = "open",
                         inPauseAge |-> ~paused \/ now <= pausedAt + CacheAge,
                         inRevokeWindow |->
                             ~lease.revoked \/ now < revokedAt + Window,
                         late |-> handoff # "none"]]
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, handoff, audRead,
                    paused, pausedAt, view, revokedAt, listed >>

\* A hold's terminal, wherever it lands.
HoldEnds(h) ==
    /\ holds[h].open
    /\ holds' = [holds EXCEPT ![h] = NoHold]
    /\ listed' = listed \ {h}
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, handoff, audRead,
                    paused, pausedAt, view, revokedAt >>

\* The process stops admitting under the lease: it went idle, reached its
\* maximum life, the workspace is paused, or it is leaving.
OwnerStop ==
    /\ has
    /\ ~stopped
    /\ stopped' = TRUE
    /\ UNCHANGED << now, lease, epoch, has, known, holds, handoff, audRead,
                    paused, pausedAt, view, revokedAt, listed >>

\* With no hold open, it publishes a final checkpoint and marks the lease
\* draining, in a write conditional on the lease being open and on its epoch.
\* It forgets the lease in the same step. Only the process the lease was
\* granted to passes the write's conditions, and while the lease is open
\* that process holds it, so `has` is not asked for again.
OwnerFinish ==
    /\ stopped
    /\ Own = {}
    /\ lease.state = "open"
    /\ lease.epoch = epoch
    /\ lease' = [lease EXCEPT !.state = "draining"]
    /\ handoff' = "complete"
    /\ listed' = Own
    /\ has' = FALSE
    /\ UNCHANGED << now, epoch, known, stopped, holds, audRead, paused,
                    pausedAt, view, revokedAt >>

\* A forced exit: the process stops admitting, then publishes its open
\* holds in chunks. Until the manifest says how many chunks there are, the
\* auditor may have any of them, or none...
ForcedExitStart ==
    /\ has
    /\ handoff = "none"
    /\ lease.state = "open"
    /\ stopped' = TRUE
    /\ handoff' = "partial"
    /\ listed' \in SUBSET Own
    /\ UNCHANGED << now, lease, epoch, has, known, holds, audRead, paused,
                    pausedAt, view, revokedAt >>

\* ...then the manifest, which makes the list whole, and marks the lease
\* draining. The list is of the holds the chunks were cut from: one admitted
\* after the hand-off began is in no chunk.
ForcedExitFinish ==
    /\ handoff = "partial"
    /\ lease.state = "open"
    /\ lease.epoch = epoch
    /\ lease' = [lease EXCEPT !.state = "draining"]
    /\ handoff' = "complete"
    /\ listed' = { h \in Own : ~holds[h].late }
    /\ has' = FALSE
    /\ UNCHANGED << now, epoch, known, stopped, holds, audRead, paused,
                    pausedAt, view, revokedAt >>

\* A renewal that changed no row: the process re-reads the lease, finds it
\* is not open, and stops using it.
OwnerDrops ==
    /\ has
    /\ lease.state # "open"
    /\ has' = FALSE
    /\ UNCHANGED << now, lease, epoch, known, stopped, holds, handoff, audRead,
                    paused, pausedAt, view, revokedAt, listed >>

\* The process dies and another starts on the node, with a new epoch and
\* nothing in memory. It reads the workspace's state as it starts.
Restart ==
    /\ epoch < MaxRestarts
    /\ epoch' = epoch + 1
    /\ has' = FALSE
    /\ view' = [paused |-> paused, at |-> now]
    /\ UNCHANGED << now, lease, known, stopped, holds, handoff, audRead,
                    paused, pausedAt, revokedAt, listed >>

----------------------------------------------------------------------------
\* The auditor

\* It reads a lease whose expiry, plus the skew allowance, its clock has
\* passed...
AuditorRead ==
    /\ lease.state = "open"
    /\ audRead = NoRead
    /\ \E c \in Readings : c >= lease.expiry + Skew
    /\ audRead' = lease.expiry
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, holds, handoff,
                    paused, pausedAt, view, revokedAt, listed >>

\* ...and marks it draining, conditional on the lease still being open with
\* the expiry it read.
AuditorMark ==
    /\ audRead # NoRead
    /\ audRead' = NoRead
    /\ lease' =
           IF lease.state = "open" /\ lease.expiry = audRead
               THEN [lease EXCEPT !.state = "draining"]
               ELSE lease
    /\ UNCHANGED << now, epoch, has, known, stopped, holds, handoff, paused,
                    pausedAt, view, revokedAt, listed >>

\* The drain's end condition. Either the owner listed its holds and they
\* have all ended, or the auditor's clock is past the expiry plus the longest
\* a hold can live plus the grace.
HoldsKnownEnded == handoff = "complete" /\ listed = {}
HoldsEndedByTime == \E c \in Readings : c >= lease.expiry + MaxLife + Grace

\* Close releases the money the lease still reserves.
Close ==
    /\ lease.state = "draining"
    /\ HoldsKnownEnded \/ HoldsEndedByTime
    /\ lease' = [lease EXCEPT !.state = "closed"]
    /\ UNCHANGED << now, epoch, has, known, stopped, holds, handoff, audRead,
                    paused, pausedAt, view, revokedAt, listed >>

----------------------------------------------------------------------------
\* Time. A3: a hold that reaches MaxLife has ended. Until a pause, the
\* process's view of the workspace stays fresh.
Tick ==
    /\ now < MaxTime
    /\ now' = now + 1
    /\ holds' = [h \in HoldIds |->
                    IF ~holds[h].open THEN holds[h]
                    ELSE IF holds[h].life = 1 THEN NoHold
                    ELSE [holds[h] EXCEPT !.life = @ - 1]]
    /\ view' = IF paused THEN view ELSE [paused |-> FALSE, at |-> now + 1]
    /\ listed' = { h \in listed : holds[h].life # 1 }
    /\ UNCHANGED << lease, epoch, has, known, stopped, handoff, audRead,
                    paused, pausedAt, revokedAt >>

----------------------------------------------------------------------------
Next ==
    \/ OwnerRenew
    \/ ReplayedRenew
    \/ Revoke
    \/ Pause
    \/ RefreshView
    \/ Admit
    \/ \E h \in HoldIds : HoldEnds(h)
    \/ OwnerStop
    \/ OwnerFinish
    \/ ForcedExitStart
    \/ ForcedExitFinish
    \/ OwnerDrops
    \/ Restart
    \/ AuditorRead
    \/ AuditorMark
    \/ Close
    \/ Tick

\* Time passes and the auditor keeps working. Nothing promises that an owner
\* does anything.
Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Tick)
    /\ WF_vars(AuditorRead)
    /\ WF_vars(AuditorMark)
    /\ WF_vars(Close)

----------------------------------------------------------------------------
\* Invariants

TypeOK ==
    /\ now \in 0..MaxTime
    /\ lease.state \in {"open", "draining", "closed"}
    /\ lease.epoch \in 0..MaxRestarts
    /\ lease.expiry \in 0..MaxExpiry
    /\ lease.revoked \in BOOLEAN
    /\ epoch \in 0..MaxRestarts
    /\ has \in BOOLEAN
    /\ known \in 0..MaxExpiry
    /\ stopped \in BOOLEAN
    /\ \A h \in HoldIds :
           /\ holds[h].open \in BOOLEAN
           /\ holds[h].epoch \in 0..MaxRestarts
           /\ holds[h].life \in 0..MaxLife
           /\ holds[h].underOpen \in BOOLEAN
           /\ holds[h].inPauseAge \in BOOLEAN
           /\ holds[h].inRevokeWindow \in BOOLEAN
           /\ holds[h].late \in BOOLEAN
    /\ handoff \in {"none", "partial", "complete"}
    /\ listed \subseteq Open
    /\ audRead \in 0..MaxExpiry \cup {NoRead}
    /\ paused \in BOOLEAN
    /\ pausedAt \in 0..MaxTime
    /\ view.paused \in BOOLEAN
    /\ view.at \in 0..MaxTime
    /\ revokedAt \in 0..MaxTime

\* Once the lease is draining or closed, a process that still holds it in
\* memory is past its cutoff: the owner has stopped admitting, deciding and
\* publishing under it.
CutoffBeforeDrain == (has /\ lease.state # "open") => ~WithinCutoff

\* Invariant 1, first sentence. Every admission was under a lease that was
\* open in Spanner at that moment.
AdmittedOnlyUnderOpenLease == \A h \in Open : holds[h].underOpen

\* Invariant 4, the reservation half. The lease is not closed, and its money
\* released, while a hold admitted under it is still open.
NoOpenHoldOnClosedLease == Open # {} => lease.state # "closed"

\* Invariant 1, second sentence. The lease's open holds fit its allocation.
HoldsFitAllocation == Cardinality(Open) <= LeaseSize

\* Invariants 6 and 7. Only the process the lease was granted to ever admits
\* under it. A later process on the node gets a lease of its own.
SingleWriter == \A h \in Open : holds[h].epoch = lease.epoch

\* Invariant 9. A pause stops admission within the state cache's maximum age.
PauseBoundsAdmission == \A h \in Open : holds[h].inPauseAge

\* Invariant 10, the renewal half, by its consequence. Once the lease's
\* renewal is revoked its expiry cannot move, so nothing is admitted under
\* it a window later.
RevocationBoundsAdmission == \A h \in Open : holds[h].inRevokeWindow

\* The same half, said of Spanner's row. A revoked lease's expiry is at most
\* a window after its revocation, whoever asks for a renewal: the owner, or
\* a request of its that arrives when nobody is listening. That is what lets
\* the lease expire and the log take over.
RevokedExpiryStays == lease.revoked => lease.expiry <= revokedAt + Window

----------------------------------------------------------------------------
\* Liveness. The lease closes, whatever its owner did or failed to do, and
\* its money comes back.

EventuallyClosed == <>[](lease.state = "closed")

\* A lease is open, then draining, then closed: one step at a time, and
\* never back. A close releases the lease's money once, so a lease that
\* could be marked draining again would release it twice.
LeaseMovesOneWay ==
    [][\/ lease'.state = lease.state
       \/ lease.state = "open" /\ lease'.state = "draining"
       \/ lease.state = "draining" /\ lease'.state = "closed"]_vars

\* Once a lease is draining, nothing about it changes but its state. Its
\* expiry least of all: the close on time alone is measured from it, so a
\* renewal that still moved it would put that close off, a window at a time.
OnlyAnOpenLeaseChanges ==
    [][lease.state # "open" =>
           /\ lease'.expiry = lease.expiry
           /\ lease'.revoked = lease.revoked
           /\ lease'.epoch = lease.epoch]_vars

=============================================================================
