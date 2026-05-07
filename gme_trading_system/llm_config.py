"""LLM configuration and routing.

Single-local-model setup:
  - Gemma 2:9b (local Ollama)  — All agents
  - Gemini Flash (Google)      — Cloud fallback for Ollama outages / 429s
  - Gemini Pro (Google)        — Final fallback

We previously routed Futurist/CTO/Boss to DeepSeek-r1:8b for deeper reasoning,
but loading two models in one Ollama runner caused VRAM thrash and request
queue starvation. Pinning everything to Gemma keeps the runner hot and
predictable.
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
    """All agents route to Gemma 2:9b. Cloud fallback is Gemini Flash → Pro."""
    log.info(f"[llm_config] {agent_name} → Gemma 2:9b")
    return gemma_local


# ── Fallback chain (for ResilientAgent) ────────────────────────────────────────

def get_llm_fallback_chain(agent_name: str) -> list:
    """Return ordered list of LLMs to try if primary fails: Gemma → Flash → Pro."""
    chain = [gemma_local]
    if os.getenv("GOOGLE_API_KEY"):
        chain.extend([gemini_flash, gemini_pro])
    return chain
