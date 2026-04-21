"""Timeout-guarded wrapper for crew.kickoff().

CrewAI's kickoff() can hang indefinitely on slow/stuck LLM calls.
This wraps it in a thread with a hard timeout so schedulers don't pile up.
"""
import concurrent.futures
import logging

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
