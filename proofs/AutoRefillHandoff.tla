--------------------------- MODULE AutoRefillHandoff ---------------------------
(***************************************************************************)
(* Settlement-to-charge handoff for auto-refill across an internal/control *)
(* surface split with a temporary combined-surface rollback.                *)
(*                                                                         *)
(* A row has an origin and a settlement-outbox lifecycle independent of its *)
(* refill sub-work. In particular, a pre-existing combined-origin row has no *)
(* refill attached. Internal delivery must confirm a real NULL -> pending   *)
(* attachment before finalization, even if that row is leased or terminal.  *)
(*                                                                         *)
(* Stripe idempotency is modeled as a key -> PaymentIntent relation, not as *)
(* a charged boolean. Inline combined and control-drain initiation are      *)
(* separate actions. Stripe may accept a request whose confirmation is lost *)
(* or delayed; after the pending-suppression window and minute bucket move, *)
(* the durable refill can retry. Every path intentionally derives the same  *)
(* settlement key in the restored model.                                   *)
(*                                                                         *)
(* MUTATION CHECKS (TLC -workers 1, restored after every run)               *)
(*                                                                         *)
(* NoDoubleCharge: delete requestKey[e] \notin AssignedKeys(e) from         *)
(*   StripeCreatePaymentIntent. Violated in 12 states: internal start ->    *)
(*   attach -> finalize -> claim -> initiate -> create -> defer -> expire -> *)
(*   claim -> initiate -> create. Two PaymentIntent IDs share one key.       *)
(* NoSilentDrop: delete attachmentConfirmed[e] from FinalizeSettlement.     *)
(*   Violated in 3 states: internal start -> finalize, refill still none.    *)
(* ChargeOnlyWhereCredentialed: delete site \in CredentialedSurfaces from  *)
(*   InitiateCharge. Violated in 4 states: internal start -> attach ->       *)
(*   finalize, where internal initiation is then enabled.                   *)
(*                                                                         *)
(* P1-A, different path keys: change InlineKey to BucketKey(e, bucket[e]).  *)
(*   NoDoubleCharge is violated in 14 states: internal start -> attach ->    *)
(*   inline-finalize failure -> rollback redelivery -> combined finalize ->  *)
(*   inline initiate/create -> control claim/defer -> suppression expiry ->  *)
(*   control claim/initiate/create. IDs 1 and 2 have minute and settlement   *)
(*   keys respectively while the first confirmation remains delayed.        *)
(* P1-B, vacuous attachment: for a combined-origin leased/dead row, leave   *)
(*   refillStatus none but set attachmentConfirmed TRUE. NoSilentDrop is     *)
(*   violated in 6 states for each single-worker mutant: create combined row *)
(*   -> lease (or dead-letter) -> internal start -> vacuous attach ->        *)
(*   finalize. The terminal run specifically reached rowState = "dead".     *)
(*                                                                         *)
(* Every quoted trace is the shortest single-worker breadth-first trace.    *)
(* The restored model passes at the counts in AutoRefillHandoff.cfg.         *)
(***************************************************************************)

EXTENDS Naturals, FiniteSets

CONSTANTS
    NumSettlements,
    MaxDeliveries,
    MaxAttempts,
    MaxCrashes,
    MaxBuckets,
    MaxPaymentIntents

ASSUME /\ NumSettlements \in Nat \ {0}
       /\ MaxDeliveries \in Nat \ {0}
       /\ MaxAttempts \in Nat \ {0}
       /\ MaxCrashes \in Nat
       /\ MaxBuckets \in Nat \ {0}
       /\ MaxPaymentIntents \in Nat \ {0}

Settlements == 1..NumSettlements
IntentIds == 1..MaxPaymentIntents
Surfaces == {"none", "internal", "combined", "control"}
DeliverySurfaces == {"internal", "combined"}
CredentialedSurfaces == {"combined", "control"}
Origins == {"none", "combined", "internal"}
RowStates == {"absent", "pending", "leased", "done", "dead"}
TerminalRowStates == {"done", "dead"}
RefillStates == {"none", "pending", "charging", "charged", "failed-recorded"}
AttachedRefillStates == RefillStates \ {"none"}
TerminalRefillStates == {"charged", "failed-recorded"}
Confirmations == {"none", "pending", "lost", "confirmed"}
Suppressions == {"clear", "fresh", "expired"}

