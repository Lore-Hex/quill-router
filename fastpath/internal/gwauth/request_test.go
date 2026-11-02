package gwauth

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

const lookup = `"api_key_lookup_hash":"b57b533cfd309e134fe4b62ad6f0a17741de686ef5b1535dffd8e664e9eec507"`

var jsonHeaders = Headers{ContentType: []string{"application/json"}}

func bodyOf(fields ...string) []byte {
	return bodyModel("z-ai/glm-5.3", fields...)
}

func bodyModel(model string, fields ...string) []byte {
	return []byte("{" + strings.Join(append([]string{lookup, `"model":"` + model + `"`}, fields...), ",") + "}")
}

// TestTheReasons pins one body per request reason that the vectors reach
// only by sampling, and its positive control: the same body without the
// trigger parses.
func TestTheReasons(t *testing.T) {
	deep := func(levels int) string {
		return `"x":` + strings.Repeat("[", levels) + "1" + strings.Repeat("]", levels)
	}
	trace := func(levels int) string {
		return `"trace":` + strings.Repeat(`{"a":`, levels) + "1" + strings.Repeat("}", levels)
	}
	items := make([]string, 256)
	for i := range items {
		items[i] = `"k` + strconv.Itoa(i) + `":0`
	}
	for _, c := range []struct {
		name, reason string
		body         []byte
		headers      Headers
	}{
		{"plain", "", bodyOf(), jsonHeaders},
		{"charset", "", bodyOf(), Headers{ContentType: []string{"Application/JSON; charset=UTF-8"}}},
		{"no content type", ReasonContentType, bodyOf(), Headers{}},
		{"text", ReasonContentType, bodyOf(), Headers{ContentType: []string{"text/plain"}}},
		{"bom", ReasonUnparseable, append([]byte("\xef\xbb\xbf"), bodyOf()...), jsonHeaders},
		{"not an object", ReasonUnparseable, []byte(`[1]`), jsonHeaders},
		{"not UTF-8", ReasonUnparseable, bodyOf(`"x":"` + "\xff" + `"`), jsonHeaders},
		{"depth 128", "", bodyOf(deep(127)), jsonHeaders},
		{"depth 129", ReasonUnparseable, bodyOf(deep(128)), jsonHeaders},
		{"4000 digits", "", bodyOf(`"x":` + strings.Repeat("9", 4000)), jsonHeaders},
		{"4001 digits", ReasonUnparseable, bodyOf(`"x":` + strings.Repeat("9", 4001)), jsonHeaders},
		{"digits in a string", "", bodyOf(`"x":"` + strings.Repeat("9", 5000) + `"`), jsonHeaders},
		{"over 4 MiB", ReasonUnparseable, bodyOf(`"x":"` + strings.Repeat("a", maxBodyBytes) + `"`), jsonHeaders},
		{"lone surrogate", ReasonUnparseable, bodyOf(`"x":"\ud800"`), jsonHeaders},
		{"NaN", ReasonFloat, bodyOf(`"x":NaN`), jsonHeaders},
		{"exponent", ReasonFloat, bodyOf(`"max_tokens":1e3`), jsonHeaders},
		{"nested float", ReasonFloat, bodyOf(`"metadata":{"a":[0.5]}`), jsonHeaders},
		{"duplicate", ReasonDuplicateKey, bodyOf(`"metadata":{"a":1,"a":2}`), jsonHeaders},
		{"int as string", ReasonLaxType, bodyOf(`"max_tokens":"64"`), jsonHeaders},
		{"bool as int", ReasonLaxType, bodyOf(`"stream":0`), jsonHeaders},
		{"strict int as string", ReasonRefused, bodyOf(`"transcription_duration_ms":"5"`), jsonHeaders},
		{"int as str field", ReasonRefused, bodyOf(`"region":5`), jsonHeaders},
		{"null estimate", ReasonRefused, bodyOf(`"estimated_input_tokens":null`), jsonHeaders},
		{"lax before refused", ReasonLaxType, bodyOf(`"max_tokens":"64"`, `"region":5`), jsonHeaders},
		{"strip before refused", ReasonStripAmbiguous, bodyOf(`"user":"\tu"`, `"region":5`), jsonHeaders},
		{"dict key before refused", ReasonDictKeyStrip, bodyOf(`"tags":{" k":"v"}`, `"region":5`), jsonHeaders},
		{"lax before strip", ReasonLaxType, bodyOf(`"stream":"yes"`, `"user":"é"`), jsonHeaders},
		{"tab", ReasonStripAmbiguous, bodyOf(`"user":"\tu"`), jsonHeaders},
		{"unicode", ReasonStripAmbiguous, bodyOf(`"models":["é"]`), jsonHeaders},
		{"literal not stripped", ReasonRefused, bodyOf(`"input_modalities":[" text"]`), jsonHeaders},
		{"dict key", ReasonDictKeyStrip, bodyOf(`"provider":{" sort":"price"}`), jsonHeaders},
		{"nested dict key kept", "", bodyOf(`"metadata":{"k":{" inner ":1}}`), jsonHeaders},
		{"no key", ReasonRefused, []byte(`{"model":"z-ai/glm-5.3"}`), jsonHeaders},
		{"empty model", ReasonRefused, bodyModel("  "), jsonHeaders},
		{"trace 8 deep", "", bodyOf(trace(7)), jsonHeaders},
		{"trace 9 deep", ReasonRefused, bodyOf(trace(8)), jsonHeaders},
		{"trace 256 items", "", bodyOf(`"trace":{` + strings.Join(items, ",") + `}`), jsonHeaders},
		{"trace 257 items", ReasonRefused, bodyOf(`"trace":{` + strings.Join(items, ",") + `,"x":[]}`), jsonHeaders},
		{"trace bytes", ReasonRefused, bodyOf(`"trace":{"a":"` + strings.Repeat("é", 4100) + `"}`), jsonHeaders},
		{"referer ftp", ReasonRefused, bodyOf(`"http_referer":"ftp://example.com"`), jsonHeaders},
		{"referer no host", ReasonRefused, bodyOf(`"app":"a","http_referer":"https://user@:80/"`), jsonHeaders},
		{"referer userinfo, app", "", bodyOf(`"app":"a","http_referer":"https://user@example.com/"`), jsonHeaders},
		{"referer userinfo", ReasonReferer, bodyOf(`"http_referer":"https://user@example.com/"`), jsonHeaders},
		{"referer upper scheme, app", "", bodyOf(`"app":"a","http_referer":"HTTPS://example.com"`), jsonHeaders},
		{"referer bracket", ReasonReferer, bodyOf(`"app":"a","http_referer":"https://[::1]/"`), jsonHeaders},
		{"categories", ReasonRefused, bodyOf(`"app_categories":["a","b","c"]`), jsonHeaders},
		{"tag value", ReasonRefused, bodyOf(`"tags":{"k":1}`), jsonHeaders},
		{"tag unicode", ReasonTags, bodyOf(`"tags":{"k":"é"}`), jsonHeaders},
		{"tag reserved", ReasonRefused, bodyOf(`"tags":{"AWS:x":"y"}`), jsonHeaders},
		{"key hash", ReasonAPIKeyHash, bodyOf(`"api_key_hash":"h"`), jsonHeaders},
		{"stream", ReasonStream, bodyOf(`"stream":true`), jsonHeaders},
		{"stream false", "", bodyOf(`"stream":false`), jsonHeaders},
		{"tier", ReasonServiceTier, bodyOf(`"service_tier":"auto"`), jsonHeaders},
		{"affinity", "", bodyOf(`"cache_affinity_key":"` + strings.Repeat("a", 64) + `"`), jsonHeaders},
		{"affinity explicit only", "", bodyOf(`"cache_affinity_explicit":true`), jsonHeaders},
		{"receipt", ReasonPricedExtra, bodyOf(`"inference_receipt":true`), jsonHeaders},
		{"zero cost", "", bodyOf(`"additional_cost_reservation_microdollars":0`), jsonHeaders},
		{"cost", ReasonPricedExtra, bodyOf(`"additional_cost_reservation_microdollars":5`), jsonHeaders},
		{"video", ReasonPricedExtra, bodyOf(`"video_resolution":"720p"`), jsonHeaders},
		{"alias", ReasonCanonicalModel, bodyOf(`"models":["minimax/minimax-m3-1-flash"]`), jsonHeaders},
		{"nyte", ReasonCanonicalModel, bodyModel("nyte/auto"), jsonHeaders},
		{"custom", ReasonCustomModel, bodyOf(`"models":["TR-Custom-Model/x"]`), jsonHeaders},
		{"internal", ReasonInternalModel, bodyOf(`"models":[" trustedrouter/monitor "]`), jsonHeaders},
		{"route type", ReasonEvaluatorInput, bodyOf(`"route_type":"videos"`), jsonHeaders},
		{"route type spaced", "", bodyOf(`"route_type":" responses "`), jsonHeaders},
		{"huge tokens", ReasonEvaluatorInput, bodyOf(`"max_tokens":9223372036854775808`), jsonHeaders},
		{"header twice", ReasonIdempotencyKey, bodyOf(),
			Headers{ContentType: []string{"application/json"}, IdempotencyKey: []string{"a", "b"}}},
		{"header latin-1", ReasonIdempotencyKey, bodyOf(),
			Headers{ContentType: []string{"application/json"}, IdempotencyKey: []string{"cl\xe9"}}},
		{"header too long", ReasonIdempotencyKey, bodyOf(),
			Headers{ContentType: []string{"application/json"}, IdempotencyKey: []string{strings.Repeat("k", 257)}}},
		{"header 256 spaced", "", bodyOf(),
			Headers{ContentType: []string{"application/json"}, IdempotencyKey: []string{" " + strings.Repeat("k", 256) + " "}}},
		{"body native batch", ReasonIdempotencyKey, bodyOf(`"idempotency_key":"tr-native-batch:x"`), jsonHeaders},
		{"header partner", ReasonIdempotencyKey, bodyOf(`"idempotency_key":"k"`),
			Headers{ContentType: []string{"application/json"}, IdempotencyKey: []string{"partner:x"}}},
		{"settlement", ReasonSettlementAsync, bodyOf(),
			Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"sync"}}},
		{"settlement repeated", ReasonSettlementAsync, bodyOf(),
			Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"async-v1", "async-v1"}}},
		{"settlement spaced", ReasonSettlementAsync, bodyOf(),
			Headers{ContentType: []string{"application/json"}, SettlementMode: []string{" async-v1"}}},
		// Exactly ["async-v1"] is composed (G15c), not passed here.
		{"settlement async-v1", "", bodyOf(),
			Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"async-v1"}}},
		{"video header", ReasonVideoProviders, bodyOf(),
			Headers{ContentType: []string{"application/json"}, VideoAllowedProviders: true}},
	} {
		if _, p := ParseRequest(c.body, c.headers); p.Reason != c.reason {
			t.Errorf("%s: %q, want %q", c.name, p.Reason, c.reason)
		}
	}
}

