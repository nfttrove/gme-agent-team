"""
Discord Bot — Complete GME trading system interface (mirrors Telegram functionality).

Commands:
  /help, /status, /balance, /ticks, /agents, /brief, /update, /halt, /resume,
  /frequency, /learn, /lessons, /trove

Chat: Send any message (no /) for LLM responses with trading context.

Setup:
  1. https://discord.com/developers/applications → New App
  2. Copy token → .env: DISCORD_BOT_TOKEN=...
  3. OAuth2 → "bot" + "applications.commands" scopes
  4. Invite to server
  5. python discord_bot.py
"""

import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
HALT_FILE = os.path.join(os.path.dirname(__file__), "halt.flag")

if not DISCORD_TOKEN:
    print("[discord_bot] ERROR: DISCORD_BOT_TOKEN not in .env")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── Database helpers ──────────────────────────────────────────────────────────

def _db_scalar(query: str, params=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(query, params or ()).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        log.error(f"[discord] DB query failed: {e}")
        return None


def _db_query(query: str, params=None) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params or ()).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"[discord] DB query failed: {e}")
        return []


def _db_write(query: str, params=None) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(query, params or ())
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.error(f"[discord] DB write failed: {e}")
        return False


# ── Trading halt/resume ────────────────────────────────────────────────────────

def is_halted() -> bool:
    return os.path.exists(HALT_FILE)


def set_halted(halted: bool):
    if halted and not os.path.exists(HALT_FILE):
        open(HALT_FILE, "w").close()
    elif not halted and os.path.exists(HALT_FILE):
        os.remove(HALT_FILE)


# ── Notification frequency ────────────────────────────────────────────────────

def _get_frequency() -> str:
    freq = _db_scalar("SELECT value FROM bot_settings WHERE key='notify_frequency'")
    return freq or "medium"


def _set_frequency(level: str) -> bool:
    _db_write(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('notify_frequency', ?)",
        (level,),
    )
    return True


# ── Agent refresh (from telegram_bot.py) ──────────────────────────────────────

def _run_agent_refresh() -> dict:
    try:
        from orchestrator import run_agents_sync
        results = run_agents_sync()
        return results or {}
    except Exception as e:
        log.error(f"[discord] Agent refresh failed: {e}")
        return {}


# ── LLM chat (from telegram_bot.py) ────────────────────────────────────────

def _build_context() -> str:
    try:
        price = _db_scalar(
            "SELECT close, timestamp FROM price_ticks WHERE symbol='GME' ORDER BY timestamp DESC LIMIT 1"
        )
        synthesis = _db_scalar("SELECT summary FROM agent_logs WHERE agent_name='synthesis' ORDER BY timestamp DESC LIMIT 1")
        return f"Latest GME price: ${price}\nLatest synthesis: {synthesis or 'N/A'}"
    except Exception:
        return "No context available."


def _ask_llm(question: str) -> str:
    """Query LLM with trading context."""
    try:
        from llm_config import get_llm
        llm = get_llm()
        context = _build_context()
        prompt = f"{context}\n\nUser question: {question}"
        response = llm.invoke(prompt)
        return response if isinstance(response, str) else str(response)
    except Exception as e:
        return f"LLM error: {str(e)[:200]}"


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Discord bot logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        log.error(f"[discord] Command sync failed: {e}")


