"""
Options chain, max pain, and dark pool intelligence for GME.

Data sources (all free):
  - yfinance: full options chain (calls + puts, all strikes, all expirations)
  - Max pain: calculated from OI across all strikes (no API needed)
  - Dark pool: FINRA ATS delayed data via requests (free, 2-week lag)
    For real-time dark pool: upgrade to Unusual Whales ($50/mo) or FlowAlgo

Max Pain theory:
  Market makers are net short options (they sell to retail).
  They hedge dynamically, creating gravitational pull toward the strike
  where the total value of expiring options (calls + puts) is minimised.
  That strike = max pain. Stock gravitates toward it into expiry.

Usage:
    from options_feed import OptionsFeed
    feed = OptionsFeed()
    chain = feed.get_chain("GME")           # full options chain
    mp = feed.max_pain("GME")               # this Friday's max pain
    dp = feed.dark_pool_summary("GME")      # recent dark pool prints
    feed.update_db()                        # persist to DB + notify Telegram
"""
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL  = "GME"


class OptionsFeed:

    def __init__(self):
        self._ticker = yf.Ticker(SYMBOL)

    # ── Options chain ──────────────────────────────────────────────────────────

    def get_expirations(self) -> list[str]:
        """All available expiration dates."""
        try:
            return list(self._ticker.options)
        except Exception as e:
            log.error(f"[options] Failed to fetch expirations: {e}")
            return []

    def get_chain(self, expiration: str | None = None) -> dict:
        """
        Fetch calls and puts for a given expiration (defaults to nearest Friday).
        Returns {"calls": DataFrame, "puts": DataFrame, "expiration": str}
        """
        expirations = self.get_expirations()
        if not expirations:
            return {}

        if expiration is None:
            expiration = self._nearest_friday_expiry(expirations)

        try:
            chain = self._ticker.option_chain(expiration)
            calls = chain.calls[["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]]
            puts  = chain.puts[["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]]
            calls = calls.rename(columns={"lastPrice": "last", "openInterest": "OI", "impliedVolatility": "IV"})
            puts  = puts.rename(columns={"lastPrice": "last", "openInterest": "OI", "impliedVolatility": "IV"})
            return {"calls": calls, "puts": puts, "expiration": expiration}
        except Exception as e:
            log.error(f"[options] Chain fetch failed for {expiration}: {e}")
            return {}

    def get_all_chains(self) -> list[dict]:
        """Fetch chains for all available expirations."""
        chains = []
        for exp in self.get_expirations():
            chain = self.get_chain(exp)
            if chain:
                chains.append(chain)
        return chains

    # ── Max pain ──────────────────────────────────────────────────────────────

    def max_pain(self, expiration: str | None = None) -> dict:
        """
        Calculate the max pain strike for the given expiration.

        Method: For each possible strike S, compute:
          total_loss = SUM over all call strikes K: max(0, S - K) × call_OI(K)
                     + SUM over all put strikes K:  max(0, K - S) × put_OI(K)
        Max pain = strike S that minimises total_loss.

        Returns:
          {
            "expiration": "2026-04-25",
            "max_pain_strike": 22.0,
            "current_price": 22.45,
            "delta_to_max_pain": -0.45,
            "call_oi_total": 45000,
            "put_oi_total": 38000,
            "put_call_ratio": 0.84,
            "net_oi_bias": "calls"  # more OI on calls → bullish hedging demand
          }
        """
        chain = self.get_chain(expiration)
        if not chain:
            return {}

        calls = chain["calls"].copy()
        puts  = chain["puts"].copy()
        exp   = chain["expiration"]

        all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))

        call_oi = dict(zip(calls["strike"], calls["OI"].fillna(0)))
        put_oi  = dict(zip(puts["strike"],  puts["OI"].fillna(0)))

        min_loss  = float("inf")
        max_pain_strike = None

        for s in all_strikes:
            call_loss = sum(max(0, s - k) * call_oi.get(k, 0) for k in all_strikes)
            put_loss  = sum(max(0, k - s) * put_oi.get(k,  0) for k in all_strikes)
            total = call_loss + put_loss
            if total < min_loss:
                min_loss = total
                max_pain_strike = s

        # Current price
        try:
            hist = self._ticker.history(period="1d")
            current_price = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
        except Exception:
            current_price = 0.0

        total_call_oi = sum(call_oi.values())
        total_put_oi  = sum(put_oi.values())
        pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi else 0

        return {
            "expiration": exp,
            "max_pain_strike": max_pain_strike,
            "current_price": round(current_price, 2),
            "delta_to_max_pain": round(current_price - (max_pain_strike or 0), 2),
            "call_oi_total": int(total_call_oi),
            "put_oi_total":  int(total_put_oi),
            "put_call_ratio": pcr,
            "net_oi_bias": "puts" if pcr > 1 else "calls",
        }

    def top_strikes_by_oi(self, expiration: str | None = None, n: int = 10) -> dict:
        """
        Top N strikes by total open interest (calls + puts combined).
        These are the 'walls' that market makers defend.
        """
        chain = self.get_chain(expiration)
        if not chain:
            return {}

        calls = chain["calls"][["strike", "OI"]].rename(columns={"OI": "call_OI"})
        puts  = chain["puts"][["strike",  "OI"]].rename(columns={"OI": "put_OI"})
        merged = calls.merge(puts, on="strike", how="outer").fillna(0)
        merged["total_OI"] = merged["call_OI"] + merged["put_OI"]
        top = merged.nlargest(n, "total_OI")
        return {"expiration": chain["expiration"], "top_strikes": top.to_dict("records")}

    # ── Dark pool ─────────────────────────────────────────────────────────────

    def dark_pool_summary(self) -> dict:
        """
        FINRA ATS (Alternative Trading System) weekly data.
        Free but 2-week delayed. Real-time requires Unusual Whales/FlowAlgo.

        Returns summary of dark pool volume as % of total reported FINRA short volume.
        Note: FINRA 'short volume' ≠ short selling — it includes all dark pool prints.
        """
        try:
            import requests
            # FINRA short sale volume data — GME ticker
            # This endpoint returns CSV with daily short volume data
            url = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
            today = date.today()

            for days_back in range(0, 10):
                check_date = today - timedelta(days=days_back)
                if check_date.weekday() >= 5:  # skip weekends
                    continue
                date_str = check_date.strftime("%Y%m%d")
                resp = requests.get(url.format(date=date_str), timeout=10)
                if resp.status_code != 200:
                    continue

                lines = resp.text.strip().split("\n")
                header = lines[0].split("|")
                for line in lines[1:]:
                    parts = line.split("|")
                    if len(parts) < 4:
                        continue
                    if parts[0].upper() == SYMBOL:
                        short_vol = int(parts[1]) if parts[1].isdigit() else 0
                        total_vol = int(parts[2]) if parts[2].isdigit() else 0
                        short_pct = round(short_vol / total_vol * 100, 1) if total_vol else 0
                        return {
                            "date": check_date.isoformat(),
                            "short_volume": short_vol,
                            "total_volume": total_vol,
                            "short_pct": short_pct,
                            "note": "FINRA ATS dark pool proxy — 2-week delay. Not real-time.",
                            "upgrade": "Unusual Whales ($50/mo) or FlowAlgo for real-time dark pool prints",
                        }

            return {"note": "No recent FINRA data found — check back later"}
        except Exception as e:
            log.error(f"[options] Dark pool fetch failed: {e}")
            return {"error": str(e)}

    # ── DB persistence ────────────────────────────────────────────────────────

    def update_db(self, send_telegram: bool = True):
        """
        Fetch options data, persist to DB, and optionally push Telegram alert.
        Called by orchestrator on Monday mornings before market open.
        """
        mp = self.max_pain()
        if not mp:
            log.warning("[options] Could not compute max pain — yfinance may be down")
            return

        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT OR REPLACE INTO options_snapshots
               (timestamp, expiration, max_pain_strike, current_price, delta_to_max_pain,
                call_oi_total, put_oi_total, put_call_ratio, net_oi_bias)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(),
                mp["expiration"], mp["max_pain_strike"], mp["current_price"],
                mp["delta_to_max_pain"], mp["call_oi_total"], mp["put_oi_total"],
                mp["put_call_ratio"], mp["net_oi_bias"],
            ),
        )
        conn.commit()
        conn.close()

        log.info(
            f"[options] Max pain {mp['expiration']}: ${mp['max_pain_strike']} "
            f"| Current: ${mp['current_price']} | PCR: {mp['put_call_ratio']}"
        )

        if send_telegram:
            from notifier import notify_max_pain
            notify_max_pain(
                strike=mp["max_pain_strike"],
                current_price=mp["current_price"],
                friday_date=mp["expiration"],
                net_oi_direction=mp["net_oi_bias"],
            )

    def _nearest_friday_expiry(self, expirations: list[str]) -> str:
        """Find the closest expiration to the next Friday."""
        today = date.today()
        days_to_friday = (4 - today.weekday()) % 7
        next_friday = (today + timedelta(days=days_to_friday)).isoformat()

        for exp in expirations:
            if exp >= next_friday:
                return exp
        return expirations[0] if expirations else ""


# ── DB schema for options snapshots ──────────────────────────────────────────

def ensure_options_table():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS options_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT    NOT NULL,
            expiration       TEXT    NOT NULL,
            max_pain_strike  REAL,
            current_price    REAL,
            delta_to_max_pain REAL,
            call_oi_total    INTEGER,
            put_oi_total     INTEGER,
            put_call_ratio   REAL,
            net_oi_bias      TEXT,
            UNIQUE(expiration)
        );
    """)
    conn.commit()
    conn.close()


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ensure_options_table()
    feed = OptionsFeed()

    print("\n=== Expirations ===")
    exps = feed.get_expirations()
    print(exps[:8])

    print("\n=== Max Pain (nearest Friday) ===")
    mp = feed.max_pain()
    print(json.dumps(mp, indent=2))

    print("\n=== Top OI Strikes ===")
    top = feed.top_strikes_by_oi(n=5)
    print(json.dumps(top, indent=2))

    print("\n=== Dark Pool (FINRA) ===")
    dp = feed.dark_pool_summary()
    print(json.dumps(dp, indent=2))
