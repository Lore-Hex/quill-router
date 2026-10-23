package pyjson

import (
	"fmt"
	"time"
)

// ISOFormat writes an instant as the control plane stores one:
// datetime.isoformat() of an aware UTC datetime with "+00:00" written "Z"
// (storage_gcp._iso_timestamp, and storage_models.iso_now, whose clock
// drops the microseconds). The fraction is six digits, or absent when the
// microseconds are zero. A datetime holds microseconds, so a finer
// instant is truncated to the microsecond. Python has no year outside 1
// to 9999; the caller keeps to those.
func ISOFormat(t time.Time) string {
	t = t.UTC()
	out := fmt.Sprintf("%04d-%02d-%02dT%02d:%02d:%02d",
		t.Year(), int(t.Month()), t.Day(), t.Hour(), t.Minute(), t.Second())
	if micro := t.Nanosecond() / 1000; micro != 0 {
		out += fmt.Sprintf(".%06d", micro)
	}
	return out + "Z"
}
