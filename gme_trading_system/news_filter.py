"""
GME news relevance filter — disambiguates the GME ticker from other
abbreviations that share the same three letters.

Pure module. Caller passes an article dict (headline + summary), gets back
a boolean. Drop the article if False.

False-positive sources seen in the wild:
  - Global Medical Equipment (industry trade press)
  - Graduate Medical Education (academic / healthcare)

If a new false-positive keyword surfaces, extend NON_GME_TERMS. The
deny-list is permissive-by-default so we don't over-filter — we only
exclude when we recognise a known non-GME context.
"""

# Strong signals that the article IS about GameStop the company.
# Lowercase substrings; matches anywhere in headline + summary.
GME_TERMS: tuple[str, ...] = (
    "gamestop",
    "game stop",
    "ryan cohen",
    "rc ventures",
    "$gme",
    "nyse:gme",
    "nasdaq:gme",
    "video game retailer",
)

# Known abbreviation collisions — present in healthcare / academia / industry press.
# Each one is unambiguous when seen; if matched, the article is NOT about GameStop.
NON_GME_TERMS: tuple[str, ...] = (
    "global medical equipment",
    "graduate medical education",
    "graduate management education",
    "generalized mineral exploration",
    "global media engineering",
)


def is_gme_relevant(headline: str, summary: str = "") -> bool:
    """Return True if the article is about GameStop, False if it's a known
    GME-abbreviation collision.

    Decision order:
      1. Strong-positive signal (GameStop / Cohen / $GME …) → include.
      2. Strong-negative signal (medical / academic abbreviation) → exclude.
      3. Ambiguous bare 'GME' mention with no context → include (permissive
         default; rely on the deny-list to grow as new collisions surface).
    """
    text = f"{headline or ''} {summary or ''}".lower()

    if any(term in text for term in GME_TERMS):
        return True
    if any(term in text for term in NON_GME_TERMS):
        return False
    return True


def filter_articles(articles: list[dict]) -> list[dict]:
    """Apply is_gme_relevant to each article in a list. Preserves order.

    Articles without 'headline' or with 'error' keys are passed through
    untouched — the caller does its own error handling on those.
    """
    out: list[dict] = []
    for a in articles:
        if "error" in a or not a.get("headline"):
            out.append(a)
            continue
        if is_gme_relevant(a.get("headline", ""), a.get("summary", "")):
            out.append(a)
    return out
