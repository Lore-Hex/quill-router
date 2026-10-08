package frontdoor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
)

// The spike's network (spike plan §2): JSON over HTTP. Each request is a
// POST of one JSON value, and each answer a 200 with one, the request's
// status in it. A node serves its front door's gateway requests under /v1/,
// its owner's part under /owner/, and its front door's relays for peers
// under /peer/.

// maxBody bounds a request's and an answer's body: a settle's full record
// is the largest.
const maxBody = 4 << 20

// Handler serves a node: its front door's requests and relays, if it has a
// front door, and its owner's part, if it has an owner.
func Handler(fd *FrontDoor, local *Local) http.Handler {
	mux := http.NewServeMux()
	if fd != nil {
		mux.HandleFunc("POST /v1/authorize", serve(func(ctx context.Context, a AuthorizeOf) (Authorized, error) {
			return fd.Authorize(ctx, a), nil
		}))
		mux.HandleFunc("POST /v1/heartbeat", serve(func(ctx context.Context, hb HeartbeatOf) (HeartbeatAnswer, error) {
			return fd.Heartbeat(ctx, hb), nil
		}))
		mux.HandleFunc("POST /v1/settle", serve(func(ctx context.Context, s SettleOf) (TerminalAnswer, error) {
			return fd.Settle(ctx, s), nil
		}))
		mux.HandleFunc("POST /v1/refund", serve(func(ctx context.Context, r RefundOf) (TerminalAnswer, error) {
			return fd.Refund(ctx, r), nil
		}))
		mux.HandleFunc("POST /peer/heartbeat", serve(func(ctx context.Context, r relayHeartbeat) (HeartbeatAnswer, error) {
			return fd.RelayHeartbeat(ctx, r.Owner, r.Request)
		}))
		mux.HandleFunc("POST /peer/terminal", serve(func(ctx context.Context, r relayTerminal) (OwnerTerminalAnswer, error) {
			return fd.RelayTerminal(ctx, r.Owner, r.Request)
		}))
	}
	if local != nil {
		mux.HandleFunc("POST /owner/authorize", serve(func(_ context.Context, req OwnerAuthorize) (OwnerAdmitted, error) {
			return local.Authorize(req), nil
		}))
		mux.HandleFunc("POST /owner/heartbeat", serve(func(ctx context.Context, req OwnerHeartbeat) (HeartbeatAnswer, error) {
			return local.Heartbeat(ctx, req), nil
		}))
		mux.HandleFunc("POST /owner/terminal", serve(func(ctx context.Context, req OwnerTerminal) (OwnerTerminalAnswer, error) {
			return local.Terminal(ctx, req), nil
		}))
		mux.HandleFunc("GET /owner/ping", func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		})
	}
	return mux
}

// relayHeartbeat and relayTerminal are a peer's relay requests: the owner
// to send the request to, and the request.
type relayHeartbeat struct {
	Owner   string
	Request OwnerHeartbeat
}

type relayTerminal struct {
	Owner   string
	Request OwnerTerminal
}

// serve reads a request's one JSON value (readOne) and answers f's answer:
// 400 for a body that is not one, and 502 for f's error, an owner a relay
// did not reach. A request whose caller has gone by then starts nothing.
func serve[Req, Ans any](f func(context.Context, Req) (Ans, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req Req
		if err := readOne(r.Body, &req); err != nil {
			http.Error(w, "frontdoor: a request that is not one JSON value of its kind", http.StatusBadRequest)
			return
		}
		if r.Context().Err() != nil {
			return
		}
		ans, err := f(r.Context(), req)
		if err != nil {
			http.Error(w, "frontdoor: the owner was not reached", http.StatusBadGateway)
			return
		}
		body, err := json.Marshal(ans)
		if err != nil {
			http.Error(w, "frontdoor: an answer that is not JSON", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}
}

// readOne reads a body whole into v: one JSON object of v's type, with no
// field the type lacks, and nothing after it but space. A body that is not
// that, one past maxBody, or one whose read fails, as one cut short does,
// is an error.
func readOne(r io.Reader, v any) error {
	body, err := io.ReadAll(io.LimitReader(r, maxBody+1))
	if err != nil {
		return err
	}
	if len(body) > maxBody {
		return fmt.Errorf("frontdoor: a body past %d bytes", maxBody)
	}
	if trimmed := bytes.TrimLeft(body, " \t\r\n"); len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("frontdoor: a body that is not a JSON object")
	}
	d := json.NewDecoder(bytes.NewReader(body))
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return err
	}
	if _, err := d.Token(); !errors.Is(err, io.EOF) {
		return errors.New("frontdoor: a body with more than one JSON value")
	}
	return nil
}

// noRedirect is c following no redirect, so that an answer is the
// addressed node's own; c is left as it is.
func noRedirect(c *http.Client) *http.Client {
	nc := *c
	nc.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &nc
}