// TestACacheAffinityKeyParsesAndIsFlagged (§1 item 5): a body naming a
// cache_affinity_key is not the request half's to pass; it parses with
// CacheAffinity set, the key excluded from the dump as the schema excludes
// it, and Go decides it in the cold order.
func TestACacheAffinityKeyParsesAndIsFlagged(t *testing.T) {
	key := strings.Repeat("a", 64)
	r, p := ParseRequest(bodyOf(`"cache_affinity_key":"`+key+`"`, `"cache_affinity_explicit":true`), jsonHeaders)
	if p.Passed() || !r.CacheAffinity {
		t.Fatalf("pass %q, affinity %v", p.Reason, r.CacheAffinity)
	}
	for _, m := range r.Dump {
		if m.Key == "cache_affinity_key" || m.Key == "cache_affinity_explicit" {
			t.Errorf("the dump carries %s", m.Key)
		}
	}
	if r, p := ParseRequest(bodyOf(`"cache_affinity_explicit":true`), jsonHeaders); p.Passed() || r.CacheAffinity {
		t.Errorf("without a key: pass %q, affinity %v", p.Reason, r.CacheAffinity)
	}
	if _, p := ParseRequest(bodyOf(`"cache_affinity_key":null`), jsonHeaders); p.Passed() {
		t.Errorf("a null key: pass %q", p.Reason)
	}
}

