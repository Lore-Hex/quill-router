package service

import (
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
)

// TestTheGoroutineDumpListensOnLoopbackAlone: an address off the loopback
// interface, or a name rather than an IP address, is refused before
// anything listens; the default is on the loopback interface.
func TestTheGoroutineDumpListensOnLoopbackAlone(t *testing.T) {
	for _, address := range []string{"0.0.0.0:0", ":0", "10.0.0.7:0", "[::]:0", "localhost:0", "example.com:6060"} {
		ln, err := ListenDebug(address)
		if err == nil {
			_ = ln.Close()
			t.Fatalf("%s: listened at %v", address, ln.Addr())
		}
		if !errors.Is(err, ErrNotLoopback) {
			t.Fatalf("%s: %v", address, err)
		}
	}
	if _, err := ListenDebug("127.0.0.1"); err == nil || errors.Is(err, ErrNotLoopback) {
		t.Fatalf("an address with no port: %v", err)
	}
	host, _, err := net.SplitHostPort(DebugAddress)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		t.Fatalf("the default address %q", DebugAddress)
	}
}

// TestTheGoroutineDumpShowsEveryGoroutine: GET /debug/goroutines answers
// every goroutine's full stack, this test's own among them, and nothing
// else is served.
func TestTheGoroutineDumpShowsEveryGoroutine(t *testing.T) {
	ln, err := ListenDebug("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	stop := ServeDebug(ln)
	defer stop()
	base := "http://" + ln.Addr().String()
	resp, err := http.Get(base + "/debug/goroutines")
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("%d: %v", resp.StatusCode, err)
	}
	dump := string(body)
	if !strings.HasPrefix(dump, "goroutine ") || !strings.Contains(dump, "TestTheGoroutineDumpShowsEveryGoroutine") {
		t.Fatalf("the dump: %.500s", dump)
	}
	for path, want := range map[string]int{"/debug/pprof/": http.StatusNotFound, "/": http.StatusNotFound} {
		resp, err := http.Get(base + path)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != want {
			t.Fatalf("%s: %d", path, resp.StatusCode)
		}
	}
	resp, err = http.Post(base+"/debug/goroutines", "text/plain", nil)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("a POST: %d", resp.StatusCode)
	}
}
