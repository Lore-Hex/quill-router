----------------------------- MODULE SurfaceCutover -----------------------------
(***************************************************************************)
(* The routed Cloud Run rollout shared by public_surface.sh and             *)
(* internal_surface.sh. A region is handled at a time: capture its serving  *)
(* revision, durably arm ingress recovery, widen ingress, create a          *)
(* no-traffic candidate, tag and smoke it, durably arm promotion recovery,  *)
(* promote it, restrict ingress, and clear the marker.                      *)
(*                                                                         *)
(* The shell scripts which prompted this model restored only the region in  *)
(* flight when a later region failed. That is not a fleet rollback: an      *)
(* earlier region could keep serving New while the failing region returned  *)
(* to Old. The repaired protocol represented here keeps every captured      *)
(* previous revision durable for the whole attempt and cannot terminate a   *)
(* failed attempt until every region has been restored. Deleting the        *)
(* AllOld guard from TerminateFailure recreates the reviewed defect.        *)
(*                                                                         *)
(* ProviderFailure and SmokeFailure may interrupt any running phase. Crash  *)
(* is a separate action enabled at every nonterminal state (including       *)
(* recovery), up to the explicit MaxCrashes bound. Recovery operations are  *)
(* atomic abstractions of the corresponding provider calls. Weak fairness   *)
(* says an enabled finite workflow is not ignored forever; it does not say  *)
(* that a permanently unavailable provider recovers.                       *)
(*                                                                         *)
(* MUTATION CHECKS (TLC -workers 1, restored after every run)               *)
(*                                                                         *)
(* NoSplitFleet: weaken AllOld in TerminateFailure to current-region Old.    *)
(*   Violated in 14 states:                                                 *)
(*   Record -> ArmIngress -> Widen -> Deploy -> Tag -> PassSmoke ->         *)
(*   ArmPromotion -> Promote -> Restrict -> Clear -> Advance ->             *)
(*   ProviderFailure -> TerminateFailure, leaving <<New, Old>>.             *)
(* AlwaysRecoverable: delete serving[r] = Old from ForgetUnusedRecovery.   *)
(*   Violated in 11 states: the first region promotes, ProviderFailure      *)
(*   begins recovery, then ForgetUnusedRecovery erases its rollback record. *)
(* NoTrafficWithoutSmoke: delete smoked[current] from ArmPromotion.         *)
(*   Violated in 8 states: Tag -> ArmPromotion -> Promote skips PassSmoke.  *)
(* IngressNeverStrandedOpen: delete ~ingressOpen[current] from              *)
(*   ClearPromotionMarker. Violated in 22 states: both regions clear and    *)
(*   advance without RestrictIngress, then FinishSuccess strands both open. *)
(*                                                                         *)
(* Every quoted trace is the shortest single-worker breadth-first trace.    *)
(* The restored model passes at the counts recorded in SurfaceCutover.cfg.  *)
(***************************************************************************)

EXTENDS Naturals

CONSTANTS NumRegions, MaxCrashes

ASSUME /\ NumRegions \in Nat \ {0}
       /\ MaxCrashes \in Nat

Regions == 1..NumRegions
Versions == {"Old", "New"}
NoRevision == "none"
MarkerPhases == {"none", "ingress-armed", "promotion-armed"}
RunningPhases == {
    "record", "arm-ingress", "widen", "deploy", "tag",
    "smoke", "promote", "post-promotion", "advance",
    "finish"
}
FailureOutcomes == {"provider-failed", "smoke-failed", "crashed"}
TerminalOutcomes == {"succeeded", "failed"}

VARIABLES
    phase,
    current,
    serving,
    previous,
    ingressOpen,
    deployed,
    tagged,
    smoked,
    markerRegion,
    markerPhase,
    outcome,
    crashCount

vars == <<
    phase, current, serving, previous, ingressOpen, deployed, tagged, smoked,
    markerRegion, markerPhase, outcome, crashCount
>>

AllOld == \A r \in Regions : serving[r] = "Old"
AllNew == \A r \in Regions : serving[r] = "New"
AllIngressRestricted == \A r \in Regions : ~ingressOpen[r]
SameVersion == \E version \in Versions : \A r \in Regions : serving[r] = version