// TestACohortPassKeepsTheParse: a body that parsed whole and then passes a
// cohort reason returns its parse, so the shadow can count an async-v1
// request's population (G15 §4.4); a body the parse refuses returns none.
func TestACohortPassKeepsTheParse(t *testing.T) {
	key := `"cache_affinity_key":"` + strings.Repeat("a", 64) + `"`
	async := Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"async-v1"}}
	r, p := ParseRequest(bodyOf(key, `"stream":true`), async)
	if p.Reason != ReasonStream || !r.CacheAffinity || !r.Async.V1 {
		t.Errorf("a stream: pass %q, affinity %v, async-v1 %v", p.Reason, r.CacheAffinity, r.Async.V1)
	}
	r, p = ParseRequest(bodyOf(key, `"max_tokens":"64"`), async)
	if p.Reason != ReasonLaxType || r.CacheAffinity || r.Async.V1 {
		t.Errorf("a lax type: pass %q, affinity %v, async-v1 %v", p.Reason, r.CacheAffinity, r.Async.V1)
	}
}

// TestACacheAffinityKeyChangesNoPass: a body naming a cache_affinity_key
// passes with the reason its tags, attribution, routing inputs, key
// identifier, cohort fields, headers or schema give; the key adds none of
// its own (one off its pattern is the schema's refusal).
func TestACacheAffinityKeyChangesNoPass(t *testing.T) {
	key := `"cache_affinity_key":"` + strings.Repeat("a", 64) + `"`
	for _, c := range []struct {
		name    string
		body    []byte
		headers Headers
		reason  string
	}{
		{"tag non-ASCII", bodyOf(key, `"tags":{"x":"é"}`), jsonHeaders, ReasonTags},
		{"tag value", bodyOf(key, `"tags":{"x":1}`), jsonHeaders, ReasonRefused},
		{"tag reserved", bodyOf(key, `"tags":{"aws:x":"x"}`), jsonHeaders, ReasonRefused},
		{"referer", bodyOf(key, `"http_referer":"https://user@example.com"`), jsonHeaders, ReasonReferer},
		{"categories", bodyOf(key, `"app_categories":["BAD"]`), jsonHeaders, ReasonRefused},
		{"huge tokens", bodyOf(key, `"estimated_input_tokens":9223372036854775808`), jsonHeaders, ReasonEvaluatorInput},
		{"no key identifier", []byte(`{"model":"z-ai/glm-5.3",` + key + `}`), jsonHeaders, ReasonRefused},
		{"stream", bodyOf(key, `"stream":true`), jsonHeaders, ReasonStream},
		{"settlement header", bodyOf(key),
			Headers{ContentType: []string{"application/json"}, SettlementMode: []string{"sync"}}, ReasonSettlementAsync},
		{"before the key: lax type", bodyOf(key, `"max_tokens":"64"`), jsonHeaders, ReasonLaxType},
		{"before the key: dict key", bodyOf(key, `"tags":{" k":"v"}`), jsonHeaders, ReasonDictKeyStrip},
		{"before the key: refused field", bodyOf(key, `"region":5`), jsonHeaders, ReasonRefused},
		{"before the key: bad key", bodyOf(`"cache_affinity_key":"not-hex"`, `"tags":{"x":"é"}`), jsonHeaders, ReasonRefused},
		{"no key", bodyOf(`"tags":{"x":"é"}`), jsonHeaders, ReasonTags},
	} {
		_, p := ParseRequest(c.body, c.headers)
		if p.Reason != c.reason {
			t.Errorf("%s: pass %q, want %q", c.name, p.Reason, c.reason)
		}
	}
}

