"""Circuit breaker pattern for external API calls.

Prevents cascading failures by tracking failures per service and opening the circuit
when a threshold is exceeded. Automatically attempts recovery via half-open state.
"""
import time
import logging
from enum import Enum
from threading import Lock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


class State(Enum):
    """Circuit breaker state."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"          # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitOpenError(Exception):
    """Raised when circuit is open and no call can be made."""
    pass


class CircuitBreaker:
    """Thread-safe circuit breaker for external service calls."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        half_open_calls: int = 1,
    ):
        """
        Args:
            name: Service identifier (e.g. 'telegram', 'supabase_edge')
            failure_threshold: Failures until open (default 5)
            recovery_timeout: Seconds until attempting recovery (default 60)
            half_open_calls: Successful calls needed to close (default 1)
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_calls = half_open_calls

        self._state = State.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_at = None
        self._lock = Lock()

    def call(self, func, *args, **kwargs):
        """Execute func(*args, **kwargs) if circuit allows. Raises CircuitOpenError if open."""
        with self._lock:
            if self._state == State.OPEN:
                if self._should_attempt_recovery():
                    self._state = State.HALF_OPEN
                    self._success_count = 0
                    log.info(f"[CB] {self.name} attempting recovery (half-open)")
                else:
                    raise CircuitOpenError(f"Circuit '{self.name}' is open")

        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure()
            raise

    def record_success(self):
        """Mark a successful call."""
        with self._lock:
            self._failure_count = 0
            if self._state == State.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_calls:
                    self._state = State.CLOSED
                    log.info(f"[CB] {self.name} recovered (closed)")

    def record_failure(self):
        """Mark a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_at = datetime.now(ET)

            if self._state == State.HALF_OPEN:
                self._state = State.OPEN
                log.warning(f"[CB] {self.name} failed recovery; reopening")
            elif self._state == State.CLOSED and self._failure_count >= self.failure_threshold:
                self._state = State.OPEN
                log.error(
                    f"[CB] {self.name} exceeded failure threshold ({self.failure_threshold}); opening"
                )

    def _should_attempt_recovery(self) -> bool:
        """Check if recovery timeout has elapsed."""
        if self._last_failure_at is None:
            return True
        elapsed = (datetime.now(ET) - self._last_failure_at).total_seconds()
        return elapsed >= self.recovery_timeout

    @property
    def state(self) -> State:
        """Current circuit state."""
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        """Number of consecutive failures."""
        with self._lock:
            return self._failure_count

    @property
    def last_failure_at(self) -> datetime | None:
        """Timestamp of the last failure."""
        with self._lock:
            return self._last_failure_at

    def __repr__(self) -> str:
        return f"CB({self.name}={self.state.value}, failures={self.failure_count})"


# Global registry of circuit breakers
_breakers: dict[str, CircuitBreaker] = {
    "gemini_flash": CircuitBreaker("gemini_flash", failure_threshold=5, recovery_timeout=60),
    "gemini_pro": CircuitBreaker("gemini_pro", failure_threshold=5, recovery_timeout=60),
    "gemini": CircuitBreaker("gemini", failure_threshold=5, recovery_timeout=60),
}
_breakers_lock = Lock()


def get_breaker(name: str) -> CircuitBreaker:
    """Get or create a circuit breaker by name."""
    global _breakers
    if name not in _breakers:
        with _breakers_lock:
            if name not in _breakers:  # double-check
                _breakers[name] = CircuitBreaker(name)
    return _breakers[name]


def list_breakers() -> dict[str, dict]:
    """Return snapshot of all circuit breaker states (for metrics/monitoring)."""
    result = {}
    for name, breaker in _breakers.items():
        state = breaker.state
        result[name] = {
            "state": state.value,
            "state_code": 0 if state == State.CLOSED else (1 if state == State.HALF_OPEN else 2),
            "failure_count": breaker.failure_count,
            "last_failure_at": breaker.last_failure_at.isoformat() if breaker.last_failure_at else None,
        }
    return result
