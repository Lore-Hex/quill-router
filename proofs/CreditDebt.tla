----------------------------- MODULE CreditDebt -----------------------------
(***************************************************************************)
(* Money across leases and credit shards, from docs/design/fast-admission-  *)
(* and-batched-settlement.md sections 4.2 and 4.7: grants under the         *)
(* allowance, a settle above its hold and the shortfall it leaves, returns, *)
(* covering a negative shard, the debt mark, and money coming in.           *)
(*                                                                          *)
(* This is written before the code. Nothing implements it yet.              *)
(*                                                                          *)
(* The question it answers: is every hold that can still charge backed by   *)
(* money Spanner has reserved for its lease? A lease is an amount reserved  *)
(* on a credit shard. Its owner admits holds against it in memory and       *)
(* decides their terminals, which may charge more than the hold. The        *)
(* auditor books what the log holds, later. A front door records what an    *)
(* owner could not take. Each of the three changes the lease's allocation,  *)
(* and none waits for another.                                              *)
(*                                                                          *)
(* ACTORS                                                                   *)
(*                                                                          *)
(*   Spanner. The credit rows (credits, usage, reserved and the debt mark,  *)
(*   per shard), and the lease rows (state, donor shard, allocation, what   *)
(*   is booked, and the shortfall total stored). Each action that writes    *)
(*   them is one transaction.                                               *)
(*                                                                          *)
(*   Owners, one per lease. An owner admits a hold when its books have      *)
(*   room, decides each hold's terminal, and gives each record its place    *)
(*   in the lease's one order in the same step. It may be cut off and       *)
(*   come back, or die.                                                     *)
(*                                                                          *)
(*   The log. It stores an owner's records in the order they were decided,  *)
(*   and may never store the last of them.                                  *)
(*                                                                          *)
(*   Front doors. A terminal for a hold whose owner cannot be reached       *)
(*   becomes a row in the drain log, with its raise.                        *)
(*                                                                          *)
(*   The auditor. It books stored records in order, storing each one's      *)
(*   shortfall total first. Once the lease drains it stores the boundary,   *)
(*   books the drain rows that win, reaps what is left, and closes.         *)
(*                                                                          *)
(*   Python's synchronous path, and a payment.                              *)
(*                                                                          *)
(* WHAT IS ABSTRACTED                                                       *)
(*                                                                          *)
(*   - Every hold's estimate is one unit. A terminal charges any amount in  *)
(*     Charges; a reap charges any amount in Reaps, which is at most one.   *)
(*   - A lease has one donor shard. The design spreads an allocation over   *)
(*     several; the identity is per shard either way.                       *)
(*   - Time. When a lease expires, when its boundary may be stored and      *)
(*     when it may close are LeaseLifecycle's and TerminalOrder's. Here a   *)
(*     lease whose owner is unreachable may be marked draining at any       *)
(*     moment, and the boundary may fall before any unstored record.        *)
(*   - Which terminal wins is TerminalOrder's. Here an owner's record that  *)
(*     is stored wins over a drain row for the same hold, and a hold has    *)
(*     at most one of each.                                                 *)
(*   - The auditor applies whatever the log holds. The gap rule, which      *)
(*     stops a lease whose records are out of order, is AuditorCommit's:    *)
(*     here a broken order misbooks, so that its mutant shows what the      *)
(*     order is for.                                                        *)
(*   - An owner learns that a record is stored at the moment it is. A real  *)
(*     owner learns later, and so lets freed room again later, never        *)
(*     sooner.                                                              *)
(*   - The buffer for expected overruns is left out. It is for load: no     *)
(*     claim reads it, and an owner with no buffer is the worst case.       *)
(*   - The state cache. An owner here admits whether or not its workspace   *)
(*     is in debt, which is the cache's window at its widest. A grant and   *)
(*     a synchronous reservation read the mark in Spanner.                  *)
(*                                                                          *)
(* ASSUMPTIONS, each with a mutant that widens it (proofs/manifest.toml)    *)
(*                                                                          *)
(*   A1. The log stores a lease's records in the order its owner decided    *)
(*       them: they share one ordering key. What the auditor does with a    *)
(*       gap is AuditorCommit's. Mutant store-out-of-order.                 *)
(*   A2. A reap charges at most its hold. A heartbeat's running charge is   *)
(*       capped at the hold (section 4.8), and a reap charges the last one. *)
(*       Mutant reap-above-the-hold.                                        *)
(*                                                                          *)
(* THE TWO CLAIMS OF SECTION 4.2                                            *)
(*                                                                          *)
(*   HoldsCoveredInSpanner. A lease's remaining allocation in Spanner is    *)
(*   never less than its open holds, where a hold is open until the log     *)
(*   holds its terminal. It is stated with everything stored counted as     *)
(*   booked, which is the stronger form. Writing it from the design's       *)
(*   first wording ("until a terminal is applied") failed in seven steps,   *)
(*   and the design was corrected.                                          *)
(*                                                                          *)
(*   It must not lean on the owner's own shortfall write. The variant       *)
(*   configuration `silent` has an owner that never writes, and the claim   *)
(*   holds there. The auditor's raise is what keeps it: the mutant that     *)
(*   removes that raise runs against `silent`.                              *)
(*                                                                          *)
(*   StoredCoversOwner and ShortfallIsBeingWritten. What Spanner has        *)
(*   reserved for a lease is never below what its owner counts on, except   *)
(*   by a shortfall the owner knows and Spanner does not yet; and that      *)
(*   lasts only while the owner's write is in flight, or the owner is cut   *)
(*   off or dead.                                                           *)
(*                                                                          *)
(*   ShortfallLandsWithinTwoWrites. And for how long: within one of the     *)
(*   owner's writes landing, or two when a write was already in flight. The *)
(*   write after one in flight carries the owner's total as it then is.     *)
(*   The history variable `due` counts the landings left.                   *)
(*                                                                          *)
(*   OwnerBooksBalance and ShortfallIsTheDeficit. A settle the owner's      *)
(*   allocation has no room for raises the allocation by the shortfall it   *)
(*   leaves, and by exactly that: the owner's books are left with no room.  *)
(*   The shortfall is the lease's deficit, not a settle's overrun of its    *)
(*   own hold.                                                              *)
(*                                                                          *)
(*   StoredShortfallNeverFalls, StoredShortfallWithinOwners and             *)
(*   StoredShortfallIsTheLarger. The owner's write is the larger of the     *)
(*   stored total and its own: so a write that lands after the auditor      *)
(*   stored a later total changes nothing, the stored total is never more   *)
(*   than the owner's, and a landing, whether or not its owner is still     *)
(*   reachable, or the auditor's applying a terminal, stores exactly the    *)
(*   larger of the stored total and the one it carries. A write is given    *)
(*   up, storing nothing, only once its owner is cut off or dead or the     *)
(*   lease has closed. A write that lowered the total would not show in     *)
(*   StoredCoversOwner, which counts the owner's own total as not yet       *)
(*   landed.                                                                *)
(*                                                                          *)
(*   AllocationAccounted. A lease's allocation is its grant, plus the       *)
(*   shortfall total stored and the front doors' raises, less the returns   *)
(*   applied (the history variable `returned`). Every raise of the          *)
(*   allocation, and of the reservation behind it, is one of those.         *)
(*                                                                          *)
(* THE CLAIMS OF SECTION 4.7 AND INVARIANT 11                               *)
(*                                                                          *)
(*   ShardIdentity, per credit shard. BookedOnce: each hold is booked once, *)
(*   at the charge its terminal names, which the auditor does not choose.   *)
(*   UsageIsBooked: a shard's usage is the charges booked under the leases  *)
(*   it donated to and its synchronous settles' (the history variable       *)
(*   `synced`).                                                             *)
(*   DebtMarksEveryShard and MarkMeansDebt: debt marks every row, and a     *)
(*   workspace no longer in debt is not left marked.                        *)
(*   MarkRefusesReservations: a marked row takes no reservation, and a      *)
(*   marked workspace no grant.                                             *)
(*   RepaysDebtFirst: money coming in, a payment or a reservation a return, *)
(*   a close or a settle frees, goes to the negative shards first, lowest   *)
(*   first and each at most to zero, even when the workspace stays in debt. *)
(*   CreditConserved: covering makes and loses no credit.                   *)
(*                                                                          *)
(* THE CONFIGURATIONS                                                       *)
(*                                                                          *)
(*   The main one has one lease and two holds. Each variant is there for a  *)
(*   hazard the main bounds leave out: `silent` (no owner write), `refund`  *)
(*   (a third hold), `money` (Python's synchronous path, a second           *)
(*   reservation and a payment smaller than the debt) and `two` (a second   *)
(*   lease, for the trust allowance). Each .cfg says what its own bounds    *)
(*   leave out.                                                             *)
(***************************************************************************)

EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    NShards,      \* credit shards of the workspace
    Leases,       \* leases that may be granted
    CreditEach,   \* credit on each shard at the start
    LeaseSize,    \* L, in holds: every hold's estimate is one unit
    Allowance,    \* the trust tier's allowance
    Floor,        \* headroom a grant leaves outside leases
    Charges,      \* what a settle may charge for a hold
    Reaps,        \* what a reap may charge: a heartbeat's running charge
    MaxHolds,     \* fast admissions in a behaviour
    MaxSync,      \* synchronous reservations in a behaviour
    MaxPays,      \* payments in a behaviour
    PaySize,      \* the size of each
    MaxCuts,      \* times an owner is cut off from Spanner and front doors
    OwnerWrites   \* whether an owner stores its shortfall itself

Shards == 1..NShards
Holds == 1..MaxHolds
NoWrite == 0
NoLease == "none"

VARIABLES
    credits, usage, reserved, mark,       \* Spanner: the credit rows
    sync,                                 \* Spanner: open synchronous holds
    synced,     \* per shard: what synchronous settles charged (history)
    st, donor, alloc, booked, sfStored,   \* Spanner: the lease rows
    sealed,     \* Spanner: the lease's owner boundary is stored
    hold,       \* each hold: its lease, and what has been decided for it
    out,        \* per lease: records the owner decided, not yet stored
    log,        \* per lease: stored records the auditor has not applied
    oUp, oStop, oAlloc, oSf,   \* the owner's memory, beyond its holds
    write,      \* the owner's shortfall write in flight: the total it carries
    due,        \* per lease: the owner's writes that may land before Spanner
                \* holds its total, 0 when none is owed (a history variable)
    returned,   \* per lease: the returns the auditor has applied (history)
    syncs, pays, cuts

spanner == << credits, usage, reserved, mark, sync, synced >>
leaseRows == << st, donor, alloc, booked, sfStored, sealed, returned >>
owner == << oUp, oStop, oAlloc, oSf >>
bounds == << syncs, pays, cuts >>
vars == << spanner, leaseRows, hold, out, log, owner, write, due, bounds >>

----------------------------------------------------------------------------
\* Helpers

RECURSIVE SumTo(_, _)
SumTo(f, n) == IF n = 0 THEN 0 ELSE f[n] + SumTo(f, n - 1)
Total(f) == SumTo(f, NShards)

RECURSIVE SumOver(_, _)
SumOver(f, set) ==
    IF set = {} THEN 0
    ELSE LET x == CHOOSE y \in set : TRUE IN f[x] + SumOver(f, set \ {x})

Pos(n) == IF n > 0 THEN n ELSE 0
Max(a, b) == IF a >= b THEN a ELSE b

Room(c, u, r) == [s \in Shards |-> c[s] - u[s] - r[s]]
RoomNow == Room(credits, usage, reserved)

Live(l) == st[l] \in {"open", "draining"}
Remaining(l) == alloc[l] - booked[l]

Mine(l) == { h \in Holds : hold[h].lease = l }
\* A terminal has been applied for the hold: the auditor has booked one.
Applied(h) == hold[h].own = "applied" \/ hold[h].row = "applied" \/ hold[h].reaped
\* Open in the log's sense: admitted, and no terminal that the lease's order
\* will keep. A stored owner record is kept, applied or not. A decided record
\* that is not stored may never be. A drain row may yet lose to one.
OpenLog(l) == { h \in Mine(l) : ~Applied(h) /\ hold[h].own # "log" }
\* Charges the log holds and the auditor has not booked, and the shortfall
\* its commit will store before it books them.
RECURSIVE LastSf(_, _)
LastSf(q, floor) ==
    IF q = << >> THEN floor
    ELSE LastSf(Tail(q), IF Head(q).k = "term" /\ Head(q).sf > floor THEN Head(q).sf ELSE floor)
RaiseToCome(l) == LastSf(log[l], sfStored[l]) - sfStored[l]
ReturnsToCome(l) ==
    LET q == log[l] IN
    SumOver([i \in 1..Len(q) |-> IF q[i].k = "ret" THEN q[i].a ELSE 0], 1..Len(q))
StoredUnbooked(l) ==
    SumOver([h \in Holds |-> hold[h].oa], { h \in Mine(l) : hold[h].own = "log" })

\* The owner's books, from what it has decided.
OHeld(l) == Cardinality({ h \in Mine(l) : hold[h].own = "none" })
OCons(l) == SumOver([h \in Holds |-> hold[h].oa], { h \in Mine(l) : hold[h].own # "none" })
\* Room that decided terminals freed and whose records are not yet stored.
Pending(l) == SumOver([h \in Holds |-> Pos(1 - hold[h].oa)],
                      { h \in Mine(l) : hold[h].own = "out" })
Remain(l) == oAlloc[l] - OCons(l) - OHeld(l)
Free(l) == Remain(l) - Pending(l)

Rec(k, h, a, sf) == [k |-> k, h |-> h, a |-> a, sf |-> sf]

\* Section 4.7: move credit from shards with headroom to the negative ones,
\* lowest shard first on both sides. Called only when the signed sum is not
\* negative, so a shard with headroom exists while one is negative.
RECURSIVE Cover(_)
Cover(h) ==
    IF \A s \in Shards : h[s] >= 0 THEN h
    ELSE LET neg == CHOOSE s \in Shards :
                        h[s] < 0 /\ \A t \in Shards : h[t] < 0 => s <= t
             pos == CHOOSE s \in Shards :
                        h[s] > 0 /\ \A t \in Shards : h[t] > 0 => s <= t
             x   == IF -h[neg] < h[pos] THEN -h[neg] ELSE h[pos]
         IN Cover([h EXCEPT ![neg] = @ + x, ![pos] = @ - x])

\* What a write to the credit rows ends with (section 4.7). If the signed
\* sum is negative it marks every row. Otherwise it covers every negative
\* shard from the others and clears the mark.
Squared(c, u, r) ==
    LET h == Room(c, u, r) IN
    IF Total(h) < 0
    THEN [c |-> c, m |-> [s \in Shards |-> TRUE]]
    ELSE LET h2 == Cover(h) IN
         [c |-> [s \in Shards |-> c[s] + h2[s] - h[s]],
          m |-> [s \in Shards |-> FALSE]]

\* Money x coming in, as headroom h shows it without x, goes to the negative
\* shards first, lowest first, and what is left to shard s.
RECURSIVE Distribute(_, _, _)
Distribute(h, x, s) ==
    IF x = 0 THEN h
    ELSE IF \A t \in Shards : h[t] >= 0 THEN [h EXCEPT ![s] = @ + x]
    ELSE LET neg == CHOOSE t \in Shards :
                        h[t] < 0 /\ \A v \in Shards : h[v] < 0 => t <= v
             y   == IF x < -h[neg] THEN x ELSE -h[neg]
         IN Distribute([h EXCEPT ![neg] = @ + y], x - y, s)

\* What a write that brings money x in on shard s ends with: a payment's
\* credit, or a reservation a return, a close or a settle frees. Section 4.7:
\* money coming in repays the negative shards first, in the same
\* transaction, and the write is then squared like any other. c, u and r
\* already hold the money on s.
Inflow(c, u, r, s, x) ==
    LET h     == Room(c, u, r)
        after == Distribute([h EXCEPT ![s] = @ - x], x, s)
    IN Squared([t \in Shards |-> c[t] + after[t] - h[t]], u, r)

\* The exposure a grant counts: remaining allocation, lease by lease.
Exposure == SumOver([l \in Leases |-> IF Live(l) THEN Remaining(l) ELSE 0], Leases)

NoHold == [lease |-> NoLease, own |-> "none", oa |-> 0, row |-> "none", ra |-> 0,
           reaped |-> FALSE, b |-> 0]

----------------------------------------------------------------------------

Init ==
    /\ credits = [s \in Shards |-> CreditEach]
    /\ usage = [s \in Shards |-> 0]
    /\ reserved = [s \in Shards |-> 0]
    /\ mark = [s \in Shards |-> FALSE]
    /\ sync = [s \in Shards |-> 0]
    /\ synced = [s \in Shards |-> 0]
    /\ st = [l \in Leases |-> "none"]
    /\ donor = [l \in Leases |-> 1]
    /\ alloc = [l \in Leases |-> 0]
    /\ booked = [l \in Leases |-> 0]
    /\ sfStored = [l \in Leases |-> 0]
    /\ sealed = [l \in Leases |-> FALSE]
    /\ hold = [h \in Holds |-> NoHold]
    /\ out = [l \in Leases |-> << >>]
    /\ log = [l \in Leases |-> << >>]
    /\ oUp = [l \in Leases |-> "up"]
    /\ oStop = [l \in Leases |-> FALSE]
    /\ oAlloc = [l \in Leases |-> 0]
    /\ oSf = [l \in Leases |-> 0]
    /\ write = [l \in Leases |-> NoWrite]
    /\ due = [l \in Leases |-> 0]
    /\ returned = [l \in Leases |-> 0]
    /\ syncs = 0 /\ pays = 0 /\ cuts = 0

----------------------------------------------------------------------------
\* Spanner: a grant

Grant(l, s) ==
    /\ st[l] = "none"
    /\ \A t \in Shards : ~mark[t]
    /\ Exposure + LeaseSize <= Allowance
    /\ Total(RoomNow) - LeaseSize >= Floor
    /\ RoomNow[s] >= LeaseSize
    /\ st' = [st EXCEPT ![l] = "open"]
    /\ donor' = [donor EXCEPT ![l] = s]
    /\ alloc' = [alloc EXCEPT ![l] = LeaseSize]
    /\ reserved' = [reserved EXCEPT ![s] = @ + LeaseSize]
    /\ oAlloc' = [oAlloc EXCEPT ![l] = LeaseSize]
    /\ UNCHANGED << credits, usage, mark, sync, synced, booked, sfStored, sealed, returned, hold,
                    out, log, oUp, oStop, oSf, write, due, bounds >>

----------------------------------------------------------------------------
\* The owner

\* An owner admits, decides and returns only while its lease is open and it
\* can be reached, and after it has adopted every drain row for a hold it
\* has not decided. Each of those actions asks for the three, so that each
\* has a row of its own in the guard table.

Admit(l) ==
    /\ st[l] = "open"
    /\ oUp[l] = "up"
    /\ \A g \in Mine(l) : ~(hold[g].row = "row" /\ hold[g].own = "none")
    /\ ~oStop[l]
    /\ Free(l) >= 1
    /\ \E h \in Holds :
        /\ hold[h].lease = NoLease
        /\ \A g \in Holds : hold[g].lease = NoLease => h <= g
        /\ hold' = [hold EXCEPT ![h] = [NoHold EXCEPT !.lease = l]]
    /\ UNCHANGED << spanner, leaseRows, out, log, owner, write, due, bounds >>

\* A terminal the owner decides for hold h, charged a. Deciding it, giving
\* its record its place in the lease's order and moving the books are one
\* step. `raise` is a front door's raise that an adopted row brings with it.
Decide(l, h, a, raise) ==
    /\ LET cons  == OCons(l) + a
           held  == OHeld(l) - 1
           mine  == oAlloc[l] + raise
           short == Pos(cons + held - mine)
           sf    == oSf[l] + short
       IN /\ hold' = [hold EXCEPT ![h].own = "out", ![h].oa = a]
          /\ oAlloc' = [oAlloc EXCEPT ![l] = mine + short]
          /\ oSf' = [oSf EXCEPT ![l] = sf]
          /\ out' = [out EXCEPT ![l] = Append(@, Rec("term", h, a, sf))]
          /\ write' = [write EXCEPT ![l] =
                           IF OwnerWrites /\ short > 0 /\ @ = NoWrite THEN sf ELSE @]
          /\ due' = [due EXCEPT ![l] =
                         IF OwnerWrites /\ short > 0
                         THEN (IF write[l] = NoWrite THEN 1 ELSE 2) ELSE @]

OwnerSettle(l, h, a) ==
    /\ st[l] = "open"
    /\ oUp[l] = "up"
    /\ \A g \in Mine(l) : ~(hold[g].row = "row" /\ hold[g].own = "none")
    /\ h \in Mine(l)
    /\ hold[h].own = "none"
    /\ Decide(l, h, a, 0)
    /\ UNCHANGED << spanner, leaseRows, log, oUp, oStop, bounds >>

\* The owner takes the drain log's row for a hold it has not decided, and
\* counts the front door's raise as allocation.
OwnerAdopt(l, h) ==
    /\ st[l] = "open"
    /\ oUp[l] = "up"
    /\ h \in Mine(l)
    /\ hold[h].own = "none"
    /\ hold[h].row = "row"
    /\ Decide(l, h, hold[h].ra, Pos(hold[h].ra - 1))
    /\ UNCHANGED << spanner, leaseRows, log, oUp, oStop, bounds >>

\* The owner stops admitting and returns what the lease holds beyond its
\* open holds and its pending room, in a checkpoint record.
OwnerReturn(l) ==
    /\ st[l] = "open"
    /\ oUp[l] = "up"
    /\ \A g \in Mine(l) : ~(hold[g].row = "row" /\ hold[g].own = "none")
    /\ Free(l) > 0
    /\ oAlloc' = [oAlloc EXCEPT ![l] = @ - Free(l)]
    /\ out' = [out EXCEPT ![l] = Append(@, Rec("ret", 0, Free(l), oSf[l]))]
    /\ oStop' = [oStop EXCEPT ![l] = TRUE]
    /\ UNCHANGED << spanner, leaseRows, hold, log, oUp, oSf, write, due, bounds >>

\* Its last hold has ended and its records are stored: it marks the lease
\* draining.
OwnerFinish(l) ==
    /\ st[l] = "open"
    /\ oUp[l] = "up"
    /\ \A g \in Mine(l) : ~(hold[g].row = "row" /\ hold[g].own = "none")
    /\ OHeld(l) = 0
    /\ out[l] = << >>
    /\ st' = [st EXCEPT ![l] = "draining"]
    /\ oStop' = [oStop EXCEPT ![l] = TRUE]
    /\ UNCHANGED << spanner, donor, alloc, booked, sfStored, sealed, returned, hold, out,
                    log, oUp, oAlloc, oSf, write, due, bounds >>

\* The owner's shortfall write: the larger of the stored total and its own.
OwnerWriteLands(l) ==
    /\ write[l] # NoWrite
    /\ Live(l)
    /\ LET rise == Pos(write[l] - sfStored[l])
           r2   == [reserved EXCEPT ![donor[l]] = @ + rise]
           sq   == Squared(credits, usage, r2)
       IN /\ sfStored' = [sfStored EXCEPT ![l] = @ + rise]
          /\ alloc' = [alloc EXCEPT ![l] = @ + rise]
          /\ reserved' = r2
          /\ credits' = sq.c
          /\ mark' = sq.m
    /\ write' = [write EXCEPT ![l] =
                     IF oUp[l] = "up" /\ oSf[l] > write[l] THEN oSf[l] ELSE NoWrite]
    /\ due' = [due EXCEPT ![l] = Pos(@ - 1)]
    /\ UNCHANGED << usage, sync, synced, st, donor, booked, sealed, returned, hold, out, log,
                    owner, bounds >>

\* A write is given up only when its owner cannot retry it, or the lease has
\* closed and Spanner refuses it.
OwnerWriteEnds(l) ==
    /\ write[l] # NoWrite
    /\ oUp[l] # "up" \/ st[l] = "closed"
    /\ write' = [write EXCEPT ![l] = NoWrite]
    /\ due' = [due EXCEPT ![l] = 0]
    /\ UNCHANGED << spanner, leaseRows, hold, out, log, owner, bounds >>

OwnerCut(l) ==
    /\ Live(l)
    /\ oUp[l] = "up"
    /\ cuts < MaxCuts
    /\ oUp' = [oUp EXCEPT ![l] = "cut"]
    /\ cuts' = cuts + 1
    /\ UNCHANGED << spanner, leaseRows, hold, out, log, oStop, oAlloc, oSf,
                    write, due, syncs, pays >>

\* It reads its lease row again, and retries a shortfall Spanner lacks.
OwnerReconnect(l) ==
    /\ oUp[l] = "cut"
    /\ oUp' = [oUp EXCEPT ![l] = "up"]
    /\ write' = [write EXCEPT ![l] =
                     IF OwnerWrites /\ Live(l) /\ oSf[l] > sfStored[l]
                         THEN oSf[l] ELSE NoWrite]
    /\ due' = [due EXCEPT ![l] =
                   IF OwnerWrites /\ Live(l) /\ oSf[l] > sfStored[l] THEN 1 ELSE 0]
    /\ UNCHANGED << spanner, leaseRows, hold, out, log, oStop, oAlloc, oSf,
                    bounds >>

OwnerDie(l) ==
    /\ Live(l)
    /\ oUp[l] # "dead"
    /\ oUp' = [oUp EXCEPT ![l] = "dead"]
    /\ UNCHANGED << spanner, leaseRows, hold, out, log, oStop, oAlloc, oSf,
                    write, due, bounds >>

----------------------------------------------------------------------------
\* The log

\* The log stores the owner's next record. Records are stored in the order
\* they were decided, whether or not the owner still lives; what the lease's
\* boundary finds unstored is never stored.
NextToStore(q) == Head(q)
AfterStoring(q) == Tail(q)

Store(l) ==
    /\ out[l] # << >>
    /\ ~sealed[l]
    /\ LET rec == NextToStore(out[l]) IN
       /\ log' = [log EXCEPT ![l] = Append(@, rec)]
       /\ hold' = IF rec.k = "term" THEN [hold EXCEPT ![rec.h].own = "log"] ELSE hold
    /\ out' = [out EXCEPT ![l] = AfterStoring(@)]
    /\ UNCHANGED << spanner, leaseRows, owner, write, due, bounds >>

----------------------------------------------------------------------------
\* A front door: a terminal for a hold whose owner cannot take it.

FrontDoorAppend(l, h, a) ==
    /\ Live(l)
    /\ oUp[l] # "up" \/ st[l] = "draining"
    /\ h \in Mine(l)
    /\ hold[h].row = "none"
    /\ hold[h].own \in {"none", "out"}
    /\ ~hold[h].reaped
    /\ LET over == Pos(a - 1)
           r2   == [reserved EXCEPT ![donor[l]] = @ + over]
           sq   == Squared(credits, usage, r2)
       IN /\ alloc' = [alloc EXCEPT ![l] = @ + over]
          /\ reserved' = r2
          /\ credits' = sq.c
          /\ mark' = sq.m
    /\ hold' = [hold EXCEPT ![h].row = "row", ![h].ra = a]
    /\ UNCHANGED << usage, sync, synced, st, donor, booked, sfStored, sealed, returned, out, log,
                    owner, write, due, bounds >>

----------------------------------------------------------------------------
\* The auditor

\* A lease whose owner cannot renew expires.
MarkDraining(l) ==
    /\ st[l] = "open"
    /\ oUp[l] # "up"
    /\ st' = [st EXCEPT ![l] = "draining"]
    /\ UNCHANGED << spanner, donor, alloc, booked, sfStored, sealed, returned, hold, out,
                    log, owner, write, due, bounds >>

\* What booking a charge does to the lease's donor shard: matched, so that
\* the shard's headroom does not move.
Book(l, a, rise) ==
    /\ LET d  == donor[l]
           r2 == [reserved EXCEPT ![d] = @ + rise - a]
           u2 == [usage EXCEPT ![d] = @ + a]
           sq == Squared(credits, u2, r2)
       IN /\ sfStored' = [sfStored EXCEPT ![l] = @ + rise]
          /\ alloc' = [alloc EXCEPT ![l] = @ + rise]
          /\ booked' = [booked EXCEPT ![l] = @ + a]
          /\ reserved' = r2
          /\ usage' = u2
          /\ credits' = sq.c
          /\ mark' = sq.m

\* The next stored record. A terminal's shortfall total is stored first, then
\* its charge is booked.
AuditorApply(l) ==
    /\ Live(l)
    /\ log[l] # << >>
    /\ LET rec == Head(log[l]) IN
       IF rec.k = "term"
       THEN /\ Book(l, rec.a, Pos(rec.sf - sfStored[l]))
            /\ hold' = [hold EXCEPT ![rec.h].own = "applied", ![rec.h].b = rec.a]
            /\ UNCHANGED returned
       ELSE LET r2 == [reserved EXCEPT ![donor[l]] = @ - rec.a]
                sq == Inflow(credits, usage, r2, donor[l], rec.a)
            IN /\ alloc' = [alloc EXCEPT ![l] = @ - rec.a]
               /\ reserved' = r2
               /\ credits' = sq.c
               /\ mark' = sq.m
               /\ returned' = [returned EXCEPT ![l] = @ + rec.a]
               /\ UNCHANGED << usage, booked, sfStored, hold >>
    /\ log' = [log EXCEPT ![l] = Tail(@)]
    /\ UNCHANGED << sync, synced, st, donor, sealed, out, owner, write, due, bounds >>

\* The owner boundary: every stored record is applied, and a record not
\* stored by now is ignored for good.
Seal(l) ==
    /\ st[l] = "draining"
    /\ ~sealed[l]
    /\ log[l] = << >>
    /\ sealed' = [sealed EXCEPT ![l] = TRUE]
    /\ out' = [out EXCEPT ![l] = << >>]
    /\ hold' = [h \in Holds |->
                   IF hold[h].lease = l /\ hold[h].own = "out"
                       THEN [hold[h] EXCEPT !.own = "lost"] ELSE hold[h]]
    /\ UNCHANGED << spanner, st, donor, alloc, booked, sfStored, returned, log,
                    owner, write, due, bounds >>

\* A drain-log row, after the boundary. It loses to an owner terminal that
\* was applied; otherwise it is booked. Its raise was made by its append.
AuditorApplyRow(l, h) ==
    /\ sealed[l]
    /\ Live(l)
    /\ h \in Mine(l)
    /\ hold[h].row = "row"
    /\ IF hold[h].own = "applied"
       THEN /\ hold' = [hold EXCEPT ![h].row = "lost"]
            /\ UNCHANGED << spanner, alloc, booked, sfStored >>
       ELSE /\ Book(l, hold[h].ra, 0)
            /\ hold' = [hold EXCEPT ![h].row = "applied", ![h].b = hold[h].ra]
            /\ UNCHANGED << sync, synced >>
    /\ UNCHANGED << st, donor, sealed, returned, out, log, owner, write, due, bounds >>

\* A hold with no terminal anywhere is reaped at its last heartbeat's
\* running charge.
AuditorReap(l, h, a) ==
    /\ sealed[l]
    /\ Live(l)
    /\ h \in Mine(l)
    /\ ~Applied(h)
    /\ hold[h].row = "none"
    /\ Book(l, a, 0)
    /\ hold' = [hold EXCEPT ![h].reaped = TRUE, ![h].b = a]
    /\ UNCHANGED << sync, synced, st, donor, sealed, returned, out, log, owner, write, due, bounds >>

\* Close releases what the lease still holds.
Close(l) ==
    /\ sealed[l]
    /\ st[l] = "draining"
    /\ \A h \in Mine(l) : Applied(h) /\ hold[h].row # "row"
    /\ LET r2 == [reserved EXCEPT ![donor[l]] = @ - Remaining(l)]
           sq == Inflow(credits, usage, r2, donor[l], Remaining(l))
       IN /\ reserved' = r2
          /\ credits' = sq.c
          /\ mark' = sq.m
    /\ alloc' = [alloc EXCEPT ![l] = booked[l]]
    /\ st' = [st EXCEPT ![l] = "closed"]
    /\ UNCHANGED << usage, sync, synced, donor, booked, sfStored, sealed, returned, hold, out,
                    log, owner, write, due, bounds >>

----------------------------------------------------------------------------
\* Python's synchronous path, and money coming in

SyncReserve(s) ==
    /\ syncs < MaxSync
    /\ ~mark[s]
    /\ RoomNow[s] >= 1
    /\ reserved' = [reserved EXCEPT ![s] = @ + 1]
    /\ sync' = [sync EXCEPT ![s] = @ + 1]
    /\ syncs' = syncs + 1
    /\ UNCHANGED << credits, usage, mark, synced, leaseRows, hold, out, log, owner,
                    write, due, pays, cuts >>

SyncSettle(s, a) ==
    /\ sync[s] >= 1
    /\ LET r2 == [reserved EXCEPT ![s] = @ - 1]
           u2 == [usage EXCEPT ![s] = @ + a]
           sq == Inflow(credits, u2, r2, s, Pos(1 - a))
       IN /\ reserved' = r2
          /\ usage' = u2
          /\ credits' = sq.c
          /\ mark' = sq.m
    /\ sync' = [sync EXCEPT ![s] = @ - 1]
    /\ synced' = [synced EXCEPT ![s] = @ + a]
    /\ UNCHANGED << leaseRows, hold, out, log, owner, write, due, bounds >>

Pay ==
    /\ pays < MaxPays
    /\ LET c2 == [credits EXCEPT ![1] = @ + PaySize]
           sq == Inflow(c2, usage, reserved, 1, PaySize)
       IN /\ credits' = sq.c
          /\ mark' = sq.m
    /\ pays' = pays + 1
    /\ UNCHANGED << usage, reserved, sync, synced, leaseRows, hold, out, log, owner,
                    write, due, syncs, cuts >>

----------------------------------------------------------------------------

Next ==
    \/ \E l \in Leases, s \in Shards : Grant(l, s)
    \/ \E l \in Leases : Admit(l)
    \/ \E l \in Leases, h \in Holds, a \in Charges : OwnerSettle(l, h, a)
    \/ \E l \in Leases, h \in Holds : OwnerAdopt(l, h)
    \/ \E l \in Leases : OwnerReturn(l)
    \/ \E l \in Leases : OwnerFinish(l)
    \/ \E l \in Leases : OwnerWriteLands(l)
    \/ \E l \in Leases : OwnerWriteEnds(l)
    \/ \E l \in Leases : OwnerCut(l)
    \/ \E l \in Leases : OwnerReconnect(l)
    \/ \E l \in Leases : OwnerDie(l)
    \/ \E l \in Leases : Store(l)
    \/ \E l \in Leases, h \in Holds, a \in Charges : FrontDoorAppend(l, h, a)
    \/ \E l \in Leases : MarkDraining(l)
    \/ \E l \in Leases : AuditorApply(l)
    \/ \E l \in Leases : Seal(l)
    \/ \E l \in Leases, h \in Holds : AuditorApplyRow(l, h)
    \/ \E l \in Leases, h \in Holds, a \in Reaps : AuditorReap(l, h, a)
    \/ \E l \in Leases : Close(l)
    \/ \E s \in Shards : SyncReserve(s)
    \/ \E s \in Shards, a \in Charges : SyncSettle(s, a)
    \/ Pay

Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
\* Claims

Unlanded(l) == Pos(oSf[l] - sfStored[l])

\* The owner's books: a charge the allocation has no room for raises it.
OwnerBooksBalance ==
    \A l \in Leases : oUp[l] # "dead" => Remain(l) >= 0

\* By exactly what it lacks: when the owner's shortfall total rises, its
\* books are left with no room. The shortfall is the lease's deficit, not a
\* settle's overrun of its own hold.
ShortfallIsTheDeficit ==
    [][\A l \in Leases : oSf'[l] > oSf[l] => Remain(l)' = 0]_vars

\* Section 4.2's first claim. A lease's remaining allocation in Spanner is
\* never less than its open holds, in the log's sense, once everything the
\* log holds is booked: the charges and returns stored and not yet applied
\* come out of it, and the shortfall those records carry goes in. So it
\* holds before a record is applied as well as after.
HoldsCoveredInSpanner ==
    \A l \in Leases : Live(l) =>
        Remaining(l) + RaiseToCome(l) - StoredUnbooked(l) - ReturnsToCome(l)
            >= Cardinality(OpenLog(l))

\* Section 4.2's second claim, in money. What Spanner has reserved for a
\* lease is never below what its owner counts on, except by a shortfall the
\* owner knows and Spanner does not yet.
StoredCoversOwner ==
    \A l \in Leases : Live(l) => alloc[l] + Unlanded(l) >= oAlloc[l]

\* And when that exception may last: only while the owner's write is in
\* flight, or the owner is cut off or dead.
ShortfallIsBeingWritten ==
    OwnerWrites =>
        \A l \in Leases :
            (Live(l) /\ oUp[l] = "up" /\ Unlanded(l) > 0) => write[l] # NoWrite

\* And how long: Spanner holds an owner's shortfall within one of its writes
\* landing, or two when a write was already in flight (section 4.2). The
\* write that follows one in flight carries the owner's total as it then is.
ShortfallLandsWithinTwoWrites ==
    OwnerWrites =>
        \A l \in Leases :
            (Live(l) /\ oUp[l] = "up" /\ due[l] = 0) => Unlanded(l) = 0

\* The larger-of write (section 4.2): a write that lands after the auditor
\* stored a later total, or a repeat of one, changes nothing. So the stored
\* total never falls. StoredCoversOwner does not see a write that lowers it
\* for a while, since the owner's own total then counts as not yet landed.
StoredShortfallNeverFalls ==
    [][\A l \in Leases : sfStored'[l] >= sfStored[l]]_vars

\* And never rises past a total the owner reached: a write lands the larger
\* of the two totals, not their sum.
StoredShortfallWithinOwners ==
    \A l \in Leases : sfStored[l] <= oSf[l]

\* Exactly. When the owner's write in flight ends, it lands, and the stored
\* total becomes the larger of it and the total the write carried, whether
\* or not the owner is still reachable; or, only once the owner is cut off
\* or dead or the lease has closed, it is given up and stores nothing. When
\* the auditor applies a terminal, the stored total becomes the larger of it
\* and the total the record carried. It changes at no other step.
StoredShortfallIsTheLarger ==
    [][\A l \in Leases :
          LET ended   == write[l] # NoWrite /\ write'[l] # write[l]
              applied == log[l] # << >> /\ log'[l] = Tail(log[l]) /\ Head(log[l]).k = "term"
          IN /\ ended =>
                    \/ sfStored'[l] = Max(sfStored[l], write[l])
                    \/ sfStored'[l] = sfStored[l] /\ (oUp[l] # "up" \/ ~Live(l))
             /\ applied => sfStored'[l] = Max(sfStored[l], Head(log[l]).sf)
             /\ sfStored'[l] # sfStored[l] => ended \/ applied]_vars

\* A lease's allocation is its grant, plus the shortfall total stored and
\* the front doors' raises, less the returns applied: every raise of the
\* allocation, and of the reservation behind it, is one of those, at the
\* amount it names.
DoorRaised(l) ==
    SumOver([h \in Holds |-> IF hold[h].row # "none" THEN Pos(hold[h].ra - 1) ELSE 0], Mine(l))
AllocationAccounted ==
    \A l \in Leases :
        Live(l) => alloc[l] = LeaseSize + sfStored[l] + DoorRaised(l) - returned[l]

\* Section 4.7's identity, per credit shard.
ShardIdentity ==
    \A s \in Shards :
        reserved[s] = sync[s]
            + SumOver([l \in Leases |->
                          IF Live(l) /\ donor[l] = s THEN Remaining(l) ELSE 0], Leases)

\* Usage is what was booked: per shard, the charges booked under the leases
\* it donated to, and those of synchronous settles on it.
UsageIsBooked ==
    \A s \in Shards :
        usage[s] = synced[s] + SumOver([l \in Leases |-> IF donor[l] = s THEN booked[l] ELSE 0], Leases)

\* Each hold is booked once, at its winner's charge: the owner's terminal
\* at the amount its owner decided, a front door's row at the amount it
\* appended, a reap at its heartbeat's running charge.
WinCharge(h) ==
    IF hold[h].own = "applied" THEN hold[h].oa
    ELSE IF hold[h].row = "applied" THEN hold[h].ra
    ELSE hold[h].b
BookedOnce ==
    \A l \in Leases :
        booked[l] = SumOver([h \in Holds |-> WinCharge(h)], { h \in Mine(l) : Applied(h) })

\* Invariant 9, the exposure half: the holds open under leases stay within
\* the allowance.
ExposureWithinAllowance ==
    SumOver([l \in Leases |-> IF Live(l) THEN Cardinality(OpenLog(l)) ELSE 0], Leases)
        <= Allowance

\* Invariant 11.
DebtMarksEveryShard ==
    (\E s \in Shards : RoomNow[s] < 0) => \A s \in Shards : mark[s]

\* The v11 hazard: a workspace that is no longer in debt is not left marked.
MarkMeansDebt ==
    (\E s \in Shards : mark[s]) => Total(RoomNow) < 0

\* Invariant 11, the refusal: a marked row takes no synchronous reservation,
\* and a marked workspace no grant.
MarkRefusesReservations ==
    [][ /\ \A s \in Shards : sync'[s] > sync[s] => ~mark[s]
        /\ \A l \in Leases : (st[l] = "none" /\ st'[l] = "open") => \A s \in Shards : ~mark[s] ]_vars

\* Money coming in repays debt first (section 4.7): while a shard is still
\* negative after a step, the step raised only shards that were negative,
\* each at most to zero, and none while a lower shard stays negative.
RepaysDebtFirst ==
    [][ (\E s \in Shards : RoomNow'[s] < 0) =>
          \A s \in Shards : RoomNow'[s] > RoomNow[s] =>
              /\ RoomNow[s] < 0
              /\ RoomNow'[s] <= 0
              /\ \A t \in Shards : t < s => RoomNow'[t] >= 0 ]_vars

\* Covering moves credit between shards and never makes or loses any.
CreditConserved ==
    Total(credits) = NShards * CreditEach + pays * PaySize

TypeOK ==
    /\ credits \in [Shards -> Int] /\ usage \in [Shards -> Nat]
    /\ reserved \in [Shards -> Int] /\ mark \in [Shards -> BOOLEAN]
    /\ sync \in [Shards -> Nat] /\ synced \in [Shards -> Nat]
    /\ st \in [Leases -> {"none", "open", "draining", "closed"}]
    /\ donor \in [Leases -> Shards]
    /\ alloc \in [Leases -> Nat] /\ booked \in [Leases -> Nat]
    /\ sfStored \in [Leases -> Nat]
    /\ sealed \in [Leases -> BOOLEAN]
    /\ \A h \in Holds :
        /\ hold[h].lease \in Leases \cup {NoLease}
        /\ hold[h].own \in {"none", "out", "log", "applied", "lost"}
        /\ hold[h].row \in {"none", "row", "applied", "lost"}
        /\ hold[h].reaped \in BOOLEAN
        /\ hold[h].oa \in Nat /\ hold[h].ra \in Nat /\ hold[h].b \in Nat
    /\ oUp \in [Leases -> {"up", "cut", "dead"}]
    /\ oStop \in [Leases -> BOOLEAN]
    /\ oAlloc \in [Leases -> Nat] /\ oSf \in [Leases -> Nat]
    /\ write \in [Leases -> Nat]
    /\ due \in [Leases -> 0..2]
    /\ returned \in [Leases -> Nat]
    /\ syncs \in 0..MaxSync /\ pays \in 0..MaxPays /\ cuts \in 0..MaxCuts

=============================================================================
