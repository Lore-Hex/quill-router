package gwauth

import (
	"reflect"
	"strings"
	"testing"
)

// TestFloatAtNamesWhereTheFirstFloatIs: every name of the vocabulary from a
// body whose first float is there, nested and in lists included, through
// ParseRequest, the parse the decide pipeline makes; each passes as
// body.float, with the zero Request, whatever its place.
func TestFloatAtNamesWhereTheFirstFloatIs(t *testing.T) {
	for _, c := range []struct {
		name, want string
		body       []byte
	}{
		{"max_price", FloatAtProviderMaxPrice, bodyOf(`"provider":{"order":["a"],"max_price":{"prompt":0.5}}`)},
		{"max latency", FloatAtProviderPreferredMaxLatency, bodyOf(`"provider":{"preferred_max_latency":1.5}`)},
		{"min throughput", FloatAtProviderPreferredMinThroughput,
			bodyOf(`"provider":{"preferred_min_throughput":{"p90":{"value":12.5}}}`)},
		{"sort object", FloatAtProviderSort, bodyOf(`"provider":{"sort":{"by":"price","weight":0.25}}`)},
		{"options list", FloatAtProviderOptions,
			bodyOf(`"provider":{"options":{"vendor":[1,{"knobs":[2,3e-1]}]}}`)},
		{"another provider member", FloatAtProviderOther, bodyOf(`"provider":{"sort":"price","only_mine":2.5}`)},
		{"provider not an object", FloatAtProviderOther, bodyOf(`"provider":[1,1.5]`)},
		{"provider a float", FloatAtProviderOther, bodyOf(`"provider":1.5`)},
		{"trace nested", FloatAtTrace, bodyOf(`"trace":{"span":{"weights":[0,1,0.5]}}`)},
		{"metadata deep", FloatAtMetadata, bodyOf(`"metadata":{"a":{"b":[{"c":[{"d":{"e":1e-3}}]}]}}`)},
		{"tags", FloatAtTags, bodyOf(`"tags":{"team":2.0}`)},
		{"extra", FloatAtExtra, bodyOf(`"customer_chosen_name":{"v":[1.5]}`)},
		{"extra exponent", FloatAtExtra, bodyOf(`"x":1E2`)},
		{"max_tokens written as a float", FloatAtDeclared + "max_tokens", bodyOf(`"max_tokens":64.0`)},
		{"estimated_input_tokens exponent", FloatAtDeclared + "estimated_input_tokens",
			bodyOf(`"estimated_input_tokens":1e3`)},
		{"a float in models", FloatAtDeclared + "models", bodyOf(`"models":["a",1.5]`)},
		{"a float in idempotency_key_source", FloatAtDeclared + "idempotency_key_source",
			bodyOf(`"idempotency_key_source":{"weight":0.5}`)},
		{"NaN", FloatAtNonFinite, bodyOf(`"max_tokens":NaN`)},
		{"Infinity deep in metadata", FloatAtNonFinite, bodyOf(`"metadata":{"a":[-Infinity]}`)},
		{"non-finite after a finite float", FloatAtNonFinite, bodyOf(`"metadata":{"a":0.5}`, `"x":NaN`)},
	} {
		r, p := ParseRequest(c.body, jsonHeaders)
		if p.Reason != ReasonFloat || p.FloatAt != c.want {
			t.Errorf("%s: %q at %q, want body.float at %q", c.name, p.Reason, p.FloatAt, c.want)
		}
		if !reflect.DeepEqual(r, Request{}) {
			t.Errorf("%s: a Request beside body.float: %+v", c.name, r)
		}
		if !FloatAtNames()[p.FloatAt] {
			t.Errorf("%s: %q is not of the vocabulary", c.name, p.FloatAt)
		}
	}
}

