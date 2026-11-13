package routingsnapshot

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestEdgeTicketPolicyVectors(t *testing.T) {
	var file struct {
		Generator string `json:"generator"`
		Cases     []struct {
			Name    string          `json:"name"`
			Body    string          `json:"body"`
			Accept  bool            `json:"accept"`
			Tickets map[string]bool `json:"tickets"`
		} `json:"cases"`
	}
	decoder := json.NewDecoder(bytes.NewReader(testdata(t, "edge_policy_tickets_v1.json")))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) < 40 {
		t.Fatal("missing ticket cases")
	}
	for _, c := range file.Cases {
		t.Run(c.Name, func(t *testing.T) {
			p, err := ParseEdgePolicy([]byte(c.Body))
			if (err == nil) != c.Accept {
				t.Fatalf("accept=%v, want %v: %v", err == nil, c.Accept, err)
			}
			if err != nil {
				return
			}
			for workspace, want := range c.Tickets {
				if got := p.AsyncSettle.Tickets(workspace); got != want {
					t.Errorf("Tickets(%q)=%v, want %v", workspace, got, want)
				}
			}
		})
	}
}

func TestEdgeTickets(t *testing.T) {
	on, off := true, false
	listed := []string{"ws_test_1"}
	for _, tc := range []struct {
		name          string
		policy        AsyncSettle
		first, second bool
	}{
		{"absent", AsyncSettle{AdmissionEnabled: true}, false, false},
		{"false", AsyncSettle{AdmissionEnabled: true, EdgeTickets: &off}, false, false},
		{"all", AsyncSettle{AdmissionEnabled: true, EdgeTickets: &on}, true, true},
		{"list", AsyncSettle{AdmissionEnabled: true, EdgeTickets: &on, EdgeTicketWorkspaces: &listed}, true, false},
		{"excluded", AsyncSettle{AdmissionEnabled: true, EdgeTickets: &on, AdmissionExcludedWorkspaces: &listed}, false, true},
		{"admission_off", AsyncSettle{EdgeTickets: &on}, false, false},
		{"pilot", AsyncSettle{AdmissionEnabled: true, EdgeTickets: &on, AdmissionWorkspaces: &listed}, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.policy.Tickets("ws_test_1") != tc.first || tc.policy.Tickets("ws_test_2") != tc.second {
				t.Fatal("ticket coverage differs")
			}
		})
	}
}
