package loadgen

import (
	"context"
	"testing"
	"time"
)

// TestEchoGrantedPlaysTheControlPlanesHeartbeats: with EchoGranted, every
// heartbeat after a stream's first asks the owner for the deadline it last
// granted and echoes none, as the control plane relays the enclave's
// (docs/design/fast-admission-streams.md section 4.6); without it, each
// echoes the deadline the one before was granted.
func TestEchoGrantedPlaysTheControlPlanesHeartbeats(t *testing.T) {
	for _, granted := range []bool{false, true} {
		gw := admitting(t)
		cfg := config(gw)
		cfg.HeartbeatEvery, cfg.EchoGranted = 50*time.Millisecond, granted
		if _, err := Run(context.Background(), cfg); err != nil {
			t.Fatal(err)
		}
		gw.mu.Lock()
		hbs := append(gw.heartbeats[:0:0], gw.heartbeats...)
		gw.mu.Unlock()
		if len(hbs) != 3 {
			t.Fatalf("EchoGranted %v: %d heartbeats", granted, len(hbs))
		}
		if hbs[0].EchoGranted || !hbs[0].Echoed.IsZero() {
			t.Fatalf("EchoGranted %v: the first heartbeat %+v", granted, hbs[0])
		}
		for _, hb := range hbs[1:] {
			if hb.EchoGranted != granted || hb.Echoed.IsZero() == !granted {
				t.Fatalf("EchoGranted %v: a later heartbeat %+v", granted, hb)
			}
		}
	}
}