// TestTheMaterial pins §7's rules on one body: stripped strings, the
// defaults, a dropped null extra and nonce, a kept nested null, {} tags,
// the hostname default, an empty app_categories dropped, and the
// workspace and key hash over an extra of the same name.
func TestTheMaterial(t *testing.T) {
	b := bodyOf(`"x_null":null`, `"metadata":{"n":null}`, `"invocation_nonce":"n"`, `"tags":{}`,
		`"http_referer":"https://Example.COM:8443/p?q=1"`, `"app_categories":[]`, `"user":"  u  "`,
		`"key_hash":"spoof"`, `"spend_lease_admission":{"x":1}`, `"idempotency_key":" k "`)
	r, p := ParseRequest(b, jsonHeaders)
	if p.Passed() {
		t.Fatal(p.Reason)
	}
	id, p := IdentityOf(r, jsonHeaders, "ws", "kh", func() string { t.Fatal("minted"); return "" })
	if p.Passed() {
		t.Fatal(p.Reason)
	}
	want := `{"app":"example.com","estimated_input_tokens":1,"http_referer":"https://Example.COM:8443/p?q=1",` +
		`"key_hash":"kh","metadata":{"n":null},"model":"z-ai/glm-5.3","tags":{},"user":"u","workspace_id":"ws"}`
	if string(id.Material) != want {
		t.Fatalf("material\n%s\nwant\n%s", id.Material, want)
	}
	if !id.Presented || id.IdempotencyKey != "k" ||
		id.Scope != "ws#kh#8254c329a92850f6d539dd376f4816ee2764517da5e0235514af433164480d7a" {
		t.Fatalf("%+v", id)
	}
	// The body's key wins over the header's; with none, the header's;
	// with neither, the minted one.
	h := Headers{ContentType: []string{"application/json"}, IdempotencyKey: []string{" hdr "}}
	if id, _ := IdentityOf(r, h, "ws", "kh", nil); id.IdempotencyKey != "k" {
		t.Fatalf("body key lost to the header: %q", id.IdempotencyKey)
	}
	plain, _ := ParseRequest(bodyOf(), h)
	if id, _ := IdentityOf(plain, h, "ws", "kh", nil); id.IdempotencyKey != "hdr" || !id.Presented {
		t.Fatalf("header key: %+v", id)
	}
	if id, _ := IdentityOf(plain, jsonHeaders, "ws", "kh", func() string { return "m" }); id.IdempotencyKey != "m" ||
		id.Presented || !strings.HasPrefix(id.Scope, "ws#kh#") {
		t.Fatalf("minted key: %+v", id)
	}
	if r.EstimatedInputTokens != 1 || r.OutputEstimate != 512 || !r.HasTags || len(r.Tags) != 0 {
		t.Fatalf("%+v", r)
	}
}

