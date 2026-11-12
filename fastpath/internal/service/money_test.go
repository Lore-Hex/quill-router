package service

import "testing"

// TestTheDefaultsAreTodaysMoneySettings: with no flag a node runs today's
// values, tier 1, the allowance 1 << 50 and no floor, with no allowance for
// tiers 2 and 1, so only tier 3 is leased, and they pass the bounds; the
// node's startup script holds the same bounds, so they are pinned here
// (tests/test_fastpath_money_settings.py reads the script's).
func TestTheDefaultsAreTodaysMoneySettings(t *testing.T) {
	cfg := Defaults()
	if st := cfg.Store; st.RequiredTier != 1 || st.Allowance != 1<<50 || st.Floor != 0 {
		t.Fatalf("the defaults: tier %d, allowance %d, floor %d", st.RequiredTier, st.Allowance, st.Floor)
	}
	if st := cfg.Store; st.AllowanceTier2 != 0 || st.AllowanceTier1 != 0 || st.AllowanceAt(0) != 0 {
		t.Fatalf("the defaults: tier 2's allowance %d, tier 1's %d, tier 0's %d", st.AllowanceTier2, st.AllowanceTier1,
			st.AllowanceAt(0))
	}
	if err := CheckMoney(cfg); err != nil {
		t.Fatalf("the defaults: %v", err)
	}
	if cfg.Owner.TopUps.Max != 10_000_000 || MinFloor != 1_000_000 || MaxMoney != 1<<50 {
		t.Fatalf("the bounds: a top-up's most %d, the least floor %d, the most %d", cfg.Owner.TopUps.Max, MinFloor,
			MaxMoney)
	}
}

// TestCheckMoneyHoldsEachSettingToItsBounds: each bound passes at its edge
// and is refused one past it.
func TestCheckMoneyHoldsEachSettingToItsBounds(t *testing.T) {
	maxTopUp := Defaults().Owner.TopUps.Max
	cases := []struct {
		name                   string
		tier, allowance, floor int64
		ok                     bool
	}{
		{"the design's values", 3, 1_000_000_000, 20_000_000, true},
		{"tier 0", 0, 1 << 50, 0, true},
		{"tier 3", 3, 1 << 50, 0, true},
		{"tier -1", -1, 1 << 50, 0, false},
		{"tier 4", 4, 1 << 50, 0, false},
		{"the allowance at a top-up's most", 1, maxTopUp, 0, true},
		{"the allowance below a top-up's most", 1, maxTopUp - 1, 0, false},
		{"an allowance in dollars", 1, 1000, 0, false},
		{"no allowance", 1, 0, 0, false},
		{"a negative allowance", 1, -1, 0, false},
		{"the allowance at the most", 1, MaxMoney, 0, true},
		{"the allowance past the most", 1, MaxMoney + 1, 0, false},
		{"no floor", 1, 1 << 50, 0, true},
		{"the least floor", 1, 1 << 50, MinFloor, true},
		{"a floor below the least", 1, 1 << 50, MinFloor - 1, false},
		{"a floor in dollars", 1, 1 << 50, 20, false},
		{"a negative floor", 1, 1 << 50, -1, false},
		{"the floor at the most", 1, 1 << 50, MaxMoney, true},
		{"the floor past the most", 1, 1 << 50, MaxMoney + 1, false},
	}
	for _, c := range cases {
		cfg := Defaults()
		cfg.Store.RequiredTier, cfg.Store.Allowance, cfg.Store.Floor = c.tier, c.allowance, c.floor
		if err := CheckMoney(cfg); (err == nil) != c.ok {
			t.Errorf("%s: %v, want ok %v", c.name, err, c.ok)
		}
	}
}

// TestCheckMoneyHoldsTheTiersAllowances: tier 2's and tier 1's allowances
// are each 0, none, or within the tier-3 allowance's bounds, and a lower
// tier's is never above a higher one's.
func TestCheckMoneyHoldsTheTiersAllowances(t *testing.T) {
	maxTopUp := Defaults().Owner.TopUps.Max
	cases := []struct {
		name                    string
		allowance, tier2, tier1 int64
		ok                      bool
	}{
		{"none below tier 3", 1_000_000_000, 0, 0, true},
		{"tier 2 alone", 1_000_000_000, 100_000_000, 0, true},
		{"all three, falling", 1_000_000_000, 100_000_000, 25_000_000, true},
		{"all three equal", 1_000_000_000, 1_000_000_000, 1_000_000_000, true},
		{"each at a top-up's most", maxTopUp, maxTopUp, maxTopUp, true},
		{"tier 2 below a top-up's most", 1_000_000_000, maxTopUp - 1, 0, false},
		{"tier 1 below a top-up's most", 1_000_000_000, 100_000_000, maxTopUp - 1, false},
		{"tier 2 in dollars", 1_000_000_000, 100, 0, false},
		{"tier 2 negative", 1_000_000_000, -1, 0, false},
		{"tier 1 negative", 1_000_000_000, 0, -1, false},
		{"tier 2 past the most", MaxMoney, MaxMoney + 1, 0, false},
		{"tier 1 past the most", MaxMoney, MaxMoney, MaxMoney + 1, false},
		{"tier 2 above tier 3", 100_000_000, 100_000_001, 0, false},
		{"tier 1 above tier 2", 1_000_000_000, 100_000_000, 100_000_001, false},
		{"tier 1 without tier 2", 1_000_000_000, 0, 25_000_000, false},
	}
	for _, c := range cases {
		cfg := Defaults()
		cfg.Store.Allowance, cfg.Store.AllowanceTier2, cfg.Store.AllowanceTier1 = c.allowance, c.tier2, c.tier1
		if err := CheckMoney(cfg); (err == nil) != c.ok {
			t.Errorf("%s: %v, want ok %v", c.name, err, c.ok)
		}
	}
}
