--------------------------- MODULE RegionalQuotaLease ---------------------------
(***************************************************************************)
(* A model of services/regional_quota_leases.py, plus the escrow layer that *)
(* module is designed to sit under but does not yet have.                   *)
(*                                                                          *)
(* WHY THIS EXISTS AS A MODEL AND NOT ONLY AS A TEST                        *)
(*                                                                          *)
(* The Python module is a pure state machine, so property tests can drive it *)
(* well and do (tests/test_regional_quota_lease_property.py is the           *)
(* executable shadow of this spec). What tests cannot reach is the part that *)
(* is not implemented yet: multiple regional planes holding leases against   *)
(* ONE workspace escrow, a granter handing out new leases, and a reclaimer   *)
(* sweeping expired ones back. That composition is where oversubscription    *)
(* would live — collectively spending more than the workspace escrowed — and *)
(* it is exactly the design being validated before the Spanner billing path  *)
(* delegates real prepaid spend to it.                                       *)
(*                                                                          *)
(* Modelling it now is the cheapest this will ever be. The module is still   *)
(* dark: no migration, no data, no callers to break.                         *)
(*                                                                          *)
(* WHAT IS ABSTRACTED                                                        *)
(*                                                                          *)
(*   - Spanner transactions become atomic actions. Justified because every   *)
(*     transition in the Python module is a single pure function returning a *)
(*     new value; the durable version is intended to be one transaction.     *)
(*   - Time is a bounded counter, not a clock. Expiry is a nondeterministic  *)
(*     event, which OVER-approximates clock skew rather than assuming a      *)
(*     synchronised clock.                                                   *)
(*   - Money is small naturals. The conservation law is about arithmetic     *)
(*     structure, not magnitude; int64 overflow is a separate concern and is *)
(*     covered by the Python property tests.                                 *)
(*                                                                          *)
(* The fencing token is the load-bearing mechanism: lease succession bumps   *)
(* it, and a stale holder's writes must be refused. S2 is the property that  *)
(* says so.                                                                  *)
(***************************************************************************)

EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    Escrow,       \* total microdollars the workspace has escrowed
    MaxLeases,    \* how many leases may be granted over the run
    MaxHolds,     \* holds per lease
    HoldAmount,   \* the reservation size (one size keeps the state space small)
    MaxTime       \* bounded clock

ASSUME Escrow \in Nat /\ Escrow > 0
ASSUME MaxLeases \in Nat /\ MaxLeases > 0
ASSUME MaxHolds \in Nat /\ MaxHolds > 0
ASSUME HoldAmount \in Nat /\ HoldAmount > 0
ASSUME MaxTime \in Nat /\ MaxTime > 0

LeaseIds == 1..MaxLeases
HoldIds  == 1..MaxHolds

VARIABLES
    escrowFree,   \* unallocated escrow
    granted,      \* [LeaseIds -> Nat]  amount escrowed into each lease
    leaseState,   \* [LeaseIds -> {"none","active","draining","closed","quarantined"}]
    leaseToken,   \* [LeaseIds -> Nat]  fencing token
    leaseExpiry,  \* [LeaseIds -> Nat]  clock value at which it expires
    holdState,    \* [LeaseIds][HoldIds -> {"none","reserved","settled","refunded"}]
    holdActual,   \* [LeaseIds][HoldIds -> Nat]  settled amount
    reclaimed,    \* [LeaseIds -> Nat]  amount returned to escrow
    clock,
    nextToken

vars == << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
           holdState, holdActual, reclaimed, clock, nextToken >>

\* ---------------------------------------------------------------- helpers ---

ReservedIn(l) ==
    LET rs == { h \in HoldIds : holdState[l][h] = "reserved" }
    IN Cardinality(rs) * HoldAmount

SpentIn(l) ==
    LET ss == { h \in HoldIds : holdState[l][h] = "settled" }
    IN LET Sum[S \in SUBSET ss] ==
            IF S = {} THEN 0
            ELSE LET x == CHOOSE y \in S : TRUE
                 IN holdActual[l][x] + Sum[S \ {x}]
       IN Sum[ss]

AccountedIn(l) == ReservedIn(l) + SpentIn(l)
AvailableIn(l) == granted[l] - AccountedIn(l)

LiveLeases  == { l \in LeaseIds : leaseState[l] \notin {"none", "closed"} }
IssuedLeases == { l \in LeaseIds : leaseState[l] # "none" }

\* Escrow still committed to a lease: what was granted, minus what was returned.
OutstandingIn(l) == granted[l] - reclaimed[l]

TotalOutstanding ==
    LET Sum[S \in SUBSET IssuedLeases] ==
        IF S = {} THEN 0
        ELSE LET x == CHOOSE y \in S : TRUE
             IN OutstandingIn(x) + Sum[S \ {x}]
    IN Sum[IssuedLeases]

TotalSpent ==
    LET Sum[S \in SUBSET IssuedLeases] ==
        IF S = {} THEN 0
        ELSE LET x == CHOOSE y \in S : TRUE
             IN SpentIn(x) + Sum[S \ {x}]
    IN Sum[IssuedLeases]

\* ------------------------------------------------------------------- init ---

Init ==
    /\ escrowFree = Escrow
    /\ granted = [l \in LeaseIds |-> 0]
    /\ leaseState = [l \in LeaseIds |-> "none"]
    /\ leaseToken = [l \in LeaseIds |-> 0]
    /\ leaseExpiry = [l \in LeaseIds |-> 0]
    /\ holdState = [l \in LeaseIds |-> [h \in HoldIds |-> "none"]]
    /\ holdActual = [l \in LeaseIds |-> [h \in HoldIds |-> 0]]
    /\ reclaimed = [l \in LeaseIds |-> 0]
    /\ clock = 0
    /\ nextToken = 1

\* ---------------------------------------------------------------- actions ---

\* The granter escrows a bounded amount into a new lease. This is the action
\* with no Python counterpart yet, and the one oversubscription would come from.
Grant(l, amount, expiry) ==
    /\ leaseState[l] = "none"
    /\ amount > 0
    /\ amount <= escrowFree
    /\ expiry > clock
    /\ escrowFree' = escrowFree - amount
    /\ granted' = [granted EXCEPT ![l] = amount]
    /\ leaseState' = [leaseState EXCEPT ![l] = "active"]
    /\ leaseToken' = [leaseToken EXCEPT ![l] = nextToken]
    /\ leaseExpiry' = [leaseExpiry EXCEPT ![l] = expiry]
    /\ nextToken' = nextToken + 1
    /\ UNCHANGED << holdState, holdActual, reclaimed, clock >>

\* Mirrors RegionalQuotaLease.reserve: active, unexpired, fits in available.
Reserve(l, h, token) ==
    /\ leaseState[l] = "active"
    /\ holdState[l][h] = "none"
    /\ token = leaseToken[l]              \* S2: a stale token cannot write
    /\ clock < leaseExpiry[l]
    /\ HoldAmount <= AvailableIn(l)
    /\ holdState' = [holdState EXCEPT ![l][h] = "reserved"]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdActual, reclaimed, clock, nextToken >>

\* Mirrors settle: the actual must fit inside the exact reservation.
Settle(l, h, actual, token) ==
    /\ holdState[l][h] = "reserved"
    /\ token = leaseToken[l]
    /\ actual >= 0 /\ actual <= HoldAmount
    /\ holdState' = [holdState EXCEPT ![l][h] = "settled"]
    /\ holdActual' = [holdActual EXCEPT ![l][h] = actual]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    reclaimed, clock, nextToken >>

Refund(l, h, token) ==
    /\ holdState[l][h] = "reserved"
    /\ token = leaseToken[l]
    /\ holdState' = [holdState EXCEPT ![l][h] = "refunded"]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdActual, reclaimed, clock, nextToken >>

\* A stale holder attempting a write. Modelled explicitly so TLC must show the
\* fence actually blocks it rather than the property holding vacuously.
StaleWrite(l, h, token) ==
    /\ leaseState[l] # "none"
    /\ token # leaseToken[l]
    /\ UNCHANGED vars                     \* refused: no state change at all

Tick ==
    /\ clock < MaxTime
    /\ clock' = clock + 1
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdState, holdActual, reclaimed, nextToken >>

\* The reclaimer sweeps an expired lease: unreserved remainder plus anything
\* still merely reserved goes back to escrow. Settled spend does not return.
\*
\* FINDING (from this model, 2026-08-14): the guard must admit "quarantined",
\* not only "active". TLC's first run had it active-only and produced a
\* liveness counterexample in six states — grant a lease, quarantine it, let
\* it expire, and its escrow is stranded permanently because no action can
\* ever move it to "closed". Quarantine is meant to freeze a suspicious lease
\* from SPENDING, not to make the workspace forfeit the money. Recording it
\* here because regional_quota_leases.py has no reclaimer yet: whoever builds
\* it must sweep quarantined leases too, and the property below is what says
\* so. Set the guard back to active-only and TLC fails in seconds.
Reclaim(l) ==
    /\ leaseState[l] \in {"active", "quarantined"}
    /\ clock >= leaseExpiry[l]
    /\ LET returnable == granted[l] - SpentIn(l)
       IN /\ escrowFree' = escrowFree + returnable
          /\ reclaimed' = [reclaimed EXCEPT ![l] = returnable]
    \* Reserved holds are cancelled by the sweep; the fence bump is what stops
    \* a plane that still believes it holds them from settling afterwards.
    /\ holdState' = [holdState EXCEPT ![l] =
                        [h \in HoldIds |->
                            IF holdState[l][h] = "reserved" THEN "refunded"
                            ELSE holdState[l][h]]]
    /\ leaseState' = [leaseState EXCEPT ![l] = "closed"]
    /\ leaseToken' = [leaseToken EXCEPT ![l] = nextToken]
    /\ nextToken' = nextToken + 1
    /\ UNCHANGED << granted, leaseExpiry, holdActual, clock >>

Quarantine(l) ==
    /\ leaseState[l] = "active"
    /\ leaseState' = [leaseState EXCEPT ![l] = "quarantined"]
    /\ UNCHANGED << escrowFree, granted, leaseToken, leaseExpiry,
                    holdState, holdActual, reclaimed, clock, nextToken >>

Next ==
    \/ \E l \in LeaseIds, a \in 1..Escrow, e \in 1..MaxTime : Grant(l, a, e)
    \/ \E l \in LeaseIds, h \in HoldIds, t \in 0..MaxLeases+1 : Reserve(l, h, t)
    \/ \E l \in LeaseIds, h \in HoldIds, a \in 0..HoldAmount,
          t \in 0..MaxLeases+1 : Settle(l, h, a, t)
    \/ \E l \in LeaseIds, h \in HoldIds, t \in 0..MaxLeases+1 : Refund(l, h, t)
    \/ \E l \in LeaseIds, h \in HoldIds, t \in 0..MaxLeases+1 : StaleWrite(l, h, t)
    \/ \E l \in LeaseIds : Reclaim(l)
    \/ \E l \in LeaseIds : Quarantine(l)
    \/ Tick

Spec == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ WF_vars(\E l \in LeaseIds : Reclaim(l))

\* ------------------------------------------------------------- invariants ---

TypeOK ==
    /\ escrowFree \in 0..Escrow
    /\ clock \in 0..MaxTime
    /\ \A l \in LeaseIds :
        /\ leaseState[l] \in {"none","active","draining","closed","quarantined"}
        /\ granted[l] \in 0..Escrow
        /\ reclaimed[l] \in 0..Escrow

\* S1 — NO OVERSUBSCRIPTION. The headline. Escrow is conserved: what is free
\* plus what is still committed to leases plus what has actually been spent
\* never exceeds what the workspace escrowed. A violation here is minted money:
\* regional planes collectively spending more than the workspace has.
NoOversubscription ==
    escrowFree + TotalOutstanding <= Escrow

\* S1b — spend never exceeds the escrow, however the leases are interleaved.
SpendWithinEscrow ==
    TotalSpent <= Escrow

\* S2 — a lease never accounts for more than it was granted. This is the
\* invariant the Python __post_init__ asserts; here it must survive concurrent
\* reserve/settle/reclaim rather than one construction.
NoLeaseOverspend ==
    \A l \in IssuedLeases : AccountedIn(l) <= granted[l]

\* S3 — a settled hold's actual always fits inside its reservation, so a
\* settlement can never charge more than was held.
SettlementFitsReservation ==
    \A l \in LeaseIds, h \in HoldIds :
        holdState[l][h] = "settled" => holdActual[l][h] <= HoldAmount

\* S4 — a hold is never both settled and refunded. The two exits are exclusive,
\* which is what stops a double-count on the reclaim path.
ExclusiveHoldExit ==
    \A l \in LeaseIds, h \in HoldIds :
        holdState[l][h] \in {"none","reserved","settled","refunded"}

\* S5 — reclaim never returns more than was granted.
ReclaimIsBounded ==
    \A l \in LeaseIds : reclaimed[l] <= granted[l]

Safety ==
    /\ TypeOK
    /\ NoOversubscription
    /\ SpendWithinEscrow
    /\ NoLeaseOverspend
    /\ SettlementFitsReservation
    /\ ExclusiveHoldExit
    /\ ReclaimIsBounded

\* L1 — liveness: an expired lease is eventually closed, so escrow does not
\* stay stranded. Checked under weak fairness on Tick and Reclaim.
\*
\* This is the property that caught the quarantine gap. It deliberately covers
\* BOTH live states: a safety-only spec would have been perfectly happy with a
\* design that quietly keeps a customer's money forever.
ExpiredLeasesAreEventuallyReclaimed ==
    \A l \in LeaseIds :
        (leaseState[l] \in {"active", "quarantined"} /\ clock >= leaseExpiry[l])
            ~> (leaseState[l] = "closed")

=============================================================================