NoKey == <<"none", 0, 0>>
SettlementKey(e) == <<"settlement", e, 0>>
BucketKey(e, b) == <<"minute", e, b>>

VARIABLES
    thresholdCrossed,
    finalized,
    deliverySurface,
    deliveryCount,
    rowOrigin,
    rowState,
    refillStatus,
    attachmentConfirmed,
    finalizeFailed,
    bucket,
    submissionAttempts,
    requestKey,
    requestSite,
    paymentIntents,
    stripeKey,
    confirmation,
    lastSubmitSite,
    suppression,
    refillDue,
    crashCount

(* MUTATION P1-A: replace SettlementKey(e) with BucketKey(e, bucket[e]). *)
InlineKey(e) == SettlementKey(e)
ControlKey(e) == SettlementKey(e)
InternalKey(e) == SettlementKey(e)

vars == <<
    thresholdCrossed, finalized, deliverySurface, deliveryCount,
    rowOrigin, rowState, refillStatus, attachmentConfirmed, finalizeFailed,
    bucket, submissionAttempts, requestKey, requestSite, paymentIntents,
    stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
    crashCount
>>

AssignedKeys(e) == {stripeKey[e][i] : i \in paymentIntents[e]}

Init ==
    /\ thresholdCrossed = [e \in Settlements |-> FALSE]
    /\ finalized = [e \in Settlements |-> FALSE]
    /\ deliverySurface = [e \in Settlements |-> "none"]
    /\ deliveryCount = [e \in Settlements |-> 0]
    /\ rowOrigin = [e \in Settlements |-> "none"]
    /\ rowState = [e \in Settlements |-> "absent"]
    /\ refillStatus = [e \in Settlements |-> "none"]
    /\ attachmentConfirmed = [e \in Settlements |-> FALSE]
    /\ finalizeFailed = [e \in Settlements |-> FALSE]
    /\ bucket = [e \in Settlements |-> 1]
    /\ submissionAttempts = [e \in Settlements |-> 0]
    /\ requestKey = [e \in Settlements |-> NoKey]
    /\ requestSite = [e \in Settlements |-> "none"]
    /\ paymentIntents = [e \in Settlements |-> {}]
    /\ stripeKey = [e \in Settlements |-> [i \in IntentIds |-> NoKey]]
    /\ confirmation = [e \in Settlements |-> "none"]
    /\ lastSubmitSite = [e \in Settlements |-> "none"]
    /\ suppression = [e \in Settlements |-> "clear"]
    /\ refillDue = [e \in Settlements |-> FALSE]
    /\ crashCount = 0

(* A combined deployment can have written this row before refill attachment *)
(* existed. It deliberately starts with every refill field absent.          *)
CreateCombinedRow(e) ==
    /\ e \in Settlements
    /\ rowState[e] = "absent"
    /\ ~thresholdCrossed[e]
    /\ rowOrigin' = [rowOrigin EXCEPT ![e] = "combined"]
    /\ rowState' = [rowState EXCEPT ![e] = "pending"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

LeaseSettlementRow(e) ==
    /\ e \in Settlements
    /\ rowState[e] = "pending"
    /\ rowState' = [rowState EXCEPT ![e] = "leased"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

FinishSettlementRow(e) ==
    /\ e \in Settlements
    /\ rowState[e] \in {"pending", "leased"}
    /\ rowState' = [rowState EXCEPT ![e] = "done"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

DeadLetterSettlementRow(e) ==
    /\ e \in Settlements
    /\ rowState[e] \in {"pending", "leased"}
    /\ rowState' = [rowState EXCEPT ![e] = "dead"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

