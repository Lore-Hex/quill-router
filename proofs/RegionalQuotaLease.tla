--------------------------- MODULE RegionalQuotaLease ---------------------------
(***************************************************************************)
(* A model of services/regional_quota_leases.py, plus the escrow layer that *)
(* module is designed to sit under but does not yet have.                   *)
(*                                                                          *)
(* WHY THIS EXISTS AS A MODEL AND NOT ONLY AS A TEST                        *)
(*                                                                          *)
(* The Python module is a pure state machine, so property tests can drive it *)
(* well and do (tests/test_regional_quota_leases.py is the                   *)
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
(*   - A hold carries the fencing token it was reserved under (holdToken).   *)
(*     That is what a settling plane actually presents: the token it held    *)
(*     when it took the reservation, not a token freshly read from the row.  *)
(*                                                                           *)
(* ======================================================================== *)
(* THE FENCING TOKEN: FOUR ROUNDS, AND THE ANSWER IS NOT THE ONE ANY EARLIER *)
(* ROUND PREDICTED                                                          *)
(* ======================================================================== *)
(*                                                                           *)
(* This section is the history of a claim this file made, had refuted, made  *)
(* again, and had refuted again — four times now. The history is kept        *)
(* because it is the most useful thing in the file: it is a record of three  *)
(* different ways to write a check that proves nothing, of a prediction that *)
(* turned out wrong, and of a decomposition that was stated one guard too    *)
(* coarse and hid a real gap in the Python for a round.                      *)
(*                                                                           *)
(* ROUND 1 — the vacuous adversary. The original StaleWrite action was       *)
(* `UNCHANGED vars`. An action that does nothing is trivially safe, so the   *)
(* fence could have been deleted and TLC would still have passed. Fixed by   *)
(* making StaleWrite perform a real write.                                   *)
(*                                                                           *)
(* ROUND 2 — the vacuous guard. Even with a real StaleWrite, DELETING the    *)
(* token guard from Reserve yielded no violation (4,634,802 states, no       *)
(* error). The header blamed the missing succession action. That was honest  *)
(* but the diagnosis was wrong, and the wrong diagnosis is worth writing     *)
(* down, because it is a trap any spec in this directory can fall into:      *)
(*                                                                           *)
(*     Reserve took the token as a parameter and Next quantified it          *)
(*     existentially — `\E t \in Tokens : Reserve(l, h, t)`. Under an        *)
(*     existential, the guard `t = leaseToken[l]` restricts NOTHING: the     *)
(*     model checker simply picks the t that satisfies it. Deleting such a   *)
(*     guard cannot produce a violation, ever, for any spec, because it only *)
(*     widens the choice of a variable that was already free. The 4.6M-state *)
(*     run was not evidence about fencing. It was evidence about quantifiers.*)
(*                                                                           *)
(* An existentially chosen token models "some plane presents some token",    *)
(* which is not the hazard. The hazard is a specific plane presenting the    *)
(* specific token it was handed, after that token has been superseded. So    *)
(* the token a hold was reserved under is now STATE (holdToken), and Settle  *)
(* and Refund consult it instead of taking a token argument. That guard is a *)
(* real guard: deleting it changes the transition relation.                  *)
(*                                                                           *)
(* ROUND 3 — succession exists now, and it refutes round 2's prediction.     *)
(*                                                                           *)
(* Round 2 predicted: "the hazard the fence actually addresses is lease      *)
(* SUCCESSION ... this model has no succession action, so the fence has      *)
(* nothing to protect here." Regrant now exists. The prediction was wrong    *)
(* twice over.                                                               *)
(*                                                                           *)
(*   (a) The hazard is not succession. The trace that needs the fence is     *)
(*       six states long and contains no Regrant at all. It is: grant a      *)
(*       lease, reserve a hold against it, tick past expiry, sweep it — the  *)
(*       sweep returns the whole grant to escrow, because nothing has been   *)
(*       spent yet — and then let the holder settle the hold it still        *)
(*       believes it owns. The money is now back in escrowFree AND recorded  *)
(*       as spent: reclaimed[l] + SpentIn(l) = 3 against granted[l] = 2.     *)
(*       Minted. What the fence guards is a hold OUTLIVING THE SWEEP OF THE  *)
(*       GENERATION THAT AUTHORISED IT. Succession is one way to notice      *)
(*       that; it is not necessary for it, and plain expire-and-sweep gets   *)
(*       there first.                                                        *)
(*                                                                           *)
(*       (This paragraph used to say SEVEN states, with a second Grant on    *)
(*       the way past. That length came from a -workers auto run, and a      *)
(*       parallel breadth-first search reports the first counterexample any  *)
(*       worker hands back, which need not be a shortest one. Re-run at      *)
(*       -workers 1 the trace is six states and the second Grant is gone.    *)
(*       Every trace length quoted in this file is now from a single-worker  *)
(*       re-run of the same mutant, for that reason; the state COUNTS are    *)
(*       still from -workers auto.)                                          *)
(*                                                                           *)
(*   (b) Adding succession did not change the fence answer at all. With the  *)
(*       sweep intact, deleting the fence from Settle explores 7,008,021     *)
(*       states and finds nothing — and reaches exactly the same 1,292,173   *)
(*       DISTINCT states as the unmutated model. That equality is a proof,   *)
(*       not a coincidence of counting: dropping a conjunct only ever WIDENS *)
(*       an action's guard, so the mutant's reachable set contains the       *)
(*       original's, and two sets in that relation with the same finite size *)
(*       are the same set. The fence does not remove one reachable state.    *)
(*       The mechanism: a hold's stamp is erased when it leaves "reserved",  *)
(*       so the only settles the fence blocks are settles of holds a         *)
(*       superseded plane reserved, and those land in states an honest       *)
(*       reserve already reaches.                                            *)
(*                                                                           *)
(*       That argument is only available for a GUARD deletion. Three rows in *)
(*       the table below delete an ASSIGNMENT instead (a token bump), which  *)
(*       does not widen anything, and two of them also report exactly        *)
(*       1,292,173 distinct states. Those equalities are NOT the same proof, *)
(*       and must not be quoted as one. The reachable sets are not nested,   *)
(*       so equal size says nothing about equality; what they most likely    *)
(*       reflect is a relabelling — at these bounds a lease's token is a     *)
(*       function of its own history, so dropping a bump plausibly maps the  *)
(*       state graph onto an isomorphic copy with smaller numbers in it —    *)
(*       but no run here establishes that isomorphism. What those rows DO    *)
(*       establish is the weaker and sufficient statement: no invariant and  *)
(*       no property in the .cfg fires.                                      *)
(*                                                                           *)
(* ROUND 4 — the decomposition round 3 wrote down was wrong, and getting it  *)
(* right is the difference between a footnote and a live defect.             *)
(*                                                                           *)
(* Round 3 said: the fence and the reclaimer's hold cancellation are TWO     *)
(* GUARDS on one hole, remove either and nothing breaks. The first clause    *)
(* is right; the second is not, because "the fence" is not one guard. The    *)
(* hole — a hold outliving the sweep of the generation that authorised it —  *)
(* is closed by EITHER of:                                                   *)
(*                                                                           *)
(*     (i)  the reclaimer cancelling outstanding holds inside the            *)
(*          transaction that closes the lease; or                            *)
(*     (ii) the PAIR { fence at settle, token bump at close }.               *)
(*                                                                           *)
(* (i) and (ii) are alternatives: either alone closes the hole and neither   *)
(* is necessary, which is what round 3 measured. The two HALVES of (ii) are  *)
(* not alternatives, which round 3 never measured:                           *)
(*                                                                           *)
(*     drop (i), delete the fence, keep the bump    VIOLATED, six states     *)
(*     drop (i), keep the fence, delete the bump    VIOLATED, six states,    *)
(*                                                  with the fence passing   *)
(*                                                  on every step            *)
(*                                                                           *)
(* The second row is the one that matters, because it is the shape the       *)
(* Python is in today: `_require_fence` exists and nothing ever advances     *)
(* `fencing_token`. See scope limit 2.                                       *)
(*                                                                           *)
(* What succession DID add is an obligation of its own, and it is the one    *)
(* guard here that nothing else covers. Regrant overwrites granted[l] and    *)
(* reclaimed[l] on the lease, so the retired generation's settled spend has  *)
(* to be banked before the overwrite or it stops existing. Delete the        *)
(* `retiredSpend' = retiredSpend + SpentIn(l)` line from Regrant and         *)
(* EscrowConserved fires in seven states: escrow that a regional plane       *)
(* genuinely spent silently reappears as unspent.                            *)
(*                                                                           *)
(* What succession did NOT add is any load on the token bump in Regrant      *)
(* itself. Deleting that bump — a plain token REUSE at succession — is       *)
(* caught by nothing here. The note on TokensNeverGoBackwards below says so  *)
(* with the counts, and says what is and is not caught instead. The bump     *)
(* that carries weight is Reclaim's, at close.                               *)
(*                                                                           *)
(* MEASURED, at the constants in RegionalQuotaLease.cfg. Every row is an     *)
(* edit to this spec — to the source, never to an invariant and never to     *)
(* the .cfg — re-run to completion for this commit.                          *)
(*                                                                           *)
(* The table is exhaustive over one class of edit and silent outside it. It  *)
(* covers every fencing-token guard (Reserve, Settle, Refund), every token   *)
(* bump (Reclaim's and Regrant's), the reclaimer's hold cancellation, and    *)
(* Regrant's retiredSpend carry. The other guards in this spec — expiry,     *)
(* lease state, `HoldAmount <= AvailableIn(l)` — have NO deletion            *)
(* experiment here, and nothing below should be read as one.                 *)
(*                                                                           *)
(*   this file, unmodified            no error   5,844,105 / 1,292,173 dist. *)
(*   - fence in Settle                no error   7,008,021 / 1,292,173 dist. *)
(*   - fence in Refund                no error   6,232,077 / 1,292,173 dist. *)
(*   - fence in Reserve, faithfully   no error   7,204,605 / 1,609,117 dist. *)
(*   - fence in Reserve, literally    VIOLATED   TokenStampsAreWellFormed,   *)
(*                                               3-state trace — a           *)
(*                                               model artifact, not money;  *)
(*                                               see scope limit 5           *)
(*   - hold cancellation in Reclaim   no error   7,213,297 / 1,780,589 dist. *)
(*   - token bump in Reclaim          no error   5,844,105 / 1,292,173 dist. *)
(*   - token bump in Regrant (reuse)  no error   5,844,105 / 1,292,173 dist. *)
(*   - hold cancellation in Reclaim   VIOLATED   SpendIsCoveredByCommitment, *)
(*     AND the fence in Settle                   6-state trace               *)
(*   - hold cancellation in Reclaim   VIOLATED   SpendIsCoveredByCommitment, *)
(*     AND the token bump in Reclaim             6-state trace; the fence    *)
(*                                               passes on every step        *)
(*   Regrant's bump RESET to 1        VIOLATED   TokensNeverGoBackwards,     *)
(*     rather than deleted                       5-state trace               *)
(*   - retiredSpend carry in Regrant  VIOLATED   EscrowConserved,            *)
(*                                               7-state trace               *)
(*                                                                           *)
(* WHAT THIS DOES *NOT* ESTABLISH — read this before citing the file          *)
(*                                                                           *)
(*   1. It does not establish that the fence is NECESSARY, and it does not   *)
(*      establish that the fence ALONE is sufficient either. What it         *)
(*      establishes is that the fence is sufficient IN COMPANY WITH a token  *)
(*      bump in the closing transaction — round 4 above, measured both ways. *)
(*      The fence is not necessary because in this model the reclaimer       *)
(*      refunds every outstanding hold in the same atomic action that closes *)
(*      the lease, and that alone closes the hole too. Nobody should delete  *)
(*      `_require_fence` from regional_quota_leases.py on the strength of    *)
(*      this file; they would be leaning on a reclaimer that has not been    *)
(*      written. Nobody should keep it and stop there either.                *)
(*                                                                           *)
(*   2. And that reclaimer is exactly the thing to worry about. A lease has  *)
(*      an unbounded number of holds and a Spanner transaction has a         *)
(*      mutation limit, so the durable sweep very likely CANNOT refund every *)
(*      hold in the transaction that closes the lease; it will close the     *)
(*      lease row and let hold cleanup trail. The moment it does, guard (i)  *)
(*      is gone and the pair (ii) is all that is left — and (ii) is two      *)
(*      things, not one. Whoever writes the reclaimer must do EITHER:        *)
(*                                                                           *)
(*        (a) cancel the holds atomically with the close; or                 *)
(*        (b) BOTH advance the lease row's fencing token in the transaction  *)
(*            that closes it AND make the settle path compare the presented  *)
(*            token against that row INSIDE the settling transaction.        *)
(*                                                                           *)
(*      (b) with either conjunct missing is the six-state double-charge in   *)
(*      the table above. This corrects what this item said before, which     *)
(*      offered the settle-side comparison ALONE as the alternative to (a).  *)
(*      That was false, and false in the direction that costs money.         *)
(*                                                                           *)
(*      FINDING (from reading the module, not from TLC): the bump that (b)   *)
(*      requires does not exist. `fencing_token` occurs seventeen times in   *)
(*      src/, and every occurrence is a field declaration, a keyword         *)
(*      parameter, a `_require_fence` comparison, or the `<= 0` check in     *)
(*      `__post_init__`. Nothing assigns it: `close()` returns               *)
(*      `replace(self, state=LeaseState.CLOSED)` with the token untouched,   *)
(*      and `begin_drain()` and `quarantine()` do the same for their         *)
(*      states. So regional_quota_leases.py today IS the mutant in the       *)
(*      "keep the fence, delete the token bump" row — the fence is honoured  *)
(*      on every call and the hold from the retired generation settles       *)
(*      anyway, because its token still matches the row it is compared       *)
(*      against. Closing that is a code change (advance the token in the     *)
(*      transaction that closes or supersedes a lease, and make the sweep    *)
(*      the only writer of it); it is NOT made in this commit, and this      *)
(*      file's job is only to establish that it is needed.                   *)
(*                                                                           *)
(*      One thing in the current code does lean the right way and is worth   *)
(*      recording so nobody re-derives it. `close()` refuses to close a      *)
(*      lease that still has open reservations, so the GRACEFUL path cannot  *)
(*      strand a reserved hold behind a close at all — which is also why     *)
(*      the missing bump has not cost anything yet. The whole hazard lives   *)
(*      in the forced sweep, which does not exist yet. And `settle()` has    *)
(*      no lease-state guard at all — a CLOSED lease can still be settled    *)
(*      against — which is why Settle here has none either, and is why the   *)
(*      fence and the hold state are the only two things standing between    *)
(*      the sweep and a late settlement.                                     *)
(*                                                                           *)
(*   3. FINDING, from reading docs/design/regional-quota-leases.md rather    *)
(*      than from TLC: the schema sketch keys holds by                       *)
(*      `PRIMARY KEY (lease_id, hold_id)` with no fencing_token column, so   *)
(*      hold rows are NOT scoped to a generation. Regrant here CLEARS the    *)
(*      holds, and the comment on Regrant argues why that is the faithful    *)
(*      choice — but the sketched schema does not do it. Regranting into the *)
(*      same lease row without clearing charges the new grant for the        *)
(*      retired generation's settled spend, which was already netted out of  *)
(*      escrow at the sweep; `__post_init__`'s `accounted <= granted` can    *)
(*      then raise on a lease nobody touched. Either put the generation in   *)
(*      the hold key or clear the rows at succession. This model assumes the *)
(*      second and checks nothing about the first.                           *)
(*                                                                           *)
(*   4. It does not establish anything about ATTRIBUTION, and deliberately   *)
(*      does not try. See the note above SpendIsCoveredByCommitment for the  *)
(*      argument that a "settled holds belong to the generation that         *)
(*      authorised them" invariant is circular here, and was left out rather *)
(*      than added to make the fence look busy.                              *)
(*                                                                           *)
(*   5. The reserve-side fence still has no honest deletion experiment.      *)
(*      Deleting `token = leaseToken[l]` from Reserve literally now fails    *)
(*      in three states (22 generated) — but on TokenStampsAreWellFormed,    *)
(*      because it lets a plane present a token that has not been issued     *)
(*      yet. That is a model artifact, not money. Weakened to the faithful   *)
(*      reading, "accept any token this lease has ever had"                  *)
(*      (`token <= leaseToken[l]`), the run is clean. Which is what          *)
(*      StaleWrite already says: the fence-free reserve is in Next           *)
(*      unconditionally, in every run above, and the accounting guard        *)
(*      `HoldAmount <= AvailableIn(l)` is what bounds it.                    *)
(*                                                                           *)
(*   6. The bounds are small (see the .cfg). One regrant per lease means     *)
(*      this checks succession, not a chain of successions.                  *)
(*                                                                           *)
(*   7. Nothing here catches a token REUSE at succession, and the note on    *)
(*      TokensNeverGoBackwards records the measurement rather than hiding    *)
(*      it. The reason is item 3's assumption: Regrant clears the hold rows, *)
(*      so a reused token has no retired holder to let back in. A reader     *)
(*      who implements the OTHER schema — hold rows not cleared at           *)
(*      succession — is outside everything this file checks about reuse.     *)
(***************************************************************************)

EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    Escrow,       \* total microdollars the workspace has escrowed
    MaxLeases,    \* how many lease IDs exist
    MaxHolds,     \* holds per lease generation
    HoldAmount,   \* the reservation size (one size keeps the state space small)
    MaxTime,      \* bounded clock
    MaxRegrants   \* how many times one lease ID may be re-granted

ASSUME Escrow \in Nat /\ Escrow > 0
ASSUME MaxLeases \in Nat /\ MaxLeases > 0
ASSUME MaxHolds \in Nat /\ MaxHolds > 0
ASSUME HoldAmount \in Nat /\ HoldAmount > 0
ASSUME MaxTime \in Nat /\ MaxTime > 0
ASSUME MaxRegrants \in Nat

LeaseIds == 1..MaxLeases
HoldIds  == 1..MaxHolds

\* Fencing tokens are PER LEASE, counted from 1 at Grant and bumped on every
\* close and every succession. The previous version of this spec drew them from
\* one global counter, which is sound but costs a great deal: a monotone global
\* counter records the ORDER in which unrelated leases were bumped, so two runs
\* that differ only in which lease was swept first are distinct states forever
\* after. Per-lease is also the more faithful shape — `fencing_token` is a
\* field on the lease row in regional_quota_leases.py, and the natural durable
\* implementation is that row's generation number — and the fence only ever
\* compares a token against the SAME lease's token, so nothing is lost.
\*
\* The universe has to contain every token that can actually be issued, or the
\* model silently loses behaviours: a guard `t = leaseToken[l]` becomes
\* unsatisfiable once leaseToken[l] leaves the range, which disables actions
\* rather than failing loudly. One lease is bumped once by Grant, once per
\* Reclaim (MaxRegrants + 1 of them) and once per Regrant.
\*
\* The previous version used `0..MaxLeases+1`, which was already too small for
\* the reclaim bumps — with MaxLeases = 2 the second reclaim assigned token 4
\* to a lease whose token could then never be presented. It did not matter
\* there because nothing could act on a closed lease anyway, but under
\* succession it would have quietly deleted the whole hazard.
MaxToken == 2 * (MaxRegrants + 1)
Tokens   == 0..MaxToken

\* The token a superseded holder presents. 0 is never issued — a lease's first
\* generation is token 1 — so it never matches a live lease's token, which is
\* the only thing the fence asks about.
\*
\* Collapsing every superseded token to one value is sound, not a convenience:
\* holdToken is read by exactly one expression, `holdToken[l][h] =
\* leaseToken[l]`, and TokensNeverGoBackwards (checked below as a property,
\* not assumed) says leaseToken[l] only ever climbs. So a stamp below the live
\* token today is smaller than it forever, and every such stamp is
\* indistinguishable from every other under the only test applied to it.
\* Keeping them distinct multiplies the state space by the token range for no
\* behaviour, which is what made the first attempt at this spec uncheckable.
StaleToken == 0

\* Grant sizes. Restricted to multiples of HoldAmount because a remainder is
\* escrow that can never be reserved against — a grant of 3 with HoldAmount 2
\* reaches exactly the hold configurations a grant of 2 reaches, plus one unit
\* of slack that rides along through reclaim. Those grants multiply `granted`,
\* `AvailableIn` and every reclaim amount without adding an interleaving, and
\* with succession re-drawing `granted` per generation that multiplication is
\* what made the model too large to finish. Ragged amounts against a ragged
\* reservation size are the Python property tests' job; this model is about
\* the composition.
\*
\* Stated as a BOUND, not as a proven-lossless reduction. The runs with
\* unrestricted amounts were abandoned before they completed, so nothing here
\* rules out an arithmetic path that only a remainder can produce. The
\* argument above is why one is not expected; it is not a check that there
\* isn't one.
LeaseAmounts == { a \in 1..Escrow : a % HoldAmount = 0 }

VARIABLES
    escrowFree,   \* unallocated escrow
    granted,      \* [LeaseIds -> Nat]  amount escrowed into the LIVE generation
    leaseState,   \* [LeaseIds -> {"none","active","draining","closed","quarantined"}]
    leaseToken,   \* [LeaseIds -> Nat]  fencing token of the live generation
    leaseExpiry,  \* [LeaseIds -> Nat]  clock value at which it expires
    holdState,    \* [LeaseIds][HoldIds -> {"none","reserved","settled","refunded"}]
    holdActual,   \* [LeaseIds][HoldIds -> Nat]  settled amount
    holdToken,    \* [LeaseIds][HoldIds -> Nat]  token the hold was reserved under
    reclaimed,    \* [LeaseIds -> Nat]  amount of the live grant returned to escrow
    retiredSpend, \* Nat  settled spend of generations that have been superseded
    regrants,     \* [LeaseIds -> Nat]  successions performed, bounded by MaxRegrants
    clock

vars == << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
           holdState, holdActual, holdToken, reclaimed, retiredSpend,
           regrants, clock >>

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

LiveLeases   == { l \in LeaseIds : leaseState[l] \notin {"none", "closed"} }
IssuedLeases == { l \in LeaseIds : leaseState[l] # "none" }
ClosedLeases == { l \in LeaseIds : leaseState[l] = "closed" }

\* Escrow still committed to a lease's LIVE generation: what it was granted,
\* minus what has been returned. Retired generations do not appear here; their
\* residue was returned at their sweep and their spend moved to retiredSpend.
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
    IN retiredSpend + Sum[IssuedLeases]

\* ------------------------------------------------------------------- init ---

Init ==
    /\ escrowFree = Escrow
    /\ granted = [l \in LeaseIds |-> 0]
    /\ leaseState = [l \in LeaseIds |-> "none"]
    /\ leaseToken = [l \in LeaseIds |-> 0]
    /\ leaseExpiry = [l \in LeaseIds |-> 0]
    /\ holdState = [l \in LeaseIds |-> [h \in HoldIds |-> "none"]]
    /\ holdActual = [l \in LeaseIds |-> [h \in HoldIds |-> 0]]
    /\ holdToken = [l \in LeaseIds |-> [h \in HoldIds |-> 0]]
    /\ reclaimed = [l \in LeaseIds |-> 0]
    /\ retiredSpend = 0
    /\ regrants = [l \in LeaseIds |-> 0]
    /\ clock = 0

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
    /\ leaseToken' = [leaseToken EXCEPT ![l] = 1]
    /\ leaseExpiry' = [leaseExpiry EXCEPT ![l] = expiry]
    /\ UNCHANGED << holdState, holdActual, holdToken, reclaimed, retiredSpend,
                    regrants, clock >>

\* LEASE SUCCESSION. The same lease ID, re-granted after its previous
\* generation was swept: a fresh amount out of escrow, a fresh expiry, and a
\* bumped fencing token. This is the action the previous version of this spec
\* said was missing.
\*
\* WHY THE HOLDS ARE RESET AND THE SPEND IS CARRIED
\*
\* The two obvious choices are both wrong, which is why this deserves a
\* paragraph rather than a line.
\*
\*   - Carry the hold rows into the new generation. Then AvailableIn charges
\*     the NEW grant for the OLD generation's settled spend, which was already
\*     netted out of escrow when the old generation was swept. The workspace
\*     pays for one request twice: once in the sweep's withholding, once in the
\*     new grant's shrunken availability. Conservative-looking and wrong.
\*
\*   - Reset the hold rows and let SpentIn fall to zero. Then the previous
\*     generation's spend vanishes from TotalSpent and the conservation law
\*     degrades from an equality into a slack inequality that no longer says
\*     "escrow is conserved" — it says "escrow is not exceeded", which a
\*     ledger that quietly forgets charges also satisfies.
\*
\* So: reset the hold rows AND carry the retired generation's spend in
\* retiredSpend. The carry is not a modelling convenience — it is what the
\* design doc already calls for, since the reconciler "imports settled spend to
\* Spanner" when it closes a lease, and closing does not un-bill. Regrant then
\* OVERWRITES granted[l] and reclaimed[l], so anything derived from them has to
\* be banked before the overwrite or it stops existing; EscrowConserved below
\* is stated as an equality precisely so that dropping the carry is caught.
\*
\* The reset is the weaker half of the choice and is called out as a scope
\* limit in the header (item 3): the schema sketch in
\* docs/design/regional-quota-leases.md keys holds by (lease_id, hold_id) with
\* no generation column, so a succession into the same lease row would NOT
\* clear them. This model assumes the clearing implementation and says nothing
\* about the other one.
Regrant(l, amount, expiry) ==
    /\ leaseState[l] = "closed"
    /\ regrants[l] < MaxRegrants
    /\ amount > 0
    /\ amount <= escrowFree
    /\ expiry > clock
    /\ escrowFree' = escrowFree - amount
    /\ granted' = [granted EXCEPT ![l] = amount]
    /\ reclaimed' = [reclaimed EXCEPT ![l] = 0]
    /\ retiredSpend' = retiredSpend + SpentIn(l)
    /\ holdState' = [holdState EXCEPT ![l] = [h \in HoldIds |-> "none"]]
    /\ holdActual' = [holdActual EXCEPT ![l] = [h \in HoldIds |-> 0]]
    /\ holdToken' = [holdToken EXCEPT ![l] = [h \in HoldIds |-> 0]]
    /\ leaseState' = [leaseState EXCEPT ![l] = "active"]
    \* MEASURED AS INERT at these bounds: delete this bump and nothing in the
    \* .cfg fires. It is kept because it is what a real succession does and
    \* because a RESET here — token back to 1 rather than left alone — is
    \* caught, by TokensNeverGoBackwards and by nothing else. The note on
    \* that property has both runs and says why the reuse case is invisible
    \* to this model (the holdState/holdActual/holdToken lines above have
    \* already wiped every hold this lease had).
    /\ leaseToken' = [leaseToken EXCEPT ![l] = leaseToken[l] + 1]
    /\ leaseExpiry' = [leaseExpiry EXCEPT ![l] = expiry]
    /\ regrants' = [regrants EXCEPT ![l] = regrants[l] + 1]
    /\ UNCHANGED clock

\* Mirrors RegionalQuotaLease.reserve: active, unexpired, fits in available.
\*
\* The token here is still existentially chosen in Next, so this guard is the
\* structurally vacuous one described in the header — it is kept because it
\* mirrors `_require_fence` in the Python and because the hold has to be
\* stamped with SOME token, not because deleting it would prove anything.
\* Weakening it to `token <= leaseToken[l]`, which is what deleting
\* `_require_fence` from `reserve()` actually means when tokens are monotone,
\* runs clean. StaleWrite is the action that carries the reserve-side hazard,
\* and it is in Next unconditionally rather than behind a mutation.
Reserve(l, h, token) ==
    /\ leaseState[l] = "active"
    /\ holdState[l][h] = "none"
    /\ token = leaseToken[l]              \* structurally vacuous: see header
    /\ clock < leaseExpiry[l]
    /\ HoldAmount <= AvailableIn(l)
    /\ holdState' = [holdState EXCEPT ![l][h] = "reserved"]
    /\ holdToken' = [holdToken EXCEPT ![l][h] = token]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdActual, reclaimed, retiredSpend, regrants, clock >>

\* Mirrors settle: the actual must fit inside the exact reservation.
\*
\* The settling plane presents the token it held when it reserved, which is
\* holdToken[l][h] — NOT a token freshly read from the lease row, and not a
\* token chosen by the model checker. That is the whole point: this guard can
\* fail. Delete it and the hold's generation stops mattering.
Settle(l, h, actual) ==
    /\ holdState[l][h] = "reserved"
    /\ holdToken[l][h] = leaseToken[l]    \* S2: the fence
    /\ actual >= 0 /\ actual <= HoldAmount
    /\ holdState' = [holdState EXCEPT ![l][h] = "settled"]
    /\ holdActual' = [holdActual EXCEPT ![l][h] = actual]
    \* Zeroing the stamp on the terminal transition is a state-space
    \* reduction with no behavioural content: nothing reads holdToken except
    \* the guards above, and a hold leaves "reserved" exactly once.
    /\ holdToken' = [holdToken EXCEPT ![l][h] = 0]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    reclaimed, retiredSpend, regrants, clock >>

\* Refund consults the stamp for the same reason Settle does, and mirrors
\* `refund()`'s own `_require_fence` call.
\*
\* MEASURED: deleting this guard alone changes nothing — no error, 6,232,077
\* generated / 1,292,173 distinct, the same DISTINCT count as the unmutated
\* model, so by the widening argument in the header it removes no reachable
\* state. It is the third inert token guard in this file, and it is in the
\* header's table for that reason: the table is a record of which guards are
\* load-bearing, and a guard that is not has to appear there too or the table
\* reads as a list of the ones that are.
Refund(l, h) ==
    /\ holdState[l][h] = "reserved"
    /\ holdToken[l][h] = leaseToken[l]
    /\ holdState' = [holdState EXCEPT ![l][h] = "refunded"]
    /\ holdToken' = [holdToken EXCEPT ![l][h] = 0]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdActual, reclaimed, retiredSpend, regrants, clock >>

\* A stale holder attempting a reserve.
\*
\* REVIEW FIX (round 1): this was `UNCHANGED vars`, which proved nothing — an
\* action that does nothing is trivially safe. It now performs the SAME state
\* change Reserve does, stamping a token that by construction cannot match the
\* lease's, so safety must hold even though a superseded plane really did
\* write. It is unguarded by any token equality, which is the point: the
\* fence-free reserve path is in Next in every run, not behind a mutation.
\*
\* The stale token is StaleToken rather than a quantified one; see the note on
\* StaleToken for why that loses no behaviour. A LARGER token is deliberately
\* not modelled: tokens are issued from a monotone counter, so a plane cannot
\* hold one that has not been issued yet, and admitting one would let a stamped
\* hold turn valid later when the counter caught up. That is a spurious
\* behaviour, not a conservative over-approximation.
StaleWrite(l, h) ==
    /\ leaseState[l] = "active"
    /\ holdState[l][h] = "none"
    /\ HoldAmount <= AvailableIn(l)       \* still bounded by the lease's own grant
    /\ holdState' = [holdState EXCEPT ![l][h] = "reserved"]
    /\ holdToken' = [holdToken EXCEPT ![l][h] = StaleToken]
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdActual, reclaimed, retiredSpend, regrants, clock >>

Tick ==
    /\ clock < MaxTime
    /\ clock' = clock + 1
    /\ UNCHANGED << escrowFree, granted, leaseState, leaseToken, leaseExpiry,
                    holdState, holdActual, holdToken, reclaimed, retiredSpend,
                    regrants >>

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
    /\ leaseState[l] \in {"active", "draining", "quarantined"}
    /\ clock >= leaseExpiry[l]
    /\ LET returnable == granted[l] - SpentIn(l)
       IN /\ escrowFree' = escrowFree + returnable
          /\ reclaimed' = [reclaimed EXCEPT ![l] = returnable]
    \* FINDING (from this model, 2026-08-15): this cancellation is guard (i)
    \* of the header's round 4. The reserved money is returned to escrow on
    \* the line above, so if a hold could survive the sweep in "reserved"
    \* state and be settled afterwards, that money would be spent as well as
    \* returned. Cancelling the holds closes the hole; so does the PAIR
    \* {fence at settle, token bump below}; dropping this cancellation and
    \* either half of that pair mints money in six states.
    \*
    \* CORRECTION (2026-08-15, from review): an earlier version of this
    \* comment said a reclaimer that cannot cancel every hold "is relying
    \* entirely on the fence". It is relying on the fence AND on the bump
    \* below, and regional_quota_leases.py has only the first.
    \* Scope limit 2 in the header has the measurement and the consequence.
    /\ holdState' = [holdState EXCEPT ![l] =
                        [h \in HoldIds |->
                            IF holdState[l][h] = "reserved" THEN "refunded"
                            ELSE holdState[l][h]]]
    /\ holdToken' = [holdToken EXCEPT ![l] = [h \in HoldIds |-> 0]]
    /\ leaseState' = [leaseState EXCEPT ![l] = "closed"]
    \* The close-time bump: the other half of guard (ii). It is what makes a
    \* stamp from the swept generation stale, and therefore what makes the
    \* fence in Settle able to fail at all. Delete it and keep the
    \* cancellation above and nothing in the .cfg fires (5,844,105 /
    \* 1,292,173); delete it and the cancellation together,
    \* fence intact, and SpendIsCoveredByCommitment fires in six states.
    \* `close()` in regional_quota_leases.py does not do this.
    /\ leaseToken' = [leaseToken EXCEPT ![l] = leaseToken[l] + 1]
    /\ UNCHANGED << granted, leaseExpiry, holdActual, retiredSpend, regrants,
                    clock >>

Quarantine(l) ==
    /\ leaseState[l] \in {"active", "draining"}
    /\ leaseState' = [leaseState EXCEPT ![l] = "quarantined"]
    /\ UNCHANGED << escrowFree, granted, leaseToken, leaseExpiry, holdState,
                    holdActual, holdToken, reclaimed, retiredSpend, regrants,
                    clock >>

\* REVIEW FIX: DRAINING is a real LeaseState in regional_quota_leases.py
\* (begin_drain) and was unreachable in the first version of this spec, so
\* neither safety nor liveness said anything about it. A draining lease stops
\* accepting new reservations but must still settle and reclaim.
BeginDrain(l) ==
    /\ leaseState[l] = "active"
    /\ leaseState' = [leaseState EXCEPT ![l] = "draining"]
    /\ UNCHANGED << escrowFree, granted, leaseToken, leaseExpiry, holdState,
                    holdActual, holdToken, reclaimed, retiredSpend, regrants,
                    clock >>

Next ==
    \/ \E l \in LeaseIds, a \in LeaseAmounts, e \in 1..MaxTime : Grant(l, a, e)
    \/ \E l \in LeaseIds, a \in LeaseAmounts, e \in 1..MaxTime : Regrant(l, a, e)
    \/ \E l \in LeaseIds, h \in HoldIds, t \in Tokens : Reserve(l, h, t)
    \/ \E l \in LeaseIds, h \in HoldIds, a \in 0..HoldAmount : Settle(l, h, a)
    \/ \E l \in LeaseIds, h \in HoldIds : Refund(l, h)
    \/ \E l \in LeaseIds, h \in HoldIds : StaleWrite(l, h)
    \/ \E l \in LeaseIds : Reclaim(l)
    \/ \E l \in LeaseIds : Quarantine(l)
    \/ \E l \in LeaseIds : BeginDrain(l)
    \/ Tick

Spec == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ WF_vars(\E l \in LeaseIds : Reclaim(l))

\* ------------------------------------------------------------- invariants ---

TypeOK ==
    /\ escrowFree \in 0..Escrow
    /\ clock \in 0..MaxTime
    /\ retiredSpend \in 0..Escrow
    /\ \A l \in LeaseIds :
        /\ leaseState[l] \in {"none","active","draining","closed","quarantined"}
        /\ granted[l] \in 0..Escrow
        /\ reclaimed[l] \in 0..Escrow
        /\ leaseToken[l] \in Tokens
        /\ regrants[l] \in 0..MaxRegrants
        /\ \A h \in HoldIds : holdToken[l][h] \in Tokens

\* S1 — NO OVERSUBSCRIPTION. What is free plus what is still committed to the
\* live generation of every lease never exceeds what the workspace escrowed. A
\* violation here is minted money: regional planes collectively holding more
\* than the workspace has. It is the weakest of the money laws and is kept
\* because it is the one a reader expects to find; S1a and S2b are the ones
\* that do the work, and the header records which mutations each of them
\* catches.
NoOversubscription ==
    escrowFree + TotalOutstanding <= Escrow

\* S1a — the same law as an EQUALITY, which is what "conserved" actually means.
\* The inequality above is also satisfied by a ledger that quietly loses money,
\* and lease succession is exactly where money gets lost: the retired
\* generation's spend has to go somewhere when its hold rows are archived.
\* Delete the `retiredSpend' = retiredSpend + SpentIn(l)` carry from Regrant
\* and this is the invariant that fires.
EscrowConserved ==
    escrowFree + TotalOutstanding + retiredSpend = Escrow

\* S1b — spend never exceeds the escrow, however the leases are interleaved.
SpendWithinEscrow ==
    TotalSpent <= Escrow

\* S2 — a lease never accounts for more than it was granted. This is the
\* invariant the Python __post_init__ asserts; here it must survive concurrent
\* reserve/settle/reclaim rather than one construction.
NoLeaseOverspend ==
    \A l \in IssuedLeases : AccountedIn(l) <= granted[l]

\* S2b — SPEND IS COVERED BY WHAT IS STILL COMMITTED. The sharp one, and the
\* one the fence turns out to be about.
\*
\* Neither S1 nor S1b can see a settle that lands after the sweep. Both are
\* computed from escrow MOVEMENTS (granted, reclaimed), and a late settle moves
\* no escrow — it only changes SpentIn, after reclaimed[l] was already computed
\* from the old SpentIn. The books balance and the money is still gone twice.
\* This invariant closes that gap by relating the two directly: whatever a
\* lease has spent must still be covered by the part of its grant that was not
\* handed back.
\*
\* WHY THIS IS NOT THE ATTRIBUTION INVARIANT, AND WHY THERE ISN'T ONE
\*
\* The obvious property to reach for here is attribution: "a settled hold
\* belongs to the generation that authorised it", i.e. holdToken[l][h] =
\* leaseToken[l] at settle time. Do not add it. It is the text of the Settle
\* guard with the word INVARIANT in front of it, so it holds exactly when the
\* guard is present and fails exactly when the guard is deleted — it proves
\* "the fence implies the fence". The money-flavoured variants are no better:
\* attribute a late charge to the generation that AUTHORISED it and it is
\* bounded by that generation's grant at reserve time; attribute it to the
\* generation whose grant it DRAWS DOWN and it is bounded by NoLeaseOverspend.
\* Either way the invariant is circular or already implied, so this file states
\* the conservation law instead and says plainly that it establishes nothing
\* about attribution. Reconciling against the regional plane's own ledger would
\* be a real requirement, but it needs a model of that ledger as an independent
\* record, which is a different spec.
SpendIsCoveredByCommitment ==
    \A l \in IssuedLeases : reclaimed[l] + SpentIn(l) <= granted[l]

\* S2c — the equality half of S2b, for leases that have been swept. A closed
\* lease has had its whole grant resolved: what the sweep withheld from escrow
\* is exactly what the generation spent. Weaker than S2b in one direction and
\* stronger in the other — it also catches a sweep that returns too LITTLE,
\* which strands customer money rather than minting it, and which no
\* inequality about oversubscription would ever notice.
ClosedLeaseBooksBalance ==
    \A l \in ClosedLeases : reclaimed[l] + SpentIn(l) = granted[l]

\* S3 — a settled hold's actual always fits inside its reservation, so a
\* settlement can never charge more than was held.
SettlementFitsReservation ==
    \A l \in LeaseIds, h \in HoldIds :
        holdState[l][h] = "settled" => holdActual[l][h] <= HoldAmount

\* S4 — the type assertion for holdState, which TypeOK does not cover.
\*
\* HONESTY NOTE, because the name oversells it. This used to be described as
\* "a hold is never both settled and refunded, which is what stops a
\* double-count on the reclaim path". It does not establish that. holdState is
\* one value per hold, so exclusivity is a consequence of the ENCODING and
\* cannot fail here whatever the actions do. That encoding is itself a
\* modelling assumption: the schema sketch stores a hold's state in one column
\* and its actual in another, so it holds there too, but an implementation
\* that recorded settlement and refund as separate facts could violate the
\* real property while this invariant stayed green. Kept because holdState
\* still needs a type check; renamed in spirit, not strengthened, because the
\* honest strengthening ("a hold's exit is terminal") is FALSE under
\* succession — Regrant returns a hold id to "none" for the next generation.
ExclusiveHoldExit ==
    \A l \in LeaseIds, h \in HoldIds :
        holdState[l][h] \in {"none","reserved","settled","refunded"}

\* S5 — reclaim never returns more than was granted.
ReclaimIsBounded ==
    \A l \in LeaseIds : reclaimed[l] <= granted[l]

\* S6 — an issued lease has a real token, and no hold is stamped with a token
\* its lease has not reached yet. Cheap, and it is the standing half of the
\* premise that StaleToken relies on.
TokenStampsAreWellFormed ==
    /\ \A l \in IssuedLeases : leaseToken[l] >= 1
    /\ \A l \in LeaseIds, h \in HoldIds : holdToken[l][h] <= leaseToken[l]

\* The invariants above are declared to TLC one by one in the .cfg rather than
\* bundled into a single `Safety == /\ ...` conjunction, which is what this
\* file used to do. A bundle reports "Invariant Safety is violated" and leaves
\* you to work out which clause failed by reading the dumped state — and the
\* whole method here is deleting a guard and asking WHICH law that guard was
\* holding up. Getting the answer as a name rather than as a puzzle is worth
\* the longer .cfg.

\* L1 — liveness: an expired lease is eventually closed, so escrow does not
\* stay stranded. Checked under weak fairness on Tick and Reclaim.
\*
\* This is the property that caught the quarantine gap. It deliberately covers
\* BOTH live states: a safety-only spec would have been perfectly happy with a
\* design that quietly keeps a customer's money forever.
\*
\* It survives succession unchanged because it is a leads-to over all leases at
\* all times: a regranted lease that expires again must be closed again. That
\* is worth stating because the cheap version of this property — "eventually
\* every lease is closed" — becomes FALSE under succession, and rewriting it
\* that way would have been the natural mistake.
ExpiredLeasesAreEventuallyReclaimed ==
    \A l \in LeaseIds :
        (leaseState[l] \in {"active", "draining", "quarantined"}
            /\ clock >= leaseExpiry[l]) ~> (leaseState[l] = "closed")

\* L2 — an ACTION property, not a state invariant: a lease's fencing token
\* never goes backwards. Every result in the header rests on this, because the
\* whole reason a superseded stamp stays superseded is that the row's token
\* only climbs; the soundness argument for StaleToken cites it by name.
\*
\* MEASURED, and the result is half negative. Two mutations of Regrant's token
\* handling, at the .cfg bounds:
\*
\*   RESET — `leaseToken' = [leaseToken EXCEPT ![l] = 1]`, the plausible "a new
\*   generation starts at token 1" mistake. This property is violated in five
\*   states. It is the ONLY check that catches it: re-run with L2 struck from
\*   PROPERTIES and nothing else fires (no error, 5,844,105 / 1,292,173). So L2
\*   earns its place, and this is what it earns it against.
\*
\*   REUSE — delete the bump, leaving leaseToken unchanged across succession.
\*   Nothing fires. Not this property, which is stated with >= and so admits
\*   equality, and no invariant either: no error, 5,844,105 / 1,292,173.
\*
\* An earlier version of this comment asserted that a reuse would be caught
\* here and would "quietly revive retired holders". Both halves were wrong, and
\* the second is why: Regrant wipes holdState, holdActual and holdToken three
\* lines before it touches the token, so this model has no retired holder left
\* to revive. That is the model's own choice, recorded as scope limit 3 — under
\* the schema the design doc actually sketches, hold rows keyed
\* (lease_id, hold_id) and NOT cleared at succession, a reuse would leave a
\* retired generation's stamp matching the live token, and nothing here checks
\* that. Stated plainly: at these bounds the succession-side bump is inert, and
\* L2 covers a reset and nothing else.
TokensNeverGoBackwards ==
    [][\A l \in LeaseIds : leaseToken'[l] >= leaseToken[l]]_vars

=============================================================================
