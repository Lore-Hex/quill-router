// Package service runs one process of the spike (spike plan §2): an
// admission node's front door and owner, an auditor member, or both, over
// Spanner and Pub/Sub, until its context ends.
//
// The store's configuration is the one source of the times every part must
// agree on: the expiry window, the skew allowance, a hold's longest life and
// the reaper's grace. Run sets the owner's, the front door's and the
// auditor's from it.
package service

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"slices"
	"sync"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/spanner"
	"google.golang.org/api/option"

	"github.com/Lore-Hex/quill-router/fastpath/internal/auditor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/owner"
	"github.com/Lore-Hex/quill-router/fastpath/internal/ring"
	"github.com/Lore-Hex/quill-router/fastpath/internal/settlelog"
	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// Config is a process's.
type Config struct {
	// Admission runs the node's front door and owner, which the design runs
	// together on every admission node (§4.1); Auditor runs an auditor
	// member. The spike runs auditor members as processes of their own, so
	// that they can be stopped alone.
	Admission bool
	Auditor   bool

	// Address is the admission node's host and port, as the ring and the
	// other nodes reach it. Listener, when set, is where it serves, and Run
	// closes it however Run returns; without it, it listens at Address.
	Address  string
	Listener net.Listener
	// Region is the settle log's region: the owner's shard keys carry it,
	// and the ticker ticks its leases.
	Region string
	// Key is the fleet's envelope key.
	Key []byte
	// Shards is every workspace's shard count K (§4.3).
	Shards int64

	// The settle log's topic, the record topic, and the auditor's
	// subscriptions to each.
	SettleTopic, RecordTopic               string
	SettleSubscription, RecordSubscription string
	// MaxOutstanding bounds the messages a subscription holds at once.
	MaxOutstanding int

	// Store's times are the ones every part agrees on.
	Store store.Config
	// Ring is how often a node writes its row and a front door reads the
	// members.
	Ring time.Duration

	// The parts' own settings. Run fills in what joins them: the store, the
	// logs, the ring, the clock, and the times Store holds.
	Owner     owner.Config
	FrontDoor frontdoor.Config
	Runtime   auditor.Config
	Ticker    auditor.TickerConfig
	Pending   auditor.PendingConfig

	// Alert tells a person what the auditor finds that needs one; nil logs
	// it.
	Alert func(subject, what string)
	// Stopping bounds how long the HTTP server waits for its requests when
	// the process stops.
	Stopping time.Duration
}

// Defaults is the spike's configuration, but for what a deployment names:
// the address, the region, the key, the topics and subscriptions.
func Defaults() Config {
	maxLife := 2*time.Hour + 20*time.Minute
	return Config{
		Shards:         1,
		MaxOutstanding: 1000,
		Store: store.Config{LiveFor: 3 * time.Second, Window: 30 * time.Second, Skew: 2 * time.Second,
			PublishDeadline: 5 * time.Second, MaxLife: maxLife, Grace: time.Minute, Allowance: 1 << 50,
			RequiredTier: 1},
		Ring: time.Second,
		Owner: owner.Config{AnswerWait: 5 * time.Second, HeartbeatEvery: 30 * time.Second, RenewEvery: 5 * time.Second,
			KeyStatus: 1, TopUps: owner.TopUps{LowWater: 1000, Cooldown: time.Second, Horizon: time.Minute, Min: 10_000,
				Max: 10_000_000, IdleAfter: 10 * time.Minute, MaxLife: time.Hour},
			// The authorize-to-heartbeat latency, the provider's out of it:
			// a declared stream's first heartbeat is sent at its opening.
			FirstHeartbeat: 10 * time.Second},
		FrontDoor: frontdoor.Config{OwnerWait: time.Second, PublishWait: 5 * time.Second, PeerWait: 800 * time.Millisecond,
			WithdrawWithin: 5 * time.Second, ProbeEvery: 2 * time.Second, RevokeAfter: 15 * time.Second,
			RevokeEvery: time.Second},
		Runtime: auditor.Config{CommitEvery: time.Second, MaxBatch: 100, Retry: time.Second, ForgetAfter: 10 * time.Minute,
			Wait: 5 * time.Second},
		Ticker:   auditor.TickerConfig{Every: time.Second, Limit: 100, Wait: 5 * time.Second},
		Pending:  auditor.PendingConfig{Every: 5 * time.Second, Limit: 100, Wait: 5 * time.Second},
		Stopping: 10 * time.Second,
	}
}

