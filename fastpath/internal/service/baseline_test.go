package service

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/spanner/apiv1/spannerpb"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store/storetest"
)

// calls counts a client's Spanner calls by method and tag.
type calls struct {
	mu sync.Mutex
	n  map[string]int
}

// tagOf is a request's tag: its request tag, else its transaction's.
func tagOf(m any) string {
	type tagged interface {
		GetRequestOptions() *spannerpb.RequestOptions
	}
	if t, ok := m.(tagged); ok && t.GetRequestOptions() != nil {
		if tag := t.GetRequestOptions().GetRequestTag(); tag != "" {
			return tag
		}
		return t.GetRequestOptions().GetTransactionTag()
	}
	return ""
}

func (c *calls) count(method string, m any) {
	name := method[strings.LastIndex(method, "/")+1:]
	if tag := tagOf(m); tag != "" {
		name += " " + tag
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.n[name]++
}

func (c *calls) options() []option.ClientOption {
	unary := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker,
		opts ...grpc.CallOption) error {
		c.count(method, req)
		return invoker(ctx, method, req, reply, cc, opts...)
	}
	stream := func(ctx context.Context, desc *grpc.StreamDesc, cc *grpc.ClientConn, method string,
		streamer grpc.Streamer, opts ...grpc.CallOption) (grpc.ClientStream, error) {
		s, err := streamer(ctx, desc, cc, method, opts...)
		if err != nil {
			return s, err
		}
		return &counted{ClientStream: s, c: c, method: method}, nil
	}
	return []option.ClientOption{option.WithGRPCDialOption(grpc.WithChainUnaryInterceptor(unary)),
		option.WithGRPCDialOption(grpc.WithChainStreamInterceptor(stream))}
}

// counted counts a stream's first request.
type counted struct {
	grpc.ClientStream
	c      *calls
	method string
	once   sync.Once
}

func (s *counted) SendMsg(m any) error {
	if _, ok := m.(proto.Message); ok {
		s.once.Do(func() { s.c.count(s.method, m) })
	}
	return s.ClientStream.SendMsg(m)
}

// since is what was counted, each per second over d.
func (c *calls) since(before map[string]int, d time.Duration) map[string]float64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]float64{}
	for k, n := range c.n {
		if n -= before[k]; n > 0 {
			out[k] = float64(n) / d.Seconds()
		}
	}
	return out
}

func (c *calls) snapshot() map[string]int {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]int, len(c.n))
	for k, n := range c.n {
		out[k] = n
	}
	return out
}

// baseline is P0's baseline load (docs/runbooks/fastpath-watch.md): an idle
// node's Spanner calls a second, with both roles and no workspace enabled,
// at the service's default intervals. Each is a ceiling the measured rate
// must stay under; a call the node makes that is not here fails the test,
// so the table is the node's whole load.
var baseline = map[string]float64{
	// The ring: its row's heartbeat, a statement and its commit, and the
	// members read, each once a second.
	"Commit fastpath-heartbeat":            1.5,
	"ExecuteSql fastpath-heartbeat":        1.5,
	"ExecuteStreamingSql fastpath-members": 1.5,
	// The switch's copy, read once a second.
	"ExecuteStreamingSql fastpath-switch": 1.5,
	// The ticker's scans for leases past their expiry and leases draining,
	// each once a second.
	"ExecuteStreamingSql fastpath-scan-expired":  1.5,
	"ExecuteStreamingSql fastpath-scan-draining": 1.5,
	// The pending work's sweep and its staged records' scan, each every five
	// seconds.
	"ExecuteStreamingSql fastpath-pending-packs": 0.3,
	"ExecuteStreamingSql fastpath-staging":       0.3,
	// The client's sessions, kept up now and then.
	"BatchCreateSessions": 0.5,
	"CreateSession":       0.5,
	"GetSession":          0.5,
}

// observed are the calls an idle node must be seen making, each at least as
// often as this, half its measured rate: so the count is shown to work, a
// part of the node that stopped calling is seen, and a measurement of
// nothing does not pass.
var observed = map[string]float64{
	"Commit fastpath-heartbeat":                  0.5,
	"ExecuteSql fastpath-heartbeat":              0.5,
	"ExecuteStreamingSql fastpath-members":       0.5,
	"ExecuteStreamingSql fastpath-switch":        0.5,
	"ExecuteStreamingSql fastpath-scan-expired":  0.5,
	"ExecuteStreamingSql fastpath-scan-draining": 0.5,
	"ExecuteStreamingSql fastpath-pending-packs": 0.1,
	"ExecuteStreamingSql fastpath-staging":       0.1,
}

// TestAnIdleNodesLoadIsTheBaseline: a node with both roles, its front door
// and owner and its auditor, idle and with no workspace enabled, makes the
// baseline's Spanner calls and no others, each at most as often as it says,
// and each call observed says at least as often.
func TestAnIdleNodesLoadIsTheBaseline(t *testing.T) {
	if emulator == nil {
		t.Skip(skipped)
	}
	ctx := context.Background()
	name := storetest.UniqueID("idle")
	created, err := emulator.Database(ctx, name, nil)
	if err != nil {
		t.Fatal(err)
	}
	created.Close()
	c := &calls{n: map[string]int{}}
	db, err := emulator.Client(ctx, name, c.options()...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	ln := listen(t)
	start(t, config(ln), Clients{Spanner: db, PubSub: pubSub(t)})
	gw := frontdoor.Gateway{Client: &http.Client{Timeout: 10 * time.Second}, Base: "http://" + ln.Addr().String()}
	eventually(t, 20*time.Second, "the node serving", func() (bool, error) {
		_, err := gw.Authorize(ctx, frontdoor.AuthorizeOf{Workspace: "nobody", Request: "r", Estimate: 1,
			Boot: []byte("boot")})
		return err == nil, nil
	})
	// Past its start, a window of its idle running.
	time.Sleep(3 * time.Second)
	before, window := c.snapshot(), 20*time.Second
	time.Sleep(window)
	rates := c.since(before, window)
	keys := make([]string, 0, len(rates))
	for k := range rates {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "\n  %-45s %.2f/s", k, rates[k])
		if limit, ok := baseline[k]; !ok {
			t.Errorf("a call the baseline does not have: %s, %.2f/s", k, rates[k])
		} else if rates[k] > limit {
			t.Errorf("%s at %.2f/s, past the baseline's %.2f/s", k, rates[k], limit)
		}
	}
	t.Logf("an idle node's Spanner calls:%s", b.String())
	for k, least := range observed {
		if rates[k] < least {
			t.Errorf("%s at %.2f/s, under the %.2f/s an idle node makes", k, rates[k], least)
		}
	}
}
