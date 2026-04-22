import sqlite3
import os
import requests
from typing import Optional
from crewai.tools import BaseTool

from circuit_breaker import get_breaker, CircuitOpenError

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


class SQLQueryTool(BaseTool):
    name: str = "SQL Query"
    description: str = "Run read-only SQL SELECT queries on agent_memory.db to fetch price data, trends, predictions, and trade history."

    def _run(self, query: str) -> list:
        if not query.strip().upper().startswith("SELECT"):
            return [{"error": "Only SELECT queries are allowed"}]
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            cur.execute(query)
            rows = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            rows = [{"error": str(e)}]
        finally:
            conn.close()
        return rows


class SQLWriteTool(BaseTool):
    name: str = "SQL Write"
    description: str = "Run INSERT or UPDATE SQL on agent_memory.db. Use for writing comments, predictions, or analysis results."

    def _run(self, query: str) -> str:
        q = query.strip().upper()
        if not (q.startswith("INSERT") or q.startswith("UPDATE")):
            return "Error: Only INSERT or UPDATE queries allowed"
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(query)
            conn.commit()
            conn.close()
            return "ok"
        except Exception as e:
            return f"Error: {e}"


class NewsAPITool(BaseTool):
    name: str = "News API"
    description: str = (
        "Fetch the latest GME news headlines with pre-computed sentiment scores. "
        "Returns up to 15 articles from Google News, Finnhub, NewsAPI, and Alpha Vantage. "
        "Each article has: headline, source, sentiment (bullish/bearish/neutral), timestamp, url."
    )

    def _run(self, query: str = "GME GameStop") -> list:
        """Primary: Supabase edge function aggregating 4 news sources. Fallback: local Finnhub."""
        result = self._supabase_edge_news()
        if result and not (len(result) == 1 and "error" in result[0]):
            return result
        return self._finnhub_news()

    def _supabase_edge_news(self) -> list:
        supabase_url = os.getenv("SUPABASE_URL", "")
        supabase_key = os.getenv("SUPABASE_KEY", "")
        if not supabase_url or not supabase_key:
            return []
        breaker = get_breaker("supabase_edge")
        try:
            endpoint = f"{supabase_url.rstrip('/')}/functions/v1/gamestop-news"
            resp = breaker.call(
                requests.get,
                endpoint,
                headers={
                    "Authorization": f"Bearer {supabase_key}",
                    "apikey": supabase_key,
                },
                timeout=20,
            )
            if resp.status_code != 200:
                return [{"error": f"Edge function {resp.status_code}"}]
            data = resp.json()
            if not data.get("success"):
                return [{"error": data.get("error", "unknown")}]
            articles = data.get("news", [])[:15]
            return [
                {
                    "headline":  a.get("title", ""),
                    "source":    a.get("source", ""),
                    "sentiment": a.get("sentiment", "neutral"),   # bullish/bearish/neutral
                    "timestamp": a.get("timestamp", ""),
                    "url":       a.get("url", ""),
                    "summary":   a.get("description", ""),
                }
                for a in articles
                if a.get("title")
            ]
        except CircuitOpenError:
            return [{"error": "supabase_edge circuit open"}]
        except Exception as e:
            return [{"error": str(e)}]

    def _finnhub_news(self) -> list:
        api_key = os.getenv("FINNHUB_KEY") or os.getenv("FINHUB_KEY") or os.getenv("FINNHUB_API_KEY")
        if not api_key:
            return [{"headline": "GME: No news source configured", "sentiment": "neutral"}]
        breaker = get_breaker("finnhub")
        try:
            from datetime import datetime, timedelta
            today = datetime.now().strftime("%Y-%m-%d")
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            resp = breaker.call(
                requests.get,
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": "GME", "from": week_ago, "to": today, "token": api_key},
                timeout=10,
            )
            news = resp.json()
            return [
                {"headline": n.get("headline", ""), "source": n.get("source", ""), "sentiment": "neutral"}
                for n in news[:10]
            ]
        except CircuitOpenError:
            return [{"error": "finnhub circuit open"}]
        except Exception as e:
            return [{"error": str(e)}]


