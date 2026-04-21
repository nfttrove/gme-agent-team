"""
Twitter/X feed monitor for key GME-related accounts.

Tracks posts from:
  - @ryancohen       — GameStop CEO (any post is market-moving)
  - @larryvc         — Larry Cheng, GME board member, Vanguard partner
  - @michaeljburry   — Michael Burry ("The Big Short") — contrarian signals
  - @DeepFuckingValue / @TheRoaringKitty — Keith Gill (rare but explosive)
  - Any account you add to TRACKED_ACCOUNTS

Setup (X API v2 — Free tier):
  1. Go to https://developer.x.com/en/portal/dashboard
  2. Create a project → create an app → get Bearer Token
  3. Add to .env: X_BEARER_TOKEN=your-bearer-token
  Free tier: 500,000 tweets/month read. More than enough for monitoring 5 accounts.

  Alternative (no API key): The monitor falls back to polling nitter.poast.org
  (a public Nitter instance) if X_BEARER_TOKEN is not set.
  Note: Nitter instances can be unreliable — X API is recommended.

Signals generated:
  - Any post from @ryancohen or @larryvc → CRITICAL alert to Telegram
  - Posts from @michaeljburry mentioning GME/retail/markets → BEARISH signal
  - Keyword detection (GME, squeeze, moon, buy, sold) adjusts signal_type

Run:
    python twitter_monitor.py            # one-shot check, then exits
    From orchestrator: run_social_scan() every 15 minutes during market hours
"""
import logging
import os
import sqlite3
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

from circuit_breaker import get_breaker, CircuitOpenError

load_dotenv()

log = logging.getLogger(__name__)

X_BEARER_TOKEN  = os.getenv("X_BEARER_TOKEN", "")
SUPABASE_URL    = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

# Accounts to track — username: {display_name, signal_weight, alert_level}
TRACKED_ACCOUNTS = {
    "ryancohen": {
        "display": "Ryan Cohen (GME CEO)",
        "weight": 1.0,
        "alert_level": "CRITICAL",   # every post matters
        "keywords_bullish": [],        # any post is bullish for GME
        "keywords_bearish": ["sold", "selling", "reduce"],
    },
    "larryvc": {
        "display": "Larry Cheng (GME Board)",
        "weight": 0.9,
        "alert_level": "CRITICAL",
        "keywords_bullish": ["buy", "gme", "gamestop", "belief", "conviction"],
        "keywords_bearish": [],
    },
    "michaeljburry": {
        "display": "Michael Burry",
        "weight": 0.8,
        "alert_level": "IMPORTANT",
        "keywords_bullish": ["buy", "long", "undervalued", "opportunity"],
        "keywords_bearish": ["short", "overvalued", "bubble", "crash", "sell"],
    },
    "TheRoaringKitty": {
        "display": "Keith Gill (Roaring Kitty)",
        "weight": 1.0,
        "alert_level": "CRITICAL",   # any appearance triggers vol spike historically
        "keywords_bullish": [],
        "keywords_bearish": [],
    },
    "DeepFuckingValue": {
        "display": "Keith Gill (DFV)",
        "weight": 1.0,
        "alert_level": "CRITICAL",
        "keywords_bullish": [],
        "keywords_bearish": [],
    },
}

# Keywords that upgrade any post to BULLISH/BEARISH regardless of account
GLOBAL_BULLISH_KEYWORDS = ["gme", "gamestop", "squeeze", "moass", "buy", "moon", "long", "calls"]
GLOBAL_BEARISH_KEYWORDS = ["sold", "short", "puts", "crash", "bearish", "overvalued", "selling"]


# ── X API v2 client ────────────────────────────────────────────────────────────

