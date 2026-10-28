package main

import (
	"testing"
	"time"
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
