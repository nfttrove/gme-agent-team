"""
SEC EDGAR Scanner — real-time filing signal detection.

Queries the SEC EDGAR full-text search API for PE playbook trigger filings:
  - 8-K: restructuring advisor hired, benefit cuts, leadership changes
  - DEF 14A: board composition (PE connections)
  - Form 4: insider selling clusters
  - 13D/13G: activist investor positions

All results are persisted to structural_signals table and scored by pe_playbook.py.

SEC EDGAR APIs used (all free, no API key required):
  - Full-text search: https://efts.sec.gov/LATEST/search-index?q=...&dateRange=custom&...
  - Company filings: https://data.sec.gov/submissions/CIK{cik}.json
  - Company facts:   https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json

Rate limit: 10 requests/second per SEC guidelines. We throttle to 5/s.

Usage:
    scanner = SECScanner()
    signals = scanner.scan_company("GME", "0001326380")   # GME CIK
    watchlist = scanner.scan_watchlist()                   # all tracked companies
"""
import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

# SEC requires a user-agent header identifying your app and contact email
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "GMETradingSystem research@example.com")
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT}

_REQUEST_INTERVAL = 0.2   # 200ms between requests = 5 req/s (well under 10/s limit)

# ── Company registry ───────────────────────────────────────────────────────────
# CIK numbers from EDGAR. Add PE-targeted companies here to expand the watchlist.

COMPANY_REGISTRY = {
    "GME":  {"cik": "0001326380", "name": "GameStop Corp"},
    "AMC":  {"cik": "0001411579", "name": "AMC Entertainment Holdings"},
    "BBBY": {"cik": "0000886136", "name": "Bed Bath & Beyond"},  # historical reference
    "EXPR": {"cik": "0001483510", "name": "Express Inc"},
    "MACY": {"cik": "0000794367", "name": "Macy's Inc"},
    "CONN": {"cik": "0000723603", "name": "Conn's Inc"},
}

# Key investors to monitor — any new 13D or 13F from these filers is intelligence
KEY_INVESTOR_REGISTRY = {
    "RC_VENTURES": {
        "cik":  "0001822844",
        "name": "RC Ventures LLC (Ryan Cohen)",
        "disclosure_type": "13D",   # crosses 5% activist threshold
        "known_positions": {
            "GME":  {"type": "activist", "stake_pct": 9.0,      "approx_value_usd": "~$800M", "note": "CEO/Chairman, activist since 2020"},
            "BABA": {"type": "activist", "stake_pct": "~0.07%", "approx_value_usd": "~$1B",   "note": "Pushed for $40B→$60B buyback program"},
            "AAPL": {"type": "passive", "approx_value_usd": "large", "note": "Passive long-term holding"},
            "WFC":  {"type": "passive", "approx_value_usd": "large", "note": "Passive; paid FTC $985k for HSR violation"},
            "NFLX": {"type": "passive", "approx_value_usd": "large", "note": "Passive holding"},
            "C":    {"type": "passive", "approx_value_usd": "large", "note": "Passive holding"},
        },
        "exited": {"BBBY": "Aug 2022 — $68M profit, then BBBY went bankrupt"},
    },
    "SCION": {
        "cik":  "0001649339",
        "name": "Scion Asset Management LLC (Michael Burry)",
        "disclosure_type": "13F",   # quarterly institutional filing
        "known_positions": {},       # populated dynamically from latest 13F
        "notes": (
            "Burry personally owns GME (not in Scion 13F — personal account). "
            "Scion Q3 2025 pivot: 66% Palantir, 14% Nvidia, 11% Pfizer — exited all Chinese tech. "
            "Q4 2024: heavy Chinese tech (BABA, Baidu, JD, PDD). Major strategy rotations quarterly."
        ),
    },
}

