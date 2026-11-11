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