// TestMergeTags: the key's tags in stored order, the request's after, a
// request key overwriting in place; the merged map validated whole.
func TestMergeTags(t *testing.T) {
	r, p := ParseRequest(bodyOf(`"tags":{"env":"dev","app":"cli"}`), jsonHeaders)
	if p.Passed() {
		t.Fatal(p.Reason)
	}
	got, p := MergeTags([]Tag{{"team", "core"}, {"env", "prod"}}, true, r)
	if p.Passed() || len(got) != 3 || got[0] != (Tag{"team", "core"}) || got[1] != (Tag{"env", "dev"}) ||
		got[2] != (Tag{"app", "cli"}) {
		t.Fatalf("%v %v", got, p)
	}
	many := make([]Tag, 49)
	for i := range many {
		many[i] = Tag{"k" + strconv.Itoa(i), "v"}
	}
	if _, p := MergeTags(many, true, r); p.Reason != ReasonTagsMergedInvalid {
		t.Fatalf("51 tags merged: %v", p)
	}
	if _, p := MergeTags(many[:48], true, r); p.Passed() {
		t.Fatalf("50 tags merged: %v", p)
	}
	for _, key := range [][]Tag{{{"bad!", "x"}}, {{"k", "é"}}} {
		if _, p := MergeTags(key, true, r); p.Reason != ReasonTagsMergedInvalid {
			t.Fatalf("stored %v: %v", key, p)
		}
	}
	if _, p := MergeTags(nil, false, r); p.Reason != ReasonTagsMergedInvalid {
		t.Fatalf("unusable stored tags: %v", p)
	}
}

// TestSplitURL holds the urlsplit port to what Python 3.12 answers for
// these referers (urlparse(x).scheme, urlparse(x).hostname or "").
func TestSplitURL(t *testing.T) {
	for _, c := range []struct{ url, scheme, host string }{
		{"https://Example.COM:8443/p?q=1", "https", "example.com"},
		{"HTTP://x.y", "http", "x.y"},
		{"https://user:pw@host:1/", "https", "host"},
		{"https://a@b@c/", "https", "c"},
		{"https://", "https", ""},
		{"https://:80", "https", ""},
		{"http:/x", "http", ""},
		{"//host/x", "", "host"},
		{"1http://x", "", ""},
		{"h_t://x", "", ""},
		{"https://host%ZONE/", "https", "host%ZONE"},
		{"https://h?x/y", "https", "h"},
		{"https://h#f", "https", "h"},
		{"mailto:x@y", "mailto", ""},
	} {
		scheme, host, ok := splitURL(c.url)
		if !ok || scheme != c.scheme || host != c.host {
			t.Errorf("%q: %q %q %v, want %q %q", c.url, scheme, host, ok, c.scheme, c.host)
		}
	}
	if _, _, ok := splitURL("https://[::1]/"); ok {
		t.Error("a bracketed netloc read")
	}
}