// Clients are the process's connections: to the spike's database, and to
// Pub/Sub in its project.
type Clients struct {
	Spanner *spanner.Client
	PubSub  *pubsub.Client
}

func (c Config) valid() error {
	switch {
	case !c.Admission && !c.Auditor:
		return errors.New("service: a process runs an admission node, an auditor member, or both")
	case c.Admission && (c.Address == "" || len(c.Key) < frontdoor.MinKeySize || c.Shards < 1):
		return errors.New("service: an admission node needs its address, a key of at least 32 bytes and a shard count")
	case c.Region == "" || c.SettleTopic == "" || c.RecordTopic == "":
		return errors.New("service: a process needs its region, the settle log's topic and the record topic")
	case c.Auditor && (c.SettleSubscription == "" || c.RecordSubscription == ""):
		return errors.New("service: an auditor member needs its subscriptions to the settle log and the record topic")
	case c.MaxOutstanding < 1 || c.Ring <= 0 || c.Stopping <= 0:
		return errors.New("service: a positive subscription bound, ring interval and stopping time")
	}
	return nil
}

// agreed is the configuration with the times every part must agree on set
// from the store's: the owner's skew allowance, window, grace and hold life,
// the front door's hold life, and the auditor's skew allowance, grace and
// hold life.
func (c Config) agreed() Config {
	st := c.Store
	c.Owner.Skew, c.Owner.Window, c.Owner.Grace, c.Owner.HoldLife = st.Skew, st.Window, st.Grace, st.MaxLife
	// A revoked lease's holds end within their life and the lease's expiry
	// window.
	c.FrontDoor.HoldLife = st.MaxLife + st.Window
	c.Runtime.Skew, c.Runtime.Grace, c.Runtime.MaxLife = st.Skew, st.Grace, st.MaxLife
	return c
}

// publish is a publish's deadline, the owner's (§4.5): the fence waits for
// it to pass, so a publisher that kept trying longer could land a record
// after the fence tick.
func (c Config) publish() settlelog.Settings {
	return settlelog.Settings{Deadline: c.Store.PublishDeadline}
}

// PubSubOptions are the Pub/Sub client's options for a region: its
// locational endpoint, so that every publisher of a lease's records orders
// them as one (settlelog.Endpoint), unless PUBSUB_EMULATOR_HOST names an
// emulator.
func PubSubOptions(region string) []option.ClientOption {
	if os.Getenv("PUBSUB_EMULATOR_HOST") != "" {
		return nil
	}
	return []option.ClientOption{option.WithEndpoint(settlelog.Endpoint(region))}
}

// Run runs the process until ctx ends, or one of its parts fails, and
// returns once every part has stopped: nil if ctx ended it, else the first
// failure. A Listener given it is its own, closed however it returns.
func Run(ctx context.Context, cfg Config, c Clients) error {
	if cfg.Listener != nil {
		defer cfg.Listener.Close()
	}
	if err := cfg.valid(); err != nil {
		return err
	}
	if c.Spanner == nil || c.PubSub == nil {
		return errors.New("service: a Spanner client and a Pub/Sub client")
	}
	if cfg.Alert == nil {
		cfg.Alert = func(subject, what string) { log.Printf("alert: %s: %s", subject, what) }
	}
	cfg = cfg.agreed()
	s, err := store.New(c.Spanner, cfg.Store)
	if err != nil {
		return err
	}
	settle, err := settlelog.OpenLog(c.PubSub, cfg.SettleTopic, cfg.publish())
	if err != nil {
		return err
	}
	defer settle.Stop()
	records, err := settlelog.OpenRecords(c.PubSub, cfg.RecordTopic, cfg.publish())
	if err != nil {
		return err
	}
	defer records.Stop()

	p := &parts{ctx: ctx}
	p.ctx, p.cancel = context.WithCancel(ctx)
	defer p.cancel()
	if cfg.Admission {
		if err := p.admission(cfg, s, settle, records); err != nil {
			p.startFailed(ctx, err)
		}
	}
	if cfg.Auditor && p.ctx.Err() == nil {
		if err := p.auditor(cfg, c, s, settle, records); err != nil {
			p.startFailed(ctx, err)
		}
	}
	p.wait()
	if p.err != nil {
		return p.err
	}
	return nil
}

// parts are a process's running parts: the first to fail ends them all.
type parts struct {
	ctx    context.Context
	cancel context.CancelFunc
	group  sync.WaitGroup
	stops  []func()

	mu  sync.Mutex
	err error
}

