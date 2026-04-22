"""LLM configuration and routing.

Multi-model setup optimized for cost + reasoning quality:
  - Gemma 2:9b (local Ollama)    — Fast, cheap, default for all agents
  - DeepSeek-r1:8b (local Ollama) — Deep reasoning (Futurist, CTO)
  - Gemini Flash (Google)         — Rate limit fallback for Gemma
  - Gemini Pro (Google)           — Complex reasoning fallback

Routing strategy:
  - Simple agents (Valerie, Chatty, Newsie, Pattern, Trendy, GeoRisk, Synthesis)
    → Gemma 2:9b (0.1 temp, deterministic)
  - Complex reasoning agents (Futurist, CTO)
    → DeepSeek-r1:8b (0.3 temp, deep thought)
  - Fallback for rate limits or errors
    → Gemini Flash → Gemini Pro
"""
import os
import logging
import time
from crewai import LLM
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


# ── Fallback configuration ─────────────────────────────────────────────────────

class FallbackConfig:
    MAX_RETRIES = 2
    RETRY_DELAY_SEC = 5
    RATE_LIMIT_THRESHOLD = 429  # HTTP status or API error code

# ── Local models (Ollama) ──────────────────────────────────────────────────────

ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")

gemma_local = LLM(
    model="ollama/gemma2:9b",
    base_url=ollama_host,
    temperature=0.1,  # Deterministic for consistency
)

deepseek_r1_local = LLM(
    model="ollama/deepseek-r1:8b",
    base_url=ollama_host,
    temperature=0.3,  # Allow exploration for reasoning
)

# ── Google Gemini (fallback) ───────────────────────────────────────────────────

gemini_flash = LLM(
    model="gemini/gemini-2.5-flash",
    api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0,
)

gemini_pro = LLM(
    model="gemini/gemini-2.5-pro",
    api_key=os.getenv("GOOGLE_API_KEY"),
    temperature=0.3,
)


# ── Routing function ───────────────────────────────────────────────────────────

def get_llm_for_agent(agent_name: str) -> LLM:
    """
    Route agent to best LLM based on reasoning complexity.

    Agent reasoning tiers:
      Tier 1 (simple): Valerie, Chatty, Newsie, Pattern, Trendy, GeoRisk, Synthesis
      Tier 2 (complex): Futurist (calibrate predictions), CTO (PE playbook)
      Tier 3 (orchestration): Boss (daily briefing, strategy)

    Selection:
      - Tier 2+ → DeepSeek-r1:8b (deep reasoning for signal quality)
      - Others → Gemma 2:9b (fast, cheap)
      - Fallback chain: Ollama → Gemini Flash → Gemini Pro
    """
    complex_reasoning_agents = {
        "Futurist",  # Price prediction needs calibration + reasoning
        "CTO",       # PE playbook analysis (structural complexity)
        "Boss",      # Daily briefing synthesizes all agents
    }

    if agent_name in complex_reasoning_agents:
        log.info(f"[llm_config] {agent_name} → DeepSeek-r1:8b (complex reasoning)")
        return deepseek_r1_local
    else:
        log.info(f"[llm_config] {agent_name} → Gemma 2:9b (fast, deterministic)")
        return gemma_local


# ── Fallback chain (for ResilientAgent) ────────────────────────────────────────

def get_llm_fallback_chain(agent_name: str) -> list:
    """
    Return ordered list of LLMs to try if primary fails.

    Prioritizes local Ollama models. Includes Gemini only if API key available.
    """
    primary = get_llm_for_agent(agent_name)
    fallbacks = []

    # If primary is Gemma, fallback to DeepSeek (better reasoning)
    # If primary is DeepSeek, fallback to Gemma (faster)
    if primary.model == "ollama/gemma2:9b":
        fallbacks.append(deepseek_r1_local)
    else:
        fallbacks.append(gemma_local)

    # Only add Gemini if API key is configured
    if os.getenv("GOOGLE_API_KEY"):
        fallbacks.extend([gemini_flash, gemini_pro])

    return [primary] + fallbacks


# ── Agent LLM assignments (reference) ──────────────────────────────────────────

AGENT_LLM_ASSIGNMENT = {
    # Tier 1: Fast agents (Gemma 2:9b)
    "Valerie": "Gemma 2:9b (data validation)",
    "Chatty": "Gemma 2:9b (commentary)",
    "Newsie": "Gemma 2:9b (sentiment extraction)",
    "Pattern": "Gemma 2:9b (pattern detection)",
    "Trendy": "Gemma 2:9b (trend analysis)",
    "GeoRisk": "Gemma 2:9b (risk scoring)",
    "Synthesis": "Gemma 2:9b (consensus brief)",
    # Tier 2: Complex reasoning (DeepSeek-r1:8b)
    "Futurist": "DeepSeek-r1:8b (price prediction + calibration)",
    "CTO": "DeepSeek-r1:8b (PE playbook + structural analysis)",
    "Boss": "DeepSeek-r1:8b (daily strategy + review)",
}
