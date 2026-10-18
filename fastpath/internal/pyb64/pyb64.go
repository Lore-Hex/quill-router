// Package pyb64 decodes base64url values as the control plane does
// (src/trusted_router/receipt_keys.py, b64url_decode), so the edge and the
// store accept and refuse exactly what Python does: no empty or
// whitespace-bearing value, padding added to a multiple of four, the URL
// alphabet or the standard one, nothing outside either, and unused bits
// in the last character as they come (Python's decoder does not check
// them).
package pyb64

import (
	"encoding/base64"
	"errors"
	"strings"
	"unicode"
)

// Decode decodes a base64url value by Python's rules.
func Decode(value string) ([]byte, error) {
	if value == "" || strings.IndexFunc(value, unicode.IsSpace) >= 0 {
		return nil, errors.New("pyb64: an empty or whitespace-bearing value")
	}
	padded := value + strings.Repeat("=", (4-len(value)%4)%4)
	standard := strings.NewReplacer("-", "+", "_", "/").Replace(padded)
	out, err := base64.StdEncoding.DecodeString(standard)
	if err != nil {
		return nil, errors.New("pyb64: not a base64url value")
	}
	return out, nil
}
