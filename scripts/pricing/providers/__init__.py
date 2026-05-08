"""Per-provider config modules — human-only.

Each module hardcodes its `URL` and `EXPECTED_MODELS` constants.
The LLM never reads or writes these files. URLs and network IO live
here; the LLM-rewriteable parser tier (`scripts/pricing/parsers/`)
only sees a `html: str` input and returns a dict.
"""
