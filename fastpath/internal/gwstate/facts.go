// Package gwstate holds what the edge knows of an API key and its workspace
// when it decides an authorize itself (E2, docs/design/fast-admission-e2-*):
// facts read from production's stored JSON, and whether they keep the key in
// the cohort Go may answer for.
//
// The facts mirror what Python's authorize reads from the same bodies
// (storage_models.ApiKey and Workspace, decoded by decode_auth_record), and
// their verdicts are held to Python's own predicates by
// fastpath/testdata/gateway_key_vectors_v1.json
// (scripts/fastpath/gateway_key_vectors.py). Where Python's reading turns on
// a body Go does not read the way Python does (a field of another JSON type
// than production writes, a timestamp in an ISO form production never
// writes, a tag outside ASCII), the facts say so and are not eligible: the
// edge passes the request to Python, which decides. They never admit a key
// Python would not.
//
// Nothing imports the package yet: it is dark until the edge's decide path.
package gwstate

import (
	"bytes"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/Lore-Hex/quill-router/fastpath/internal/store"
)

// The reasons a key is not in the cohort, in the order the facts check them.
// The order is Python's authorize order where Python refuses (401 disabled or
// expired, 403 scope, 400 tags) and the pilot's for the rest.
const (
	// ReasonKeyShape prefixes a body Go does not read the way Python does:
	// "key_shape:<field>".
	ReasonKeyShape    = "key_shape"
	ReasonKeyDisabled = "key_disabled"
	// ReasonKeyExpiresAt is an expires_at Go does not parse (Python may
	// read it as a time, or count it expired).
	ReasonKeyExpiresAt = "key_expires_at"
	ReasonKeyExpired   = "key_expired"
	ReasonKeyScope     = "key_scope"
	ReasonKeyFederated = "key_federated"
	ReasonKeyTags      = "key_tags"
	// ReasonKeyTagsNonASCII is a tag outside ASCII, whose validity turns
	// on Unicode categories Go does not reproduce.
	ReasonKeyTagsNonASCII  = "key_tags_non_ascii"
	ReasonKeyApp           = "key_app"
	ReasonKeyLimit         = "key_limit"
	ReasonKeyWindowLimit   = "key_window_limit"
	ReasonKeyBudgetStrict  = "key_budget_strict"
	ReasonWorkspaceShape   = "workspace_shape"
	ReasonWorkspaceGone    = "workspace_unavailable"
	ReasonWorkspacePaused  = "workspace_paused"
	ReasonWorkspaceFederal = "workspace_federated"
)

// Pair is one of a key's default tags.
type Pair struct{ Key, Value string }

// KeyFacts is an API key as the edge's cohort gate reads it.
type KeyFacts struct {
	// Eligible is whether every fact but expiry keeps the key in the
	// cohort; Reason is the first that does not ("" when Eligible). At
	// adds expiry, which depends on the time. It is necessary, not
	// sufficient: what turns on the request or the edge policy rather than
	// the key's body is gated where those are known (G10): the monitor key
	// (the policy's monitor_lookup_hash), a request that names its key by
	// api_key_hash, _require_monitor_model_key, and the boot binding of
	// LookupHash.
	Eligible bool
	Reason   string
	// Hash is the key's hash (the authorization's key_hash), LookupHash the
	// lookup hash its body records (what a boot signature binds), and
	// WorkspaceID the workspace the key spends from.
	Hash, LookupHash, WorkspaceID string
	// ExpiresAt is when the key stops working, zero for never.
	ExpiresAt time.Time
	// Tags are the key's default tags in the order its body lists them, as
	// Python's merge keeps them (a repeated key keeps its first place and
	// its last value).
	Tags []Pair
}

// At is whether the key is in the cohort at now, and the first reason it is
// not. An expired key is refused before any reason but a body Go cannot
// read, and being disabled, as Python checks them together first.
func (k KeyFacts) At(now time.Time) (bool, string) {
	if !k.Eligible && (strings.HasPrefix(k.Reason, ReasonKeyShape) || k.Reason == ReasonKeyDisabled ||
		k.Reason == ReasonKeyExpiresAt) {
		return false, k.Reason
	}
	// auth.is_api_key_expired: expired once the instant is not after now.
	if !k.ExpiresAt.IsZero() && !k.ExpiresAt.After(now) {
		return false, ReasonKeyExpired
	}
	return k.Eligible, k.Reason
}

