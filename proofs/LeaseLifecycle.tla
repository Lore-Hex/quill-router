---------------------------- MODULE LeaseLifecycle ----------------------------
(*****************************************************************************)
(* One lease over time, from docs/design/fast-admission-and-batched-         *)
(* settlement.md sections 4.2, 4.3, 4.7 and 4.8: renewals and their answers, *)
(* the owner's cutoff, expiry, draining and close, with clocks that differ   *)
(* by up to the skew allowance, and an owner process that dies and comes     *)
(* back.                                                                     *)
(*                                                                           *)
(* This is written before the code. Nothing implements it yet.               *)
(*                                                                           *)
(* The question it answers: can an owner admit a request under a lease that  *)
(* is no longer its to use? The owner admits in memory, on its own clock,    *)
(* against an expiry that lives in Spanner. The auditor drains and closes    *)
(* the lease on its own clock, and closing releases the lease's money. What  *)
(* keeps the two apart is the skew allowance, three conditional writes, and  *)
(* what a process takes for a lease of its own.                              *)
(*                                                                           *)
(* ACTORS                                                                    *)
(*                                                                           *)
(*   Spanner. It holds the lease row: its state, the epoch of the process it *)
(*   was granted to, its expiry, and whether its renewal is revoked. Each    *)
(*   action that writes it is one transaction.                               *)
(*                                                                           *)
(*   Owner processes. The node runs one process at a time; a restart gives   *)
(*   it a new epoch and an empty memory. A process that holds the lease      *)
(*   knows the expiry Spanner last answered and whether it has stopped       *)
(*   admitting. It admits while its own clock is before that expiry less the *)
(*   skew allowance, and while its view of the workspace's state is fresh    *)
(*   and not paused.                                                         *)
(*                                                                           *)
(*   The auditor. It marks the lease draining once its own clock passes the  *)
(*   expiry plus the skew allowance, in a write conditional on the expiry it *)
(*   read. It closes a draining lease once it knows every hold has ended:    *)
(*   from the owner's final checkpoint or complete hand-off, or else once    *)
(*   its clock passes the expiry plus the longest a hold can live plus a     *)
(*   grace.                                                                  *)
(*                                                                           *)
(*   Holds. Each is one admitted request. It ends at its terminal, or at the *)
(*   longest life the gateway allows, whichever is first.                    *)
(*                                                                           *)
(* WHAT IS ABSTRACTED                                                        *)
(*                                                                           *)
(*   - Money. Every hold is one unit against an allocation of LeaseSize.     *)
(*     Grants, the trust allowance, returns, consumption, overruns and debt  *)
(*     are CreditDebt's. Leases interact only through money, so one lease    *)
(*     and one node are the whole of this model: a second owner would hold a *)
(*     lease of its own.                                                     *)
(*   - One renewal is out at a time: the write, then its answer on its way   *)
(*     to the process that asked. The answer may be lost. A request applied  *)
(*     when nobody is listening, because it was sent twice or its process    *)
(*     died, is ReplayedRenew.                                               *)
(*   - A publish is one step and is stored at once. Which records the        *)
(*     lease's log holds, their order, and the owner's cutoff on publishing  *)
(*     them are TerminalOrder's. Here a hold is open or it is gone.          *)
(*   - An owner learns at once that one of its holds ended. A real owner may *)
(*     learn later, and so admits less than this one, never more.            *)
(*   - A hand-off is none, partial (chunks without their manifest) or        *)
(*     complete. A final checkpoint is a complete one. While it is partial   *)
(*     the auditor may have any of its chunks, or none.                      *)
(*   - Before a pause every process's view of the workspace is as fresh as   *)
(*     can be. Only after one does it matter whether a process looked again, *)
(*     and that is left free.                                                *)
(*   - Time is whole steps up to MaxTime. No renewal commits after           *)
(*     LastRenew, so that the lease can close inside the model.              *)
(*   - One process at a time. A process still running when its successor     *)
(*     starts holds this lease as before, and its successor holds another:   *)
(*     for this lease that is the same as no restart.                        *)
(*                                                                           *)
(* ASSUMPTIONS, each with a mutant that widens it (proofs/manifest.toml)     *)
(*                                                                           *)
(*   A1. Bounded skew. Every clock reading is within Skew of the true time.  *)
(*       Mutant skew-wider-than-allowed.                                     *)
(*   A2. A process measures the age of its view of the workspace exactly (a  *)
(*       monotonic clock) and never admits on one older than CacheAge.       *)
(*       Mutant admit-on-a-stale-view.                                       *)
(*   A3. No hold outlives MaxLife: the gateway ends a request by its own     *)
(*       timer. Mutant a-hold-outlives-its-life.                             *)
(*   A4. A process uses only the lease it was granted, and only until it     *)
(*       lets it go. It starts with nothing. It takes no lease up from       *)
(*       Spanner, or from a request that names one, and an answer that       *)
(*       arrives after it finished or dropped the lease does not bring the   *)
(*       lease back. This is the design's Invariant 6. It is the code's to   *)
(*       keep: Spanner cannot keep it. Mutants a-later-process-takes-up-the- *)
(*       lease, an-answer-brings-the-lease-back and a-later-process-         *)
(*       publishes-the-last-record.                                          *)
(*                                                                           *)
(* Spanner's conditions on the epoch are a second check of A4, for a process *)
(* that held a lease it was not granted. No behavior here has one, so in the *)
(* guard table those conditions hold up nothing.                             *)
(*                                                                           *)
(* TerminalOrder does not need the lease to be marked draining after its     *)
(* owner's cutoff: it needs the fence tick after the publish deadline.       *)
(* CutoffBeforeDrain is here for admission's sake: an owner that is still    *)
(* within its cutoff may admit, and a draining lease must admit nothing.     *)
(*                                                                           *)
(* A pause here stands for every stop that reaches an owner through its view *)
(* of the workspace: a revoke of the workspace, the debt mark, a trust       *)
(* downgrade and a switch out of fast mode travel the same way. The one      *)
(* pause is never lifted; a stop that comes and goes, as the debt mark can,  *)
(* is outside the bounds. Revoking one lease's renewal (section 4.3) is      *)
(* another thing, and is Revoke below.                                       *)
(*****************************************************************************)

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
NoAnswer == MaxExpiry + 1
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
    answer,      \* a renewal's answer on its way to the process that asked
    holds,       \* the open holds, with what was true when each was admitted
    handoff,     \* what the auditor has of the lease's hand-off
    listed,      \* open holds named by the hand-off, as the auditor has it
    audRead,     \* the expiry the auditor read before marking draining
    paused,      \* Spanner: the workspace is paused
    pausedFor,   \* for how long, counted up to CacheAge + 1
    view,        \* the process's view of the workspace: paused, and its age
    revokedFor   \* how long the lease's renewal has been revoked, up to Window

vars == << now, lease, epoch, has, known, stopped, answer, holds, handoff,
           listed, audRead, paused, pausedFor, view, revokedFor >>

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
    /\ answer = NoAnswer
    /\ holds = [h \in HoldIds |-> NoHold]
    /\ handoff = "none"
    /\ listed = {}
    /\ audRead = NoRead
    /\ paused = FALSE
    /\ pausedFor = 0
    /\ view = [paused |-> FALSE, age |-> 0]
    /\ revokedFor = 0

----------------------------------------------------------------------------
\* Spanner: renewals, revocation, pause

\* A renewal by the process the lease was granted to. The write is
\* conditional on the lease being open and not revoked, and on the process's
\* epoch. Its answer, the new expiry, sets out for the process. While the
\* lease is open and unrevoked the process it was granted to holds it, so
\* `has` is not asked for.
OwnerRenew ==
    /\ now <= LastRenew
    /\ answer = NoAnswer
    /\ lease.state = "open"
    /\ ~lease.revoked
    /\ lease.epoch = epoch
    /\ lease' = [lease EXCEPT !.expiry = now + Window]
    /\ answer' = now + Window
    /\ UNCHANGED << now, epoch, has, known, stopped, holds, handoff, audRead,
                    paused, pausedFor, view, revokedFor, listed >>

\* The answer arrives. The process takes the new expiry from it, not from
\* its own clock, and only for a lease it still holds (A4).
RenewAnswer ==
    /\ answer # NoAnswer
    /\ has
    /\ known' = answer
    /\ answer' = NoAnswer
    /\ UNCHANGED << now, lease, epoch, has, stopped, holds, handoff, audRead,
                    paused, pausedFor, view, revokedFor, listed >>

\* The answer is lost, or reaches a process that no longer holds the lease
\* and is dropped there.
AnswerLost ==
    /\ answer # NoAnswer
    /\ answer' = NoAnswer
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* A renewal request from the lease's own process, applied when nobody is
\* listening: it was sent twice, or its process has died.
ReplayedRenew ==
    /\ now <= LastRenew
    /\ lease.state = "open"
    /\ ~lease.revoked
    /\ lease.expiry < now + Window
    /\ lease' = [lease EXCEPT !.expiry = now + Window]
    /\ UNCHANGED << now, epoch, has, known, stopped, answer, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* A front door that cannot reach the owner revokes the lease's renewal.
Revoke ==
    /\ ~lease.revoked
    /\ lease' = [lease EXCEPT !.revoked = TRUE]
    /\ revokedFor' = 0
    /\ UNCHANGED << now, epoch, has, known, stopped, answer, holds, handoff,
                    audRead, paused, pausedFor, view, listed >>

Pause ==
    /\ ~paused
    /\ paused' = TRUE
    /\ pausedFor' = 0
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, holds,
                    handoff, audRead, view, revokedFor, listed >>

----------------------------------------------------------------------------
\* The owner process

\* After a pause, a process that looks again sees it.
RefreshView ==
    /\ paused
    /\ ~view.paused
    /\ view' = [paused |-> TRUE, age |-> 0]
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, holds,
                    handoff, audRead, paused, pausedFor, revokedFor, listed >>

\* An admission, in memory. The process reads its clock after recording the
\* hold, so the cutoff check and the hold are one step.
Admit ==
    /\ has
    /\ ~stopped
    /\ Open # HoldIds
    /\ Cardinality(Own) < LeaseSize
    /\ WithinCutoff
    /\ ~view.paused
    /\ view.age <= CacheAge
    /\ LET h == Min(HoldIds \ Open)
       IN  holds' = [holds EXCEPT ![h] =
                        [open |-> TRUE, epoch |-> epoch, life |-> MaxLife,
                         underOpen |-> lease.state = "open",
                         inPauseAge |-> ~paused \/ pausedFor <= CacheAge,
                         inRevokeWindow |->
                             ~lease.revoked \/ revokedFor < Window,
                         late |-> handoff # "none"]]
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* A hold's terminal, wherever it lands.
HoldEnds(h) ==
    /\ holds[h].open
    /\ holds' = [holds EXCEPT ![h] = NoHold]
    /\ listed' = listed \ {h}
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, handoff,
                    audRead, paused, pausedFor, view, revokedFor >>

\* The process stops admitting under the lease: it went idle, reached its
\* maximum life, the workspace is paused, or it is leaving.
OwnerStop ==
    /\ has
    /\ ~stopped
    /\ stopped' = TRUE
    /\ UNCHANGED << now, lease, epoch, has, known, answer, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* With no hold open, it publishes a final checkpoint: the lease's last
\* record, which lists the holds still open.
FinalCheckpoint ==
    /\ has
    /\ stopped
    /\ Own = {}
    /\ handoff' = "complete"
    /\ listed' = Own
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, holds,
                    audRead, paused, pausedFor, view, revokedFor >>

\* A forced exit: the process stops admitting, then publishes its open
\* holds in chunks. Until the manifest says how many chunks there are, the
\* auditor may have any of them, or none...
ForcedExitStart ==
    /\ has
    /\ handoff = "none"
    /\ stopped' = TRUE
    /\ handoff' = "partial"
    /\ listed' \in SUBSET Own
    /\ UNCHANGED << now, lease, epoch, has, known, answer, holds, audRead,
                    paused, pausedFor, view, revokedFor >>

\* ...then the manifest, which makes the list whole. The list is of the
\* holds the chunks were cut from: one admitted after the hand-off began is
\* in no chunk.
ForcedExitManifest ==
    /\ has
    /\ handoff = "partial"
    /\ handoff' = "complete"
    /\ listed' = { h \in Own : ~holds[h].late }
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, holds,
                    audRead, paused, pausedFor, view, revokedFor >>

\* Its last record published, the process marks the lease draining, in a
\* write conditional on the lease being open and on its epoch, and lets the
\* lease go.
OwnerDrains ==
    /\ has
    /\ handoff = "complete"
    /\ lease.state = "open"
    /\ lease.epoch = epoch
    /\ lease' = [lease EXCEPT !.state = "draining"]
    /\ has' = FALSE
    /\ UNCHANGED << now, epoch, known, stopped, answer, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* A renewal that changed no row: the process re-reads the lease, finds it
\* revoked or no longer open, and lets it go.
OwnerDrops ==
    /\ has
    /\ lease.state # "open" \/ lease.revoked
    /\ has' = FALSE
    /\ UNCHANGED << now, lease, epoch, known, stopped, answer, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* The process dies and another starts on the node, with a new epoch and
\* nothing in memory. An answer on its way to the dead process reaches
\* nobody. The new process reads the workspace's state as it starts.
Restart ==
    /\ epoch < MaxRestarts
    /\ epoch' = epoch + 1
    /\ has' = FALSE
    /\ known' = 0
    /\ stopped' = FALSE
    /\ answer' = NoAnswer
    /\ view' = [paused |-> paused, age |-> 0]
    /\ UNCHANGED << now, lease, holds, handoff, audRead, paused, pausedFor,
                    revokedFor, listed >>

----------------------------------------------------------------------------
\* The auditor

\* It reads a lease whose expiry, plus the skew allowance, its clock has
\* passed...
AuditorRead ==
    /\ lease.state = "open"
    /\ audRead = NoRead
    /\ \E c \in Readings : c >= lease.expiry + Skew
    /\ audRead' = lease.expiry
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, holds,
                    handoff, paused, pausedFor, view, revokedFor, listed >>

\* ...and marks it draining, in a write conditional on the lease still being
\* open with the expiry it read.
AuditorMark ==
    /\ audRead # NoRead
    /\ lease.state = "open"
    /\ lease.expiry = audRead
    /\ lease' = [lease EXCEPT !.state = "draining"]
    /\ audRead' = NoRead
    /\ UNCHANGED << now, epoch, has, known, stopped, answer, holds, handoff,
                    paused, pausedFor, view, revokedFor, listed >>

\* The write changed no row: the lease was renewed since the read, or is no
\* longer open. The auditor will read again.
AuditorMarkRefused ==
    /\ audRead # NoRead
    /\ lease.state # "open" \/ lease.expiry # audRead
    /\ audRead' = NoRead
    /\ UNCHANGED << now, lease, epoch, has, known, stopped, answer, holds,
                    handoff, paused, pausedFor, view, revokedFor, listed >>

\* The drain's end. Either the owner's last record listed its holds and they
\* have all ended...
CloseOnTheList ==
    /\ lease.state = "draining"
    /\ handoff = "complete"
    /\ listed = {}
    /\ lease' = [lease EXCEPT !.state = "closed"]
    /\ UNCHANGED << now, epoch, has, known, stopped, answer, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

\* ...or the auditor's clock is past the expiry plus the longest a hold can
\* live plus the grace. Close releases the money the lease still reserves.
CloseOnTime ==
    /\ lease.state = "draining"
    /\ \E c \in Readings : c >= lease.expiry + MaxLife + Grace
    /\ lease' = [lease EXCEPT !.state = "closed"]
    /\ UNCHANGED << now, epoch, has, known, stopped, answer, holds, handoff,
                    audRead, paused, pausedFor, view, revokedFor, listed >>

----------------------------------------------------------------------------
\* Time. A3: a hold that reaches MaxLife has ended. Until a pause, the
\* process's view of the workspace stays fresh. An age is counted only as
\* far as anything asks about it.
Tick ==
    /\ now < MaxTime
    /\ now' = now + 1
    /\ holds' = [h \in HoldIds |->
                    IF ~holds[h].open THEN holds[h]
                    ELSE IF holds[h].life = 1 THEN NoHold
                    ELSE [holds[h] EXCEPT !.life = @ - 1]]
    /\ listed' = { h \in listed : holds[h].life # 1 }
    /\ view' = IF paused
                   THEN [view EXCEPT !.age = Min({@ + 1, CacheAge + 1})]
                   ELSE [paused |-> FALSE, age |-> 0]
    /\ pausedFor' = IF paused THEN Min({pausedFor + 1, CacheAge + 1}) ELSE 0
    /\ revokedFor' = IF lease.revoked THEN Min({revokedFor + 1, Window}) ELSE 0
    /\ UNCHANGED << lease, epoch, has, known, stopped, answer, handoff,
                    audRead, paused >>

----------------------------------------------------------------------------
Next ==
    \/ OwnerRenew
    \/ RenewAnswer
    \/ AnswerLost
    \/ ReplayedRenew
    \/ Revoke
    \/ Pause
    \/ RefreshView
    \/ Admit
    \/ \E h \in HoldIds : HoldEnds(h)
    \/ OwnerStop
    \/ FinalCheckpoint
    \/ ForcedExitStart
    \/ ForcedExitManifest
    \/ OwnerDrains
    \/ OwnerDrops
    \/ Restart
    \/ AuditorRead
    \/ AuditorMark
    \/ AuditorMarkRefused
    \/ CloseOnTheList
    \/ CloseOnTime
    \/ Tick

\* Time passes and the auditor keeps working. Nothing promises that an owner
\* does anything, or that the auditor closes a lease sooner than time alone
\* allows.
Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Tick)
    /\ WF_vars(AuditorRead)
    /\ WF_vars(AuditorMark)
    /\ WF_vars(AuditorMarkRefused)
    /\ WF_vars(CloseOnTime)

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
    /\ answer \in 0..MaxExpiry \cup {NoAnswer}
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
    /\ pausedFor \in 0..(CacheAge + 1)
    /\ view.paused \in BOOLEAN
    /\ view.age \in 0..(CacheAge + 1)
    /\ revokedFor \in 0..Window

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

\* What Invariant 6 protects, in holds: the lease's open holds fit its
\* allocation. A second process admitting under the lease would not know
\* the first one's holds. With charges this is the second sentence of
\* Invariant 1, which is CreditDebt's.
HoldsFitAllocation == Cardinality(Open) <= LeaseSize

\* Invariants 6 and 7. Only the process the lease was granted to ever admits
\* under it. A later process on the node gets a lease of its own.
SingleWriter == \A h \in Open : holds[h].epoch = lease.epoch

\* Invariant 9. A pause stops admission within the state cache's maximum age.
\* It rests on the two things Admit asks of the view, and on A2.
PauseBoundsAdmission == \A h \in Open : holds[h].inPauseAge

\* Invariant 10, the renewal half, by its consequence. Once the lease's
\* renewal is revoked its expiry cannot move, so nothing is admitted under
\* it a window later.
RevocationBoundsAdmission == \A h \in Open : holds[h].inRevokeWindow

----------------------------------------------------------------------------
\* Liveness. The lease closes, whatever its owner did or failed to do, and
\* its money comes back.

EventuallyClosed == <>[](lease.state = "closed")

----------------------------------------------------------------------------
\* What a step may do to the lease's row.

\* A lease is open, then draining, then closed: one step at a time, and
\* never back. A close releases the lease's money once, so a lease that
\* could be marked draining again would release it twice.
LeaseMovesOneWay ==
    [][\/ lease'.state = lease.state
       \/ lease.state = "open" /\ lease'.state = "draining"
       \/ lease.state = "draining" /\ lease'.state = "closed"]_vars

\* Once a lease is draining its expiry does not move. The close on time
\* alone is measured from it, so a renewal that still moved it would put
\* that close off, a window at a time.
DrainingExpiryStays ==
    [][lease.state # "open" => lease'.expiry = lease.expiry]_vars

\* Invariant 10, the renewal half, said of Spanner's row. Once a lease's
\* renewal is revoked its expiry does not move, whoever asks for a renewal:
\* the owner, or a request of its that arrives when nobody is listening, in
\* the moment of the revocation as much as a window later. That is what
\* lets the lease expire and the log take over.
RevokedExpiryStays == [][lease.revoked => lease'.expiry = lease.expiry]_vars

=============================================================================
