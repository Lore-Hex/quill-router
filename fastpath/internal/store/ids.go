package store

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"strings"
)

// A lease ID is 16 random bytes, base64url: 22 characters. An authorization
// ID is `gwa-`, its lease's ID and 16 more random bytes (design §4.9), 48
// characters, so a lookup by authorization alone finds the lease. The lease
// ID sits at a fixed place, since base64url's alphabet has `-` in it.
const (
	leaseIDLength         = 22
	authorizationPrefix   = "gwa-"
	authorizationIDLength = len(authorizationPrefix) + 2*leaseIDLength
)

// NewLeaseID mints a lease ID, which a grant's caller passes so a retried
// grant finds its lease.
func NewLeaseID() string {
	return randomID()
}

// NewAuthorizationID mints an ID for an authorization admitted under a lease.
func NewAuthorizationID(leaseID string) (string, error) {
	if !isID(leaseID) {
		return "", errors.New("store: not a lease ID")
	}
	return authorizationPrefix + leaseID + randomID(), nil
}

// LeaseOfAuthorization is the ID of the lease an authorization was admitted
// under, read from the authorization's ID.
func LeaseOfAuthorization(authorization string) (string, error) {
	if len(authorization) != authorizationIDLength || !strings.HasPrefix(authorization, authorizationPrefix) ||
		!isID(authorization[len(authorizationPrefix):len(authorizationPrefix)+leaseIDLength]) ||
		!isID(authorization[len(authorizationPrefix)+leaseIDLength:]) {
		return "", errors.New("store: not an authorization ID minted under a lease")
	}
	return authorization[len(authorizationPrefix) : len(authorizationPrefix)+leaseIDLength], nil
}

func randomID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

func isID(s string) bool {
	if len(s) != leaseIDLength {
		return false
	}
	b, err := base64.RawURLEncoding.DecodeString(s)
	return err == nil && len(b) == 16
}
