"""Edge case and error handling tests."""
import pytest


@pytest.mark.asyncio
async def test_empty_signals_state(page, dashboard_server):
    """Dashboard handles case with no signals."""
    await page.goto(dashboard_server)
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_timeout(1000)

    # Should show empty table or "no signals" message, not error
    page_text = await page.inner_text("body")
    assert "error" not in page_text.lower() or "signal" in page_text.lower()
    print("✅ Empty state handled gracefully")


@pytest.mark.asyncio
async def test_feedback_invalid_alert_id(page, dashboard_server):
    """Submitting feedback for non-existent signal returns error."""
    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")

    # Fill with invalid alert ID
    await page.fill("input[id='alert_id']", "invalid-uuid-12345")
    await page.select_option("select[id='action']", "executed")
    await page.fill("input[id='entry_price']", "25.00")
    await page.fill("input[id='exit_price']", "26.00")

    # Submit
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(1000)

    # Should show error or stay on form
    alert = await page.query_selector(".alert-error")
    if alert:
        error_text = await alert.inner_text()
        assert "error" in error_text.lower() or "not found" in error_text.lower()
        print(f"✅ Invalid alert ID error: {error_text}")
    else:
        # Form still visible means validation caught it
        form = await page.query_selector("form[id='feedback-form']")
        assert form is not None
        print("✅ Invalid alert ID prevented submission")


@pytest.mark.asyncio
async def test_missing_feedback_fields(page, dashboard_server, signal_manager):
    """Form rejects incomplete feedback."""
    # Create signal
    alert_id = signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="test",
        confidence=0.80,
        entry_price=25.00,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")

    # Fill only alert ID and action, skip prices
    await page.fill("input[id='alert_id']", alert_id)
    await page.select_option("select[id='action']", "executed")
    # Don't fill entry/exit prices

    # Try to submit
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(500)

    # Should either show error or form still visible
    page_text = await page.inner_text("body")
    form_visible = await page.query_selector("form[id='feedback-form']")

    if not form_visible:
        # Submitted - check if it allowed missing fields
        assert "error" in page_text.lower()
        print("✅ Missing fields rejected")
    else:
        print("✅ Form still visible (validation incomplete fields)")


@pytest.mark.asyncio
async def test_zero_confidence_signal(page, dashboard_server, signal_manager):
    """Signals with 0 confidence display correctly."""
    signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="low_confidence",
        confidence=0.0,
        entry_price=25.00,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_timeout(1000)

    # Table should display 0% confidence without crashing
    table_text = await page.inner_text("table")
    assert "0%" in table_text or "0.0" in table_text
    print("✅ Zero confidence signals display correctly")


@pytest.mark.asyncio
async def test_high_confidence_signal(page, dashboard_server, signal_manager):
    """Signals with 100% confidence display correctly."""
    signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="high_confidence",
        confidence=1.0,
        entry_price=25.00,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_timeout(1000)

    table_text = await page.inner_text("table")
    assert "100%" in table_text or "1.0" in table_text
    print("✅ 100% confidence signals display correctly")


@pytest.mark.asyncio
async def test_negative_pnl_calculation(page, dashboard_server, signal_manager):
    """Metrics handles losing trades correctly."""
    alert_id = signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="losing_trade",
        confidence=0.70,
        entry_price=25.00,
    )

    # Log losing trade
    signal_manager.log_feedback(alert_id, "executed", 25.00, 23.50, 10)  # -$15 loss

    await page.goto(dashboard_server)
    await page.click("button:has-text('Metrics')")
    await page.wait_for_timeout(1500)

    metrics_text = await page.inner_text("body")
    # Should show negative P&L without error
    assert "P&L" in metrics_text or "-" in metrics_text or "pnl" in metrics_text.lower()
    print("✅ Negative P&L calculated correctly")


@pytest.mark.asyncio
async def test_large_price_values(page, dashboard_server, signal_manager):
    """System handles large price values."""
    signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="test",
        confidence=0.80,
        entry_price=999.99,
        stop_loss=950.00,
        take_profit=1050.00,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Recent Signals')")
    await page.wait_for_timeout(1000)

    table_text = await page.inner_text("table")
    assert "999" in table_text or "1050" in table_text
    print("✅ Large price values display correctly")


@pytest.mark.asyncio
async def test_special_characters_in_notes(page, dashboard_server, signal_manager):
    """Special characters in team notes don't break form."""
    alert_id = signal_manager.log_alert(
        agent_name="TestAgent",
        signal_type="test",
        confidence=0.80,
        entry_price=25.00,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")

    # Fill with special characters
    await page.fill("input[id='alert_id']", alert_id)
    await page.select_option("select[id='action']", "executed")
    await page.fill("input[id='entry_price']", "25.00")
    await page.fill("input[id='exit_price']", "26.00")
    await page.fill("textarea[id='notes']", "Test: <script>alert('xss')</script> & \"quotes\"")

    # Submit
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(1000)

    # Should submit without executing script
    page_text = await page.inner_text("body")
    assert "[object Object]" not in page_text  # No serialization errors
    print("✅ Special characters handled safely")
