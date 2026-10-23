package gwauth

// The idempotency key, scope and fingerprint
// (docs/design/fast-admission-e2-decide.md, §7): the material is
// _gateway_authorize_body's dict as prepare_fingerprint hands it to
// _gateway_authorize_fingerprint (gateway.py), encoded as
// json.dumps(sort_keys=True, separators=(",", ":")) and hashed with
// sha256. The reservation's idempotency_scope is
// f"{workspace_id}#{key_hash}#{sha256(idempotency_key)}"
// (storage_gcp_keys.py, _gateway_authorization_idempotency_index_id).

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/Lore-Hex/quill-router/fastpath/internal/pyjson"
)

// Identity is the request's idempotency key, scope and fingerprint.
type Identity struct {
	IdempotencyKey string // presented (stripped) or minted
	Presented      bool
	Material       []byte // the bytes hashed
	Fingerprint    string
	Scope          string
}

// materialDropped are the keys _gateway_authorize_fingerprint leaves out
// of the material, an extra of the same name included.
var materialDropped = map[string]bool{"api_key_hash": true, "api_key_lookup_hash": true, "idempotency_key": true,
	"idempotency_key_source": true, "spend_lease_admission": true}

// attributionKeys are the keys _gateway_authorize_body replaces with the
// validated attribution.
var attributionKeys = map[string]bool{"user": true, "session_id": true, "trace": true, "app": true,
	"http_referer": true, "app_categories": true}

// IdentityOf is the request's identity under the key's workspace and hash.
// The key is the body's idempotency_key, else the one Idempotency-Key
// header, both stripped; none presented, it is mint() (Python's
// str(uuid4())). r must be a ParseRequest result without a Pass, which
// already passed every request whose key, route type or fields would make
// Python's material differ from this one.
func IdentityOf(r Request, h Headers, workspaceID, keyHash string, mint func() string) (Identity, Pass) {
	if p := r.idempotencyHeaders(h); p.Passed() {
		return Identity{}, p
	}
	var id Identity
	switch {
	case r.BodyIdempotencyKey != nil:
		id.IdempotencyKey, id.Presented = *r.BodyIdempotencyKey, true
	case headerKey(h) != nil:
		id.IdempotencyKey, id.Presented = *headerKey(h), true
	default:
		id.IdempotencyKey = mint()
	}
	material := r.material(workspaceID, keyHash)
	encoded, err := pyjson.Canonical(material)
	if err != nil {
		return Identity{}, passFor(ReasonUnparseable)
	}
	id.Material = encoded
	sum := sha256.Sum256(encoded)
	id.Fingerprint = hex.EncodeToString(sum[:])
	keySum := sha256.Sum256([]byte(id.IdempotencyKey))
	id.Scope = fmt.Sprintf("%s#%s#%s", workspaceID, keyHash, hex.EncodeToString(keySum[:]))
	return id, Pass{}
}

// material is §7 steps 2 to 5 over the dump.
func (r Request) material(workspaceID, keyHash string) pyjson.Object {
	out := make(pyjson.Object, 0, len(r.Dump)+8)
	for _, m := range r.Dump {
		switch {
		case m.Key == "invocation_nonce", materialDropped[m.Key], attributionKeys[m.Key]:
			continue
		case m.Key == "additional_cost_reservation_microdollars" && !r.additionalCost:
			continue
		case m.Key == "inference_receipt" && !r.inferenceReceipt:
			continue
		case m.Key == "tags":
			// validate_tags(body.tags): a detached map of the same pairs,
			// {} kept for an empty object.
			tags := make(pyjson.Object, len(r.Tags))
			for i, t := range r.Tags {
				tags[i] = pyjson.Member{Key: t.Key, Value: t.Value}
			}
			out = append(out, pyjson.Member{Key: "tags", Value: tags})
			continue
		}
		out = append(out, m)
	}
	out = append(out, r.attribution.fields()...)
	return set(set(out, "workspace_id", workspaceID), "key_hash", keyHash)
}

// set is material[key] = value: in place over an extra of that name.
func set(o pyjson.Object, key string, value any) pyjson.Object {
	for i := range o {
		if o[i].Key == key {
			o[i].Value = value
			return o
		}
	}
	return append(o, pyjson.Member{Key: key, Value: value})
}
