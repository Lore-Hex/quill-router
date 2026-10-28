package main

import (
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
)

// TestAStartThatStallsExits: the watch exits with 1 once its time is up,
// and never once stopped in time.
func TestAStartThatStallsExits(t *testing.T) {
	exited := make(chan int, 1)
	startWatch(20*time.Millisecond, func(code int) { exited <- code })
	select {
	case code := <-exited:
		if code != 1 {
			t.Fatalf("exit %d", code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a stalled start did not exit")
	}

	stopped := make(chan int, 1)
	stop := startWatch(50*time.Millisecond, func(code int) { stopped <- code })
	stop()
	stop()
	select {
	case code := <-stopped:
		t.Fatalf("a start that reached Run exited %d", code)
	case <-time.After(200 * time.Millisecond):
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
