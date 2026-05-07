"""LLM configuration and routing.

Default: Gemma 2:9b local → Gemini Flash → Gemini Pro.

STREAM_MODE=1 flips the chain to cloud-primary: Gemini Flash → Pro. This is
for live OBS streams where Ollama's 9 GB resident footprint pushes a 16 GB
Mac into swap and inference latency blows up. Cloud calls cost fractions of
a cent each; far cheaper than a stuttering stream. Unset to return local.
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


# ── Stream mode ────────────────────────────────────────────────────────────────

STREAM_MODE = os.getenv("STREAM_MODE", "").strip() in {"1", "true", "yes", "on"}
if STREAM_MODE:
    log.info("[llm_config] STREAM_MODE=on — cloud-primary (Gemini Flash → Pro). "
             "Local Ollama stays idle and auto-unloads after ~5 min.")


# ── Routing function ───────────────────────────────────────────────────────────

def get_llm_for_agent(agent_name: str) -> LLM:
    """Primary LLM for an agent. STREAM_MODE flips local Gemma → cloud Flash."""
    if STREAM_MODE:
        log.info(f"[llm_config] {agent_name} → Gemini Flash (stream mode)")
        return gemini_flash
    log.info(f"[llm_config] {agent_name} → Gemma 2:9b")
    return gemma_local


# ── Fallback chain (for ResilientAgent) ────────────────────────────────────────

def get_llm_fallback_chain(agent_name: str) -> list:
    """Return ordered list of LLMs to try if primary fails.

    Default:    Gemma → Flash → Pro
    Stream:     Flash → Pro  (no local; Ollama is what we're freeing)
    """
    has_gemini = bool(os.getenv("GOOGLE_API_KEY"))
    if STREAM_MODE:
        chain = [gemini_flash]
        if has_gemini:
            chain.append(gemini_pro)
        return chain
    chain = [gemma_local]
    if has_gemini:
        chain.extend([gemini_flash, gemini_pro])
    return chain
