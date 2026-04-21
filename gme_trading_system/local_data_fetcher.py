"""
Local data fetcher for agent tasks.
Pre-fetches external data so agents don't need tool use (which Gemma 2:9b doesn't support).
This layer caches results and provides structured data to agents in task descriptions.
"""
import os
import json
import sqlite3
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

ET = ZoneInfo("America/New_York")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


class LocalDataFetcher:
    """Fetches and caches external data locally for agent use."""

    @staticmethod
    def fetch_latest_news(query: str = "GME") -> Dict[str, Any]:
        """Fetch latest news from local or external sources, cache results."""
        cache_key = f"news_{query}"

        # Try Supabase edge function first (aggregates 4 sources)
        try:
            supabase_url = os.getenv("SUPABASE_URL", "")
            supabase_key = os.getenv("SUPABASE_KEY", "")
            if supabase_url and supabase_key:
                endpoint = f"{supabase_url.rstrip('/')}/functions/v1/gamestop-news"
                resp = requests.get(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "apikey": supabase_key,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success"):
                        articles = data.get("news", [])[:10]
                        return {
                            "status": "ok",
                            "source": "supabase",
                            "articles": [
                                {
                                    "headline": a.get("title", ""),
                                    "source": a.get("source", ""),
                                    "sentiment": a.get("sentiment", "neutral"),
                                    "timestamp": a.get("timestamp", ""),
                                    "url": a.get("url", ""),
                                }
                                for a in articles if a.get("title")
                            ]
                        }
        except Exception as e:
            print(f"[LocalDataFetcher] Supabase news fetch failed: {e}")

        # Fallback: Finnhub API
        try:
            api_key = (
                os.getenv("FINNHUB_KEY") or
                os.getenv("FINHUB_KEY") or
                os.getenv("FINNHUB_API_KEY")
            )
            if api_key:
                resp = requests.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={"symbol": "GME", "token": api_key},
                    timeout=10,
                )
                if resp.status_code == 200:
                    articles = resp.json()[:10]
                    return {
                        "status": "ok",
                        "source": "finnhub",
                        "articles": [
                            {
                                "headline": a.get("headline", ""),
                                "source": a.get("source", ""),
                                "sentiment": "neutral",  # Finnhub doesn't provide sentiment
                                "timestamp": datetime.fromtimestamp(a.get("datetime", 0)).isoformat(),
                                "url": a.get("url", ""),
                            }
                            for a in articles if a.get("headline")
                        ]
                    }
        except Exception as e:
            print(f"[LocalDataFetcher] Finnhub news fetch failed: {e}")

        # Fallback: Return empty results with cache indicator
        return {
            "status": "no_data",
            "source": "cache_empty",
            "articles": [],
            "note": "Unable to fetch news. Using cached or zero results."
        }

    @staticmethod
    def fetch_current_price() -> Dict[str, Any]:
        """Fetch latest price tick from database."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT close, timestamp, volume FROM price_ticks WHERE symbol='GME' "
                "ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()

            if row:
                return {
                    "status": "ok",
                    "price": row["close"],
                    "timestamp": row["timestamp"],
                    "volume": row["volume"]
                }
        except Exception as e:
            print(f"[LocalDataFetcher] Current price fetch failed: {e}")

        return {"status": "no_data", "price": None, "timestamp": None}

    @staticmethod
    def fetch_daily_candles(symbol: str = "GME", days: int = 30) -> Dict[str, Any]:
        """Fetch last N days of OHLCV data."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                f"SELECT open, high, low, close, volume, timestamp FROM daily_candles "
                f"WHERE symbol=? ORDER BY timestamp DESC LIMIT ?",
                (symbol, days)
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()

            if rows:
                return {
                    "status": "ok",
                    "symbol": symbol,
                    "candles": rows,
                    "count": len(rows)
                }
        except Exception as e:
            print(f"[LocalDataFetcher] Daily candles fetch failed: {e}")

        return {"status": "no_data", "symbol": symbol, "candles": []}

    @staticmethod
    def fetch_agent_context(agent_names: Optional[List[str]] = None, limit: int = 5) -> Dict[str, Any]:
        """Fetch recent outputs from other agents for context."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            if agent_names:
                placeholders = ",".join("?" * len(agent_names))
                query = (
                    f"SELECT agent_name, content, timestamp FROM agent_logs "
                    f"WHERE agent_name IN ({placeholders}) "
                    f"ORDER BY timestamp DESC LIMIT ?"
                )
                cur.execute(query, agent_names + [limit])
            else:
                query = (
                    "SELECT agent_name, content, timestamp FROM agent_logs "
                    "ORDER BY timestamp DESC LIMIT ?"
                )
                cur.execute(query, (limit,))

            rows = [dict(r) for r in cur.fetchall()]
            conn.close()

            if rows:
                return {
                    "status": "ok",
                    "context": rows,
                    "count": len(rows)
                }
        except Exception as e:
            print(f"[LocalDataFetcher] Agent context fetch failed: {e}")

        return {"status": "no_data", "context": []}

    @staticmethod
    def fetch_synthesis_brief() -> str:
        """Fetch the latest Synthesis brief for use by other agents."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT content FROM agent_logs WHERE agent_name='Synthesis' "
                "AND task_type='synthesis' ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()

            if row and row["content"]:
                return row["content"]
        except Exception as e:
            print(f"[LocalDataFetcher] Synthesis brief fetch failed: {e}")

        return "No synthesis brief available yet."

    @staticmethod
    def format_for_agent(data: Dict[str, Any], context: str = "") -> str:
        """Format fetched data as a readable string for agent context."""
        lines = []

        if context:
            lines.append(f"=== {context} ===")

        if "articles" in data:
            lines.append(f"Latest news ({len(data.get('articles', []))} articles):")
            for article in data.get("articles", [])[:5]:
                headline = article.get("headline", "")[:80]
                sentiment = article.get("sentiment", "neutral")
                lines.append(f"  - {headline} [{sentiment}]")

        if "price" in data and data["price"]:
            lines.append(f"Current price: ${data['price']} at {data.get('timestamp', 'N/A')}")

        if "candles" in data:
            candles = data.get("candles", [])
            if candles:
                latest = candles[0]
                lines.append(
                    f"Latest candle: OHLC=${latest.get('open')}, "
                    f"${latest.get('high')}, ${latest.get('low')}, "
                    f"${latest.get('close')}"
                )

        if "context" in data:
            lines.append(f"Recent agent context ({len(data.get('context', []))} entries):")
            for ctx in data.get("context", [])[:3]:
                agent = ctx.get("agent_name", "Unknown")
                content = ctx.get("content", "")[:100]
                lines.append(f"  {agent}: {content}...")

        return "\n".join(lines) if lines else "No data available."


