"""Email addresses that cannot be a real mailbox: typo TLDs and misspelled
big-provider domains.

Used by the PayPal top-up guard. A PayPal account can be opened with an
address nobody can receive mail at ("someone@gmail.con"); a payer like that
is a throwaway, not a customer. The lists are deliberately small and
conservative: every entry is a domain or TLD that does not exist or that no
mail provider serves, so a real customer is never caught. Real country-code
TLDs that look like typos (.co, .cm, .om) are only listed as part of a
specific misspelled provider domain, never as a bare TLD.
"""

from __future__ import annotations

# Not delegated TLDs; each is a one-key slip of .com, .net or .org.
IMPOSSIBLE_TLDS = frozenset({
    "con", "cmo", "comm", "coom", "ocm", "vom", "xom", "cpm", "copm",
    "nte", "nett", "ogr", "orgg",
})

# Misspellings of the big free-mail domains. Exact-domain matches only, so
# hotmail.co.uk or yahoo.co.jp are untouched.
MISSPELLED_PROVIDER_DOMAINS = frozenset({
    # gmail.com
    "gmial.com", "gmai.com", "gmal.com", "gamil.com", "gnail.com", "gmaill.com",
    "gmail.co", "gmail.cm", "gmail.om", "gmeil.com", "gmali.com",
    # hotmail.com
    "hotmial.com", "hotmai.com", "hotmal.com", "hotmil.com", "hotmail.co",
    "hotamil.com", "hormail.com",
    # outlook.com
    "outlok.com", "outllook.com", "outlook.co", "outloo.com",
    # yahoo.com
    "yaho.com", "yahooo.com", "yhoo.com", "yahoo.co", "yahho.com",
    # icloud.com
    "iclod.com", "icloud.co", "icoud.com",
})


def email_domain(email: str | None) -> str | None:
    """The lower-cased domain of a syntactically plausible address, else None."""
    if not isinstance(email, str):
        return None
    address = email.strip().lower()
    local, sep, domain = address.rpartition("@")
    domain = domain.rstrip(".")
    if not sep or not local or "." not in domain or domain.startswith("."):
        return None
    if any(not label for label in domain.split(".")):
        return None
    return domain


def is_impossible_email(email: str | None) -> bool:
    """Whether the address is malformed, ends in a typo TLD, or sits at a
    misspelled big-provider domain."""
    domain = email_domain(email)
    if domain is None:
        return True
    if domain.rsplit(".", 1)[-1] in IMPOSSIBLE_TLDS:
        return True
    return domain in MISSPELLED_PROVIDER_DOMAINS
