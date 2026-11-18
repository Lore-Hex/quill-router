package edge

import (
	"strings"
	"testing"

	"github.com/Lore-Hex/quill-router/fastpath/internal/routingsnapshot"
)

// TestEveryMaxPricePassHasItsOwnReason: each of the evaluator's
// provider.max_price passes is counted under a reason of its own, of the
// evaluate.max_price family, and the shadow logs the evaluator's sentence
// as it is (it names a field of the schema, never the body's content), where
// an unknown provider field's sentence becomes provider.<field>.
func TestEveryMaxPricePassHasItsOwnReason(t *testing.T) {
	seen := map[string]bool{}
	for _, sentence := range routingsnapshot.MaxPricePasses {
		reason, ok := maxPricePasses[sentence]
		if !ok || !strings.HasPrefix(reason, "evaluate.max_price.") || seen[reason] {
			t.Errorf("%q: reason %q", sentence, reason)
		}
		seen[reason] = true
		if got := redact(sentence); got != sentence {
			t.Errorf("%q: the shadow logs %q", sentence, got)
		}
	}
	if len(maxPricePasses) != len(routingsnapshot.MaxPricePasses) {
		t.Errorf("%d reasons for %d passes", len(maxPricePasses), len(routingsnapshot.MaxPricePasses))
	}
	if got := redact("provider.made_up_field"); got != "provider.<field>" {
		t.Errorf("an unknown field logs as %q", got) // the control: redact still hides the body's names
	}
}
