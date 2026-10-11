package auditor

import (
	"maps"
	"testing"
)

// TestEveryAlertHasAKindOfItsOwn: the alert messages and their kinds are
// these, stated here apart from the map, so a message or a label changed
// unseen fails; a message that is none of them is of kind "other", so an
// alert policy on a kind matches that alert alone.
func TestEveryAlertHasAKindOfItsOwn(t *testing.T) {
	want := map[string]string{
		"a record the auditor cannot read":                                  "unreadable_record",
		"a record the auditor cannot apply":                                 "unappliable_record",
		"a record of a lease the store does not have":                       "record_of_no_lease",
		"a gap in the lease's records stopped it":                           "gap",
		"an audit fault: the owner's checkpoint disagrees with its records": "audit_fault",
		"a charge past the lease's allocation":                              "fault_usage",
		"a record topic message of no kind the stager knows":                "unknown_record_kind",
		"a full record of no lease's authorization":                         "full_record_of_no_authorization",
		"a full record of a lease the store does not have":                  "full_record_of_no_lease",
		"a winner whose pending work cannot be read":                        "unreadable_pending_work",
		"a winner whose boot binding no record states":                      "no_boot_binding",
		"a lease still draining past its holds' longest life and the grace": "drain_overdue",
		"a pack's pending work not done in time":                            "pending_work_overdue",
	}
	if got := AlertKinds(); !maps.Equal(got, want) {
		t.Fatalf("the kinds are %v, want %v", got, want)
	}
	for message, kind := range map[string]string{AlertUnreadableRecord: "unreadable_record",
		AlertUnappliableRecord: "unappliable_record", AlertNoSuchLease: "record_of_no_lease", AlertGap: "gap",
		AlertAuditFault: "audit_fault", AlertFaultUsage: "fault_usage", AlertUnknownRecordKind: "unknown_record_kind",
		AlertNoLeaseAuth: "full_record_of_no_authorization", AlertFullRecordNoLease: "full_record_of_no_lease",
		AlertUnreadableWork: "unreadable_pending_work", AlertNoBootBinding: "no_boot_binding",
		AlertDrainOverdue: "drain_overdue", AlertPendingWorkOverdue: "pending_work_overdue"} {
		if AlertKind(message) != kind || want[message] != kind {
			t.Errorf("%q: AlertKind says %q, want %q", message, AlertKind(message), kind)
		}
	}
	if got := AlertKind("something else"); got != "other" {
		t.Fatalf("a message of no kind: %q", got)
	}
	kinds := AlertKinds()
	kinds["x"] = "y"
	if AlertKind("x") != "other" {
		t.Fatal("AlertKinds hands out the map itself")
	}
}
