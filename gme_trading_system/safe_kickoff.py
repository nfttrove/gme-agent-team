"""Timeout-guarded wrapper for crew.kickoff().

CrewAI's kickoff() can hang indefinitely on slow/stuck LLM calls.
This wraps it in a thread with a hard timeout so schedulers don't pile up.

Also includes LLM fallback logic for rate limit (429) errors.
"""
import concurrent.futures
import logging
import time

log = logging.getLogger(__name__)


class CrewTimeout(Exception):
    """Raised when crew.kickoff() exceeds the timeout."""
    pass


def safe_kickoff(crew, timeout_seconds: int = 300, label: str = "crew"):
    """Run crew.kickoff() with a hard timeout.

    Args:
        crew: The Crew instance to kick off.
        timeout_seconds: Max seconds to wait. Default 5 minutes.
        label: Short name for logging (e.g. "futurist", "manager").

    Returns:
        The result of crew.kickoff() if completed in time.

    Raises:
        CrewTimeout: If kickoff doesn't complete within timeout_seconds.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(crew.kickoff)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            log.error(
                f"[safe_kickoff] {label} exceeded {timeout_seconds}s timeout — abandoning"
            )
            # Note: cannot reliably kill a running CrewAI thread. The future
            # will continue in the background but the scheduler moves on.
            raise CrewTimeout(
                f"Crew '{label}' exceeded {timeout_seconds}s timeout"
            )


def safe_kickoff_with_fallback(crew, agent_name: str, timeout_seconds: int = 300, label: str = "crew"):
    """Run crew.kickoff() with timeout + LLM fallback on rate limit errors.

    If crew fails with 429 (rate limit), rotates agent's LLM to fallback and retries.

    Args:
        crew: The Crew instance to kick off.
        agent_name: Name of agent (for LLM routing and logging).
        timeout_seconds: Max seconds per attempt. Default 5 minutes.
        label: Short name for logging.

    Returns:
        The result of crew.kickoff() if successful.

    Raises:
        CrewTimeout: If attempt exceeds timeout_seconds.
    """
    from llm_config import get_llm_fallback_chain, FallbackConfig

    llm_chain = get_llm_fallback_chain(agent_name)
    last_error = None

    for attempt, llm in enumerate(llm_chain):
        try:
            # Swap LLM on agent
            if hasattr(crew, 'agents'):
                for agent in crew.agents:
                    if hasattr(agent, 'llm'):
                        agent.llm = llm
                        log.info(f"[safe_kickoff_with_fallback] {agent_name} attempt {attempt + 1}: using {llm.model}")

            # Try to run crew
            return safe_kickoff(crew, timeout_seconds=timeout_seconds, label=label)

        except Exception as e:
            last_error = e
            error_str = str(e).lower()

            # Check if it's a rate limit error
            if '429' in str(e) or 'rate_limit' in error_str or 'quota' in error_str:
                if attempt < len(llm_chain) - 1:
                    log.warning(
                        f"[safe_kickoff_with_fallback] {agent_name} hit rate limit, "
                        f"retrying with {llm_chain[attempt + 1].model} after {FallbackConfig.RETRY_DELAY_SEC}s"
                    )
                    time.sleep(FallbackConfig.RETRY_DELAY_SEC)
                else:
                    log.error(f"[safe_kickoff_with_fallback] {agent_name} exhausted all LLM fallbacks")
                    raise
            else:
                # Non-rate-limit error, re-raise immediately
                raise

    # Fallback for any uncaught error
    if last_error:
        raise last_error