class PriceDataTool(BaseTool):
    name: str = "Price Data"
    description: str = "Fetch recent GME OHLCV price data from the local database or Yahoo Finance as fallback."

    def _run(self, lookback_days: int = 10) -> list:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            # compute_all expects oldest-first (indicators.py docstring); yfinance
            # fallback also returns ascending. Prior DESC order meant candles[-1]
            # was the OLDEST row, so SafetyGate saw a ~2-week-stale price and
            # every indicator (RSI, EMA, VWAP) was inverted.
            cur.execute(
                "SELECT * FROM daily_candles WHERE symbol='GME' "
                "ORDER BY date DESC LIMIT ?",
                (lookback_days,)
            )
            rows = [dict(r) for r in cur.fetchall()]
            rows.reverse()  # oldest-first for compute_all
        finally:
            conn.close()

        if not rows:
            return self._yfinance_fallback(lookback_days)
        return rows

    def _yfinance_fallback(self, days: int) -> list:
        try:
            import yfinance as yf
            ticker = yf.Ticker("GME")
            hist = ticker.history(period=f"{days}d")
            return [
                {
                    "date": str(idx.date()),
                    "open": row["Open"],
                    "high": row["High"],
                    "low": row["Low"],
                    "close": row["Close"],
                    "volume": row["Volume"],
                }
                for idx, row in hist.iterrows()
            ]
        except Exception as e:
            return [{"error": str(e)}]


class IndicatorTool(BaseTool):
    name: str = "Indicators"
    description: str = (
        "Returns pre-computed technical indicators for GME: price, VWAP, EMA(8/21/50), "
        "RSI(3/14), ATR(14), and whether price is above each level. "
        "Use this instead of calculating indicators yourself."
    )

    def _run(self, lookback_days: int = 30) -> dict:
        from indicators import compute_all
        raw = PriceDataTool()._run(lookback_days=lookback_days)
        candles = [
            {
                "open":   float(r.get("open", 0) or r.get("Open", 0)),
                "high":   float(r.get("high", 0) or r.get("High", 0)),
                "low":    float(r.get("low", 0)  or r.get("Low", 0)),
                "close":  float(r.get("close", 0) or r.get("Close", 0)),
                "volume": float(r.get("volume", 0) or r.get("Volume", 0)),
            }
            for r in raw if r.get("close") or r.get("Close")
        ]
        return compute_all(candles)


class TroveScoreTool(BaseTool):
    name: str = "Trove Score"
    description: str = (
        "Score stocks using the Trove deep-value framework (Valuation/Capital Structure/Quality pillars, max 100 pts). "
        "Input: comma-separated tickers, e.g. 'GME,VIPS,AAPL'. Max 10 tickers per call. "
        "Returns a ranked list with score, star rating, pillar breakdown, and immunity flags."
    )

    def _run(self, tickers: str) -> list:
        from trove import fetch, score, DEFAULT_WATCHLIST
        ticker_list = (
            [t.strip().upper() for t in tickers.split(",") if t.strip()]
            if tickers.strip()
            else DEFAULT_WATCHLIST[:10]
        )
        results = []
        for ticker in ticker_list[:10]:
            inp = fetch(ticker)
            if inp is None:
                continue
            r = score(inp)
            results.append({
                "ticker":       ticker,
                "score":        r["total"],
                "rating":       r["rating"],
                "pillar_A":     r["pillars"]["A"],
                "pillar_B":     r["pillars"]["B"],
                "pillar_C":     r["pillars"]["C"],
                "immunity":     f"{r['immunity_count']}/5",
                "net_cash_pct": f"{round(inp.net_cash_pct * 100, 1)}%",
                "altman_z":     inp.altman_z,
            })
        return sorted(results, key=lambda x: x["score"], reverse=True)
