"""
YouTube channel subscriber-count scraper.

Lightweight HTML scrape of the channel landing page — no API key required.
YouTube serves a stripped page to unauthenticated bots, so we send
browser-like headers + the CONSENT/SOCS cookies that bypass the EU
consent gate. Returns the integer subscriber count, or None on failure.

Used by FundamentalsFeed to populate the OBS panel's @handle row.
Refresh cadence: every 30 min Mon-Fri during market hours (piggybacks
on run_fundamentals_update). YouTube updates the visible count slowly,
so even hourly would be overkill — this just rides the existing job.

If YouTube ever changes its HTML enough to break the regex, fall back
to the YouTube Data API v3 (channels.list?part=statistics, ~free).
"""
import logging
import re
import requests

log = logging.getLogger(__name__)

_CHANNEL_URL = "https://www.youtube.com/@{handle}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+; SOCS=CAI; PREF=hl=en",
}
_SUB_REGEX = re.compile(r"(\d[\d,\.]*)\s*(K|M)?\s*subscribers?", re.IGNORECASE)


def get_subscriber_count(handle: str, timeout: int = 10) -> int | None:
    """Fetch the current subscriber count for a `@handle`.

    Returns the count as an integer (e.g. "1.2K subscribers" → 1200).
    Returns None on any failure — caller should treat as "unchanged".
    """
    try:
        resp = requests.get(
            _CHANNEL_URL.format(handle=handle.lstrip("@")),
            headers=_HEADERS,
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.debug(f"[youtube] {handle} fetch error: {e}")
        return None

    if resp.status_code != 200:
        log.debug(f"[youtube] {handle} HTTP {resp.status_code}")
        return None

    m = _SUB_REGEX.search(resp.text)
    if not m:
        log.debug(f"[youtube] {handle} regex miss (len={len(resp.text)})")
        return None

    return _parse_count(m.group(1), m.group(2))


def _parse_count(num: str, suffix: str | None) -> int | None:
    try:
        n = float(num.replace(",", ""))
    except ValueError:
        return None
    if suffix and suffix.upper() == "K":
        n *= 1_000
    elif suffix and suffix.upper() == "M":
        n *= 1_000_000
    return int(round(n))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    handle = sys.argv[1] if len(sys.argv) > 1 else "TroveIsland"
    print(f"{handle}: {get_subscriber_count(handle)}")
