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
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"cloud.google.com/go/spanner"
	"google.golang.org/api/option"

	"github.com/Lore-Hex/quill-router/fastpath/internal/auditor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/edge"
	"github.com/Lore-Hex/quill-router/fastpath/internal/frontdoor"
	"github.com/Lore-Hex/quill-router/fastpath/internal/gwstate"
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
	// Key is the fleet's envelope key, which the node seals with; AcceptKeys
	// are the keys it also verifies with, a rotation's other key
	// (docs/design/fast-admission-production-rollout.md, W6).
	Key        []byte
	AcceptKeys [][]byte
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
	// Switch is how often a node reads the fast path's switch, which
	// workspaces it may admit for (switch.go).
	Switch time.Duration
	// Ring is how often a node writes its row and a front door reads the
	// members.
	Ring time.Duration

	// Edge is the edge's (docs/design/fast-admission-enclave-direct.md,
	// E1): Address empty runs none.
	Edge EdgeConfig

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
	// HandOff is how long a stopping admission node's owner has to hand its
	// leases off (§4.2, spike plan K2): it lists each lease's open holds in
	// hand-off records and marks the lease draining, and a lease it has not
	// handed off by then drains by time. Zero hands nothing off.
	HandOff time.Duration
	// Leave, once it is closed or sent on, marks the admission node leaving
	// (spike plan §4, K3): front doors send it no new requests, and its
	// owner retires, taking nothing new and serving the holds of the
	// leases it has, each of which ends once its holds have. A stopping
	// node leaves too.
	Leave <-chan struct{}
	// ClockOffset is added to the owner's clock readings: the fault
	// injector's offset of an owner's clock (spike plan K6), zero for a true
	// one. The front door reads its clock only for spans of time, which an
	// offset leaves as they were, and an auditor's parts keep a true clock.
	ClockOffset time.Duration

	// edgeMade, when set, is given the edge once it is made: the tests'
	// view of it.
	edgeMade func(*edge.Edge)
	// edgeClock, when set, is the edge's clock (time.Now otherwise): the
	// tests' clock behind the emulator's.
	edgeClock func() time.Time
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
		Ring:   time.Second,
		Switch: time.Second,
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
			Wait: 5 * time.Second, DrainOverdue: 10 * time.Minute},
		Ticker:   auditor.TickerConfig{Every: time.Second, Limit: 100, Wait: 5 * time.Second},
		Pending:  auditor.PendingConfig{Every: 5 * time.Second, Limit: 100, Wait: 5 * time.Second, Overdue: 10 * time.Minute},
		Stopping: 10 * time.Second,
		HandOff:  10 * time.Second,
	}
}

// Clients are the process's connections: to the spike's database, and to
// Pub/Sub in its project.
type Clients struct {
	Spanner *spanner.Client
	PubSub  *pubsub.Client
}

// EdgeConfig is the edge's: where it listens for the enclave's
// control-plane calls (Listener, when set, else Address), Python's
// internal surface it passes them to, the internal token the enclave
// sends, as Python holds it, and how long a boot read may take before the
// verdict is unread (edge.Config).
type EdgeConfig struct {
	Address  string
	Listener net.Listener
	Upstream string
	Token    string
	BootWait time.Duration
	// The routing snapshot the edge polls (edge.SnapshotConfig, E2): its URL
	// on the upstream, empty for none; the manifest keys' Ed25519 public keys
	// in unpadded base64url and their kids, each a comma-separated list in
	// the same order (two during a rotation); and the plane it is signed
	// for, as cloud/region. Shadow runs the comparator against it.
	SnapshotURL, SnapshotPublicKey, SnapshotKid, SnapshotPlane string
	Shadow                                                     bool
	// PolicyURL is the edge policy the edge polls (edge.PolicyConfig, E2
	// G8) on the upstream, empty for none. It is signed with the routing
	// snapshot's key for the same plane, so it takes SnapshotPublicKey,
	// SnapshotKid and SnapshotPlane.
	PolicyURL string
	// DecideShadow runs the full-response shadow (edge.DecideShadowConfig,
	// E2 G10): the decide pipeline over each authorize the edge passes on,
	// compared with Python's answer and rows, logging counts alone. It needs
	// SnapshotURL and PolicyURL. Off (the default), the key and workspace
	// cache and the decide copy are never made.
	DecideShadow bool
	// Decide runs the decide path (edge.DecideConfig, E2 G11): the edge
	// admits the authorizes it decides through the node's front door,
	// inserts their rows and answers them. It needs SnapshotURL and
	// PolicyURL. Off (the default), the edge answers nothing.
	Decide bool
}

