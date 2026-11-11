package gwauth

import (
	"bytes"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

func TestTicketRenderEqualsResponseDataForBothHeads(t *testing.T) {
	g := loadAsyncGoldens(t)
	var c asyncCase
	for _, v := range g.cases {
		if v.Name == "answer:async.B:invocation_nonce" {
			c = v
			break
		}
	}
	in, _ := asyncInputOf(t, g, c)
	in.KeyTags = []ComposeTag{{Key: "z", Value: "first"}, {Key: "a", Value: "second"}}
	staged, err := Stage(in)
	if err != nil {
		t.Fatal(err)
	}
	composed, err := staged.Finish(c.Admitted.Envelope, endOfLife(t, c.goldenCase))
	if err != nil {
		t.Fatal(err)
	}
	for _, eligible := range []bool{false, true} {
		ticket := Ticket{Eligible: eligible, SettlementTicket: "a.b.c", StatusURL: "/v1/settlements/" + in.AuthorizationID + ".settle"}
		got, err := composed.WithTicket(ticket)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got.Authorization, composed.Authorization) || !reflect.DeepEqual(got.Reservation, composed.Reservation) {
			t.Fatal("ticket changed stored rows")
		}
		async := staged.Async()
		async.Members = slices.Clone(async.Members)
		async.Members[0].Value = eligible
		async.Members = append(async.Members, pyjson.Member{Key: "settlement_ticket", Value: ticket.SettlementTicket},
			pyjson.Member{Key: "settlement_status_url", Value: ticket.StatusURL})
		for _, readBack := range []bool{false, true} {
			tags := mergeTags(in.KeyTags, in.RequestTags)
			if readBack {
				slices.SortFunc(tags, func(a, b ComposeTag) int { return strings.Compare(a.Key, b.Key) })
			}
			data, err := responseData(in, staged.generationID, tags, async, nil)
			if err != nil {
				t.Fatal(err)
			}
			head, err := renderHead(data)
			if err != nil {
				t.Fatal(err)
			}
			want := (Composed{inserted: head, readBack: head}).Render(Timing{}, readBack)
			if !bytes.Equal(got.Render(Timing{}, readBack), want) {
				t.Fatalf("ticket render differs: eligible %v, read back %v", eligible, readBack)
			}
			if bytes.Contains(composed.Render(Timing{}, readBack), []byte("settlement_ticket")) {
				t.Fatal("original head changed")
			}
		}
	}
}

// A ticket is spliced only onto the billing snapshot. Shape A's lone
// async_eligible member is unique in the head too, so without the shape
// check the splice would succeed and turn a refusal into a ticketed answer.
func TestTicketRefusesAnAnswerWithoutABillingSnapshot(t *testing.T) {
	g := loadAsyncGoldens(t)
	refused := map[string]int{}
	for _, c := range g.cases {
		if c.target == nil || c.target.response == "" {
			continue
		}
		in, _ := asyncInputOf(t, g, c)
		if in.Async.Policy.AdmissionEnabled && !in.Async.WantTicket {
			continue
		}
		staged, err := Stage(in)
		if err != nil {
			continue
		}
		shape := staged.Async().Shape
		if shape == AsyncSnapshot {
			continue
		}
		composed, err := staged.Finish(c.Admitted.Envelope, endOfLife(t, c.goldenCase))
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		ticket := Ticket{Eligible: true, SettlementTicket: "a.b.c", StatusURL: "/v1/settlements/" + in.AuthorizationID + ".settle"}
		got, err := composed.WithTicket(ticket)
		if err == nil {
			t.Fatalf("%s: a ticket was spliced onto shape %s", c.Name, shape)
		}
		for _, readBack := range []bool{false, true} {
			want := composed.Render(Timing{}, readBack)
			if !bytes.Equal(got.Render(Timing{}, readBack), want) || bytes.Contains(want, []byte("settlement_ticket")) {
				t.Fatalf("%s: a refused ticket changed the answer", c.Name)
			}
		}
		refused[shape]++
	}
	if refused[AsyncFalseOnly] == 0 {
		t.Fatalf("no shape A answer was held to the refusal: %v", refused)
	}
}
