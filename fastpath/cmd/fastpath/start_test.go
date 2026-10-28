package main

import (
	"io"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
)

// TestAStartThatStallsExits: the watch exits with 1 once its time is up,
// and a stop after that says so, after the exit, so Run is never entered;
// a watch stopped in time never exits.
func TestAStartThatStallsExits(t *testing.T) {
	exited := make(chan int, 1)
	stop := startWatch(20*time.Millisecond, func(code int) { exited <- code })
	select {
	case code := <-exited:
		if code != 1 {
			t.Fatalf("exit %d", code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a stalled start did not exit")
	}
	if stop() || stop() {
		t.Fatal("a stop after the watch fired disarmed it")
	}

	stopped := make(chan int, 1)
	stop = startWatch(50*time.Millisecond, func(code int) { stopped <- code })
	if !stop() || !stop() {
		t.Fatal("a stop in time did not disarm the watch")
	}
	select {
	case code := <-stopped:
		t.Fatalf("a start that reached Run exited %d", code)
	case <-time.After(200 * time.Millisecond):
	}
}

// blocked is a stderr that never takes a write.
type blocked struct{}

func (blocked) Write([]byte) (int, error) { select {} }

var _ io.Writer = blocked{}

// TestABlockedLogDoesNotHoldTheExit: the watch exits though its line cannot
// be written, a second after it fired.
func TestABlockedLogDoesNotHoldTheExit(t *testing.T) {
	old := watchLog
	watchLog = blocked{}
	t.Cleanup(func() { watchLog = old })
	exited := make(chan int, 1)
	startWatch(10*time.Millisecond, func(code int) { exited <- code })
	select {
	case <-exited:
	case <-time.After(5 * time.Second):
		t.Fatal("a blocked log held the exit")
	}
}

// TestTheBudgetCoversEveryRead: a start whose secret reads each take nearly
// their whole deadline, however many the flags name, still has
// service.StartStall left for its clients.
func TestTheBudgetCoversEveryRead(t *testing.T) {
	for _, reads := range []int{0, 1, 3, 8} {
		spent := time.Duration(reads) * (secretDeadline - time.Millisecond)
		if left := startBudget(reads) - spent; left < service.StartStall {
			t.Errorf("%d reads leave %v for the clients", reads, left)
		}
	}
}
