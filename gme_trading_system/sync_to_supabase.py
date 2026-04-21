#!/usr/bin/env python3
"""Sync price data from local SQLite to Supabase REST API."""
import sqlite3
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

db_path = "agent_memory.db"
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    print("Error: SUPABASE_URL and SUPABASE_KEY required in .env")
    exit(1)

# Fetch from SQLite
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

candles = conn.execute(
    "SELECT symbol, date, open, high, low, close, volume, vwap FROM daily_candles WHERE symbol='GME' ORDER BY date"
).fetchall()

ticks = conn.execute(
    "SELECT symbol, timestamp, open, high, low, close, volume, source FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 5000"
).fetchall()

conn.close()

headers = {
    "apikey": supabase_key,
    "Authorization": f"Bearer {supabase_key}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

# Sync candles
if candles:
    print(f"Syncing {len(candles)} candles...")
    candle_data = [dict(c) for c in candles]
    try:
        r = requests.post(
            f"{supabase_url}/rest/v1/daily_candles",
            headers=headers,
            json=candle_data,
            timeout=30,
        )
        if r.status_code in [200, 201]:
            print(f"✓ Synced {len(candles)} candles")
        else:
            print(f"✗ Candles: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"✗ Candles sync failed: {e}")

# Sync ticks
if ticks:
    print(f"Syncing {len(ticks)} ticks...")
    tick_data = [dict(t) for t in ticks]
    try:
        r = requests.post(
            f"{supabase_url}/rest/v1/price_ticks",
            headers=headers,
            json=tick_data,
            timeout=30,
        )
        if r.status_code in [200, 201]:
            print(f"✓ Synced {len(ticks)} ticks")
        else:
            print(f"✗ Ticks: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"✗ Ticks sync failed: {e}")

print("Done.")
