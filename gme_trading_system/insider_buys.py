"""
Insider open-market purchase aggregator (SEC Form 4).

Pulls Form 4 filings for a ticker over a lookback window, filters for
open-market purchases (transaction code "P") by Section 16 reporting
persons — directors, officers, or 10% beneficial owners (e.g. RC Ventures
on GME) — and returns aggregate count and dollar value. Used by Trove
Score (Pillar D — Insider Conviction).

SEC endpoints (no API key, fair-use UA required):
  - Ticker → CIK:        https://www.sec.gov/files/company_tickers.json
  - Filings index:       https://data.sec.gov/submissions/CIK{cik10}.json
  - Form 4 XML document: https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}

Caches the filings index and parsed result on disk for 24h to avoid hammering EDGAR.
"""
from __future__ import annotations

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Optional

import requests

try:
    from circuit_breaker import get_breaker, CircuitOpenError
except ImportError:  # allow standalone import from repo root
    from gme_trading_system.circuit_breaker import get_breaker, CircuitOpenError

log = logging.getLogger(__name__)

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "GMETradingSystem research@example.com")
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
_REQUEST_INTERVAL = 0.2  # 5 req/s, under SEC's 10/s cap

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".insider_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
_CACHE_TTL = 24 * 3600


@dataclass
class InsiderBuyStats:
    ticker: str
    years: int
    count: int = 0
    dollars: float = 0.0
    top_buyers: list[tuple[str, str, float]] = field(default_factory=list)  # (name, role, dollars)
    error: Optional[str] = None


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 15) -> requests.Response:
    breaker = get_breaker("sec")
    time.sleep(_REQUEST_INTERVAL)
    return breaker.call(requests.get, url, headers=SEC_HEADERS, timeout=timeout)


# ── Ticker → CIK ──────────────────────────────────────────────────────────────

_TICKER_MAP_PATH = os.path.join(CACHE_DIR, "ticker_cik.json")


def _load_ticker_map() -> dict:
    if os.path.exists(_TICKER_MAP_PATH) and (time.time() - os.path.getmtime(_TICKER_MAP_PATH)) < 7 * 86400:
        with open(_TICKER_MAP_PATH) as f:
            return json.load(f)
    r = _get("https://www.sec.gov/files/company_tickers.json")
    r.raise_for_status()
    raw = r.json()
    # raw is {"0": {"cik_str": 1326380, "ticker": "GME", "title": "..."}, ...}
    out = {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in raw.values()}
    with open(_TICKER_MAP_PATH, "w") as f:
        json.dump(out, f)
    return out


def resolve_cik(ticker: str) -> Optional[str]:
    return _load_ticker_map().get(ticker.upper())


# ── Form 4 parsing ────────────────────────────────────────────────────────────

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _findtext(elem, name: str) -> Optional[str]:
    for child in elem.iter():
        if _strip_ns(child.tag) == name and child.text is not None:
            return child.text.strip()
    return None


def _truthy(v: Optional[str]) -> bool:
    return v is not None and v.strip() in ("1", "true", "True")


def parse_form4(xml_bytes: bytes) -> tuple[bool, str, list[tuple[float, float]]]:
    """Return (is_director_or_officer, owner_name, [(shares, price), ...] for P-code rows)."""
    root = ET.fromstring(xml_bytes)
    is_dir = is_off = is_ten = False
    owner_name = ""

    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "reportingOwnerRelationship":
            is_dir = is_dir or _truthy(_findtext(elem, "isDirector"))
            is_off = is_off or _truthy(_findtext(elem, "isOfficer"))
            is_ten = is_ten or _truthy(_findtext(elem, "isTenPercentOwner"))
        elif tag == "reportingOwnerId" and not owner_name:
            owner_name = _findtext(elem, "rptOwnerName") or ""

    if not (is_dir or is_off or is_ten):
        return False, owner_name, []

    buys: list[tuple[float, float]] = []
    for elem in root.iter():
        if _strip_ns(elem.tag) != "nonDerivativeTransaction":
            continue
        code = None
        shares = price = None
        acquired_disposed = None
        for sub in elem.iter():
            t = _strip_ns(sub.tag)
            if t == "transactionCode" and sub.text:
                code = sub.text.strip()
            elif t == "transactionShares":
                v = _findtext(sub, "value")
                if v:
                    try:
                        shares = float(v)
                    except ValueError:
                        pass
            elif t == "transactionPricePerShare":
                v = _findtext(sub, "value")
                if v:
                    try:
                        price = float(v)
                    except ValueError:
                        pass
            elif t == "transactionAcquiredDisposedCode":
                acquired_disposed = _findtext(sub, "value")
        if code == "P" and shares and price and (acquired_disposed in (None, "A")):
            buys.append((shares, price))

    return True, owner_name, buys


# ── Aggregator ────────────────────────────────────────────────────────────────

def _cache_path(ticker: str, years: int) -> str:
    return os.path.join(CACHE_DIR, f"{ticker.upper()}_{years}y.json")


def _read_cache(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) > _CACHE_TTL:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def fetch_insider_buys(ticker: str, years: int = 3) -> InsiderBuyStats:
    """Aggregate director+officer open-market purchases (Form 4 code P) over `years`."""
    cache_path = _cache_path(ticker, years)
    cached = _read_cache(cache_path)
    if cached is not None:
        return InsiderBuyStats(**cached)

    stats = InsiderBuyStats(ticker=ticker.upper(), years=years)
    try:
        cik = resolve_cik(ticker)
        if not cik:
            stats.error = "cik_not_found"
            return stats

        cik_int = int(cik)
        sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = _get(sub_url)
        r.raise_for_status()
        sub = r.json()
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        filing_dates = recent.get("filingDate", [])

        cutoff = date.today() - timedelta(days=365 * years)
        per_buyer: dict[str, tuple[str, float]] = {}

        for form, acc, doc, fdate_str in zip(forms, accs, primary_docs, filing_dates):
            if form != "4":
                continue
            try:
                fdate = datetime.strptime(fdate_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if fdate < cutoff:
                continue

            acc_nodash = acc.replace("-", "")
            # primaryDocument often points to an xsl-rendered HTML wrapper
            # (e.g. "xslF345X05/form4...xml"); strip that prefix to get raw XML.
            raw_doc = doc.split("/", 1)[1] if doc.lower().startswith("xsl") else doc
            xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{raw_doc}"
            try:
                xr = _get(xml_url)
                xr.raise_for_status()
                eligible, name, buys = parse_form4(xr.content)
            except (CircuitOpenError, requests.RequestException, ET.ParseError) as e:
                log.debug("form4 fetch/parse failed for %s: %s", acc, e)
                continue

            if not eligible or not buys:
                continue
            filing_dollars = sum(s * p for s, p in buys)
            stats.count += len(buys)
            stats.dollars += filing_dollars
            role = "section16"
            prev = per_buyer.get(name, (role, 0.0))
            per_buyer[name] = (role, prev[1] + filing_dollars)

        stats.top_buyers = sorted(
            [(n, r, round(d, 2)) for n, (r, d) in per_buyer.items()],
            key=lambda x: x[2],
            reverse=True,
        )[:5]
        stats.dollars = round(stats.dollars, 2)

        with open(cache_path, "w") as f:
            json.dump(asdict(stats), f)
        return stats

    except CircuitOpenError:
        stats.error = "circuit_open"
        return stats
    except Exception as e:
        log.warning("insider_buys fetch failed for %s: %s", ticker, e)
        stats.error = str(e)[:120]
        return stats


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    tk = sys.argv[1] if len(sys.argv) > 1 else "GME"
    print(fetch_insider_buys(tk, 3))