// HTTPOwners reaches other nodes' owners over the network, at their
// addresses, host and port. An answer that does not come, or is not a 200
// with one JSON value (readOne) from the node addressed, is an owner not
// reached.
type HTTPOwners struct {
	Client *http.Client
	// Scheme is http or https.
	Scheme string
}

// Authorize sends an authorize to the owner at address.
func (h HTTPOwners) Authorize(ctx context.Context, address string, req OwnerAuthorize) (OwnerAdmitted, error) {
	var a OwnerAdmitted
	return a, post(ctx, h.Client, h.Scheme, address, "/owner/authorize", req, &a)
}

// Heartbeat sends a heartbeat to the owner at address.
func (h HTTPOwners) Heartbeat(ctx context.Context, address string, req OwnerHeartbeat) (HeartbeatAnswer, error) {
	var a HeartbeatAnswer
	return a, post(ctx, h.Client, h.Scheme, address, "/owner/heartbeat", req, &a)
}

// Terminal sends a terminal to the owner at address.
func (h HTTPOwners) Terminal(ctx context.Context, address string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	var a OwnerTerminalAnswer
	return a, post(ctx, h.Client, h.Scheme, address, "/owner/terminal", req, &a)
}

// Ping reaches the owner at address.
func (h HTTPOwners) Ping(ctx context.Context, address string) error {
	r, err := http.NewRequestWithContext(ctx, http.MethodGet, h.Scheme+"://"+address+"/owner/ping", nil)
	if err != nil {
		return err
	}
	resp, err := noRedirect(h.Client).Do(r)
	if err != nil {
		return fmt.Errorf("%w: %w", ErrUnreachable, err)
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxBody))
	if resp.StatusCode != http.StatusNoContent {
		return fmt.Errorf("%w: %s", ErrUnreachable, resp.Status)
	}
	return nil
}

// HTTPPeers reaches other nodes' front doors over the network, to relay a
// request to an owner.
type HTTPPeers struct {
	Client *http.Client
	Scheme string
}

// Heartbeat has the front door at peer send a heartbeat to its owner.
func (h HTTPPeers) Heartbeat(ctx context.Context, peer, owner string, req OwnerHeartbeat) (HeartbeatAnswer, error) {
	var a HeartbeatAnswer
	return a, post(ctx, h.Client, h.Scheme, peer, "/peer/heartbeat", relayHeartbeat{Owner: owner, Request: req}, &a)
}

// Terminal has the front door at peer send a terminal to its owner.
func (h HTTPPeers) Terminal(ctx context.Context, peer, owner string, req OwnerTerminal) (OwnerTerminalAnswer, error) {
	var a OwnerTerminalAnswer
	return a, post(ctx, h.Client, h.Scheme, peer, "/peer/terminal", relayTerminal{Owner: owner, Request: req}, &a)
}

// Gateway is a gateway's client of a front door, at its base URL: what
// the spike's load generator plays (spike plan §2).
type Gateway struct {
	Client *http.Client
	Base   string
}

// Authorize sends an authorize.
func (g Gateway) Authorize(ctx context.Context, a AuthorizeOf) (Authorized, error) {
	var ans Authorized
	return ans, postURL(ctx, g.Client, g.Base+"/v1/authorize", a, &ans)
}

// Heartbeat sends a heartbeat.
func (g Gateway) Heartbeat(ctx context.Context, hb HeartbeatOf) (HeartbeatAnswer, error) {
	var ans HeartbeatAnswer
	return ans, postURL(ctx, g.Client, g.Base+"/v1/heartbeat", hb, &ans)
}

// Settle sends a settle.
func (g Gateway) Settle(ctx context.Context, s SettleOf) (TerminalAnswer, error) {
	var ans TerminalAnswer
	return ans, postURL(ctx, g.Client, g.Base+"/v1/settle", s, &ans)
}

// Refund sends a refund.
func (g Gateway) Refund(ctx context.Context, r RefundOf) (TerminalAnswer, error) {
	var ans TerminalAnswer
	return ans, postURL(ctx, g.Client, g.Base+"/v1/refund", r, &ans)
}

func post(ctx context.Context, c *http.Client, scheme, address, path string, req, ans any) error {
	return postURL(ctx, c, scheme+"://"+address+path, req, ans)
}

// postURL posts req's JSON and reads the answer's into ans. Anything but a
// 200 with one JSON value (readOne), from the URL itself, is ErrUnreachable.
func postURL(ctx context.Context, c *http.Client, url string, req, ans any) error {
	body, err := json.Marshal(req)
	if err != nil {
		return err
	}
	r, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	r.Header.Set("Content-Type", "application/json")
	resp, err := noRedirect(c).Do(r)
	if err != nil {
		return fmt.Errorf("%w: %w", ErrUnreachable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxBody))
		return fmt.Errorf("%w: %s", ErrUnreachable, resp.Status)
	}
	if err := readOne(resp.Body, ans); err != nil {
		return fmt.Errorf("%w: an answer that is not one JSON value of its kind: %w", ErrUnreachable, err)
	}
	return nil
}