// fail ends the process with err, the first failure kept.
func (p *parts) fail(err error) {
	p.mu.Lock()
	if p.err == nil {
		p.err = err
	}
	p.mu.Unlock()
	p.cancel()
}

// startFailed ends a process whose part could not start: a failure, unless
// the caller's ctx ending is why.
func (p *parts) startFailed(ctx context.Context, err error) {
	if ctx.Err() != nil {
		p.cancel()
		return
	}
	p.fail(err)
}

// run runs f until it returns; an error, unless ctx ended it, fails the
// process.
func (p *parts) run(name string, f func(context.Context) error) {
	p.group.Add(1)
	go func() {
		defer p.group.Done()
		if err := f(p.ctx); err != nil && p.ctx.Err() == nil {
			p.fail(fmt.Errorf("service: %s: %w", name, err))
		}
	}()
}

// failing fails the process with a part's failure, named as run names it,
// for a part that learns of its failure before it returns.
func (p *parts) failing(name string) func(error) {
	return func(err error) { p.fail(fmt.Errorf("service: %s: %w", name, err)) }
}

// stop is run once every part has returned, latest first.
func (p *parts) stop(f func()) { p.stops = append(p.stops, f) }

func (p *parts) wait() {
	<-p.ctx.Done()
	p.group.Wait()
	for i := len(p.stops) - 1; i >= 0; i-- {
		p.stops[i]()
	}
}

// admission starts the node's listener, its ring row, its owner, its front
// door and its HTTP server. The listener comes first: a node that cannot
// listen at its address, as when another serves there, takes no ring row,
// which would stop the node that serves.
func (p *parts) admission(cfg Config, s *store.Store, settle *settlelog.Log,
	records *settlelog.Records) (err error) {
	ln := cfg.Listener
	if ln == nil {
		if ln, err = net.Listen("tcp", cfg.Address); err != nil {
			return err
		}
		// One Run made is closed with the server, or here if a part after
		// it cannot start (Run closes one it was given).
		defer func() {
			if err != nil {
				_ = ln.Close()
			}
		}()
	}
	node, err := ring.Start(p.ctx, s, cfg.Address, []string{ring.OwnerRole, "frontdoor"}, cfg.Ring)
	if err != nil {
		return err
	}
	p.stop(node.Stop)
	members, err := ring.Watch(p.ctx, s, cfg.Ring)
	if err != nil {
		return err
	}
	p.stop(members.Stop)

	oc := cfg.Owner
	oc.Epoch, oc.Spanner, oc.Node, oc.Records = node.Epoch(), s, cfg.Address, owner.FromRecords(records)
	oc.NewAuthorization, oc.Clock = store.NewAuthorizationID, time.Now
	o, err := owner.New(oc, owner.FromLog(settle))
	if err != nil {
		return err
	}
	p.stop(o.Stop)
	p.run("owner", func(ctx context.Context) error {
		o.Run(ctx)
		return nil
	})
	l, err := frontdoor.NewLocal(o, cfg.Address, cfg.Region, cfg.Key)
	if err != nil {
		return err
	}
	local := &localOwner{local: l}
	// The owner stops only once every call to it has returned: a call can
	// run on after its caller's context ends.
	p.stop(local.calls.wait)

	transport := &http.Transport{MaxIdleConnsPerHost: 256, IdleConnTimeout: 90 * time.Second}
	client := &http.Client{Transport: transport}
	// Once every caller has stopped, its idle connections close.
	p.stop(transport.CloseIdleConnections)
	fc := cfg.FrontDoor
	fc.Owners = owners{self: cfg.Address, local: local, remote: frontdoor.HTTPOwners{Client: client, Scheme: "http"}}
	fc.Store, fc.Records, fc.Members, fc.Key = s, frontdoor.FromRecords(records), members, cfg.Key
	fc.Shards = func(string) int64 { return cfg.Shards }
	fc.Self, fc.Peers, fc.Node = cfg.Address, frontdoor.HTTPPeers{Client: client, Scheme: "http"}, node
	fc.Clock = time.Now
	door, err := frontdoor.New(fc)
	if err != nil {
		return err
	}
	p.run("front door", door.Run)

	handlers := &inFlight{}
	requests, endRequests := context.WithCancel(context.Background())
	srv := &http.Server{Handler: handlers.wrap(frontdoor.Handler(door, l)), ReadHeaderTimeout: 5 * time.Second,
		BaseContext: func(net.Listener) context.Context { return requests }}
	p.run("http", func(ctx context.Context) error {
		served := make(chan error, 1)
		go func() { served <- srv.Serve(ln) }()
		var err error
		select {
		case err = <-served:
		case <-ctx.Done():
			// Requests under way get Stopping to end; then their
			// connections close and their contexts end.
			stopping, cancel := context.WithTimeout(context.Background(), cfg.Stopping)
			_ = srv.Shutdown(stopping)
			cancel()
			err = <-served
		}
		// A server that failed, not one shut down, fails the process at
		// once: its handlers are waited for below, and the process's
		// context may end meanwhile.
		if !errors.Is(err, http.ErrServerClosed) {
			p.fail(fmt.Errorf("service: http: %w", err))
		}
		_ = srv.Close()
		endRequests()
		// The parts the handlers call stop only once every handler has
		// returned.
		handlers.wait()
		return nil
	})
	// A node whose row another process took stops: its epoch is not the
	// row's, so its leases' writes are refused.
	p.run("ring", func(ctx context.Context) error {
		select {
		case <-node.Lost():
			return errors.New("the ring row was taken")
		case <-ctx.Done():
			return nil
		}
	})
	return nil
}