func TestCanonicalRouterModelID(t *testing.T) {
	for in, want := range map[string]string{
		"z-ai/glm-5.3":                     "z-ai/glm-5.3",
		"minimax/minimax-m3-1-flash":       "minimax/minimax-m3.1-flash",
		"system1models/x":                  "decisionmodelsio/x",
		"system1models-eu/x":               "decisionmodelsio-eu/x",
		"system1models":                    "system1models",
		"nyte/auto":                        "trustedrouter/auto",
		"nyte/auto-routing:floor":          "trustedrouter/auto:floor",
		"trustedrouter/auto-routing":       "trustedrouter/auto",
		"trustedrouter/auto-routing:nitro": "trustedrouter/auto:nitro",
		"trustedrouter/auto":               "trustedrouter/auto",
	} {
		if got := canonicalRouterModelID(in); got != want {
			t.Errorf("%q: %q, want %q", in, got, want)
		}
	}
}

// TestGoNeverAnswersARefusal is the property test over generated bodies:
// every body Go answers in the P1 corpus, changed by each edit below, is a
// body Python refuses (each edit names the schema rule or validator it
// breaks), and Go answers none of them.
func TestGoNeverAnswersARefusal(t *testing.T) {
	var file fingerprintFileT
	readJSON(t, fingerprintFile, &file)
	s := goldenSnapshot(t)
	edits := map[string]func(pyjson.Object) pyjson.Object{
		"model not a string (str)":          with("model", jsonValue("7")),
		"model empty (min_length=1)":        with("model", ""),
		"no key identifier (validator)":     without("api_key_lookup_hash", "api_key_hash"),
		"user over 256 (max_length)":        with("user", strings.Repeat("u", 257)),
		"estimate below 0 (ge=0)":           with("estimated_input_tokens", jsonValue("-1")),
		"max_tokens 0 (ge=1)":               with("max_tokens", jsonValue("0")),
		"models not a list":                 with("models", "x"),
		"provider not a dict":               with("provider", jsonValue("[]")),
		"stream not a bool":                 with("stream", "maybe"),
		"modality not a literal":            with("input_modalities", jsonValue(`["sound"]`)),
		"service_tier empty (min_length)":   with("service_tier", " "),
		"idempotency_key empty":             with("idempotency_key", " "),
		"idempotency_key over 256":          with("idempotency_key", strings.Repeat("k", 257)),
		"request_fingerprint pattern":       with("request_fingerprint", "x"),
		"invocation_nonce over 64":          with("invocation_nonce", strings.Repeat("n", 65)),
		"cost over 100,000,000":             with("additional_cost_reservation_microdollars", jsonValue("100000001")),
		"route_fallbacks below 0":           with("route_fallbacks", jsonValue("-1")),
		"tag not portable (invalid_tags)":   with("tags", jsonValue(`{"bad!":"x"}`)),
		"tag reserved (invalid_tags)":       with("tags", jsonValue(`{"trustedrouter:x":"y"}`)),
		"tag value not a string":            with("tags", jsonValue(`{"k":true}`)),
		"referer not http (attribution)":    with("http_referer", "ftp://example.com"),
		"category not kebab (attribution)":  with("app_categories", jsonValue(`["Bad Category"]`)),
		"trace 9 deep (attribution)":        with("trace", jsonValue(`{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":1}}}}}}}}`)),
		"app over 120 (max_length)":         with("app", strings.Repeat("a", 121)),
		"session_id over 256 (max_length)":  with("session_id", strings.Repeat("s", 257)),
		"affinity key pattern":              with("cache_affinity_key", "not-hex"),
		"speech characters over 60,000":     with("speech_input_characters", jsonValue("60001")),
		"inference_receipt null (not opt.)": with("inference_receipt", nil),
	}
	answered, tried := 0, 0
	for _, c := range file.Cases {
		h := headersOf(c.Headers)
		if !decide(t, s, []byte(c.Body), h, c.WorkspaceID, c.KeyHash, fingerprintKeyTags, peak).answered {
			continue
		}
		answered++
		tree, err := pyjson.Decode([]byte(c.Body))
		if err != nil {
			t.Fatal(err)
		}
		for name, edit := range edits {
			edited, err := pyjson.Dumps(edit(tree.(pyjson.Object)), pyjson.Options{})
			if err != nil {
				t.Fatal(err)
			}
			tried++
			if d := decide(t, s, edited, h, c.WorkspaceID, c.KeyHash, fingerprintKeyTags, peak); d.answered {
				t.Errorf("%s, %s: Go answers %s", c.Name, name, edited)
			}
		}
	}
	if answered < 1000 {
		t.Fatalf("only %d answered bodies to edit", answered)
	}
	t.Logf("%d edited bodies from %d answered ones: none answered", tried, answered)
}

