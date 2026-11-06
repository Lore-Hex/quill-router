package service

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"runtime/pprof"
	"time"
)

// DebugAddress is where a node serves its goroutine dump unless told
// otherwise: the loopback interface, which only a person on the node
// reaches (docs/runbooks/fastpath-alerts.md, "A hung node").
const DebugAddress = "127.0.0.1:6060"

// ErrNotLoopback is ListenDebug's refusal of an address off the loopback
// interface.
var ErrNotLoopback = errors.New("service: the goroutine dump listens on a loopback IP address alone, such as 127.0.0.1")

// ListenDebug listens at address for the goroutine dump, which must be a
// loopback IP address and a port: the dump shows every goroutine's stack,
// which no other machine has any business reading.
func ListenDebug(address string) (net.Listener, error) {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return nil, fmt.Errorf("service: the goroutine dump's address %q: %w", address, err)
	}
	if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
		return nil, fmt.Errorf("%w: %q", ErrNotLoopback, address)
	}
	return net.Listen("tcp", address)
}

// ServeDebug serves the goroutine dump on ln until stop is called: GET
// /debug/goroutines answers every goroutine's stack, as a SIGQUIT prints
// them, without stopping the process, so a hung part can be seen as it
// hangs. Nothing else is served.
func ServeDebug(ln net.Listener) (stop func()) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /debug/goroutines", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_ = pprof.Lookup("goroutine").WriteTo(w, 2)
	})
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = srv.Serve(ln)
	}()
	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
		<-done
	}
}
