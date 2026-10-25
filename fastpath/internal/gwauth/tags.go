package gwauth

// request_tags.py over ASCII: validate_tags and merge_tags. Python's
// portable alphabet is Unicode letters, digits and space separators plus
// "+-=._:/@"; Go reads only its ASCII members and passes any other
// character (body.tags), so a tag map Go accepts is one Python accepts and
// one Go refuses is one Python refuses.

import "strings"

// Tag is one tag: a key and its value.
type Tag struct{ Key, Value string }

// request_tags.py's limits.
const (
	maxTags          = 50
	maxTagKeyChars   = 128
	maxTagValueChars = 256
	maxTagsBytes     = 4096
)

// tagsPortableASCII reports every key and value printable ASCII, the only
// alphabet in which Go decides validate_tags as Python does.
func tagsPortableASCII(tags []Tag) bool {
	for _, t := range tags {
		if !printableASCII(t.Key) || !printableASCII(t.Value) {
			return false
		}
	}
	return true
}

// validTags is validate_tags for printable ASCII tags with distinct keys:
// at most 50; each key 1 to 128 characters, no reserved prefix
// (case-folded), portable; each value at most 256 characters, portable; at
// most 4096 bytes as json.dumps(ensure_ascii=False, separators=(",", ":")).
func validTags(tags []Tag) bool {
	if len(tags) > maxTags {
		return false
	}
	size := 2 + max(len(tags)-1, 0) // "{}" and the commas
	for _, t := range tags {
		lower := strings.ToLower(t.Key) // casefold, over ASCII
		if len(t.Key) < 1 || len(t.Key) > maxTagKeyChars || strings.HasPrefix(lower, "aws:") ||
			strings.HasPrefix(lower, "trustedrouter:") || !portable(t.Key) ||
			len(t.Value) > maxTagValueChars || !portable(t.Value) {
			return false
		}
		// Portable ASCII needs no escape: "key":"value".
		size += len(t.Key) + len(t.Value) + 5
	}
	return size <= maxTagsBytes
}

// portable is _portable_tag_text over ASCII: letters, digits, the space
// (category Zs) and +-=._:/@.
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

// MergeTags is merge_tags(api_key.tags, body.tags) (request_tags.py,
// called at gateway.py's authorize before the body is read): the key's
// stored tags in stored order, then the request's in body order, a request
// key overwriting in place, validated as one map. keyTags are the key's
// stored tags (a stored non-object is no tags, as Python treats it);
// keyTagsUsable false is a stored object Go cannot read as string pairs.
// Any failure passes with tags.merged_invalid, which Python answers 400
// invalid_tags (or, for stored tags outside ASCII, may not: Go passes
// either way).
func MergeTags(keyTags []Tag, keyTagsUsable bool, r Request) ([]Tag, Pass) {
	if !keyTagsUsable || !tagsPortableASCII(keyTags) {
		return nil, passFor(ReasonTagsMergedInvalid)
	}
	merged := make([]Tag, 0, len(keyTags)+len(r.Tags))
	at := make(map[string]int, len(keyTags)+len(r.Tags))
	for _, t := range append(append([]Tag(nil), keyTags...), r.Tags...) {
		if i, seen := at[t.Key]; seen {
			merged[i].Value = t.Value
			continue
		}
		at[t.Key] = len(merged)
		merged = append(merged, t)
	}
	if !validTags(merged) {
		return nil, passFor(ReasonTagsMergedInvalid)
	}
	return merged, Pass{}
}