Init ==
    /\ phase = "record"
    /\ current = 1
    /\ serving = [r \in Regions |-> "Old"]
    /\ previous = [r \in Regions |-> NoRevision]
    /\ ingressOpen = [r \in Regions |-> FALSE]
    /\ deployed = [r \in Regions |-> FALSE]
    /\ tagged = [r \in Regions |-> FALSE]
    /\ smoked = [r \in Regions |-> FALSE]
    /\ markerRegion = 0
    /\ markerPhase = "none"
    /\ outcome = "running"
    /\ crashCount = 0

RecordServingRevision ==
    /\ outcome = "running"
    /\ phase = "record"
    /\ current \in Regions
    /\ previous' = [previous EXCEPT ![current] = serving[current]]
    /\ phase' = "arm-ingress"
    /\ UNCHANGED <<
        current, serving, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

ArmIngressRecovery ==
    /\ outcome = "running"
    /\ phase = "arm-ingress"
    /\ previous[current] = serving[current]
    /\ markerRegion' = current
    /\ markerPhase' = "ingress-armed"
    /\ phase' = "widen"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        outcome, crashCount
       >>

WidenIngress ==
    /\ outcome = "running"
    /\ phase = "widen"
    /\ markerRegion = current
    /\ markerPhase = "ingress-armed"
    /\ ingressOpen' = [ingressOpen EXCEPT ![current] = TRUE]
    /\ phase' = "deploy"
    /\ UNCHANGED <<
        current, serving, previous, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

DeployWithoutTraffic ==
    /\ outcome = "running"
    /\ phase = "deploy"
    /\ ingressOpen[current]
    /\ deployed' = [deployed EXCEPT ![current] = TRUE]
    /\ phase' = "tag"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

TagCandidate ==
    /\ outcome = "running"
    /\ phase = "tag"
    /\ deployed[current]
    /\ tagged' = [tagged EXCEPT ![current] = TRUE]
    /\ phase' = "smoke"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

PassSmoke ==
    /\ outcome = "running"
    /\ phase = "smoke"
    /\ tagged[current]
    /\ ~smoked[current]
    /\ smoked' = [smoked EXCEPT ![current] = TRUE]
    /\ UNCHANGED <<
        phase, current, serving, previous, ingressOpen, deployed, tagged,
        markerRegion, markerPhase, outcome, crashCount
       >>

ArmPromotion ==
    /\ outcome = "running"
    /\ phase = "smoke"
    /\ smoked[current]
    /\ previous[current] = "Old"
    /\ markerRegion = current
    /\ markerPhase = "ingress-armed"
    /\ markerPhase' = "promotion-armed"
    /\ phase' = "promote"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, outcome, crashCount
       >>

Promote ==
    /\ outcome = "running"
    /\ phase = "promote"
    /\ markerRegion = current
    /\ markerPhase = "promotion-armed"
    /\ serving' = [serving EXCEPT ![current] = "New"]
    /\ phase' = "post-promotion"
    /\ UNCHANGED <<
        current, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

RestrictIngress ==
    /\ outcome = "running"
    /\ phase = "post-promotion"
    /\ ingressOpen[current]
    /\ ingressOpen' = [ingressOpen EXCEPT ![current] = FALSE]
    /\ UNCHANGED <<
        phase, current, serving, previous, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

ClearPromotionMarker ==
    /\ outcome = "running"
    /\ phase = "post-promotion"
    /\ ~ingressOpen[current]
    /\ markerRegion = current
    /\ markerPhase = "promotion-armed"
    /\ markerRegion' = 0
    /\ markerPhase' = "none"
    /\ phase' = "advance"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        outcome, crashCount
       >>

AdvanceRegion ==
    /\ outcome = "running"
    /\ phase = "advance"
    /\ IF current = NumRegions
          THEN /\ current' = NumRegions + 1
               /\ phase' = "finish"
          ELSE /\ current' = current + 1
               /\ phase' = "record"
    /\ UNCHANGED <<
        serving, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

