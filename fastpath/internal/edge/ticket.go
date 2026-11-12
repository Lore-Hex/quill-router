package edge

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// DefaultAsyncTicketTimeout and DefaultAsyncTicketGrace are startup defaults.
// The command binds its flags here because the service constructs edge.Config.
// Each Edge copies them at construction; explicit Config values take precedence.
var (
	DefaultAsyncTicketTimeout = 250 * time.Millisecond
	DefaultAsyncTicketGrace   = 40 * time.Millisecond
)

const ticketPath = "/internal/fastpath/async-ticket"
const maxTicketBody = 8 * 1024

var ticketAuthorizationID = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
var compactTicket = regexp.MustCompile(`^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$`)

type ticketRequest struct {
	V                    int      `json:"v"`
	AuthorizationID      string   `json:"authorization_id"`
	WorkspaceID          string   `json:"workspace_id"`
	KeyHash              string   `json:"key_hash"`
	ReservationID        string   `json:"reservation_id"`
	InvocationNonce      string   `json:"invocation_nonce"`
	RouteType            string   `json:"route_type"`
	Streamed             bool     `json:"streamed"`
	Region               string   `json:"region"`
	CandidateEndpointIDs []string `json:"candidate_endpoint_ids"`
	PricedAt             int64    `json:"priced_at"`
	BillingSnapshotHash  string   `json:"billing_snapshot_hash"`
}

type ticketClient struct{ fetcher }

func newTicketClient(u, upstream *url.URL, token string, transport http.RoundTripper) (*ticketClient, error) {
	if u == nil || u.Path != ticketPath || u.RawPath != "" || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("edge: the ticket route must have its fixed path")
	}
	f, err := newFetcher("async ticket", u, upstream, token, transport, nil, nil, 0, 0, 0)
	if err != nil {
		return nil, err
	}
	return &ticketClient{f}, nil
}

func ticketRequestOf(d *Decision) (ticketRequest, bool) {
	a, in := d.Staged.Async(), d.Input
	if !a.WantTicket || !ticketAuthorizationID.MatchString(in.AuthorizationID) {
		return ticketRequest{}, false
	}
	nonce := ""
	if in.InvocationNonce != nil {
		nonce = *in.InvocationNonce
	}
	return ticketRequest{V: 1, AuthorizationID: in.AuthorizationID, WorkspaceID: in.WorkspaceID,
		KeyHash: in.KeyHash, ReservationID: in.ReservationID, InvocationNonce: nonce,
		RouteType: *in.Async.Request.RouteType, Streamed: in.Async.Request.Streamed, Region: in.Region,
		CandidateEndpointIDs: a.EndpointIDs, PricedAt: a.PricedAt, BillingSnapshotHash: a.SnapshotHash}, true
}

func ticketStatusURL(id string) string { return "/v1/settlements/" + id + ".settle" }

// fetch returns a ticket, a decline (nil and no stage), or a failed stage.
// It logs no response values: only the stage is counted.
func (c *ticketClient) fetch(ctx context.Context, body ticketRequest) (*gwauth.Ticket, string) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, "request"
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url.String(), bytes.NewReader(raw))
	if err != nil {
		return nil, "request"
	}
	req.Header.Set(TokenHeader, c.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.client.Do(req)
	if err != nil {
		return nil, "transport"
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, "status"
	}
	raw, err = io.ReadAll(io.LimitReader(resp.Body, maxTicketBody+1))
	if err != nil {
		return nil, "read"
	}
	if len(raw) > maxTicketBody {
		return nil, "size"
	}
	return parseTicket(raw, ticketStatusURL(body.AuthorizationID))
}

func parseTicket(raw []byte, statusURL string) (*gwauth.Ticket, string) {
	value, err := pyjson.Decode(raw)
	if err != nil {
		return nil, "parse"
	}
	object, ok := value.(pyjson.Object)
	if !ok {
		return nil, "parse"
	}
	fields := make(map[string]any, len(object))
	for _, m := range object {
		fields[m.Key] = m.Value
	}
	eligible, ok := fields["async_eligible"].(bool)
	if !ok {
		return nil, "parse"
	}
	if len(fields) == 1 && !eligible {
		return nil, ""
	}
	ticket, ok := fields["settlement_ticket"].(string)
	if !ok || len(fields) != 3 || len(ticket) > 4096 || !compactTicket.MatchString(ticket) {
		return nil, "parse"
	}
	for _, segment := range strings.Split(ticket, ".") {
		if _, err := base64.RawURLEncoding.Strict().DecodeString(segment); err != nil {
			return nil, "parse"
		}
	}
	status, ok := fields["settlement_status_url"].(string)
	if !ok || status != statusURL {
		return nil, "parse"
	}
	return &gwauth.Ticket{Eligible: eligible, SettlementTicket: ticket, StatusURL: status}, ""
}

type ticketResult struct {
	ticket   *gwauth.Ticket
	stage    string
	finished time.Time
}

type ticketCall struct {
	result chan ticketResult
	cancel context.CancelFunc
}

func (e *Edge) startTicket(ctx context.Context, d *Decision) *ticketCall {
	body, ok := ticketRequestOf(d)
	if !ok {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, e.cfg.AsyncTicketTimeout)
	call := &ticketCall{result: make(chan ticketResult, 1), cancel: cancel}
	e.decider.count(func(t *decideTally) { t.tickets["asked"]++ })
	go func() {
		defer cancel()
		started := time.Now()
		ticket, stage := e.tickets.fetch(ctx, body)
		finished := time.Now()
		e.decider.count(func(t *decideTally) { t.ticketCall.add(finished.Sub(started)) })
		call.result <- ticketResult{ticket: ticket, stage: stage, finished: finished}
	}()
	return call
}

// awaitTicket is called only after a durable insert. A buffered result lets
// the worker finish even when the path has already returned.
func (dp *decider) awaitTicket(call *ticketCall, committed, budget time.Time) *gwauth.Ticket {
	if call == nil {
		return nil
	}
	started := time.Now()
	defer func() { dp.count(func(t *decideTally) { t.ticketWait.add(time.Since(started)) }) }()
	deadline := minTime(committed.Add(dp.ticketGrace), budget)
	accept := func(r ticketResult) *gwauth.Ticket {
		if r.finished.After(deadline) {
			dp.count(func(t *decideTally) { t.tickets["late"]++ })
			return nil
		}
		if r.stage != "" {
			dp.count(func(t *decideTally) { t.tickets["failed"]++; t.tickets["failed."+r.stage]++ })
		} else if r.ticket == nil {
			dp.count(func(t *decideTally) { t.tickets["declined"]++ })
		}
		return r.ticket
	}
	select {
	case r := <-call.result:
		return accept(r)
	default:
	}
	timer := time.NewTimer(max(time.Until(deadline), 0))
	defer timer.Stop()
	select {
	case r := <-call.result:
		return accept(r)
	case <-timer.C:
		dp.count(func(t *decideTally) { t.tickets["late"]++ })
		return nil
	}
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}