(* The first internal delivery creates a row when none pre-dates it. Refill  *)
(* attachment is split into its own step so a crash cannot look atomic here. *)
StartInternalWithNewRow(e) ==
    /\ e \in Settlements
    /\ ~thresholdCrossed[e]
    /\ rowState[e] = "absent"
    /\ thresholdCrossed' = [thresholdCrossed EXCEPT ![e] = TRUE]
    /\ deliverySurface' = [deliverySurface EXCEPT ![e] = "internal"]
    /\ deliveryCount' = [deliveryCount EXCEPT ![e] = 1]
    /\ rowOrigin' = [rowOrigin EXCEPT ![e] = "internal"]
    /\ rowState' = [rowState EXCEPT ![e] = "pending"]
    /\ UNCHANGED <<
        finalized, refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

(* An internal delivery can collide with a row written by combined earlier, *)
(* including while the old row is leased or after it became terminal.       *)
StartInternalWithExistingRow(e) ==
    /\ e \in Settlements
    /\ ~thresholdCrossed[e]
    /\ rowOrigin[e] = "combined"
    /\ rowState[e] # "absent"
    /\ thresholdCrossed' = [thresholdCrossed EXCEPT ![e] = TRUE]
    /\ deliverySurface' = [deliverySurface EXCEPT ![e] = "internal"]
    /\ deliveryCount' = [deliveryCount EXCEPT ![e] = 1]
    /\ UNCHANGED <<
        finalized, rowOrigin, rowState, refillStatus, attachmentConfirmed,
        finalizeFailed, bucket, submissionAttempts, requestKey, requestSite,
        paymentIntents, stripeKey, confirmation, lastSubmitSite, suppression,
        refillDue, crashCount
       >>