// apiKeyRequired are the fields ApiKey has no default for: a body without
// one is a TypeError in Python, not a key.
var apiKeyRequired = []string{"hash", "salt", "secret_hash", "lookup_hash", "name", "label", "workspace_id",
	"creator_user_id"}

// ParseKey reads a stored api_key body.
func ParseKey(body string) KeyFacts {
	f, ok := fieldsOf(body)
	if !ok {
		return keyShape("body")
	}
	for _, name := range apiKeyRequired {
		if _, present := f[name]; !present {
			return keyShape(name)
		}
	}
	var k KeyFacts
	for _, id := range []struct {
		name string
		into *string
	}{{"hash", &k.Hash}, {"lookup_hash", &k.LookupHash}, {"workspace_id", &k.WorkspaceID}} {
		value, ok := f.str(id.name)
		if !ok || !printableASCII(value) {
			return keyShape(id.name)
		}
		*id.into = value
	}
	disabled, ok := f.boolean("disabled")
	if !ok {
		return keyShape("disabled")
	}
	scopes, ok := f.strings("scopes")
	if !ok {
		return keyShape("scopes")
	}
	federated, ok := f.str("federated_home")
	if !ok {
		return keyShape("federated_home")
	}
	app, ok := f.str("app_id")
	if !ok {
		return keyShape("app_id")
	}
	strict, ok := f.boolean("budget_strict")
	if !ok {
		return keyShape("budget_strict")
	}
	// storage_gcp_counters.key_usage_shard_count, which the route calls on
	// every authorize: absent is 1, and what it cannot read as 1 to 64 is a
	// raise (500). Go reads a JSON integer 1 to 64 and passes the rest.
	if raw, present := f["usage_shard_count"]; present {
		n, err := strconv.ParseInt(string(bytes.TrimSpace(raw)), 10, 64)
		if err != nil || n < 1 || n > maxKeyUsageShards {
			return keyShape("usage_shard_count")
		}
	}
	expires, readable := "", true
	if raw, present := f["expires_at"]; present && !isNull(raw) {
		if err := json.Unmarshal(raw, &expires); err != nil {
			return keyShape("expires_at")
		}
		if expires != "" {
			k.ExpiresAt, readable = parseISO(expires)
		}
	}
	tags, tagReason := keyTags(f["tags"])
	switch {
	case disabled:
		k.Reason = ReasonKeyDisabled
	case !readable:
		k.Reason = ReasonKeyExpiresAt
	case len(scopes) > 0 && !slices.Contains(scopes, "inference"):
		k.Reason = ReasonKeyScope
	case federated != "":
		k.Reason = ReasonKeyFederated
	case tagReason != "":
		k.Reason = tagReason
	case app != "":
		k.Reason = ReasonKeyApp
	case f.set("limit_microdollars"):
		k.Reason = ReasonKeyLimit
	case f.set("limit_daily_microdollars") || f.set("limit_weekly_microdollars") || f.set("limit_monthly_microdollars"):
		// spend_windows.key_window_limits: configured windows, alert-only
		// or not, put a key outside the pilot.
		k.Reason = ReasonKeyWindowLimit
	case strict:
		k.Reason = ReasonKeyBudgetStrict
	default:
		k.Eligible, k.Tags = true, tags
	}
	return k
}

func keyShape(field string) KeyFacts {
	return KeyFacts{Reason: ReasonKeyShape + ":" + field}
}

// maxKeyUsageShards is storage_gcp_counters.MAX_KEY_USAGE_SHARDS.
const maxKeyUsageShards = 64

