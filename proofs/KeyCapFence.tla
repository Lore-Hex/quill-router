---------------------------- MODULE KeyCapFence ----------------------------
(***************************************************************************)
(* Adding a cap to a key while leases may hold its requests, from          *)
(* docs/design/fast-admission-and-batched-settlement.md section 4.6:        *)
(* the key-status version, the owners' caches, the checkpoint that shows    *)
(* none of the key's holds open, and the condition on which Python enables  *)
(* the cap.                                                                 *)
(*                                                                          *)
(* This is written before the code. Nothing implements it yet.              *)
(*                                                                          *)
(* The rule being checked is Invariant 8: a new cap takes effect only after *)
(* every fast hold of the key is booked.                                    *)
(*                                                                          *)
(* ACTORS                                                                   *)
(*                                                                          *)
(*   Python. Adding a cap bumps the workspace's key-status version, and the *)
(*   key answers 503 until Python enables the cap. It enables it once every *)
(*   lease that could hold one of the key's earlier holds has finished      *)
(*   draining, or has a booked checkpoint that applies the change and shows *)
(*   none of the key's holds open.                                          *)
(*                                                                          *)
(*   Grants. A grant carries the key-status version current when it was    *)
(*   made.                                                                  *)
(*                                                                          *)
(*   Owners, one per lease. An owner admits under a lease only with a       *)
(*   key-status cache at least as new as the lease's version, and stops     *)
(*   admitting the key once its cache shows the change. It decides each     *)
(*   hold's terminal and publishes checkpoints, each with the cache         *)
(*   version it applies and the key's holds it shows open.                  *)
(*                                                                          *)
(*   The auditor. It books each lease's records in order. A lease drains at *)
(*   any moment and closes once every hold it admitted is booked.           *)
(*                                                                          *)
(* WHAT IS ABSTRACTED                                                       *)
(*                                                                          *)
(*   - One workspace, one key, and one change: the cap. A window limit and  *)
(*     `budget_strict` move the key the same way.                           *)
(*   - Amounts. A hold is booked or not; what it charges is CreditDebt's.   *)
(*   - When a lease drains and closes is LeaseLifecycle's and               *)
(*     TerminalOrder's. Here a lease drains at any moment, and closes only  *)
(*     once the auditor has booked a terminal for every hold it admitted.   *)
(*   - Records are published and stored at once, in order: the order and    *)
(*     the auditor's commits are TerminalOrder's and AuditorCommit's.       *)
(*                                                                          *)
(* ASSUMPTIONS, each with a mutant that widens it (proofs/manifest.toml)    *)
(*                                                                          *)
(*   A1. A checkpoint shows the cache version its owner applies and the     *)
(*       key's holds open in its books: the owner's code, which no rule of  *)
(*       Spanner's keeps. Mutant a-checkpoint-ahead-of-the-cache.           *)
(*                                                                          *)
(* THE CLAIM                                                                *)
(*                                                                          *)
(*   CapAfterBooking. Once Python has enabled the cap, no fast hold of the  *)
(*   key is open or unbooked: the key's booked usage includes every fast    *)
(*   hold, and none is admitted after. A cleanup of the key's              *)
(*   `tr_key_limit` rows waits on the same condition (section 4.6), so the  *)
(*   claim covers it too.                                                   *)
(*                                                                          *)
(* Each condition of the enabling rule holds the claim up: the grant's      *)
(* version in admission (mutant admit-without-the-grant-version), a         *)
(* checkpoint that applies the change (cleared-by-any-checkpoint), one that *)
(* shows no hold open (cleared-with-holds-open), one the auditor has booked *)
(* (cleared-before-booked), and draining leases among those that could     *)
(* hold the key (enable-ignores-draining-leases).                           *)
(***************************************************************************)

EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    Leases,     \* the workspace's leases
    MaxAdmit,   \* the key's holds an owner admits under one lease
    MaxCkpt     \* checkpoints an owner publishes under one lease

NoCap == 0

VARIABLES
    kv,         \* the workspace's key-status version in Spanner
    capAt,      \* the version that added the cap, or NoCap
    capOn,      \* Python has enabled the cap
    lst,        \* per lease: "none", "open", "draining" or "closed"
    gv,         \* per lease: the version its grant carried
    cache,      \* per lease: its owner's key-status cache version
    open,       \* per lease: the key's holds open in its owner's books
    admitted,   \* per lease: the key's holds it admitted
    ckpts,      \* per lease: checkpoints published
    log,        \* per lease: the records published, in order
    booked,     \* per lease: how many of them the auditor has booked
    fastOpen    \* the key's fast holds admitted and not yet booked

vars == << kv, capAt, capOn, lst, gv, cache, open, admitted, ckpts, log, booked, fastOpen >>

----------------------------------------------------------------------------

\* The leases that could hold one of the key's holds from before the change:
\* every lease granted with an older version.
Earlier == { l \in Leases : lst[l] # "none" /\ gv[l] < capAt }

\* A checkpoint of l that the auditor has booked, applies the change, and
\* shows none of the key's holds open.
Cleared(l) ==
    \E i \in 1..booked[l] :
        /\ log[l][i].k = "ckpt"
        /\ log[l][i].ver >= capAt
        /\ log[l][i].open = 0

----------------------------------------------------------------------------

Grant(l) ==
    /\ lst[l] = "none"
    /\ lst' = [lst EXCEPT ![l] = "open"]
    /\ gv' = [gv EXCEPT ![l] = kv]
    /\ UNCHANGED << kv, capAt, capOn, cache, open, admitted, ckpts, log, booked, fastOpen >>

\* The owner's cache reads the version Spanner has now.
Refresh(l) ==
    /\ lst[l] # "none"
    /\ cache[l] < kv
    /\ cache' = [cache EXCEPT ![l] = kv]
    /\ UNCHANGED << kv, capAt, capOn, lst, gv, open, admitted, ckpts, log, booked, fastOpen >>

\* The owner admits one of the key's requests.
Admit(l) ==
    /\ lst[l] = "open"
    /\ admitted[l] < MaxAdmit
    /\ cache[l] >= gv[l]
    /\ capAt = NoCap \/ cache[l] < capAt
    /\ open' = [open EXCEPT ![l] = @ + 1]
    /\ admitted' = [admitted EXCEPT ![l] = @ + 1]
    /\ fastOpen' = fastOpen + 1
    /\ UNCHANGED << kv, capAt, capOn, lst, gv, cache, ckpts, log, booked >>

\* The owner decides a terminal for one of the key's holds and publishes it.
Terminal(l) ==
    /\ open[l] > 0
    /\ open' = [open EXCEPT ![l] = @ - 1]
    /\ log' = [log EXCEPT ![l] = Append(@, [k |-> "term", ver |-> 0, open |-> 0])]
    /\ UNCHANGED << kv, capAt, capOn, lst, gv, cache, admitted, ckpts, booked, fastOpen >>

\* The owner publishes a checkpoint: the cache version it applies and the
\* key's holds open in its books (A1).
Checkpoint(l) ==
    /\ lst[l] = "open"
    /\ ckpts[l] < MaxCkpt
    /\ log' = [log EXCEPT ![l] = Append(@, [k |-> "ckpt", ver |-> cache[l], open |-> open[l]])]
    /\ ckpts' = [ckpts EXCEPT ![l] = @ + 1]
    /\ UNCHANGED << kv, capAt, capOn, lst, gv, cache, open, admitted, booked, fastOpen >>

\* The auditor books the lease's next record.
Book(l) ==
    /\ booked[l] < Len(log[l])
    /\ booked' = [booked EXCEPT ![l] = @ + 1]
    /\ fastOpen' = IF log[l][booked[l] + 1].k = "term" THEN fastOpen - 1 ELSE fastOpen
    /\ UNCHANGED << kv, capAt, capOn, lst, gv, cache, open, admitted, ckpts, log >>

Drain(l) ==
    /\ lst[l] = "open"
    /\ lst' = [lst EXCEPT ![l] = "draining"]
    /\ UNCHANGED << kv, capAt, capOn, gv, cache, open, admitted, ckpts, log, booked, fastOpen >>

\* A draining lease closes once every hold it admitted has a booked terminal.
Close(l) ==
    /\ lst[l] = "draining"
    /\ open[l] = 0
    /\ booked[l] = Len(log[l])
    /\ lst' = [lst EXCEPT ![l] = "closed"]
    /\ UNCHANGED << kv, capAt, capOn, gv, cache, open, admitted, ckpts, log, booked, fastOpen >>

\* Adding the cap bumps the key-status version. The key answers 503 until
\* the cap is enabled.
AddCap ==
    /\ capAt = NoCap
    /\ kv' = kv + 1
    /\ capAt' = kv + 1
    /\ UNCHANGED << capOn, lst, gv, cache, open, admitted, ckpts, log, booked, fastOpen >>

EnableCap ==
    /\ capAt # NoCap
    /\ ~capOn
    /\ \A l \in Earlier : lst[l] = "closed" \/ Cleared(l)
    /\ capOn' = TRUE
    /\ UNCHANGED << kv, capAt, lst, gv, cache, open, admitted, ckpts, log, booked, fastOpen >>

----------------------------------------------------------------------------

Init ==
    /\ kv = 0
    /\ capAt = NoCap
    /\ capOn = FALSE
    /\ lst = [l \in Leases |-> "none"]
    /\ gv = [l \in Leases |-> 0]
    /\ cache = [l \in Leases |-> 0]
    /\ open = [l \in Leases |-> 0]
    /\ admitted = [l \in Leases |-> 0]
    /\ ckpts = [l \in Leases |-> 0]
    /\ log = [l \in Leases |-> << >>]
    /\ booked = [l \in Leases |-> 0]
    /\ fastOpen = 0

Next ==
    \/ AddCap
    \/ EnableCap
    \/ \E l \in Leases :
          \/ Grant(l)
          \/ Refresh(l)
          \/ Admit(l)
          \/ Terminal(l)
          \/ Checkpoint(l)
          \/ Book(l)
          \/ Drain(l)
          \/ Close(l)

Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
\* Claims

TypeOK ==
    /\ kv \in 0..1
    /\ capAt \in {NoCap} \cup 1..1
    /\ capOn \in BOOLEAN
    /\ lst \in [Leases -> {"none", "open", "draining", "closed"}]
    /\ gv \in [Leases -> 0..1]
    /\ cache \in [Leases -> 0..1]
    /\ \A l \in Leases :
          /\ open[l] \in 0..MaxAdmit
          /\ admitted[l] \in 0..MaxAdmit
          /\ ckpts[l] \in 0..MaxCkpt
          /\ Len(log[l]) <= MaxAdmit + MaxCkpt
          /\ booked[l] \in 0..Len(log[l])
    /\ fastOpen \in 0..(MaxAdmit * Cardinality(Leases))

\* Invariant 8: a cap in effect has every fast hold of the key booked.
CapAfterBooking == capOn => fastOpen = 0

=============================================================================
