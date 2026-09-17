"""Stable failure codes without upstream bodies, keys, or payment secrets."""


class FundingReviewRequired(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {"credit_rejected", "credit_account_unavailable", "credit_amount_limit", "invoice_missing", "invoice_invalid",
                        "unknown_backend", "wallet_unavailable", "quote_expiry_changed", "creation_expired",
                        "creation_ambiguous", "creation_recovery_limit"}:
            raise ValueError("Unknown funding failure code")
        self.code = code
        super().__init__(code)


class QuoteUnavailable(RuntimeError):
    pass