@bot.event
async def on_message(message):
    """Handle free-form chat messages."""
    if message.author == bot.user:
        return
    if message.content.startswith("/"):
        await bot.process_commands(message)
        return
    if isinstance(message.channel, discord.DMChannel) or bot.user.mentioned_in(message):
        async with message.channel.typing():
            response = _ask_llm(message.content)
            chunks = [response[i:i+2000] for i in range(0, len(response), 2000)]
            for chunk in chunks:
                await message.reply(chunk)


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="Show all commands and capabilities")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📚 GME Trading Bot — Command Guide", color=discord.Color.blue())
    embed.add_field(
        name="System Commands",
        value="/status — system health, tick count, last agent activity\n"
              "/agents — last run time for each agent\n"
              "/ticks — price data received today\n"
              "/balance — live IBKR account balance\n",
        inline=False,
    )
    embed.add_field(
        name="Research & Intel",
        value="/brief — today's strategy brief from synthesis agent\n"
              "/update — force sync local data to Supabase now\n"
              "/trove [TICKERS] — deep-value Trove Score screen\n",
        inline=False,
    )
    embed.add_field(
        name="Trading Control",
        value="/halt — pause all new trades (risk override)\n"
              "/resume — re-enable trading\n"
              "/frequency [low|medium|high] — notification level\n",
        inline=False,
    )
    embed.add_field(
        name="Agent Learning",
        value="/learn \"<lesson>\" --why \"<reason>\" — teach agents a rule\n"
              "/lessons [topic] — show lessons agents learned\n",
        inline=False,
    )
    embed.add_field(
        name="💬 Interactive Chat",
        value="Send any message (or @mention bot) to ask:\n"
              "• Current GME price & analysis\n"
              "• Trading strategies & signals\n"
              "• Market & geopolitical context\n",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="status", description="System health and activity")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    tick_count = _db_scalar("SELECT COUNT(*) FROM price_ticks WHERE date(timestamp)=date('now')") or 0
    last_log = _db_scalar("SELECT agent_name || ': ' || task_type FROM agent_logs ORDER BY timestamp DESC LIMIT 1") or "none yet"
    halt_str = "🛑 HALTED" if is_halted() else "🟢 ACTIVE"
    freq = _get_frequency()

    embed = discord.Embed(title="📊 GME System Status", color=discord.Color.green() if not is_halted() else discord.Color.red())
    embed.add_field(name="Trading", value=halt_str, inline=True)
    embed.add_field(name="Ticks Today", value=str(tick_count), inline=True)
    embed.add_field(name="Notifications", value=freq, inline=True)
    embed.add_field(name="Last Agent", value=last_log, inline=False)
    embed.timestamp = datetime.now()
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="balance", description="Live IBKR account balance")
@bot.tree.command(name="ticks", description="Price data received today")
async def ticks_cmd(interaction: discord.Interaction):
    today = _db_scalar("SELECT COUNT(*) FROM price_ticks WHERE date(timestamp)=date('now')") or 0
    total = _db_scalar("SELECT COUNT(*) FROM price_ticks") or 0
    latest_price = _db_scalar("SELECT close FROM price_ticks ORDER BY timestamp DESC LIMIT 1") or 0.0

    embed = discord.Embed(title="📊 GME Tick Data", color=discord.Color.blue())
    embed.add_field(name="Ticks Today", value=str(today), inline=True)
    embed.add_field(name="Total Ticks", value=str(total), inline=True)
    embed.add_field(name="Latest Price", value=f"${latest_price:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="agents", description="Last run time for each agent")
async def agents_cmd(interaction: discord.Interaction):
    logs = _db_query(
        "SELECT DISTINCT agent_name, MAX(timestamp) as last_run FROM agent_logs GROUP BY agent_name ORDER BY last_run DESC"
    )
    embed = discord.Embed(title="🤖 Agent Activity", color=discord.Color.blue())
    for log in logs:
        embed.add_field(name=log["agent_name"], value=log["last_run"] or "never", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="brief", description="Today's strategy brief")
async def brief_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    synthesis = _db_scalar(
        "SELECT summary FROM agent_logs WHERE agent_name='synthesis' ORDER BY timestamp DESC LIMIT 1"
    )
    embed = discord.Embed(
        title="📋 Strategy Brief",
        description=synthesis or "No synthesis available yet.",
        color=discord.Color.blue(),
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="update", description="Force sync data to Supabase")
async def update_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        agent_results = _run_agent_refresh()
        embed = discord.Embed(title="📊 System Refresh Complete", color=discord.Color.green())
        for agent, result in agent_results.items():
            embed.add_field(name=agent.title(), value=(result or "N/A")[:200], inline=False)

        await interaction.followup.send(embed=embed)

        # Also sync Supabase
        try:
            from supabase_sync import _get_client, _load_state, sync_once
            client = _get_client()
            state = _load_state()
            state = sync_once(client, state)
            await interaction.channel.send("✅ Supabase sync complete.")
        except Exception as e:
            log.error(f"[discord] Supabase sync failed: {e}")
            await interaction.channel.send(f"⚠️ Sync warning: {str(e)[:100]}")
    except Exception as e:
        await interaction.followup.send(f"❌ Refresh failed: {str(e)[:200]}")


@bot.tree.command(name="halt", description="Pause all new trades")
async def halt_cmd(interaction: discord.Interaction):
    set_halted(True)
    embed = discord.Embed(title="🛑 Trading Halted", description="All new trades paused.", color=discord.Color.red())
    await interaction.response.send_message(embed=embed)
    log.warning("[discord] Trading halted by command")


@bot.tree.command(name="resume", description="Re-enable trading")
async def resume_cmd(interaction: discord.Interaction):
    set_halted(False)
    embed = discord.Embed(title="🟢 Trading Resumed", description="Trading re-enabled.", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)
    log.info("[discord] Trading resumed by command")


@bot.tree.command(name="frequency", description="View or set notification frequency")
@app_commands.describe(level="low | medium | high")
async def frequency_cmd(interaction: discord.Interaction, level: str = None):
    if not level:
        current = _get_frequency()
        embed = discord.Embed(
            title="🔔 Notification Frequency",
            description=f"Current: **{current}**\n\nOptions: `low` | `medium` | `high`",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)
    else:
        if level.lower() not in ("low", "medium", "high"):
            await interaction.response.send_message(f"❌ Invalid frequency: {level}. Use: low | medium | high")
            return
        _set_frequency(level.lower())
        embed = discord.Embed(
            title="✅ Notification Frequency Updated",
            description=f"Set to: **{level.lower()}**",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="learn", description="Teach agents a rule")
@app_commands.describe(claim="The lesson", reason="Why this matters")
async def learn_cmd(interaction: discord.Interaction, claim: str, reason: str):
    await interaction.response.defer()
    try:
        learn_script = os.path.join(os.path.dirname(__file__), "..", ".agent", "tools", "learn.py")
        result = subprocess.run(
            [sys.executable, learn_script, claim, "--why", reason],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            embed = discord.Embed(title="✅ Lesson Graduated", color=discord.Color.green())
            embed.add_field(name="Claim", value=claim, inline=False)
            embed.add_field(name="Why", value=reason, inline=False)
            await interaction.followup.send(embed=embed)
            log.info(f"[discord] Lesson learned: {claim}")
        else:
            await interaction.followup.send(f"⚠️ Learn failed: {result.stderr[:200]}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)[:200]}")
        log.error(f"[discord] /learn failed: {e}")


@bot.tree.command(name="lessons", description="Recall learned lessons")
@app_commands.describe(topic="Search topic (default: trading strategy)")
async def lessons_cmd(interaction: discord.Interaction, topic: str = "trading strategy"):
    await interaction.response.defer()
    try:
        recall_script = os.path.join(os.path.dirname(__file__), "..", ".agent", "tools", "recall.py")
        result = subprocess.run(
            [sys.executable, recall_script, topic],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout:
            text = result.stdout[:2000]
            chunks = [text[i:i+2000] for i in range(0, len(text), 2000)]
            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"📚 Lessons for: {topic}" if i == 0 else "📚 (continued)",
                    description=chunk,
                    color=discord.Color.blue(),
                )
                if i == 0:
                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.channel.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ No lessons found for: {topic}\n\nTeach one with: `/learn \"<lesson>\" --why \"<reason>\"`")
    except Exception as e:
        await interaction.followup.send(f"❌ Recall error: {str(e)[:200]}")
        log.error(f"[discord] /lessons failed: {e}")


@bot.tree.command(name="trove", description="Score stocks with Trove framework")
@app_commands.describe(tickers="Space-separated tickers (leave empty for default watchlist)")
async def trove_cmd(interaction: discord.Interaction, tickers: str = ""):
    await interaction.response.defer()
    try:
        from trove import run_screen, DEFAULT_WATCHLIST

        ticker_list = tickers.upper().split() if tickers.strip() else DEFAULT_WATCHLIST
        results = run_screen(ticker_list, max_tickers=20)

        if not results:
            await interaction.followup.send("❌ No data returned — check ticker symbols.")
            return

        # Title embed
        title_embed = discord.Embed(
            title="📊 Trove Score Rankings",
            description=f"Scored {len(results)} ticker(s) — Deep-value framework",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        title_embed.set_footer(text="A=Valuation · B=Capital · C=Quality")

        # Result embeds
        embeds = [title_embed]
        for i, r in enumerate(results[:10]):
            stars = r["rating"]
            shields = "🛡️" * r["immunity"]

            embed = discord.Embed(
                title=f"{i+1}. {r['ticker']}",
                description=f"{r['score']:.1f}/100  {stars}",
                color=_score_color(r["score"]),
            )
            embed.add_field(
                name="Pillars",
                value=f"A: {r['pillar_A']:.0f}/30 | B: {r['pillar_B']:.0f}/45 | C: {r['pillar_C']:.0f}/25",
                inline=False,
            )
            embed.add_field(name="Immunity", value=shields or "None", inline=True)
            embed.add_field(
                name="Metrics",
                value=f"NetCash: {r['net_cash_pct']}%\nAltman Z: {r['altman_z'] or 'N/A'}\nEV/FCF: {r['ev_fcf']:.1f}x",
                inline=True,
            )
            embeds.append(embed)

        # Send in batches
        for i in range(0, len(embeds), 10):
            batch = embeds[i : i + 10]
            if i == 0:
                await interaction.followup.send(embeds=batch)
            else:
                await interaction.channel.send(embeds=batch)

    except Exception as e:
        await interaction.followup.send(f"❌ Trove error: {str(e)[:200]}")
        log.error(f"[discord] /trove failed: {e}")


def _score_color(score: float) -> discord.Color:
    if score >= 80:
        return discord.Color.gold()
    if score >= 65:
        return discord.Color.green()
    if score >= 50:
        return discord.Color.blue()
    if score >= 35:
        return discord.Color.orange()
    return discord.Color.red()


# ── Run bot ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print(f"[discord_bot] Starting...")
    bot.run(DISCORD_TOKEN)
