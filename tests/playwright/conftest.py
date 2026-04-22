"""Playwright fixtures for signal dashboard tests."""
import pytest
import asyncio
import subprocess
import time
import os
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def dashboard_server():
    """Start dashboard API server."""
    cwd = Path(__file__).parent.parent.parent / "dashboard"
    proc = subprocess.Popen(
        ["python", "api_server.py"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)  # Wait for server to start
    yield "http://localhost:8000"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
async def browser():
    """Launch browser once per session."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        yield browser
        await browser.close()


@pytest.fixture
async def page(browser):
    """Create fresh page for each test."""
    context = await browser.new_context()
    page = await context.new_page()
    yield page
    await context.close()


@pytest.fixture
def db_path():
    """Return path to test database."""
    return Path(__file__).parent.parent.parent / "gme_trading_system" / "agent_memory.db"


@pytest.fixture
def signal_manager():
    """Return initialized SignalManager."""
    from gme_trading_system.signal_manager import SignalManager

    db_path = Path(__file__).parent.parent.parent / "gme_trading_system" / "agent_memory.db"
    return SignalManager(str(db_path))


@pytest.fixture(autouse=True)
def cleanup_test_signals(signal_manager):
    """Clean up test signals after each test."""
    yield
    # Cleanup: delete signals created by test agents
    import sqlite3
    conn = sqlite3.connect(signal_manager.db_path)
    try:
        conn.execute("DELETE FROM signal_feedback WHERE alert_id IN (SELECT id FROM signal_alerts WHERE agent_name LIKE 'Test%')")
        conn.execute("DELETE FROM signal_alerts WHERE agent_name LIKE 'Test%'")
        conn.commit()
    finally:
        conn.close()
