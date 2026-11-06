package service

import "testing"

// TestTheDefaultsAreTodaysMoneySettings: with no flag a node runs today's
// values, tier 1, the allowance 1 << 50 and no floor, and they pass the
// bounds; the node's startup script holds the same bounds, so they are
// pinned here (tests/test_fastpath_money_settings.py reads the script's).
func TestTheDefaultsAreTodaysMoneySettings(t *testing.T) {
	cfg := Defaults()
	if st := cfg.Store; st.RequiredTier != 1 || st.Allowance != 1<<50 || st.Floor != 0 {
		t.Fatalf("the defaults: tier %d, allowance %d, floor %d", st.RequiredTier, st.Allowance, st.Floor)
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
