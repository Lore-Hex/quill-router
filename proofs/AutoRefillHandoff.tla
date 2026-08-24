--------------------------- MODULE AutoRefillHandoff ---------------------------
(***************************************************************************)
(* Settlement-to-charge handoff for auto-refill. Settlement runs on the     *)
(* internal surface, which deliberately has no Stripe credential. A         *)
(* threshold-crossing settlement atomically creates durable pending work; a *)
(* control-owned drain claims that work and is the only site allowed to      *)
(* submit a charge.                                                         *)
(*                                                                         *)
(* Multiple deliveries model duplicate enqueue/event delivery. Transient    *)
(* drain failures and CrashProcess return claimed work to pending. A crash   *)
(* may occur after Stripe has completed the charge but before the durable    *)
(* row records success; providerCharged is the idempotency-key result that   *)
(* makes the retry observe, rather than repeat, that charge. Permanent or    *)
(* exhausted failure is terminal only when explicitly recorded.             *)
(*                                                                         *)
(* The reviewed pre-fix path called Stripe directly from settlement. With   *)
(* no key it returned stripe_not_configured and left no work behind. The     *)
(* NoSilentDrop mutant assigns "forgotten" instead of "pending" in          *)
(* SettleAndEnqueue and is that defect as one atomic handoff mutation.       *)
(*                                                                         *)
(* MUTATION CHECKS (TLC -workers 1, restored after every run)               *)
(*                                                                         *)
(* NoDoubleCharge: delete ~providerCharged[e] from SubmitCharge. Violated   *)
(*   in 5 states: SettleAndEnqueue -> Claim -> SubmitCharge -> SubmitCharge. *)
(* NoSilentDrop: replace SettleAndEnqueue's pending delivery with forgotten *)
(*   and zero deliveries. Violated in 2 states: SettleAndEnqueue.           *)
(* ChargeOnlyWhereCredentialed: delete site = "control" from SubmitCharge.  *)
(*   Violated in 3 states: SettleAndEnqueue -> Claim; the internal-site      *)
(*   charge is then ENABLED, which is what the invariant directly checks.   *)
(*                                                                         *)
(* Every quoted trace is the shortest single-worker breadth-first trace.    *)
(* The restored model passes at the counts in AutoRefillHandoff.cfg.         *)
(***************************************************************************)

EXTENDS Naturals

CONSTANTS NumSettlements, MaxDeliveries, MaxAttempts, MaxCrashes

ASSUME /\ NumSettlements \in Nat \ {0}
       /\ MaxDeliveries \in Nat \ {0}
       /\ MaxAttempts \in Nat \ {0}
       /\ MaxCrashes \in Nat

Settlements == 1..NumSettlements
Surfaces == {"internal", "control"}
Statuses == {"unsettled", "pending", "charging", "charged", "failed", "forgotten"}
TerminalStatuses == {"charged", "failed"}

VARIABLES
    settled,
    status,
    deliveries,
    attempts,
    providerCharged,
    completedCharges,
    drainOwner,
    crashCount

vars == <<
    settled, status, deliveries, attempts, providerCharged,
    completedCharges, drainOwner, crashCount
>>

Init ==
    /\ settled = [e \in Settlements |-> FALSE]
    /\ status = [e \in Settlements |-> "unsettled"]
    /\ deliveries = [e \in Settlements |-> 0]
    /\ attempts = [e \in Settlements |-> 0]
    /\ providerCharged = [e \in Settlements |-> FALSE]
    /\ completedCharges = [e \in Settlements |-> 0]
    /\ drainOwner = [e \in Settlements |-> "none"]
    /\ crashCount = 0

(* Settlement and durable enqueue are one storage transaction. *)
SettleAndEnqueue(e) ==
    /\ e \in Settlements
    /\ ~settled[e]
    /\ settled' = [settled EXCEPT ![e] = TRUE]
    /\ status' = [status EXCEPT ![e] = "pending"]
    /\ deliveries' = [deliveries EXCEPT ![e] = 1]
    /\ UNCHANGED <<
        attempts, providerCharged, completedCharges, drainOwner, crashCount
       >>

DuplicateEnqueue(e) ==
    /\ e \in Settlements
    /\ settled[e]
    /\ status[e] \in {"pending", "charging"}
    /\ deliveries[e] < MaxDeliveries
    /\ deliveries' = [deliveries EXCEPT ![e] = @ + 1]
    /\ UNCHANGED <<
        settled, status, attempts, providerCharged, completedCharges,
        drainOwner, crashCount
       >>

DrainIdle == \A e \in Settlements : status[e] # "charging"

ClaimOnControlDrain(e) ==
    /\ e \in Settlements
    /\ status[e] = "pending"
    /\ deliveries[e] > 0
    /\ attempts[e] < MaxAttempts \/ providerCharged[e]
    /\ DrainIdle
    /\ status' = [status EXCEPT ![e] = "charging"]
    /\ attempts' = [attempts EXCEPT
          ![e] = IF providerCharged[e] THEN @ ELSE @ + 1]
    /\ drainOwner' = [drainOwner EXCEPT ![e] = "control"]
    /\ UNCHANGED <<
        settled, deliveries, providerCharged, completedCharges, crashCount
       >>