// auditor starts an auditor member: the runtime on the settle log's
// subscription, the ticker, the pending work and the record topic's stager.
func (p *parts) auditor(cfg Config, c Clients, s *store.Store, settle *settlelog.Log, records *settlelog.Records) error {
	rc := cfg.Runtime
	rc.Store, rc.Records, rc.Alert, rc.Clock = s, auditor.FromRecords(records), cfg.Alert, time.Now
	rt, err := auditor.New(rc)
	if err != nil {
		return err
	}
	sub := settlelog.Subscribe(c.PubSub, cfg.SettleSubscription, cfg.MaxOutstanding)
	src := reporting{Source: auditor.FromSubscription(sub), fail: p.failing("auditor")}
	p.run("auditor", func(ctx context.Context) error { return rt.Run(ctx, src) })

	tc := cfg.Ticker
	tc.Store, tc.Log, tc.Region, tc.Clock = s, auditor.FromLog(settle), cfg.Region, time.Now
	ticker, err := auditor.NewTicker(tc)
	if err != nil {
		return err
	}
	p.run("ticker", ticker.Run)

	pc := cfg.Pending
	pc.Store, pc.Records, pc.Alert = s, auditor.FromRecords(records), cfg.Alert
	pending, err := auditor.NewPending(pc)
	if err != nil {
		return err
	}
	p.run("pending work", pending.Run)

	stager, err := auditor.NewStager(s, cfg.Alert)
	if err != nil {
		return err
	}
	staged := gatedStaged{src: auditor.FromRecordSubscription(settlelog.SubscribeRecords(c.PubSub,
		cfg.RecordSubscription, cfg.MaxOutstanding)), calls: &inFlight{}, fail: p.failing("stager")}
	p.run("stager", func(ctx context.Context) error {
		err := stager.Run(ctx, staged)
		// The subscription can end before its callbacks have.
		staged.calls.wait()
		return err
	})
	return nil
}

// owners reaches the node's own owner in the process, and the others over
// the network.
type owners struct {
	self   string
	local  *localOwner
	remote frontdoor.HTTPOwners
}

func (o owners) at(address string) frontdoor.Owners {
	if address == o.self {
		return o.local
	}
	return o.remote
}

func (o owners) Authorize(ctx context.Context, address string, req frontdoor.OwnerAuthorize) (frontdoor.OwnerAdmitted,
	error) {
	return o.at(address).Authorize(ctx, address, req)
}

func (o owners) Heartbeat(ctx context.Context, address string, req frontdoor.OwnerHeartbeat) (frontdoor.HeartbeatAnswer,
	error) {
	return o.at(address).Heartbeat(ctx, address, req)
}

func (o owners) Terminal(ctx context.Context, address string, req frontdoor.OwnerTerminal) (
	frontdoor.OwnerTerminalAnswer, error) {
	return o.at(address).Terminal(ctx, address, req)
}

func (o owners) Ping(ctx context.Context, address string) error {
	return o.at(address).Ping(ctx, address)
}

// inFlight counts the requests a server's handler is serving, so that its
// process stops the parts they call only once each has returned. A request
// that comes after wait began is turned away.
type inFlight struct {
	mu      sync.RWMutex
	ending  bool
	serving sync.WaitGroup
}

