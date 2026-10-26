package gwauth

import (
	"bytes"
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// Positive controls: each near miss of Python's bytes, built from the same
// composed values, fails the comparisons the golden tests make. A control
// that passed would mean the goldens cannot tell that mistake apart.

// body is {"data": data + timing} by enc, as a 200 the encoder would write.
func body(t *testing.T, data pyjson.Object, timing Timing, enc func(any) ([]byte, error)) []byte {
	t.Helper()
	d := append(slices.Clone(data), pyjson.Member{Key: "timing", Value: pyjson.Object{
		{Key: "total_ms", Value: timing.TotalMS}, {Key: "spanner_rpcs", Value: timing.SpannerRPCs},
		{Key: "key_lookup_ms", Value: timing.KeyLookupMS}, {Key: "routing_ms", Value: timing.RoutingMS},
		{Key: "store_ms", Value: timing.StoreMS}, {Key: "post_commit_ms", Value: timing.PostCommitMS},
	}})
	b, err := enc(pyjson.Object{{Key: "data", Value: d}})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func dataOf(t *testing.T, in ComposeInput, c goldenCase) pyjson.Object {
	t.Helper()
	tags := mergeTags(in.KeyTags, in.RequestTags)
	if c.Insert == "unknown_landed" {
		slices.SortFunc(tags, func(a, b ComposeTag) int { return strings.Compare(a.Key, b.Key) })
	}
	data, err := responseData(in, GenerationID(in.AuthorizationID), tags)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// TestControlEncodersAgreeWithRender: the controls below build their
// bodies the way Render's are built, so a control's difference is the one
// mistake it makes, not a difference in how it was assembled.
func TestControlEncodersAgreeWithRender(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	for _, c := range answered(f) {
		in, composed := compose(t, f, snap, c)
		want := composed.Render(c.Expect.timing(), c.Insert == "unknown_landed")
		if got := body(t, dataOf(t, in, c), c.Expect.timing(), pyjson.DumpsPydantic); !bytes.Equal(got, want) {
			t.Fatalf("%s: the control's pydantic body is not Render's\n got %s\nwant %s", c.Name, got, want)
		}
	}
}

// TestControlReprFloatFails: json.dumps's float (repr) in place of
// pydantic-core's writes 1e-06 for 1e-6; every float.exponent case then
// differs from Python's 200, and only in estimated_cost.
func TestControlReprFloatFails(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	repr := func(v any) ([]byte, error) { return pyjson.Dumps(v, pyjson.Options{}) }
	caught := 0
	for _, c := range answered(f) {
		if !hasLabel(c, "float.exponent") {
			continue
		}
		in, _ := compose(t, f, snap, c)
		got := body(t, dataOf(t, in, c), c.Expect.timing(), repr)
		if string(got) == c.Expect.Response {
			t.Errorf("%s: the repr float body equals Python's 200", c.Name)
			continue
		}
		if !bytes.Contains(got, []byte(`"estimated_cost":`+pyjson.FormatFloat(float64(in.Estimate)/1e6)+`,`)) {
			t.Errorf("%s: the repr body does not carry repr's float", c.Name)
		}
		caught++
	}
	if caught < 9 {
		t.Fatalf("the repr control was caught on %d float.exponent cases, want every one (9)", caught)
	}
}

// TestControlKeyOrderFails: a body whose data has two members swapped
// decodes to the same JSON value but is not Python's bytes, for every
// answered case.
func TestControlKeyOrderFails(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	for _, c := range answered(f) {
		in, _ := compose(t, f, snap, c)
		data := dataOf(t, in, c)
		data[4], data[5] = data[5], data[4] // model, upstream_model
		got := body(t, data, c.Expect.timing(), pyjson.DumpsPydantic)
		if string(got) == c.Expect.Response {
			t.Errorf("%s: a swapped key order equals Python's 200", c.Name)
		}
		var a, b any
		if json.Unmarshal(got, &a) != nil || json.Unmarshal([]byte(c.Expect.Response), &b) != nil || !reflect.DeepEqual(a, b) {
			t.Errorf("%s: the swap changed more than the order", c.Name)
		}
	}
}

// TestControlUnsortedPayloadFails: the payload in insertion order (json_body
// without sort_keys) is not Python's payload for any answered case.
func TestControlUnsortedPayloadFails(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	for _, c := range answered(f) {
		in, composed := compose(t, f, snap, c)
		staged, err := Stage(in)
		if err != nil {
			t.Fatal(err)
		}
		members := append(slices.Clone(staged.payload),
			pyjson.Member{Key: "fastpath_envelope", Value: c.Admitted.Envelope},
			pyjson.Member{Key: "fastpath_end_of_life", Value: EndOfLifeText(endOfLife(t, c))})
		unsorted, err := pyjson.Dumps(members, pyjson.Options{EnsureASCII: true})
		if err != nil {
			t.Fatal(err)
		}
		if string(unsorted) == c.Expect.AuthorizationRow["payload"] {
			t.Errorf("%s: the unsorted payload equals Python's", c.Name)
		}
		sorted, _ := pyjson.Canonical(members)
		if string(sorted) != composed.Authorization.Payload {
			t.Errorf("%s: the control's sorted payload is not Finish's", c.Name)
		}
	}
}

// TestStageRefusesWhatPythonWouldNotWrite: every Stage and Finish refusal
// is an error (the caller passes), never a body. A string that is not
// UTF-8 makes DumpsPydantic fail, and Stage passes it on.
func TestStageRefusesWhatPythonWouldNotWrite(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	good := inputOf(t, f, snap, answered(f)[0])
	if _, err := Stage(good); err != nil {
		t.Fatalf("positive control: %v", err)
	}
	refuse := map[string]func(in *ComposeInput){
		"authorization id uppercase": func(in *ComposeInput) { in.AuthorizationID = strings.ToUpper(in.AuthorizationID) },
		"authorization id no prefix": func(in *ComposeInput) { in.AuthorizationID = in.AuthorizationID[4:] },
		"reservation id unhyphenated": func(in *ComposeInput) {
			in.ReservationID = strings.ReplaceAll(in.ReservationID, "-", "")
		},
		"reservation id not v4": func(in *ComposeInput) { in.ReservationID = "ae2c05ab-2ea0-12e7-a9d8-6a3ea48d6034" },
		"no candidates":         func(in *ComposeInput) { in.Candidates = nil },
		"no clock":              func(in *ComposeInput) { in.At = time.Time{} },
		"inference_location missing a key": func(in *ComposeInput) {
			in.Candidates = slices.Clone(in.Candidates)
			in.Candidates[0].InferenceLocation = in.Candidates[0].InferenceLocation[1:]
		},
		"inference_location with another key": func(in *ComposeInput) {
			in.Candidates = slices.Clone(in.Candidates)
			loc := slices.Clone(in.Candidates[0].InferenceLocation)
			loc[0].Key = "advertised_regionz"
			in.Candidates[0].InferenceLocation = loc
		},
		"a string pydantic-core cannot write": func(in *ComposeInput) { in.RequestedModel = "z-ai/\xffglm" },
	}
	for name, mutate := range refuse {
		in := good
		mutate(&in)
		if _, err := Stage(in); err == nil {
			t.Errorf("%s: Stage composed it", name)
		}
	}
	staged, _ := Stage(good)
	if _, err := staged.Finish("env\xff", time.Now()); err == nil {
		t.Error("Finish wrote an envelope that is not UTF-8")
	}
	if _, err := staged.Finish("env", time.Time{}); err == nil {
		t.Error("Finish wrote no end of life")
	}
	if _, err := (Staged{}).Finish("env", time.Now()); err == nil {
		t.Error("Finish on a zero Staged")
	}
}

// TestStageTakesOneClockReading: the authorization's created_at, the
// payload's created_at and expires_at derive from one reading, UTC, to the
// microsecond; expires_at is exactly 7200 s later (§5). The reservation's
// created_at is the commit's timestamp, no reading of the edge's.
func TestStageTakesOneClockReading(t *testing.T) {
	f, snap := loadGoldens(t), loadSnapshot(t)
	in := inputOf(t, f, snap, answered(f)[0])
	in.At = time.Date(2026, 10, 19, 4, 0, 0, 123456789, time.FixedZone("", 2*3600))
	staged, err := Stage(in)
	if err != nil {
		t.Fatal(err)
	}
	c, err := staged.Finish("env", in.At)
	if err != nil {
		t.Fatal(err)
	}
	want := time.Date(2026, 10, 19, 2, 0, 0, 123456000, time.UTC)
	if c.Authorization.CreatedAt != want || c.Reservation.ExpiresAt != want.Add(7200*time.Second) {
		t.Errorf("rows %v %v, want %v", c.Authorization.CreatedAt, c.Reservation.ExpiresAt, want)
	}
	if !strings.Contains(c.Authorization.Payload, `"created_at":"2026-10-19T02:00:00.123456Z"`) {
		t.Errorf("payload created_at: %s", c.Authorization.Payload)
	}
	if !strings.Contains(c.Authorization.Payload, `"fastpath_end_of_life":"2026-10-19T04:00:00.123456+02:00"`) {
		t.Errorf("payload end of life: %s", c.Authorization.Payload)
	}
}

// TestMergeTagsIsADictUpdate: the key's tags in their order, then the
// request's, a repeated key keeping its first place and its last value.
func TestMergeTagsIsADictUpdate(t *testing.T) {
	p := func(kv ...string) []ComposeTag {
		var out []ComposeTag
		for i := 0; i < len(kv); i += 2 {
			out = append(out, ComposeTag{Key: kv[i], Value: kv[i+1]})
		}
		return out
	}
	got := mergeTags(p("team", "core", "env", "prod"), p("env", "dev", "app", "cli", "team", "x"))
	if want := p("team", "x", "env", "dev", "app", "cli"); !slices.Equal(got, want) {
		t.Errorf("merge %v, want %v", got, want)
	}
	if got := mergeTags(nil, nil); len(got) != 0 {
		t.Errorf("empty merge %v", got)
	}
}
