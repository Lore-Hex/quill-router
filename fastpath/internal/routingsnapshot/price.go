package routingsnapshot

import "math/big"

const tokensPerMillion = 1_000_000

// Usage is what a price is applied to. Input is the uncached prompt tokens;
// PriceTierInputTokens, when positive and at most the whole prompt, chooses
// the tier instead of the whole prompt.
type Usage struct {
	InputTokens          int64
	OutputTokens         int64
	CacheReadTokens      int64
	CacheCreationTokens  int64
	PriceTierInputTokens int64
}

// tokenCost is money.token_cost_microdollars: half up per million, on
// exact integers (a rate may be as large as 2^63-1).
func tokenCost(tokens, rate int64) *big.Int {
	raw := new(big.Int).Mul(big.NewInt(tokens), big.NewInt(rate))
	if raw.Sign() <= 0 {
		return new(big.Int)
	}
	raw.Add(raw, big.NewInt(tokensPerMillion/2))
	return raw.Quo(raw, big.NewInt(tokensPerMillion))
}

// Cost is stage_d.endpoint_cost_microdollars_from_candidate: the frozen
// candidate is the only price input. Each component is rounded before the
// sum, and a positive charge is at least one microdollar. The result is
// exact; the caller decides what to do with one outside int64.
func Cost(p Price, u Usage) *big.Int {
	// The prompt the tier is chosen by, summed exactly: Python's integers
	// do not overflow, and neither may this.
	tierPrompt := new(big.Int).Add(big.NewInt(u.InputTokens), big.NewInt(u.CacheReadTokens))
	tierPrompt.Add(tierPrompt, big.NewInt(u.CacheCreationTokens))
	if basis := big.NewInt(u.PriceTierInputTokens); basis.Sign() > 0 && basis.Cmp(tierPrompt) <= 0 {
		tierPrompt = basis
	}
	rates := p.Rates
	if len(p.Tiers) > 0 {
		rates = p.Tiers[len(p.Tiers)-1].Rates
		for _, tier := range p.Tiers {
			if tier.MaxPromptTokens == nil || tierPrompt.Cmp(big.NewInt(*tier.MaxPromptTokens)) <= 0 {
				rates = tier.Rates
				break
			}
		}
	}
	cost := big.NewInt(p.RequestFeeMicro)
	cost.Add(cost, tokenCost(u.InputTokens, rates.InputMicroPerMillion))
	cost.Add(cost, tokenCost(u.OutputTokens, rates.OutputMicroPerMillion))
	cost.Add(cost, tokenCost(u.CacheReadTokens, rates.CachedInputMicroPerMillion))
	cost.Add(cost, tokenCost(u.CacheCreationTokens, rates.CacheCreationMicroPerMillion))
	positive := p.RequestFeeMicro > 0 ||
		(u.InputTokens > 0 && rates.InputMicroPerMillion > 0) ||
		(u.OutputTokens > 0 && rates.OutputMicroPerMillion > 0) ||
		(u.CacheReadTokens > 0 && rates.CachedInputMicroPerMillion > 0) ||
		(u.CacheCreationTokens > 0 && rates.CacheCreationMicroPerMillion > 0)
	if !positive {
		return new(big.Int)
	}
	if cost.Cmp(big.NewInt(1)) < 0 {
		return big.NewInt(1)
	}
	return cost
}
