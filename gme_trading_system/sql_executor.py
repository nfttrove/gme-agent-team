"""Simple SQL and API executor for agents.

Agents output SQL/API calls as text; this module executes them.
Replaces the CrewAI tools system that Gemma doesn't support.
"""
import sqlite3
import os
import requests
from typing import Any

from circuit_breaker import get_breaker, CircuitOpenError

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")


def execute_select(query: str) -> list[dict]:
    """Execute SELECT query. Agents output these in their response."""
    if not query.strip().upper().startswith("SELECT"):
        return [{"error": "Only SELECT queries allowed"}]
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        return [{"error": str(e)}]


def execute_insert_update(query: str) -> str:
    """Execute INSERT or UPDATE query. Agents output these in their response."""
    q = query.strip().upper()
    if not (q.startswith("INSERT") or q.startswith("UPDATE")):
        return "Error: Only INSERT or UPDATE allowed"
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(query)
        conn.commit()
        conn.close()
        return "ok"
    except Exception as e:
        return f"Error: {e}"


def fetch_news(query: str = "GME") -> dict:
    """Fetch news from NewsAPI. Agents reference this in their output."""
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return {"error": "NEWS_API_KEY not set"}
    breaker = get_breaker("newsapi")
    try:
        url = "https://newsapi.org/v2/everything"
        params = {"q": query, "sortBy": "publishedAt", "language": "en"}
        resp = breaker.call(
            requests.get,
            url,
            params=params,
            headers={"X-Api-Key": api_key},
            timeout=5,
        )
        return resp.json()
    except CircuitOpenError:
        return {"error": "newsapi circuit open"}
    except Exception as e:
        return {"error": str(e)}
