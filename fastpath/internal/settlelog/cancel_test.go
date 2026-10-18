package settlelog

import (
	"context"
	"testing"
	"time"
)

// The SDK can wait for its local message inventory while stopping, even
// after it has nacked the server's inventory. Release our deliveries on
// cancellation, not after Receive returns. Graceful shutdown forces that
// wait deterministically instead of depending on the streaming-pull race.
func TestCancellationReleasesInventoryBeforeReceiveReturns(t *testing.T) {
	for _, ordered := range []bool{true, false} {
		name := "records"
		if ordered {
			name = "settlements"
		}
		t.Run(name, func(t *testing.T) {
			f := newFakeLog(t, ordered)
			l := f.log(t)
			if err := wait(t, l.Publish("la", []byte("la#1"), nil)); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			delivered := make(chan func(), 1)
			received := make(chan error, 1)
			if ordered {
				s := Subscribe(f.client, f.sub, 1)
				s.sub.ReceiveSettings.ShutdownOptions.Timeout = -1
				go func() {
					received <- s.Receive(ctx, func(_ context.Context, d *Delivery) {
						delivered <- d.Nack
					})
				}()
			} else {
				s := SubscribeRecords(f.client, f.sub, 1)
				s.sub.ReceiveSettings.ShutdownOptions.Timeout = -1
				go func() {
					received <- s.Receive(ctx, func(_ context.Context, d *RecordDelivery) {
						delivered <- d.Nack
					})
				}()
			}
			var release func()
			select {
			case release = <-delivered:
			case <-time.After(5 * time.Second):
				t.Fatal("no delivery")
			}
			cancel()
			select {
			case err := <-received:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(time.Second):
				// Unblock the old implementation so a red regression fails
				// promptly rather than consuming the entire CI timeout.
				release()
				select {
				case <-received:
				case <-time.After(5 * time.Second):
				}
				t.Fatal("Receive waited for a withheld delivery after cancellation")
			}
			if got := f.receive(t, 1, 5*time.Second, nil); !equal(got["la"], []string{"la#1"}) {
				t.Fatalf("the next consumer got %v", got)
			}
		})
	}
}