// TestFloatAtIsTheFirstFloat: of floats in several places the first in body
// order is named, at the body's level and within provider, and each request
// once; a float in a member that is not a float's first holder is not.
func TestFloatAtIsTheFirstFloat(t *testing.T) {
	for _, c := range []struct {
		name, want string
		body       []byte
	}{
		{"metadata first", FloatAtMetadata, bodyOf(`"metadata":{"a":1.5}`, `"provider":{"max_price":{"prompt":0.5}}`)},
		{"provider first", FloatAtProviderMaxPrice,
			bodyOf(`"provider":{"max_price":{"prompt":0.5}}`, `"metadata":{"a":1.5}`)},
		{"declared first", FloatAtDeclared + "max_tokens", bodyOf(`"max_tokens":8.0`, `"tags":{"a":1.5}`)},
		{"extra first", FloatAtExtra, bodyOf(`"zz":0.5`, `"max_tokens":8.0`)},
		// Within provider: sort is a string here, so the first member that
		// holds a float is the latency preference, not max_price after it.
		{"within provider", FloatAtProviderPreferredMaxLatency,
			bodyOf(`"provider":{"sort":"price","preferred_max_latency":2.5,"max_price":{"prompt":0.1}}`)},
		// Another member's float first: provider.other, not the named
		// preference after it.
		{"another provider member first", FloatAtProviderOther,
			bodyOf(`"provider":{"only_mine":2.5,"max_price":{"prompt":0.5}}`)},
		{"integers before", FloatAtTrace, bodyOf(`"max_tokens":8`, `"metadata":{"a":1}`, `"trace":{"t":[1,2,0.5]}`)},
	} {
		if _, p := ParseRequest(c.body, jsonHeaders); p.Reason != ReasonFloat || p.FloatAt != c.want {
			t.Errorf("%s: %q at %q, want body.float at %q", c.name, p.Reason, p.FloatAt, c.want)
		}
	}
}

// TestFloatAtIsOnlyForBodyFloat: a body with no float, or one passed for
// another reason, names no place, and the vocabulary holds no name a caller
// chose.
func TestFloatAtIsOnlyForBodyFloat(t *testing.T) {
	for _, body := range [][]byte{
		bodyOf(),
		bodyOf(`"max_tokens":"64"`),      // body.lax_type
		bodyOf(`"metadata":{"a":"0.5"}`), // a float's text in a string
		[]byte(`{"a":1.5,"a":2.5}`),      // body.duplicate_key, before any float
		bodyOf(`"x":` + strings.Repeat("[", 129) + "1.5" + strings.Repeat("]", 129)), // body.unparseable
	} {
		if _, p := ParseRequest(body, jsonHeaders); p.FloatAt != "" || p.Reason == ReasonFloat {
			t.Errorf("%s: %q at %q", body, p.Reason, p.FloatAt)
		}
	}
	for name := range FloatAtNames() {
		field, isDeclared := strings.CutPrefix(name, FloatAtDeclared)
		if isDeclared {
			if _, ok := declared[field]; !ok || field == "provider" || field == "trace" || field == "metadata" ||
				field == "tags" {
				t.Errorf("%q: not a declared field of its own", name)
			}
		}
	}
}

// TestEveryVectorLocatesItsFloat: over every P1 body (the fingerprint
// vectors and the cases), FloatAt is named exactly for the body.float
// passes, from the vocabulary, and the parse is otherwise what it was: the
// zero Request beside it, and the reason the vectors already hold to
// Python's (TestEveryFingerprintVector, TestEveryAuthorizeCase).
func TestEveryVectorLocatesItsFloat(t *testing.T) {
	var fingerprints fingerprintFileT
	readJSON(t, fingerprintFile, &fingerprints)
	var cases struct {
		Cases []struct {
			Name    string `json:"name"`
			Request struct {
				Headers [][]string `json:"headers"`
				Body    string     `json:"body"`
			} `json:"request"`
		} `json:"cases"`
	}
	readJSON(t, casesFile, &cases)
	type vector struct {
		name, body string
		headers    [][]string
	}
	var all []vector
	for _, c := range fingerprints.Cases {
		all = append(all, vector{c.Name, c.Body, c.Headers})
	}
	for _, c := range cases.Cases {
		all = append(all, vector{c.Name, c.Request.Body, c.Request.Headers})
	}
	names := FloatAtNames()
	located := map[string]int{}
	for _, v := range all {
		r, p := ParseRequest([]byte(v.body), headersOf(v.headers))
		switch {
		case (p.FloatAt != "") != (p.Reason == ReasonFloat):
			t.Errorf("%s: %q at %q", v.name, p.Reason, p.FloatAt)
		case p.FloatAt != "" && !names[p.FloatAt]:
			t.Errorf("%s: %q is not of the vocabulary", v.name, p.FloatAt)
		case p.FloatAt != "" && !reflect.DeepEqual(r, Request{}):
			t.Errorf("%s: a Request beside body.float", v.name)
		}
		if p.FloatAt != "" {
			located[p.FloatAt]++
		}
	}
	// The vectors float in many places: a locator that named one place for
	// every float would not pass.
	if len(all) < 14_000 || len(located) < 4 {
		t.Fatalf("%d vectors, floats located at %v", len(all), located)
	}
	t.Logf("%d vectors, floats located at %v", len(all), located)
}