// consultedKeyFields and consultedWorkspaceFields are every ApiKey and
// Workspace field the authorize route's checks consult on the leased path
// (the vector file's consulted_fields, which the generator records while it
// runs them): each is read here, or needs no reading, as each says. A field
// the route starts to consult fails the vector test until it is listed.
var consultedKeyFields = map[string]string{
	"app_id":                     "key_app",
	"budget_alert_only":          "not read: any configured window is key_window_limit, alert-only or not",
	"budget_strict":              "key_budget_strict",
	"disabled":                   "key_disabled",
	"expires_at":                 "ExpiresAt, key_expired",
	"federated_home":             "key_federated",
	"hash":                       "Hash",
	"include_byok_in_limit":      "not read: truthiness never raises, and only a BYOK request, never Go's, uses it",
	"limit_daily_microdollars":   "key_window_limit",
	"limit_microdollars":         "key_limit",
	"limit_monthly_microdollars": "key_window_limit",
	"limit_weekly_microdollars":  "key_window_limit",
	"lookup_hash":                "LookupHash",
	"scopes":                     "key_scope",
	"tags":                       "Tags, key_tags",
	"usage_shard_count":          "key_shape:usage_shard_count unless an integer 1 to 64",
	"workspace_id":               "WorkspaceID",
}

var consultedWorkspaceFields = map[string]string{
	"billing_pause_causes": "PauseCauses, workspace_paused",
	"billing_paused":       "PauseCauses (the legacy flag)",
	"deleted":              "Deleted, workspace_unavailable",
	"federated_home":       "Federated, workspace_federated",
	"id":                   "must be the key's workspace_id (workspace_shape:id_mismatch)",
}

// Tag rules, request_tags.py's.
const (
	maxTags          = 50
	maxTagKeyChars   = 128
	maxTagValueChars = 256
	maxTagsBytes     = 4096
)

// keyTags reads a key's default tags as merge_tags(api_key.tags, None)
// does: anything but an object is no tags (a stored non-object can only be
// corruption, and Python treats it as absent), and an object is validated.
func keyTags(raw json.RawMessage) ([]Pair, string) {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || raw[0] != '{' {
		return nil, ""
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if _, err := dec.Token(); err != nil {
		return nil, ReasonKeyTags
	}
	var pairs []Pair
	at := map[string]int{}
	invalid := false
	for dec.More() {
		token, err := dec.Token()
		if err != nil {
			return nil, ReasonKeyTags
		}
		key, _ := token.(string)
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, ReasonKeyTags
		}
		var text string
		if err := json.Unmarshal(value, &text); err != nil || isNull(value) {
			// A value that is not a string is invalid; keep reading, so a
			// tag outside ASCII elsewhere is still seen.
			invalid = true
			continue
		}
		if i, seen := at[key]; seen {
			pairs[i].Value = text
			continue
		}
		at[key] = len(pairs)
		pairs = append(pairs, Pair{key, text})
	}
	for _, p := range pairs {
		if !ascii(p.Key) || !ascii(p.Value) {
			return nil, ReasonKeyTagsNonASCII
		}
	}
	if invalid || len(pairs) > maxTags {
		return nil, ReasonKeyTags
	}
	size := 2 + max(len(pairs)-1, 0)
	for _, p := range pairs {
		lower := strings.ToLower(p.Key)
		if len(p.Key) < 1 || len(p.Key) > maxTagKeyChars || strings.HasPrefix(lower, "aws:") ||
			strings.HasPrefix(lower, "trustedrouter:") || !portable(p.Key) ||
			len(p.Value) > maxTagValueChars || !portable(p.Value) {
			return nil, ReasonKeyTags
		}
		// json.dumps(ensure_ascii=False, separators=(",", ":")) of portable
		// ASCII escapes nothing: "key":"value".
		size += len(p.Key) + len(p.Value) + 5
	}
	if size > maxTagsBytes {
		return nil, ReasonKeyTags
	}
	return pairs, ""
}

// portable is request_tags._portable_tag_text over ASCII: letters, digits,
// the space (category Zs) and +-=._:/@.
func portable(s string) bool {
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case 'a' <= c && c <= 'z', 'A' <= c && c <= 'Z', '0' <= c && c <= '9', c == ' ':
		case strings.IndexByte("+-=._:/@", c) >= 0:
		default:
			return false
		}
	}
	return true
}

// WorkspaceFacts is a key's workspace as the edge's cohort gate reads it.
type WorkspaceFacts struct {
	Eligible bool
	Reason   string
	// Deleted is a workspace row marked deleted (Python reads it as none).
	Deleted bool
	// PauseCauses are the document's causes as Workspace.__post_init__
	// leaves them: the legacy billing_paused flag with no causes is
	// "migration"; sorted, without repeats.
	PauseCauses []string
	// Federated is a shadow of a workspace on its home plane.
	Federated bool
}

