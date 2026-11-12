package store

import (
	"context"
	"slices"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
)

// tiered is a configuration that leases tiers 1 to 3, each with its own
// allowance, smaller as the tier falls (docs/design/fast-admission-
// widening.md, (b)).
func tiered(c *Config) {
	c.RequiredTier, c.Allowance, c.AllowanceTier2, c.AllowanceTier1 = 1, 1000, 100, 30
}

// rowsAt is one credit row per tier given, each with headroom 1000 and none
// latched, marked or paused.
func rowsAt(tiers ...int64) creditRows {
	n := len(tiers)
	rows := creditRows{headroom: make([]int64, n), marks: make([]bool, n), tiers: slices.Clone(tiers),
		latched: make([]bool, n), paused: make([]bool, n)}
	for i := range rows.headroom {
		rows.headroom[i] = 1000
	}
	return rows
}

// TestAllowanceAtIsTheTiersOwn: each tier's allowance is its own field;
// tier 0, and any tier outside 0 to 3, has none.
func TestAllowanceAtIsTheTiersOwn(t *testing.T) {
	cfg := testConfig()
	tiered(&cfg)
	for tier, want := range map[int64]int64{3: 1000, 2: 100, 1: 30, 0: 0, -1: 0, 4: 0} {
		if got := cfg.AllowanceAt(tier); got != want {
			t.Errorf("tier %d: allowance %d, want %d", tier, got, want)
		}
	}
}

// TestRowsRefuseByTheTiersAllowance: the rows give the workspace's tier,
// the least of its rows' with a latched row at zero, and refuse a tier
// below RequiredTier for the tier and one at or above it with no allowance
// for the allowance; read without a database, as the grant, its readiness
// and the gateway's credit state all read them.
func TestRowsRefuseByTheTiersAllowance(t *testing.T) {
	cfg := testConfig()
	tiered(&cfg)
	cfg.AllowanceTier1 = 0
	s := &Store{cfg: cfg}
	latched := rowsAt(3, 3)
	latched.latched[1] = true
	cases := []struct {
		name    string
		rows    creditRows
		tier    int64
		refused Refusal
	}{
		{"tier 3", rowsAt(3), 3, ""},
		{"tier 2", rowsAt(2), 2, ""},
		{"tier 2 by one shard behind", rowsAt(3, 2), 2, ""},
		{"tier 1, whose allowance is 0", rowsAt(1), 1, RefusedTierAllowance},
		{"tier 1 by one shard behind, whose allowance is 0", rowsAt(3, 1), 1, RefusedTierAllowance},
		{"tier 0, below the required tier", rowsAt(0), 0, RefusedTier},
		{"a latched shard is tier 0", latched, 0, RefusedTier},
	}
	for _, c := range cases {
		signed, tier, refused := s.rowsRefuse(c.rows)
		if tier != c.tier || refused != c.refused || signed.Int64() != 1000*int64(len(c.rows.tiers)) {
			t.Errorf("%s: tier %d, refused %q, signed %v; want tier %d, refused %q", c.name, tier, refused, signed,
				c.tier, c.refused)
		}
	}
	// At RequiredTier 0, tier 0 is not below it, and has no allowance.
	s.cfg.RequiredTier = 0
	if _, tier, refused := s.rowsRefuse(latched); tier != 0 || refused != RefusedTierAllowance {
		t.Errorf("latched at RequiredTier 0: tier %d, refused %q", tier, refused)
	}
	// The single allowance of before: tiers 2 and 1 have none, so only tier
	// 3 is leased whatever the required tier.
	today := &Store{cfg: testConfig()}
	today.cfg.RequiredTier = 1
	for _, tier := range []int64{2, 1} {
		if _, _, refused := today.rowsRefuse(rowsAt(tier)); refused != RefusedTierAllowance {
			t.Errorf("tier %d with no allowance below tier 3: refused %q", tier, refused)
		}
	}
	if _, _, refused := today.rowsRefuse(rowsAt(3)); refused != "" {
		t.Errorf("tier 3: refused %q", refused)
	}
}

