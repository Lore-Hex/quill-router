package main

import (
	"flag"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/service"
)

// parseMoney parses args as the node parses its money flags, from the
// defaults, and holds them to their bounds.
func parseMoney(args ...string) (service.Config, error) {
	cfg := service.Defaults()
	fs := flag.NewFlagSet("fastpath", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	moneyFlags(fs, &cfg.Store)
	if err := fs.Parse(args); err != nil {
		return cfg, err
	}
	return cfg, parsed(fs.Args(), cfg)
}

// runWith runs the node with args on a fresh command line, its usage
// discarded, as main would.
func runWith(t *testing.T, args ...string) error {
	t.Helper()
	oldArgs, oldLine := os.Args, flag.CommandLine
	t.Cleanup(func() { os.Args, flag.CommandLine = oldArgs, oldLine })
	flag.CommandLine = flag.NewFlagSet("fastpath", flag.ContinueOnError)
	flag.CommandLine.SetOutput(io.Discard)
	os.Args = append([]string{"fastpath"}, args...)
	return run()
}

// TestTheNodeRefusesItsMoneyFlagsOutOfBoundsOrDropped: the node itself, not
// only the flag set, refuses a money setting out of bounds and an argument
// left after the flags, before it reads anything, since the parser stops
// at that argument and the money flags after it would not apply.
func TestTheNodeRefusesItsMoneyFlagsOutOfBoundsOrDropped(t *testing.T) {
	for _, c := range []struct {
		args []string
		want string
	}{
		{[]string{"-floor", "20"}, "the floor is"},
		{[]string{"-allowance", "1000"}, "the allowance is"},
		{[]string{"-roles", "admission", "stray", "-required-tier", "3"}, "after the flags"},
	} {
		if err := runWith(t, c.args...); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%q: %v, want an error naming %q", c.args, err, c.want)
		}
	}
}

// TestTheMoneyFlagsDefaultToTodaysValues: a node given none runs tier 1,
// the allowance 1 << 50 and no floor, as before the flags.
func TestTheMoneyFlagsDefaultToTodaysValues(t *testing.T) {
	cfg, err := parseMoney()
	if err != nil {
		t.Fatal(err)
	}
	if st := cfg.Store; st.RequiredTier != 1 || st.Allowance != 1<<50 || st.Floor != 0 {
		t.Fatalf("tier %d, allowance %d, floor %d", st.RequiredTier, st.Allowance, st.Floor)
	}
}

// TestTheMoneyFlagsSetTheStore: the design's values reach the store's
// configuration as given.
func TestTheMoneyFlagsSetTheStore(t *testing.T) {
	cfg, err := parseMoney("-required-tier", "3", "-allowance", "1000000000", "-floor", "20000000")
	if err != nil {
		t.Fatal(err)
	}
	if st := cfg.Store; st.RequiredTier != 3 || st.Allowance != 1_000_000_000 || st.Floor != 20_000_000 {
		t.Fatalf("tier %d, allowance %d, floor %d", st.RequiredTier, st.Allowance, st.Floor)
	}
}

// TestTheMoneyFlagsAreRefusedOutOfBounds: a value that is not an integer,
// or one past its bound, is refused before the node starts anything.
func TestTheMoneyFlagsAreRefusedOutOfBounds(t *testing.T) {
	for _, args := range [][]string{
		{"-required-tier", "4"}, {"-required-tier", "-1"}, {"-required-tier", "three"},
		{"-allowance", "0"}, {"-allowance", "1000"}, {"-allowance", "9999999"}, {"-allowance", "1125899906842625"},
		{"-allowance", "1e9"}, {"-allowance", "9223372036854775808"}, {"-allowance", "0x3b9aca00"},
		{"-allowance", "1_000_000_000"}, {"-allowance", "+1000000000"}, {"-floor", "020000000"}, {"-required-tier", "03"},
		{"-floor", "-1"}, {"-floor", "20"}, {"-floor", "999999"}, {"-floor", "1125899906842625"}, {"-floor", "$20"},
	} {
		if _, err := parseMoney(args...); err == nil {
			t.Errorf("%q was taken", args)
		}
	}
	for _, args := range [][]string{
		{"-required-tier", "0"}, {"-allowance", "10000000"}, {"-allowance", "1125899906842624"}, {"-floor", "0"},
		{"-floor", "1000000"}, {"-floor", "1125899906842624"},
	} {
		if _, err := parseMoney(args...); err != nil {
			t.Errorf("%q: %v", args, err)
		}
	}
}