(* Correct attachment is independent of the settlement row's lease/terminal *)
(* state. Confirmation means the refill fields really are present.          *)
AttachRefill(e) ==
    /\ e \in Settlements
    /\ thresholdCrossed[e]
    /\ deliverySurface[e] = "internal"
    /\ rowState[e] # "absent"
    /\ refillStatus[e] = "none"
    /\ refillStatus' = [refillStatus EXCEPT ![e] = "pending"]
    /\ attachmentConfirmed' = [attachmentConfirmed EXCEPT ![e] = TRUE]
    /\ refillDue' = [refillDue EXCEPT ![e] = TRUE]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, finalizeFailed, bucket, submissionAttempts,
        requestKey, requestSite, paymentIntents, stripeKey, confirmation,
        lastSubmitSite, suppression, crashCount
       >>

(* A storage failure can still reject attachment. Against the historically  *)
(* problematic combined-origin leased/terminal rows, the fixed caller ends  *)
(* this delivery without finalizing rather than treating no exception as    *)
(* confirmation. A later delivery is outside the two-delivery rollback path. *)
AttachmentFailsAndCallerRefuses(e) ==
    /\ e \in Settlements
    /\ thresholdCrossed[e]
    /\ ~finalized[e]
    /\ deliverySurface[e] = "internal"
    /\ rowOrigin[e] = "combined"
    /\ rowState[e] \in {"leased", "done", "dead"}
    /\ refillStatus[e] = "none"
    /\ ~attachmentConfirmed[e]
    /\ deliverySurface' = [deliverySurface EXCEPT ![e] = "none"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliveryCount, rowOrigin, rowState,
        refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

(* The internal inline finalize may fail after attachment. The URL-map       *)
(* rollback then makes the same delivery reach combined.                    *)
InternalFinalizeFailure(e) ==
    /\ e \in Settlements
    /\ thresholdCrossed[e]
    /\ ~finalized[e]
    /\ deliverySurface[e] = "internal"
    /\ attachmentConfirmed[e]
    /\ ~finalizeFailed[e]
    /\ finalizeFailed' = [finalizeFailed EXCEPT ![e] = TRUE]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, refillStatus, attachmentConfirmed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

RollbackRedeliveryToCombined(e) ==
    /\ e \in Settlements
    /\ finalizeFailed[e]
    /\ ~finalized[e]
    /\ deliverySurface[e] = "internal"
    /\ deliveryCount[e] < MaxDeliveries
    /\ deliverySurface' = [deliverySurface EXCEPT ![e] = "combined"]
    /\ deliveryCount' = [deliveryCount EXCEPT ![e] = @ + 1]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, rowOrigin, rowState, refillStatus,
        attachmentConfirmed, finalizeFailed, bucket, submissionAttempts,
        requestKey, requestSite, paymentIntents, stripeKey, confirmation,
        lastSubmitSite, suppression, refillDue, crashCount
       >>

FinalizeSettlement(e, site) ==
    /\ e \in Settlements
    /\ site \in DeliverySurfaces
    /\ thresholdCrossed[e]
    /\ ~finalized[e]
    /\ deliverySurface[e] = site
    /\ (site = "combined" \/ ~finalizeFailed[e])
    /\ attachmentConfirmed[e]
    /\ finalized' = [finalized EXCEPT ![e] = TRUE]
    /\ UNCHANGED <<
        thresholdCrossed, deliverySurface, deliveryCount, rowOrigin, rowState,
        refillStatus, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

ClaimControlDrain(e) ==
    /\ e \in Settlements
    /\ finalized[e]
    /\ refillStatus[e] = "pending"
    /\ refillDue[e]
    /\ requestKey[e] = NoKey
    /\ submissionAttempts[e] < MaxAttempts
    /\ refillStatus' = [refillStatus EXCEPT ![e] = "charging"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, refillDue,
        crashCount
       >>

(* A fresh pending marker makes the claimed drain row retry later. *)
DeferControlForFreshSuppression(e) ==
    /\ e \in Settlements
    /\ refillStatus[e] = "charging"
    /\ suppression[e] = "fresh"
    /\ requestKey[e] = NoKey
    /\ refillStatus' = [refillStatus EXCEPT ![e] = "pending"]
    /\ refillDue' = [refillDue EXCEPT ![e] = FALSE]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, crashCount
       >>

ChargeReady(e, site) ==
    \/ /\ site = "internal"
       /\ finalized[e]
       /\ deliverySurface[e] = "internal"
       /\ refillStatus[e] = "pending"
    \/ /\ site = "combined"
       /\ finalized[e]
       /\ deliverySurface[e] = "combined"
       /\ refillStatus[e] = "pending"
       /\ suppression[e] # "fresh"
       /\ submissionAttempts[e] = 0
    \/ /\ site = "control"
       /\ finalized[e]
       /\ refillStatus[e] = "charging"
       /\ suppression[e] # "fresh"

ChargeKey(e, site) ==
    IF site = "combined" THEN InlineKey(e)
    ELSE IF site = "control" THEN ControlKey(e)
    ELSE InternalKey(e)

InitiateCharge(e, site) ==
    /\ e \in Settlements
    /\ site \in Surfaces
    /\ site \in CredentialedSurfaces
    /\ ChargeReady(e, site)
    /\ requestKey[e] = NoKey
    /\ submissionAttempts[e] < MaxAttempts
    /\ requestKey' = [requestKey EXCEPT ![e] = ChargeKey(e, site)]
    /\ requestSite' = [requestSite EXCEPT ![e] = site]
    /\ submissionAttempts' = [submissionAttempts EXCEPT ![e] = @ + 1]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, refillStatus, attachmentConfirmed, finalizeFailed,
        bucket, paymentIntents, stripeKey, confirmation, lastSubmitSite,
        suppression, refillDue, crashCount
       >>

InitiateInlineCharge(e) == InitiateCharge(e, "combined")
InitiateDrainCharge(e) == InitiateCharge(e, "control")

(* Stripe creates a fresh PaymentIntent only when this key is unseen.       *)
(* Removing the AssignedKeys guard mutates provider idempotency itself.      *)
StripeCreatePaymentIntent(e, i) ==
    /\ e \in Settlements
    /\ i \in IntentIds
    /\ requestKey[e] # NoKey
    /\ i \notin paymentIntents[e]
    /\ requestKey[e] \notin AssignedKeys(e)
    /\ paymentIntents' = [paymentIntents EXCEPT ![e] = @ \cup {i}]
    /\ stripeKey' = [stripeKey EXCEPT ![e][i] = requestKey[e]]
    /\ confirmation' = [confirmation EXCEPT ![e] = "pending"]
    /\ lastSubmitSite' = [lastSubmitSite EXCEPT ![e] = requestSite[e]]
    /\ suppression' = [suppression EXCEPT ![e] = "fresh"]
    /\ requestKey' = [requestKey EXCEPT ![e] = NoKey]
    /\ requestSite' = [requestSite EXCEPT ![e] = "none"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, refillStatus, attachmentConfirmed, finalizeFailed,
        bucket, submissionAttempts, refillDue, crashCount
       >>

(* A retry under a known key returns the same PaymentIntent identity. *)
StripeReturnExistingPaymentIntent(e, i) ==
    /\ e \in Settlements
    /\ i \in paymentIntents[e]
    /\ requestKey[e] # NoKey
    /\ stripeKey[e][i] = requestKey[e]
    /\ confirmation' = [confirmation EXCEPT ![e] = "pending"]
    /\ lastSubmitSite' = [lastSubmitSite EXCEPT ![e] = requestSite[e]]
    /\ suppression' = [suppression EXCEPT ![e] = "fresh"]
    /\ requestKey' = [requestKey EXCEPT ![e] = NoKey]
    /\ requestSite' = [requestSite EXCEPT ![e] = "none"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, refillStatus, attachmentConfirmed, finalizeFailed,
        bucket, submissionAttempts, paymentIntents, stripeKey, refillDue,
        crashCount
       >>

(* Stripe accepted the PaymentIntent, but the response/webhook can disappear. *)
LoseChargeConfirmation(e) ==
    /\ e \in Settlements
    /\ confirmation[e] = "pending"
    /\ confirmation' = [confirmation EXCEPT ![e] = "lost"]
    /\ refillStatus' = [refillStatus EXCEPT
          ![e] = IF lastSubmitSite[e] = "control" THEN "pending" ELSE @]
    /\ refillDue' = [refillDue EXCEPT
          ![e] = IF lastSubmitSite[e] = "control"
                  THEN suppression[e] # "fresh"
                  ELSE @]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, lastSubmitSite, suppression, crashCount
       >>

