package storetest

import (
	"context"
	"strings"
	"testing"
)

func TestStartSkipsUnlessOptedIn(t *testing.T) {
	t.Setenv("FASTPATH_SPANNER_EMULATOR", "")
	e, reason, err := Start(context.Background())
	if e != nil || err != nil || !strings.Contains(reason, "FASTPATH_SPANNER_EMULATOR=1") {
		t.Fatalf("not opted in: emulator %v, reason %q, error %v", e, reason, err)
	}
}

// TestStartRefusesWhatIsNotAnEmulatorHere: opted in, the tests fail rather
// than skip or reach anything but an emulator on this machine without
// credentials.
func TestStartRefusesWhatIsNotAnEmulatorHere(t *testing.T) {
	t.Setenv("FASTPATH_SPANNER_EMULATOR", "1")
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
	t.Setenv("GCP_SERVICE_ACCOUNT_KEY_JSON", "")
	refused := func(want string) {
		t.Helper()
		e, _, err := Start(context.Background())
		if e != nil || err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("want a refusal saying %q, got emulator %v, error %v", want, e, err)
		}
	}
	for host, want := range map[string]string{
		"":                           "is not host:port",
		"spanner.googleapis.com:443": "must be on this machine",
		"10.1.2.3:9010":              "must be on this machine",
		"127.0.0.1:1":                "cannot be reached",
	} {
		t.Setenv("SPANNER_EMULATOR_HOST", host)
		refused(want)
	}
	t.Setenv("SPANNER_EMULATOR_HOST", "127.0.0.1:1")
	for _, name := range []string{"GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY_JSON"} {
		t.Setenv(name, "set")
		refused(name + " is set")
		t.Setenv(name, "")
	}
}
