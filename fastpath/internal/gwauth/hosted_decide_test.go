package gwauth

import (
	"encoding/json"
	"testing"
)

func TestHostedDecideComposeGoldens(t *testing.T) {
	g := loadAsyncGoldensFile(t, "authorize_hosted_decide_v1.json")
	for _, c := range g.cases {
		t.Run(c.Name, func(t *testing.T) {
			if c.Expect.Outcome != "answer" {
				return
			}
			in, _ := asyncInputOf(t, g, c)
			staged, err := Stage(in)
			if err != nil {
				t.Fatal(err)
			}
			a := staged.Async()
			if a.Shape != asyncShapes[c.target.shape] {
				t.Fatalf("shape=%s reason=%s", a.Shape, a.Reason)
			}
			composed, err := staged.Finish(c.Admitted.Envelope, endOfLife(t, c.goldenCase))
			if err != nil {
				t.Fatal(err)
			}
			if c.target.shape == "C" {
				var expected struct {
					Data struct {
						Eligible bool   `json:"async_eligible"`
						Ticket   string `json:"settlement_ticket"`
						URL      string `json:"settlement_status_url"`
					} `json:"data"`
				}
				if err := json.Unmarshal([]byte(c.target.response), &expected); err != nil {
					t.Fatal(err)
				}
				if !a.WantTicket {
					t.Fatal("ticket not requested")
				}
				composed, err = composed.WithTicket(Ticket{Eligible: expected.Data.Eligible, SettlementTicket: expected.Data.Ticket, StatusURL: expected.Data.URL})
				if err != nil {
					t.Fatal(err)
				}
			}
			if string(composed.Render(c.target.timing, false)) != c.target.response {
				t.Fatal("composed bytes differ from Python")
			}
		})
	}
}

func TestDecideAsyncFactsNeverStream(t *testing.T) {
	for _, stream := range []string{"false", "true"} {
		r, _, p := parseBody(bodyOf(`"route_type":"decide"`, `"stream":`+stream), Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"async-v1"}})
		if p.Passed() || r.Async.Streamed {
			t.Fatalf("decide async facts: pass=%s streamed=%v", p.Reason, r.Async.Streamed)
		}
	}
}
