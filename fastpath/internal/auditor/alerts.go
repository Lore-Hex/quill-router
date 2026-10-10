package auditor

// The alerts the auditor raises (the production rollout's W7): each a
// message a person reads, with a kind that production's alert policies
// match. A kind, once a policy names it, does not change.
const (
	AlertUnreadableRecord   = "a record the auditor cannot read"
	AlertUnappliableRecord  = "a record the auditor cannot apply"
	AlertNoSuchLease        = "a record of a lease the store does not have"
	AlertGap                = "a gap in the lease's records stopped it"
	AlertAuditFault         = "an audit fault: the owner's checkpoint disagrees with its records"
	AlertFaultUsage         = "a charge past the lease's allocation"
	AlertUnknownRecordKind  = "a record topic message of no kind the stager knows"
	AlertNoLeaseAuth        = "a full record of no lease's authorization"
	AlertFullRecordNoLease  = "a full record of a lease the store does not have"
	AlertUnreadableWork     = "a winner whose pending work cannot be read"
	AlertNoBootBinding      = "a winner whose boot binding no record states"
	AlertDrainOverdue       = "a lease still draining past its holds' longest life and the grace"
	AlertPendingWorkOverdue = "a pack's pending work not done in time"
)

var alertKinds = map[string]string{
	AlertUnreadableRecord:   "unreadable_record",
	AlertUnappliableRecord:  "unappliable_record",
	AlertNoSuchLease:        "record_of_no_lease",
	AlertGap:                "gap",
	AlertAuditFault:         "audit_fault",
	AlertFaultUsage:         "fault_usage",
	AlertUnknownRecordKind:  "unknown_record_kind",
	AlertNoLeaseAuth:        "full_record_of_no_authorization",
	AlertFullRecordNoLease:  "full_record_of_no_lease",
	AlertUnreadableWork:     "unreadable_pending_work",
	AlertNoBootBinding:      "no_boot_binding",
	AlertDrainOverdue:       "drain_overdue",
	AlertPendingWorkOverdue: "pending_work_overdue",
}

// AlertKind is an alert's kind, by its message; "" for a message that is
// none of the auditor's.
func AlertKind(message string) string {
	if kind, ok := alertKinds[message]; ok {
		return kind
	}
	return "other"
}

// AlertKinds are every alert's kind, by its message.
func AlertKinds() map[string]string {
	out := make(map[string]string, len(alertKinds))
	for m, k := range alertKinds {
		out[m] = k
	}
	return out
}