var workspaceRequired = []string{"id", "name", "owner_user_id"}

// ParseWorkspace reads a stored workspace body for a key whose workspace_id
// is keyWorkspace; found false is no row. Python bills the body's id and Go
// the key's workspace_id, so a body whose id is not the key's (a repeated
// "id" counts as Python reads it, the last) is not Go's to answer.
func ParseWorkspace(body string, found bool, keyWorkspace string) WorkspaceFacts {
	if !found {
		return WorkspaceFacts{Reason: ReasonWorkspaceGone}
	}
	f, ok := fieldsOf(body)
	if !ok {
		return workspaceShape("body")
	}
	for _, name := range workspaceRequired {
		if _, present := f[name]; !present {
			return workspaceShape(name)
		}
	}
	if id, ok := f.str("id"); !ok || id == "" || id != keyWorkspace {
		return workspaceShape("id_mismatch")
	}
	var w WorkspaceFacts
	if w.Deleted, ok = f.boolean("deleted"); !ok {
		return workspaceShape("deleted")
	}
	paused, ok := f.boolean("billing_paused")
	if !ok {
		return workspaceShape("billing_paused")
	}
	causes, ok := f.strings("billing_pause_causes")
	if !ok {
		return workspaceShape("billing_pause_causes")
	}
	federated, ok := f.str("federated_home")
	if !ok {
		return workspaceShape("federated_home")
	}
	if paused && len(causes) == 0 {
		causes = []string{"migration"}
	}
	slices.Sort(causes)
	w.PauseCauses = slices.Compact(causes)
	w.Federated = federated != ""
	switch {
	case w.Deleted:
		w.Reason = ReasonWorkspaceGone
	case len(w.PauseCauses) > 0:
		w.Reason = ReasonWorkspacePaused
	case w.Federated:
		w.Reason = ReasonWorkspaceFederal
	default:
		w.Eligible = true
	}
	return w
}

func workspaceShape(field string) WorkspaceFacts {
	return WorkspaceFacts{Reason: ReasonWorkspaceShape + ":" + field}
}

// Facts are what one key-context read tells the edge: the key, its
// workspace, the providers the workspace holds BYOK credentials for, and
// whether it has a broadcast destination.
type Facts struct {
	Key       KeyFacts
	Workspace WorkspaceFacts
	// BYOKProviders are the slugs of the workspace's BYOK entities, matched
	// on the complete id "workspace#slug" as Python matches them: an
	// imported workspace "ws#x" shares ws's id range, and its entities are
	// not ws's.
	BYOKProviders        []string
	BroadcastDestination bool
}

// FromContext reads the facts of a key context.
func FromContext(c store.KeyContext) Facts {
	key := ParseKey(c.KeyBody)
	out := Facts{Key: key, Workspace: ParseWorkspace(c.WorkspaceBody, c.WorkspaceFound, key.WorkspaceID),
		BroadcastDestination: c.BroadcastDestination}
	// The query joined the workspace and took the BYOK and broadcast ranges
	// by Spanner's JSON_VALUE of the key's workspace_id; Go's reading of the
	// body must be the same id (a repeated field could be read otherwise),
	// or none of them is the key's workspace.
	if c.RangeWorkspaceID != key.WorkspaceID {
		out.Workspace = workspaceShape("id_mismatch")
		return out
	}
	if ws := out.Key.WorkspaceID; ws != "" {
		for _, id := range c.BYOKIDs {
			slug, ok := strings.CutPrefix(id, ws+"#")
			if ok && slug != "" && !strings.Contains(slug, "#") {
				out.BYOKProviders = append(out.BYOKProviders, slug)
			}
		}
	}
	return out
}

// fields are a body's top-level fields, undecoded; a repeated field keeps
// its last value, as Python's json.loads does.
type fields map[string]json.RawMessage

func fieldsOf(body string) (fields, bool) {
	var f fields
	if err := json.Unmarshal([]byte(body), &f); err != nil || f == nil {
		return nil, false
	}
	return f, true
}

func isNull(raw json.RawMessage) bool {
	return string(bytes.TrimSpace(raw)) == "null"
}

// set is a field present and not null.
func (f fields) set(name string) bool {
	raw, present := f[name]
	return present && !isNull(raw)
}

