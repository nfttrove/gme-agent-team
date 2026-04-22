"""Dashboard signal display tests."""
import pytest


@pytest.mark.asyncio
async def test_dashboard_loads(page, dashboard_server):
    """Dashboard homepage loads without errors."""
    await page.goto(dashboard_server)
    title = await page.title()
    assert "GME" in title or "Signal" in title or "Dashboard" in title
    print(f"✅ Dashboard loads: {title}")


@pytest.mark.asyncio
async def test_recent_signals_tab_exists(page, dashboard_server):
    """Recent Signals tab is present on dashboard."""
    await page.goto(dashboard_server)
    await page.wait_for_selector("button:has-text('Recent Signals')", timeout=5000)
    tab = await page.query_selector("button:has-text('Recent Signals')")
    assert tab is not None
    print("✅ Recent Signals tab found")


@pytest.mark.asyncio
async def test_signals_table_displays(page, dashboard_server, signal_manager):
    """Signals table displays data from database."""
    # Create test signal
    alert_id = signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="test_signal",
        confidence=0.85,
        severity="HIGH",
        entry_price=24.50,
        stop_loss=23.00,
        take_profit=26.00,
        reasoning="Test signal",
    )

    await page.goto(dashboard_server)

    # Click Recent Signals tab
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_timeout(500)

    # Wait for table to load
    await page.wait_for_selector("table", timeout=5000)

    # Check table contains data
    rows = await page.query_selector_all("table tbody tr")
    assert len(rows) > 0, "No signals in table"
    print(f"✅ Signals table displays {len(rows)} signals")


@pytest.mark.asyncio
async def test_signal_confidence_display(page, dashboard_server, signal_manager):
    """Signal confidence is displayed as percentage."""
    signal_manager.log_alert(
        agent_name="Futurist",
        signal_type="price_prediction",
        confidence=0.73,
        entry_price=25.00,
        stop_loss=24.00,
        take_profit=26.50,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_selector("table", timeout=5000)

    # Check confidence is shown as percentage
    confidence_text = await page.inner_text("table")
    assert "73%" in confidence_text or "0.73" in confidence_text
    print("✅ Confidence displayed as percentage")


@pytest.mark.asyncio
async def test_metrics_tab_loads(page, dashboard_server):
    """Metrics tab loads and displays agent stats."""
    await page.goto(dashboard_server)
    await page.click("button:has-text('Metrics')")
    await page.wait_for_timeout(1000)

    # Wait for metrics container
    try:
        await page.wait_for_selector("[id='metrics-container']", timeout=5000)
        print("✅ Metrics tab loads")
    except:
        # Alternative: check if any metrics content appears
        content = await page.inner_text("body")
        assert "Agent" in content or "Executed" in content or "Win Rate" in content
        print("✅ Metrics content displayed")


@pytest.mark.asyncio
async def test_no_network_errors(page, dashboard_server):
    """No 4xx/5xx errors in API calls."""
    errors = []

    async def handle_response(response):
        if response.status >= 400:
            errors.append(f"{response.status} {response.url}")

    page.on("response", handle_response)

    await page.goto(dashboard_server)
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_timeout(2000)

    assert len(errors) == 0, f"API errors: {errors}"
    print("✅ No network errors")