func (c Config) valid() error {
	switch {
	case c.Edge.Address != "" && (c.Edge.Upstream == "" || c.Edge.Token == ""):
		return errors.New("service: an edge needs its upstream and the internal token")
	case c.Edge.SnapshotURL != "" && c.Edge.Address == "":
		return errors.New("service: the routing snapshot is the edge's, which needs its address")
	case c.Edge.Shadow && c.Edge.SnapshotURL == "":
		return errors.New("service: the edge's shadow compares against a routing snapshot, which needs its URL")
	case c.Edge.SnapshotURL != "" && (c.Edge.SnapshotPublicKey == "" || c.Edge.SnapshotKid == "" ||
		c.Edge.SnapshotPlane == ""):
		return errors.New("service: the routing snapshot needs its public key, kid and plane")
	case c.Edge.PolicyURL != "" && c.Edge.Address == "":
		return errors.New("service: the edge policy is the edge's, which needs its address")
	case c.Edge.PolicyURL != "" && (c.Edge.SnapshotPublicKey == "" || c.Edge.SnapshotKid == "" ||
		c.Edge.SnapshotPlane == ""):
		return errors.New("service: the edge policy needs the routing snapshot's public key, kid and plane")
	case (c.Edge.DecideShadow || c.Edge.Decide) && (c.Edge.SnapshotURL == "" || c.Edge.PolicyURL == ""):
		return errors.New("service: the edge's decide shadow and decide path decide from the routing snapshot and " +
			"the edge policy, which need their URLs")
	case c.Edge.Address != "" && !c.Admission:
		// The edge leaves with the admission node, whose Leave it follows; a
		// process without one would answer the balancer serving for ever.
		return errors.New("service: an edge runs beside an admission node")
	case !c.Admission && !c.Auditor:
		return errors.New("service: a process runs an admission node, an auditor member, or both")
	case c.Address == "":
		return errors.New("service: a process needs its address, an admission node's host and port as other nodes " +
			"reach it, or the name of an auditor member's own row")
	case c.Admission && (len(c.Key) < frontdoor.MinKeySize || c.Shards < 1):
		return errors.New("service: an admission node needs a key of at least 32 bytes and a shard count")
	case c.Region == "" || c.SettleTopic == "" || c.RecordTopic == "":
		return errors.New("service: a process needs its region, the settle log's topic and the record topic")
	case c.Auditor && (c.SettleSubscription == "" || c.RecordSubscription == ""):
		return errors.New("service: an auditor member needs its subscriptions to the settle log and the record topic")
	case c.MaxOutstanding < 1 || c.Ring <= 0 || c.Switch <= 0 || c.Stopping <= 0 || c.HandOff < 0:
		return errors.New("service: a positive subscription bound, ring interval and stopping time, and a hand-off time")
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
// returns once every part has stopped: the first failure, though ctx ended
// too, else nil. When ctx ends, the parts run on while the process leaves:
// an admission node is marked leaving and its owner hands its leases off,
// within HandOff; then they stop. A part that fails stops them all at once,
// a leaving under way too (lifecycle). A Listener given it is its own,
// closed however it returns.
func Run(ctx context.Context, cfg Config, c Clients) error {
	if cfg.Edge.Listener != nil {
		defer cfg.Edge.Listener.Close()
	}
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
		cfg.Alert = func(subject, what string) { writeAlert(alertOutput, subject, what) }
	}
	cfg = cfg.agreed()
	s, err := store.New(c.Spanner, cfg.Store)
	if err != nil {
		return err
	}
	// The client's session before anything else uses it: no session take
	// of the process's ever waits, so none is served on an ended context
	// (which ends the client's session worker for good) and none blocks the
	// client's close (store.WarmSession). Bounded by the client's own
	// session creations, past Run's context: a node that cannot get a
	// session does not start.
	if err := s.WarmSession(); err != nil {
		return fmt.Errorf("service: the Spanner client's session: %w", err)
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

	if cfg.ClockOffset != 0 {
		log.Printf("fastpath: the owner's clock readings offset by %v", cfg.ClockOffset)
	}
	return lifecycle(ctx, func(p *parts) {
		// The edge first: one that cannot listen or has no upstream fails
		// the process before the node takes its ring row.
		if cfg.Edge.Address != "" {
			if err := p.edge(cfg, s); err != nil {
				p.startFailed(ctx, err)
			}
		}
		if cfg.Admission && p.ctx.Err() == nil {
			if err := p.admission(cfg, s, settle, records); err != nil {
				p.startFailed(ctx, err)
			}
		}
		if cfg.Auditor && p.ctx.Err() == nil {
			if err := p.auditor(cfg, c, s, settle, records); err != nil {
				p.startFailed(ctx, err)
			}
		}
	})
}

// edge starts the edge's server: the enclave's control-plane calls, checked
// and passed to Python with the edge's verdict (edge.Edge). It reads boots
// from the store and nothing else of the node's; it stops as the node's
// own server stops, requests under way given Stopping to end.
func (p *parts) edge(cfg Config, s *store.Store) (err error) {
	ln := cfg.Edge.Listener
	if ln == nil {
		if ln, err = net.Listen("tcp", cfg.Edge.Address); err != nil {
			return err
		}
	}
	upstream, err := url.Parse(cfg.Edge.Upstream)
	if err != nil || (upstream.Scheme != "http" && upstream.Scheme != "https") {
		_ = ln.Close()
		return fmt.Errorf("service: the edge's upstream %q is not an http or https URL", cfg.Edge.Upstream)
	}
	// The transport is the process's: its idle connections close once the
	// edge's handlers have returned, as the node's own do.
	transport := &http.Transport{
		DialContext:         (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		TLSHandshakeTimeout: 10 * time.Second,
		DisableCompression:  true,
		MaxIdleConnsPerHost: 256,
		IdleConnTimeout:     90 * time.Second,
	}
	snapshot, err := snapshotConfig(cfg.Edge)
	if err != nil {
		_ = ln.Close()
		return err
	}
	policy, err := policyConfig(cfg.Edge)
	if err != nil {
		_ = ln.Close()
		return err
	}
	// The key and workspace cache and its reads exist only with the decide
	// shadow or the decide path on; the two share it.
	var decideShadow *edge.DecideShadowConfig
	var decide *edge.DecideConfig
	var cache *gwstate.Cache
	if cfg.Edge.DecideShadow || cfg.Edge.Decide {
		if cache, err = gwstate.NewCache(gwstate.CacheConfig{Reader: s}); err != nil {
			_ = ln.Close()
			return err
		}
	}
	if cfg.Edge.DecideShadow {
		decideShadow = &edge.DecideShadowConfig{Cache: cache, Decides: s, Rows: s,
			// edge.AlertWouldAdmit's alert line, as the auditor's are
			// written (docs/runbooks/fastpath-alerts.md); its policy is
			// I1's.
			Alert: func(kind, subject, what string) { writeAlertOf(alertOutput, subject, what, kind) }}
	}
	if cfg.Edge.Decide {
		decide = &edge.DecideConfig{Cache: cache, Decides: s, Rows: s}
	}
	e, err := edge.New(edge.Config{Token: cfg.Edge.Token, Upstream: upstream, Boots: storeBoots{s}, BootWait: cfg.Edge.BootWait,
		Clock: cfg.edgeClock, Transport: transport, Serving: p.serving, Snapshot: snapshot, Policy: policy,
		Admitter:     &p.admitter,
		DecideShadow: decideShadow, Decide: decide})
	if err != nil {
		_ = ln.Close()
		return err
	}
	// The cache is closed (its reads ended, before the store's client
	// closes) only after the last Lookup: once the edge's handlers have
	// returned, the decide path's refunds and inserts have ended, and the
	// decide shadow has compared what it followed.
	followed := make(chan struct{})
	if cache != nil {
		p.run("edge decide cache", func(ctx context.Context) error {
			cache.Run(ctx)
			<-followed
			cache.Close()
			return nil
		})
	}
	if cfg.edgeMade != nil {
		cfg.edgeMade(e)
	}
	// The snapshot's and the policy's polls and the shadow comparator,
	// beside the server and never in its way; with none configured this
	// returns at once.
	p.run("edge snapshot", func(ctx context.Context) error {
		e.Run(ctx)
		return nil
	})
	p.whenLeaving(func() { p.retiring.Store(true) })
	handlers := &inFlight{}
	requests, endRequests := context.WithCancel(context.Background())
	srv := &http.Server{Handler: handlers.wrap(e), ReadHeaderTimeout: 5 * time.Second,
		BaseContext: func(net.Listener) context.Context { return requests }}
	p.run("edge", func(ctx context.Context) error {
		served := make(chan error, 1)
		go func() { served <- srv.Serve(ln) }()
		var err error
		select {
		case err = <-served:
		case <-ctx.Done():
			stopping, cancel := context.WithTimeout(context.Background(), cfg.Stopping)
			_ = srv.Shutdown(stopping)
			cancel()
			err = <-served
		}
		// A server that failed, not one shut down, fails the process at
		// once, whatever the context did meanwhile; either way it is
		// closed, its connections with it, and its handlers waited for.
		if !errors.Is(err, http.ErrServerClosed) {
			p.fail(fmt.Errorf("service: edge: %w", err))
		}
		_ = srv.Close()
		endRequests()
		handlers.wait()
		// The decide path's refunds run beside its passes, and its inserts'
		// transactions can outlive the inserts: Run returns, and the client
		// closes, only once both have ended, since closing the client under
		// a transaction still waiting for its session panics. The refunds
		// are best effort here: the owner and the front door are parts of
		// their own and may stop first, and a refund they no longer serve
		// fails within its 1 s, its hold ending with the lease as any refund
		// not sent does (decision 12). The drain is short: with the session
		// warmed no take waits, an insert's transaction ends within its
		// 1.5 s, a refund within 1 s, all well inside the node's 30 s stop
		// (TimeoutStopSec) after the hand-off and the server's Stopping.
		e.WaitDecide()
		s.WaitLeasedInserts()
		// What the decide shadow follows ends with its requests; its reads
		// end with them, and then the cache may close.
		e.WaitDecideShadow()
		close(followed)
		transport.CloseIdleConnections()
		return nil
	})
	return nil
}

// snapshotConfig is the edge's routing snapshot from the node's flags; the
// zero config, polling nothing, when no URL is given.
func snapshotConfig(c EdgeConfig) (edge.SnapshotConfig, error) {
	if c.SnapshotURL == "" {
		return edge.SnapshotConfig{}, nil
	}
	u, keys, err := pinned("routing snapshot", c.SnapshotURL, c)
	if err != nil {
		return edge.SnapshotConfig{}, err
	}
	return edge.SnapshotConfig{URL: u, Keys: keys, Plane: c.SnapshotPlane, Shadow: c.Shadow}, nil
}

// policyConfig is the edge policy from the node's flags: its URL, and the
// routing snapshot's keys and plane; the zero config, polling nothing, when
// no URL is given.
func policyConfig(c EdgeConfig) (edge.PolicyConfig, error) {
	if c.PolicyURL == "" {
		return edge.PolicyConfig{}, nil
	}
	u, keys, err := pinned("edge policy", c.PolicyURL, c)
	if err != nil {
		return edge.PolicyConfig{}, err
	}
	return edge.PolicyConfig{URL: u, Keys: keys, Plane: c.SnapshotPlane}, nil
}

// pinned parses a document's URL and the routing snapshot key's public
// keys and kids, each a comma-separated list in the same order.
func pinned(what, rawURL string, c EdgeConfig) (*url.URL, []edge.SnapshotKey, error) {
	u, err := url.Parse(rawURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, nil, fmt.Errorf("service: the %s's URL %q is not an http or https URL", what, rawURL)
	}
	publics, kids := strings.Split(c.SnapshotPublicKey, ","), strings.Split(c.SnapshotKid, ",")
	if len(publics) != len(kids) {
		return nil, nil, errors.New("service: the routing snapshot's public keys and kids pair up")
	}
	var keys []edge.SnapshotKey
	for i, public := range publics {
		key, err := base64.RawURLEncoding.Strict().DecodeString(public)
		if err != nil || len(key) != ed25519.PublicKeySize {
			return nil, nil, errors.New("service: a routing snapshot public key is not 32 bytes of " +
				"unpadded base64url")
		}
		keys = append(keys, edge.SnapshotKey{Kid: kids[i], PublicKey: key})
	}
	return u, keys, nil
}

// storeBoots reads the edge's boots from the store: production's entity
// table, by key id.
type storeBoots struct{ s *store.Store }

func (b storeBoots) Boot(ctx context.Context, kid string) (edge.Boot, bool, error) {
	boot, found, err := b.s.ReadBoot(ctx, kid)
	if err != nil || !found {
		return edge.Boot{}, false, err
	}
	return edge.Boot{Kid: boot.Kid, PublicKey: boot.PublicKey, Verified: boot.Verified, ImageDigest: boot.ImageDigest,
		DeclaresUsageHeartbeat: boot.DeclaresUsageHeartbeat}, true, nil
}

// lifecycle starts a process's parts with start and runs them until ctx
// ends or one fails, and returns once every part has stopped: the first
// failure, whenever it came, else nil. While start runs, ctx's end stops
// the parts, a start under way too; once they have started, it begins the
// process's leaving: what whenLeaving was given runs while every part still
// runs, and then the parts stop. A part's failure stops them at once, a
// leaving under way too, which it may have left undone, so it is returned
// though ctx had ended: a failure that ends ctx itself, as a server's whose
// listener's close does, comes after ctx's end all the same.
func lifecycle(ctx context.Context, start func(p *parts)) error {
	p := &parts{caller: ctx, armed: make(chan struct{})}
	p.ctx, p.cancel = context.WithCancel(context.WithoutCancel(ctx))
	defer p.cancel()
	starting := context.AfterFunc(ctx, p.cancel)
	start(p)
	if starting() {
		defer context.AfterFunc(ctx, func() {
			p.leave()
			p.cancel()
		})()
		close(p.armed)
	}
	p.wait()
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.err
}

// parts are a process's running parts: the first to fail ends them all.
type parts struct {
	// caller is Run's context, whose end begins the process's leaving; ctx
	// is the parts', which ends once it has left, or a part has failed.
	// armed is closed once the parts have started and caller's end will
	// begin their leaving.
	caller context.Context
	armed  chan struct{}
	ctx    context.Context
	cancel context.CancelFunc
	group  sync.WaitGroup
	stops  []func()
	// leaving run when the caller's context ends, while every part still
	// runs, in order.
	leaving []func()
	// retiring also follows Leave, which keeps the parts running.
	retiring atomic.Bool
	// admitter is the edge's: the admission node's front door, bound once
	// it is made, and none on a process without one.
	admitter edge.LateAdmitter

	mu  sync.Mutex
	err error
}

// serving is the edge's answer to the balancer: the node serves until the
// process begins leaving (Leave, or the caller's context) or a part has
// failed, which ends the parts without their leaving.
func (p *parts) serving() bool {
	return !p.retiring.Load() && p.ctx.Err() == nil
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

// whenLeaving is run when the caller's context ends, before the parts are
// stopped.
func (p *parts) whenLeaving(f func()) { p.leaving = append(p.leaving, f) }

// leave runs what whenLeaving was given, unless a part's failure has
// stopped the process already.
func (p *parts) leave() {
	for _, f := range p.leaving {
		if p.ctx.Err() != nil {
			return
		}
		f()
	}
}

// ownersClock is a reading of the owner's clock, offset as the
// configuration says.
func (c Config) ownersClock() time.Time { return time.Now().Add(c.ClockOffset) }

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
	node, err := ring.Start(p.ctx, s, cfg.Address, roles(cfg), cfg.Ring)
	if err != nil {
		return err
	}
	p.stop(node.Stop)
	members, err := ring.Watch(p.ctx, s, cfg.Ring)
	if err != nil {
		return err
	}
	p.stop(members.Stop)

	// The switch is read once before serving, and then every interval: a
	// node that cannot read it admits for no workspace. The owner asks it
	// as it makes each hold, and the front door before it routes.
	sw := &workspaceSwitch{read: s.EnabledWorkspaces, every: cfg.Switch, clock: time.Now}
	if err := sw.refresh(p.ctx); err != nil {
		log.Printf("service: reading the fast path's switch: %v", err)
	}
	p.run("switch", func(ctx context.Context) error {
		sw.run(ctx)
		return nil
	})
	oc := cfg.Owner
	oc.Epoch, oc.Spanner, oc.Node, oc.Records = node.Epoch(), s, cfg.Address, owner.FromRecords(records)
	oc.NewAuthorization, oc.Clock, oc.Enabled = store.NewAuthorizationID, cfg.ownersClock, sw.Enabled
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
	fc.Enabled = sw.Enabled
	fc.Owners = owners{self: cfg.Address, local: local, remote: frontdoor.HTTPOwners{Client: client, Scheme: "http"}}
	fc.Store, fc.Records, fc.Members, fc.Key, fc.Accept = s, frontdoor.FromRecords(records), members, cfg.Key,
		cfg.AcceptKeys
	fc.Shards = func(string) int64 { return cfg.Shards }
	fc.Self, fc.Peers, fc.Node = cfg.Address, frontdoor.HTTPPeers{Client: client, Scheme: "http"}, node
	fc.Clock = time.Now
	door, err := frontdoor.New(fc)
	if err != nil {
		return err
	}
	p.run("front door", door.Run)
	// The edge, made before the node, admits through its front door.
	p.admitter.Bind(door)

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
	// The node leaves when Leave says so, and when the process stops: its
	// owner retires, and its row is marked leaving, within LiveFor; a write
	// that fails is written again with the node's next heartbeat. A
	// stopping node's owner then hands its leases off.
	leave := func(ctx context.Context) {
		p.retiring.Store(true)
		o.Retire()
		ctx, cancel := context.WithTimeout(ctx, cfg.Store.LiveFor)
		defer cancel()
		if err := node.SetState(ctx, store.Leaving); err != nil {
			log.Printf("fastpath: marking %s leaving: %v", cfg.Address, err)
		}
	}
	if cfg.Leave != nil {
		p.run("leaving", func(ctx context.Context) error {
			select {
			case <-cfg.Leave:
				// The write ends when the process begins to leave, which
				// writes the row itself, or when a part fails.
				lctx, cancel := context.WithCancel(p.caller)
				defer context.AfterFunc(ctx, cancel)()
				leave(lctx)
				cancel()
			case <-ctx.Done():
			}
			return nil
		})
	}
	p.whenLeaving(func() {
		leave(p.ctx)
		if cfg.HandOff == 0 {
			return
		}
		hctx, cancel := context.WithTimeout(p.ctx, cfg.HandOff)
		defer cancel()
		if err := o.Handoff(hctx); err != nil {
			log.Printf("fastpath: the owner's hand-off: %v", err)
		}
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

// roles are a process's membership roles (tr_fastpath_member): an
// admission node's owner and front door, and the auditor's, so that a
// deploy sees what a node runs (docs/runbooks/fastpath-deploy.md).
func roles(cfg Config) []string {
	var out []string
	if cfg.Admission {
		out = append(out, ring.OwnerRole, "frontdoor")
	}
	if cfg.Auditor {
		out = append(out, "auditor")
	}
	if cfg.Edge.Address != "" {
		out = append(out, "edge")
	}
	return out
}

// auditor starts an auditor member: the runtime on the settle log's
// subscription, the ticker, the pending work and the record topic's stager.
// One of its own joins the membership with the auditor role, so that a
// deploy sees it live; one beside an admission node is on the node's row.
func (p *parts) auditor(cfg Config, c Clients, s *store.Store, settle *settlelog.Log, records *settlelog.Records) error {
	if !cfg.Admission {
		node, err := ring.Start(p.ctx, s, cfg.Address, roles(cfg), cfg.Ring)
		if err != nil {
			return err
		}
		p.stop(node.Stop)
	}
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

// alertOutput is where the default alert writes: the process's standard
// error, which Cloud Logging reads.
var alertOutput io.Writer = os.Stderr

// writeAlert writes an alert as one line of JSON, as Cloud Logging reads a
// process's standard error: severity ERROR, under the plain name Cloud
// Logging's own agents read and under the Ops Agent's special field, which
// a node's journal reaches Cloud Logging through; the message, the subject,
// and the alert's kind (auditor.AlertKind) as the label fastpath_alert,
// which the alert policies match (docs/runbooks/fastpath-alerts.md). It
// carries what the alert carries: a lease or an authorization and what
// happened, never a record's contents.
func writeAlert(w io.Writer, subject, what string) {
	writeAlertOf(w, subject, what, auditor.AlertKind(what))
}

// writeAlertOf is writeAlert with its kind given: an alert that is not the
// auditor's (edge.AlertWouldAdmit).
func writeAlertOf(w io.Writer, subject, what, kind string) {
	line, err := json.Marshal(map[string]any{
		"message":                       "fastpath alert: " + what,
		"subject":                       subject,
		"logging.googleapis.com/labels": map[string]string{"fastpath_alert": kind},
	})
	if err != nil {
		log.Printf("alert: %s: %s", subject, what)
		return
	}
	// The line's severity is the journal's priority: systemd reads a
	// service's stdout line's "<3>" prefix as PRIORITY=3 (err) and strips
	// it, and the Ops Agent's journald receiver maps that priority to the
	// entry's ERROR. A severity written inside the JSON instead, the
	// agent's special field included, is parsed but outranked by the
	// priority (seen in Cloud Logging as INFO alerts, 2026-10-14).
	fmt.Fprintln(w, "<3>"+string(line))
}