RecordChargeSuccess(e) ==
    /\ e \in Settlements
    /\ confirmation[e] = "pending"
    /\ refillStatus[e] \in {"pending", "charging"}
    /\ confirmation' = [confirmation EXCEPT ![e] = "confirmed"]
    /\ refillStatus' = [refillStatus EXCEPT ![e] = "charged"]
    /\ refillDue' = [refillDue EXCEPT ![e] = FALSE]
    /\ suppression' = [suppression EXCEPT ![e] = "clear"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, lastSubmitSite, crashCount
       >>

(* Time passing both expires pending suppression and exposes the next minute *)
(* key to the P1-A mutant. The fixed InlineKey deliberately ignores bucket.  *)
ExpireSuppressionWindow(e) ==
    /\ e \in Settlements
    /\ suppression[e] = "fresh"
    /\ ~refillDue[e]
    /\ suppression' = [suppression EXCEPT ![e] = "expired"]
    /\ refillDue' = [refillDue EXCEPT ![e] = TRUE]
    /\ bucket' = [bucket EXCEPT
          ![e] = IF @ < MaxBuckets THEN @ + 1 ELSE @]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, refillStatus, attachmentConfirmed, finalizeFailed,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, crashCount
       >>

RecordPermanentFailure(e) ==
    /\ e \in Settlements
    /\ refillStatus[e] = "charging"
    /\ requestKey[e] = NoKey
    /\ confirmation[e] # "pending"
    /\ suppression[e] # "fresh"
    /\ refillStatus' = [refillStatus EXCEPT ![e] = "failed-recorded"]
    /\ refillDue' = [refillDue EXCEPT ![e] = FALSE]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, crashCount
       >>

