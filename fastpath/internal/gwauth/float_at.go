package gwauth

import "github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"

// Where a body passed as body.float has its first float (Pass.FloatAt, the
// decide line's float_at in edge/decide_path.go). It is a measurement and
// nothing decides on it: the pass is body.float whichever it is. The names
// are a fixed vocabulary, so a count keyed by one never carries a value or
// a key the caller chose:
//
//   - a float inside provider is named by the member of provider that holds
//     it, one of the five preferences below, or provider.other for any other
//     member, or for a provider that is not an object;
//   - a float anywhere inside trace, metadata or tags is that field's;
//   - a float inside any other declared field (max_tokens written 64.0, a
//     float in models or in idempotency_key_source) is declared.<field>,
//     only for the fields the schema declares (floatAtDeclared);
//   - a float inside a member the schema does not declare is extra, and the
//     member's name is never recorded;
//   - a non-finite number (NaN, Infinity), which the decode refuses before
//     any float is looked for, is non_finite.
//
// The first float is the first in body order: the first member of the body
// that holds a float, and within provider the first member of provider that
// does.
const (
	FloatAtProviderMaxPrice               = "provider.max_price"
	FloatAtProviderPreferredMaxLatency    = "provider.preferred_max_latency"
	FloatAtProviderPreferredMinThroughput = "provider.preferred_min_throughput"
	FloatAtProviderSort                   = "provider.sort"
	FloatAtProviderOptions                = "provider.options"
	FloatAtProviderOther                  = "provider.other"
	FloatAtTrace                          = "trace"
	FloatAtMetadata                       = "metadata"
	FloatAtTags                           = "tags"
	FloatAtExtra                          = "extra"
	FloatAtNonFinite                      = "non_finite"
	// FloatAtDeclared is the prefix of a declared field's name.
	FloatAtDeclared = "declared."
)

// floatAtProvider names the members of provider whose floats are counted
// apart: the price and performance preferences and the two object-valued
// members a float can hide in.
var floatAtProvider = map[string]string{
	"max_price":                FloatAtProviderMaxPrice,
	"preferred_max_latency":    FloatAtProviderPreferredMaxLatency,
	"preferred_min_throughput": FloatAtProviderPreferredMinThroughput,
	"sort":                     FloatAtProviderSort,
	"options":                  FloatAtProviderOptions,
}

// floatAtDeclared is declared.<field> for every field of declared but the
// four dictionaries, which have names of their own: each name made once,
// from the schema's own list.
var floatAtDeclared = func() map[string]string {
	out := make(map[string]string, len(declared))
	for name := range declared {
		switch name {
		case "provider", "trace", "metadata", "tags":
			continue
		}
		out[name] = FloatAtDeclared + name
	}
	return out
}()

// FloatAtNames is the whole vocabulary of FloatAt, for a test to hold a
// count's keys to.
func FloatAtNames() map[string]bool {
	out := map[string]bool{FloatAtProviderOther: true, FloatAtTrace: true, FloatAtMetadata: true, FloatAtTags: true,
		FloatAtExtra: true, FloatAtNonFinite: true}
	for _, name := range floatAtProvider {
		out[name] = true
	}
	for _, name := range floatAtDeclared {
		out[name] = true
	}
	return out
}

// floatAt is where the first float of a decoded body is, "" for none.
func floatAt(object pyjson.Object) string {
	for _, m := range object {
		if !hasFloat(m.Value) {
			continue
		}
		switch m.Key {
		case "provider":
			return floatAtInProvider(m.Value)
		case "trace":
			return FloatAtTrace
		case "metadata":
			return FloatAtMetadata
		case "tags":
			return FloatAtTags
		}
		if name, ok := floatAtDeclared[m.Key]; ok {
			return name
		}
		return FloatAtExtra
	}
	return ""
}

// floatAtInProvider is where in provider, which holds a float, the first one
// is.
func floatAtInProvider(v any) string {
	o, ok := v.(pyjson.Object)
	if !ok {
		return FloatAtProviderOther
	}
	for _, m := range o {
		if !hasFloat(m.Value) {
			continue
		}
		if name, ok := floatAtProvider[m.Key]; ok {
			return name
		}
		break
	}
	return FloatAtProviderOther
}