// TestNewHoldsTheTiersAllowancesInOrder: tier 2's and tier 1's allowances
// are 0 or more, and never rise as the tier falls; read without a database,
// since New checks its configuration before any read.
func TestNewHoldsTheTiersAllowancesInOrder(t *testing.T) {
	client := &spanner.Client{}
	for name, change := range map[string]func(*Config){
		"tier 2 above tier 3":         func(c *Config) { c.Allowance, c.AllowanceTier2 = 100, 101 },
		"tier 1 above tier 2":         func(c *Config) { c.Allowance, c.AllowanceTier2, c.AllowanceTier1 = 100, 50, 51 },
		"tier 1 with tier 2 at none":  func(c *Config) { c.Allowance, c.AllowanceTier2, c.AllowanceTier1 = 100, 0, 1 },
		"a negative tier-2 allowance": func(c *Config) { c.AllowanceTier2 = -1 },
		"a negative tier-1 allowance": func(c *Config) { c.AllowanceTier1 = -1 },
	} {
		cfg := testConfig()
		change(&cfg)
		if _, err := New(client, cfg); err == nil {
			t.Errorf("%s is taken", name)
		}
	}
	for name, change := range map[string]func(*Config){
		"none below tier 3": func(*Config) {},
		"falling":           tiered,
		"tier 2 alone":      func(c *Config) { c.Allowance, c.AllowanceTier2 = 100, 50 },
		"all equal":         func(c *Config) { c.Allowance, c.AllowanceTier2, c.AllowanceTier1 = 100, 100, 100 },
	} {
		cfg := testConfig()
		change(&cfg)
		if _, err := New(client, cfg); err != nil {
			t.Errorf("%s is refused: %v", name, err)
		}
	}
}

// TestTheGrantWeighsTheWorkspacesTierAllowance: on the database, a grant to
// a tier-3, a tier-2 and a tier-1 workspace is weighed against that tier's
// allowance, each granted at it and refused one past it; a workspace one
// shard behind takes the lower tier's; a tier whose allowance is 0 is
// refused, by the grant, its readiness and the credit state alike, and the
// refused grant writes nothing; a latched workspace is tier 0, refused
// below the required tier.
func TestTheGrantWeighsTheWorkspacesTierAllowance(t *testing.T) {
	ctx := context.Background()
	s := spikeStore(t, tiered)
	seedAt := func(tiers ...int64) string {
		t.Helper()
		credits := make([]int64, len(tiers))
		for i := range credits {
			credits[i] = 2000
		}
		ws := seedWorkspace(t, credits...)
		for shard, tier := range tiers {
			setRow(t, ws, int64(shard), map[string]any{"trust_tier": tier})
		}
		return ws
	}
	for _, c := range []struct {
		name      string
		tiers     []int64
		allowance int64
	}{
		{"tier 3", []int64{3}, 1000},
		{"tier 2", []int64{2}, 100},
		{"tier 1", []int64{1}, 30},
		{"tier 2 by one shard behind", []int64{3, 2}, 100},
	} {
		ws := seedAt(c.tiers...)
		if got, err := s.Grant(ctx, grantOf(ws, c.allowance+1)); err != nil || got.Refused != RefusedAllowance {
			t.Errorf("%s, a grant past its allowance %d: %+v %v", c.name, c.allowance, got, err)
		}
		if got, err := s.Grant(ctx, grantOf(ws, c.allowance)); err != nil || got.Refused != "" {
			t.Errorf("%s, a grant of its allowance %d: %+v %v", c.name, c.allowance, got, err)
		}
		if got, err := s.Grant(ctx, grantOf(ws, 1)); err != nil || got.Refused != RefusedAllowance {
			t.Errorf("%s, a grant past its allowance's exposure: %+v %v", c.name, got, err)
		}
	}

	none := spikeStore(t, tiered, func(c *Config) { c.AllowanceTier1 = 0 })
	ws := seedAt(1)
	before := readRows(t, ws)
	if got, err := none.Grant(ctx, grantOf(ws, 10)); err != nil || got.Refused != RefusedTierAllowance {
		t.Fatalf("tier 1 with no allowance: %+v %v", got, err)
	}
	if after := readRows(t, ws); !slices.Equal(after, before) || leaseCount(t, ws) != 0 {
		t.Fatalf("the refused grant wrote rows %+v, leases %d", after, leaseCount(t, ws))
	}
	if why, err := none.GrantReadiness(ctx, ws); err != nil || why != RefusedTierAllowance {
		t.Fatalf("readiness at tier 1 with no allowance: %q %v", why, err)
	}
	if got, err := none.ReadCreditState(ctx, ws); err != nil ||
		got != (CreditState{Shards: 1, Tier: 1, Refused: RefusedTierAllowance}) {
		t.Fatalf("the credit state at tier 1 with no allowance: %+v %v", got, err)
	}
	if why, err := s.GrantReadiness(ctx, ws); err != nil || why != "" {
		t.Fatalf("readiness at tier 1 with an allowance: %q %v", why, err)
	}

	latched := seedAt(3)
	setRows(t, latched, map[string]any{"trust_latched_at": time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)})
	if got, err := s.Grant(ctx, grantOf(latched, 10)); err != nil || got.Refused != RefusedTier {
		t.Fatalf("a latched workspace, tier 0: %+v %v", got, err)
	}
}
