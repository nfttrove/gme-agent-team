"""
Discord bot for GME trading system — Trove Score deep-value screening.

Setup (5 minutes):
  1. Go to https://discord.com/developers/applications → New Application
  2. Copy the bot token → add to .env as DISCORD_BOT_TOKEN=...
  3. Under OAuth2 → URL Generator: select "bot" scope + "applications.commands"
  4. Copy the generated URL, open it, select your server
  5. Run: python discord_bot.py

Usage in Discord:
  /trove                  — score default watchlist (27 tickers)
  /trove amc gme aapl    — score those tickers
  /trove vips            — score a single ticker
"""

import logging
import os
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
if not DISCORD_TOKEN:
    print("[discord_bot] ERROR: DISCORD_BOT_TOKEN not in .env")
    exit(1)

# ── Bot setup ──────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Discord bot logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"⚠️ Failed to sync commands: {e}")


# ── Trove Score command ────────────────────────────────────────────────────────

@bot.tree.command(name="trove", description="Score stocks with Trove deep-value framework (0-100 pts)")
@app_commands.describe(tickers="Space-separated tickers (e.g. 'amc gme aapl'). Leave empty for default watchlist.")
async def trove(interaction: discord.Interaction, tickers: str = ""):
    """Score tickers using Trove framework (Valuation/Capital/Quality)."""
    await interaction.response.defer()

    try:
        from trove import run_screen, DEFAULT_WATCHLIST

        ticker_list = (
            tickers.upper().split() if tickers.strip()
            else DEFAULT_WATCHLIST
        )

        embeds = []
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
        embeds.append(title_embed)

        # Result embeds (max 25 per message, Discord limit is 10 embeds per message)
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
                value=f"A: {r['pillar_A']:.0f}/30  |  B: {r['pillar_B']:.0f}/45  |  C: {r['pillar_C']:.0f}/25",
                inline=False,
            )
            embed.add_field(
                name="Immunity",
                value=shields or "None",
                inline=True,
            )
            embed.add_field(
                name="Key Metrics",
                value=(
                    f"NetCash: {r['net_cash_pct']}%\n"
                    f"Altman Z: {r['altman_z'] or 'N/A'}\n"
                    f"EV/FCF: {r['ev_fcf']:.1f}x"
                ),
                inline=True,
            )
            embeds.append(embed)

        # Send in batches (max 10 embeds per message)
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
    """Return embed color based on Trove Score."""
    if score >= 80:
        return discord.Color.gold()  # ★★★★★
    if score >= 65:
        return discord.Color.green()  # ★★★★☆
    if score >= 50:
        return discord.Color.blue()  # ★★★☆☆
    if score >= 35:
        return discord.Color.orange()  # ★★☆☆☆
    return discord.Color.red()  # ★☆☆☆☆ or lower


# ── Run bot ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    print(f"[discord_bot] Starting with token: {DISCORD_TOKEN[:20]}...")
    bot.run(DISCORD_TOKEN)