// str is a string field, "" when absent; ok is false for another type.
func (f fields) str(name string) (string, bool) {
	raw, present := f[name]
	if !present {
		return "", true
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil || isNull(raw) {
		return "", false
	}
	return s, true
}

// boolean is a bool field, false when absent; ok is false for another type.
func (f fields) boolean(name string) (bool, bool) {
	raw, present := f[name]
	if !present {
		return false, true
	}
	var b bool
	if err := json.Unmarshal(raw, &b); err != nil || isNull(raw) {
		return false, false
	}
	return b, true
}

// strings is a list of strings, empty when absent; ok is false for another
// type, null among them.
func (f fields) strings(name string) ([]string, bool) {
	raw, present := f[name]
	if !present {
		return nil, true
	}
	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil || isNull(raw) {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		var s string
		if err := json.Unmarshal(item, &s); err != nil || isNull(item) {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

func ascii(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

func printableASCII(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 0x21 || s[i] > 0x7e {
			return false
		}
	}
	return true
}

// parseISO reads the ISO 8601 forms production writes, as Python's
// datetime.fromisoformat reads them after auth.is_api_key_expired turns "Z"
// into "+00:00": YYYY-MM-DD, optionally followed by "T" or " ", HH:MM,
// optionally :SS and a fraction of 1 to 9 digits (truncated to the
// microsecond), then optionally "Z" or ±HH:MM; no offset is UTC. Any other
// text is not read (false), whether Python reads it or counts it expired.
func parseISO(s string) (time.Time, bool) {
	num := func(t string) (int, bool) {
		for i := 0; i < len(t); i++ {
			if t[i] < '0' || t[i] > '9' {
				return 0, false
			}
		}
		n, err := strconv.Atoi(t)
		return n, err == nil && t != ""
	}
	if len(s) < 10 || s[4] != '-' || s[7] != '-' {
		return time.Time{}, false
	}
	year, ok1 := num(s[0:4])
	month, ok2 := num(s[5:7])
	day, ok3 := num(s[8:10])
	if !ok1 || !ok2 || !ok3 || year < 1 || month < 1 || month > 12 || day < 1 ||
		day > daysIn(year, time.Month(month)) {
		return time.Time{}, false
	}
	rest := s[10:]
	var hour, minute, second, micro int
	offset := 0
	if rest != "" {
		if rest[0] != 'T' && rest[0] != ' ' {
			return time.Time{}, false
		}
		rest = rest[1:]
		if len(rest) < 5 || rest[2] != ':' {
			return time.Time{}, false
		}
		var okH, okM bool
		hour, okH = num(rest[0:2])
		minute, okM = num(rest[3:5])
		if !okH || !okM || hour > 23 || minute > 59 {
			return time.Time{}, false
		}
		rest = rest[5:]
		if strings.HasPrefix(rest, ":") {
			if len(rest) < 3 {
				return time.Time{}, false
			}
			var okS bool
			if second, okS = num(rest[1:3]); !okS || second > 59 {
				return time.Time{}, false
			}
			rest = rest[3:]
			if strings.HasPrefix(rest, ".") {
				end := 1
				for end < len(rest) && rest[end] >= '0' && rest[end] <= '9' {
					end++
				}
				digits := rest[1:end]
				if len(digits) < 1 || len(digits) > 9 {
					return time.Time{}, false
				}
				digits = (digits + "000000")[:6]
				micro, _ = num(digits)
				rest = rest[end:]
			}
		}
		switch {
		case rest == "":
		case rest == "Z":
		case len(rest) == 6 && (rest[0] == '+' || rest[0] == '-') && rest[3] == ':':
			oh, okH := num(rest[1:3])
			om, okM := num(rest[4:6])
			if !okH || !okM || oh > 23 || om > 59 {
				return time.Time{}, false
			}
			offset = oh*3600 + om*60
			if rest[0] == '-' {
				offset = -offset
			}
		default:
			return time.Time{}, false
		}
	}
	zone := time.UTC
	if offset != 0 {
		zone = time.FixedZone(fmt.Sprintf("%+d", offset), offset)
	}
	return time.Date(year, time.Month(month), day, hour, minute, second, micro*1000, zone).UTC(), true
}

func daysIn(year int, month time.Month) int {
	return time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
}