def inject_data_into_task(task_description: str, data_fetcher: LocalDataFetcher = None) -> str:
    """
    Inject pre-fetched data into a task description so agents don't need tool use.
    This solves the "Gemma 2:9b doesn't support tools" issue.
    """
    if not data_fetcher:
        data_fetcher = LocalDataFetcher()

    # Fetch relevant data
    if "news" in task_description.lower():
        news_data = data_fetcher.fetch_latest_news()
        task_description += "\n\n=== INJECTED: LATEST NEWS ===\n"
        task_description += data_fetcher.format_for_agent(news_data, "News Headlines")

    if "price" in task_description.lower() or "current" in task_description.lower():
        price_data = data_fetcher.fetch_current_price()
        task_description += "\n\n=== INJECTED: CURRENT PRICE ===\n"
        task_description += data_fetcher.format_for_agent(price_data, "Price Data")

    if "daily" in task_description.lower() or "candle" in task_description.lower():
        candle_data = data_fetcher.fetch_daily_candles()
        task_description += "\n\n=== INJECTED: DAILY CANDLES ===\n"
        task_description += data_fetcher.format_for_agent(candle_data, "Daily OHLCV Data")

    if "synthesis" in task_description.lower() or "consensus" in task_description.lower():
        brief = data_fetcher.fetch_synthesis_brief()
        task_description += f"\n\n=== INJECTED: SYNTHESIS BRIEF ===\n{brief}"

    if "context" in task_description.lower():
        context_data = data_fetcher.fetch_agent_context()
        task_description += "\n\n=== INJECTED: AGENT CONTEXT ===\n"
        task_description += data_fetcher.format_for_agent(context_data, "Recent Agent Outputs")

    return task_description