// enter counts a call in, unless wait has begun; leave counts it out.
func (f *inFlight) enter() bool {
	f.mu.RLock()
	defer f.mu.RUnlock()
	if f.ending {
		return false
	}
	f.serving.Add(1)
	return true
}

func (f *inFlight) leave() { f.serving.Done() }

func (f *inFlight) wrap(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !f.enter() {
			http.Error(w, "stopping", http.StatusServiceUnavailable)
			return
		}
		defer f.leave()
		h.ServeHTTP(w, r)
	})
}

func (f *inFlight) wait() {
	f.mu.Lock()
	f.ending = true
	f.mu.Unlock()
	f.serving.Wait()
}

// localOwner reaches the node's own owner in the process, as
// frontdoor.Direct does: each call's work runs in a goroutine of its own, so
// a call ends with its context, though the owner may finish what it began.
// Unlike Direct's, those goroutines are counted, so the owner is stopped
// only once each has returned.
type localOwner struct {
	local *frontdoor.Local
	calls inFlight
}

// callLocal runs f with the owner and returns its answer, or the context's
// end if that comes first; f starts nothing once the context has ended, nor
// once the process is stopping, when the owner is one no longer reached.
func callLocal[A any](ctx context.Context, lo *localOwner, f func(*frontdoor.Local) A) (A, error) {
	var zero A
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	if !lo.calls.enter() {
		return zero, frontdoor.ErrUnreachable
	}
	answer := make(chan A, 1)
	go func() {
		defer lo.calls.leave()
		if ctx.Err() != nil {
			return
		}
		answer <- f(lo.local)
	}()
	select {
	case a := <-answer:
		return a, nil
	case <-ctx.Done():
		return zero, ctx.Err()
	}
}

func (lo *localOwner) Authorize(ctx context.Context, _ string, req frontdoor.OwnerAuthorize) (frontdoor.OwnerAdmitted,
	error) {
	req.Boot = slices.Clone(req.Boot)
	return callLocal(ctx, lo, func(l *frontdoor.Local) frontdoor.OwnerAdmitted { return l.Authorize(req) })
}

func (lo *localOwner) Heartbeat(ctx context.Context, _ string, req frontdoor.OwnerHeartbeat) (frontdoor.HeartbeatAnswer,
	error) {
	req.Hash, req.Basis = slices.Clone(req.Hash), slices.Clone(req.Basis)
	return callLocal(ctx, lo, func(l *frontdoor.Local) frontdoor.HeartbeatAnswer { return l.Heartbeat(ctx, req) })
}

func (lo *localOwner) Terminal(ctx context.Context, _ string, req frontdoor.OwnerTerminal) (
	frontdoor.OwnerTerminalAnswer, error) {
	req.Digest = slices.Clone(req.Digest)
	return callLocal(ctx, lo, func(l *frontdoor.Local) frontdoor.OwnerTerminalAnswer { return l.Terminal(ctx, req) })
}

func (lo *localOwner) Ping(ctx context.Context, _ string) error {
	_, err := callLocal(ctx, lo, func(*frontdoor.Local) struct{} { return struct{}{} })
	return err
}

// gatedStaged is the record topic's subscription as the stager takes it,
// each callback counted: the subscription can end before its callbacks
// have, and the process stops the store they write only once each has
// returned. A message that comes once stopping has begun is left for
// redelivery. The subscription's failure fails the process as soon as
// Receive returns it, before the stager waits for its callbacks.
type gatedStaged struct {
	src   auditor.StagedSource
	calls *inFlight
	fail  func(error)
}

func (g gatedStaged) Receive(ctx context.Context, handle func(context.Context, auditor.Staged)) error {
	err := g.src.Receive(ctx, func(ctx context.Context, s auditor.Staged) {
		if !g.calls.enter() {
			return
		}
		defer g.calls.leave()
		handle(ctx, s)
	})
	if err != nil && ctx.Err() == nil {
		g.fail(err)
	}
	return err
}

// reporting is a settle log subscription whose failure fails the process as
// soon as its Receive returns it: the runtime waits for its handlers before
// it returns, and the process's context may end meanwhile.
type reporting struct {
	auditor.Source
	fail func(error)
}

func (r reporting) Receive(ctx context.Context, handle func(context.Context, auditor.Delivery)) error {
	err := r.Source.Receive(ctx, handle)
	if err != nil && ctx.Err() == nil {
		r.fail(err)
	}
	return err
}
