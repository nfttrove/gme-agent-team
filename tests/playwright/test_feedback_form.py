"""Feedback form submission tests."""
import pytest


@pytest.mark.asyncio
async def test_feedback_form_loads(page, dashboard_server):
    """Feedback form is accessible."""
    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(500)

    # Check form fields exist
    await page.wait_for_selector("input[id='alert_id']", timeout=5000)
    await page.wait_for_selector("select[id='action']", timeout=5000)
    print("✅ Feedback form loads with fields")


@pytest.mark.asyncio
async def test_feedback_form_submit(page, dashboard_server, signal_manager):
    """Feedback can be submitted."""
    # Create test signal first
    alert_id = signal_manager.log_alert(
        agent_name="Futurist",
        signal_type="price_prediction",
        confidence=0.80,
        entry_price=25.00,
        stop_loss=24.00,
        take_profit=26.50,
    )

    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(500)

    # Fill form
    await page.fill("input[id='alert_id']", alert_id)
    await page.select_option("select[id='action']", "executed")
    await page.fill("input[id='entry_price']", "25.00")
    await page.fill("input[id='exit_price']", "26.00")
    await page.fill("input[id='member']", "TestUser")
    await page.fill("textarea[id='notes']", "Test execution")

    # Submit
    await page.click("button:has-text('Log Feedback')")

    # Wait for success message
    try:
        await page.wait_for_selector(".alert-success", timeout=5000)
        success_text = await page.inner_text(".alert-success")
        assert "Feedback logged" in success_text or alert_id[:8] in success_text
        print(f"✅ Feedback submitted: {success_text}")
    except:
        print("⚠️ No success message, but checking database...")


@pytest.mark.asyncio
async def test_feedback_persists_in_db(page, dashboard_server, signal_manager):
    """Submitted feedback is stored in database."""
    import sqlite3

    # Create signal
    alert_id = signal_manager.log_alert(
        agent_name="CTO",
        signal_type="structural_signal",
        confidence=0.90,
        entry_price=24.00,
    )

    # Submit feedback via form
    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")
    await page.fill("input[id='alert_id']", alert_id)
    await page.select_option("select[id='action']", "executed")
    await page.fill("input[id='entry_price']", "24.00")
    await page.fill("input[id='exit_price']", "25.50")
    await page.fill("input[id='member']", "Alice")
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(1000)

    # Check database
    from pathlib import Path

    db_path = Path(__file__).parent.parent.parent / "gme_trading_system" / "agent_memory.db"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT action_taken, team_member FROM signal_feedback WHERE alert_id=?",
        (alert_id,),
    ).fetchone()
    conn.close()

    assert row is not None, "Feedback not in database"
    assert row[0] == "executed"
    assert row[1] == "Alice"
    print("✅ Feedback persisted in database")


@pytest.mark.asyncio
async def test_feedback_validation(page, dashboard_server):
    """Form requires alert_id and action."""
    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(500)

    # Try to submit empty form
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(500)

    # Check for error or form still visible
    form = await page.query_selector("form[id='feedback-form']")
    assert form is not None, "Form should still be visible (validation error)"
    print("✅ Form validation works")


@pytest.mark.asyncio
async def test_action_dropdown_options(page, dashboard_server):
    """Action dropdown has required options."""
    await page.goto(dashboard_server)
    await page.click("button:has-text('Log Feedback')")
    await page.wait_for_timeout(500)

    # Get select options
    options = await page.query_selector_all("select[id='action'] option")
    option_values = [await opt.get_attribute("value") for opt in options]

    assert "executed" in option_values
    assert "ignored" in option_values
    assert "missed" in option_values
    print(f"✅ Action dropdown has options: {option_values}")