RecordExhaustedFailure(e) ==
    /\ e \in Settlements
    /\ refillStatus[e] = "pending"
    /\ submissionAttempts[e] = MaxAttempts
    /\ requestKey[e] = NoKey
    /\ confirmation[e] = "lost"
    /\ refillStatus' = [refillStatus EXCEPT ![e] = "failed-recorded"]
    /\ refillDue' = [refillDue EXCEPT ![e] = FALSE]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, requestKey, requestSite, paymentIntents,
        stripeKey, confirmation, lastSubmitSite, suppression, crashCount
       >>

CrashProcess ==
    /\ crashCount < MaxCrashes
    /\ \E e \in Settlements :
          refillStatus[e] = "charging" \/ requestKey[e] # NoKey
    /\ crashCount' = crashCount + 1
    /\ refillStatus' = [e \in Settlements |->
          IF refillStatus[e] = "charging" THEN "pending" ELSE refillStatus[e]]
    /\ refillDue' = [e \in Settlements |->
          IF refillStatus[e] = "charging" THEN TRUE ELSE refillDue[e]]
    /\ requestKey' = [e \in Settlements |-> NoKey]
    /\ requestSite' = [e \in Settlements |-> "none"]
    /\ UNCHANGED <<
        thresholdCrossed, finalized, deliverySurface, deliveryCount,
        rowOrigin, rowState, attachmentConfirmed, finalizeFailed, bucket,
        submissionAttempts, paymentIntents, stripeKey, confirmation,
        lastSubmitSite, suppression
       >>

Next ==
    \/ \E e \in Settlements : CreateCombinedRow(e)
    \/ \E e \in Settlements : LeaseSettlementRow(e)
    \/ \E e \in Settlements : FinishSettlementRow(e)
    \/ \E e \in Settlements : DeadLetterSettlementRow(e)
    \/ \E e \in Settlements : StartInternalWithNewRow(e)
    \/ \E e \in Settlements : StartInternalWithExistingRow(e)
    \/ \E e \in Settlements : AttachRefill(e)
    \/ \E e \in Settlements : AttachmentFailsAndCallerRefuses(e)
    \/ \E e \in Settlements : InternalFinalizeFailure(e)
    \/ \E e \in Settlements : RollbackRedeliveryToCombined(e)
    \/ \E e \in Settlements, site \in DeliverySurfaces :
          FinalizeSettlement(e, site)
    \/ \E e \in Settlements : ClaimControlDrain(e)
    \/ \E e \in Settlements : DeferControlForFreshSuppression(e)
    \/ \E e \in Settlements : InitiateInlineCharge(e)
    \/ \E e \in Settlements : InitiateDrainCharge(e)
    \/ \E e \in Settlements, i \in IntentIds : StripeCreatePaymentIntent(e, i)
    \/ \E e \in Settlements, i \in IntentIds : StripeReturnExistingPaymentIntent(e, i)
    \/ \E e \in Settlements : LoseChargeConfirmation(e)
    \/ \E e \in Settlements : RecordChargeSuccess(e)
    \/ \E e \in Settlements : ExpireSuppressionWindow(e)
    \/ \E e \in Settlements : RecordPermanentFailure(e)
    \/ \E e \in Settlements : RecordExhaustedFailure(e)
    \/ CrashProcess

(* Weak fairness is the model's assumption that an enabled finite workflow *)
(* step is not ignored forever. All retry/crash/bucket bounds are finite.    *)
Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

(* Stripe assigns at most one distinct PaymentIntent identity to a settlement. *)
NoDoubleCharge ==
    \A e \in Settlements : Cardinality(paymentIntents[e]) <= 1

(* Crossing the threshold may not finalize with refill columns still absent. *)
NoSilentDrop ==
    \A e \in Settlements :
        (thresholdCrossed[e] /\ finalized[e]) =>
            refillStatus[e] \in AttachedRefillStates

ChargeOnlyWhereCredentialed ==
    \A e \in Settlements : ~ENABLED InitiateCharge(e, "internal")

EventuallyCharged ==
    \A e \in Settlements :
        (thresholdCrossed[e] /\ finalized[e]) ~>
            refillStatus[e] \in TerminalRefillStates

=============================================================================