// jsonValue is a value decoded from JSON text.
func jsonValue(text string) any {
	v, err := pyjson.Decode([]byte(text))
	if err != nil {
		panic(err)
	}
	return v
}

func with(key string, value any) func(pyjson.Object) pyjson.Object {
	return func(o pyjson.Object) pyjson.Object {
		out := make(pyjson.Object, 0, len(o)+1)
		for _, m := range o {
			if m.Key != key {
				out = append(out, m)
			}
		}
		return append(out, pyjson.Member{Key: key, Value: value})
	}
}

func without(keys ...string) func(pyjson.Object) pyjson.Object {
	return func(o pyjson.Object) pyjson.Object {
		out := make(pyjson.Object, 0, len(o))
		for _, m := range o {
			if !strings.Contains(" "+strings.Join(keys, " ")+" ", " "+m.Key+" ") {
				out = append(out, m)
			}
		}
		return out
	}
}

// TestOnlyTheEdgeUsesTheRequestSide: the request side is the edge's decide
// pipeline's (G10, behind -edge-decide-shadow). A file outside the package
// and the edge may use only the row types of rows.go, which the store (G9)
// inserts.
func TestOnlyTheEdgeUsesTheRequestSide(t *testing.T) {
	const self = "github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	rows := map[string]bool{"AuthorizationRow": true, "ReservationRow": true}
	root := filepath.Join("..", "..")
	edgeDir := filepath.Join(root, "internal", "edge")
	files, edgeUses := 0, 0
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
			return err
		}
		files++
		uses, err := gwauthUses(path, nil)
		if err != nil {
			return err
		}
		if filepath.Dir(path) == edgeDir {
			edgeUses += len(uses)
			return nil
		}
		for _, name := range uses {
			if !rows[name] {
				t.Errorf("%s uses gwauth.%s", path, name)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if files < 50 || edgeUses == 0 {
		t.Fatalf("scanned %d files, %d uses by the edge", files, edgeUses)
	}
	// Positive control: a file that uses the request side is seen, under
	// an import name of its own too.
	uses, err := gwauthUses("x.go", []byte("package x\nimport g \""+self+"\"\nvar _ = g.ParseRequest\nvar _ g.AuthorizationRow\n"))
	if err != nil || len(uses) != 2 || uses[0] != "ParseRequest" {
		t.Fatalf("%v %v", uses, err)
	}
}

// gwauthUses is every gwauth.X a Go file names (src nil: read path).
func gwauthUses(path string, src []byte) ([]string, error) {
	const self = "github.com/Lore-Hex/quill-router/fastpath/internal/gwauth"
	var source any
	if src != nil {
		source = src
	}
	parsed, err := parser.ParseFile(token.NewFileSet(), path, source, 0)
	if err != nil {
		return nil, err
	}
	var uses []string
	for _, spec := range parsed.Imports {
		if imported, _ := strconv.Unquote(spec.Path.Value); imported != self {
			continue
		}
		name := "gwauth"
		if spec.Name != nil {
			name = spec.Name.Name
		}
		ast.Inspect(parsed, func(n ast.Node) bool {
			if sel, ok := n.(*ast.SelectorExpr); ok {
				if x, ok := sel.X.(*ast.Ident); ok && x.Name == name {
					uses = append(uses, sel.Sel.Name)
				}
			}
			return true
		})
	}
	return uses, nil
}
