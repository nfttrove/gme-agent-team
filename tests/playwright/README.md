# Playwright Test Suite for Signal Dashboard

Automated regression tests for the GME signal dashboard, feedback form, and metrics computation.

## Setup

```bash
# Install Playwright and dependencies
pip install pytest pytest-asyncio pytest-playwright aiohttp

# Install browser binaries
playwright install chromium
```

## Running Tests

**All tests:**
```bash
pytest tests/playwright/ -v
```

**Dashboard tests only:**
```bash
pytest tests/playwright/test_signal_dashboard.py -v
```

**Feedback form tests:**
```bash
pytest tests/playwright/test_feedback_form.py -v
```

**Metrics tests:**
```bash
pytest tests/playwright/test_metrics_computation.py -v
```

**Single test:**
```bash
pytest tests/playwright/test_signal_dashboard.py::test_dashboard_loads -v
```

## Test Coverage

### Dashboard Display (6 tests)
- ✅ Dashboard loads
- ✅ Recent Signals tab exists
- ✅ Signals table displays data
- ✅ Confidence shown as percentage
- ✅ Metrics tab loads
- ✅ No network errors

### Feedback Form (5 tests)
- ✅ Form loads with fields
- ✅ Feedback submission works
- ✅ Feedback persists in database
- ✅ Form validation required
- ✅ Action dropdown has options

### Metrics Computation (7 tests)
- ✅ Win rate calculation (correct %)
- ✅ Execution rate (% of signals acted upon)
- ✅ P&L calculation (profit/loss per trade)
- ✅ Per-agent breakdown
- ✅ Metrics API endpoint
- ✅ Empty state handling
- ✅ Signal count accuracy

### Edge Cases (8 tests)
- ✅ Empty signals state (no data in table)
- ✅ Invalid alert ID error handling
- ✅ Missing form fields validation
- ✅ Zero confidence signals (0%)
- ✅ High confidence signals (100%)
- ✅ Negative P&L (losing trades)
- ✅ Large price values (999.99)
- ✅ Special characters in notes (XSS prevention)

### API Robustness (9 tests)
- ✅ Signals API returns valid structure
- ✅ Metrics API returns valid structure
- ✅ Feedback API validates required fields
- ✅ Concurrent request handling (5x parallel)
- ✅ API response times (<2s for metrics)
- ✅ Health check endpoint
- ✅ Feedback API accepts valid payload
- ✅ CORS header handling
- ✅ Invalid JSON error handling

**Total: 35 tests**

## Test Data

Tests use temporary signals created on-the-fly:
- Each test creates isolated test data
- No shared state between tests
- Database changes are ephemeral (can be cleaned up)

## CI/CD Integration

To run in CI:
```bash
# Install deps
pip install -r requirements.txt
pip install pytest pytest-asyncio pytest-playwright

# Run tests with headless browser
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pytest tests/playwright/ --tb=short
```

## Notes

- Dashboard server starts automatically (port 8000)
- Tests wait for server startup (2s timeout)
- Browser created once per session (reused across tests)
- Timeouts: 5s for network, 1.5s for render
- **Test data cleanup**: Automatic! Each test creates signals with `TestAgent` prefix, auto-cleaned after run
- Test isolation: No shared state between tests (each gets fresh browser context)
- Safe to run multiple times: Database cleanup fixture prevents accumulation