# EDGAR search terms that map directly to pe_playbook signals
SIGNAL_SEARCH_TERMS = {
    "restructuring_advisor_hired": [
        "Chief Restructuring Officer",
        "AlixPartners",
        "Alvarez & Marsal",
        "Lazard restructuring",
    ],
    "sale_leaseback_announced": [
        "sale-leaseback",
        "sale leaseback",
        "sold and leased back",
    ],
    "employee_benefit_cuts": [
        "401(k) match",
        "employer match eliminated",
        "health insurance premium",
        "pension freeze",
    ],
    "pe_executive_rotation": [
        "Chief Restructuring Officer appointed",
        "interim Chief Executive",
    ],
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class FilingSignal:
    ticker: str
    signal_name: str
    filing_type: str       # 8-K, DEF 14A, Form 4, etc.
    filing_date: str
    headline: str
    url: str
    confidence: float
    action: str            # SHORT, SQUEEZE_WATCH, EXIT, MONITOR
    timeline_months: int


# ── Scanner ────────────────────────────────────────────────────────────────────

class SECScanner:
    def __init__(self):
        self._last_request = 0.0

    def _get(self, url: str, params: dict = None, max_retries: int = 5) -> dict | None:
        """
        Throttled EDGAR GET request with exponential backoff for 429 errors.

        Rate limit handling:
          - Normal 200: proceeds immediately
          - 429 (rate limit): exponential backoff with Retry-After header respect
          - Transient errors (timeout, connection): 500ms delay, retry up to 5 times
          - Hard failure: returns None after max_retries exceeded
        """
        base_backoff_s = 1.0
        max_backoff_s = 60.0

        for attempt in range(max_retries):
            # Respect base request throttle (200ms = 5 req/s)
            elapsed = time.time() - self._last_request
            if elapsed < _REQUEST_INTERVAL:
                time.sleep(_REQUEST_INTERVAL - elapsed)

            try:
                resp = requests.get(url, headers=SEC_HEADERS, params=params, timeout=15)
                self._last_request = time.time()

                # Success
                if resp.status_code == 200:
                    return resp.json()

                # Rate limit — exponential backoff
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get('Retry-After', base_backoff_s))
                    backoff = min(retry_after, max_backoff_s)
                    jitter = random.uniform(0, backoff * 0.1)  # ±10% jitter
                    wait_time = backoff + jitter
                    log.warning(f"[sec] 429 rate limit on attempt {attempt+1}/{max_retries} — backing off {wait_time:.1f}s")
                    time.sleep(wait_time)
                    continue

                # Other HTTP errors (4xx, 5xx except 429) — fail
                if attempt == max_retries - 1:
                    log.error(f"[sec] HTTP {resp.status_code} after {max_retries} attempts for {url}")
                    return None

                # Retry on 5xx errors
                log.warning(f"[sec] HTTP {resp.status_code} on attempt {attempt+1}/{max_retries} — retrying")
                time.sleep(0.5)
                continue

            except requests.Timeout:
                if attempt == max_retries - 1:
                    log.error(f"[sec] Request timeout after {max_retries} attempts for {url}")
                    return None
                log.warning(f"[sec] Request timeout on attempt {attempt+1}/{max_retries} — retrying in 500ms")
                time.sleep(0.5)
                continue

            except requests.ConnectionError:
                if attempt == max_retries - 1:
                    log.error(f"[sec] Connection error after {max_retries} attempts for {url}")
                    return None
                log.warning(f"[sec] Connection error on attempt {attempt+1}/{max_retries} — retrying in 500ms")
                time.sleep(0.5)
                continue

            except requests.RequestException as e:
                log.error(f"[sec] Unexpected request error: {e}")
                return None

        return None

    def _full_text_search(self, query: str, ticker: str, days_back: int = 7) -> list[dict]:
        """Search EDGAR full-text for recent filings containing a phrase."""
        since = (date.today() - timedelta(days=days_back)).isoformat()
        url = "https://efts.sec.gov/LATEST/search-index"
        params = {
            "q": f'"{query}"',
            "dateRange": "custom",
            "startdt": since,
            "enddt": date.today().isoformat(),
            "entity": ticker,
        }
        data = self._get(url, params)
        if not data:
            return []
        return data.get("hits", {}).get("hits", [])

    def get_recent_filings(self, cik: str, form_types: list[str], days_back: int = 30) -> list[dict]:
        """Fetch recent filings for a company by CIK."""
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        data = self._get(url)
        if not data:
            return []

        filings = data.get("filings", {}).get("recent", {})
        if not filings:
            return []

        cutoff = (date.today() - timedelta(days=days_back)).isoformat()
        results = []
        dates = filings.get("filingDate", [])
        forms = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        descriptions = filings.get("primaryDocument", [])

        for i, (filing_date, form, accession, primary_doc) in enumerate(
            zip(dates, forms, accessions, descriptions)
        ):
            if filing_date < cutoff:
                break
            if form_types and form not in form_types:
                continue
            acc_clean = accession.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc_clean}/{primary_doc}"
            results.append({
                "date": filing_date,
                "form": form,
                "accession": accession,
                "url": filing_url,
                "description": primary_doc,
            })

        return results

    def get_company_debt(self, cik: str) -> dict | None:
        """
        Pull long-term debt from EDGAR XBRL facts.
        Returns {"long_term_debt": float, "cash": float, "period": str} or None.
        """
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        data = self._get(url)
        if not data:
            return None

        facts = data.get("facts", {})
        us_gaap = facts.get("us-gaap", {})

        def latest_value(concept: str) -> float | None:
            entries = us_gaap.get(concept, {}).get("units", {}).get("USD", [])
            # Filter to annual (10-K) or quarterly (10-Q) and sort by end date
            annual = [e for e in entries if e.get("form") in ("10-K", "10-Q")]
            if not annual:
                return None
            latest = sorted(annual, key=lambda x: x.get("end", ""), reverse=True)[0]
            return latest.get("val")

        debt = latest_value("LongTermDebt") or latest_value("LongTermDebtNoncurrent")
        cash = latest_value("CashAndCashEquivalentsAtCarryingValue")

        return {
            "long_term_debt": debt,
            "cash": cash,
        }

    def scan_company(self, ticker: str, cik: str, days_back: int = 7) -> list[FilingSignal]:
        """
        Run all playbook signal scans for a single company.
        Returns list of FilingSignal objects.
        """
        from pe_playbook import PLAYBOOK_SIGNALS
        signal_map = {s.name: s for s in PLAYBOOK_SIGNALS}

        detected = []

        for signal_name, search_terms in SIGNAL_SEARCH_TERMS.items():
            ps = signal_map.get(signal_name)
            if not ps:
                continue

            for term in search_terms:
                hits = self._full_text_search(term, ticker, days_back=days_back)
                for hit in hits[:3]:  # max 3 per term to avoid noise
                    source = hit.get("_source", {})
                    filing_date = source.get("file_date", date.today().isoformat())
                    form_type = source.get("form_type", "8-K")
                    entity = source.get("entity_name", ticker)
                    filing_url = source.get("file_url", "")
                    headline = f"[{form_type}] {entity}: '{term}' detected"

                    sig = FilingSignal(
                        ticker=ticker,
                        signal_name=signal_name,
                        filing_type=form_type,
                        filing_date=filing_date,
                        headline=headline,
                        url=filing_url,
                        confidence=ps.confidence,
                        action=ps.action,
                        timeline_months=ps.timeline_months,
                    )
                    detected.append(sig)
                    log.warning(
                        f"[sec] SIGNAL: {ticker} | {signal_name} ({ps.confidence:.0%}) | {term}"
                    )

        # Persist to DB
        if detected:
            self._persist_signals(detected)

        return detected

    def scan_watchlist(self, days_back: int = 7) -> dict[str, list[FilingSignal]]:
        """Run scan across all companies in COMPANY_REGISTRY."""
        results = {}
        for ticker, info in COMPANY_REGISTRY.items():
            log.info(f"[sec] Scanning {ticker} ({info['name']})...")
            signals = self.scan_company(ticker, info["cik"], days_back=days_back)
            if signals:
                results[ticker] = signals
            time.sleep(_REQUEST_INTERVAL)
        return results

    def check_gme_immunity(self) -> dict:
        """
        Check GME's structural immunity indicators against live EDGAR data.
        Returns a health report for the CTO agent.
        """
        from pe_playbook import GME_IMMUNITY_CHECKS

        cik = COMPANY_REGISTRY["GME"]["cik"]
        financials = self.get_company_debt(cik)
        recent_8k = self.get_recent_filings(cik, ["8-K"], days_back=30)

        report = {
            "timestamp": datetime.now().isoformat(),
            "ticker": "GME",
            "checks": {},
            "overall_status": "GREEN",
            "alerts": [],
        }

        # Debt check
        if financials:
            debt = financials.get("long_term_debt") or 0
            cash = financials.get("cash") or 0

            debt_status = "GREEN" if debt == 0 else ("YELLOW" if debt < 500_000_000 else "RED")
            cash_status = "GREEN" if cash >= 1_000_000_000 else ("YELLOW" if cash >= 500_000_000 else "RED")

            report["checks"]["debt_free"] = {
                "status": debt_status,
                "value": f"${debt/1e9:.2f}B long-term debt" if debt else "$0 debt",
            }
            report["checks"]["cash_position"] = {
                "status": cash_status,
                "value": f"${cash/1e9:.2f}B cash" if cash else "Unknown",
            }

            if debt_status == "RED":
                alert = f"CRITICAL: GME now carries ${debt/1e9:.2f}B in long-term debt — PE playbook weapon restored"
                report["alerts"].append(alert)
                report["overall_status"] = "RED"

        # Restructuring advisor check — scan recent 8-Ks
        restructuring_terms = ["AlixPartners", "Alvarez & Marsal", "Chief Restructuring Officer"]
        for filing in recent_8k:
            for term in restructuring_terms:
                if term.lower() in str(filing).lower():
                    alert = f"CRITICAL: Restructuring advisor detected in GME 8-K — EXIT IMMEDIATELY"
                    report["alerts"].append(alert)
                    report["overall_status"] = "RED"

        return report

    def _persist_signals(self, signals: list[FilingSignal]):
        """Write detected signals to structural_signals table."""
        try:
            conn = sqlite3.connect(DB_PATH)
            for sig in signals:
                conn.execute(
                    """INSERT OR IGNORE INTO structural_signals
                       (timestamp, ticker, signal_name, filing_type, filing_date,
                        headline, url, confidence, action, timeline_months)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        datetime.now().isoformat(),
                        sig.ticker, sig.signal_name, sig.filing_type,
                        sig.filing_date, sig.headline, sig.url,
                        sig.confidence, sig.action, sig.timeline_months,
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[sec] Failed to persist signals: {e}")

    def fetch_scion_latest_13f(self) -> dict:
        """
        Fetch Scion Asset Management's most recent 13F holdings.
        Returns {period, filed, holdings: [{name, value_usd, shares}]}.
        """
        import xml.etree.ElementTree as ET

        cik = KEY_INVESTOR_REGISTRY["SCION"]["cik"]
        data = self._get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        if not data:
            return {"error": "Could not reach EDGAR"}

        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])

        # Find the latest 13F-HR
        latest_acc = latest_date = None
        for f, dt, acc in zip(forms, dates, accessions):
            if f == "13F-HR":
                latest_date = dt
                latest_acc = acc
                break

        if not latest_acc:
            return {"error": "No 13F-HR found for Scion"}

        acc_clean = latest_acc.replace("-", "")
        info_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc_clean}/infotable.xml"
        time.sleep(_REQUEST_INTERVAL)
        resp = requests.get(info_url, headers=SEC_HEADERS, timeout=15)
        if resp.status_code != 200:
            return {"error": f"Could not fetch infotable: {resp.status_code}"}

        # Regex parse — handles both namespaced and plain XML (ET fails on default-namespace docs)
        import re as _re
        raw = resp.text
        holdings = []
        # Match both <infoTable> and <ns:infoTable> (some filers use prefixed tags)
        for match in _re.finditer(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", raw, _re.DOTALL):
            h = match.group(1)
            name   = _re.search(r"<nameOfIssuer>(.*?)</nameOfIssuer>", h)
            value  = _re.search(r"<value>(.*?)</value>", h)
            shares = _re.search(r"<sshPrnamt>(.*?)</sshPrnamt>", h)
            if name and value:
                raw_val = value.group(1).strip()
                raw_sh  = shares.group(1).strip() if shares else "0"
                # This filing format reports values in actual dollars (verified: NVDA $186.58/sh, HAL $24.60/sh ✓)
                holdings.append({
                    "name":      name.group(1).strip(),
                    "value_usd": int(raw_val) if raw_val.isdigit() else 0,
                    "shares":    int(raw_sh) if raw_sh.isdigit() else 0,
                })

        holdings.sort(key=lambda x: -x["value_usd"])
        total = sum(h["value_usd"] for h in holdings)
        for h in holdings:
            h["pct_portfolio"] = round(h["value_usd"] / total * 100, 1) if total else 0

        return {
            "investor": "Michael Burry / Scion Asset Management",
            "filing_date": latest_date,
            "total_aum_usd": total,
            "holdings": holdings,
            "note": KEY_INVESTOR_REGISTRY["SCION"]["notes"],
        }

    def check_rc_ventures_new_filings(self, days_back: int = 7) -> list[dict]:
        """
        Check RC Ventures for new 13D/4 filings (new activist positions or share purchases).
        Any new filing is an actionable signal.
        """
        cik = KEY_INVESTOR_REGISTRY["RC_VENTURES"]["cik"]
        data = self._get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        if not data:
            return []

        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        descriptions = filings.get("primaryDocument", [])

        cutoff = (date.today() - timedelta(days=days_back)).isoformat()
        new_filings = []

        for f, dt, acc, desc in zip(forms, dates, accessions, descriptions):
            if dt < cutoff:
                break
            if f in ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "4"):
                acc_clean = acc.replace("-", "")
                cik_num = cik.lstrip("0")
                url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_clean}/{desc}"
                new_filings.append({
                    "investor": "Ryan Cohen / RC Ventures LLC",
                    "form": f,
                    "date": dt,
                    "url": url,
                    "signal": "CRITICAL" if f in ("SC 13D", "SC 13D/A") else "IMPORTANT",
                    "note": (
                        "New 13D = possible new activist position or major GME purchase. "
                        "Form 4 = insider transaction at GameStop."
                    ),
                })
                log.info(f"[sec] RC Ventures new filing: {f} on {dt}")

        return new_filings

    def key_investor_intelligence_report(self) -> dict:
        """
        Full intelligence report on Ryan Cohen and Michael Burry.
        Called by the CTO agent's daily brief. Cached in agent_logs.
        """
        log.info("[sec] Generating key investor intelligence report...")
        rc_filings = self.check_rc_ventures_new_filings(days_back=30)
        scion_13f = self.fetch_scion_latest_13f()

        report = {
            "timestamp": datetime.now().isoformat(),
            "rc_ventures": {
                "known_positions": KEY_INVESTOR_REGISTRY["RC_VENTURES"]["known_positions"],
                "recent_filings": rc_filings,
                "signal": "CRITICAL" if rc_filings else "MONITOR",
                "alert": f"RC Ventures filed {len(rc_filings)} new document(s) in last 30 days" if rc_filings else "No new RC Ventures filings in 30 days",
            },
            "scion": scion_13f,
        }

        # Persist to agent_logs for CTO to query
        try:
            import sqlite3
            _top = (scion_13f.get("holdings") or [{}])[0]
            summary = (
                f"RC_VENTURES: {report['rc_ventures']['alert']} | "
                f"RC_POSITIONS: GME(9%activist), BABA($1B activist), AAPL/WFC/NFLX(passive) | "
                f"SCION_LATEST: {scion_13f.get('filing_date','?')} — "
                f"top={_top.get('name','?')} "
                f"({_top.get('pct_portfolio','?')}%) | "
                f"BURRY_GME: Personal position, not in Scion 13F"
            )
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO agent_logs (agent_name, timestamp, task_type, content, status) VALUES (?,?,?,?,?)",
                ("CTO", datetime.now().isoformat(), "investor_intel", summary, "ok"),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[sec] Could not log investor intel: {e}")

        return report

    def short_watchlist_report(self) -> list[dict]:
        """
        Return the current short watchlist sorted by signal score.
        For the CTO agent's daily briefing.
        """
        from pe_playbook import score_short_target

        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT ticker, GROUP_CONCAT(DISTINCT signal_name) as signals,
                      AVG(confidence) as avg_conf, MIN(timeline_months) as min_timeline
               FROM structural_signals
               WHERE filing_date >= date('now', '-90 days')
               GROUP BY ticker ORDER BY avg_conf DESC"""
        ).fetchall()
        conn.close()

        report = []
        for ticker, signals_csv, avg_conf, min_timeline in rows:
            signal_list = signals_csv.split(",") if signals_csv else []
            scored = score_short_target(signal_list)
            report.append({
                "ticker": ticker,
                "signals": signal_list,
                **scored,
            })

        return sorted(report, key=lambda x: -x["score"])


# ── Standalone entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    scanner = SECScanner()

    print("\n=== GME Immunity Check ===")
    immunity = scanner.check_gme_immunity()
    print(json.dumps(immunity, indent=2))

    print("\n=== Watchlist Scan (last 7 days) ===")
    results = scanner.scan_watchlist(days_back=7)
    for ticker, sigs in results.items():
        print(f"\n{ticker}: {len(sigs)} signal(s)")
        for s in sigs:
            print(f"  [{s.action}] {s.signal_name} ({s.confidence:.0%}) — {s.headline}")
