package main

import (
	"flag"
	"io"
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
	return cfg, service.CheckMoney(cfg)
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
		{"-allowance", "1e9"}, {"-allowance", "9223372036854775808"},
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