(* Stripe's idempotency record is durable before the local outcome record. *)
SubmitCharge(e, site) ==
    /\ e \in Settlements
    /\ site \in Surfaces
    /\ site = "control"
    /\ status[e] = "charging"
    /\ drainOwner[e] = "control"
    /\ ~providerCharged[e]
    /\ completedCharges[e] < 2
    /\ providerCharged' = [providerCharged EXCEPT ![e] = TRUE]
    /\ completedCharges' = [completedCharges EXCEPT ![e] = @ + 1]
    /\ UNCHANGED <<
        settled, status, deliveries, attempts, drainOwner, crashCount
       >>

RecordChargeSuccess(e) ==
    /\ e \in Settlements
    /\ status[e] = "charging"
    /\ drainOwner[e] = "control"
    /\ providerCharged[e]
    /\ status' = [status EXCEPT ![e] = "charged"]
    /\ deliveries' = [deliveries EXCEPT ![e] = 0]
    /\ drainOwner' = [drainOwner EXCEPT ![e] = "none"]
    /\ UNCHANGED <<
        settled, attempts, providerCharged, completedCharges, crashCount
       >>

RecordPermanentFailure(e) ==
    /\ e \in Settlements
    /\ status[e] = "charging"
    /\ drainOwner[e] = "control"
    /\ ~providerCharged[e]
    /\ status' = [status EXCEPT ![e] = "failed"]
    /\ deliveries' = [deliveries EXCEPT ![e] = 0]
    /\ drainOwner' = [drainOwner EXCEPT ![e] = "none"]
    /\ UNCHANGED <<
        settled, attempts, providerCharged, completedCharges, crashCount
       >>

RetryTransientFailure(e) ==
    /\ e \in Settlements
    /\ status[e] = "charging"
    /\ drainOwner[e] = "control"
    /\ ~providerCharged[e]
    /\ attempts[e] < MaxAttempts
    /\ status' = [status EXCEPT ![e] = "pending"]
    /\ drainOwner' = [drainOwner EXCEPT ![e] = "none"]
    /\ UNCHANGED <<
        settled, deliveries, attempts, providerCharged, completedCharges,
        crashCount
       >>

RecordExhaustedFailure(e) ==
    /\ e \in Settlements
    /\ status[e] = "pending"
    /\ attempts[e] = MaxAttempts
    /\ ~providerCharged[e]
    /\ status' = [status EXCEPT ![e] = "failed"]
    /\ deliveries' = [deliveries EXCEPT ![e] = 0]
    /\ UNCHANGED <<
        settled, attempts, providerCharged, completedCharges, drainOwner,
        crashCount
       >>

CrashProcess ==
    /\ crashCount < MaxCrashes
    /\ \E e \in Settlements : status[e] \in {"pending", "charging"}
    /\ crashCount' = crashCount + 1
    /\ status' = [e \in Settlements |->
          IF status[e] = "charging" THEN "pending" ELSE status[e]]
    /\ drainOwner' = [e \in Settlements |->
          IF drainOwner[e] = "control" THEN "none" ELSE drainOwner[e]]
    /\ UNCHANGED <<
        settled, deliveries, attempts, providerCharged, completedCharges
       >>

Next ==
    \/ \E e \in Settlements : SettleAndEnqueue(e)
    \/ \E e \in Settlements : DuplicateEnqueue(e)
    \/ \E e \in Settlements : ClaimOnControlDrain(e)
    \/ \E e \in Settlements, site \in Surfaces : SubmitCharge(e, site)
    \/ \E e \in Settlements : RecordChargeSuccess(e)
    \/ \E e \in Settlements : RecordPermanentFailure(e)
    \/ \E e \in Settlements : RetryTransientFailure(e)
    \/ \E e \in Settlements : RecordExhaustedFailure(e)
    \/ CrashProcess

(* The weak-fairness assumption is the model's "assuming the drain runs". *)
Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

NoDoubleCharge ==
    \A e \in Settlements : completedCharges[e] <= 1

NoSilentDrop ==
    \A e \in Settlements :
        settled[e] => status[e] \in {"pending", "charging", "charged", "failed"}

ChargeOnlyWhereCredentialed ==
    \A e \in Settlements : ~ENABLED SubmitCharge(e, "internal")

EventuallyCharged ==
    \A e \in Settlements : settled[e] ~> status[e] \in TerminalStatuses

=============================================================================
