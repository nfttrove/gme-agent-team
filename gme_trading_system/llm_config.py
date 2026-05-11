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


# ── Single-shot generate (bypasses CrewAI) ────────────────────────────────────

def llm_generate(prompt: str, num_predict: int = 200, temperature: float = 0.2,
                 timeout: int = 45) -> str:
    """Synchronous LLM call with no CrewAI wrapping. STREAM_MODE-aware.

    Use this for the orchestrator's "CrewAI-bypass" cycles (Synthesis, GeoRisk,
    etc.) — Gemma can't tool-call, so those functions pre-fetch data and POST
    a fully-formed prompt. They previously hit Ollama unconditionally;
    STREAM_MODE now routes them to Gemini Flash so live streams don't drag
    them through swap.

    Returns the raw text response (whitespace-stripped). Raises on timeout
    or API error so the caller can log + fall back.
    """
    if STREAM_MODE:
        return llm_generate_gemini(
            prompt,
            model="gemini-2.5-flash-lite",
            num_predict=num_predict,
            temperature=temperature,
        )

    import requests
    r = requests.post(
        f"{ollama_host}/api/generate",
        json={
            "model": "gemma2:9b",
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": num_predict, "temperature": temperature},
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("response", "").strip()


# ── Gemini direct call via the modern google.genai SDK ────────────────────────

def llm_generate_gemini(
    prompt: str,
    model: str = "gemini-2.5-flash-lite",
    num_predict: int = 200,
    temperature: float = 0.2,
) -> str:
    """Direct Gemini call via the modern google.genai SDK.

    Use when you specifically want Gemini (not the local-vs-cloud route
    that llm_generate's STREAM_MODE switch decides). Single helper so
    every call site uses the new SDK consistently — the legacy
    `google.generativeai` package was deprecated as of 2026-04.

    `model='gemini-2.5-flash-lite'` is the cost-optimised default; pass
    `'gemini-2.5-flash'` for the full-tier model when you need grounding
    or higher quality. The lite tier disables internal "thinking" tokens,
    which is what we want for short generations (~80 num_predict) where
    thinking budget would otherwise eat the visible response.

    Returns the raw text response (whitespace-stripped). Raises on API
    error so the caller can log + fall back.
    """
    from google import genai as google_genai
    from google.genai import types as genai_types
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("llm_generate_gemini called but GOOGLE_API_KEY missing")
    client = google_genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max(num_predict * 4, 512),
        ),
    )
    return (resp.text or "").strip()


# ── Grounded generate (Gemini Flash + Google Search) ──────────────────────────

def llm_generate_grounded(prompt: str, num_predict: int = 200, temperature: float = 0.2,
                          timeout: int = 45) -> str:
    """Like llm_generate, but routes through Gemini 2.5 Flash with Google Search
    grounding when USE_GEMINI_GROUNDING is on. Falls back to llm_generate on
    flag-off or any error so callers never have to handle two paths.

    Free tier: 1,500 grounded calls/day. Newsie + GeoRisk together use ~31/day,
    so this stays free in practice; only token usage bills (~3-5p/day total).
    Lite doesn't support grounding — uses full Flash 2.5.
    """
    flag = os.getenv("USE_GEMINI_GROUNDING", "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return llm_generate(prompt, num_predict, temperature, timeout)
    try:
        from google import genai as google_genai
        from google.genai import types as genai_types
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            raise RuntimeError("USE_GEMINI_GROUNDING set but GOOGLE_API_KEY missing")
        client = google_genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max(num_predict * 4, 512),
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
            ),
        )
        text = (resp.text or "").strip()
        if not text:
            log.warning("[grounded] empty response, falling back to llm_generate")
            return llm_generate(prompt, num_predict, temperature, timeout)
        return text
    except Exception as e:
        log.warning(f"[grounded] failed ({e}), falling back to llm_generate")
        return llm_generate(prompt, num_predict, temperature, timeout)