class XAPIClient:
    _BASE = "https://api.twitter.com/2"

    def __init__(self, bearer_token: str):
        self._headers = {"Authorization": f"Bearer {bearer_token}"}

    def get_user_id(self, username: str) -> str | None:
        """Resolve @username → numeric user ID (needed for timeline endpoint)."""
        breaker = get_breaker("twitter")
        try:
            resp = breaker.call(
                requests.get,
                f"{self._BASE}/users/by/username/{username}",
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", {}).get("id")
            log.warning(f"[twitter] Failed to resolve @{username}: {resp.status_code}")
            return None
        except CircuitOpenError:
            log.warning(f"[twitter] circuit open for @{username}")
            return None

    def get_recent_tweets(self, user_id: str, since_id: str | None = None,
                          max_results: int = 5) -> list[dict]:
        """Fetch recent tweets for a user. Returns list of tweet dicts."""
        breaker = get_breaker("twitter")
        params = {
            "max_results": max_results,
            "tweet.fields": "created_at,text,public_metrics",
            "exclude": "retweets,replies",
        }
        if since_id:
            params["since_id"] = since_id

        try:
            resp = breaker.call(
                requests.get,
                f"{self._BASE}/users/{user_id}/tweets",
                headers=self._headers,
                params=params,
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("data", [])
            if resp.status_code == 429:
                log.warning("[twitter] Rate limit hit — backing off 15 minutes")
            else:
                log.warning(f"[twitter] Tweet fetch failed {resp.status_code}: {resp.text[:200]}")
            return []
        except CircuitOpenError:
            log.warning(f"[twitter] circuit open for user {user_id}")
            return []
        return []


# ── Supabase Edge Function client (TwitterAPI.io proxy) ──────────────────────

class SupabaseEdgeClient:
    """
    Calls the existing Supabase Edge Function at:
      {SUPABASE_URL}/functions/v1/twitter-search
    which proxies TwitterAPI.io. Rate-limited to ~1 req/16 min, 1000/day.
    Auth: apikey header with the Supabase service key.
    """

    def __init__(self, supabase_url: str, supabase_key: str):
        project_id = supabase_url.rstrip("/").split("//")[-1].split(".")[0]
        self._endpoint = f"https://{project_id}.functions.supabase.co/twitter-search"
        self._headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
        }

    def get_recent_tweets(self, username: str, limit: int = 5) -> list[dict]:
        try:
            resp = requests.get(
                self._endpoint,
                params={"query": f"from:{username}", "limit": limit},
                headers=self._headers,
                timeout=15,
            )
            if resp.status_code != 200:
                log.warning(f"[twitter/supabase] Edge function {resp.status_code}: {resp.text[:200]}")
                return []

            data = resp.json()
            # Handle both {tweets: [...]} and plain list responses
            tweets_raw = data.get("tweets", data) if isinstance(data, dict) else data
            if not isinstance(tweets_raw, list):
                return []

            results = []
            for t in tweets_raw[:limit]:
                results.append({
                    "id":         str(t.get("id", t.get("tweet_id", hash(t.get("text", ""))))),
                    "text":       t.get("text", t.get("full_text", "")),
                    "created_at": t.get("created_at", t.get("timestamp", datetime.utcnow().isoformat())),
                })
            return results
        except Exception as e:
            log.warning(f"[twitter/supabase] Edge call failed for @{username}: {e}")
            return []


# ── Nitter fallback (no API key needed) ──────────────────────────────────────

class NitterFallback:
    """
    Scrapes public Nitter instances as a fallback when X_BEARER_TOKEN is not set.
    Less reliable but requires no API key.
    """
    INSTANCES = [
        "https://nitter.poast.org",
        "https://nitter.privacydev.net",
    ]

    def get_recent_tweets(self, username: str, limit: int = 5) -> list[dict]:
        import re
        for instance in self.INSTANCES:
            try:
                url = f"{instance}/{username}/rss"
                resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    continue

                # Parse RSS XML minimally
                items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
                tweets = []
                for item in items[:limit]:
                    title_match = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
                    date_match  = re.search(r"<pubDate>(.*?)</pubDate>", item)
                    if title_match:
                        text = title_match.group(1).strip()
                        # Strip HTML tags
                        text = re.sub(r"<[^>]+>", "", text)
                        # Unescape HTML entities
                        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                        tweets.append({
                            "id": hash(text),
                            "text": text,
                            "created_at": date_match.group(1) if date_match else "",
                        })
                if tweets:
                    return tweets
            except Exception as e:
                log.debug(f"[twitter] Nitter {instance} failed: {e}")
        return []


# ── Main monitor class ─────────────────────────────────────────────────────────

class TwitterMonitor:
    def __init__(self):
        self._ensure_tables()
        self._user_id_cache: dict[str, str] = {}
        self._last_tweet_id: dict[str, str] = {}

        if X_BEARER_TOKEN:
            self._client = XAPIClient(X_BEARER_TOKEN)
            self._edge = None
            self._fallback = None
            log.info("[twitter] Using X API v2 (authenticated)")
        elif SUPABASE_URL and SUPABASE_KEY:
            self._client = None
            self._edge = SupabaseEdgeClient(SUPABASE_URL, SUPABASE_KEY)
            self._fallback = None
            log.info("[twitter] Using Supabase Edge Function → TwitterAPI.io")
        else:
            self._client = None
            self._edge = None
            self._fallback = NitterFallback()
            log.warning("[twitter] No API keys — using Nitter fallback (less reliable)")

    def scan_all(self) -> list[dict]:
        """Scan all tracked accounts for new posts. Returns list of new signal dicts."""
        all_signals = []
        for username in TRACKED_ACCOUNTS:
            try:
                signals = self._scan_account(username)
                all_signals.extend(signals)
                time.sleep(1)  # polite rate limit
            except Exception as e:
                log.error(f"[twitter] Error scanning @{username}: {e}")
        return all_signals

    def _scan_account(self, username: str) -> list[dict]:
        account = TRACKED_ACCOUNTS[username]
        signals = []

        if self._client:
            if username not in self._user_id_cache:
                uid = self._client.get_user_id(username)
                if not uid:
                    return []
                self._user_id_cache[username] = uid
            tweets = self._client.get_recent_tweets(
                self._user_id_cache[username],
                since_id=self._last_tweet_id.get(username),
            )
        elif self._edge:
            tweets = self._edge.get_recent_tweets(username)
        else:
            tweets = self._fallback.get_recent_tweets(username)

        for tweet in tweets:
            tweet_id = str(tweet.get("id", ""))
            text = tweet.get("text", "")
            created_at = tweet.get("created_at", datetime.now().isoformat())

            # Skip if already processed
            if self._already_stored(tweet_id):
                continue

            signal_type = self._classify_signal(username, text, account)

            self._store_tweet(username, tweet_id, text, created_at, signal_type)

            # Update last seen ID for pagination
            if tweet_id and (not self._last_tweet_id.get(username) or
                              tweet_id > self._last_tweet_id[username]):
                self._last_tweet_id[username] = tweet_id

            # Decide whether to notify
            if self._should_notify(username, signal_type, account):
                from notifier import notify_social_signal
                notify_social_signal(username, text, signal_type)
                log.info(f"[twitter] NOTIFIED: @{username} [{signal_type}]: {text[:80]}")

            signals.append({
                "username": username,
                "tweet_id": tweet_id,
                "text": text,
                "signal_type": signal_type,
                "created_at": created_at,
            })

        return signals

    def _classify_signal(self, username: str, text: str, account: dict) -> str:
        """Classify a tweet as CRITICAL, BULLISH, BEARISH, or INFO."""
        text_lower = text.lower()
        alert_level = account.get("alert_level", "INFO")

        # CRITICAL accounts always fire 🚨 regardless of content
        if alert_level == "CRITICAL":
            return "CRITICAL"

        # For other accounts: keyword-driven classification
        bullish_hits = sum(1 for kw in GLOBAL_BULLISH_KEYWORDS if kw in text_lower)
        bearish_hits = sum(1 for kw in GLOBAL_BEARISH_KEYWORDS if kw in text_lower)

        if bullish_hits > bearish_hits:
            return "BULLISH"
        if bearish_hits > bullish_hits:
            return "BEARISH"
        return "INFO"

    def _should_notify(self, username: str, signal_type: str, account: dict) -> bool:
        """Only push notifications that are actionable."""
        return signal_type in ("CRITICAL", "BULLISH", "BEARISH")

    def _already_stored(self, tweet_id: str) -> bool:
        if not tweet_id:
            return False
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id FROM social_posts WHERE tweet_id=?", (tweet_id,)
        ).fetchone()
        conn.close()
        return row is not None

    def _store_tweet(self, username: str, tweet_id: str, text: str,
                     created_at: str, signal_type: str):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT OR IGNORE INTO social_posts "
                "(timestamp, username, tweet_id, content, signal_type) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(), username, str(tweet_id), text, signal_type),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[twitter] DB store failed: {e}")

    def _ensure_tables(self):
        conn = sqlite3.connect(DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS social_posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                username    TEXT    NOT NULL,
                tweet_id    TEXT,
                content     TEXT,
                signal_type TEXT    DEFAULT 'INFO',
                UNIQUE(tweet_id)
            );
            CREATE INDEX IF NOT EXISTS idx_social_posts_username ON social_posts(username);
            CREATE INDEX IF NOT EXISTS idx_social_posts_timestamp ON social_posts(timestamp);
        """)
        conn.commit()
        conn.close()


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    monitor = TwitterMonitor()
    print(f"\nScanning {len(TRACKED_ACCOUNTS)} accounts...")
    results = monitor.scan_all()
    print(f"Found {len(results)} new posts")
    for r in results:
        print(f"  @{r['username']} [{r['signal_type']}]: {r['text'][:80]}")
