--------------------------- MODULE AuditorCommit ---------------------------
(***************************************************************************)
(* The auditor's per-lease commit, from docs/design/fast-admission-and-     *)
(* batched-settlement.md section 4.8: what one member of the auditor stores *)
(* so that any member can carry on after it, and why redelivery, a member   *)
(* taking over and a member that stalled never book a record twice or lose  *)
(* one.                                                                     *)
(*                                                                          *)
(* This is written before the code. Nothing implements it yet.              *)
(*                                                                          *)
(* ACTORS                                                                   *)
(*                                                                          *)
(*   The owner. While the lease is open it issues records with              *)
(*   consecutive sequence numbers: heartbeats, each carrying its hold's     *)
(*   running charge as the snapshot, from 0; one terminal per               *)
(*   authorization, a settle or a refund; and checkpoints, each carrying    *)
(*   its cumulative `consumed`.                                             *)
(*                                                                          *)
(*   The log. It stores what the owner issued, usually in order. It may     *)
(*   store a record before the one issued just before it, and may store a   *)
(*   record twice: a republish of a publish that timed out and landed. A    *)
(*   record may also land after the fence tick, or never.                   *)
(*                                                                          *)
(*   Pub/Sub. It gives the lease's records to one member at a time, in      *)
(*   the order stored, and gives a member it moves the lease to every       *)
(*   record not acknowledged. The member it moved them from is not told,    *)
(*   and may still commit what it applied.                                  *)
(*                                                                          *)
(*   Members of the auditor's consumer group. A member loads the lease row, *)
(*   applies records in memory and commits them in one transaction,         *)
(*   conditional on the commit version it read; an audit fault it finds is  *)
(*   raised with that commit. Only then does it acknowledge them. It may    *)
(*   crash and lose its memory. Everything it writes for the lease, its     *)
(*   commit, a reap, the close and a gap's stop, is conditional on the      *)
(*   commit version, and when the version refuses one, it re-reads: it      *)
(*   drops what it applied and its places, and loads again.                 *)
(*                                                                          *)
(*   Owners and front doors, as writers of the lease row: each raises the   *)
(*   allocation, at any moment, between a member's load and its commit.     *)
(*                                                                          *)
(*   Once the lease is draining, front doors append terminals to its drain  *)
(*   log, and the auditor publishes the fence tick into the lease's         *)
(*   records. The member that applies the tick takes S, the highest owner   *)
(*   sequence number it has applied, and stores it with its next commit.    *)
(*   Once S is stored, a member reaps the open holds at their latest        *)
(*   snapshots, books the drain log after the owner's records, and closes   *)
(*   the lease. An owner record that arrives after the tick is above S, or  *)
(*   a duplicate of one at or below it, and is ignored.                     *)
(*                                                                          *)
(* WHAT IS ABSTRACTED                                                       *)
(*                                                                          *)
(*   - A Spanner transaction is one atomic action.                          *)
(*   - Amounts are small numbers: an owner's settle charges 2, a refund     *)
(*     0, a front door's settle 1, and a reap its hold's latest snapshot,   *)
(*     0 included. An open hold is told from no hold by NoHold, not by      *)
(*     its snapshot.                                                        *)
(*   - The fence tick may come at any time once the lease drains. When it   *)
(*     comes is TerminalOrder's: after the publish deadline of everything   *)
(*     the owner issued before its cutoff. Here a record still unstored     *)
(*     then may land later (MaxLate bounds how many), or never.             *)
(*   - Drain-log progress is not stored. A member reads the drain log       *)
(*     from its start, and a row for an authorization that already has a    *)
(*     winner charges nothing. That is the design's rule, and the reason    *)
(*     the stored winners are loaded once the lease drains.                 *)
(*   - Raises are counted, not priced. What a raise is for, and when the    *)
(*     auditor raises the allocation itself, is CreditDebt's.               *)
(*   - Several leases in one transaction. Each lease's statement is         *)
(*     conditional on its own commit version, so Reread is one lease's      *)
(*     statement matching no row, whatever the others did.                  *)
(*                                                                          *)
(* ASSUMPTION, with a mutant that widens it (proofs/manifest.toml)          *)
(*                                                                          *)
(*   A1. Pub/Sub gives a member it moves the lease to, or a member that     *)
(*       comes back, every record not acknowledged, in the order stored.    *)
(*       Mutant redelivery-skips-a-record.                                  *)
(*                                                                          *)
(*   The log's order is not assumed. A record stored ahead of an earlier    *)
(*   one shows as a gap in the sequence numbers, and the gap stops the      *)
(*   lease for an operator.                                                 *)
(*                                                                          *)
(* THE CLAIMS                                                               *)
(*                                                                          *)
(*   The records they are about are the owner's at or below S, as the       *)
(*   order the log received them in defines it, and the drain log's.        *)
(*                                                                          *)
(*   BoundaryIsS. The stored S is that boundary: the highest sequence       *)
(*   number up to which every owner record was received before the fence    *)
(*   tick. StoredSFirst: S is stored before the drain log is reaped or      *)
(*   booked, in a member's memory too.                                      *)
(*                                                                          *)
(*   WinnerIsFirst. An authorization's stored winner is the first           *)
(*   terminal in the lease's order: the owner's, else the drain log's       *)
(*   first row. A refund counts: a front door's settle for a refunded       *)
(*   request loses.                                                         *)
(*                                                                          *)
(*   BookedIsWinners. The booked consumption is the sum of the winners'     *)
(*   charges: no record booked twice, across redelivery and takeover.       *)
(*                                                                          *)
(*   NoRaiseLost. The allocation is the grant, plus every raise, less       *)
(*   what is booked: a raise between a member's load and its commit is      *)
(*   kept.                                                                  *)
(*                                                                          *)
(*   ReapAtLastSnapshot. A reap charges its hold's latest snapshot in the   *)
(*   log (Invariant 5's reap half).                                         *)
(*                                                                          *)
(*   NoChargeLost. A closed lease has a winner for every authorization      *)
(*   the log or the drain log showed (Invariant 4, across takeover).        *)
(*                                                                          *)
(*   AuditsEachCheckpoint. The audit at each checkpoint: a commit that      *)
(*   stores a checkpoint at or below S whose `consumed` differs from the    *)
(*   sum of the owner's terminals with lower sequence numbers, a            *)
(*   republished one counted once, raises the alert (Invariant 2), and      *)
(*   nothing else raises it. The configurations `lying` and `again` have an *)
(*   owner that may lie. A property of steps.                               *)
(*                                                                          *)
(*   GapIsReal: a gap stops the lease only where the log, before the fence  *)
(*   tick, stored an owner record before one the owner issued before it.    *)
(*   DrainingLeaseCloses: a draining lease closes, unless such a gap        *)
(*   stopped it, as long as the members keep working. It is what a member   *)
(*   left with nothing it may do would break.                               *)
(*                                                                          *)
(* WHAT WRITING THIS FOUND                                                  *)
(*                                                                          *)
(*   Section 4.8 makes the commit, and so the close, conditional on the     *)
(*   commit version, and says only that a reap's transaction first reads    *)
(*   the hold's drain-log rows. But Pub/Sub can give the lease back to a    *)
(*   member that lost it after another member committed and acknowledged    *)
(*   a newer heartbeat. Nothing is redelivered to bring that member's       *)
(*   memory up to date, and a reap from it charges the older snapshot. So   *)
(*   Reap is conditional on the commit version too (mutant                  *)
(*   reap-without-the-version, in the configuration `again`).               *)
(*                                                                          *)
(*   And a member refused that way re-reads even with nothing applied to    *)
(*   commit, or it is left with nothing it may do (mutant                   *)
(*   no-reread-when-clean, in `again`). A gap is declared with the commit   *)
(*   version too: a member another overtook compares a record with progress *)
(*   the row has passed, and stops the lease for a gap the log does not     *)
(*   have (mutant a-gap-from-stale-progress, in `again`). So is an audit's  *)
(*   alert, raised with the commit: that member may apply a wrong           *)
(*   checkpoint above S it never learned to ignore (mutant                  *)
(*   a-refused-member-alerts, in `again`). Design v44 states one guard on   *)
(*   every write.                                                           *)
(*                                                                          *)
(* EVERY GUARD IS ACCOUNTED FOR in AuditorCommit.guards.toml: what breaks   *)
(* when it alone is removed, or why nothing does.                           *)
(***************************************************************************)

EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    Auths,         \* authorizations admitted under the lease
    Members,       \* members of the auditor's consumer group
    MaxSeq,        \* records the owner issues
    MaxSnap,       \* heartbeats per hold
    MaxDup,        \* records the log stores twice
    MaxAhead,      \* records the log stores ahead of the one issued before
    MaxLate,       \* records the log stores after the fence tick
    MaxRaise,      \* raises of the allocation by owners and front doors
    MaxAssign,     \* times Pub/Sub moves the lease to another member
    MaxCrash,      \* times a member loses its memory
    MaxAppend,     \* terminals front doors append to the drain log
    Lying,         \* whether the owner may issue a wrong checkpoint
    Grant          \* the lease's allocation when granted

ASSUME
    /\ MaxSeq \in Nat /\ MaxSnap \in Nat /\ MaxDup \in Nat /\ MaxAhead \in Nat /\ MaxLate \in Nat
    /\ MaxRaise \in Nat /\ MaxAssign \in Nat /\ MaxCrash \in Nat
    /\ MaxAppend \in Nat /\ Lying \in BOOLEAN /\ Grant \in Nat
    /\ Members # {}

SettleCharge == 2
DoorCharge == 1
NoAuth == "none"

Rec(k, a, c, seq, idx) == [k |-> k, a |-> a, c |-> c, seq |-> seq, idx |-> idx]
NoWin == Rec("none", NoAuth, 0, 0, 0)
Tick == Rec("tick", NoAuth, 0, 0, 0)
NoHold == -1
NoS == -1
OwnerTerminal(r) == r.k \in {"settle", "refund"}

\* A member's memory. `loaded` is false until it reads the lease row.
Blank == [loaded |-> FALSE, ver |-> 0, prog |-> 0, alloc |-> 0,
          holds |-> [a \in Auths |-> NoHold], win |-> [a \in Auths |-> NoWin],
          wl |-> FALSE, osum |-> 0, dbooked |-> 0, dirty |-> FALSE, S |-> NoS,
          fault |-> FALSE]

VARIABLES
    \* The owner
    out,        \* records issued and not yet stored, in issue order
    nextSeq,    \* the next sequence number
    osnap,      \* per authorization, the heartbeats issued
    ownerSum,   \* the charges of the terminals issued: the owner's `consumed`
    ownerDone,  \* authorizations with a terminal issued
    \* The log
    log,        \* what the log stored, in the order it stored it
    dups,       \* records stored twice
    aheads,     \* records stored ahead of an earlier one
    ticked,     \* whether the auditor has published the fence tick
    lates,      \* records stored after it
    \* The lease row in Spanner
    st,         \* "open", "draining" or "closed"
    ver,        \* the commit version
    prog,       \* the highest owner sequence number applied
    booked,     \* the consumption booked
    alloc,      \* the remaining allocation
    holds,      \* per authorization, the open hold's latest snapshot, or NoHold
    win,        \* per authorization, the winning terminal, or NoWin
    osum,       \* the charges of the owner's terminals applied, for the audit
    raised,     \* raises written by owners and front doors
    S,          \* the owner boundary, once stored, else NoS
    \* The drain log
    drain,      \* rows in commit order
    appends,    \* front doors' appends
    \* Pub/Sub
    holder,     \* the member the lease's records go to
    acked,      \* how many of the log's records are acknowledged
    assigns,    \* times the lease moved
    \* Members
    mem,        \* per member, its memory
    pos,        \* per member, the index in the log of its next record
    dpos,       \* per member, the index in the drain log of its next row
    done,       \* per member, the records its last commit made durable
    crashes,
    \* What the audit and the gap rule did
    alert,
    gap

ownerv == << out, nextSeq, osnap, ownerSum, ownerDone >>
logv == << log, dups, aheads, ticked, lates >>
row == << st, ver, prog, booked, alloc, holds, win, osum, raised, S >>
drainv == << drain, appends >>
pubsub == << holder, acked, assigns >>
members == << mem, pos, dpos, done, crashes >>
ghosts == << alert, gap >>
vars == << ownerv, logv, row, drainv, pubsub, members, ghosts >>

----------------------------------------------------------------------------
\* Helpers

RECURSIVE SumOver(_, _)
SumOver(f, set) ==
    IF set = {} THEN 0
    ELSE LET x == CHOOSE y \in set : TRUE IN f[x] + SumOver(f, set \ {x})

Min(set) == CHOOSE x \in set : \A y \in set : x <= y
Range(s) == { s[i] : i \in DOMAIN s }

\* A member's memory after it applies the owner record r. A terminal for an
\* authorization with a winner charges nothing. The audit's sum counts every
\* owner terminal, once per sequence number.
AppliedTo(M, r) ==
    LET decides == OwnerTerminal(r) /\ M.win[r.a] = NoWin
    IN [M EXCEPT
          !.prog = r.seq,
          !.dirty = TRUE,
          !.holds = IF r.k = "hb" /\ M.win[r.a] = NoWin
                      THEN [M.holds EXCEPT ![r.a] = r.c]
                    ELSE IF decides THEN [M.holds EXCEPT ![r.a] = NoHold]
                    ELSE M.holds,
          !.win = IF decides THEN [M.win EXCEPT ![r.a] = r] ELSE M.win,
          !.dbooked = IF decides THEN M.dbooked + r.c ELSE M.dbooked,
          !.osum = IF OwnerTerminal(r) THEN M.osum + r.c ELSE M.osum,
          !.fault = M.fault \/ (r.k = "ckpt" /\ r.c # M.osum)]

\* A member's memory after it applies the drain-log row r.
RowAppliedTo(M, r) ==
    IF M.win[r.a] = NoWin
    THEN [M EXCEPT !.win = [M.win EXCEPT ![r.a] = r],
                   !.holds = [M.holds EXCEPT ![r.a] = NoHold],
                   !.dbooked = M.dbooked + r.c,
                   !.dirty = TRUE]
    ELSE M

\* The commit version after a commit: it advances when the commit writes the
\* row.
Advanced(writes) == IF writes THEN ver + 1 ELSE ver

----------------------------------------------------------------------------
\* The owner

IssueHeartbeat(a) ==
    /\ st = "open"
    /\ nextSeq <= MaxSeq
    /\ a \notin ownerDone
    /\ osnap[a] < MaxSnap
    /\ out' = Append(out, Rec("hb", a, osnap[a], nextSeq, 0))
    /\ osnap' = [osnap EXCEPT ![a] = @ + 1]
    /\ nextSeq' = nextSeq + 1
    /\ UNCHANGED << ownerSum, ownerDone, logv, row, drainv, pubsub, members, ghosts >>

IssueTerminal(a, k) ==
    /\ st = "open"
    /\ nextSeq <= MaxSeq
    /\ a \notin ownerDone
    /\ out' = Append(out, Rec(k, a, IF k = "settle" THEN SettleCharge ELSE 0, nextSeq, 0))
    /\ ownerSum' = ownerSum + (IF k = "settle" THEN SettleCharge ELSE 0)
    /\ ownerDone' = ownerDone \cup {a}
    /\ nextSeq' = nextSeq + 1
    /\ UNCHANGED << osnap, logv, row, drainv, pubsub, members, ghosts >>

\* An honest owner's `consumed` is what its terminals issued so far charge.
IssueCheckpoint ==
    /\ st = "open"
    /\ nextSeq <= MaxSeq
    /\ out' = Append(out, Rec("ckpt", NoAuth, ownerSum, nextSeq, 0))
    /\ nextSeq' = nextSeq + 1
    /\ UNCHANGED << osnap, ownerSum, ownerDone, logv, row, drainv, pubsub, members, ghosts >>

\* A lying owner's is one more.
IssueWrongCheckpoint ==
    /\ Lying
    /\ st = "open"
    /\ nextSeq <= MaxSeq
    /\ out' = Append(out, Rec("ckpt", NoAuth, ownerSum + 1, nextSeq, 0))
    /\ nextSeq' = nextSeq + 1
    /\ UNCHANGED << osnap, ownerSum, ownerDone, logv, row, drainv, pubsub, members, ghosts >>

----------------------------------------------------------------------------
\* The log

Store ==
    /\ out # << >>
    /\ ~ticked
    /\ log' = Append(log, Head(out))
    /\ out' = Tail(out)
    /\ UNCHANGED << nextSeq, osnap, ownerSum, ownerDone, dups, aheads, ticked, lates, row, drainv, pubsub, members, ghosts >>

\* A publish that timed out lands after the fence tick: above S, whenever it
\* arrives. A record still issued and not stored may never be.
StoreLate ==
    /\ out # << >>
    /\ ticked
    /\ lates < MaxLate
    /\ log' = Append(log, Head(out))
    /\ out' = Tail(out)
    /\ lates' = lates + 1
    /\ UNCHANGED << nextSeq, osnap, ownerSum, ownerDone, dups, aheads, ticked, row, drainv, pubsub, members, ghosts >>

\* The record issued second is stored before the first.
StoreAhead ==
    /\ aheads < MaxAhead
    /\ ~ticked
    /\ Len(out) >= 2
    /\ log' = Append(log, out[2])
    /\ out' = << out[1] >> \o SubSeq(out, 3, Len(out))
    /\ aheads' = aheads + 1
    /\ UNCHANGED << nextSeq, osnap, ownerSum, ownerDone, dups, ticked, lates, row, drainv, pubsub, members, ghosts >>

\* A record the log holds is stored again.
StoreAgain(i) ==
    /\ dups < MaxDup
    /\ i <= Len(log)
    /\ log' = Append(log, log[i])
    /\ dups' = dups + 1
    /\ UNCHANGED << ownerv, aheads, ticked, lates, row, drainv, pubsub, members, ghosts >>

----------------------------------------------------------------------------
\* Other writers of the lease row, and draining

\* An owner's shortfall write, or a front door's append of a charge above
\* its hold, raises the allocation by arithmetic on the row.
Raise ==
    /\ st # "closed"
    /\ raised < MaxRaise
    /\ alloc' = alloc + 1
    /\ raised' = raised + 1
    /\ UNCHANGED << ownerv, logv, st, ver, prog, booked, holds, win, osum, S, drainv, pubsub, members, ghosts >>

\* The lease expires, or its owner stops: either way it drains, and its
\* owner issues nothing more.
MarkDraining ==
    /\ st = "open"
    /\ st' = "draining"
    /\ UNCHANGED << ownerv, logv, ver, prog, booked, alloc, holds, win, osum, raised, S, drainv, pubsub, members, ghosts >>

\* A front door appends a terminal it could not give the owner, after
\* checking, in its transaction, that the lease is not closed.
FrontDoorAppend(a) ==
    /\ st = "draining"
    /\ appends < MaxAppend
    /\ drain' = Append(drain, Rec("settle", a, DoorCharge, 0, Len(drain) + 1))
    /\ appends' = appends + 1
    /\ UNCHANGED << ownerv, logv, row, pubsub, members, ghosts >>

\* Once the lease drains, the auditor publishes the fence tick into the
\* lease's records. A record the owner issued that the log stores after it
\* is above S, or a duplicate of one at or below S.
FenceTick ==
    /\ st = "draining"
    /\ ~ticked
    /\ log' = Append(log, Tick)
    /\ ticked' = TRUE
    /\ UNCHANGED << ownerv, dups, aheads, lates, row, drainv, pubsub, members, ghosts >>

----------------------------------------------------------------------------
\* Pub/Sub and the members

\* The lease's records go to member m from now on, starting with the first
\* not acknowledged (A1). The member they went to is not told.
Assign(m) ==
    /\ assigns < MaxAssign
    /\ m # holder
    /\ holder' = m
    /\ pos' = [pos EXCEPT ![m] = acked + 1]
    /\ assigns' = assigns + 1
    /\ UNCHANGED << ownerv, logv, row, drainv, acked, mem, dpos, done, crashes, ghosts >>

\* A member loads the lease row: its progress, open holds, allocation and
\* commit version, and its winners only once it is draining.
Load(m) ==
    /\ m = holder
    /\ ~mem[m].loaded
    /\ mem' = [mem EXCEPT ![m] =
                 [loaded |-> TRUE, ver |-> ver, prog |-> prog, alloc |-> alloc,
                  holds |-> holds,
                  win |-> IF st = "open" THEN [a \in Auths |-> NoWin] ELSE win,
                  wl |-> st # "open", osum |-> osum, dbooked |-> 0, dirty |-> FALSE,
                  S |-> S, fault |-> FALSE]]
    /\ dpos' = [dpos EXCEPT ![m] = 1]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, pos, done, crashes, ghosts >>

\* A member that loaded an open lease which has since drained loads its
\* winners, beside the ones it decided itself.
LoadWinners(m) ==
    /\ st # "open"
    /\ mem[m].loaded
    /\ ~mem[m].wl
    /\ mem' = [mem EXCEPT ![m].win = [a \in Auths |-> IF mem[m].win[a] # NoWin
                                                     THEN mem[m].win[a] ELSE win[a]],
                          ![m].wl = TRUE]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, pos, dpos, done, crashes, ghosts >>

\* The member applies the next owner record, the one after its progress.
ApplyRecord(m) ==
    /\ m = holder
    /\ mem[m].loaded
    /\ ~gap
    /\ pos[m] <= Len(log)
    /\ st = "open" \/ mem[m].wl
    /\ mem[m].S = NoS
    /\ log[pos[m]].seq = mem[m].prog + 1
    /\ mem' = [mem EXCEPT ![m] = AppliedTo(mem[m], log[pos[m]])]
    /\ pos' = [pos EXCEPT ![m] = @ + 1]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, dpos, done, crashes, ghosts >>

\* A record at or below its progress is a redelivery or a duplicate. Once
\* the member knows S, every owner record is one of those or above S, and
\* ignored.
SkipRecord(m) ==
    /\ m = holder
    /\ mem[m].loaded
    /\ ~gap
    /\ pos[m] <= Len(log)
    /\ log[pos[m]].k # "tick"
    /\ log[pos[m]].seq <= mem[m].prog \/ mem[m].S # NoS
    /\ pos' = [pos EXCEPT ![m] = @ + 1]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, mem, dpos, done, crashes, ghosts >>

\* A record beyond the next sequence number is a gap. The lease's
\* processing stops, and an operator rebuilds it. A member declares one only
\* with the commit version it read, as it commits: one another member
\* overtook has fallen behind the stored progress, and re-reads first.
Gap(m) ==
    /\ m = holder
    /\ mem[m].loaded
    /\ ~gap
    /\ pos[m] <= Len(log)
    /\ mem[m].S = NoS
    /\ log[pos[m]].seq > mem[m].prog + 1
    /\ ver = mem[m].ver
    /\ gap' = TRUE
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, members, alert >>

\* The fence tick. S is the highest owner sequence number applied before
\* it, unless the member loaded a stored S. It stores S with its next commit.
ApplyTick(m) ==
    /\ m = holder
    /\ mem[m].loaded
    /\ ~gap
    /\ pos[m] <= Len(log)
    /\ log[pos[m]].k = "tick"
    /\ mem' = [mem EXCEPT ![m].S = IF @ = NoS THEN mem[m].prog ELSE @,
                          ![m].dirty = @ \/ mem[m].S = NoS]
    /\ pos' = [pos EXCEPT ![m] = @ + 1]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, dpos, done, crashes, ghosts >>

\* Once S is stored, the member books the drain log in order.
ApplyRow(m) ==
    /\ m = holder
    /\ mem[m].loaded
    /\ mem[m].wl
    /\ ~gap
    /\ st = "draining"
    /\ S # NoS
    /\ pos[m] = Len(log) + 1
    /\ dpos[m] <= Len(drain)
    /\ mem' = [mem EXCEPT ![m] = RowAppliedTo(mem[m], drain[dpos[m]])]
    /\ dpos' = [dpos EXCEPT ![m] = @ + 1]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, pos, done, crashes, ghosts >>

\* Once S is stored, the member reaps an open hold at its latest snapshot, by
\* appending a reap row in a transaction that first reads the hold's rows.
\* The transaction is conditional on the commit version, as a commit is: a
\* member another overtook may hold an older snapshot than the one stored.
Reap(m, a) ==
    /\ m = holder
    /\ mem[m].loaded
    /\ mem[m].wl
    /\ ~gap
    /\ st = "draining"
    /\ S # NoS
    /\ pos[m] = Len(log) + 1
    /\ mem[m].holds[a] # NoHold
    /\ mem[m].win[a] = NoWin
    /\ ver = mem[m].ver
    /\ \A i \in DOMAIN drain : drain[i].a # a
    /\ drain' = Append(drain, Rec("reap", a, mem[m].holds[a], 0, Len(drain) + 1))
    /\ UNCHANGED << ownerv, logv, row, appends, pubsub, members, ghosts >>

\* The per-lease commit: conditional on the commit version the member read,
\* with the bookings as arithmetic on the row as it reads it here. A member
\* commits once it has applied something (`dirty`), and every such commit
\* writes the row; a commit that wrote nothing would leave the version too.
Commit(m) ==
    /\ mem[m].loaded
    /\ mem[m].dirty
    /\ ~gap
    /\ ver = mem[m].ver
    /\ ver' = Advanced(mem[m].dirty)
    /\ prog' = mem[m].prog
    /\ booked' = booked + mem[m].dbooked
    /\ alloc' = alloc - mem[m].dbooked
    /\ holds' = mem[m].holds
    /\ win' = [a \in Auths |-> IF mem[m].win[a] # NoWin THEN mem[m].win[a] ELSE win[a]]
    /\ osum' = mem[m].osum
    /\ S' = mem[m].S
    /\ alert' = (alert \/ mem[m].fault)
    /\ mem' = [mem EXCEPT ![m].ver = Advanced(mem[m].dirty), ![m].dbooked = 0, ![m].dirty = FALSE,
                          ![m].fault = FALSE]
    /\ done' = [done EXCEPT ![m] = pos[m] - 1]
    /\ UNCHANGED << ownerv, logv, st, raised, drainv, pubsub, pos, dpos, crashes, gap >>

\* Another member committed since this one read the row, so whatever this
\* one does next with the lease, a commit, a reap or a close, is refused:
\* with many leases in one transaction, its statement matches no row. It
\* drops what it applied, its place in the log and in the drain log among
\* it, and re-reads.
Reread(m) ==
    /\ mem[m].loaded
    /\ ver # mem[m].ver
    /\ mem' = [mem EXCEPT ![m] = Blank]
    /\ pos' = [pos EXCEPT ![m] = acked + 1]
    /\ dpos' = [dpos EXCEPT ![m] = 1]
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, done, crashes, ghosts >>

\* Only what a commit made durable is acknowledged.
Ack(m) ==
    /\ done[m] > acked
    /\ acked' = done[m]
    /\ UNCHANGED << ownerv, logv, row, drainv, holder, assigns, members, ghosts >>

Crash(m) ==
    /\ crashes < MaxCrash
    /\ mem[m].loaded
    /\ mem' = [mem EXCEPT ![m] = Blank]
    /\ pos' = [pos EXCEPT ![m] = acked + 1]
    /\ dpos' = [dpos EXCEPT ![m] = 1]
    /\ crashes' = crashes + 1
    /\ UNCHANGED << ownerv, logv, row, drainv, pubsub, done, ghosts >>

\* The close: after the fence, with every record and row applied and
\* committed, no open hold left, and the drain log read in the same
\* transaction. Conditional on the commit version, like any commit.
Close(m) ==
    /\ m = holder
    /\ st = "draining"
    /\ mem[m].loaded
    /\ mem[m].wl
    /\ ~gap
    /\ mem[m].S # NoS
    /\ pos[m] = Len(log) + 1
    /\ dpos[m] = Len(drain) + 1
    /\ ~mem[m].dirty
    /\ \A a \in Auths : mem[m].holds[a] = NoHold
    /\ ver = mem[m].ver
    /\ st' = "closed"
    /\ ver' = Advanced(st # "closed")
    /\ mem' = [mem EXCEPT ![m].ver = Advanced(st # "closed")]
    /\ UNCHANGED << ownerv, logv, prog, booked, alloc, holds, win, osum, raised, S, drainv, pubsub, pos, dpos, done, crashes, ghosts >>

----------------------------------------------------------------------------

Init ==
    /\ out = << >>
    /\ nextSeq = 1
    /\ osnap = [a \in Auths |-> 0]
    /\ ownerSum = 0
    /\ ownerDone = {}
    /\ log = << >>
    /\ dups = 0
    /\ aheads = 0
    /\ ticked = FALSE
    /\ lates = 0
    /\ st = "open"
    /\ ver = 0
    /\ prog = 0
    /\ booked = 0
    /\ alloc = Grant
    /\ holds = [a \in Auths |-> NoHold]
    /\ win = [a \in Auths |-> NoWin]
    /\ osum = 0
    /\ raised = 0
    /\ S = NoS
    /\ drain = << >>
    /\ appends = 0
    /\ holder = CHOOSE m \in Members : TRUE
    /\ acked = 0
    /\ assigns = 0
    /\ mem = [m \in Members |-> Blank]
    /\ pos = [m \in Members |-> 1]
    /\ dpos = [m \in Members |-> 1]
    /\ done = [m \in Members |-> 0]
    /\ crashes = 0
    /\ alert = FALSE
    /\ gap = FALSE

Next ==
    \/ \E a \in Auths : IssueHeartbeat(a)
    \/ \E a \in Auths, k \in {"settle", "refund"} : IssueTerminal(a, k)
    \/ IssueCheckpoint
    \/ IssueWrongCheckpoint
    \/ Store
    \/ StoreLate
    \/ StoreAhead
    \/ \E i \in 1..(MaxSeq + MaxDup + 1) : StoreAgain(i)
    \/ Raise
    \/ MarkDraining
    \/ \E a \in Auths : FrontDoorAppend(a)
    \/ FenceTick
    \/ \E m \in Members :
          \/ Assign(m)
          \/ Load(m)
          \/ LoadWinners(m)
          \/ ApplyRecord(m)
          \/ SkipRecord(m)
          \/ Gap(m)
          \/ ApplyTick(m)
          \/ ApplyRow(m)
          \/ \E a \in Auths : Reap(m, a)
          \/ Commit(m)
          \/ Reread(m)
          \/ Ack(m)
          \/ Crash(m)
          \/ Close(m)

\* What a member does with a lease's records and row.
Works(m) ==
    \/ Load(m)
    \/ LoadWinners(m)
    \/ ApplyRecord(m)
    \/ SkipRecord(m)
    \/ Gap(m)
    \/ ApplyTick(m)
    \/ ApplyRow(m)
    \/ \E a \in Auths : Reap(m, a)
    \/ Commit(m)
    \/ Reread(m)
    \/ Close(m)

\* The auditor keeps working. Nothing promises that the owner, the log or a
\* front door makes progress.
Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(FenceTick)
    /\ \A m \in Members : WF_vars(Works(m))

----------------------------------------------------------------------------
\* Claims

MaxC == SettleCharge * Cardinality(Auths) + MaxSnap + 1
Recs == [k : {"hb", "settle", "refund", "ckpt", "reap", "tick", "none"},
         a : Auths \cup {NoAuth}, c : 0..MaxC,
         seq : 0..MaxSeq, idx : 0..(MaxAppend + Cardinality(Auths))]

\* The model's bounds. Each record a member applies, in each of the
\* memories it can have, commits at most once, and the close once more.
MaxVer == (MaxSeq + MaxDup + 1 + MaxAppend + Cardinality(Auths)) * (2 + 2 * MaxAssign + MaxCrash) + 1

TypeOK ==
    /\ out \in Seq(Recs) /\ log \in Seq(Recs) /\ drain \in Seq(Recs)
    /\ Len(out) <= MaxSeq
    /\ Len(log) <= MaxSeq + MaxDup + 1
    /\ lates \in 0..MaxLate
    /\ Len(drain) <= MaxAppend + Cardinality(Auths)
    /\ nextSeq \in 1..(MaxSeq + 1)
    /\ osnap \in [Auths -> 0..MaxSnap]
    /\ ownerDone \subseteq Auths
    /\ dups \in 0..MaxDup /\ aheads \in 0..MaxAhead /\ ticked \in BOOLEAN /\ raised \in 0..MaxRaise
    /\ appends \in 0..MaxAppend /\ assigns \in 0..MaxAssign /\ crashes \in 0..MaxCrash
    /\ st \in {"open", "draining", "closed"}
    /\ ver \in 0..MaxVer /\ prog \in 0..MaxSeq /\ booked \in Nat /\ alloc \in Int
    /\ holds \in [Auths -> {NoHold} \cup 0..MaxSnap]
    /\ S \in {NoS} \cup 0..MaxSeq
    /\ win \in [Auths -> Recs]
    /\ holder \in Members
    /\ acked \in 0..Len(log)
    /\ \A m \in Members : pos[m] \in 1..(Len(log) + 1) /\ dpos[m] \in 1..(Len(drain) + 1)
    /\ alert \in BOOLEAN /\ gap \in BOOLEAN

\* The boundary S as section 4.8 defines it: the highest owner sequence
\* number up to which every record was received before the fence tick.
\* Before the tick, every record the log stores is at or below it.
TickAt == IF ticked THEN Min({ i \in DOMAIN log : log[i].k = "tick" }) ELSE 0
SeqsBefore == { log[i].seq : i \in { j \in 1..(TickAt - 1) : log[j].k # "tick" } }
Bound ==
    IF ~ticked THEN MaxSeq
    ELSE CHOOSE k \in 0..MaxSeq : 1..k \subseteq SeqsBefore /\ k + 1 \notin SeqsBefore

\* The owner's records the claims are about: those at or below S. One above
\* S is ignored whenever it arrives.
Accepted(r) == r.seq <= Bound

OwnerTerms(a) == { r \in Range(log) : OwnerTerminal(r) /\ r.a = a /\ Accepted(r) }
RowsFor(a) == { i \in DOMAIN drain : drain[i].a = a }
Heartbeats(a) == { r \in Range(log) : r.k = "hb" /\ r.a = a /\ Accepted(r) }

\* The stored S is the boundary the order of receipt defines.
BoundaryIsS == S # NoS => S = Bound

\* S is stored before the drain log is reaped or booked: a reap row, and a
\* winner from the drain log, stored or decided in a member's memory, exist
\* only once S does.
StoredSFirst ==
    /\ \A i \in DOMAIN drain : drain[i].k = "reap" => S # NoS
    /\ \A a \in Auths : win[a].idx > 0 => S # NoS
    /\ \A m \in Members, a \in Auths : mem[m].win[a].idx > 0 => S # NoS

\* A gap stops the lease only where the log has one: before the fence tick,
\* an owner record it stored before one the owner issued before it.
StoredAhead ==
    \E i \in DOMAIN log :
        /\ ~ticked \/ i < TickAt
        /\ log[i].k # "tick"
        /\ \E s \in 1..(log[i].seq - 1) : \A k \in 1..(i - 1) : log[k].seq # s
GapIsReal == gap => StoredAhead

\* The lease's order: the owner's records, then the drain log. The owner
\* issues one terminal per authorization, and a record stored twice is the
\* same record, so OwnerTerms(a) has at most one element.
FirstInOrder(a) ==
    IF OwnerTerms(a) # {} THEN CHOOSE r \in OwnerTerms(a) : TRUE
    ELSE IF RowsFor(a) # {} THEN drain[Min(RowsFor(a))]
    ELSE NoWin

WinnerIsFirst == \A a \in Auths : win[a] # NoWin => win[a] = FirstInOrder(a)

BookedIsWinners == booked = SumOver([a \in Auths |-> win[a].c], Auths)

NoRaiseLost == alloc = Grant + raised - booked

LastSnap(a) ==
    IF Heartbeats(a) = {} THEN 0
    ELSE (CHOOSE r \in Heartbeats(a) : \A q \in Heartbeats(a) : q.seq <= r.seq).c

ReapAtLastSnapshot ==
    \A i \in DOMAIN drain : drain[i].k = "reap" => drain[i].c = LastSnap(drain[i].a)

NoChargeLost ==
    st = "closed" =>
        \A a \in Auths :
            (OwnerTerms(a) # {} \/ RowsFor(a) # {} \/ Heartbeats(a) # {}) => win[a] # NoWin

\* The audit's reference: the owner's terminals in the log with lower
\* sequence numbers, each record once.
TermsBelow(n) == { r \in Range(log) : OwnerTerminal(r) /\ r.seq < n }
BadCheckpoint(r) ==
    r.k = "ckpt" /\ r.c # SumOver([q \in TermsBelow(r.seq) |-> q.c], TermsBelow(r.seq))

\* The checkpoints a commit stores the application of: those its progress
\* passes.
Committed(r) == r.k = "ckpt" /\ prog < r.seq /\ r.seq <= prog'

\* The audit at each checkpoint, raised with the commit that stores it, as
\* every write a member makes is: a commit that stores a wrong checkpoint at
\* or below S raises the alert, and nothing else raises it.
AuditsEachCheckpoint ==
    [][/\ prog' # prog =>
            alert' = (alert \/ \E r \in Range(log) : Committed(r) /\ Accepted(r) /\ BadCheckpoint(r))
       /\ alert' # alert => prog' # prog]_vars

\* A draining lease closes, unless a gap stopped it for an operator: no
\* member is left with nothing it can do.
DrainingLeaseCloses == st = "draining" ~> (st = "closed" \/ gap)

=============================================================================