FinishSuccess ==
    /\ outcome = "running"
    /\ phase = "finish"
    /\ AllNew
    /\ markerPhase = "none"
    /\ outcome' = "succeeded"
    /\ phase' = "terminal"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, crashCount
       >>

BeginFailure(kind) ==
    /\ kind \in {"provider-failed", "smoke-failed"}
    /\ outcome = "running"
    /\ phase \in RunningPhases
    /\ outcome' = kind
    /\ phase' = "recover"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, crashCount
       >>

Crash ==
    /\ outcome \notin TerminalOutcomes
    /\ crashCount < MaxCrashes
    /\ crashCount' = crashCount + 1
    /\ outcome' = "crashed"
    /\ phase' = "recover"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase
       >>

RestoreTraffic(r) ==
    /\ outcome \in FailureOutcomes
    /\ phase = "recover"
    /\ r \in Regions
    /\ serving[r] = "New"
    /\ previous[r] = "Old"
    /\ serving' = [serving EXCEPT ![r] = "Old"]
    /\ UNCHANGED <<
        phase, current, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

RestoreIngress(r) ==
    /\ outcome \in FailureOutcomes
    /\ phase = "recover"
    /\ r \in Regions
    /\ ingressOpen[r]
    /\ ingressOpen' = [ingressOpen EXCEPT ![r] = FALSE]
    /\ UNCHANGED <<
        phase, current, serving, previous, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

ClearRecoveryMarker ==
    /\ outcome \in FailureOutcomes
    /\ phase = "recover"
    /\ markerPhase # "none"
    /\ markerRegion' = 0
    /\ markerPhase' = "none"
    /\ UNCHANGED <<
        phase, current, serving, previous, ingressOpen, deployed, tagged, smoked,
        outcome, crashCount
       >>

(* A previous revision may be discarded only while that region still serves *)
(* Old. Removing this guard is the AlwaysRecoverable mutation experiment.   *)
ForgetUnusedRecovery(r) ==
    /\ outcome \notin TerminalOutcomes
    /\ phase = "recover"
    /\ r \in Regions
    /\ previous[r] # NoRevision
    /\ serving[r] = "Old"
    /\ previous' = [previous EXCEPT ![r] = NoRevision]
    /\ UNCHANGED <<
        phase, current, serving, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, outcome, crashCount
       >>

TerminateFailure ==
    /\ outcome \in FailureOutcomes
    /\ phase = "recover"
    /\ AllOld
    /\ AllIngressRestricted
    /\ markerPhase = "none"
    /\ outcome' = "failed"
    /\ phase' = "terminal"
    /\ UNCHANGED <<
        current, serving, previous, ingressOpen, deployed, tagged, smoked,
        markerRegion, markerPhase, crashCount
       >>

Next ==
    \/ RecordServingRevision
    \/ ArmIngressRecovery
    \/ WidenIngress
    \/ DeployWithoutTraffic
    \/ TagCandidate
    \/ PassSmoke
    \/ ArmPromotion
    \/ Promote
    \/ RestrictIngress
    \/ ClearPromotionMarker
    \/ AdvanceRegion
    \/ FinishSuccess
    \/ \E kind \in {"provider-failed", "smoke-failed"} : BeginFailure(kind)
    \/ Crash
    \/ \E r \in Regions : RestoreTraffic(r)
    \/ \E r \in Regions : RestoreIngress(r)
    \/ ClearRecoveryMarker
    \/ \E r \in Regions : ForgetUnusedRecovery(r)
    \/ TerminateFailure

Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

NoSplitFleet ==
    outcome \in TerminalOutcomes => SameVersion

AlwaysRecoverable ==
    \A r \in Regions : serving[r] = "Old" \/ previous[r] = "Old"

NoTrafficWithoutSmoke ==
    \A r \in Regions : serving[r] = "New" => smoked[r]

IngressNeverStrandedOpen ==
    outcome \in TerminalOutcomes => AllIngressRestricted

EventuallyConsistent ==
    <> (outcome \in TerminalOutcomes /\ SameVersion)

=============================================================================
