package pyb64

import (
	"bytes"
	"testing"
)

// The cases are b64url_decode's (receipt_keys.py): computed with it.
func TestDecodeIsPythons(t *testing.T) {
	ok := []struct {
		in   string
		want []byte
	}{
		{"AA", []byte{0}},
		{"AA==", []byte{0}},
		{"-_8", []byte{0xfb, 0xff}},
		{"+/8", []byte{0xfb, 0xff}},
		{"-_8=", []byte{0xfb, 0xff}},
		{"AB", []byte{0}}, // unused bits set: Python decodes it
	}
	for _, c := range ok {
		got, err := Decode(c.in)
		if err != nil || !bytes.Equal(got, c.want) {
			t.Fatalf("%q: %x %v, want %x", c.in, got, err, c.want)
		}
	}
	for _, in := range []string{"", " AA", "AA\n", "AA===", "A", "AA=A", "A!A", "AA\t"} {
		if _, err := Decode(in); err == nil {
			t.Fatalf("%q decoded", in)
		}
	}
}
