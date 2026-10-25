package gwauth

// validate_request_attribution (request_attribution.py) for the values the
// schema already stripped: bounded strings, an http or https referer with a
// hostname, kebab-case categories, a bounded trace, and app defaulting to
// the referer's hostname. Every string here is printable ASCII
// (body.strip_ambiguous passed the rest), where urllib.parse.urlsplit is
// short enough to port exactly; the hostname default is applied only to a
// referer of the strict shape §7 names.

import (
	"regexp"
	"strings"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// request_attribution.py's bounds.
const (
	maxUserChars        = 256
	maxSessionChars     = 256
	maxAppChars         = 120
	maxRefererChars     = 2048
	maxCategories       = 2
	maxCategoryChars    = 30
	maxTraceBytes       = 8192
	maxTraceDepth       = 8
	maxTraceItems       = 256
	schemeChars         = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-."
	netlocDelimiters    = "/?#"
	strictRefererSource = `^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?([/?#][\x21-\x7e]*)?$`
)

var (
	category = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)
	// strictReferer is §7's rule: a referer of this shape has the hostname
	// Go reads (its host, lower-cased) with no urlparse subtlety left.
	strictReferer = regexp.MustCompile(strictRefererSource)
)

// attribution is RequestAttribution: nil is None.
type attribution struct {
	user, sessionID, app, referer *string
	trace                         pyjson.Object // nil when absent
	categories                    []string
}

// fields is RequestAttribution.body_fields(), in its order.
func (a attribution) fields() pyjson.Object {
	var out pyjson.Object
	add := func(key string, v *string) {
		if v != nil {
			out = append(out, pyjson.Member{Key: key, Value: *v})
		}
	}
	add("user", a.user)
	add("session_id", a.sessionID)
	if a.trace != nil {
		out = append(out, pyjson.Member{Key: "trace", Value: a.trace})
	}
	add("app", a.app)
	add("http_referer", a.referer)
	if len(a.categories) > 0 {
		categories := make([]any, len(a.categories))
		for i, c := range a.categories {
			categories[i] = c
		}
		out = append(out, pyjson.Member{Key: "app_categories", Value: categories})
	}
	return out
}

// validateAttribution is validate_request_attribution over the dumped
// (stripped) values: body.refused where Python raises InvalidAttribution,
// body.referer where Go does not derive app as Python would.
func validateAttribution(dumped map[string]any) (attribution, Pass) {
	refuse := passFor(ReasonRefused)
	var a attribution
	str := func(name string) *string {
		if s, ok := dumped[name].(string); ok {
			return &s
		}
		return nil
	}
	a.user, a.sessionID, a.app, a.referer = str("user"), str("session_id"), str("app"), str("http_referer")
	for _, bound := range []struct {
		v     *string
		limit int
	}{{a.user, maxUserChars}, {a.sessionID, maxSessionChars}, {a.app, maxAppChars}, {a.referer, maxRefererChars}} {
		if bound.v != nil && len(*bound.v) > bound.limit {
			return a, refuse
		}
	}
	var host string
	if a.referer != nil {
		scheme, hostname, ok := splitURL(*a.referer)
		if !ok {
			// A bracketed netloc: urlsplit's IPv6 checks, which Go does not
			// port, may raise.
			return a, passFor(ReasonReferer)
		}
		if (scheme != "http" && scheme != "https") || hostname == "" {
			return a, refuse
		}
		host = hostname
	}
	if items, ok := dumped["app_categories"].([]any); ok {
		if len(items) > maxCategories {
			return a, refuse
		}
		for _, item := range items {
			c := item.(string)
			if len(c) > maxCategoryChars || !category.MatchString(c) {
				return a, refuse
			}
			a.categories = append(a.categories, c)
		}
	}
	if trace, ok := dumped["trace"].(pyjson.Object); ok {
		encoded, err := pyjson.Dumps(trace, pyjson.Options{})
		if err != nil {
			return a, passFor(ReasonUnparseable)
		}
		if len(encoded) > maxTraceBytes {
			return a, refuse
		}
		count, deep := traceItems(trace, 1)
		if deep || count > maxTraceItems {
			return a, refuse
		}
		a.trace = trace
	}
	if a.app == nil && a.referer != nil {
		// app = urlparse(http_referer).hostname, only where Go is sure of it.
		if !strictReferer.MatchString(*a.referer) {
			return a, passFor(ReasonReferer)
		}
		a.app = &host
	}
	return a, Pass{}
}

// splitURL is urllib.parse.urlsplit's scheme and the result's hostname for
// printable ASCII, as Python 3.12 computes them: the scheme when the text
// before the first ':' is a letter then scheme characters, lower-cased;
// the netloc after "//" up to the first of "/?#"; the hostname the netloc
// after its last '@', before its first ':', lower-cased ("" for None). ok
// is false for a netloc with a bracket, which urlsplit checks as IPv6.
func splitURL(url string) (scheme, hostname string, ok bool) {
	url = strings.TrimLeft(url, " ") // _WHATWG_C0_CONTROL_OR_SPACE, of printable ASCII
	if i := strings.IndexByte(url, ':'); i > 0 && isASCIILetter(url[0]) && allIn(url[:i], schemeChars) {
		scheme, url = strings.ToLower(url[:i]), url[i+1:]
	}
	if !strings.HasPrefix(url, "//") {
		return scheme, "", true
	}
	netloc := url[2:]
	if i := strings.IndexAny(netloc, netlocDelimiters); i >= 0 {
		netloc = netloc[:i]
	}
	if strings.ContainsAny(netloc, "[]") {
		return scheme, "", false
	}
	if i := strings.LastIndexByte(netloc, '@'); i >= 0 {
		netloc = netloc[i+1:]
	}
	if i := strings.IndexByte(netloc, ':'); i >= 0 {
		netloc = netloc[:i]
	}
	// hostname.lower() before a '%' zone, the zone kept as written.
	before, zone, hasZone := strings.Cut(netloc, "%")
	hostname = strings.ToLower(before)
	if hasZone {
		hostname += "%" + zone
	}
	return scheme, hostname, true
}

func isASCIILetter(c byte) bool { return ('a' <= c && c <= 'z') || ('A' <= c && c <= 'Z') }

func allIn(s, set string) bool {
	for i := 0; i < len(s); i++ {
		if strings.IndexByte(set, s[i]) < 0 {
			return false
		}
	}
	return true
}

// traceItems is _trace_item_count: the keys and array elements at every
// level, and whether any value sits deeper than maxTraceDepth (the top
// object is depth 1; a scalar counts by its own depth too).
func traceItems(v any, depth int) (int, bool) {
	if depth > maxTraceDepth {
		return 0, true
	}
	var children []any
	switch v := v.(type) {
	case pyjson.Object:
		for _, m := range v {
			children = append(children, m.Value)
		}
	case []any:
		children = v
	default:
		return 0, false
	}
	count := len(children)
	for _, child := range children {
		n, deep := traceItems(child, depth+1)
		if deep {
			return 0, true
		}
		count += n
	}
	return count, false
}
